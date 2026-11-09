"""Restricted UI-1 bridge to the existing conversational LD.

This module deliberately contains no chat routing, goal state machine, or
scheduler.  It recreates the exact service graph used by ``scripts/ld_cli.py``
for one request and delegates every user sentence to :meth:`LDChat.handle`.

Connections are request-scoped.  The read-only dashboard keeps using
``ReadOnlyLedger``; enabling chat creates a separate, explicit writer path for
the two operations LDChat already permits (submit a Goal and confirm a staged
resume).  Closing every store after the request also avoids sharing SQLite
connections between ``ThreadingHTTPServer`` workers.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any, Iterator

from controlplane.plane import ControlPlane
from controlplane.project_service import ProjectService
from controlplane.store import TaskStore
from ld.chat import LDChat
from ld.chat_store import LDChatStore
from ld.goal_store import LDGoalStore
from ld.model import OpenAIChatIntentModel
from ld.tools import LDTools

from .ledger import NameOnlyAdapter


CONVERSATION_ID_RE = re.compile(r"^conv-[0-9a-f]{12}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
MAX_MESSAGE_CHARS = 8_000
MAX_CONVERSATIONS = 100
MAX_MESSAGES = 500


class ChatBridgeError(RuntimeError):
    """A stable, path-free failure the HTTP layer may classify."""


#: What the console tells the operator about "who acts next".  Kept as a single
#: mapping so the wording and the state names cannot drift apart.
_NEXT_STEP_NOTES = {
    "waiting_for_ld_runner": (
        "Goal 已写入 Ledger。派工由 LD 在既有 LDGoalRunner 唤醒后执行，"
        "Dashboard 不会直接提交 WorkItem；Runner 存活状态无法从 Ledger 判断。"),
    "goal_is_terminal": (
        "该 Goal 已处于终态（completed/failed/needs_human），不会自动恢复；"
        "需要人工 resume 或重新提出一个新目标。"),
}


def _next_step_note(next_step: str | None) -> str:
    return _NEXT_STEP_NOTES.get(
        next_step or "",
        "Goal 已写入 Ledger；后续推进由既有 Runner/LD 负责。")


class ConversationProjectMismatch(ChatBridgeError):
    """An existing conversation cannot silently move to another project."""


def _domains(value: str | None, *, model_configured: bool) -> tuple[str, ...]:
    if value is None:
        value = os.environ.get("LD_MODEL_DOMAINS", "")
    # With no endpoint, all routing is in-process; this mirrors ld_cli.py and
    # does not grant an external service access to anything.
    if not model_configured and not value.strip():
        value = "local,shareable"
    return tuple(item.strip() for item in value.split(",") if item.strip())


class ChatBridge:
    """Request-scoped facade over the existing LDChat + stores."""

    def __init__(
        self,
        ledger_path: Path | str,
        *,
        model_base_url: str | None = None,
        model_name: str | None = None,
        model_api_key: str | None = None,
        model_timeout_s: float | None = None,
        model_domains: str | None = None,
        intent_model: Any | None = None,
    ):
        self.path = Path(ledger_path)
        base_url = (model_base_url
                    if model_base_url is not None
                    else os.environ.get("LD_MODEL_BASE_URL", "")).strip()
        name = (model_name
                if model_name is not None
                else os.environ.get("LD_MODEL_NAME", "")).strip()
        self.model_endpoint_configured = bool(base_url and name)
        if intent_model is not None:
            self._model = intent_model
            self.model_endpoint_configured = True
        elif self.model_endpoint_configured:
            timeout = (model_timeout_s if model_timeout_s is not None else
                       float(os.environ.get("LD_MODEL_TIMEOUT_S", "60")))
            self._model = OpenAIChatIntentModel(
                base_url=base_url,
                model=name,
                api_key=(model_api_key if model_api_key is not None
                         else os.environ.get("LD_MODEL_API_KEY")),
                timeout_s=timeout,
            )
        else:
            self._model = None
        self.authorized_domains = _domains(
            model_domains, model_configured=self.model_endpoint_configured)

    @property
    def capabilities(self) -> dict[str, Any]:
        """Safe configuration projection; never includes endpoint or key."""
        return {
            "intent_model_configured": self.model_endpoint_configured,
            "intent_routing": ("model_available" if self.model_endpoint_configured
                               else "deterministic_local_fallback"),
            "model_local_domain_authorized": "local" in self.authorized_domains,
            # Runtime is a separate process and has no durable heartbeat.  Do
            # not infer liveness from a Goal row or this process's environment.
            "goal_runner_liveness": "unknown",
            "goal_runner_note": (
                "The ledger has no runner heartbeat. This Dashboard cannot "
                "claim that LDGoalRunner is online."),
            "decision_model_runtime_status": "unknown",
        }

    def prepare(self) -> None:
        """Create existing Goal/chat tables at explicit UI-1 startup only."""
        self._require_existing_ledger()
        goals = LDGoalStore(self.path)
        chats = LDChatStore(self.path)
        chats.close()
        goals.close()

    def list_conversations(self, *, project_id: str | None = None,
                           limit: int = 50) -> list[dict[str, Any]]:
        if project_id is not None:
            self._validate_project_id(project_id)
            self._require_project(project_id)
        limit = max(1, min(int(limit), MAX_CONVERSATIONS))
        chats = LDChatStore(self.path)
        try:
            rows = chats.list_conversations(limit=MAX_CONVERSATIONS)
        finally:
            chats.close()
        if project_id is not None:
            rows = [row for row in rows if row.get("project_id") == project_id]
        return [self._conversation_view(row) for row in rows[:limit]]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        self._validate_conversation_id(conversation_id)
        chats = LDChatStore(self.path)
        try:
            conversation = chats.get_conversation(conversation_id)
            if conversation is None:
                return None
            messages = chats.messages(conversation_id, limit=MAX_MESSAGES)
        finally:
            chats.close()
        return {
            "conversation": self._conversation_view(conversation),
            "messages": [self._message_view(row) for row in messages],
        }

    def handle(self, *, message: str, conversation_id: str | None,
               project_id: str | None) -> dict[str, Any]:
        """One chat turn, optionally without a project (global LD).

        ``project_id`` is optional now:

        * ``None`` starts/continues a **global** conversation.  The LD resolves
          the project of each turn itself (named project, known task/goal id,
          or a demonstrative reference that is unambiguous within this
          conversation) and asks when it cannot.  A global conversation is
          never rebound by mentioning a project, so project A and project B can
          be discussed under one ``conversation_id``.
        * a value **binds** a new conversation to that project (historical
          behaviour) and, for an existing *global* conversation, selects the
          project for this one turn only.
        * a value on an existing *bound* conversation must equal its project.
          The old isolation rule is unchanged and cannot be bypassed by sending
          ``null``: an omitted field never rebinds anything, it only means "no
          per-turn selection".
        """
        if project_id is not None:
            self._validate_project_id(project_id)
        if conversation_id is not None:
            self._validate_conversation_id(conversation_id)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"message exceeds the {MAX_MESSAGE_CHARS} character limit")

        with self._chat_session() as (chat, chats, store):
            if project_id is not None and store.get_project(project_id) is None:
                raise KeyError(f"no project {project_id}")
            per_turn: str | None = project_id
            if conversation_id:
                existing = chats.get_conversation(conversation_id)
                if existing is None:
                    raise KeyError(f"no conversation {conversation_id}")
                bound = existing.get("project_id")
                if bound and project_id and bound != project_id:
                    raise ConversationProjectMismatch(
                        "conversation belongs to a different project")
                if bound and not project_id:
                    # Omitting the field is not a way to leave the project: a
                    # bound conversation keeps answering about its own project.
                    per_turn = None
                elif not bound:
                    # Global conversation: the field selects this turn's project
                    # and the row's project_id stays NULL (no silent rebinding).
                    per_turn = project_id
            result = chat.handle(
                message, conversation_id=conversation_id, project_id=per_turn)
        return self._turn_view(result)

    def publish_task(self, *, project_id: str, objective: str) -> dict[str, Any]:
        """Console publication: create/reuse the Goal, then stop.

        The whole point of going through :class:`LDChat` instead of calling
        ``ProjectService.submit_project_task`` here is that the Dashboard must
        not become a second dispatch path.  The LD — woken by the existing
        ``LDGoalRunner`` — is still the only component allowed to turn a goal
        into a WorkItem, so approval gates, idempotency signatures, the frozen
        Effective Prompt and the concurrency limits all keep applying exactly
        once, in the place they already live.
        """
        self._validate_project_id(project_id)
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        if len(objective) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"objective exceeds the {MAX_MESSAGE_CHARS} character limit")
        with self._chat_session() as (chat, chats, store):
            if store.get_project(project_id) is None:
                raise KeyError(f"no project {project_id}")
            published = chat.publish_goal(project_id=project_id,
                                          objective=objective)
        return {
            **published,
            "conversation_id": None,
            "runner_liveness": "unknown",
            "next_step_note": _next_step_note(published.get("next_step")),
        }

    def project_targets(self) -> list[dict[str, Any]]:
        """Name/id/root of every registered project, for the publish confirmation.

        The read-only dashboard projections deliberately omit roots (UI-0 rule:
        no local machine paths in a cockpit payload).  A *publication* is a
        different question: before creating a Goal the operator must see which
        repository it will run in, and mistaking the project is exactly the bug
        this endpoint exists to prevent.  So it is exposed only on the
        authenticated chat surface, only as the three fields needed to make that
        check, and it is still never combined with prompts or task text.
        """
        self._require_existing_ledger()
        store = TaskStore(self.path, read_only=True)
        try:
            projects = store.all_projects()
        finally:
            store.close()
        return [{"project_id": project.get("id"),
                 "name": project.get("name"),
                 "root": project.get("root"),
                 "active": bool(project.get("active"))}
                for project in sorted(projects, key=lambda item: str(item.get("id")))]

    def stage_goal_resume(self, *, project_id: str, goal_id: str) -> dict[str, Any]:
        """Stage a resume for one explicitly named stopped goal.

        This is the console's "handle this goal" entry point, and it is the same
        write intent the chat already has (``resume_goal``), reached with the goal
        id supplied by the caller instead of guessed from a sentence.  Nothing is
        resumed here: the pending action is stored in a (possibly new)
        conversation and only an exact ``confirm`` turn applies it, exactly as
        for a chat-staged resume.  No budget, WorkItem or goal state changes.
        """
        self._validate_project_id(project_id)
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise ValueError("goal_id must be a non-empty string")
        with self._chat_session() as (chat, chats, store):
            if store.get_project(project_id) is None:
                raise KeyError(f"no project {project_id}")
            staged = chat.stage_resume_goal(project_id=project_id,
                                            goal_id=goal_id.strip())
        return {
            **self._turn_view(staged),
            "runner_liveness": "unknown",
            "next_step_note": (
                "恢复尚未生效：需要在 LD Chat 的同一会话中回复 confirm（或单击"
                "「确认」）才会调用既有的 GoalStore.resume()。预算不会被扩大。"),
        }

    @contextlib.contextmanager
    def _chat_session(self) -> Iterator[tuple[LDChat, LDChatStore, TaskStore]]:
        self._require_existing_ledger()
        store = TaskStore(self.path)
        goals: LDGoalStore | None = None
        chats: LDChatStore | None = None
        try:
            read_plane = ControlPlane(NameOnlyAdapter("ledger-only"), store)
            read_service = ProjectService(read_plane)

            def submission_service(project_id: str) -> ProjectService:
                project = read_plane.project(project_id)
                adapter_name = project.get("default_adapter")
                if not adapter_name:
                    raise ValueError(
                        f"project {project_id} has no default_adapter")
                plane = ControlPlane(
                    NameOnlyAdapter(adapter_name), store,
                    require_registered_projects=True)
                return ProjectService(plane)

            tools = LDTools(
                read_service, submission_service_factory=submission_service)
            goals = LDGoalStore(store.path)
            chats = LDChatStore(store.path)
            chat = LDChat(
                tools, goals, chats, self._model,
                model_authorized_security_domains=self.authorized_domains)
            yield chat, chats, store
        finally:
            if chats is not None:
                chats.close()
            if goals is not None:
                goals.close()
            store.close()

    def _require_existing_ledger(self) -> None:
        if not self.path.exists():
            raise ChatBridgeError(
                "the configured ledger does not exist; UI-1 will not create it")

    def _require_project(self, project_id: str) -> None:
        self._require_existing_ledger()
        store = TaskStore(self.path)
        try:
            if store.get_project(project_id) is None:
                raise KeyError(f"no project {project_id}")
        finally:
            store.close()

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id has an invalid format")

    @staticmethod
    def _validate_conversation_id(conversation_id: str) -> None:
        if (not isinstance(conversation_id, str)
                or not CONVERSATION_ID_RE.fullmatch(conversation_id)):
            raise ValueError("conversation_id has an invalid format")

    @staticmethod
    def _pending_view(pending: Any) -> dict[str, Any] | None:
        if not isinstance(pending, dict):
            return None
        return {
            key: pending.get(key) for key in
            ("kind", "goal_id", "project_id", "staged_at")
            if pending.get(key) is not None
        }

    @classmethod
    def _conversation_view(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "conversation_id": row.get("conversation_id"),
            "project_id": row.get("project_id"),
            "title": row.get("title") or "",
            "message_count": int(row.get("message_count") or 0),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "requires_confirmation": bool(row.get("pending_action")),
            "pending_action": cls._pending_view(row.get("pending_action")),
        }

    @staticmethod
    def _intent_view(intent: Any) -> dict[str, Any] | None:
        if not isinstance(intent, dict):
            return None
        # Parameters can contain the full Goal objective.  The transcript
        # already contains the owner's sentence, so the web projection needs
        # only routing identity, not another arbitrary nested object.
        return {
            key: intent.get(key) for key in
            ("intent", "project_id", "task_id", "goal_id", "reason")
            if intent.get(key) is not None
        }

    @classmethod
    def _message_view(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": int(row.get("seq") or 0),
            "role": row.get("role"),
            "content": row.get("content") or "",
            "intent": cls._intent_view(row.get("intent")),
            "goal_ids": [str(item) for item in (row.get("goal_ids") or [])][:20],
            "created_at": row.get("created_at"),
        }

    @staticmethod
    def _facts_view(facts: Any) -> dict[str, Any] | None:
        """Only the two outcome flags the console needs, never free-form facts.

        ``facts`` can carry goal text, counts and stop signatures.  The web
        projection already carries the answer text and the identifiers, so it
        needs exactly "did the staged action apply, or was it declined" — which
        is what the handling dialog reports after a confirm/cancel.
        """
        if not isinstance(facts, dict):
            return None
        return {key: bool(facts[key]) for key in
                ("resumed", "cancelled", "stale_confirmation", "already_consumed",
                 "project_mismatch", "confirmed_by_human")
                if key in facts}

    @staticmethod
    def _routing_view(routing: Any) -> dict[str, Any] | None:
        """Which project the LD resolved for this turn, and how.

        The console shows "当前识别的目标项目", so it must come from the routing
        decision itself — never from what the UI had selected, and never from a
        model's prose.  ``candidates`` is included only for the ambiguous case,
        where the answer is a question rather than a target.
        """
        if not isinstance(routing, dict):
            return None
        view = {"source": routing.get("source") or "unknown"}
        if routing.get("project_id"):
            view["project_id"] = routing["project_id"]
        candidates = [str(item) for item in routing.get("candidates") or []][:20]
        if candidates:
            view["candidates"] = candidates
        return view

    @staticmethod
    def _portfolio_view(facts: Any) -> dict[str, Any] | None:
        """The structured part of a cross-project report, and nothing else.

        ``facts`` for a portfolio turn carries per-project counters, the totals
        and the priority list.  Keeping it out of the generic projection is
        deliberate (``_facts_view`` exposes only outcome flags), but the console
        needs the numbers to render a table, so they get their own narrow view.

        What is *not* passed through: goal text, task titles, stop reasons,
        warnings, project roots.  Those stay in the deterministic answer text,
        which is already rendered from the ledger with the same care as any other
        answer.  Re-exposing them here would create a second, less reviewed path
        from ledger content to the browser.
        """
        if not isinstance(facts, dict) or not facts.get("portfolio"):
            return None
        projects = []
        for row in facts.get("projects") or []:
            if not isinstance(row, dict):
                continue
            projects.append({
                "project_id": row.get("project_id"),
                "name": row.get("name"),
                "active": bool(row.get("active")),
                "counts": dict(row.get("counts") or {}),
                "blocked": int(row.get("blocked") or 0),
                "abnormal_verdict": int(row.get("abnormal_verdict") or 0),
                # A bounded scan yields a lower bound, so the console must be
                # able to render "≥N" rather than an exact figure.
                "abnormal_detail_complete": bool(
                    row.get("abnormal_detail_complete", True)),
                "abnormal_detail_note": (str(row["abnormal_detail_note"])[:300]
                                         if row.get("abnormal_detail_note")
                                         else None),
                "needs_human_goals": int(row.get("needs_human_goals") or 0),
                "goal_status_counts": dict(row.get("goal_status_counts") or {}),
                "truncated": bool(row.get("truncated")),
                # Completeness, so a console can refuse to render a precise
                # number that was never read.  Only flags and the error string
                # are forwarded — never the full ``facts`` payload.
                "complete": bool(row.get("complete", True)),
                "unknown": [str(item) for item in row.get("unknown") or []],
                "read_error": (str(row["read_error"])[:300]
                               if row.get("read_error") else None),
            })
        totals = dict(facts.get("totals") or {})
        priorities = []
        for item in facts.get("priorities") or []:
            if not isinstance(item, dict):
                continue
            priorities.append({
                "rank": item.get("rank"),
                "project_id": item.get("project_id"),
                "project": item.get("project"),
                "why": str(item.get("why") or "")[:500],
                "basis": str(item.get("basis") or "")[:300],
                "also": [str(text)[:300] for text in item.get("also") or []][:5],
            })
        return {
            "projects": projects,
            "totals": totals,
            # A cross-project report must not look complete when it is not.
            "complete": bool(facts.get("complete", True)),
            "incomplete_projects": [str(item) for item in
                                    facts.get("incomplete_projects") or []][:20],
            "priorities": priorities,
            "unknowns": [str(item)[:500] for item in
                         (facts.get("unknowns") or [])][:20],
        }

    @staticmethod
    def _project_rows_view(facts: Any) -> list[dict[str, Any]] | None:
        """Per-project counters for the registry list (no prose, no titles).

        The list turn's ``facts`` carry exactly the numbers the console tabulates:
        raw counts plus the three separately-defined attention numbers.  Free-form
        fields (titles, goal text, stop reasons, roots) are *not* forwarded; they
        remain in the deterministic answer text.
        """
        if not isinstance(facts, dict):
            return None
        rows = facts.get("projects")
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append({
                "project_id": row.get("project_id"),
                "name": row.get("name"),
                "active": bool(row.get("active")),
                "counts": dict(row.get("counts") or {}),
                "blocked": int(row.get("blocked") or 0),
                "abnormal_verdict": int(row.get("abnormal_verdict") or 0),
                "abnormal_detail_complete": bool(
                    row.get("abnormal_detail_complete", True)),
                "abnormal_detail_note": (str(row["abnormal_detail_note"])[:300]
                                         if row.get("abnormal_detail_note")
                                         else None),
                "needs_human_goals": int(row.get("needs_human_goals") or 0),
                "truncated": bool(row.get("truncated")),
                # Without these a console cannot distinguish "0 rejected" from
                # "could not read", and would render the second as the first.
                "complete": bool(row.get("complete", True)),
                "counts_known": bool(row.get("counts_known", True)),
                "blocking_known": bool(row.get("blocking_known", True)),
                "tasks_known": bool(row.get("tasks_known", True)),
                "goals_known": bool(row.get("goals_known", True)),
                "unknown": [str(item) for item in row.get("unknown") or []],
                "read_error": (str(row["read_error"])[:300]
                               if row.get("read_error") else None),
            })
        return out

    @classmethod
    def _turn_view(cls, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "conversation_id": result.get("conversation_id"),
            "answer": str(result.get("answer") or ""),
            "facts": cls._facts_view(result.get("facts")),
            "portfolio": cls._portfolio_view(result.get("facts")),
            "project_rows": cls._project_rows_view(result.get("facts")),
            "intent": cls._intent_view(result.get("intent")),
            "intent_source": str(result.get("intent_source") or "unknown"),
            "project_routing": cls._routing_view(result.get("project_routing")),
            "goal_ids": [str(item) for item in (result.get("goal_ids") or [])][:20],
            "requires_confirmation": bool(result.get("requires_confirmation")),
            "staged_for_confirmation": bool(
                result.get("staged_for_confirmation")),
            "pending_action": cls._pending_view(result.get("pending_action")),
        }


__all__ = [
    "ChatBridge", "ChatBridgeError", "ConversationProjectMismatch",
    "CONVERSATION_ID_RE", "MAX_MESSAGE_CHARS", "PROJECT_ID_RE",
]
