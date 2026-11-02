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
               project_id: str) -> dict[str, Any]:
        self._validate_project_id(project_id)
        if conversation_id is not None:
            self._validate_conversation_id(conversation_id)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"message exceeds the {MAX_MESSAGE_CHARS} character limit")

        with self._chat_session() as (chat, chats, store):
            if store.get_project(project_id) is None:
                raise KeyError(f"no project {project_id}")
            if conversation_id:
                existing = chats.get_conversation(conversation_id)
                if existing is None:
                    raise KeyError(f"no conversation {conversation_id}")
                bound = existing.get("project_id")
                if bound and bound != project_id:
                    raise ConversationProjectMismatch(
                        "conversation belongs to a different project")
            result = chat.handle(
                message, conversation_id=conversation_id, project_id=project_id)
        return self._turn_view(result)

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

    @classmethod
    def _turn_view(cls, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "conversation_id": result.get("conversation_id"),
            "answer": str(result.get("answer") or ""),
            "intent": cls._intent_view(result.get("intent")),
            "intent_source": str(result.get("intent_source") or "unknown"),
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
