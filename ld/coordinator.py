"""Minimal one-turn coordinator for the single global LD (M4-A).

The coordinator asks an injected model for exactly one structured decision,
validates it, applies deterministic safety gates, and invokes at most one LD
action.  It is not an autonomous loop and it does not implement approvals,
Handoff, remote execution, or arbitrary model-authored commands.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ld.decision import (
    DecisionValidationError,
    LDAction,
    LDDecision,
    decision_schema,
)
from ld.tools import LDTools

_SECURITY_DOMAINS = {"local", "shareable"}
_HUMAN_ATTENTION_FLAGS = {
    "needs_approval",
    "dispatch_unknown",
    "suspected_lost",
    "session_binding_conflict",
}


class DecisionModel(Protocol):
    """Small injectable boundary; no Agent Framework is required."""

    def decide(self, decision_input: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return one JSON-shaped M4-A decision."""


class LDCoordinator:
    """Execute one globally coordinated LD decision."""

    def __init__(
        self,
        tools: LDTools,
        model: DecisionModel | Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        *,
        model_authorized_security_domains: tuple[str, ...] = (),
        goal_security_domain: str = "local",
    ):
        unknown = set(model_authorized_security_domains) - _SECURITY_DOMAINS
        if unknown:
            raise ValueError(
                "unknown model security domains: " + ", ".join(sorted(unknown)))
        if goal_security_domain not in _SECURITY_DOMAINS:
            raise ValueError(f"unknown goal security domain: {goal_security_domain}")
        self._tools = tools
        self._model = model
        self._model_domains = tuple(sorted(set(model_authorized_security_domains)))
        self._goal_security_domain = goal_security_domain

    def run_once(
        self,
        goal: str,
        project_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Plan and optionally execute exactly one LD action."""
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")

        projects = self._tools.list_projects()
        selected_id = self._resolve_project_id(projects, project_id)
        if self._model is None:
            return self._human_result(
                project_id=selected_id,
                reason="LD decision model is not configured; no decision was fabricated",
                next_state="model_not_configured",
                blocking_reasons=["model_not_configured"],
                evidence_refs=[],
                source="coordinator",
                dry_run=dry_run,
            )
        if self._goal_security_domain not in self._model_domains:
            return self._human_result(
                project_id=selected_id,
                reason=(
                    f"model is not authorized for the goal security domain "
                    f"{self._goal_security_domain!r}; model was not called"
                ),
                next_state="human_required",
                blocking_reasons=[
                    f"security_domain_unauthorized:{self._goal_security_domain}"
                ],
                evidence_refs=[],
                source="security_guard",
                dry_run=dry_run,
            )
        brief = None
        full_progress = None
        model_progress = None
        if selected_id:
            brief = self._tools.get_project_brief(
                selected_id, allowed_security_domains=self._model_domains)
            full_progress = self._tools.get_project_progress(selected_id)
            model_progress = self._tools.get_project_progress(
                selected_id, allowed_security_domains=self._model_domains)

            blockers = self._project_attention_blockers(full_progress)
            location_problem = self._execution_location_problem(brief)
            if location_problem:
                blockers.append(location_problem)
            if blockers:
                return self._human_result(
                    project_id=selected_id,
                    reason="deterministic project safety gate requires Human Owner input",
                    next_state="human_required",
                    blocking_reasons=blockers,
                    evidence_refs=self._blocker_evidence(full_progress),
                    source="deterministic_guard",
                    dry_run=dry_run,
                )

        model_input = self._decision_input(
            goal.strip(), projects, selected_id, brief, model_progress)
        try:
            raw = (self._model.decide(model_input)
                   if hasattr(self._model, "decide") else self._model(model_input))
            if not isinstance(raw, Mapping):
                raise DecisionValidationError("model output must be one structured object")
            decision = LDDecision.from_mapping(raw)
            decision = self._resolve_decision_project(decision, projects, selected_id)
            self._validate_decision_scope(decision, selected_id)
        except Exception as exc:  # model/validation boundary must fail closed
            return self._human_result(
                project_id=selected_id,
                reason=f"model decision rejected: {type(exc).__name__}: {exc}",
                next_state="invalid_model_decision",
                blocking_reasons=["model_decision_invalid"],
                evidence_refs=[],
                source="decision_validator",
                dry_run=dry_run,
            )

        target_progress = full_progress
        target_brief = brief
        if decision.project_id and decision.project_id != selected_id:
            target_brief = self._tools.get_project_brief(decision.project_id)
            target_progress = self._tools.get_project_progress(decision.project_id)
            blockers = self._project_attention_blockers(target_progress)
            location_problem = self._execution_location_problem(target_brief)
            if location_problem:
                blockers.append(location_problem)
            if blockers:
                return self._human_result(
                    project_id=decision.project_id,
                    reason="deterministic project safety gate requires Human Owner input",
                    next_state="human_required",
                    blocking_reasons=blockers,
                    evidence_refs=self._blocker_evidence(target_progress),
                    source="deterministic_guard",
                    dry_run=dry_run,
                    decision=decision,
                )

        merge_blockers = self._relevant_needs_merge(decision, target_progress)
        if merge_blockers:
            return self._human_result(
                project_id=decision.project_id,
                reason="needs_merge context affects the proposed task prerequisites",
                next_state="human_required",
                blocking_reasons=merge_blockers,
                evidence_refs=[
                    f"context:{entry['entry_id']}"
                    for entry in (target_progress or {}).get("needs_merge_context", [])
                ],
                source="deterministic_guard",
                dry_run=dry_run,
                decision=decision,
            )

        decision_location_problem = self._decision_execution_location_problem(
            decision, target_brief)
        if decision_location_problem:
            return self._human_result(
                project_id=decision.project_id,
                reason="proposed task execution location is not safely executable",
                next_state="human_required",
                blocking_reasons=[decision_location_problem],
                evidence_refs=list(decision.evidence_refs),
                source="deterministic_guard",
                dry_run=dry_run,
                decision=decision,
            )

        if dry_run:
            return {
                "action": decision.action.value,
                "project_id": decision.project_id,
                "reason": decision.reason,
                "evidence_refs": list(decision.evidence_refs),
                "task_id": decision.parameters.get("task_id"),
                "next_state": "planned",
                "executed": False,
                "dry_run": True,
                "decision_source": "model",
                "decision": decision.to_dict(),
            }

        try:
            return self._execute(decision)
        except Exception as exc:  # tool boundary fails closed; never invent success
            return self._human_result(
                project_id=decision.project_id,
                reason=f"LD action failed: {type(exc).__name__}: {exc}",
                next_state="human_required",
                blocking_reasons=["ld_action_failed"],
                evidence_refs=list(decision.evidence_refs),
                source="tool_boundary",
                dry_run=False,
                decision=decision,
            )

    # ------------------------------------------------------------------ #
    # bounded model input
    # ------------------------------------------------------------------ #
    def _decision_input(
        self,
        goal: str,
        projects: list[dict],
        selected_id: str | None,
        brief: dict | None,
        progress: dict | None,
    ) -> dict[str, Any]:
        local_allowed = "local" in self._model_domains
        project_rows = []
        for item in projects:
            row = {
                "project_id": item["project_id"],
                "active": item.get("active"),
                "context_version": item.get("context_version"),
                "task_count": item.get("task_count"),
                "verified": item.get("verified"),
                "rejected": item.get("rejected"),
            }
            if local_allowed:
                row["name"] = item.get("name")
            project_rows.append(row)

        selected = None
        if brief and progress:
            selected = {
                "brief": {
                    key: brief.get(key)
                    for key in (
                        "project_id", "active", "context_version", "entry_count",
                        "verified_count", "background", "constraints", "progress",
                        "open_questions", "context_visibility", "tasks",
                    )
                },
                "workspaces": [
                    {key: workspace.get(key) for key in (
                        "id", "node_id", "is_default", "active", "executable")}
                    for workspace in brief.get("workspaces", [])
                ],
                "progress": {
                    "project_id": progress.get("project_id"),
                    "context_version": progress.get("context_version"),
                    "counts": progress.get("counts"),
                    "derived_from": progress.get("derived_from"),
                    "blocked_tasks": [
                        {key: task.get(key) for key in (
                            "task_id", "state", "verdict", "queue_status",
                            "attention_flags")}
                        for task in progress.get("blocked_tasks", [])
                    ],
                    "narrative_progress": progress.get("narrative_progress", []),
                    "needs_merge_context": progress.get("needs_merge_context", []),
                    "recent_tasks": [
                        self._model_task_row(task, local_allowed)
                        for task in progress.get("recent_tasks", [])
                    ],
                },
            }
        return {
            "schema_version": "m4-a.v1",
            "goal": goal,
            "requested_project_id": selected_id,
            "projects": project_rows,
            "selected_project": selected,
            "security": {
                "authorized_project_context_domains": list(self._model_domains),
                "goal_security_domain": self._goal_security_domain,
                "local_project_content_included": local_allowed,
                "note": (
                    "Project roots, Effective Prompts and unapproved security domains "
                    "are excluded from model input."
                ),
            },
            "rules": [
                "Return exactly one structured decision; never return Python or shell code.",
                "Do not approve, deny, modify constraints/verified knowledge, mutate an "
                "Effective Prompt, dispatch a Worker directly, or request Handoff.",
                "Use request_human for approval, uncertainty, unsafe execution location, "
                "or unresolved control-plane state.",
                "Use finish only with verified task evidence or deterministic completion "
                "conditions.",
            ],
            "decision_schema": decision_schema(),
        }

    @staticmethod
    def _model_task_row(task: dict, local_allowed: bool) -> dict:
        row = {key: task.get(key) for key in (
            "task_id", "state", "verdict", "queue_status", "context_version",
            "created_at",
        )}
        if local_allowed:
            row["title"] = task.get("title")
        return row

    # ------------------------------------------------------------------ #
    # deterministic guards
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_project_id(projects: list[dict], requested: str | None) -> str | None:
        if requested is None:
            return None
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("project_id must be a non-empty id or name")
        matches = [item for item in projects
                   if requested in (item.get("project_id"), item.get("name"))]
        if not matches:
            raise KeyError(f"no project {requested}")
        return matches[0]["project_id"]

    @staticmethod
    def _resolve_decision_project(
        decision: LDDecision,
        projects: list[dict],
        selected_id: str | None,
    ) -> LDDecision:
        if decision.project_id is None:
            return decision
        matches = [item for item in projects
                   if decision.project_id in (item.get("project_id"), item.get("name"))]
        if not matches:
            raise DecisionValidationError(f"unknown decision project {decision.project_id}")
        resolved = matches[0]["project_id"]
        if selected_id and resolved != selected_id:
            raise DecisionValidationError(
                f"decision project {resolved} does not match requested project {selected_id}")
        return LDDecision(
            action=decision.action,
            project_id=resolved,
            reason=decision.reason,
            evidence_refs=decision.evidence_refs,
            parameters=decision.parameters,
        )

    @staticmethod
    def _validate_decision_scope(decision: LDDecision, selected_id: str | None) -> None:
        if selected_id and decision.project_id not in (selected_id, None):
            raise DecisionValidationError("decision escaped the requested project scope")
        if (selected_id and decision.action not in (LDAction.INSPECT, LDAction.REQUEST_HUMAN)
                and decision.project_id is None):
            raise DecisionValidationError("project-scoped action requires project_id")

    @staticmethod
    def _project_attention_blockers(progress: dict) -> list[str]:
        blockers: list[str] = []
        for task in progress.get("blocked_tasks", []):
            for flag in task.get("attention_flags", []):
                if flag in _HUMAN_ATTENTION_FLAGS:
                    blockers.append(f"{flag}:task:{task.get('task_id')}")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _blocker_evidence(progress: dict) -> list[str]:
        return [f"task:{task['task_id']}" for task in progress.get("blocked_tasks", [])
                if task.get("task_id")]

    @staticmethod
    def _execution_location_problem(brief: dict) -> str | None:
        if not brief.get("active"):
            return "project_disabled"
        workspaces = brief.get("workspaces", [])
        defaults = [workspace for workspace in workspaces if workspace.get("is_default")]
        if len(defaults) != 1:
            return "execution_location_ambiguous:no_single_default_workspace"
        workspace = defaults[0]
        if not workspace.get("active"):
            return f"execution_location_unavailable:{workspace.get('id')}:inactive"
        if workspace.get("node_id") != "local" or not workspace.get("executable"):
            return f"execution_location_unsupported:{workspace.get('id')}:remote"
        return None

    @staticmethod
    def _decision_execution_location_problem(
        decision: LDDecision,
        brief: dict | None,
    ) -> str | None:
        if decision.action is not LDAction.SUBMIT_TASK:
            return None
        if not brief:
            return "execution_location_ambiguous:no_project_brief"
        requested = decision.parameters.get("workspace_id")
        workspaces = brief.get("workspaces", [])
        if requested:
            matches = [workspace for workspace in workspaces
                       if workspace.get("id") == requested]
            if len(matches) != 1:
                return f"execution_location_unknown:{requested}"
            workspace = matches[0]
        else:
            defaults = [workspace for workspace in workspaces
                        if workspace.get("is_default")]
            if len(defaults) != 1:
                return "execution_location_ambiguous:no_single_default_workspace"
            workspace = defaults[0]
        if not workspace.get("active"):
            return f"execution_location_unavailable:{workspace.get('id')}:inactive"
        if workspace.get("node_id") != "local" or not workspace.get("executable"):
            return f"execution_location_unsupported:{workspace.get('id')}:remote"
        return None

    @staticmethod
    def _relevant_needs_merge(
        decision: LDDecision,
        progress: dict | None,
    ) -> list[str]:
        if decision.action is not LDAction.SUBMIT_TASK or not progress:
            return []
        prerequisites = set(decision.parameters.get("prerequisites") or [])
        require_constraints = bool(
            decision.parameters.get("require_verified_constraints"))
        blockers: list[str] = []
        for entry in progress.get("needs_merge_context", []):
            entry_tags = set(entry.get("tags") or [])
            relevant = bool(prerequisites & entry_tags)
            relevant = relevant or entry.get("entry_id") in prerequisites
            relevant = relevant or entry.get("title") in prerequisites
            relevant = relevant or (
                require_constraints and entry.get("kind") == "constraints")
            if relevant:
                blockers.append(f"needs_merge:context:{entry.get('entry_id')}")
        return blockers

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def _execute(self, decision: LDDecision) -> dict[str, Any]:
        if decision.action is LDAction.INSPECT:
            result = self._inspect(decision)
            return self._result(
                decision,
                result=result,
                task_id=decision.parameters.get("task_id"),
                next_state="inspection_complete",
            )
        if decision.action is LDAction.SUBMIT_TASK:
            result = self._tools.submit_project_task(
                decision.project_id or "", **decision.parameters)
            flags = set(result.get("attention_flags") or [])
            if result.get("queue_status") == "needs_approval":
                flags.add("needs_approval")
            next_state = "human_required" if flags else "task_submitted"
            return self._result(
                decision,
                result=result,
                task_id=result.get("task_id"),
                next_state=next_state,
                blocking_reasons=sorted(flags),
            )
        if decision.action is LDAction.WAIT:
            status = self._tools.get_task_status(decision.parameters["task_id"])
            self._require_task_project(status, decision.project_id)
            flags = set(status.get("attention_flags") or []) & _HUMAN_ATTENTION_FLAGS
            if flags:
                next_state = "human_required"
            elif status.get("verdict") == "verified":
                next_state = "task_verified"
            elif status.get("verdict") in ("rejected", "inconclusive"):
                next_state = "human_required"
            else:
                next_state = "waiting"
            return self._result(
                decision,
                result=status,
                task_id=status.get("task_id"),
                next_state=next_state,
                blocking_reasons=sorted(flags),
            )
        if decision.action is LDAction.REQUEST_HUMAN:
            return self._result(
                decision,
                result={"question": decision.parameters["question"]},
                next_state="human_required",
                blocking_reasons=decision.parameters.get("blocking_reasons") or [],
            )
        return self._finish(decision)

    def _inspect(self, decision: LDDecision) -> Any:
        tool = decision.parameters["tool"]
        if tool == "list_projects":
            return self._tools.list_projects()
        if tool == "get_project_brief":
            return self._tools.get_project_brief(decision.project_id or "")
        if tool == "get_project_progress":
            return self._tools.get_project_progress(decision.project_id or "")
        if tool == "get_project_tasks":
            return self._tools.get_project_tasks(
                decision.project_id or "", limit=decision.parameters.get("limit", 200))
        if tool == "get_task_status":
            result = self._tools.get_task_status(decision.parameters["task_id"])
        else:
            result = self._tools.get_task_provenance(decision.parameters["task_id"])
        self._require_task_project(result, decision.project_id)
        return result

    def _finish(self, decision: LDDecision) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        task_ids = list(decision.parameters.get("verified_task_ids") or [])
        task_ids.extend(
            condition["task_id"]
            for condition in decision.parameters.get("completion_conditions", [])
            if condition["kind"] == "task_verdict")
        for task_id in dict.fromkeys(task_ids):
            status = self._tools.get_task_status(task_id)
            self._require_task_project(status, decision.project_id)
            if status.get("verdict") != "verified":
                return self._human_result(
                    project_id=decision.project_id,
                    reason=f"finish evidence task {task_id} is not verified",
                    next_state="human_required",
                    blocking_reasons=[f"unverified_finish_evidence:task:{task_id}"],
                    evidence_refs=list(decision.evidence_refs),
                    source="finish_validator",
                    dry_run=False,
                    decision=decision,
                )
            if not self._references_task(decision.evidence_refs, task_id):
                return self._human_result(
                    project_id=decision.project_id,
                    reason=f"finish evidence_refs do not identify verified task {task_id}",
                    next_state="human_required",
                    blocking_reasons=[f"missing_finish_reference:task:{task_id}"],
                    evidence_refs=list(decision.evidence_refs),
                    source="finish_validator",
                    dry_run=False,
                    decision=decision,
                )
            checked.append({"kind": "task_verdict", "task_id": task_id,
                            "actual": "verified", "passed": True})

        project_conditions = [
            condition for condition in decision.parameters.get("completion_conditions", [])
            if condition["kind"] == "project_count"
        ]
        if project_conditions:
            progress = self._tools.get_project_progress(decision.project_id or "")
            if not self._references_project_progress(
                    decision.evidence_refs, decision.project_id or ""):
                return self._human_result(
                    project_id=decision.project_id,
                    reason="finish evidence_refs do not identify the project progress projection",
                    next_state="human_required",
                    blocking_reasons=["missing_finish_reference:project_progress"],
                    evidence_refs=list(decision.evidence_refs),
                    source="finish_validator",
                    dry_run=False,
                    decision=decision,
                )
            for condition in project_conditions:
                actual = int(progress["counts"].get(condition["field"], 0))
                expected = condition["value"]
                passed = {
                    "eq": actual == expected,
                    "gte": actual >= expected,
                    "lte": actual <= expected,
                }[condition["operator"]]
                checked.append({**condition, "actual": actual, "passed": passed})
                if not passed:
                    return self._human_result(
                        project_id=decision.project_id,
                        reason="a deterministic finish condition is not satisfied",
                        next_state="human_required",
                        blocking_reasons=[
                            f"finish_condition_failed:{condition['field']}:"
                            f"{condition['operator']}:{expected}:actual:{actual}"
                        ],
                        evidence_refs=list(decision.evidence_refs),
                        source="finish_validator",
                        dry_run=False,
                        decision=decision,
                    )
        return self._result(
            decision,
            result={"verified_conditions": checked},
            next_state="finished",
        )

    @staticmethod
    def _require_task_project(task: Mapping[str, Any], project_id: str | None) -> None:
        if project_id and task.get("project_id") != project_id:
            raise DecisionValidationError(
                f"task {task.get('task_id')} belongs to project "
                f"{task.get('project_id')}, not {project_id}")

    @staticmethod
    def _references_task(refs: tuple[str, ...], task_id: str) -> bool:
        accepted = {task_id, f"task:{task_id}", f"ledger:task:{task_id}"}
        return bool(accepted & set(refs))

    @staticmethod
    def _references_project_progress(refs: tuple[str, ...], project_id: str) -> bool:
        accepted = {
            f"project:{project_id}:progress",
            f"ledger:project:{project_id}:progress",
        }
        return bool(accepted & set(refs))

    @staticmethod
    def _result(
        decision: LDDecision,
        *,
        result: Any = None,
        task_id: str | None = None,
        next_state: str,
        blocking_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "action": decision.action.value,
            "project_id": decision.project_id,
            "reason": decision.reason,
            "evidence_refs": list(decision.evidence_refs),
            "task_id": task_id,
            "next_state": next_state,
            "executed": True,
            "dry_run": False,
            "decision_source": "model",
            "decision": decision.to_dict(),
            "blocking_reasons": blocking_reasons or [],
            "result": result,
        }

    @staticmethod
    def _human_result(
        *,
        project_id: str | None,
        reason: str,
        next_state: str,
        blocking_reasons: list[str],
        evidence_refs: list[str],
        source: str,
        dry_run: bool,
        decision: LDDecision | None = None,
    ) -> dict[str, Any]:
        question = "Human Owner decision is required before LD can continue."
        return {
            "action": "request_human",
            "project_id": project_id,
            "reason": reason,
            "evidence_refs": evidence_refs,
            "task_id": None,
            "next_state": next_state,
            "executed": False,
            "dry_run": dry_run,
            "decision_source": source,
            "decision": decision.to_dict() if decision else None,
            "blocking_reasons": blocking_reasons,
            "result": {"question": question},
        }


__all__ = ["DecisionModel", "LDCoordinator"]
