"""Strict structured decisions for the minimal single-turn LD (M4-A).

Model output is data, never executable control-plane code.  This module accepts
only a small JSON-shaped vocabulary and rejects unknown actions, fields and
parameters before the coordinator can invoke a tool.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


class DecisionValidationError(ValueError):
    """A model decision is not in the deterministic M4-A schema."""


class DecisionPolicyError(DecisionValidationError):
    """The decision violates the *policy* contract, not just its shape.

    Raised for an unknown/forbidden action and for unknown fields.  These are
    never retried: a model that proposes ``rm -rf /`` (or invents a field) must
    be answered deterministically, not re-asked until it happens to comply.
    Missing/incomplete fields stay plain ``DecisionValidationError`` because they
    are generation defects that a retry may fix.
    """


class LDAction(str, Enum):
    INSPECT = "inspect"
    SUBMIT_TASK = "submit_task"
    WAIT = "wait"
    REQUEST_HUMAN = "request_human"
    FINISH = "finish"


_TOP_LEVEL_FIELDS = {"action", "project_id", "reason", "evidence_refs", "parameters"}
_READ_TOOLS = {
    "list_projects",
    "get_project_brief",
    "get_project_progress",
    "get_project_tasks",
    "get_task_status",
    "get_task_provenance",
}
_PROJECT_READ_TOOLS = {
    "get_project_brief", "get_project_progress", "get_project_tasks",
}
_TASK_READ_TOOLS = {"get_task_status", "get_task_provenance"}
_ASSERTION_FIELDS = {"kind", "target", "value", "description"}
_ASSERTION_KINDS = {
    "file_exists", "file_contains", "git_commit", "git_clean", "path_absent",
}
_PROJECT_COUNT_FIELDS = {
    "total", "active", "verified", "rejected", "inconclusive", "pending",
    "needs_human", "needs_approval", "dispatch_unknown", "suspected_lost",
    "session_binding_conflict",
}
_COUNT_OPERATORS = {"eq", "gte", "lte"}


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DecisionValidationError(f"{field} must be an object")
    return dict(value)


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        # An invented field is an attempt to extend the contract: policy, not
        # shape.  Never retried (see DecisionPolicyError).
        raise DecisionPolicyError(
            f"{field} has unsupported fields: " + ", ".join(sorted(unknown)))


def _require(value: Mapping[str, Any], required: set[str], field: str) -> None:
    missing = required - set(value)
    if missing:
        raise DecisionValidationError(
            f"{field} is missing required fields: " + ", ".join(sorted(missing)))


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise DecisionValidationError(f"{field} must be an array of strings")
    return [_nonempty_string(item, f"{field}[{index}]")
            for index, item in enumerate(value)]


def _integer(value: Any, field: str, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DecisionValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise DecisionValidationError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise DecisionValidationError(f"{field} must be <= {maximum}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DecisionValidationError(f"{field} must be a boolean")
    return value


def _validate_assertions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DecisionValidationError("parameters.assertions must be an array")
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"parameters.assertions[{index}]")
        _reject_unknown(item, _ASSERTION_FIELDS, f"parameters.assertions[{index}]")
        _require(item, {"kind", "target"}, f"parameters.assertions[{index}]")
        kind = _nonempty_string(item["kind"], f"parameters.assertions[{index}].kind")
        if kind not in _ASSERTION_KINDS:
            raise DecisionValidationError(
                f"parameters.assertions[{index}].kind is not allowed: {kind}")
        target = _nonempty_string(
            item["target"], f"parameters.assertions[{index}].target")
        if kind in {"file_exists", "file_contains", "path_absent"}:
            posix_target = PurePosixPath(target.replace("\\", "/"))
            if (posix_target.is_absolute() or PureWindowsPath(target).is_absolute()
                    or ".." in posix_target.parts):
                raise DecisionValidationError(
                    f"parameters.assertions[{index}].target must stay inside the workspace")
        normalized: dict[str, Any] = {
            "kind": kind,
            "target": target,
        }
        if item.get("value") is not None:
            normalized["value"] = _nonempty_string(
                item["value"], f"parameters.assertions[{index}].value")
        if item.get("description") is not None:
            normalized["description"] = _nonempty_string(
                item["description"], f"parameters.assertions[{index}].description")
        out.append(normalized)
    return out


def _validate_inspect(project_id: str | None, parameters: dict[str, Any]) -> dict:
    _reject_unknown(parameters, {"tool", "task_id", "limit"}, "parameters")
    _require(parameters, {"tool"}, "parameters")
    tool = _nonempty_string(parameters["tool"], "parameters.tool")
    if tool not in _READ_TOOLS:
        raise DecisionPolicyError(f"inspect tool is not allowed: {tool}")
    out: dict[str, Any] = {"tool": tool}
    if tool == "list_projects":
        if project_id is not None:
            raise DecisionPolicyError("list_projects requires project_id=null")
        if set(parameters) != {"tool"}:
            raise DecisionPolicyError("list_projects accepts no parameters besides tool")
    elif tool in _PROJECT_READ_TOOLS:
        if project_id is None:
            raise DecisionPolicyError(f"{tool} requires project_id")
        if "task_id" in parameters:
            raise DecisionPolicyError(f"{tool} does not accept task_id")
        if "limit" in parameters:
            if tool != "get_project_tasks":
                raise DecisionPolicyError(f"{tool} does not accept limit")
            out["limit"] = _integer(parameters["limit"], "parameters.limit",
                                    minimum=1, maximum=200)
    else:
        _require(parameters, {"task_id"}, "parameters")
        if "limit" in parameters:
            raise DecisionValidationError(f"{tool} does not accept limit")
        out["task_id"] = _nonempty_string(parameters["task_id"], "parameters.task_id")
    return out


def _validate_submit(project_id: str | None, parameters: dict[str, Any]) -> dict:
    if project_id is None:
        raise DecisionValidationError("submit_task requires project_id")
    allowed = {
        "task", "assertions", "priority", "idempotency_key", "title", "tags",
        "prerequisites", "require_verified_constraints", "workspace_id", "timeout_s",
    }
    _reject_unknown(parameters, allowed, "parameters")
    _require(parameters, {"task", "idempotency_key"}, "parameters")
    out: dict[str, Any] = {
        "task": _nonempty_string(parameters["task"], "parameters.task"),
        "idempotency_key": _nonempty_string(
            parameters["idempotency_key"], "parameters.idempotency_key"),
    }
    if "assertions" in parameters:
        out["assertions"] = _validate_assertions(parameters["assertions"])
    if "priority" in parameters:
        out["priority"] = _integer(parameters["priority"], "parameters.priority")
    if "title" in parameters:
        out["title"] = _nonempty_string(parameters["title"], "parameters.title")
    if "tags" in parameters:
        out["tags"] = _string_list(parameters["tags"], "parameters.tags")
    if "prerequisites" in parameters:
        out["prerequisites"] = _string_list(
            parameters["prerequisites"], "parameters.prerequisites")
    if "require_verified_constraints" in parameters:
        out["require_verified_constraints"] = _boolean(
            parameters["require_verified_constraints"],
            "parameters.require_verified_constraints")
    if "workspace_id" in parameters:
        out["workspace_id"] = _nonempty_string(
            parameters["workspace_id"], "parameters.workspace_id")
    if "timeout_s" in parameters:
        out["timeout_s"] = _integer(parameters["timeout_s"], "parameters.timeout_s",
                                    minimum=1, maximum=86_400)
    return out


def _validate_wait(parameters: dict[str, Any]) -> dict:
    _reject_unknown(parameters, {"task_id"}, "parameters")
    _require(parameters, {"task_id"}, "parameters")
    return {"task_id": _nonempty_string(parameters["task_id"], "parameters.task_id")}


def _validate_request_human(parameters: dict[str, Any]) -> dict:
    _reject_unknown(parameters, {"question", "blocking_reasons"}, "parameters")
    _require(parameters, {"question"}, "parameters")
    out: dict[str, Any] = {
        "question": _nonempty_string(parameters["question"], "parameters.question")
    }
    if "blocking_reasons" in parameters:
        out["blocking_reasons"] = _string_list(
            parameters["blocking_reasons"], "parameters.blocking_reasons")
    return out


def _validate_finish(project_id: str | None, evidence_refs: list[str],
                     parameters: dict[str, Any]) -> dict:
    _reject_unknown(
        parameters, {"verified_task_ids", "completion_conditions"}, "parameters")
    if not evidence_refs:
        raise DecisionPolicyError("finish requires non-empty evidence_refs")
    verified_task_ids = _string_list(
        parameters.get("verified_task_ids", []), "parameters.verified_task_ids")
    raw_conditions = parameters.get("completion_conditions", [])
    if not isinstance(raw_conditions, list):
        raise DecisionValidationError("parameters.completion_conditions must be an array")
    conditions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_conditions):
        condition = _object(raw, f"parameters.completion_conditions[{index}]")
        kind = _nonempty_string(
            condition.get("kind"), f"parameters.completion_conditions[{index}].kind")
        if kind == "task_verdict":
            _reject_unknown(
                condition, {"kind", "task_id", "expected_verdict"},
                f"parameters.completion_conditions[{index}]")
            _require(
                condition, {"kind", "task_id", "expected_verdict"},
                f"parameters.completion_conditions[{index}]")
            expected = _nonempty_string(
                condition["expected_verdict"],
                f"parameters.completion_conditions[{index}].expected_verdict")
            if expected != "verified":
                raise DecisionValidationError(
                    "finish task_verdict conditions only accept expected_verdict=verified")
            conditions.append({
                "kind": kind,
                "task_id": _nonempty_string(
                    condition["task_id"],
                    f"parameters.completion_conditions[{index}].task_id"),
                "expected_verdict": expected,
            })
        elif kind == "project_count":
            if project_id is None:
                raise DecisionPolicyError("project_count requires project_id")
            _reject_unknown(
                condition, {"kind", "field", "operator", "value"},
                f"parameters.completion_conditions[{index}]")
            _require(
                condition, {"kind", "field", "operator", "value"},
                f"parameters.completion_conditions[{index}]")
            field = _nonempty_string(
                condition["field"], f"parameters.completion_conditions[{index}].field")
            operator = _nonempty_string(
                condition["operator"],
                f"parameters.completion_conditions[{index}].operator")
            if field not in _PROJECT_COUNT_FIELDS:
                raise DecisionPolicyError(f"unsupported project_count field: {field}")
            if operator not in _COUNT_OPERATORS:
                raise DecisionPolicyError(f"unsupported project_count operator: {operator}")
            conditions.append({
                "kind": kind,
                "field": field,
                "operator": operator,
                "value": _integer(
                    condition["value"],
                    f"parameters.completion_conditions[{index}].value", minimum=0),
            })
        else:
            raise DecisionPolicyError(f"unsupported completion condition: {kind}")
    has_verified_task_condition = any(
        condition["kind"] == "task_verdict" for condition in conditions)
    if not verified_task_ids and not has_verified_task_condition:
        raise DecisionPolicyError(
            "finish requires at least one explicit verified task; project counts "
            "may only be auxiliary conditions")
    return {
        "verified_task_ids": verified_task_ids,
        "completion_conditions": conditions,
    }


@dataclass(frozen=True)
class LDDecision:
    action: LDAction
    project_id: str | None
    reason: str
    evidence_refs: tuple[str, ...]
    parameters: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LDDecision":
        value = _object(raw, "decision")
        _reject_unknown(value, _TOP_LEVEL_FIELDS, "decision")
        _require(value, _TOP_LEVEL_FIELDS, "decision")
        try:
            action = LDAction(_nonempty_string(value["action"], "decision.action"))
        except ValueError as exc:
            # Unknown action == forbidden action: policy violation, never retried.
            raise DecisionPolicyError(
                f"decision.action is not allowed: {value.get('action')!r}") from exc
        project_id = _optional_string(value["project_id"], "decision.project_id")
        reason = _nonempty_string(value["reason"], "decision.reason")
        evidence_refs = _string_list(value["evidence_refs"], "decision.evidence_refs")
        parameters = _object(value["parameters"], "decision.parameters")

        if action is LDAction.INSPECT:
            normalized = _validate_inspect(project_id, parameters)
        elif action is LDAction.SUBMIT_TASK:
            normalized = _validate_submit(project_id, parameters)
        elif action is LDAction.WAIT:
            normalized = _validate_wait(parameters)
        elif action is LDAction.REQUEST_HUMAN:
            normalized = _validate_request_human(parameters)
        else:
            normalized = _validate_finish(project_id, evidence_refs, parameters)
        return cls(action, project_id, reason, tuple(evidence_refs), normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "project_id": self.project_id,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "parameters": dict(self.parameters),
        }


def decision_schema() -> dict[str, Any]:
    """JSON-shaped contract included in every model decision request."""
    return {
        "schema_version": "m4-a.v1",
        "required": ["action", "project_id", "reason", "evidence_refs", "parameters"],
        "additional_properties": False,
        "actions": {
            "inspect": {
                "parameters": {"tool": sorted(_READ_TOOLS),
                               "task_id": "string (task tools only)",
                               "limit": "integer 1..200 (get_project_tasks only)"},
            },
            "submit_task": {
                "requires_project_id": True,
                "required_parameters": ["task", "idempotency_key"],
                "optional_parameters": [
                    "assertions", "priority", "title", "tags", "prerequisites",
                    "require_verified_constraints", "workspace_id", "timeout_s",
                ],
                "parameter_notes": {
                    "prerequisites": (
                        "titles or tags of verified Project Context entries the task "
                        "relies on. Dependencies on earlier tasks are NOT expressed "
                        "here: within a goal the control plane hands a verified "
                        "earlier task's accepted code to the next task "
                        "automatically. A task id listed here is accepted only if "
                        "that task is verified and belongs to the current goal."),
                    "idempotency_key": (
                        "required; inside a persistent goal the control plane "
                        "replaces it with a key derived from the goal step"),
                    "assertions": (
                        "artifact checks the control plane verifies independently "
                        "after the task: file_exists / file_contains / path_absent / "
                        "git_commit / git_clean / git_branch"),
                },
            },
            "wait": {"required_parameters": ["task_id"]},
            "request_human": {
                "required_parameters": ["question"],
                "optional_parameters": ["blocking_reasons"],
            },
            "finish": {
                "requires_evidence_refs": True,
                "minimum_evidence_rule": (
                    "at least one current-goal task with verdict=verified; project "
                    "counts are auxiliary only"),
                "parameters": {
                    "verified_task_ids": "array of task ids",
                    "completion_conditions": [
                        {"kind": "task_verdict", "task_id": "...",
                         "expected_verdict": "verified"},
                        {"kind": "project_count", "field": sorted(_PROJECT_COUNT_FIELDS),
                         "operator": sorted(_COUNT_OPERATORS), "value": "non-negative integer"},
                    ],
                },
            },
        },
        "forbidden_control_parameters": [
            "python", "shell", "command", "code", "approve", "deny", "handoff",
            "policy", "metadata", "allow_blocked", "effective_prompt",
        ],
    }


__all__ = [
    "DecisionValidationError", "LDAction", "LDDecision", "decision_schema",
]
