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


class HeuristicIntentRouter:
    """Deterministic, model-free router used when no endpoint is configured.

    It exists so the conversational surface is usable (and testable) without an
    LLM.  It is deliberately shallow: it recognises the four document-level
    intents and otherwise asks a clarifying question instead of guessing a
    write.  ``LDChat`` labels every answer with ``intent_source`` so nobody
    mistakes a keyword match for a model decision.
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

        if any(word in lowered for word in self._RESUME) and project_id:
            if goal_id:
                return self._intent("resume_goal", project_id=project_id,
                                    goal_id=goal_id, reason="keyword: resume")
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
        if any(word in lowered for word in self._SUBMIT) and project_id:
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
        turn = self._dispatch(intent, conversation, message)
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

        known_task_ids = self._recent_task_ids(project_id)
        chat_input = {
            "schema_version": "m4-d.v1",
            "message": message,
            "conversation": {
                "conversation_id": conversation_id,
                "selected_project_id": project_id,
                "selected_goal_id": selected_goal,
                "recent_turns": [
                    {"role": item["role"], "content": item["content"][:600]}
                    for item in self._chats.recent_turns(
                        conversation_id, limit=CONVERSATION_WINDOW)],
            },
            "projects": project_rows,
            "goals": goals,
            "known_task_ids": known_task_ids,
            "security": {
                "authorized_context_domains": list(self._domains),
                "project_names_included": "local" in self._domains,
                "note": ("Project roots, Effective Prompts, task text and "
                         "unapproved security domains are excluded."),
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
        if self._model is None:
            raw = HeuristicIntentRouter().classify(chat_input)
            return parse_intent(raw), "heuristic_router"
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
                  message: str) -> dict[str, Any]:
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
        answer, facts, goal_ids, pending = handler(intent, conversation, message)
        return {"answer": answer, "facts": facts, "goal_ids": goal_ids,
                "pending_action": pending,
                "requires_confirmation": bool(pending)}

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
    def _submit_goal(self, intent, conversation, message):
        project_id = intent.project_id
        goal_text = intent.parameters["goal"].strip()
        goal_id, goal_ref = goal_identity(goal_text, project_id)
        existing = self._goals.get(goal_id) or {}
        goal = self._goals.get_or_create(goal_id, project_id, goal_text, goal_ref)
        lifetime_decisions = intent.parameters.get("max_decisions")
        lifetime_tasks = intent.parameters.get("max_tasks")
        if lifetime_decisions or lifetime_tasks:
            goal = self._goals.set_lifetime_budget(
                goal_id, max_decisions=lifetime_decisions,
                max_tasks=lifetime_tasks)
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
        if intent.project_id and goal.get("project_id") != intent.project_id:
            raise ChatPolicyError(
                f"goal {intent.goal_id} belongs to project "
                f"{goal.get('project_id')}, not {intent.project_id}")
        if goal.get("status") != "needs_human":
            return (f"Goal {intent.goal_id} is {goal.get('status')}, not "
                    "needs_human; there is nothing to resume."), \
                {"goal_id": intent.goal_id, "status": goal.get("status")}, \
                [intent.goal_id], None
        pending = {"kind": "resume_goal", "goal_id": intent.goal_id,
                   "project_id": goal.get("project_id"),
                   "goal_text": (goal.get("goal_text") or "")[:300],
                   "staged_at": time.time()}
        answer = (f"Goal {intent.goal_id} is stopped: "
                  f"{(goal.get('goal_text') or '')[:200]}\n"
                  f"Last step: {json.dumps(goal.get('last_decision') or {}, ensure_ascii=False, default=str)[:300]}\n\n"
                  "Say 'confirm' (or '确认') to resume it, or 'cancel' to leave it "
                  "stopped. Resuming does not increase the goal's budget.")
        return answer, {"goal_id": intent.goal_id,
                        "status": goal.get("status"),
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
                intent, self._chats.get_conversation(conversation_id) or {}, message)
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": intent_meta,
                    "superseded_pending_action": pending, **turn}

        self._chats.set_pending_action(conversation_id, None)
        if decision == "deny":
            answer = (f"Cancelled. Goal {pending.get('goal_id')} was not resumed "
                      "and remains in needs_human.")
            turn = {"answer": answer,
                    "facts": {"cancelled": True, "pending": pending},
                    "goal_ids": [pending.get("goal_id")] if pending.get("goal_id") else [],
                    "pending_action": None, "requires_confirmation": False}
            intent = ChatIntent(ChatIntentKind.CLARIFY, pending.get("project_id"),
                                None, pending.get("goal_id"),
                                "human declined the staged action",
                                {"question": answer})
            self._record_turn(conversation_id, intent, turn)
            return {"conversation_id": conversation_id, "intent": intent.to_dict(),
                    "intent_source": "human_confirmation_denied", **turn}

        if pending.get("kind") != "resume_goal":
            raise ChatPolicyError(f"unknown pending action {pending.get('kind')!r}")
        goal_id = pending.get("goal_id")
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
                                None, goal_id, "confirmed resume failed",
                                {})
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

    @staticmethod
    def _safe(label: str, operation, default):
        """Best-effort read used only for classifier hints."""
        try:
            return operation()
        except Exception:  # noqa: BLE001 - a hint must never break the turn
            return default


__all__ = ["CONVERSATION_WINDOW", "HeuristicIntentRouter", "IntentModel", "LDChat"]
