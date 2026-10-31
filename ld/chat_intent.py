"""Strict structured intents for the conversational LD (M4-D).

The chat interface is an *interaction and routing* layer.  A model may only
classify one user turn into this small vocabulary; it cannot execute anything,
cannot approve anything, and cannot supply prose that later gets treated as a
project fact.  Every answer a user sees is rendered deterministically by
:mod:`ld.chat` from Ledger / Goal / Project Context rows.

Two consequences matter for safety:

* **Policy vs shape.**  An unknown intent kind, an unknown field, an
  unsupported parameter or a forbidden capability is a policy violation
  (``ChatPolicyError``): the turn fails closed and is never retried.  A
  missing/invalid field is a generation defect (``ChatValidationError``) — the
  same split the decision validator uses.
* **What a shape defect does here.**  Unlike the LD decision loop, chat has no
  resample budget: a ``ChatValidationError`` from a model intent ends the turn
  as a ``clarify`` answer (``intent_source='invalid_model_intent'``) so the
  human rephrases, instead of silently retrying a model that produced garbage.
  Callers must therefore treat it as "validation failed, clarification asked",
  not as "will be retried automatically".
* **No write without a control-plane path.**  ``submit_goal`` and
  ``resume_goal`` only *name* an intent; ``LDChat`` performs them through the
  existing Goal store, and ``resume_goal`` additionally requires an explicit
  confirmation token from the human (never from the model).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ChatValidationError(ValueError):
    """A model intent is not in the M4-D chat schema (retryable shape defect)."""


class ChatPolicyError(ChatValidationError):
    """The turn proposes an action outside the chat contract (never retried)."""


class ChatIntentKind(str, Enum):
    LIST_PROJECTS = "list_projects"
    PROJECT_STATUS = "project_status"
    EXPLAIN_BLOCKERS = "explain_blockers"
    GOAL_STATUS = "goal_status"
    TASK_STATUS = "task_status"
    KNOWLEDGE_QUERY = "knowledge_query"
    SUBMIT_GOAL = "submit_goal"
    RESUME_GOAL = "resume_goal"
    CLARIFY = "clarify"


#: Intent kinds that only read.  Everything else is gated.
READ_INTENTS = frozenset({
    ChatIntentKind.LIST_PROJECTS, ChatIntentKind.PROJECT_STATUS,
    ChatIntentKind.EXPLAIN_BLOCKERS, ChatIntentKind.GOAL_STATUS,
    ChatIntentKind.TASK_STATUS, ChatIntentKind.KNOWLEDGE_QUERY,
    ChatIntentKind.CLARIFY,
})

#: Intents that change control-plane state.  Both go through existing, already
#: bounded machinery (goal creation / explicit human resume).
WRITE_INTENTS = frozenset({ChatIntentKind.SUBMIT_GOAL, ChatIntentKind.RESUME_GOAL})

#: Intents that must never execute on a model's say-so alone.
CONFIRMATION_REQUIRED = frozenset({ChatIntentKind.RESUME_GOAL})

_TOP_FIELDS = {"intent", "project_id", "task_id", "goal_id", "parameters", "reason"}

_PARAMS: dict[ChatIntentKind, set[str]] = {
    ChatIntentKind.LIST_PROJECTS: set(),
    ChatIntentKind.PROJECT_STATUS: set(),
    ChatIntentKind.EXPLAIN_BLOCKERS: set(),
    ChatIntentKind.GOAL_STATUS: {"include_completed"},
    ChatIntentKind.TASK_STATUS: set(),
    ChatIntentKind.KNOWLEDGE_QUERY: {"kind", "tags", "keywords", "limit"},
    # Deliberately no max_decisions / max_tasks: a lifetime budget is a Human
    # Owner management operation (``LDGoalStore.set_lifetime_budget``), never a
    # side effect of a chat sentence or of a model output.  Passing one is an
    # unsupported parameter, i.e. a policy violation (M4-D audit P0-3).
    ChatIntentKind.SUBMIT_GOAL: {"goal"},
    ChatIntentKind.RESUME_GOAL: set(),
    ChatIntentKind.CLARIFY: {"question"},
}

_REQUIRES_PROJECT = frozenset({
    ChatIntentKind.PROJECT_STATUS, ChatIntentKind.EXPLAIN_BLOCKERS,
    ChatIntentKind.GOAL_STATUS, ChatIntentKind.KNOWLEDGE_QUERY,
    ChatIntentKind.SUBMIT_GOAL,
})
_REQUIRES_TASK = frozenset({ChatIntentKind.TASK_STATUS})
_REQUIRES_GOAL = frozenset({ChatIntentKind.RESUME_GOAL})

#: Context kinds a knowledge query may filter on (mirrors
#: ``controlplane.project_context.KINDS``; duplicated so this module stays
#: importable without the store).
KNOWLEDGE_KINDS = ("background", "architecture", "constraints", "progress",
                   "knowledge", "decisions")


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChatValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, field)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ChatValidationError(f"{field} must be a boolean")
    return value


def _int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChatValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ChatValidationError(f"{field} must be between {minimum} and {maximum}")
    return value


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ChatValidationError(f"{field} must be an array of strings")
    return [_nonempty(item, f"{field}[{index}]") for index, item in enumerate(value)]


@dataclass(frozen=True)
class ChatIntent:
    intent: ChatIntentKind
    project_id: str | None
    task_id: str | None
    goal_id: str | None
    reason: str
    parameters: dict[str, Any]

    @property
    def is_read(self) -> bool:
        return self.intent in READ_INTENTS

    @property
    def needs_confirmation(self) -> bool:
        return self.intent in CONFIRMATION_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "reason": self.reason,
            "parameters": dict(self.parameters),
        }


def parse_intent(raw: Any) -> ChatIntent:
    """Validate one model classificaton into a :class:`ChatIntent`."""
    if not isinstance(raw, dict):
        raise ChatValidationError("intent output must be one object")
    unknown = set(raw) - _TOP_FIELDS
    if unknown:
        raise ChatPolicyError(
            "intent has unsupported fields: " + ", ".join(sorted(unknown)))
    missing = _TOP_FIELDS - set(raw)
    if missing:
        raise ChatValidationError(
            "intent is missing required fields: " + ", ".join(sorted(missing)))
    try:
        kind = ChatIntentKind(_nonempty(raw["intent"], "intent"))
    except ValueError as exc:
        raise ChatPolicyError(
            f"intent is not allowed: {raw.get('intent')!r}") from exc

    project_id = _optional_id(raw["project_id"], "project_id")
    task_id = _optional_id(raw["task_id"], "task_id")
    goal_id = _optional_id(raw["goal_id"], "goal_id")
    reason = _nonempty(raw["reason"], "reason")
    params = raw["parameters"]
    if not isinstance(params, dict):
        raise ChatValidationError("parameters must be an object")
    unknown_params = set(params) - _PARAMS[kind]
    if unknown_params:
        raise ChatPolicyError(
            f"{kind.value} has unsupported parameters: "
            + ", ".join(sorted(unknown_params)))

    normalized: dict[str, Any] = {}
    if kind in _REQUIRES_PROJECT and not project_id:
        raise ChatValidationError(f"{kind.value} requires project_id")
    if kind in _REQUIRES_TASK and not task_id:
        raise ChatValidationError(f"{kind.value} requires task_id")
    if kind in _REQUIRES_GOAL and not goal_id:
        raise ChatValidationError(f"{kind.value} requires goal_id")
    if kind is ChatIntentKind.LIST_PROJECTS and project_id:
        raise ChatPolicyError("list_projects must not name a project")

    if kind is ChatIntentKind.GOAL_STATUS:
        if "include_completed" in params:
            normalized["include_completed"] = _bool(
                params["include_completed"], "parameters.include_completed")
    elif kind is ChatIntentKind.KNOWLEDGE_QUERY:
        if "kind" in params:
            value = _nonempty(params["kind"], "parameters.kind")
            if value not in KNOWLEDGE_KINDS:
                raise ChatPolicyError(f"unsupported context kind: {value}")
            normalized["kind"] = value
        if "tags" in params:
            normalized["tags"] = _str_list(params["tags"], "parameters.tags")
        if "keywords" in params:
            normalized["keywords"] = _nonempty(
                params["keywords"], "parameters.keywords")
        if "limit" in params:
            normalized["limit"] = _int(params["limit"], "parameters.limit",
                                       minimum=1, maximum=50)
    elif kind is ChatIntentKind.SUBMIT_GOAL:
        normalized["goal"] = _nonempty(params.get("goal"), "parameters.goal")
    elif kind is ChatIntentKind.CLARIFY:
        normalized["question"] = _nonempty(
            params.get("question"), "parameters.question")

    return ChatIntent(intent=kind, project_id=project_id, task_id=task_id,
                      goal_id=goal_id, reason=reason, parameters=normalized)


def chat_intent_schema() -> dict[str, Any]:
    """JSON-shaped contract embedded in every chat classification request."""
    return {
        "schema_version": "m4-d.v1",
        "required": sorted(_TOP_FIELDS),
        "additional_properties": False,
        "intents": {
            "list_projects": {"parameters": {}},
            "project_status": {"requires_project_id": True, "parameters": {}},
            "explain_blockers": {"requires_project_id": True, "parameters": {}},
            "goal_status": {"requires_project_id": True,
                            "parameters": {"include_completed": "boolean"}},
            "task_status": {"requires_task_id": True, "parameters": {}},
            "knowledge_query": {
                "requires_project_id": True,
                "parameters": {"kind": list(KNOWLEDGE_KINDS),
                               "tags": "array of strings",
                               "keywords": "string",
                               "limit": "integer 1..50"},
            },
            "submit_goal": {
                "requires_project_id": True,
                "parameters": {"goal": "string (the owner's objective)"},
                "note": ("budgets are not settable here: a goal's lifetime budget "
                         "is stamped by the runner or changed by an explicit human "
                         "management operation, never by a chat turn"),
            },
            "resume_goal": {
                "requires_goal_id": True,
                "parameters": {},
                "note": ("only propose this for a goal in needs_human; the control "
                         "plane asks the human to confirm before applying it"),
            },
            "clarify": {"parameters": {"question": "string"}},
        },
        "forbidden": [
            "python", "shell", "command", "code", "approve", "deny",
            "write_context", "verify", "handoff", "policy", "metadata",
            "effective_prompt", "dispatch",
        ],
    }


__all__ = [
    "ChatIntent", "ChatIntentKind", "ChatPolicyError", "ChatValidationError",
    "CONFIRMATION_REQUIRED", "KNOWLEDGE_KINDS", "READ_INTENTS", "WRITE_INTENTS",
    "chat_intent_schema", "parse_intent",
]
