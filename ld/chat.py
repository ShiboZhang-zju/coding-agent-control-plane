"""Conversational entry point for the single global LD (M4-D).

What this module is
-------------------
An **interaction and intent-routing layer**.  It turns one human sentence into
one validated :class:`~ld.chat_intent.ChatIntent`, and then renders the answer
deterministically from rows that already exist:

* Ledger (``WorkItem`` state / verdict) — task truth,
* ``LDGoalStore`` — goal advancement,
* Project Context — verified/known project knowledge.

What this module is NOT
-----------------------
* Not a second agent: it holds no execution capability.  It never dispatches a
  Worker, never approves/denies, never writes project knowledge and never
  fabricates a verdict.  The only state changes it can cause are
  ``submit_goal`` (create/reuse a bounded goal row, which the existing
  :class:`~ld.goal_runner.LDGoalRunner` then advances) and ``resume_goal``
  (the existing explicit human unwedge of a ``needs_human`` goal).
* Not a second execution path for console submissions either:
  :meth:`LDChat.publish_goal` is the *same* ``submit_goal`` write, reached from
  a structured form instead of a sentence.  It creates or reuses one Goal row
  and nothing else — the LD still has to submit the WorkItem itself.
* Not a second source of truth: it does not store progress.  Re-opening a
  conversation re-reads the Ledger, because an old chat summary is not current
  state.  ``CURRENT.md``-style narration can therefore always be regenerated.

Global conversations (UI-4)
--------------------------
One conversation may discuss several projects.  A conversation is either
**bound** (``ld_conversations.project_id`` set; created by ``ld chat
--project``, a console resume, ...) or **global** (``project_id`` NULL).  A
global conversation is never silently bound to a project by mentioning it: the
project of a turn is a property of *that turn* (``ChatIntent.project_id``), and
the previous turn's project is only a **hint** for resolving "这个项目".

That has a deliberate consequence: a *write* never picks its project from the
ambient hint.  It needs a bound conversation, an explicitly named registered
project, or a demonstrative reference with exactly one project in context
(:meth:`LDChat._write_project`).  "It was the most recently used project" is not
a reason to start work.

Why the human must confirm a resume
-----------------------------------
``needs_human`` means the control plane (or the model itself) decided a human
had to look.  A chat sentence is not that look.  ``resume_goal`` is therefore
staged as a ``pending_action`` and only applied when the *next* message is an
exact confirmation token — never inferred from the model's output.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from ld.chat_intent import (
    PORTFOLIO_INTENTS,
    READ_INTENTS,
    WRITE_INTENTS,
    ChatIntent,
    ChatIntentKind,
    ChatPolicyError,
    ChatValidationError,
    chat_intent_schema,
    parse_intent,
)
from ld.chat_store import LDChatStore, normalize_confirmation
from ld.goal_store import GoalStoreError, goal_identity

_SECURITY_DOMAINS = {"local", "shareable"}
_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{3,}|[\u4e00-\u9fff]{2,}")

#: How many transcript turns the classifier sees.  Bounded on purpose: the
#: conversation is a routing hint, never the source of a factual answer.
CONVERSATION_WINDOW = 6


class IntentModel:
    """Protocol-like boundary for the classifier (no agent framework)."""

    def classify(self, chat_input: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return one JSON-shaped M4-D intent."""


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def _stop_signature(goal: Mapping[str, Any]) -> str:
    """A stable fingerprint of *why* a goal is paused.

    Deliberately ``sha256`` and not the builtin ``hash()``: ``hash()`` of a
    string is randomized per interpreter, so a value persisted in a staged
    action would never match after a restart.  This signature is compared in
    :meth:`LDChat._confirm_resume_goal`, so it must be reproducible across
    processes and machines.
    """
    last = goal.get("last_decision") if isinstance(goal.get("last_decision"), dict) \
        else {}
    payload = {
        "status": goal.get("status"),
        "stop": last.get("stop"),
        "action": last.get("action"),
        "next_state": last.get("next_state"),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


#: Characters per retained transcript turn in the classifier input.  Long
#: enough to keep the reference, short enough to bound the payload.
HISTORY_CHARS = 240

#: Demonstrative references that may resolve to the project discussed earlier in
#: *this* conversation ("这个项目", "继续刚才的任务").  Deliberately narrow: a write
#: is only allowed to inherit a project through one of these, never because a
#: project happens to be the most recently touched one in the ledger.
_PROJECT_REFERENCES = (
    "这个项目", "该项目", "此项目", "本项目", "同一个项目", "同一项目", "那个项目",
    "刚才那个", "刚刚那个", "刚才的项目", "上面的项目",
    "这个任务", "该任务", "这个目标", "该目标", "刚才的任务", "刚才的目标",
    "this project", "that project", "the project", "same project",
    "this task", "that task", "this goal", "that goal",
)


def _has_project_reference(message: str) -> bool:
    """True when the sentence points at a project previously discussed."""
    text = (message or "").lower()
    return any(marker in text for marker in _PROJECT_REFERENCES)


def _intent_from_dict(raw: Mapping[str, Any] | None) -> ChatIntent | None:
    """Rebuild a :class:`ChatIntent` from the dict a handler returned.

    Used only when a handler *re-mapped* the turn (a write that the routing
    refused is answered as a clarification).  The turn must be persisted and
    reported under the intent that actually happened, so this reconstructs the
    mapped intent rather than keeping the classifier's original one.  It never
    parses untrusted model output: the dict was built by this module.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        kind = ChatIntentKind(str(raw.get("intent")))
    except ValueError:
        return None
    parameters = raw.get("parameters")
    return ChatIntent(
        kind,
        raw.get("project_id"),
        raw.get("task_id"),
        raw.get("goal_id"),
        str(raw.get("reason") or "mapped by the LD"),
        dict(parameters) if isinstance(parameters, Mapping) else {})


#: Wording that asks about the *registry* rather than about one project.
_REGISTRY_PHRASES = (
    "哪些项目", "所有项目", "项目列表", "几个项目", "有什么项目",
    "what projects", "which projects", "all projects", "list projects",
)


def _is_registry_question(message: str) -> bool:
    """True when the answer is the project list, which is not about one project.

    Used to exempt ``list_projects`` from the "ambiguous routing cannot answer"
    rule: an ambiguity in the sentence does not matter when the answer is the
    whole registry anyway.
    """
    text = (message or "").lower()
    return any(phrase in text for phrase in _REGISTRY_PHRASES)


def _is_portfolio_answer(kind, message: str) -> bool:
    """An answer that is legitimately about *every* project.

    Two cases, and both are checked against the sentence rather than trusted
    from the classifier:

    * ``portfolio_status`` — its whole subject is the set of projects, so an
      ambiguous reference inside the sentence is irrelevant to it;
    * ``list_projects`` — only when the wording really asks for the registry,
      because an ambiguous sentence that merely got labelled ``list_projects``
      must still be refused rather than answered with a list.
    """
    from ld.chat_intent import ChatIntentKind as _Kind
    if kind is _Kind.PORTFOLIO_STATUS:
        return True
    if kind is _Kind.LIST_PROJECTS:
        return _is_registry_question(message)
    return False


#: Write intents that may only run against one *resolved* project.  ``clarify``
#: and the read intents are absent on purpose: reading a list of projects needs
#: no single target.
_PROJECT_BOUND_WRITES = frozenset(
    {ChatIntentKind.SUBMIT_GOAL, ChatIntentKind.RESUME_GOAL} & WRITE_INTENTS)


class ProjectRouting(NamedTuple):
    """Where a single turn's project came from, and how sure we are.

    ``source`` values, from strongest to weakest for *write* intents:

    * ``named``      — the sentence contains exactly one registered project's id
                       or name;
    * ``task``/``goal`` — the sentence contains a known task/goal id, whose owner
                       is authoritative;
    * ``reference``  — a demonstrative ("这个项目") with exactly one project
                       discussed in this conversation;
    * ``selected``   — the caller selected the project for this turn (bound
                       conversation or explicit ``project_id`` argument);
    * ``ambiguous``  — several projects could match, so ``candidates`` is
                       non-empty and ``project_id`` is ``None``;
    * ``none``       — nothing resolved it.

    Only the first four (plus ``selected``) may carry a write.  A turn whose
    routing is ``ambiguous`` or ``none`` must ask, never guess: "it was first in
    the list" / "we looked at it last" is not authority to start work.
    """

    project_id: str | None
    source: str
    candidates: tuple[str, ...] = ()


#: Hard ceiling for a goal objective.  A longer text is refused rather than
#: truncated: the objective *is* the goal's identity, so silently shortening it
#: would create a different goal than the one that was confirmed.
MAX_GOAL_CHARS = 2000

#: How many ledger work items one project may contribute to a report.  This is a
#: *safety* bound on a single read, not a "recent tasks" window: it is the largest
#: page the tools layer will serve, so an old rejection is still counted instead
#: of falling outside the 10-row ``recent_tasks`` window.  When the bound is
#: actually hit the report says so rather than presenting a partial count as
#: complete (see ``truncated`` in :meth:`LDChat._project_metrics`).
#:
#: It must not exceed the tools layer's own maximum: asking for more raises
#: ``ValueError``, and a swallowed exception here once turned every project's
#: task list into "empty" — the report then said "0 tasks" for a project with
#: rejected work.  A wrong number is worse than an error.
MAX_REPORTED_WORK_ITEMS = 200

#: Verdicts that mean "the control plane did not accept this artifact".  They are
#: *not* "blocked": nothing is waiting in a queue.  Mixing the two is what made
#: a global overview say "0 needing attention" while three rejected tasks and
#: five stopped goals were sitting in the ledger.
ABNORMAL_VERDICTS = ("rejected", "inconclusive")

#: Goal statuses that are finished, so they are not "unfinished work".
TERMINAL_GOAL_STATUSES = ("completed", "cancelled", "failed")

#: Wording a recorded stop reason uses when a goal exhausted its lifetime
#: budget.  Matched against the *recorded* text only — a goal is never called
#: "budget exhausted" because no other reason matched.
BUDGET_STOP_MARKERS = ("lifetime decision budget exhausted",
                       "budget exhausted",
                       "budget_exhausted",
                       "decision budget")
#: Stop reasons this module understands well enough to describe.  Anything else
#: is reported verbatim and marked 待核查 rather than folded into a known cause.
KNOWN_STOP_REASONS = ("request_human", "human_required") + BUDGET_STOP_MARKERS


def _is_budget_stop(goal: Mapping[str, Any]) -> bool:
    """True only when the *recorded* reason says the budget ran out.

    The previous rule treated "not a ``request_human``" as synonymous with
    "budget exhausted", which silently relabelled ``invalid_model_decision``
    and every future/unknown reason.  Absence of evidence is not evidence of a
    particular cause, so this returns False for anything it cannot read.
    """
    reason = str(goal.get("stop_reason") or "").lower()
    if not reason:
        return False
    return any(marker in reason for marker in BUDGET_STOP_MARKERS)


def _is_known_stop_reason(reason: str) -> bool:
    """Whether a recorded reason is one this module can name confidently."""
    lowered = str(reason or "").lower()
    return any(marker in lowered for marker in KNOWN_STOP_REASONS)

#: Markers that a sentence is quoting project knowledge, credentials or other
#: local-domain content.  This is a conservative *detector*, not a redactor:
#: when it fires and the model is not authorized for ``local``, the turn falls
#: back to the deterministic router instead of sending the text anywhere.
_LOCAL_CONTENT_MARKERS = (
    "密钥", "秘钥", "密码", "口令", "令牌", "token", "secret", "password",
    "apikey", "api key", "api_key", "credential", "私钥", "证书",
    "内网", "内网地址", "数据库连接", "连接串", "dsn", ".env",
    "本地部署", "部署地址", "生产环境地址", "localhost", "127.0.0.1",
    "10.", "192.168.", "172.16.",
)


def _looks_like_local_content(message: str) -> bool:
    """Extra tripwire: does this sentence obviously quote local content?

    This is **not** the authorization boundary, and must never be used as one.
    Sensitivity cannot be proven by a word list: "t53 的客户内部代号是蓝鲸七号"
    carries internal information and matches nothing here.  The boundary is the
    model's explicit ``local`` authorization (see
    :meth:`LDChat._classify`); this function only adds a second check on top of
    it, for the case where an operator deliberately opted into raw sharing.

    It never edits the text: "auto-redaction" of a sentence whose meaning
    depends on the redacted span produces a confidently wrong answer *and* hides
    from the operator that nothing was sent.
    """
    text = (message or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _LOCAL_CONTENT_MARKERS)


def _safe_history(messages: list[Mapping[str, Any]], *,
                  domains: tuple[str, ...]) -> list[dict[str, Any]]:
    """Build the classifier's transcript window without leaking content.

    The classifier only needs to know *what kind of thing* was just said.  What
    it must never receive is the body of an answer: ``knowledge_query`` renders
    project knowledge into the LD reply, and forwarding that reply verbatim to
    a model endpoint that is not authorized for ``local`` would be a leak that
    the per-field security projection (project names, prompts) does not cover.

    So an LD/system turn is reduced to its intent label plus the identifiers it
    referenced — never its prose.  The user's own turn is kept only when the
    model is authorized for ``local``; otherwise even that is reduced to a
    reference, because the owner routinely quotes project knowledge back ("那
    刚才那条部署约定是什么来着"): the quoted content would travel with the
    question, so the question is not automatically safe either.
    """
    local_ok = "local" in domains
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        intent = (message.get("intent") or {}).get("intent")
        if role == "user" and local_ok:
            content = str(message.get("content") or "")
            items.append({"role": "user", "content": content[:HISTORY_CHARS]})
            continue
        if role == "user":
            items.append({
                "role": "user",
                "content_omitted": True,
                "chars": len(str(message.get("content") or "")),
                "note": ("the owner's message text is withheld because this "
                         "model is not authorized for local-domain content, "
                         "which the owner may be quoting"),
            })
            continue
        items.append({
            "role": role,
            "intent": intent,
            "goal_ids": list(message.get("goal_ids") or [])[:5],
            "content_omitted": True,
            "note": ("the LD's own answer is omitted: it may contain project "
                     "knowledge or local-domain text"),
        })
    return items


class HeuristicIntentRouter:
    """Deterministic, model-free router used when no endpoint is configured.

    It exists so the conversational surface is usable (and testable) without an
    LLM.  It is deliberately shallow: it recognises the document-level intents
    and otherwise asks a clarifying question instead of guessing a write.
    ``LDChat`` labels every answer with ``intent_source`` so nobody mistakes a
    keyword match for a model decision.

    Safety ordering (M4-D audit P0-1) — the reason this is more than keyword
    matching:

    1. **A question is never a write.**  "这个项目的任务完成了吗？" contains 完成;
       a naive verb match turned it into a Goal that the Runtime then advanced.
       Interrogative form therefore wins over every verb, and a submit-looking
       question is answered as a status read.
    2. **A quoted or negated verb is not an instruction.**  "“实现”是什么意思"
       and "先不要推进" talk *about* a verb rather than issuing it.
    3. **Only then** may a verb mean "do it", and even then ``LDChat`` stages a
       model-proposed ``submit_goal`` for confirmation unless this router
       independently agrees the sentence is an imperative
       (:meth:`is_imperative_submission`).
    """

    _SUBMIT = ("继续推进", "继续完善", "推进", "完成", "实现", "开始做", "start",
               "continue", "implement", "build")
    _RESUME = ("恢复", "解封", "继续跑", "resume", "unblock")
    _KNOWLEDGE = ("为什么", "决策", "知识", "约定", "规范", "why", "decision",
                  "convention", "knowledge")
    _BLOCKERS = ("阻塞", "卡住", "需要我", "决定什么", "blocked", "blocker",
                 "attention")
    _GOAL = ("目标", "goal", "进度", "progress")
    _TASK = ("任务", "task")
    #: A question about the registry rather than about one project.
    _LIST_PROJECTS = ("哪些项目", "所有项目", "项目列表", "几个项目", "what projects",
                      "which projects", "all projects", "list projects")

    #: Unambiguous aggregation words: on their own they already mean "more than
    #: one project", so they are a portfolio request by themselves.
    _PORTFOLIO_STRONG = ("汇总", "各项目", "每个项目", "跨项目", "优先", "优先级",
                         "需要我", "要我处理", "需要人工", "待我", "介入",
                         "portfolio", "across projects", "every project",
                         "priorit", "needs me")
    #: Words that may mean "all projects" *or* merely introduce the topic.  They
    #: only count when a second signal says the sentence aggregates (e.g.
    #: "所有项目有哪些未完成目标" vs "现在有哪些项目").  Recognising the bare
    #: "所有项目" as aggregation would turn the registry question into a report.
    _PORTFOLIO_WEAK = ("所有项目", "全部项目", "all projects")
    #: Second signals that make a weak phrase an aggregation request.
    _PORTFOLIO_SECOND = ("未完成", "未结束", "没完成", "还在跑", "未达成", "遗留",
                         "阻塞", "卡住", "进度", "风险", "优先", "需要",
                         "unfinished", "outstanding", "blocked", "progress",
                         "risk", "priorit", "open goals", "attention")
    #: Phrases that select ``focus=goals`` / ``focus=attention``.
    _PORTFOLIO_GOALS = ("未完成", "未结束", "没完成", "还在跑", "未达成", "遗留",
                        "目标", "unfinished", "open goals", "outstanding", "goals")
    _PORTFOLIO_ATTENTION = ("需要我", "要我处理", "需要人工", "待我", "处理什么",
                            "介入", "阻塞", "卡住", "blocked", "needs me",
                            "attention")

    #: Interrogative markers (CJK and English).  Presence of any of these means
    #: the turn asks something; it can never be executed as an instruction.
    _QUESTION_MARKS = ("?", "？", "吗", "呢", "么", "是不是", "有没有", "如何",
                       "怎么", "怎样", "为什么", "哪些", "哪个", "什么", "多少",
                       "几", "谁", "是否", "能不能", "可不可以")
    _QUESTION_EN = ("what", "which", "who", "when", "where", "why", "how",
                    "is ", "are ", "does ", "did ", "has ", "have ", "can ",
                    "could ", "should ", "any ")
    #: Quoting or meta-discussion of a verb ("“实现”是什么意思", 'the word implement').
    _QUOTE_MARKS = ("“", "”", "「", "」", "‘", "’", '"', "'", "的意思", "是什么意思",
                    "quote", "quoted", "word ")
    #: Negation / deferral / prohibition words.  This list cannot be exhaustive
    #: (暂缓/请勿/禁止 were all missing), which is why :meth:`is_negated` also
    #: applies the structural rules below.  Treat this as the fast path, not as
    #: the definition.
    _NEGATIONS = ("不要", "不用", "别", "先别", "暂不", "暂时不", "暂缓", "暂缓推进",
                  "不需要", "不必", "请勿", "勿要", "勿", "禁止", "严禁", "不得",
                  "不准", "不许", "停止", "取消", "暂停", "搁置", "搁",
                  "don't", "do not", "not now", "never", "stop", "hold off",
                  "on hold", "defer")
    #: Structural prohibition stems: a prohibition character standing directly in
    #: front of a submit verb ("禁止推进", "请勿继续推进", "暂缓推进", "搁置实现")
    #: is caught even when the exact wording is not in ``_NEGATIONS``.
    _PROHIBITION_STEMS = ("禁", "勿", "缓", "停", "搁置", "搁", "免", "罢")
    #: Phrases that make a sentence a new instruction rather than a question
    #: about an existing goal (imperative voice).
    _IMPERATIVE_EN = ("please ", "go ahead", "let's ", "lets ")

    def classify(self, chat_input: Mapping[str, Any]) -> Mapping[str, Any]:
        message = str(chat_input.get("message") or "")
        lowered = message.lower()
        projects = list(chat_input.get("projects") or [])
        conversation = chat_input.get("conversation") or {}
        project_id = self._routed_project(message, projects, conversation)
        task_id = self._match_id(message, chat_input.get("known_task_ids") or [])
        goal_id = (conversation.get("selected_goal_id")
                   or chat_input.get("selected_goal_id"))

        is_question = self.is_question(message)
        mentions_verb = any(word in lowered for word in self._SUBMIT)

        routing = conversation.get("project_routing") or {}
        candidates = [str(item) for item in routing.get("candidates") or []]
        if routing.get("source") == "ambiguous" and mentions_verb and not is_question:
            # Several projects could be meant and the owner asked for work.  The
            # deterministic answer is "which one?", never the first candidate.
            return self._intent(
                "clarify",
                reason="ambiguous project for a write request",
                parameters={"question": (
                    "这可能指向多个项目（" + "、".join(candidates) + "）。"
                    "我没有创建任何目标。请指明项目名称或 ID。")})
        # 1. Resume is a read-only staging request, and safe to recognise before
        #    the question guard (it never applies without an explicit confirm).
        if (not is_question
                and any(word in lowered for word in self._RESUME) and project_id
                and goal_id):
            return self._intent("resume_goal", project_id=project_id,
                                goal_id=goal_id, reason="keyword: resume")
        # 2. A cross-project request is a read that needs *no* single project.
        #    Checked before the question branch because these requests are
        #    usually phrased as questions ("所有项目有哪些未完成目标？") and the
        #    question branch would otherwise answer them as a clarification or
        #    as one project's status.  Only fires when no project resolved: an
        #    explicit "ACP-Development 的汇总" stays a single-project read.
        if self.is_portfolio_request(message) and not project_id:
            return self._intent(
                "portfolio_status", reason="aggregation over all projects",
                parameters={"focus": self._portfolio_focus(message)})
        # 3. A question about a verb is answered by reading state, never by
        #    creating work.  This is the P0-1 fix.
        if is_question:
            return self._read_for_question(message, project_id=project_id,
                                          task_id=task_id,
                                          mentions_verb=mentions_verb)
        if self.is_quoted_reference(message) and project_id:
            return self._intent(
                "knowledge_query", project_id=project_id,
                reason="reference to a term, not an instruction",
                parameters={"keywords": message})
        if any(word in lowered for word in self._KNOWLEDGE) and project_id:
            return self._intent(
                "knowledge_query", project_id=project_id,
                reason="keyword: knowledge",
                parameters={"keywords": message})
        if any(word in lowered for word in self._BLOCKERS) and project_id:
            return self._intent("explain_blockers", project_id=project_id,
                                reason="keyword: blockers")
        if task_id:
            return self._intent("task_status", project_id=project_id,
                                task_id=task_id, reason="keyword: task id")
        # 3. A verb only becomes an instruction after the guards above.  A verb
        #    wrapped in a negation or a condition is neither an instruction nor
        #    a question: ask which it is instead of guessing either way.  Note
        #    that the refusal is *not* silently downgraded to a status read —
        #    "先不要在未审批通过前继续推进" is about starting work, so answering
        #    "here is the project status" would misread the intent.
        if mentions_verb and project_id:
            if self.is_negated(message) or self.is_conditional(message):
                return self._intent(
                    "clarify", project_id=project_id,
                    reason="instruction with a negation or a condition",
                    parameters={"question": (
                        "This sounds like it both mentions advancing the project "
                        "and qualifies it (a negation or a precondition), so I did "
                        "not start anything. If you want work to start, say it "
                        "directly ('推进 <项目>：<目标>'); if you want it held, say "
                        "'先不要推进'.")})
            return self._intent("submit_goal", project_id=project_id,
                                reason="keyword: advance the project",
                                parameters={"goal": self._objective(message)})
        if project_id and any(word in lowered for word in self._GOAL):
            return self._intent("goal_status", project_id=project_id,
                                reason="keyword: goal status")
        if project_id and any(word in lowered for word in self._TASK):
            return self._intent("project_status", project_id=project_id,
                                reason="keyword: project status")
        if mentions_verb and not project_id:
            # A write request that identifies no project is a question for the
            # human, not an excuse to read the project list or to pick one.  The
            # candidates come from the registry so the human can answer without
            # guessing at ids.
            known = "、".join(str(item.get("project_id")) for item in projects)
            return self._intent(
                "clarify", reason="write request without a resolved project",
                parameters={"question": (
                    "我需要知道这是哪个项目才能开始工作，因此没有创建任何目标。"
                    f"已注册项目：{known or '（无）'}。请说明项目名称或 ID。")})
        if project_id:
            return self._intent("project_status", project_id=project_id,
                               reason="default: project status")
        return self._intent("list_projects", reason="default: no project named")

    # -- guards --------------------------------------------------------- #
    @classmethod
    def is_question(cls, message: str) -> bool:
        """True when the sentence asks something rather than requesting work."""
        text = message or ""
        lowered = text.lower()
        if any(mark in text for mark in cls._QUESTION_MARKS):
            return True
        return any(marker in lowered for marker in cls._QUESTION_EN)

    @classmethod
    def is_quoted_reference(cls, message: str) -> bool:
        """True when a verb appears as a quoted term being discussed."""
        text = message or ""
        if any(mark in text for mark in cls._QUOTE_MARKS):
            return True
        lowered = text.lower()
        return any(f'"{word}"' in lowered for word in cls._SUBMIT)

    #: Conditions/permissions attached to an instruction.  "先不要在未审批通过前
    #: 继续推进" is not a refusal *and* not an unconditional request: it depends
    #: on an approval that has not happened.  Such a sentence must not create
    #: work on its own.
    _CONDITIONAL = ("如果", "假如", "若", "待", "等", "在未", "在……之前", "之前",
                    "审批", "批准", "通过后", "确认后", "unless", "if ", "after ",
                    "before ", "once ", "pending")

    @staticmethod
    def _directive_part(message: str) -> str:
        """The part of the sentence that *issues* the instruction.

        "在 t53chat 完成这件事：…，不要用 git_clean" has a directive head and an
        objective body; a negation inside the body constrains *how* the work is
        done, it does not refuse the work.  Splitting them is what lets the
        strict negation rule below coexist with well-specified requests.
        """
        text = (message or "").strip()
        for separator in ("：", ":"):
            head, _, tail = text.partition(separator)
            if head.strip() and tail.strip():
                return head
        return text

    @classmethod
    def is_negated(cls, message: str) -> bool:
        """True when the *directive* is negated or prohibited.

        This used to be a proximity window after a negation word (6 chars), which
        was fragile in exactly the way the re-audit predicted: "先不要在未审批
        通过前继续推进 t53 项目" puts 继续推进 more than six characters after 不要
        and would have become a real Goal.  Widening the window only moves the
        counterexample, so the rule is inverted: **any** negation/prohibition in
        the directive means "not an unconditional instruction", regardless of
        where the verb sits.

        Deliberately asymmetric: a false positive asks the human once, a false
        negative advances a project nobody asked to advance.
        """
        directive = cls._directive_part(message)
        lowered = directive.lower()
        if any(word in lowered for word in cls._NEGATIONS):
            return True
        # Structural fallback: "禁止推进" / "暂缓推进" / "请勿继续推进" — a
        # prohibition stem sitting immediately before the submit verb.  This is
        # what keeps the check from being a race between us and the vocabulary.
        for verb in cls._SUBMIT:
            index = lowered.find(verb)
            while index != -1:
                prefix = lowered[max(0, index - 4):index]
                if any(stem in prefix for stem in cls._PROHIBITION_STEMS):
                    return True
                index = lowered.find(verb, index + 1)
        return False

    @classmethod
    def is_conditional(cls, message: str) -> bool:
        """True when the *directive* is gated on something that has not happened."""
        lowered = cls._directive_part(message).lower()
        return any(word in lowered for word in cls._CONDITIONAL)

    @classmethod
    def is_imperative_submission(cls, message: str) -> bool:
        """Deterministic second opinion for a *model-proposed* ``submit_goal``.

        Returns True only when the sentence is unambiguously an instruction to
        do work: it names a submit verb, is not a question, not a quoted term,
        not negated and not conditional.  Anything else is staged for human
        confirmation rather than applied, so neither a model that over-eagerly
        reads a question as a request, nor a sentence with an ambiguous
        negation, can create work on its own.
        """
        text = message or ""
        lowered = text.lower()
        if cls.is_question(text) or cls.is_quoted_reference(text):
            return False
        if cls.is_negated(text) or cls.is_conditional(text):
            return False
        if any(word in lowered for word in cls._IMPERATIVE_EN):
            return True
        return any(word in lowered for word in cls._SUBMIT)

    @classmethod
    def _read_for_question(cls, message: str, *, project_id: str | None,
                           task_id: str | None,
                           mentions_verb: bool) -> dict[str, Any]:
        """The read-only answer a question deserves."""
        lowered = message.lower()
        if task_id:
            return cls._intent("task_status", project_id=project_id,
                               task_id=task_id, reason="question about a task id")
        if any(word in lowered for word in cls._BLOCKERS) and project_id:
            return cls._intent("explain_blockers", project_id=project_id,
                               reason="question: blockers")
        if any(word in lowered for word in cls._KNOWLEDGE) and project_id:
            return cls._intent(
                "knowledge_query", project_id=project_id,
                reason="question: knowledge",
                parameters={"keywords": message})
        if not project_id:
            # "现在有哪些项目？" asks for the registry itself; anything else
            # without a resolvable project cannot be answered truthfully, so it
            # asks instead of guessing one.
            if any(word in lowered for word in cls._LIST_PROJECTS):
                return cls._intent("list_projects",
                                   reason="question: which projects exist")
            return cls._intent(
                "clarify", reason="question without a project",
                parameters={"question": "Which project should I look at? Add "
                                        "its name or id and I will read its state."})
        if any(word in lowered for word in cls._GOAL) or mentions_verb:
            return cls._intent("goal_status", project_id=project_id,
                               reason="question about goal progress")
        return cls._intent("project_status", project_id=project_id,
                           reason="question about project status")

    @classmethod
    def is_portfolio_request(cls, message: str) -> bool:
        """True when the sentence asks about several projects at once.

        Deliberately requires either a strong aggregation word or a weak one
        *plus* a second signal: "现在有哪些项目" is a registry question, while
        "所有项目有哪些未完成目标" is a portfolio question.  Treating the former
        as a portfolio request would replace a two-line list with a full report.
        """
        lowered = (message or "").lower()
        if any(word in lowered for word in cls._PORTFOLIO_STRONG):
            return True
        return (any(word in lowered for word in cls._PORTFOLIO_WEAK)
                and any(word in lowered for word in cls._PORTFOLIO_SECOND))

    @classmethod
    def _portfolio_focus(cls, message: str) -> str:
        lowered = (message or "").lower()
        if any(word in lowered for word in cls._PORTFOLIO_ATTENTION):
            return "attention"
        if any(word in lowered for word in cls._PORTFOLIO_GOALS):
            return "goals"
        if "优先" in lowered or "priorit" in lowered:
            return "priority"
        return "summary"

    @staticmethod
    def _intent(kind: str, *, reason: str, project_id=None, task_id=None,
                goal_id=None, parameters=None) -> dict[str, Any]:
        return {"intent": kind, "project_id": project_id, "task_id": task_id,
                "goal_id": goal_id, "reason": reason,
                "parameters": dict(parameters or {})}

    @staticmethod
    def _routed_project(message: str, projects: list[dict],
                        conversation: Mapping[str, Any]) -> str | None:
        """The project of this turn, as *already decided* by ``LDChat``.

        The router must not re-derive it: the routing decision is where the
        "do not guess" rules live (ambiguity, references, first-in-list).  It
        falls back to a name match only for callers that construct the payload
        themselves (the CLI/tests), where no routing is attached.
        """
        routing = conversation.get("project_routing") or {}
        if routing:
            return routing.get("project_id")
        return (HeuristicIntentRouter._match_project(message, projects)
                or conversation.get("selected_project_id"))

    @staticmethod
    def _match_project(message: str, projects: list[dict]) -> str | None:
        lowered = message.lower()
        for project in projects:
            for candidate in (project.get("name"), project.get("project_id")):
                if candidate and str(candidate).lower() in lowered:
                    return project.get("project_id")
        return None

    @staticmethod
    def _objective(message: str) -> str:
        """The owner's objective, not the whole instruction sentence.

        "继续推进 t50 项目：把包测试补齐" -> "把包测试补齐".  A goal's identity is
        derived from this text, so a polite prefix must not create a second goal
        for the same work.
        """
        text = message.strip()
        for separator in ("：", ":"):
            head, _, tail = text.partition(separator)
            if tail.strip() and head.strip():
                return tail.strip()
        return text

    @staticmethod
    def _match_id(message: str, candidates: list[str]) -> str | None:
        for candidate in candidates:
            if candidate and candidate in message:
                return candidate
        return None


class LDChat:
    """One conversational turn at a time, over the existing deterministic core."""

    def __init__(
        self,
        tools,
        goal_store,
        chat_store: LDChatStore,
        intent_model: Any | None = None,
        *,
        model_authorized_security_domains: tuple[str, ...] = (),
        fault_hook: Callable[[str], None] | None = None,
    ):
        unknown = set(model_authorized_security_domains) - _SECURITY_DOMAINS
        if unknown:
            raise ValueError(
                "unknown model security domains: " + ", ".join(sorted(unknown)))
        # Chat already owns the durable Goal store.  Bind it to the restricted
        # tool facade so task/goal questions can use the same read-only
        # relational recall path as model-driven ``inspect`` decisions.
        self._tools = (tools.with_goal_store(goal_store)
                       if hasattr(tools, "with_goal_store") else tools)
        self._goals = goal_store
        self._chats = chat_store
        self._model = intent_model
        self._domains = tuple(sorted(set(model_authorized_security_domains)))
        self._fault_hook = fault_hook

    # ------------------------------------------------------------------ #
    # public turn entry point
    # ------------------------------------------------------------------ #
    def handle(self, message: str, *, conversation_id: str | None = None,
               project_id: str | None = None) -> dict[str, Any]:
        """Process one user message and return the rendered turn.

        ``project_id`` has two distinct meanings, decided by whether the
        conversation already exists:

        * **creating** a conversation — it is the conversation's project binding
          (the historical UI-1 behaviour: ``ld chat --project`` and the console
          resume entry point both bind);
        * an **existing global conversation** — it is the project selected for
          *this turn only*.  A global conversation is never rebound by a turn,
          so discussing project B after project A keeps one conversation id.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        conversation = self._chats.ensure_conversation(
            conversation_id, project_id=project_id)
        conversation_id = conversation["conversation_id"]
        bound = conversation.get("project_id")
        if bound:
            # A project-bound conversation is the isolation boundary: it may not
            # be switched by a request field or by a sentence.  Callers that need
            # a cross-project discussion must start a global conversation.
            if project_id is not None and project_id != bound:
                raise ChatPolicyError(
                    "this conversation is bound to project "
                    f"{bound} and cannot be switched to another project")
            other = self._foreign_project_in(message, bound)
            if other:
                # The sentence points at another project's data.  Answering it
                # about this conversation's project would be worse than
                # refusing: it looks like a correct answer to the question the
                # human asked.  A cross-project discussion needs a global
                # conversation, which is one click away and adds no capability.
                raise ChatPolicyError(
                    f"this conversation is bound to project {bound}; it cannot "
                    f"discuss project {other}. Start a new (global) conversation.")
            routing = ProjectRouting(bound, "bound")
        else:
            routing = self._route_project(message, conversation_id, project_id)

        self._chats.append_message(conversation_id, role="user",
                                   content=message.strip())

        pending = conversation.get("pending_action")
        if pending:
            return self._handle_confirmation(
                conversation_id, message, pending, routing=routing,
                requested_project=project_id)

        intent, intent_meta = self._classify(message, conversation_id, routing)
        turn = self._dispatch(intent, conversation, message,
                              intent_source=intent_meta, routing=routing)
        # The turn may have been *re-mapped* (a write refused for lack of a
        # single project becomes a clarification).  The transcript and the audit
        # log must record what actually happened, not what was classified:
        # storing the original ``submit_goal`` while answering "I did not create
        # anything" makes the history say work was requested and accepted.
        effective = turn.get("intent")
        intent = _intent_from_dict(effective) if effective else intent
        turn = {key: value for key, value in turn.items() if key != "intent"}
        self._record_turn(conversation_id, intent, turn)
        return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                "intent_source": intent_meta,
                "project_routing": routing._asdict(), **turn}

    # ------------------------------------------------------------------ #
    # project routing
    # ------------------------------------------------------------------ #
    def _route_project(self, message: str, conversation_id: str,
                       explicit: str | None) -> ProjectRouting:
        """Decide which project *this turn* is about, and on what authority.

        All *deterministic* clues found in the sentence are gathered first and
        then reconciled — never "whichever check ran first wins":

        1. the project the caller selected for this turn;
        2. owner projects of task/goal ids present in the sentence (the ledger
           owns the project of its own rows);
        3. registered projects named by id or name in the sentence.

        If those clues point at more than one project the turn is ``ambiguous``:
        there is no ordering among them, because a sentence that names project B
        and task A does not mean "B, because a name was seen first" nor "A,
        because a task id was seen first".  Asking is the only honest answer.

        A demonstrative ("这个项目") is resolved afterwards from what this
        conversation actually discussed; ``none`` otherwise.  The first project in
        the registry, the most recently updated project and the operator's last
        click are all *absent* on purpose: none of them is a statement about what
        the human meant.
        """
        projects = self._safe("list_projects", self._tools.list_projects, [])
        selected = self._resolve_project(explicit) if explicit else None
        task_projects = self._projects_for_tasks(message)
        goal_projects = self._projects_for_goals(message)
        named = self._match_projects(message, projects)

        clues: list[str] = []

        def add(project_id: str | None) -> None:
            if project_id and project_id not in clues:
                clues.append(project_id)

        for project_id in ([selected] if selected else []):
            add(project_id)
        for project_id in task_projects + goal_projects + named:
            add(project_id)

        if len(clues) > 1:
            return ProjectRouting(None, "ambiguous", tuple(sorted(clues)))
        if len(clues) == 1:
            project_id = clues[0]
            if selected and project_id == selected:
                source = "selected"
            elif task_projects:
                source = "task"
            elif goal_projects:
                source = "goal"
            else:
                source = "named"
            return ProjectRouting(project_id, source)

        if _has_project_reference(message):
            return self._reference_project(conversation_id)

        # Nothing identified a project.  That is an honest "no project", never a
        # fallback to the first/most recent one.
        return ProjectRouting(None, "none")

    def _reference_project(self, conversation_id: str) -> ProjectRouting:
        """Resolve "这个项目" / "继续刚才的任务" from the conversation's own topic.

        The *most recent turn that has a routing record* decides, including a
        turn that was itself ambiguous: once the human asked about "A and B",
        "这个项目" has no referent, and quietly falling back to an older turn
        that mentioned only A would answer a question the human did not ask.
        Only a record-less turn (a confirmation, a system notice) is skipped,
        because those do not move the topic.

        This is topic tracking inside the transcript, which is what the
        demonstrative refers to; it is explicitly *not* "the project the ledger
        touched last" or "the project that is first in the registry".
        """
        for row in reversed(self._chats.recent_turns(
                conversation_id, limit=CONVERSATION_WINDOW * 2)):
            record = self._routing_of_turn(row)
            if record is None:
                continue
            project_id = record.get("project_id")
            if project_id:
                return ProjectRouting(str(project_id), "reference")
            candidates = tuple(sorted(str(item) for item in
                                      record.get("candidates") or ()))
            return ProjectRouting(None, "ambiguous", candidates)
        return ProjectRouting(None, "ambiguous", ())

    def _routing_of_turn(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        """The routing decision recorded for one stored turn, if any.

        New transcripts store it in the LD row's ``facts``.  Older ones only kept
        the ids, so they are read as "that one project" when there is exactly one
        — the same answer the old code gave, without pretending it saw a record.
        """
        facts = row.get("facts") or {}
        record = facts.get("project_routing")
        if isinstance(record, dict) and record.get("source"):
            return record
        intent = row.get("intent") or {}
        legacy = {str(item) for item in
                  (intent.get("project_id"), facts.get("project_id")) if item}
        if len(legacy) == 1:
            return {"source": "legacy", "project_id": legacy.pop()}
        if len(legacy) > 1:
            return {"source": "legacy", "candidates": sorted(legacy)}
        return None

    @staticmethod
    def _match_projects(message: str, projects: list[dict]) -> list[str]:
        """Every registered project whose id or name appears in the sentence.

        All matches are returned (not the first one) so that "alpha 和 beta 哪个
        更急?" is reported as ambiguous instead of silently picking one.
        """
        lowered = (message or "").lower()
        if not lowered:
            return []
        found: list[str] = []
        for project in projects:
            project_id = project.get("project_id")
            if not project_id or project_id in found:
                continue
            candidates = [str(project_id).lower()]
            name = project.get("name")
            if name and len(str(name).strip()) >= 2:
                candidates.append(str(name).strip().lower())
            if any(candidate and candidate in lowered for candidate in candidates):
                found.append(project_id)
        return found

    def _foreign_project_in(self, message: str, bound: str) -> str | None:
        """Another registered project explicitly named in the sentence.

        Used only to refuse a turn in a bound conversation.  It must not fire on
        the bound project itself (that is the normal case) nor on a bare task id.
        """
        projects = self._safe("list_projects", self._tools.list_projects, [])
        named = [item for item in self._match_projects(message, projects)
                 if item != bound]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            return named[0]
        owner = self._project_for_task(message) or self._project_for_goal(message)
        if owner and owner != bound:
            return owner
        return None

    def _projects_for_tasks(self, message: str) -> list[str]:
        """Every project owning a task id that appears in the sentence.

        Plural on purpose: two task ids from two projects is not "the first task
        wins", it is a sentence whose target cannot be determined.
        """
        text = message or ""
        found: list[str] = []
        for task in self._safe("recent_tasks_all", self._all_task_rows, []):
            task_id = task.get("task_id")
            if task_id and task_id in text and task.get("project_id"):
                project_id = str(task["project_id"])
                if project_id not in found:
                    found.append(project_id)
        return found

    def _projects_for_goals(self, message: str) -> list[str]:
        """Every project owning a goal id that appears in the sentence."""
        text = message or ""
        found: list[str] = []
        for goal in self._safe("all_goals", self._goals.all_goals, []):
            goal_id = goal.get("goal_id")
            if goal_id and goal_id in text and goal.get("project_id"):
                project_id = str(goal["project_id"])
                if project_id not in found:
                    found.append(project_id)
        return found

    def _project_for_task(self, message: str) -> str | None:
        """Owner project of the task ids in the sentence, if they agree."""
        found = self._projects_for_tasks(message)
        return found[0] if len(found) == 1 else None

    def _project_for_goal(self, message: str) -> str | None:
        """Owner project of the goal ids in the sentence, if they agree."""
        found = self._projects_for_goals(message)
        return found[0] if len(found) == 1 else None

    def _all_task_rows(self) -> list[dict]:
        return list(self._tools.service.store.all())

    def _is_global(self, conversation_id: str) -> bool:
        """True when this conversation is not bound to a single project."""
        conversation = self._chats.get_conversation(conversation_id) or {}
        return not conversation.get("project_id")

    def _write_project(self, intent: ChatIntent, routing: ProjectRouting,
                       message: str) -> tuple[str | None, str | None]:
        """Resolve the project of a *write*, or explain why we will not.

        Returns ``(project_id, question)``.  ``question`` non-empty means the
        turn must be answered as a clarification: no Goal, no WorkItem, no
        resume, and no "nearest" project chosen on the human's behalf.

        The deterministic routing decision is the *only* authority here.  A
        project id carried by the intent is a **model output**: it can only ever
        agree with a decision that already resolved one, never create one.  If
        the router said ``none`` or ``ambiguous``, the model naming a valid
        registered project does not make the sentence about that project — it
        just means the model guessed, and a guess is exactly what must not start
        work.
        """
        candidates = ", ".join(routing.candidates) or ", ".join(
            sorted(str(item.get("project_id"))
                   for item in self._safe("list_projects",
                                          self._tools.list_projects, [])))
        if routing.source == "ambiguous":
            return None, (
                "这可能是多个项目中的一个："
                f"{', '.join(routing.candidates)}。写操作需要确定唯一项目，"
                "我没有创建任何目标。请指明项目名称或 ID（例如"
                "「推进 <项目ID>：<目标>」）。")
        if routing.project_id:
            resolved = self._require_project(routing.project_id)
            if intent.project_id and self._resolve_project(
                    intent.project_id) != resolved:
                # The model wants a different project than the one the sentence
                # deterministically points at.  Model output cannot move the
                # target of a write; refuse rather than pick a winner.
                return None, (
                    f"这一轮的项目归属已由确定性路由确定为 {resolved}，"
                    "与模型给出的项目不一致，因此我没有创建任何目标。"
                    f"如果确实要在 {intent.project_id} 上工作，请直接说明该项目。")
            return resolved, None
        if _has_project_reference(message):
            return None, (
                "对话里还没有可以指向的项目（或指向了不止一个），所以我没有"
                "创建任何目标。请明确项目名称或 ID。")
        # ``none``: the sentence resolved nothing.  ``intent.project_id`` is
        # deliberately ignored — see the docstring.
        if intent.project_id:
            return None, (
                "这一轮没有从你的话里确定目标项目（模型提到的 "
                f"{intent.project_id} 不能作为项目归属依据），因此我没有创建"
                f"任何目标。已注册项目：{candidates}。请明确说明项目名称或 ID。")
        return None, (
            "这次请求没有指明项目，写操作需要确定的项目归属，因此我没有创建"
            f"任何目标。已注册项目：{candidates}。请说明是哪一个。")

    # ------------------------------------------------------------------ #
    # classification
    # ------------------------------------------------------------------ #
    def _classify(self, message: str, conversation_id: str,
                  routing: ProjectRouting) -> tuple[ChatIntent, str]:
        projects = self._safe("list_projects", self._tools.list_projects, [])
        project_id = routing.project_id
        project_rows = [
            {"project_id": item.get("project_id"),
             "active": item.get("active"),
             "context_version": item.get("context_version"),
             "task_count": item.get("task_count"),
             "verified": item.get("verified"),
             "rejected": item.get("rejected"),
             **({"name": item.get("name")} if "local" in self._domains else {})}
            for item in projects]

        conversation = self._chats.get_conversation(conversation_id) or {}
        goals = []
        if project_id:
            goals = [{"goal_id": goal.get("goal_id"),
                      "status": goal.get("status"),
                      "task_count": goal.get("task_count")}
                     for goal in self._goals.find_by_project(project_id)]
        selected_goal = None
        if goals:
            live = [goal for goal in goals
                    if goal.get("status") in ("active", "waiting", "needs_human")]
            selected_goal = (live or goals)[0]["goal_id"]

        # The current message is *also* model input, and the history projection
        # said nothing about it (M4-D re-audit P0).  "DNF 里那个本地密钥是 XXX,
        # 为什么这样设计?" forwarded the secret verbatim to an endpoint that was
        # never authorized for ``local``.
        #
        # Policy is authorization-based, NOT detection-based (second re-audit
        # P0-1).  Detection cannot establish that a sentence is safe: "t53 的
        # 客户内部代号是蓝鲸七号" is internal information and matches no marker,
        # so "withhold only when a keyword fires" is equivalent to "send
        # everything that does not look like a password".  So:
        #
        #   * model authorized for ``local``  -> the message may be sent (plus an
        #     extra tripwire, which only ever withholds more);
        #   * model not authorized for local  -> the raw message is NOT sent,
        #     regardless of what it looks like.  The turn is answered by the
        #     in-process router, which needs no endpoint.  Sharing raw text with
        #     such a model requires deliberately granting ``local``, i.e. an
        #     explicit decision by the operator rather than a guess.
        #
        # We do not redact instead of withholding: a mangled question gets a
        # confidently wrong answer, and the operator cannot tell that nothing
        # left the machine.
        local_authorized = "local" in self._domains
        withheld = (not local_authorized) or _looks_like_local_content(message)
        withhold_reason = (
            "model_not_authorized_for_local" if not local_authorized
            else "local_content_markers")
        known_task_ids = self._recent_task_ids(project_id)
        chat_input = {
            "schema_version": "m4-d.v1",
            "message": ("" if withheld else message),
            "conversation": {
                "conversation_id": conversation_id,
                "selected_project_id": project_id,
                "selected_goal_id": selected_goal,
                # How the project of *this turn* was determined.  Told to the
                # classifier so it does not have to guess, and so a model that
                # sees an ambiguous sentence is expected to ask rather than
                # pick one of the candidates.
                "project_routing": {
                    "source": routing.source,
                    "project_id": routing.project_id,
                    "candidates": list(routing.candidates),
                },
                "conversation_is_global": self._is_global(conversation_id),
                # Security projection, not the raw transcript: the LD's own
                # answers may quote project knowledge, and this payload goes to
                # the classifier endpoint (M4-D audit P0-2).
                "recent_turns": _safe_history(
                    self._chats.recent_turns(
                        conversation_id, limit=CONVERSATION_WINDOW),
                    domains=self._domains),
            },
            "projects": project_rows,
            "goals": goals,
            "known_task_ids": known_task_ids,
            "security": {
                "authorized_context_domains": list(self._domains),
                "project_names_included": "local" in self._domains,
                "message_withheld": withheld,
                "message_withheld_reason": withhold_reason if withheld else None,
                "note": ("Project roots, Effective Prompts, task text, the LD's "
                         "own answer text and unapproved security domains are "
                         "excluded. recent_turns carries the owner's words plus "
                         "intent labels and ids only. message_withheld=true means "
                         "the current message was not sent at all: either this "
                         "model is not authorized for local-domain content "
                         "(the default), or an extra tripwire matched obvious "
                         "credentials in it."),
            },
            "rules": [
                "Return exactly one structured intent; never return code or commands.",
                "You cannot approve, deny, write project knowledge, or dispatch work.",
                "Use submit_goal only when the owner clearly asks to advance a project.",
                "Use resume_goal only for a goal in needs_human; the human confirms it.",
                "Use clarify when the request is ambiguous or names no project.",
                ("When project_routing.source is 'ambiguous', or is 'none' and the "
                 "owner wants work started, return clarify naming the candidate "
                 "projects: never pick one because it appeared first or was "
                 "discussed before."),
                ("A project named only by 'this project' is resolved from "
                 "conversation.project_routing; do not substitute a different "
                 "project."),
            ],
            "intent_schema": chat_intent_schema(),
        }
        if self._model is None or withheld:
            # The router runs in-process: it may see the real sentence even when
            # the model may not, because nothing leaves this machine.
            local_input = dict(chat_input, message=message)
            raw = HeuristicIntentRouter().classify(local_input)
            return (parse_intent(raw),
                    "local_only" if withheld else "heuristic_router")
        try:
            raw = (self._model.classify(chat_input)
                   if hasattr(self._model, "classify") else self._model(chat_input))
        except Exception as exc:  # noqa: BLE001 - transport boundary
            return (ChatIntent(ChatIntentKind.CLARIFY, None, None, None,
                               f"intent classifier failed: {type(exc).__name__}",
                               {"question": "I could not reach the LD model. "
                                            "Please retry, or ask for a project "
                                            "status by name."}),
                    "model_unavailable")
        try:
            return parse_intent(raw), "model"
        except ChatPolicyError:
            raise
        except ChatValidationError as exc:
            return (ChatIntent(ChatIntentKind.CLARIFY, None, None, None,
                               f"intent rejected: {exc}",
                               {"question": "I did not understand that as a "
                                            "project-management request. Which "
                                            "project should I look at?"}),
                    "invalid_model_intent")

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, intent: ChatIntent, conversation: dict,
                  message: str, *, intent_source: str = "heuristic_router",
                  routing: ProjectRouting | None = None,
                  confirmed_stage: bool = False) -> dict[str, Any]:
        handlers = {
            ChatIntentKind.LIST_PROJECTS: self._list_projects,
            ChatIntentKind.PORTFOLIO_STATUS: self._portfolio_status,
            ChatIntentKind.PROJECT_STATUS: self._project_status,
            ChatIntentKind.EXPLAIN_BLOCKERS: self._explain_blockers,
            ChatIntentKind.GOAL_STATUS: self._goal_status,
            ChatIntentKind.TASK_STATUS: self._task_status,
            ChatIntentKind.KNOWLEDGE_QUERY: self._knowledge_query,
            ChatIntentKind.SUBMIT_GOAL: self._submit_goal,
            ChatIntentKind.RESUME_GOAL: self._resume_goal,
            ChatIntentKind.CLARIFY: self._clarify,
        }
        handler = handlers[intent.intent]
        staged = False
        if routing is None:
            routing = ProjectRouting(intent.project_id, "named")
        if routing.source == "bound" and intent.project_id and \
                intent.project_id != routing.project_id:
            # A bound conversation is the project-isolation boundary.  A sentence
            # naming another project does not get to move it, not even for a
            # read: that would answer project B's question in project A's
            # conversation and, worse, could later be confirmed as A's write.
            raise ChatPolicyError(
                f"this conversation is bound to project {routing.project_id}; "
                f"it cannot read or write project {intent.project_id}. Start a "
                "global conversation to discuss several projects.")
        if intent.intent in _PROJECT_BOUND_WRITES:
            # A write must name *one* project.  The routing decision is the only
            # authority; a missing/ambiguous one becomes a clarification here,
            # before any handler can create a Goal.
            project_id, question = self._write_project(intent, routing, message)
            if question is not None:
                return {"answer": question,
                        # The turn *is* a clarification: reporting the classifier's
                        # original write intent would claim work was requested
                        # when nothing was accepted.
                        "intent": ChatIntent(
                            ChatIntentKind.CLARIFY, None, None, None,
                            "write without a single resolved project",
                            {"question": question}).to_dict(),
                        "facts": {"clarify": True,
                                  "routing": routing._asdict(),
                                  "candidates": list(routing.candidates)},
                        "goal_ids": [], "pending_action": None,
                        "requires_confirmation": False,
                        "staged_for_confirmation": False,
                        "project_routing": routing._asdict()}
            intent = replace(intent, project_id=project_id)
        elif (routing.source == "ambiguous"
                and intent.intent is not ChatIntentKind.CLARIFY
                and not _is_portfolio_answer(intent.intent, message)):
            # A read also cannot be answered when the sentence points at several
            # projects: any single-project answer would be about a project the
            # human may not have meant.  Two kinds of answer are exempt because
            # they are not about one project at all: a genuine registry question,
            # and a portfolio report (whose subject *is* every project).  The
            # registry exemption stays conditional on the wording — an ambiguous
            # sentence that merely got classified as ``list_projects`` must still
            # be refused, or ambiguity would silently become a project list.
            answer = (
                "这一句可能指向多个项目："
                f"{', '.join(routing.candidates)}。我没有读取或创建任何东西。"
                "请指明项目名称或 ID。")
            return {"answer": answer,
                    # The turn *is* a clarification, whatever the classifier
                    # labelled it: saying "list_projects" while answering with a
                    # question would misreport what happened.
                    "intent": ChatIntent(
                        ChatIntentKind.CLARIFY, None, None, None,
                        "ambiguous project for this turn",
                        {"question": answer}).to_dict(),
                    "facts": {"clarify": True,
                              "routing": routing._asdict(),
                              "candidates": list(routing.candidates)},
                    "goal_ids": [], "pending_action": None,
                    "requires_confirmation": False,
                    "staged_for_confirmation": False,
                    "project_routing": routing._asdict()}
        elif routing.project_id and intent.project_id and intent.intent not in (
                ChatIntentKind.LIST_PROJECTS,):
            # A read is refused when the model asks about a different project
            # than the sentence deterministically points at.  Answering it would
            # render one project's state under another project's question.
            resolved = self._resolve_project(routing.project_id)
            if resolved and self._resolve_project(intent.project_id) != resolved:
                raise ChatPolicyError(
                    f"this turn is deterministically about project {resolved}; "
                    f"it cannot read project {intent.project_id}. Naming the "
                    "project you mean is required.")
        if intent.intent is ChatIntentKind.SUBMIT_GOAL and not confirmed_stage:
            bound_project = conversation.get("project_id")
            # A **global** conversation always stages: the operator talks about
            # many projects there, so creating a goal is a project-level write
            # that must be read back (name, id, full objective, goal id) and
            # confirmed.  Even a sentence the deterministic router reads as an
            # unambiguous instruction does not skip that step — the routing
            # proves *which project*, not that the human meant to start work.
            #
            # A **project-bound** conversation keeps the historical behaviour:
            # only a model-proposed submit that the deterministic second opinion
            # does not read as an instruction is staged.
            must_stage = bound_project is None or (
                intent_source == "model"
                and not HeuristicIntentRouter.is_imperative_submission(message))
            if must_stage:
                staged = True
                answer, facts, goal_ids, pending = self._submit_goal(
                    intent, conversation, message, staged=True)
            else:
                answer, facts, goal_ids, pending = handler(
                    intent, conversation, message)
        else:
            answer, facts, goal_ids, pending = handler(intent, conversation, message)
        return {"answer": answer, "facts": facts, "goal_ids": goal_ids,
                "pending_action": pending,
                "requires_confirmation": bool(pending),
                "staged_for_confirmation": staged,
                "project_routing": routing._asdict()}

    # -- read handlers -------------------------------------------------- #
    def _list_projects(self, intent, conversation, message):
        projects = self._tools.list_projects()
        if not projects:
            return ("No projects are registered yet. Add one with "
                    "`controlplane project add <name> <root>`."), \
                {"projects": []}, [], None
        lines = ["Current projects (all figures derived from the ledger):", ""]
        rows = []
        for item in projects:
            metrics = self._project_metrics(item["project_id"])
            counts = metrics["counts"]
            blocked = metrics["blocked_workitems"]
            abnormal = metrics["abnormal_verdict_workitems"]
            # Three separate numbers, because they mean three separate things.
            # The old single "needing attention" number read only
            # ``blocked_tasks``, so a rejected task and a stopped goal were both
            # invisible here even though the single-project view reported them.
            #
            # A number that could not be read is printed as "未知", never as 0:
            # "0 rejected" and "could not tell" are different statements, and
            # only one of them is safe to act on.
            def figure(value, known: bool, label: str = "") -> str:
                if not known:
                    return "未知"
                return str(value) + (f" {label}" if label else "")

            task_part = (f"{counts.get('total', item.get('task_count', 0))} tasks "
                         f"({counts.get('verified', 0)} verified, "
                         f"{counts.get('rejected', 0)} rejected, "
                         f"{counts.get('inconclusive', 0)} inconclusive)"
                         if metrics["counts_known"] else "tasks 计数未知")
            # The abnormal-verdict figure is qualified when the scan was
            # bounded: a count taken from a truncated scan is a lower bound, not
            # an exact figure, and must not be printed as one.
            abnormal_figure = figure(len(abnormal), metrics["tasks_known"])
            if metrics["tasks_known"] and not metrics["abnormal_detail_complete"]:
                abnormal_figure = f"≥{len(abnormal)}（明细可能不完整）"
            lines.append(
                f"- {item.get('name')} ({item['project_id']})"
                f"{'' if item.get('active') else ' [disabled]'}: "
                f"{task_part}, "
                f"{figure(len(blocked), metrics['blocking_known'])} blocked, "
                f"{abnormal_figure} abnormal verdict, "
                f"{figure(len(metrics['needs_human_goals']), metrics['goals_known'])}"
                " goals needing a human"
                f"{'' if metrics['complete'] else ' ⚠ 数据不完整'}")
            if metrics["read_error"]:
                lines.append(f"    读取失败：{metrics['read_error']}")
            if metrics.get("abnormal_detail_note"):
                lines.append(f"    {metrics['abnormal_detail_note']}")
            rows.append({"project_id": item["project_id"], "name": item.get("name"),
                         "active": item.get("active"), "counts": counts,
                         "counts_known": metrics["counts_known"],
                         "blocked": len(blocked),
                         "blocking_known": metrics["blocking_known"],
                         "abnormal_verdict": len(abnormal),
                         "tasks_known": metrics["tasks_known"],
                         "abnormal_detail_complete":
                             metrics["abnormal_detail_complete"],
                         "abnormal_detail_note": metrics["abnormal_detail_note"],
                         "needs_human_goals": len(metrics["needs_human_goals"]),
                         "goals_known": metrics["goals_known"],
                         "complete": metrics["complete"],
                         "unknown": list(metrics["unknown"]),
                         "read_error": metrics["read_error"],
                         "truncated": metrics["truncated"]})
        return "\n".join(lines), {"projects": rows}, [], None

    def _portfolio_status(self, intent, conversation, message):
        """Cross-project, read-only report.  Facts first, then judgement.

        The split is the point of this handler.  Everything under "Facts" is a
        ledger row read deterministically; everything under "Judgement" is
        marked as such and is derived from a fixed, stated rule, so a reader can
        disagree with the recommendation without doubting the numbers.  Where the
        ledger cannot support a conclusion the text says so instead of inventing
        a dependency or a severity.

        This intent needs no single project: it is exempt from the
        ``source=none`` → ``clarify`` rule by construction (see
        ``PORTFOLIO_INTENTS``), because "not about one project" is its subject.
        It creates nothing, resumes nothing, dispatches nothing.
        """
        projects = self._tools.list_projects()
        if not projects:
            return ("No projects are registered yet, so there is nothing to "
                    "summarise. Add one with "
                    "`controlplane project add <name> <root>`."), \
                {"projects": [], "portfolio": True}, [], None

        portfolio = []
        for item in projects:
            metrics = self._project_metrics(item["project_id"])
            portfolio.append({"project": item, "metrics": metrics})

        facts = self._portfolio_facts(portfolio)
        judgement = self._portfolio_judgement(portfolio)

        lines = ["跨项目只读汇总（全部数值来自 Ledger 确定性读取）", ""]
        lines.append("一、事实")
        lines.extend(facts["lines"])
        lines.append("")
        lines.append("二、判断（依据上面事实的固定规则，可被质疑）")
        lines.extend(judgement["lines"])
        if facts["unknowns"]:
            lines.append("")
            lines.append("三、暂无法判断")
            lines.extend(f"- {item}" for item in facts["unknowns"])
        lines.append("")
        lines.append("本次为只读汇总：未创建 Goal，未恢复任何 Goal，"
                     "未提交 WorkItem。")

        return "\n".join(lines), {
            "portfolio": True,
            "projects": [{"project_id": entry["project"]["project_id"],
                          "name": entry["project"].get("name"),
                          "active": entry["project"].get("active"),
                          "counts": entry["metrics"]["counts"],
                          "counts_known": entry["metrics"]["counts_known"],
                          "blocked": len(entry["metrics"]["blocked_workitems"]),
                          "blocking_known": entry["metrics"]["blocking_known"],
                          "abnormal_verdict": len(
                              entry["metrics"]["abnormal_verdict_workitems"]),
                          "tasks_known": entry["metrics"]["tasks_known"],
                          "abnormal_detail_complete":
                              entry["metrics"]["abnormal_detail_complete"],
                          "needs_human_goals": len(
                              entry["metrics"]["needs_human_goals"]),
                          "goals_known": entry["metrics"]["goals_known"],
                          "complete": entry["metrics"]["complete"],
                          "unknown": list(entry["metrics"]["unknown"]),
                          "read_error": entry["metrics"]["read_error"],
                          "goal_status_counts": entry["metrics"][
                              "goal_status_counts"],
                          "truncated": entry["metrics"]["truncated"]}
                         for entry in portfolio],
            "complete": facts["totals"]["complete"],
            "incomplete_projects": facts["totals"]["incomplete_projects"],
            "totals": facts["totals"],
            "priorities": judgement["priorities"],
            "unknowns": facts["unknowns"],
        }, [], None

    def _portfolio_facts(self, portfolio: list[dict]) -> dict:
        """Every number a portfolio answer may state, and nothing else.

        Totals are summed **only over projects whose read succeeded**.  A
        cross-project total that quietly added a zero for an unreadable project
        would be a precise-looking number nobody actually measured, so when any
        project is incomplete the headline says so and the affected quantity is
        reported as a lower bound rather than an exact figure.
        """
        lines: list[str] = []
        unknowns: list[str] = []
        totals = {"projects": len(portfolio), "work_items": 0, "verified": 0,
                  "rejected": 0, "inconclusive": 0, "blocked": 0,
                  "abnormal_verdict": 0, "needs_human_goals": 0,
                  "unfinished_goals": 0, "truncated_projects": [],
                  "incomplete_projects": [], "complete": True,
                  "unread_projects": []}

        for entry in portfolio:
            project = entry["project"]
            metrics = entry["metrics"]
            counts = metrics["counts"]
            name = project.get("name")
            pid = project["project_id"]
            if not metrics["complete"]:
                totals["complete"] = False
                totals["incomplete_projects"].append(pid)
            if not metrics["counts_known"]:
                totals["unread_projects"].append(pid)
            # Only add what was actually read: a 0 for an unread project is the
            # exact failure mode this fix removes.
            if metrics["counts_known"]:
                totals["work_items"] += counts.get("total", 0)
                totals["verified"] += counts.get("verified", 0)
                totals["rejected"] += counts.get("rejected", 0)
                totals["inconclusive"] += counts.get("inconclusive", 0)
            if metrics["blocking_known"]:
                totals["blocked"] += len(metrics["blocked_workitems"])
            if metrics["tasks_known"]:
                totals["abnormal_verdict"] += len(
                    metrics["abnormal_verdict_workitems"])
            if metrics["goals_known"]:
                totals["needs_human_goals"] += len(metrics["needs_human_goals"])
                totals["unfinished_goals"] += len(metrics["unfinished_goals"])
            if metrics["truncated"]:
                totals["truncated_projects"].append(pid)

            lines.append("")
            lines.append(f"【{name}】{pid}"
                         f"{'' if project.get('active') else ' [已停用]'}")

            def figure(value, known: bool) -> str:
                return "未知" if not known else str(value)

            if metrics["counts_known"]:
                lines.append(
                    f"  任务 {counts.get('total', 0)} 个："
                    f"verified {counts.get('verified', 0)}、"
                    f"rejected {counts.get('rejected', 0)}、"
                    f"inconclusive {counts.get('inconclusive', 0)}、"
                    f"active {counts.get('active', 0)}、"
                    f"awaiting approval {counts.get('needs_approval', 0)}")
            else:
                lines.append("  任务计数：未知（未能读取项目进度）")
            if metrics.get("read_error"):
                lines.append(
                    f"  ⚠ 数据不完整（{metrics['read_error']}）："
                    "下列未知项不得当作 0 处理。")
                unknowns.append(
                    f"{name} 的部分数据未能读取（{metrics['read_error']}）："
                    f"未知项 = {', '.join(metrics['unknown'])}。"
                    "该项目的对应数字不是 0，而是没有读到。")
            if metrics["truncated"]:
                lines.append(
                    f"  ⚠ WorkItem 明细达到读取上限 {MAX_REPORTED_WORK_ITEMS}，"
                    "列表可能不完整（计数仍然正确）。")
            goal_counts = metrics["goal_status_counts"]
            if not metrics["goals_known"]:
                lines.append("  Goal：未知（未能读取 Goal 记录）")
            elif goal_counts:
                rendered = "、".join(f"{key} {value}"
                                     for key, value in sorted(goal_counts.items()))
                lines.append(f"  Goal {sum(goal_counts.values())} 个：{rendered}")
            else:
                lines.append("  Goal 0 个")

            if not metrics["blocking_known"]:
                lines.append("  阻塞：未知（未能读取阻塞信息）")
            elif metrics["blocked_workitems"]:
                lines.append("  阻塞（现在就需要人决定）：")
                for task in metrics["blocked_workitems"]:
                    flags = ", ".join(task.get("attention_flags") or []) or "unknown"
                    lines.append(f"    - {task['task_id']} "
                                 f"state={task.get('state')} "
                                 f"verdict={task.get('verdict')} flags={flags}")
            else:
                lines.append("  阻塞：无")

            if not metrics["tasks_known"]:
                lines.append("  异常验收：未知（未能读取任务明细）")
            elif metrics["abnormal_verdict_workitems"]:
                # The heading itself carries the qualification when the scan was
                # bounded: the enumeration may be missing older rows even though
                # the project-level counts are exact.
                suffix = ("" if metrics["abnormal_detail_complete"]
                          else "（明细可能不完整）")
                lines.append(f"  异常验收（历史结论，不必然是当前待办）{suffix}：")
                for task in metrics["abnormal_verdict_workitems"]:
                    lines.append(f"    - {task['task_id']} "
                                 f"verdict={task.get('verdict')} "
                                 f"{(task.get('title') or '')[:60]}")
                if metrics.get("abnormal_detail_note"):
                    lines.append(f"    ⚠ {metrics['abnormal_detail_note']}")
            elif not metrics["abnormal_detail_complete"]:
                # Nothing abnormal found in the part that was scanned, but the
                # scan was bounded: "none" is not an assertion we can make.
                lines.append("  异常验收：扫描范围内未见；明细可能不完整")
                if metrics.get("abnormal_detail_note"):
                    lines.append(f"    ⚠ {metrics['abnormal_detail_note']}")

            if not metrics["goals_known"]:
                pass  # already stated above
            elif metrics["unfinished_goals"]:
                lines.append("  未完成 Goal：")
                for goal in metrics["unfinished_goals"]:
                    text = (goal.get("goal_text") or "").replace("\n", " ")
                    lines.append(f"    - {goal['goal_id']} [{goal['status']}] "
                                 f"{text[:100]}")
                    stop = goal.get("stop_reason")
                    if stop:
                        lines.append(f"      停止原因：{stop[:120]}")
                    if goal.get("max_decisions") is not None:
                        lines.append(
                            f"      预算：decisions "
                            f"{goal.get('model_decision_count', 0)}/"
                            f"{goal.get('max_decisions')}、tasks "
                            f"{goal.get('task_count', 0)}/{goal.get('max_tasks')}")
            else:
                lines.append("  未完成 Goal：无")

            # What the ledger genuinely cannot tell us.
            for task in metrics["blocked_workitems"]:
                if task.get("state") == "unknown" or "dispatch" in str(
                        task.get("attention_flags") or ""):
                    unknowns.append(
                        f"{task['task_id']}（{name}）处于 {task.get('state')} / "
                        f"{task.get('queue_status')}：Ledger 未记录该作业是否真的"
                        "创建成功，需要查 Worker 侧日志才能定案。")

        def at_least(value: int, *known_flags: bool) -> str:
            """A total is exact only if every contributing read succeeded."""
            if all(known_flags):
                return str(value)
            return f"≥{value}（部分项目未读到）"

        counts_ok = not totals["unread_projects"]
        blocking_ok = all(entry["metrics"]["blocking_known"]
                          for entry in portfolio)
        tasks_ok = all(entry["metrics"]["tasks_known"] for entry in portfolio)
        # The *detail* enumeration is exact only if every project's scan ran to
        # completion; a bounded scan may be missing older abnormal rows, so the
        # abnormal-verdict total is a lower bound even when every read succeeded.
        abnormal_ok = tasks_ok and all(
            entry["metrics"]["abnormal_detail_complete"] for entry in portfolio)
        goals_ok = all(entry["metrics"]["goals_known"] for entry in portfolio)
        lines.insert(0, (
            f"共 {totals['projects']} 个项目、"
            f"{at_least(totals['work_items'], counts_ok)} 个 WorkItem："
            f"verified {at_least(totals['verified'], counts_ok)}、"
            f"rejected {at_least(totals['rejected'], counts_ok)}、"
            f"inconclusive {at_least(totals['inconclusive'], counts_ok)}；"
            f"当前阻塞 {at_least(totals['blocked'], blocking_ok)} 项、"
            f"异常验收 {at_least(totals['abnormal_verdict'], abnormal_ok)} 项、"
            f"待人工 Goal {at_least(totals['needs_human_goals'], goals_ok)} 个、"
            f"未完成 Goal {at_least(totals['unfinished_goals'], goals_ok)} 个。"))
        if totals["incomplete_projects"]:
            # Says plainly that the summary is partial: a cross-project report
            # must not look complete when one project could not be read.
            lines.insert(1, (
                f"⚠ 本汇总不完整：{', '.join(totals['incomplete_projects'])} "
                "读取失败，上述总计只覆盖读取成功的项目（以 ≥ 标注），"
                "不得当作全量精确值。"))
        if totals["truncated_projects"]:
            lines.insert(1, (
                f"注意：{'、'.join(totals['truncated_projects'])} 的 WorkItem 数"
                f"达到读取上限 {MAX_REPORTED_WORK_ITEMS}，上述计数可能不完整。"))
        return {"lines": lines, "totals": totals, "unknowns": unknowns}

    def _portfolio_judgement(self, portfolio: list[dict]) -> dict:
        """Priorities derived from the facts above by an explicit rule.

        The rule is stated in the output so the reader can audit it.  It only
        ranks *what the ledger shows*; it never asserts a dependency between
        projects, because the ledger records no such edge.
        """
        priorities: list[dict] = []
        for entry in portfolio:
            project = entry["project"]
            metrics = entry["metrics"]
            name = project.get("name")
            blocked = metrics["blocked_workitems"]
            needs_human = metrics["needs_human_goals"]
            abnormal = metrics["abnormal_verdict_workitems"]

            # Collect *reasons* first, then emit one entry per project at the most
            # urgent rank.  Emitting one entry per rule let a project appear twice
            # with contradictory priorities ("P3 budget" and "P4 history" for the
            # same project), which makes a ranking unusable.
            reasons: list[tuple[int, str, str]] = []

            # 1: an execution whose outcome is genuinely unknown outranks
            # everything else — acting on it twice may duplicate real work.
            uncertain = [task for task in blocked
                         if task.get("state") == "unknown"
                         or any("dispatch" in str(flag)
                                for flag in task.get("attention_flags") or [])]
            if uncertain:
                reasons.append((
                    1,
                    f"{len(uncertain)} 个任务状态未定"
                    f"（{', '.join(t['task_id'] for t in uncertain)}）："
                    "可能已创建作业但未被观测到，重复处理有重复投入风险",
                    "state=unknown / dispatch_unknown 且 queue_status 非 complete"))

            # 2: goals that stopped because the model asked for a human.  These
            # are decidable by a person reading the recorded stop reason.
            requested = [goal for goal in needs_human
                         if "request_human" in str(goal.get("last_action") or "")
                         or "human_required" in str(
                             goal.get("last_next_state") or "")]
            if requested:
                reasons.append((
                    2,
                    f"{len(requested)} 个 Goal 在等一个具体决定"
                    f"（{', '.join(g['goal_id'] for g in requested)}）",
                    "status=needs_human 且 last_decision.action=request_human"))

            # 3: goals stopped on exhausted budget.  Flagged as "do not simply
            # resume": resuming spends the same exhausted budget again.
            #
            # Only goals whose *recorded* stop reason actually names budget
            # exhaustion go here.  Classifying every non-``request_human`` goal
            # as "budget exhausted" mislabelled ``invalid_model_decision`` and
            # any unrecognised reason, and told the operator to weigh the budget
            # when the recorded cause was something else entirely.
            exhausted = [goal for goal in needs_human
                         if goal["goal_id"] not in
                         {g["goal_id"] for g in requested}
                         and _is_budget_stop(goal)]
            if exhausted:
                reasons.append((
                    3,
                    f"{len(exhausted)} 个 Goal 因预算耗尽停止"
                    f"（{', '.join(g['goal_id'] for g in exhausted)}）："
                    "恢复会再次耗尽同一预算，应先判断目标是否还值得继续",
                    "status=needs_human 且 last_decision.stop 含 budget"))

            # 3b: goals stopped for any *other* recorded reason, or for a reason
            # this code does not recognise.  Shown as recorded, marked 待核查 —
            # the report must not invent a cause it cannot read.
            other = [goal for goal in needs_human
                     if goal["goal_id"] not in
                     {g["goal_id"] for g in requested}
                     and not _is_budget_stop(goal)]
            if other:
                described = []
                for goal in other:
                    reason = (goal.get("stop_reason") or "").strip()
                    if not reason:
                        reason = "未记录停止原因"
                    elif not _is_known_stop_reason(reason):
                        reason = f"未识别的停止原因：{reason}"
                    described.append(f"{goal['goal_id']}（{reason}）")
                reasons.append((
                    3,
                    f"{len(other)} 个 Goal 等待人工处理，停止原因以记录为准："
                    f"{'；'.join(described)}。原因未明确前按「待核查」对待，"
                    "不要假定为预算耗尽",
                    "status=needs_human 且停止原因不属于 lifetime budget exhausted"))

            if abnormal and not blocked:
                reasons.append((
                    4,
                    f"{len(abnormal)} 个任务验收未通过但当前不阻塞："
                    "属历史结论，是否重做由你决定",
                    "verdict in (rejected, inconclusive) 且不在 blocked_tasks"))

            if not reasons:
                continue
            # One entry per project: the most urgent rule wins, and the other
            # findings stay visible as extra bullets instead of a second rank.
            reasons.sort(key=lambda item: item[0])
            rank, why, basis = reasons[0]
            extra = [text for _, text, _ in reasons[1:]]
            priorities.append({
                "rank": f"P{rank}",
                "project_id": project["project_id"],
                "project": name,
                "why": why,
                "basis": basis,
                "also": extra,
            })

        lines: list[str] = []
        if not priorities:
            lines.append("- 按当前规则没有需要排序的事项：Ledger 中没有阻塞、"
                         "没有待人工 Goal、没有异常验收。")
            return {"lines": lines, "priorities": []}

        lines.append("规则：先看「执行结果未知」（可能重复投入），再看「等一个具体"
                     "决定」，再看「预算耗尽（不要直接恢复）」，最后看「历史异常验收」。")
        lines.append("Ledger 未记录项目之间的依赖关系，因此这里不做跨项目依赖排序。")
        # Sort by rank *and return the sorted list*: the text was ordered while
        # the structured payload kept insertion order, so a console rendering the
        # data would show a different priority than the answer text.  Ranks are
        # zero-padded on comparison so "P10" would still sort after "P9".
        ordered = sorted(priorities,
                         key=lambda entry: int(str(entry["rank"])[1:]))
        for item in ordered:
            lines.append(f"- [{item['rank']}] {item['project']}：{item['why']}")
            lines.append(f"    依据：{item['basis']}")
            for also in item.get("also") or []:
                lines.append(f"    同时存在：{also}")
        return {"lines": lines, "priorities": ordered}

    def _project_status(self, intent, conversation, message):
        project_id = intent.project_id
        progress = self._tools.get_project_progress(project_id)
        brief = self._tools.get_project_brief(project_id)
        goals = self._goal_rows(project_id, include_completed=True)
        counts = progress.get("counts") or {}
        lines = [f"Project {progress.get('name')} ({project_id})", ""]
        lines.append(
            f"Tasks: {counts.get('total', 0)} total, {counts.get('active', 0)} active, "
            f"{counts.get('verified', 0)} verified, {counts.get('rejected', 0)} rejected, "
            f"{counts.get('inconclusive', 0)} inconclusive, "
            f"{counts.get('needs_approval', 0)} awaiting approval.")
        if goals:
            lines.append("")
            lines.append("Goals:")
            for goal in goals:
                lines.append(f"- {goal['goal_id']} [{goal['status']}]: "
                             f"{goal['goal_text'][:160]} "
                             f"({goal['task_count']} tasks, "
                             f"{goal['verified_tasks']} verified)")
        blocked = self._attention_items(progress)
        if blocked:
            lines.append("")
            lines.append("Needs attention:")
            for task in blocked:
                flags = ", ".join(task.get("attention_flags") or ["unknown"])
                lines.append(f"- {task['task_id']} ({task.get('state')}, "
                             f"{task.get('verdict')}): {flags}")
                if str(task.get("verdict")) in ("rejected", "inconclusive"):
                    lines.append("  the control plane rejected this task's declared "
                                 "assertions; review the failed assertion before "
                                 "resubmitting")
        recent = progress.get("recent_tasks") or []
        if recent:
            lines.append("")
            lines.append("Most recent tasks:")
            for task in recent[:5]:
                lines.append(f"- {task['task_id']} {task.get('state')}"
                             f"/{task.get('verdict')}: {task.get('title') or ''}"
                             .rstrip(": "))
        narrative = progress.get("narrative_progress") or []
        if narrative:
            lines.append("")
            lines.append("Narrative progress (candidate knowledge, not a verdict):")
            for entry in narrative[:5]:
                lines.append(f"- {entry['title']} [{entry.get('verification')}]")
        return "\n".join(lines), {
            "project_id": project_id, "counts": counts,
            "blocked": blocked, "goals": goals,
            "context_version": progress.get("context_version"),
            "entry_count": brief.get("entry_count"),
            "verified_context": brief.get("verified_count"),
        }, [goal["goal_id"] for goal in goals], None

    def _explain_blockers(self, intent, conversation, message):
        project_id = intent.project_id
        progress = self._tools.get_project_progress(project_id)
        blocked = self._attention_items(progress)
        goals = self._goal_rows(project_id)
        lines = [f"Blocking items in {progress.get('name')} ({project_id}):", ""]
        if not blocked:
            lines.append("- Nothing is waiting for a human decision right now.")
        for task in blocked:
            status = self._safe("get_task_status",
                                lambda tid=task["task_id"]:
                                self._tools.get_task_status(tid), {})
            lines.append(f"- {task['task_id']}: state={task.get('state')}, "
                         f"verdict={task.get('verdict')}, "
                         f"flags={', '.join(task.get('attention_flags') or []) or 'none'}")
            for warning in status.get("warnings") or []:
                lines.append(f"  warning: {str(warning)[:200]}")
            results = status.get("assertion_results") or []
            for result in results:
                if not result.get("passed", True):
                    lines.append(f"  failed assertion: {result.get('kind')} "
                                 f"{result.get('target')}")
        parked = [goal for goal in goals if goal["status"] == "needs_human"]
        if parked:
            lines.append("")
            lines.append("Goals stopped for a human:")
            for goal in parked:
                lines.append(f"- {goal['goal_id']}: {goal['goal_text'][:160]}")
                if goal.get("stop_reason"):
                    lines.append(f"  reason: {goal['stop_reason'][:300]}")
                lines.append("  Say 'confirm' after I stage a resume, or resume it "
                             "explicitly with `ld run-goal ... --resume`.")
        return "\n".join(lines), {
            "project_id": project_id, "blocked": blocked,
            "needs_human_goals": parked}, [g["goal_id"] for g in parked], None

    def _goal_status(self, intent, conversation, message):
        project_id = intent.project_id
        goals = self._goal_rows(
            project_id,
            include_completed=bool(intent.parameters.get("include_completed", True)))
        if not goals:
            return (f"Project {project_id} has no goals yet."), \
                {"project_id": project_id, "goals": []}, [], None
        lines = [f"Goals of project {project_id}:", ""]
        for goal in goals:
            lines.append(
                f"- {goal['goal_id']} [{goal['status']}]: {goal['goal_text'][:200]}")
            # Lifetime budgets are stamped on the first advancement, so an
            # unstarted goal honestly has no cap yet — never print "None" as if
            # the goal had zero budget left.
            max_tasks = goal["max_tasks"] if goal["max_tasks"] is not None \
                else "not yet stamped"
            max_decisions = (goal["max_decisions"]
                             if goal["max_decisions"] is not None
                             else "not yet stamped")
            lines.append(
                f"  tasks {goal['task_count']}/{max_tasks}, decisions "
                f"{goal['model_decision_count']}/{max_decisions}, "
                f"verified {goal['verified_tasks']}, current task "
                f"{goal.get('current_task_id') or 'none'}")
            if goal.get("last_action"):
                lines.append(f"  last step: {goal['last_action']} "
                             f"({goal.get('last_next_state')})")
            if goal.get("stop_reason"):
                lines.append(f"  stop reason: {goal['stop_reason'][:300]}")
        return "\n".join(lines), {"project_id": project_id, "goals": goals}, \
            [goal["goal_id"] for goal in goals], None

    def _task_status(self, intent, conversation, message):
        status = self._tools.get_task_status(intent.task_id)
        if intent.project_id and status.get("project_id") != intent.project_id:
            raise ChatPolicyError(
                f"task {intent.task_id} does not belong to project {intent.project_id}")
        lines = [f"Task {status['task_id']} ({status.get('project_id')})", ""]
        lines.append(f"Worker state: {status.get('state')}; "
                     f"control-plane verdict: {status.get('verdict')}; "
                     f"queue: {status.get('queue_status')}")
        if status.get("title"):
            lines.append(f"Title: {status['title']}")
        if status.get("attention_flags"):
            lines.append("Attention: " + ", ".join(status["attention_flags"]))
        results = status.get("assertion_results") or []
        if results:
            lines.append("")
            lines.append("Acceptance assertions:")
            for result in results:
                mark = "pass" if result.get("passed") else "FAIL"
                lines.append(f"- [{mark}] {result.get('kind')} {result.get('target')}"
                             + (f" = {result.get('value')}" if result.get("value") else ""))
        elif status.get("assertions"):
            lines.append("")
            lines.append("No assertion results recorded yet; the verdict cannot be "
                         "treated as a pass on Worker state alone.")
        if status.get("verdict") == "verified":
            lines.append("")
            lines.append("This verdict is the control plane's independent acceptance "
                         "of the declared assertions, not a guarantee of general "
                         "code quality.")
        elif status.get("verdict") in ("rejected", "inconclusive"):
            lines.append("")
            lines.append(f"The verdict {status['verdict']!r} was produced by the "
                         "control plane from the declared assertions; the Worker's "
                         "own final state is not a success claim. A resubmission "
                         "needs the underlying failure fixed, not a retry.")
        recall = self._safe(
            "get_recall_summary",
            lambda: self._tools.get_recall_summary(
                status.get("project_id"), object_type="task",
                object_id=status["task_id"], limit=50, char_budget=1_500),
            None)
        recall_refs = ((recall or {}).get("object") or {}).get("refs") or []
        attempts = [ref for ref in recall_refs
                    if ref.get("object_type") == "attempt"]
        sessions = [ref for ref in recall_refs
                    if ref.get("object_type") == "session"]
        evidence = [ref for ref in recall_refs
                    if ref.get("object_type") == "evidence"]
        if recall is not None:
            lines.append("")
            lines.append(
                f"Recall links: {len(attempts)} attempt(s), "
                f"{len(sessions)} session reference(s), "
                f"{len(evidence)} Ledger evidence row(s). Use their ids for "
                "an on-demand detail read; long history is not injected here.")
        return "\n".join(lines), {
            "task": status,
            "recall_refs": recall_refs,
            "recall_schema_version": (recall or {}).get("schema_version"),
        }, [], None

    def _knowledge_query(self, intent, conversation, message):
        project_id = intent.project_id
        entries = self._tools.service.context.list_entries(
            project_id, kind=intent.parameters.get("kind"))
        tags = {tag.lower() for tag in intent.parameters.get("tags") or []}
        keywords = _tokens(intent.parameters.get("keywords") or "")
        scored = []
        for entry in entries:
            entry_tags = {tag.lower() for tag in entry.get("tags") or []}
            overlap = _tokens(f"{entry['title']} {entry['body']}") & keywords
            score = 3.0 * len(entry_tags & tags) + 0.5 * min(len(overlap), 8)
            if entry.get("verification") == "verified":
                score += 1.0
            scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]["title"]))
        limit = int(intent.parameters.get("limit") or 5)
        chosen = [entry for _score, entry in scored[:limit]]
        if not chosen:
            return (f"No project knowledge in {project_id} matched that. "
                    "Say what you want to record and I will stage it as a "
                    "candidate entry for review."), \
                {"project_id": project_id, "entries": []}, [], None
        lines = [f"Project knowledge in {project_id} (most relevant first):", ""]
        for entry in chosen:
            lines.append(f"- [{entry['kind']}/{entry['verification']}] "
                         f"{entry['title']} (v{entry['version']}, id {entry['id']})")
            body = (entry.get("body") or "").strip().replace("\n", " ")
            lines.append(f"  {body[:400]}")
            if entry.get("evidence_refs"):
                lines.append("  evidence: " + ", ".join(entry["evidence_refs"][:5]))
            if entry.get("source"):
                lines.append(f"  source: {str(entry['source'])[:200]}")
        lines.append("")
        lines.append("Only entries marked 'verified' may be treated as a trusted "
                     "rule. Candidate knowledge is a proposal until evidence is "
                     "confirmed.")
        return "\n".join(lines), {
            "project_id": project_id,
            "context_version": self._tools.service.store.context_version(project_id),
            "entries": [{"id": e["id"], "kind": e["kind"], "title": e["title"],
                         "verification": e["verification"], "version": e["version"]}
                        for e in chosen]}, [], None

    def _clarify(self, intent, conversation, message):
        question = intent.parameters.get("question") or (
            "Which project should I look at?")
        return question, {"clarify": True}, [], None

    # -- write handlers ------------------------------------------------- #
    def _submit_goal(self, intent, conversation, message, *,
                     staged: bool = False):
        project_id = self._require_project(intent.project_id)
        goal_text = intent.parameters["goal"].strip()
        if not goal_text:
            raise ChatValidationError("submit_goal needs a non-empty objective")
        if len(goal_text) > MAX_GOAL_CHARS:
            # The objective *is* the goal identity (``goal_identity`` hashes it),
            # so a truncated objective is a different goal.  Refuse instead of
            # silently shortening what will be executed.
            raise ChatValidationError(
                f"submit_goal objective is {len(goal_text)} characters; the limit "
                f"is {MAX_GOAL_CHARS}. Shorten it — a truncated objective would be "
                "a different goal than the one you confirm.")
        goal_id, goal_ref = goal_identity(goal_text, project_id)
        existing = self._goals.get(goal_id) or {}
        if staged:
            # A model proposed work that the deterministic router does not read
            # as an imperative.  Nothing is written yet: the owner confirms.
            # The FULL objective is stored (bounded by MAX_GOAL_CHARS): storing a
            # prefix made the confirmed goal_id differ from the staged one.
            pending = {"kind": "submit_goal", "goal_id": goal_id,
                       "goal_ref": goal_ref, "project_id": project_id,
                       "goal_text": goal_text, "staged_at": time.time(),
                       "origin": intent.reason[:200]}
            # Everything the human is agreeing to, spelled out — including the
            # *complete* objective (bounded by MAX_GOAL_CHARS, which is what a
            # goal may be).  A truncated preview would let someone confirm text
            # they have not read, and the goal id is a hash of the full text.
            answer = (f"我理解这是要在这个项目开始工作：\n"
                      f"  项目名称：{self._project_label(project_id)}\n"
                      f"  项目 ID：{project_id}\n"
                      f"  Goal 正文：{goal_text}\n"
                      f"  目标 ID：{goal_id}\n\n"
                      "如果这不是你的意思，回复 'cancel'。回复 'confirm' 才会创建；"
                      "目前还没有创建或派发任何东西，也不会在这里授予预算。")
            return answer, {"project_id": project_id, "goal_id": goal_id,
                            "goal_ref": goal_ref, "staged": True,
                            "goal_chars": len(goal_text),
                            "reused": bool(existing)}, [], pending

        goal = self._goals.get_or_create(goal_id, project_id, goal_text, goal_ref)
        status = goal.get("status")
        lines = []
        if existing:
            lines.append(f"Goal {goal_id} already exists for project {project_id} "
                         f"(status: {status}). I reused it instead of creating a "
                         "second one.")
        else:
            lines.append(f"Goal {goal_id} created for project {project_id}: "
                         f"{goal_text}")
        others = [item for item in self._goals.find_by_project(project_id)
                  if item.get("goal_id") != goal_id
                  and item.get("status") in ("active", "waiting", "needs_human")]
        if others:
            lines.append("")
            lines.append("Note: this project also has other unfinished goals; they "
                         "are not replaced by this one:")
            for item in others:
                lines.append(f"- {item.get('goal_id')} [{item.get('status')}]: "
                             f"{(item.get('goal_text') or '')[:160]}")
        if status in ("needs_human", "failed"):
            lines.append("")
            lines.append("This goal is currently stopped. Automatic advancement "
                         "will not resume it; a human must resume it explicitly.")
        elif status == "completed":
            lines.append("")
            lines.append("This goal is already completed; nothing new will be "
                         "dispatched.")
        else:
            lines.append("")
            lines.append("The LD goal runner will advance it automatically, one "
                         "bounded decision at a time, whenever the observed state "
                         "changes. Progress and verdicts come from the ledger; ask "
                         "me again later to read them.")
        lines.append("")
        if goal.get("max_decisions_total") or goal.get("max_tasks_total"):
            lines.append(
                "Lifetime budget: "
                f"{goal.get('max_decisions_total') or 'default'} decisions / "
                f"{goal.get('max_tasks_total') or 'default'} task submissions. It "
                "is stamped once and never extended by asking again.")
        else:
            lines.append("The lifetime budget is stamped when the LD first advances "
                         "this goal (bounded decisions and task submissions); it is "
                         "never extended by asking again.")
        return "\n".join(lines), {
            "project_id": project_id, "goal_id": goal_id, "goal_ref": goal_ref,
            "status": status, "reused": bool(existing),
            "max_decisions": goal.get("max_decisions_total"),
            "max_tasks": goal.get("max_tasks_total")}, [goal_id], None

    # ------------------------------------------------------------------ #
    # structured publication (console → LD goal)
    # ------------------------------------------------------------------ #
    def publish_goal(self, *, project_id: str, objective: str) -> dict[str, Any]:
        """Create or reuse the Goal for one explicitly submitted objective.

        This is **not** a new write intent and not a second execution path: it
        is ``submit_goal`` with the intent supplied by a structured form
        instead of a natural-language sentence, so no classifier runs and no
        model is called.  It therefore writes exactly one thing — a Goal row in
        ``LDGoalStore`` — through the same deterministic ``goal_identity`` as
        :meth:`_submit_goal`, which is what makes a console publication and a
        chat sentence with the same objective converge on one goal.

        What it deliberately does not do:

        * it never submits a WorkItem.  Only the LD, woken by the existing
          :class:`~ld.goal_runner.LDGoalRunner`, may call ``submit_task``;
        * it never seeds, resets or enlarges a budget.  A goal's lifetime budget
          is stamped by the runner's first advancement, and only an explicit
          human ``set_lifetime_budget`` can change it;
        * it never revives a terminal goal.  Asking again for a
          ``completed``/``failed``/``needs_human`` objective reports that status
          instead of pretending the work will continue.

        The returned ``next_step`` is a statement about *who acts next*, never a
        claim about progress or success.
        """
        owned_project = self._require_project(project_id)
        if not isinstance(objective, str):
            raise ChatValidationError("objective must be a string")
        goal_text = objective.strip()
        if not goal_text:
            raise ChatValidationError("objective must be a non-empty string")
        if len(goal_text) > MAX_GOAL_CHARS:
            # The objective *is* the goal identity, so shortening it would create
            # a different goal than the one the owner submitted.
            raise ChatValidationError(
                f"objective is {len(goal_text)} characters; the limit is "
                f"{MAX_GOAL_CHARS}. Shorten it — a truncated objective would be a "
                "different goal than the one you submitted.")

        goal_id, goal_ref = goal_identity(goal_text, owned_project)
        existing = self._goals.get(goal_id) or {}
        goal = existing or self._goals.get_or_create(
            goal_id, owned_project, goal_text, goal_ref)
        status = goal.get("status")
        if status in ("completed", "failed", "needs_human"):
            next_step = "goal_is_terminal"
        else:
            next_step = "waiting_for_ld_runner"
        return {
            "project_id": owned_project,
            "goal_id": goal_id,
            "goal_ref": goal_ref,
            "objective": goal_text,
            "goal_chars": len(goal_text),
            "status": status,
            "reused": bool(existing),
            "created": not existing,
            "next_step": next_step,
            "max_decisions_total": goal.get("max_decisions_total"),
            "max_tasks_total": goal.get("max_tasks_total"),
            "decision_count": int(goal.get("decision_count") or 0),
            "task_count": int(goal.get("task_count") or 0),
            "current_task_id": goal.get("current_task_id"),
            # Explicit, machine-readable statement of the remaining chain.  The
            # console renders it verbatim instead of inferring progress.
            "forwarding_chain": [
                "console -> LDChat.submit_goal (this call)",
                "LDGoalRunner wakes the LD when the observed state changes",
                "LD -> LDTools.submit_project_task -> ControlPlane",
                "Runtime dispatches the WorkItem to the Worker",
                "Ledger verdict is the only acceptance truth",
            ],
        }

    def stage_resume_goal(self, *, project_id: str, goal_id: str) -> dict[str, Any]:
        """Stage a resume for **one explicitly named goal** (console entry point).

        The Dashboard's "handle this stopped goal" button must not have to guess
        which goal the operator meant, and a chat sentence cannot: it resolves
        ``goal_id`` from conversation hints and would pick the first live goal of
        the project.  So this method takes the goal id from the caller and then
        runs the *existing* staging path (:meth:`_resume_goal`), which means:

        * the goal is validated to belong to the named project (refused otherwise);
        * the action is only staged — nothing changes until a later exact
          ``confirm`` turn is consumed by :meth:`_handle_confirmation`;
        * the pending action is bound to the same ``decision_count`` and stop
          signature as a chat-staged resume, so a stale confirmation is refused;
        * no budget is touched, no WorkItem is submitted, no state is written.

        A conversation is created (or reused) because that is where the durable
        pending action lives; the operator continues in it through the normal
        chat confirmation.
        """
        owned_project = self._require_project(project_id)
        goal = self._goals.get(goal_id) or {}
        if not goal:
            raise ChatPolicyError(f"no goal {goal_id}")
        if goal.get("project_id") != owned_project:
            raise ChatPolicyError(
                f"goal {goal_id} belongs to project {goal.get('project_id')}, "
                f"not {owned_project}")
        conversation = self._chats.create_conversation(project_id=owned_project)
        conversation_id = conversation["conversation_id"]
        self._chats.append_message(
            conversation_id, role="user",
            content=f"从 Dashboard 请求恢复 Goal {goal_id}（仅暂存，需二次确认）。")
        intent = ChatIntent(
            ChatIntentKind.RESUME_GOAL, owned_project, None, goal_id,
            "console requested a resume of this specific goal", {})
        answer, facts, goal_ids, pending = self._resume_goal(intent, conversation, "")
        turn = {"answer": answer, "facts": facts, "goal_ids": goal_ids,
                "pending_action": pending, "requires_confirmation": bool(pending),
                "staged_for_confirmation": bool(pending)}
        self._record_turn(conversation_id, intent, turn)
        return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                "intent_source": "console_staged_resume", **turn}

    def _resume_goal(self, intent, conversation, message):
        goal = self._goals.get(intent.goal_id) or {}
        if not goal:
            raise ChatPolicyError(f"no goal {intent.goal_id}")
        project_id = self._require_project(
            intent.project_id or goal.get("project_id"))
        if goal.get("project_id") != project_id:
            raise ChatPolicyError(
                f"goal {intent.goal_id} belongs to project "
                f"{goal.get('project_id')}, not {project_id}")
        if goal.get("status") != "needs_human":
            return (f"Goal {intent.goal_id} is {goal.get('status')}, not "
                    "needs_human; there is nothing to resume."), \
                {"goal_id": intent.goal_id, "status": goal.get("status")}, \
                [intent.goal_id], None
        # The staged confirmation is bound to the *pause event*, not just the
        # goal id: ``decision_count`` advances on every applied step (including
        # a resume), so a later pause produced by a different problem can never
        # be cleared by this earlier confirmation.
        pending = {"kind": "resume_goal", "goal_id": intent.goal_id,
                   "project_id": project_id,
                   "goal_text": (goal.get("goal_text") or "")[:300],
                   "staged_at": time.time(),
                   "decision_count": int(goal.get("decision_count") or 0),
                   "stop_signature": _stop_signature(goal)}
        answer = (
            "这个 Goal 已停止，恢复前请你确认：\n"
            f"  项目名称：{self._project_label(project_id)}\n"
            f"  项目 ID：{project_id}\n"
            f"  Goal 正文：{goal.get('goal_text') or ''}\n"
            f"  目标 ID：{intent.goal_id}\n"
            f"上一步：{json.dumps(goal.get('last_decision') or {}, ensure_ascii=False, default=str)[:300]}\n\n"
            "回复 'confirm'（或 '确认'）才会恢复，回复 'cancel' 则保持停止。"
            "恢复不会增加该 Goal 的预算。本次确认只对这一次暂停有效。")
        return answer, {"goal_id": intent.goal_id,
                        "status": goal.get("status"),
                        "decision_count": pending["decision_count"],
                        "staged": True}, [intent.goal_id], pending

    # ------------------------------------------------------------------ #
    # confirmation of a staged write
    # ------------------------------------------------------------------ #
    def _handle_confirmation(self, conversation_id: str, message: str,
                             pending: dict, *,
                             routing: ProjectRouting,
                             requested_project: str | None = None) -> dict:
        decision = normalize_confirmation(message)
        if decision is None:
            # Any other sentence supersedes the staged action: never let a stale
            # approval fire later, from a different topic.
            self._chats.set_pending_action(conversation_id, None)
            intent, intent_meta = self._classify(message, conversation_id, routing)
            turn = self._dispatch(
                intent, self._chats.get_conversation(conversation_id) or {},
                message, intent_source=intent_meta, routing=routing)
            # Same rule as a normal turn: persist what actually happened.
            effective = turn.get("intent")
            if effective:
                intent = _intent_from_dict(effective) or intent
                turn = {key: value for key, value in turn.items()
                        if key != "intent"}
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": intent_meta,
                    "superseded_pending_action": pending, **turn}

        # A confirmation may not be pointed at a different project than the one
        # that was staged.  Silently applying the staged action would create (or
        # resume) work in a project the operator just named as *not* the target;
        # applying nothing and saying so is the only honest answer.  The staged
        # action is left in place, so the operator can confirm the original one
        # or supersede it explicitly.  Bound conversations never reach here with
        # a foreign id (``handle`` refuses it earlier), which is what keeps their
        # historical behaviour unchanged.
        staged_project = pending.get("project_id")
        if requested_project and staged_project:
            wanted = self._resolve_project(requested_project)
            if wanted and wanted != self._resolve_project(staged_project):
                answer = (
                    f"我没有执行按下的确认：这次确认是针对项目 {staged_project} 的，"
                    f"而请求里指定的是 {wanted}。为避免在错误的项目上创建或恢复"
                    "目标，本次没有任何写入；原暂存操作仍然有效，可以用来确认"
                    "原项目，或发一条新消息覆盖它。")
                turn = {"answer": answer,
                        "facts": {"project_mismatch": True,
                                  "staged_project_id": staged_project,
                                  "requested_project_id": wanted,
                                  "pending": pending},
                        "goal_ids": [], "pending_action": pending,
                        "requires_confirmation": True}
                intent = ChatIntent(ChatIntentKind.CLARIFY, staged_project, None,
                                    pending.get("goal_id"),
                                    "confirmation addressed to a different project",
                                    {"question": answer})
                self._record_turn(conversation_id, intent, turn)
                return {"conversation_id": conversation_id,
                        "intent": intent.to_dict(),
                        "intent_source": "human_confirmation_project_mismatch",
                        "project_routing": routing._asdict(), **turn}

        # Consume the staged action atomically: of two concurrent confirmations
        # exactly one wins, the other sees nothing to apply.  Doing this *before*
        # the write is what makes "at most once" true rather than "usually once".
        consumed = self._chats.consume_pending_action(
            conversation_id, expected=pending)
        if consumed is None:
            answer = ("That confirmation no longer applies: the staged action was "
                      "already consumed or superseded. Nothing was changed. Ask "
                      "again if you still want it.")
            turn = {"answer": answer,
                    "facts": {"cancelled": True, "pending": pending,
                              "already_consumed": True},
                    "goal_ids": [], "pending_action": None,
                    "requires_confirmation": False}
            intent = ChatIntent(ChatIntentKind.CLARIFY, pending.get("project_id"),
                                None, pending.get("goal_id"),
                                "staged action was already consumed", {})
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": "human_confirmation_stale", **turn}

        if decision == "deny":
            item = pending.get("goal_id")
            answer = (f"Cancelled. {item} was not applied and nothing changed.")
            turn = {"answer": answer,
                    "facts": {"cancelled": True, "pending": pending},
                    "goal_ids": [item] if item else [],
                    "pending_action": None, "requires_confirmation": False}
            intent = ChatIntent(ChatIntentKind.CLARIFY, pending.get("project_id"),
                                None, item,
                                "human declined the staged action",
                                {"question": answer})
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": "human_confirmation_denied", **turn}

        kind = pending.get("kind")
        if kind == "submit_goal":
            return self._confirm_submit_goal(conversation_id, pending)
        if kind == "resume_goal":
            return self._confirm_resume_goal(conversation_id, pending)
        raise ChatPolicyError(f"unknown pending action {kind!r}")

    def _confirm_submit_goal(self, conversation_id: str, pending: dict) -> dict:
        project_id = self._require_project(pending.get("project_id"))
        goal_text = (pending.get("goal_text") or "").strip()
        if not goal_text:
            raise ChatPolicyError("staged goal has no objective; nothing was created")
        goal_id, goal_ref = goal_identity(goal_text, project_id)
        # The ID shown to the human must be the ID that gets created.  If they
        # disagree, the staged payload was tampered with or truncated somewhere,
        # and creating a *different* goal than the one confirmed is worse than
        # refusing.
        if pending.get("goal_id") and pending["goal_id"] != goal_id:
            raise ChatPolicyError(
                f"staged goal id {pending['goal_id']} does not match the objective "
                f"it would create ({goal_id}); nothing was created. Ask again to "
                "re-stage it.")
        existing = self._goals.get(goal_id) or {}
        goal = self._goals.get_or_create(goal_id, project_id, goal_text, goal_ref)
        answer = (f"Goal {goal_id} created for project {project_id}: {goal_text[:200]}"
                  + ("\n(It already existed and was reused.)" if existing else "")
                  + "\n\nThe runner will advance it automatically within its stamped "
                    "budget. Budgets are unchanged by this confirmation.")
        turn = {"answer": answer,
                "facts": {"project_id": project_id, "goal_id": goal_id,
                          "goal_ref": goal_ref, "status": goal.get("status"),
                          "reused": bool(existing), "confirmed_by_human": True},
                "goal_ids": [goal_id], "pending_action": None,
                "requires_confirmation": False}
        intent = ChatIntent(ChatIntentKind.SUBMIT_GOAL, project_id, None, goal_id,
                            "staged submit confirmed by the human owner",
                            {"goal": goal_text})
        self._record_turn(conversation_id, intent, turn, role="system")
        return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                "intent_source": "human_confirmation", **turn}

    def _confirm_resume_goal(self, conversation_id: str, pending: dict) -> dict:
        goal_id = pending.get("goal_id")
        staged_count = pending.get("decision_count")
        current = self._goals.get(goal_id) or {}
        if not current:
            raise ChatPolicyError(f"no goal {goal_id}")
        # The confirmation is bound to the pause event that was staged.  Any
        # step since — including another terminal's resume — means this
        # confirmation refers to a pause that no longer exists.
        #
        # ``decision_count`` alone is not enough: a resume by someone else
        # followed by a *new* pause advances the count by two, and a goal that
        # was never advanced has count 0 both before and after.  So the stored
        # stop signature is compared as well; it identifies *why* the goal is
        # paused, which is what the human actually read.
        stale_reason = None
        if staged_count is not None and \
                int(current.get("decision_count") or 0) != int(staged_count):
            stale_reason = (
                f"it changed since I staged this (decision count {staged_count} -> "
                f"{current.get('decision_count')})")
        elif pending.get("stop_signature") and \
                pending["stop_signature"] != _stop_signature(current):
            stale_reason = (
                "it is paused for a different reason than the one I showed you")
        if stale_reason is not None:
            answer = (f"I did not resume {goal_id}: {stale_reason}. The "
                      "confirmation no longer refers to the pause you saw. Ask "
                      "again to re-stage it.")
            turn = {"answer": answer,
                    "facts": {"goal_id": goal_id, "resumed": False,
                              "stale_confirmation": True,
                              "staged_decision_count": staged_count,
                              "current_decision_count": current.get("decision_count"),
                              "staged_stop_signature": pending.get("stop_signature"),
                              "current_stop_signature": _stop_signature(current)},
                    "goal_ids": [goal_id], "pending_action": None,
                    "requires_confirmation": False}
            intent = ChatIntent(ChatIntentKind.RESUME_GOAL, pending.get("project_id"),
                                None, goal_id, "stale confirmation ignored", {})
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": "human_confirmation_stale", **turn}
        try:
            goal = self._goals.resume(
                goal_id, note="resumed by the Human Owner through LD chat; "
                              "confirmed in an explicit follow-up turn")
        except GoalStoreError as exc:
            answer = (f"I could not resume {goal_id}: {exc}. Nothing was changed; "
                      "the goal state is unchanged.")
            turn = {"answer": answer, "facts": {"resumed": False,
                                                "error": str(exc)},
                    "goal_ids": [goal_id], "pending_action": None,
                    "requires_confirmation": False}
            intent = ChatIntent(ChatIntentKind.RESUME_GOAL, pending.get("project_id"),
                                None, goal_id, "confirmed resume failed", {})
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": "human_confirmation", **turn}
        answer = (f"Goal {goal_id} resumed (status: {goal.get('status')}). The "
                  "runner will advance it automatically; budgets are unchanged.")
        turn = {"answer": answer,
                "facts": {"goal_id": goal_id, "status": goal.get("status"),
                          "resumed": True, "budget": {
                              "max_decisions": goal.get("max_decisions_total"),
                              "max_tasks": goal.get("max_tasks_total")}},
                "goal_ids": [goal_id], "pending_action": None,
                "requires_confirmation": False}
        intent = ChatIntent(ChatIntentKind.RESUME_GOAL, pending.get("project_id"),
                            None, goal_id, "confirmed by the human owner", {})
        self._record_turn(conversation_id, intent, turn, role="system")
        return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                "intent_source": "human_confirmation", **turn}

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _record_turn(self, conversation_id: str, intent: ChatIntent,
                     turn: dict[str, Any], *, role: str = "ld") -> None:
        facts = turn.get("facts")
        routing = turn.get("project_routing")
        if isinstance(routing, dict) and routing.get("source"):
            # Persist the routing decision with the answer, including an
            # ``ambiguous`` one: "这个项目" must be able to see that the last
            # turn pointed at two projects instead of skipping back to an older
            # turn that named one.
            facts = dict(facts or {})
            facts["project_routing"] = routing
        self._chats.append_message(
            conversation_id, role=role, content=turn["answer"][:4000],
            intent=intent.to_dict(), facts=facts,
            goal_ids=turn.get("goal_ids") or [])
        self._chats.set_pending_action(conversation_id, turn.get("pending_action"))
        if self._fault_hook is not None:
            self._fault_hook("after_chat_turn")

    def _project_metrics(self, project_id: str) -> dict:
        """One project's ledger state, classified into three disjoint buckets.

        The three buckets answer three *different* questions, and conflating them
        produced the defect this replaces (a global list that said "0 needing
        attention" while rejected work and stopped goals were sitting there):

        * ``blocked_workitems``      — execution is stuck or waiting on a human
          decision now (approval, ``dispatch_unknown``, suspected lost, a
          permission/auth refusal).  Something is *queued or undecidable*.
        * ``abnormal_verdict_workitems`` — the artifact was not accepted
          (``rejected`` / ``inconclusive``).  Historical unless it also blocks;
          re-submission is an owner's decision, not a queue state.
        * ``needs_human_goals``      — goals parked for a human.

        A work item appears in at most one of the two task buckets: a task whose
        verdict is bad *and* which is also blocked (the real
        ``inconclusive`` + ``needs_human`` overlap in this ledger) is reported
        once, as blocked, and its bad verdict is carried on the same record as
        ``verdict``.  Nothing is dropped; nothing is double counted.

        Every number comes from a deterministic ledger read.  The task scan is
        bounded by :data:`MAX_REPORTED_WORK_ITEMS` and reports ``truncated``
        rather than silently presenting a partial count as complete — reading
        only the newest 10 (``recent_tasks``) is what used to hide older
        rejections.
        """
        # ``get_project_progress`` supplies the counters *and* the blocking
        # flags.  A failure here used to fall back to ``{}``, which every reader
        # then rendered as "0 tasks, 0 verified, 0 rejected" — an exact-looking
        # number that was never read.  That is the same class of error as the
        # swallowed task read below, so it is reported rather than defaulted:
        # ``counts_known`` is False and no reader may print a precise zero.
        progress: dict = {}
        counts: dict = {}
        progress_error: str | None = None
        try:
            progress = self._tools.get_project_progress(project_id) or {}
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            progress_error = f"get_project_progress: {type(exc).__name__}: {exc}"
        counts_known = progress_error is None and isinstance(
            progress.get("counts"), dict) and bool(progress.get("counts"))
        if counts_known:
            counts = progress["counts"]

        # Full project task list, not ``recent_tasks`` (which is capped at 10).
        # A failure here is *reported*, never silently turned into "no tasks":
        # swallowing it produced a report that said "0 tasks" for a project with
        # rejected work, which is exactly the class of wrong number this fix is
        # about.  ``read_error`` makes the uncertainty visible instead.
        tasks: list[dict] = []
        tasks_error: str | None = None
        try:
            tasks = self._tools.get_project_tasks(
                project_id, limit=MAX_REPORTED_WORK_ITEMS) or []
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            tasks_error = f"get_project_tasks: {type(exc).__name__}: {exc}"
        tasks_known = tasks_error is None
        truncated = len(tasks) >= MAX_REPORTED_WORK_ITEMS
        read_error = "; ".join(
            item for item in (progress_error, tasks_error) if item) or None

        # ``blocked_tasks`` from ``get_project_progress`` is the **authority** on
        # what is blocked, and it is read independently of the task list.  The
        # previous version derived the blocked set by iterating the task list,
        # which coupled two separate reads: when ``get_project_tasks`` failed,
        # the blocked list came back empty and the project was reported as
        # "阻塞：无" even though the progress projection had just told us exactly
        # which tasks were stuck.  A known blocker must never be erased by a
        # failure in an unrelated read.
        blocking_known = progress_error is None
        blocked_rows = list(progress.get("blocked_tasks") or []) if blocking_known else []
        blocked_ids = {row.get("task_id") for row in blocked_rows}
        # Titles only exist on the task list, so they are the one thing a failed
        # ``get_project_tasks`` costs us; the blocking facts themselves survive.
        title_by_id = {task.get("task_id"): task.get("title")
                       for task in tasks}

        blocked: list[dict] = [
            {
                "task_id": row.get("task_id"),
                "title": title_by_id.get(row.get("task_id")),
                "state": row.get("state"),
                "verdict": row.get("verdict"),
                "queue_status": row.get("queue_status"),
                "attention_flags": list(row.get("attention_flags") or []),
            }
            for row in blocked_rows
        ]

        abnormal: list[dict] = []
        for task in tasks:
            task_id = task.get("task_id")
            verdict = task.get("verdict")
            # Blocked tasks are reported once, under ``blocked``, with their bad
            # verdict carried along; only the rest can be "abnormal only".
            if task_id in blocked_ids:
                continue
            if verdict in ABNORMAL_VERDICTS:
                abnormal.append({
                    "task_id": task_id,
                    "title": task.get("title"),
                    "state": task.get("state"),
                    "verdict": verdict,
                    "queue_status": task.get("queue_status"),
                    "attention_flags": [f"verdict:{verdict}"],
                })

        goals: list[dict] = []
        goals_error: str | None = None
        try:
            goals = self._goal_rows(project_id, include_completed=True)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            goals_error = f"goals: {type(exc).__name__}: {exc}"
        goals_known = goals_error is None
        if goals_error:
            read_error = "; ".join(
                item for item in (read_error, goals_error) if item)
        unfinished = [goal for goal in goals
                      if goal["status"] not in TERMINAL_GOAL_STATUSES]
        needs_human = [goal for goal in goals if goal["status"] == "needs_human"]
        goal_status_counts: dict[str, int] = {}
        for goal in goals:
            key = str(goal["status"])
            goal_status_counts[key] = goal_status_counts.get(key, 0) + 1

        # Hitting the read bound means the scan stopped early, so the *detail*
        # list of abnormal verdicts may be missing older rows.  This is a
        # different statement from "the read failed": ``tasks_known`` stays True
        # and the confirmed aggregate counts are still exact — only the detail
        # enumeration is qualified.  Claiming an exact abnormal-verdict count
        # from a truncated scan would be the same overstatement this method is
        # being fixed for.
        abnormal_detail_complete = tasks_known and not truncated
        if truncated:
            abnormal_detail_note = (
                f"仅扫描前 {MAX_REPORTED_WORK_ITEMS} 条 WorkItem："
                "异常验收明细可能缺少更早的记录，不得视为精确清单"
                "（项目汇总计数仍然准确）")
        else:
            abnormal_detail_note = None

        # ``complete`` is the single flag a reader must consult before printing
        # any precise number.  It is False when *any* read failed, so a partial
        # report can never look like a complete one; ``unknown`` names exactly
        # which quantities are unknown rather than zero.
        unknown = [name for name, known in (
            ("task_counts", counts_known),
            ("work_items", tasks_known),
            ("blocked", blocking_known),
            ("goals", goals_known)) if not known]

        return {
            "project_id": project_id,
            "counts": counts,
            "counts_known": counts_known,
            "tasks_known": tasks_known,
            "blocking_known": blocking_known,
            "goals_known": goals_known,
            "complete": not unknown,
            "unknown": unknown,
            "truncated": truncated,
            "abnormal_detail_complete": abnormal_detail_complete,
            "abnormal_detail_note": abnormal_detail_note,
            "read_error": read_error,
            "work_items_total": (counts.get("total", len(tasks))
                                 if tasks_known or counts_known else None),
            "blocked_workitems": blocked,
            "abnormal_verdict_workitems": abnormal,
            "needs_human_goals": needs_human,
            "unfinished_goals": unfinished,
            "goals": goals,
            "goal_status_counts": goal_status_counts,
        }

    def _attention_items(self, progress: dict) -> list[dict]:
        """Everything a human should look at for one project, de-duplicated.

        Kept for the single-project handlers, which already hold a ``progress``
        dict.  It reads the *full* task list through :meth:`_project_metrics`
        when it knows the project, because ``progress['recent_tasks']`` is capped
        at 10 and would silently drop an older rejection.

        Order is deliberate: things waiting on a human *now* come first, then
        bad verdicts that are history.
        """
        project_id = progress.get("project_id") or progress.get("id")
        if project_id:
            metrics = self._project_metrics(project_id)
            return metrics["blocked_workitems"] + metrics[
                "abnormal_verdict_workitems"]
        # Fallback for callers that only have a progress dict: still merge the
        # two buckets and de-duplicate, but note that ``recent_tasks`` is short.
        items = [dict(task, attention_flags=list(task.get("attention_flags") or []))
                 for task in progress.get("blocked_tasks") or []]
        seen = {task.get("task_id") for task in items}
        for task in progress.get("recent_tasks") or []:
            verdict = task.get("verdict")
            if verdict not in ABNORMAL_VERDICTS:
                continue
            if task.get("task_id") in seen:
                continue
            seen.add(task.get("task_id"))
            items.append({**task, "attention_flags": [f"verdict:{verdict}"]})
        return items

    def _goal_rows(self, project_id: str, *,
                   include_completed: bool = True) -> list[dict]:
        """Goal rows with their *ledger* task verdicts attached.

        Completed goals are included by default: "what happened?" is a question
        about the ledger's history, and hiding a finished goal would make the
        chat report "no goals" for work the owner knows is done.
        """
        rows = []
        store = self._tools.service.store
        tasks_by_ref: dict[str, list[dict]] = {}
        for task in store.all():
            ref = (task.get("metadata") or {}).get("ld_goal_ref")
            if ref:
                tasks_by_ref.setdefault(ref, []).append(task)
        for goal in self._goals.find_by_project(project_id):
            status = goal.get("status")
            if not include_completed and status in ("completed", "failed"):
                continue
            goal_tasks = tasks_by_ref.get(goal.get("goal_ref"), [])
            last = goal.get("last_decision") or {}
            stop_reason = None
            if isinstance(last, dict) and last.get("stop"):
                stop_reason = str(last.get("stop"))
            elif isinstance(goal.get("stop_reason"), str):
                stop_reason = goal["stop_reason"]
            rows.append({
                "goal_id": goal.get("goal_id"),
                "goal_ref": goal.get("goal_ref"),
                "status": status,
                "goal_text": goal.get("goal_text") or "",
                "task_count": goal.get("task_count", 0),
                "model_decision_count": goal.get("model_decision_count", 0),
                "max_decisions": goal.get("max_decisions_total"),
                "max_tasks": goal.get("max_tasks_total"),
                "current_task_id": goal.get("current_task_id"),
                "verified_tasks": sum(1 for task in goal_tasks
                                      if task.get("verdict") == "verified"),
                "tasks": [{"task_id": task.get("task_id"),
                           "state": task.get("state"),
                           "verdict": task.get("verdict")}
                          for task in sorted(goal_tasks,
                                             key=lambda t: t.get("created_at") or 0)],
                "last_action": last.get("action") if isinstance(last, dict) else None,
                "last_next_state": (last.get("next_state")
                                    if isinstance(last, dict) else None),
                "stop_reason": stop_reason,
            })
        return rows

    def _recent_task_ids(self, project_id: str | None) -> list[str]:
        if not project_id:
            return []
        try:
            return [task["task_id"] for task in
                    self._tools.get_project_tasks(project_id, limit=20)]
        except Exception:  # noqa: BLE001 - hint only
            return []

    def _project_label(self, project_id: str | None) -> str:
        """A human-readable project name for a confirmation, or the id itself.

        Only used in human-facing text, and only from the registry projection
        that already passed the security filter, so it never introduces a root
        path or local-domain content.
        """
        if not project_id:
            return "（未指定）"
        for item in self._safe("list_projects", self._tools.list_projects, []):
            if item.get("project_id") == project_id:
                name = item.get("name")
                if name and "local" in self._domains:
                    return str(name)
                break
        return project_id

    def _resolve_project(self, requested: str | None) -> str | None:
        if requested is None:
            return None
        projects = self._tools.list_projects()
        for item in projects:
            if requested in (item.get("project_id"), item.get("name")):
                return item["project_id"]
        raise KeyError(f"no project {requested}")

    def _require_project(self, requested: str | None) -> str:
        """Resolve a project for a *write* intent, or refuse the turn.

        A chat sentence is not allowed to invent a project: ``ChatIntent`` only
        checks that ``project_id`` is non-empty, so every write path resolves it
        against the registry here and fails closed with a policy error (never a
        ``KeyError``, and never an implicit "create it for me").
        """
        if not requested:
            raise ChatPolicyError(
                "this request needs a registered project; name one explicitly")
        projects = self._tools.list_projects()
        for item in projects:
            if requested in (item.get("project_id"), item.get("name")):
                return item["project_id"]
        known = ", ".join(sorted(str(item.get("project_id")) for item in projects)) \
            or "none"
        raise ChatPolicyError(
            f"no project {requested}; registered projects: {known}")

    @staticmethod
    def _safe(label: str, operation, default):
        """Best-effort read used only for classifier hints."""
        try:
            return operation()
        except Exception:  # noqa: BLE001 - a hint must never break the turn
            return default


__all__ = ["CONVERSATION_WINDOW", "HeuristicIntentRouter", "IntentModel", "LDChat"]
