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
  fabricates a verdict.  The only two state changes it can cause are
  ``submit_goal`` (create/reuse a bounded goal row, which the existing
  :class:`~ld.goal_runner.LDGoalRunner` then advances) and ``resume_goal``
  (the existing explicit human unwedge of a ``needs_human`` goal).
* Not a second source of truth: it does not store progress.  Re-opening a
  conversation re-reads the Ledger, because an old chat summary is not current
  state.  ``CURRENT.md``-style narration can therefore always be regenerated.

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
from collections.abc import Callable, Mapping
from typing import Any

from ld.chat_intent import (
    READ_INTENTS,
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

#: Hard ceiling for a goal objective.  A longer text is refused rather than
#: truncated: the objective *is* the goal's identity, so silently shortening it
#: would create a different goal than the one that was confirmed.
MAX_GOAL_CHARS = 2000

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
        default_project = conversation.get("selected_project_id")
        project_id = self._match_project(message, projects) or default_project
        task_id = self._match_id(message, chat_input.get("known_task_ids") or [])
        goal_id = (conversation.get("selected_goal_id")
                   or chat_input.get("selected_goal_id"))

        is_question = self.is_question(message)
        mentions_verb = any(word in lowered for word in self._SUBMIT)

        # 1. Resume is a read-only staging request, and safe to recognise before
        #    the question guard (it never applies without an explicit confirm).
        if (not is_question
                and any(word in lowered for word in self._RESUME) and project_id
                and goal_id):
            return self._intent("resume_goal", project_id=project_id,
                                goal_id=goal_id, reason="keyword: resume")
        # 2. A question about a verb is answered by reading state, never by
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

    @staticmethod
    def _intent(kind: str, *, reason: str, project_id=None, task_id=None,
                goal_id=None, parameters=None) -> dict[str, Any]:
        return {"intent": kind, "project_id": project_id, "task_id": task_id,
                "goal_id": goal_id, "reason": reason,
                "parameters": dict(parameters or {})}

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
        self._tools = tools
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
        """Process one user message and return the rendered turn."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        conversation = self._chats.ensure_conversation(
            conversation_id, project_id=project_id)
        conversation_id = conversation["conversation_id"]
        selected = self._resolve_project(
            project_id or conversation.get("project_id"))
        if selected and selected != conversation.get("project_id"):
            conversation = self._chats.set_project(conversation_id, selected)

        self._chats.append_message(conversation_id, role="user",
                                   content=message.strip())

        pending = conversation.get("pending_action")
        if pending:
            return self._handle_confirmation(
                conversation_id, message, pending, project_id=selected)

        intent, intent_meta = self._classify(message, conversation_id, selected)
        if intent.project_id and intent.project_id != selected:
            # "t50 项目……" in one turn binds the conversation to that project, so
            # the next sentence ("and the task?") does not need to repeat it.
            selected = intent.project_id
            self._chats.set_project(conversation_id, selected)
        turn = self._dispatch(intent, conversation, message,
                              intent_source=intent_meta)
        self._record_turn(conversation_id, intent, turn)
        return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                "intent_source": intent_meta, **turn}

    # ------------------------------------------------------------------ #
    # classification
    # ------------------------------------------------------------------ #
    def _classify(self, message: str, conversation_id: str,
                  project_id: str | None) -> tuple[ChatIntent, str]:
        projects = self._safe("list_projects", self._tools.list_projects, [])
        if project_id is None:
            # A turn may name its project ("t50 有哪些阻塞?").  Resolving it here is
            # what makes per-project goal/task hints available to the classifier
            # instead of falling back to "no project" for the whole turn.
            project_id = HeuristicIntentRouter._match_project(message, projects)
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
                  confirmed_stage: bool = False) -> dict[str, Any]:
        handlers = {
            ChatIntentKind.LIST_PROJECTS: self._list_projects,
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
        if (intent.intent is ChatIntentKind.SUBMIT_GOAL
                and intent_source == "model" and not confirmed_stage
                and not HeuristicIntentRouter.is_imperative_submission(message)):
            # The model asked to create work; a deterministic second opinion
            # does not read the sentence as an instruction.  Stage, never apply.
            staged = True
            answer, facts, goal_ids, pending = self._submit_goal(
                intent, conversation, message, staged=True)
        else:
            answer, facts, goal_ids, pending = handler(intent, conversation, message)
        return {"answer": answer, "facts": facts, "goal_ids": goal_ids,
                "pending_action": pending,
                "requires_confirmation": bool(pending),
                "staged_for_confirmation": staged}

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
            progress = self._safe("get_project_progress",
                                  lambda pid=item["project_id"]:
                                  self._tools.get_project_progress(pid), None)
            counts = (progress or {}).get("counts") or {}
            lines.append(
                f"- {item.get('name')} ({item['project_id']})"
                f"{'' if item.get('active') else ' [disabled]'}: "
                f"{counts.get('total', item.get('task_count', 0))} tasks, "
                f"{counts.get('verified', item.get('verified', 0))} verified, "
                f"{counts.get('rejected', item.get('rejected', 0))} rejected, "
                f"{len((progress or {}).get('blocked_tasks') or [])} needing attention")
            rows.append({"project_id": item["project_id"], "name": item.get("name"),
                         "active": item.get("active"), "counts": counts,
                         "blocked": len((progress or {}).get("blocked_tasks") or [])})
        return "\n".join(lines), {"projects": rows}, [], None

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
        return "\n".join(lines), {"task": status}, [], None

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
            preview = goal_text if len(goal_text) <= 300 else goal_text[:300] + "…"
            answer = (f"I read this as a request to start work in {project_id}:\n"
                      f"  {preview}\n\n"
                      f"It would create goal {goal_id}. If that is not what you "
                      "meant, say 'cancel'. Say 'confirm' to create it; nothing "
                      "has been created or dispatched yet, and no budget is "
                      "granted here.")
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
        answer = (f"Goal {intent.goal_id} is stopped: "
                  f"{(goal.get('goal_text') or '')[:200]}\n"
                  f"Last step: {json.dumps(goal.get('last_decision') or {}, ensure_ascii=False, default=str)[:300]}\n\n"
                  "Say 'confirm' (or '确认') to resume it, or 'cancel' to leave it "
                  "stopped. Resuming does not increase the goal's budget. This "
                  "confirmation applies to this pause only.")
        return answer, {"goal_id": intent.goal_id,
                        "status": goal.get("status"),
                        "decision_count": pending["decision_count"],
                        "staged": True}, [intent.goal_id], pending

    # ------------------------------------------------------------------ #
    # confirmation of a staged write
    # ------------------------------------------------------------------ #
    def _handle_confirmation(self, conversation_id: str, message: str,
                             pending: dict, *, project_id: str | None) -> dict:
        decision = normalize_confirmation(message)
        if decision is None:
            # Any other sentence supersedes the staged action: never let a stale
            # approval fire later, from a different topic.
            self._chats.set_pending_action(conversation_id, None)
            intent, intent_meta = self._classify(message, conversation_id, project_id)
            turn = self._dispatch(
                intent, self._chats.get_conversation(conversation_id) or {},
                message, intent_source=intent_meta)
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": intent_meta,
                    "superseded_pending_action": pending, **turn}

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
        self._chats.append_message(
            conversation_id, role=role, content=turn["answer"][:4000],
            intent=intent.to_dict(), facts=turn.get("facts"),
            goal_ids=turn.get("goal_ids") or [])
        self._chats.set_pending_action(conversation_id, turn.get("pending_action"))
        if self._fault_hook is not None:
            self._fault_hook("after_chat_turn")

    def _attention_items(self, progress: dict) -> list[dict]:
        """Everything a human should look at: attention flags *and* bad verdicts.

        ``ProjectService.get_project_progress()['blocked_tasks']`` covers tasks
        that need a decision (approval, dispatch_unknown, lost, ...).  A
        ``rejected`` or ``inconclusive`` verdict is not "blocking" in that sense
        — nothing is waiting on a queue — but a project owner asking "what needs
        me?" must still be told about it.  Both are derived from the ledger;
        nothing here is inferred from chat history.
        """
        items = [dict(task, attention_flags=list(task.get("attention_flags") or []))
                 for task in progress.get("blocked_tasks") or []]
        seen = {task.get("task_id") for task in items}
        for task in progress.get("recent_tasks") or []:
            verdict = task.get("verdict")
            if verdict not in ("rejected", "inconclusive"):
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
