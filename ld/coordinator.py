"""Minimal one-turn coordinator for the single global LD (M4-A).

The coordinator asks an injected model for exactly one structured decision,
validates it, applies deterministic safety gates, and invokes at most one LD
action.  It is not an autonomous loop and it does not implement approvals,
Handoff, remote execution, or arbitrary model-authored commands.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import threading
import time
from typing import Any, Protocol

from ld.decision import (
    DecisionPolicyError,
    DecisionValidationError,
    LDAction,
    LDDecision,
    decision_schema,
)
from ld.goal_store import GoalLeaseLost, goal_identity, step_idempotency_key
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
        fault_hook: Callable[[str], None] | None = None,
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
        # Test-only crash injection at named persistence boundaries; None in
        # production.  See scripts/t39_m4_goal_reliability.py.
        self._fault_hook = fault_hook

    def _hook(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    def run_once(
        self,
        goal: str,
        project_id: str | None = None,
        dry_run: bool = False,
        *,
        goal_context: Mapping[str, Any] | None = None,
        goal_ref: str | None = None,
        submit_executor: Callable[..., dict] | None = None,
        replay_decision: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Plan and optionally execute exactly one LD action.

        ``goal_context`` / ``goal_ref`` / ``submit_executor`` /
        ``replay_decision`` are internal seams used by :meth:`run_goal`;
        external callers keep the single-turn semantics and never pass them.
        ``replay_decision`` re-executes an already persisted, validated
        decision through the same safety gates without calling the model.
        """
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")

        normalized_goal = goal.strip()
        goal_ref = goal_ref or self._goal_ref(normalized_goal)
        projects = self._tools.list_projects()
        selected_id = self._resolve_project_id(projects, project_id)
        if replay_decision is None and self._model is None:
            return self._human_result(
                project_id=selected_id,
                reason="LD decision model is not configured; no decision was fabricated",
                next_state="model_not_configured",
                blocking_reasons=["model_not_configured"],
                evidence_refs=[],
                source="coordinator",
                dry_run=dry_run,
            )
        if (replay_decision is None
                and self._goal_security_domain not in self._model_domains):
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

        model_call_error: Exception | None = None
        model_output_error: Exception | None = None
        decision = None
        try:
            if replay_decision is not None:
                raw = replay_decision
            else:
                model_input = self._decision_input(
                    normalized_goal, projects, selected_id, brief, model_progress,
                    goal_context=goal_context)
                try:
                    raw = (self._model.decide(model_input)
                           if hasattr(self._model, "decide") else self._model(model_input))
                except Exception as exc:  # noqa: BLE001 - transport boundary
                    # A *transport* failure (timeout, HTTP, malformed payload) is
                    # not a decision and not a policy event: it is retryable.
                    # Marking it separately lets the goal runner back off instead
                    # of parking the goal in needs_human on the first hiccup,
                    # while still never fabricating a decision.
                    model_call_error = exc
                    raw = None
            if model_call_error is None:
                if not isinstance(raw, Mapping):
                    raise DecisionValidationError(
                        "model output must be one structured object")
                # Schema/shape errors are a *generation* defect: the model tried
                # to answer but produced something incomplete (e.g. an assertion
                # without ``target``).  That is retryable, and it is materially
                # different from proposing a forbidden action or reaching outside
                # the authorized project — those are policy violations and must
                # stay permanently closed.  Keeping the two apart is what stops a
                # perfectly healthy goal from being parked forever because one
                # sample omitted a field, without ever loosening the policy gate.
                try:
                    decision = LDDecision.from_mapping(raw)
                except DecisionPolicyError:
                    # Forbidden action / invented field: fail closed, permanently.
                    raise
                except DecisionValidationError as exc:
                    model_output_error = exc
                    decision = None
                if decision is not None:
                    decision = self._resolve_decision_project(
                        decision, projects, selected_id)
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
        if model_call_error is not None:
            result = self._human_result(
                project_id=selected_id,
                reason=(f"model call failed: {type(model_call_error).__name__}: "
                        f"{model_call_error}"),
                next_state="model_call_failed",
                blocking_reasons=["model_call_failed"],
                evidence_refs=[],
                source="model_boundary",
                dry_run=dry_run,
            )
            result["model_call_failed"] = True
            return result
        if decision is None:
            result = self._human_result(
                project_id=selected_id,
                reason=(f"model output rejected: "
                        f"{type(model_output_error).__name__}: {model_output_error}"),
                next_state="model_output_invalid",
                blocking_reasons=["model_output_invalid"],
                evidence_refs=[],
                source="decision_validator",
                dry_run=dry_run,
            )
            # Same retry contract as a transport failure: no decision was made,
            # so the caller may retry the *same* step after a bounded backoff.
            result["model_call_failed"] = True
            result["model_output_invalid"] = True
            return result

        target_progress = full_progress
        target_brief = brief
        if decision.project_id and decision.project_id != selected_id:
            target_brief = self._tools.get_project_brief(decision.project_id)
            target_progress = self._tools.get_project_progress(decision.project_id)

        # Project-level attention flags do not prevent bounded reads.  They gate
        # new writes after the proposed action is known, so an LD can still
        # inspect the affected task and report useful evidence to the owner.
        if decision.action is LDAction.SUBMIT_TASK and target_progress:
            blockers = self._project_attention_blockers(target_progress)
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
            return self._execute(decision, goal_ref=goal_ref,
                                 submit_executor=submit_executor)
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
        goal_context: Mapping[str, Any] | None = None,
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
            **({"goal_state": dict(goal_context)} if goal_context else {}),
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
                "Use finish only with an explicit verified task associated with the "
                "current goal; project counts are auxiliary evidence only.",
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
            if (selected_id and decision.action is LDAction.INSPECT
                    and decision.parameters.get("tool") in {
                        "get_task_status", "get_task_provenance",
                    }):
                return LDDecision(
                    action=decision.action,
                    project_id=selected_id,
                    reason=decision.reason,
                    evidence_refs=decision.evidence_refs,
                    parameters=decision.parameters,
                )
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
        if (decision.action is LDAction.INSPECT
                and decision.parameters.get("tool") in {
                    "get_task_status", "get_task_provenance",
                } and decision.project_id is None):
            raise DecisionValidationError(
                "task inspection requires an explicit project scope")

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
    def _decision_execution_location_problem(
        decision: LDDecision,
        brief: dict | None,
    ) -> str | None:
        if decision.action is not LDAction.SUBMIT_TASK:
            return None
        if not brief:
            return "execution_location_ambiguous:no_project_brief"
        if not brief.get("active"):
            return "project_disabled"
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
    def _execute(self, decision: LDDecision, *, goal_ref: str,
                 submit_executor: Callable[..., dict] | None = None) -> dict[str, Any]:
        if decision.action is LDAction.INSPECT:
            result = self._inspect(decision)
            return self._result(
                decision,
                result=result,
                task_id=decision.parameters.get("task_id"),
                next_state="inspection_complete",
            )
        if decision.action is LDAction.SUBMIT_TASK:
            if submit_executor is not None:
                result = submit_executor(decision, goal_ref)
            else:
                result = self._tools.submit_project_task(
                    decision.project_id or "", **decision.parameters, goal_ref=goal_ref)
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
        return self._finish(decision, goal_ref=goal_ref)

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
            # Provenance contains the frozen Effective Prompt.  Check the
            # low-sensitivity status projection first, then retrieve the
            # model-filtered view (M4B-01): prompts and unauthorized context
            # never enter model-visible results.
            status = self._tools.get_task_status(decision.parameters["task_id"])
            self._require_task_project(status, decision.project_id)
            result = self._tools.get_task_provenance(
                decision.parameters["task_id"],
                allowed_security_domains=self._model_domains)
        self._require_task_project(result, decision.project_id)
        return result

    def _finish(self, decision: LDDecision, *, goal_ref: str) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        goal_linked_tasks: list[str] = []
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
            if status.get("ld_goal_ref") == goal_ref:
                goal_linked_tasks.append(task_id)
            checked.append({"kind": "task_verdict", "task_id": task_id,
                            "actual": "verified", "passed": True,
                            "current_goal": status.get("ld_goal_ref") == goal_ref})

        if not goal_linked_tasks:
            return self._human_result(
                project_id=decision.project_id,
                reason=("finish evidence has no verified WorkItem associated with "
                        "the current Human Owner goal"),
                next_state="human_required",
                blocking_reasons=["finish_evidence_not_linked_to_current_goal"],
                evidence_refs=list(decision.evidence_refs),
                source="finish_validator",
                dry_run=False,
                decision=decision,
            )

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
            result={"verified_conditions": checked,
                    "current_goal_task_ids": goal_linked_tasks},
            next_state="finished",
        )

    @staticmethod
    def _goal_ref(goal: str) -> str:
        return "sha256:" + hashlib.sha256(goal.encode("utf-8")).hexdigest()

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

    # ------------------------------------------------------------------ #
    # M4-B: bounded, persistent goal advancement
    # ------------------------------------------------------------------ #
    def run_goal(
        self,
        goal: str,
        project_id: str,
        goal_store,
        *,
        max_decisions: int = 10,
        max_tasks: int = 3,
        max_stalled_inspections: int = 3,
        poll_interval_s: float | None = None,
        max_wait_s: float | None = None,
        lease_owner: str | None = None,
        resume: bool = False,
        resume_note: str = "",
        heartbeat: bool = True,
        budget_is_per_call: bool = False,
    ) -> dict[str, Any]:
        """Advance one persistent goal by one auditable transition at a time.

        Never an unbounded loop: every iteration performs at most one model
        decision, one action and one transactional goal transition.  Limits
        (model-decision budget, task budget, stalled inspections, model
        failures, human-attention flags) all stop the goal in an explicit
        state.  ``poll_interval_s=None`` (default) returns as soon as the goal
        waits on an in-flight task.

        Reliability (M4 reliability closure):

        * the lease carries a fencing token; every goal write is conditioned on
          it, and a heartbeat renews it during long model calls;
        * a task submission is persisted as a ``pending`` step *before* the
          side effect and uses a system-generated idempotency key derived from
          (goal, step); a restart reconciles a pending step from the Ledger
          (adopt the existing WorkItem, or replay the persisted decision)
          before ever asking the model again;
        * ``resume=True`` lifts a ``needs_human`` goal under the lease, as one
          step of the same contiguous history.
        """
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if max_decisions < 1 or max_tasks < 1:
            raise ValueError("max_decisions and max_tasks must be >= 1")
        normalized_goal = goal.strip()
        selected_id = self._resolve_project_id(self._tools.list_projects(), project_id)
        goal_id, goal_ref = goal_identity(normalized_goal, selected_id)
        goal_store.get_or_create(goal_id, selected_id, normalized_goal, goal_ref)
        if budget_is_per_call:
            # Default (``False``) keeps the documented M4-B contract: the budgets
            # are absolute for the whole goal, so an Owner re-running ``run-goal``
            # cannot silently exceed the limit by asking again.  A scheduler that
            # advances the same goal many times (``LDGoalRunner``) grants one
            # decision per wake-up instead, so it opts in here and the *overall*
            # bound stays with the runner's own escalation policy.
            existing = goal_store.get(goal_id) or {}
            max_decisions = (int(existing.get("model_decision_count") or 0)
                             + max_decisions)
            max_tasks = int(existing.get("task_count") or 0) + max_tasks
        owner = lease_owner or goal_store.default_owner()
        leased = goal_store.acquire_lease(goal_id, owner)
        lease = _GoalLease(goal_store, goal_id, owner, int(leased["lease_token"]))
        if heartbeat:
            lease.start_heartbeat()
        try:
            if resume and (goal_store.get(goal_id) or {}).get("status") == "needs_human":
                goal_store.resume(
                    goal_id, note=resume_note or "resumed by the Human Owner after review",
                    token=lease.token)
            return self._advance_goal_loop(
                goal_id=goal_id, goal_ref=goal_ref, project_id=selected_id,
                normalized_goal=normalized_goal, goal_store=goal_store,
                max_decisions=max_decisions, max_tasks=max_tasks,
                max_stalled_inspections=max_stalled_inspections,
                poll_interval_s=poll_interval_s, max_wait_s=max_wait_s,
                lease=lease)
        finally:
            lease.stop_heartbeat()
            goal_store.release_lease(goal_id, owner, lease.token)

    def _advance_goal_loop(
        self, *, goal_id: str, goal_ref: str, project_id: str,
        normalized_goal: str, goal_store, max_decisions: int, max_tasks: int,
        max_stalled_inspections: int, poll_interval_s: float | None,
        max_wait_s: float | None, lease: "_GoalLease",
    ) -> dict[str, Any]:
        deadline = (time.time() + max_wait_s
                    if poll_interval_s is not None and max_wait_s else None)
        stalled = 0
        last_result: dict[str, Any] | None = None
        while True:
            lease.check()
            goal = goal_store.get(goal_id) or {}
            pending = goal_store.pending_step(goal_id)
            if pending is not None:
                # Crash recovery first: never re-plan a step that may already
                # have produced a WorkItem.
                step = int(pending["step"])
                result = self._reconcile_pending_step(
                    pending, goal=goal, goal_id=goal_id, goal_ref=goal_ref,
                    project_id=project_id, normalized_goal=normalized_goal,
                    goal_store=goal_store, max_tasks=max_tasks, lease=lease)
                transition = self._commit_goal_step(
                    goal_store, goal_id, goal, result, step, lease,
                    pending=True, decision=pending.get("decision") or {})
                last_result = result
            else:
                if goal.get("status") in ("needs_human", "completed", "failed"):
                    return self._goal_report(goal_id, project_id, goal_ref, goal_store,
                                             last_result, reason="goal is not active")
                if int(goal.get("model_decision_count", 0)) >= max_decisions:
                    goal_store.stop_goal(
                        goal_id, lease.token, "needs_human",
                        {"stop": "decision budget exhausted",
                         "model_decision_count": goal.get("model_decision_count")})
                    return self._goal_report(goal_id, project_id, goal_ref, goal_store,
                                             last_result,
                                             reason="decision budget exhausted")
                step = int(goal.get("decision_count", 0)) + 1
                goal_context = self._goal_context(
                    goal, goal_store, goal_ref, max_decisions, max_tasks)
                result = self.run_once(
                    normalized_goal, project_id=project_id,
                    goal_context=goal_context, goal_ref=goal_ref,
                    submit_executor=self._make_goal_submit_executor(
                        project_id=project_id, goal_ref=goal_ref, goal=goal,
                        max_tasks=max_tasks, goal_id=goal_id, step=step,
                        goal_store=goal_store, lease=lease, replay=False))
                if result.get("model_call_failed"):
                    # Transport failure: no decision was made, so no step is
                    # opened or committed.  The goal keeps its status and the
                    # next holder retries the *same* step number; backoff and
                    # escalation belong to the caller (LDGoalRunner), and the
                    # failure text stays queryable there.
                    return self._goal_report(
                        goal_id, project_id, goal_ref, goal_store, result,
                        reason="model_call_failed")
                opened = goal_store.pending_step(goal_id)
                transition = self._commit_goal_step(
                    goal_store, goal_id, goal, result, step, lease,
                    pending=bool(opened and int(opened["step"]) == step),
                    decision=result.get("decision") or {})
                last_result = result

            if result.get("action") == "inspect" and not result.get("task_id"):
                stalled += 1
            else:
                stalled = 0
            if stalled >= max_stalled_inspections:
                goal_store.stop_goal(
                    goal_id, lease.token, "needs_human",
                    {"stop": "stalled: repeated inspections without progress"})
                return self._goal_report(goal_id, project_id, goal_ref, goal_store,
                                         last_result, reason="stalled")

            if transition == "waiting":
                if deadline is None or time.time() >= deadline:
                    return self._goal_report(goal_id, project_id, goal_ref, goal_store,
                                             last_result, reason="waiting for task")
                if poll_interval_s:
                    # Let the control plane observe the Worker before the next
                    # decision; a ledger-only plane cannot refresh and the
                    # caller's runtime owns observation in that case.
                    try:
                        self._tools.service.plane.refresh_known_jobs()
                    except Exception:  # noqa: BLE001 - refresh is best effort
                        pass
                    time.sleep(poll_interval_s)
                continue
            if transition in ("needs_human", "completed", "failed"):
                return self._goal_report(goal_id, project_id, goal_ref, goal_store,
                                         last_result, reason=transition)
            # active -> next decision immediately

    def _reconcile_pending_step(self, pending: dict, *, goal: dict, goal_id: str,
                                goal_ref: str, project_id: str, normalized_goal: str,
                                goal_store, max_tasks: int,
                                lease: "_GoalLease") -> dict[str, Any]:
        """Resolve a step persisted before its side effect completed.

        The WorkItem, if any, is found by the deterministic step key.  If it
        exists it is adopted; otherwise the *persisted* decision is replayed
        through the same safety gates.  The model is never consulted.
        """
        step = int(pending["step"])
        decision_map = pending.get("decision") or {}
        key = step_idempotency_key(goal_id, step)
        existing = self._tools.service.store.by_idempotency_key(key)
        if existing:
            flags: set[str] = set()
            try:
                status = self._tools.get_task_status(existing["task_id"])
                flags = set(status.get("attention_flags") or []) & _HUMAN_ATTENTION_FLAGS
            except KeyError:
                flags = {"task_status_unavailable"}
            if existing.get("queue_status") == "needs_approval":
                flags.add("needs_approval")
            return {
                "action": "submit_task",
                "project_id": existing.get("project_id"),
                "reason": "recovered pending step: adopted the existing WorkItem",
                "evidence_refs": [f"task:{existing['task_id']}"],
                "task_id": existing["task_id"],
                "next_state": "human_required" if flags else "task_submitted",
                "executed": True,
                "dry_run": False,
                "decision_source": "recovery",
                "decision": decision_map,
                "blocking_reasons": sorted(flags),
                "recovered": "adopted",
                "result": {"task_id": existing["task_id"],
                           "ld_handoff_upgrade": bool(
                               (existing.get("metadata") or {}).get("handoff"))},
            }
        result = self.run_once(
            normalized_goal, project_id=project_id, goal_ref=goal_ref,
            replay_decision=decision_map,
            submit_executor=self._make_goal_submit_executor(
                project_id=project_id, goal_ref=goal_ref, goal=goal,
                max_tasks=max_tasks, goal_id=goal_id, step=step,
                goal_store=goal_store, lease=lease, replay=True))
        result["recovered"] = "replayed"
        result["decision_source"] = "recovery"
        return result

    def _commit_goal_step(self, goal_store, goal_id: str, goal: dict, result: dict,
                          step: int, lease: "_GoalLease", *, pending: bool,
                          decision: Mapping[str, Any]) -> str:
        fields, status = self._goal_transition_fields(goal, result, step)
        outcome = {
            "action": result.get("action"),
            "next_state": result.get("next_state"),
            "task_id": result.get("task_id"),
            "executed": bool(result.get("executed")),
            "blocking_reasons": result.get("blocking_reasons") or [],
            "recovered": result.get("recovered"),
            # Why the step ended where it did (e.g. the exact refusal of a
            # failed submission), so neither the owner nor the next decision
            # has to guess.  Bounded; never contains prompts or credentials.
            "decision_source": result.get("decision_source"),
            "reason": str(result.get("reason") or "")[:600],
        }
        if pending:
            goal_store.commit_step(goal_id, step, outcome, fields, lease.token)
        else:
            goal_store.record_step(goal_id, step, dict(decision), outcome, fields,
                                   lease.token)
        return status

    @staticmethod
    def _goal_transition_fields(goal: dict, result: dict,
                                step: int) -> tuple[dict[str, Any], str]:
        action = result.get("action")
        state = result.get("next_state")
        fields: dict[str, Any] = {
            "last_decision": {
                "step": step, "action": action, "next_state": state,
                "task_id": result.get("task_id"),
                "recovered": result.get("recovered")},
        }
        if action == "submit_task" and result.get("task_id"):
            fields["task_count"] = int(goal.get("task_count", 0)) + 1
            fields["current_task_id"] = result.get("task_id")
            if (result.get("result") or {}).get("ld_handoff_upgrade"):
                handoffs = list(goal.get("handoff_task_ids") or [])
                if result.get("task_id") not in handoffs:
                    handoffs.append(result.get("task_id"))
                fields["handoff_task_ids"] = handoffs
            status = "waiting" if state == "task_submitted" else "needs_human"
        elif action == "submit_task":
            status = "needs_human"
        elif action == "wait":
            status = {"task_verified": "active",
                      "waiting": "waiting"}.get(state, "needs_human")
        elif action == "finish":
            status = "completed" if state == "finished" else "needs_human"
        elif action == "request_human":
            status = "needs_human"
        else:  # inspect or failed model decision
            status = "active" if state == "inspection_complete" else "needs_human"
        fields["status"] = status
        return fields, status

    def _goal_context(self, goal: dict, goal_store, goal_ref: str,
                      max_decisions: int, max_tasks: int) -> dict[str, Any]:
        """What the model is told about the goal's own progress.

        Task truth is re-read from the Ledger on every decision (never from
        the goal row alone), so a restart after a crash between enqueue and
        the goal-row update still shows the existing WorkItem to the model.
        """
        service = self._tools.service
        goal_tasks = []
        for task in service.store.all():
            if (task.get("metadata") or {}).get("ld_goal_ref") != goal_ref:
                continue
            goal_tasks.append(task)
        goal_tasks.sort(key=lambda t: t.get("created_at") or 0)
        current_task = None
        current_id = goal.get("current_task_id")
        if current_id:
            try:
                status = self._tools.get_task_status(current_id)
                current_task = {key: status.get(key) for key in (
                    "task_id", "state", "verdict", "queue_status",
                    "attention_flags")}
                receipts = service.context_update_receipts(current_id)
                current_task["context_updates_completed"] = bool(receipts)
            except KeyError:
                current_task = None
        completed = [
            {"task_id": t.get("task_id"), "verdict": t.get("verdict"),
             "title": t.get("title")}
            for t in goal_tasks if t.get("verdict") == "verified"]
        history = goal_store.decisions(goal.get("goal_id"), limit=500) \
            if goal.get("goal_id") else []
        last_step = None
        resumed_after = False
        for item in reversed(history):
            if item.get("state") != "committed":
                continue
            outcome = item.get("outcome") or {}
            if outcome.get("action") == "human_resume":
                # The owner only said "continue"; the model still needs to
                # know what stopped the goal before that.
                resumed_after = True
                continue
            last_step = {key: outcome.get(key) for key in (
                "action", "next_state", "task_id", "blocking_reasons",
                "decision_source")}
            last_step["step"] = item["step"]
            last_step["human_resumed_after_this_step"] = resumed_after
            # Control-plane refusal text is shown only to a model that is
            # authorized for the goal's (local) domain — which is the only
            # model the coordinator ever calls for a local goal.
            if "local" in self._model_domains:
                last_step["reason"] = outcome.get("reason")
            break
        return {
            "status": goal.get("status"),
            "decision_count": goal.get("decision_count", 0),
            "human_resume_count": goal.get("resume_count", 0),
            "task_count": goal.get("task_count", 0),
            "max_decisions": max_decisions,
            "max_tasks": max_tasks,
            "current_task": current_task,
            "last_step": last_step,
            "completed_tasks": completed,
            "all_goal_tasks": [
                {"task_id": t.get("task_id"), "verdict": t.get("verdict"),
                 "state": t.get("state")} for t in goal_tasks],
            "instruction": (
                "Continue this goal. Tasks already submitted for it are listed "
                "in all_goal_tasks; never resubmit work that already exists — "
                "wait on it, or submit the next distinct task. A task verdict "
                "'verified' is the control plane's independent acceptance of "
                "that task's declared assertions; you cannot run tests yourself "
                "and must not request a human merely to re-check a verified "
                "task. When every part of the goal is covered by verified tasks, "
                "finish with those task ids as evidence. Use request_human only "
                "for a genuine blocker (rejected/inconclusive verdict, approval "
                "needed, conflicting requirements)."),
        }

    def _make_goal_submit_executor(self, *, project_id: str, goal_ref: str,
                                   goal: dict, max_tasks: int, goal_id: str,
                                   step: int, goal_store, lease: "_GoalLease",
                                   replay: bool):
        """Two-phase, idempotent submission for one goal step.

        1. the validated decision is persisted as a ``pending`` step (skipped
           on replay: it already is);
        2. the lease is renewed under its fencing token right before the side
           effect, so a holder that lost the goal cannot submit;
        3. the submission uses the system-generated key for (goal, step), never
           the model's key, so any retry or takeover of this step converges on
           one WorkItem.

        The model only describes the next task.  When an earlier task of the
        same goal is already verified, the control plane — not the model —
        submits the new task as a session handoff with a reachable checkpoint
        of that task's accepted code.
        """
        step_key = step_idempotency_key(goal_id, step)

        def executor(decision: LDDecision, ref: str) -> dict:
            if int(goal.get("task_count", 0)) + 1 > max_tasks:
                raise RuntimeError(f"goal task budget exhausted ({max_tasks})")
            self._hook("after_model_decision")
            if not replay:
                goal_store.begin_step(goal_id, step, decision.to_dict(), lease.token)
            self._hook("after_begin_step")
            lease.renew_or_raise()
            params = dict(decision.parameters)
            params["idempotency_key"] = step_key
            context_prereqs, task_prereqs = self._split_task_prerequisites(
                project_id, ref, params.get("prerequisites") or [])
            params["prerequisites"] = context_prereqs
            prior = self._prior_verified_task(project_id, ref)
            if prior:
                record = self._tools.submit_goal_handoff(
                    decision.project_id or "", prior["task_id"], params["task"],
                    assertions=params.get("assertions") or [],
                    idempotency_key=step_key,
                    title=params.get("title") or "",
                    tags=params.get("tags") or (),
                    prerequisites=context_prereqs,
                    require_verified_constraints=bool(
                        params.get("require_verified_constraints")),
                    target_workspace_id=params.get("workspace_id"),
                    timeout_s=int(params.get("timeout_s") or 900),
                    checkpoint_mode="reachable",
                    require_checkpoint=True,
                    metadata={"submitted_by": "ld", "ld_stage": "m4-b",
                              "ld_goal_ref": ref, "ld_goal_id": goal_id,
                              "ld_goal_step": step,
                              "ld_task_prerequisites": task_prereqs})
                out = dict(record)
                out["ld_handoff_upgrade"] = True
            else:
                out = self._tools.submit_project_task(
                    decision.project_id or "", **params, goal_ref=ref)
            self._hook("after_execute")
            return out
        return executor

    def _split_task_prerequisites(self, project_id: str, goal_ref: str,
                                  prerequisites) -> tuple[list[str], list[str]]:
        """Resolve prerequisites that name WorkItems against the Ledger.

        A context prerequisite is a verified Project Context entry; a model may
        instead name an earlier task.  Such a task-id prerequisite is satisfied
        only when that WorkItem is ``verified`` and belongs to this goal (same
        project and goal_ref) — the Ledger verdict is the authority.  Anything
        else fails closed.  Non-task prerequisites pass through unchanged.
        """
        store = self._tools.service.store
        context_prereqs: list[str] = []
        task_prereqs: list[str] = []
        for item in prerequisites:
            record = store.get(item)
            if not record:
                context_prereqs.append(item)
                continue
            if (record.get("project_id") != project_id
                    or (record.get("metadata") or {}).get("ld_goal_ref") != goal_ref):
                raise ValueError(
                    f"prerequisite task {item} does not belong to this goal")
            if record.get("verdict") != "verified":
                raise ValueError(
                    f"prerequisite task {item} is not verified "
                    f"(verdict={record.get('verdict')})")
            task_prereqs.append(item)
        return context_prereqs, task_prereqs

    def _prior_verified_task(self, project_id: str, goal_ref: str) -> dict | None:
        service = self._tools.service
        candidates = [
            task for task in service.store.all()
            if task.get("project_id") == project_id
            and (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref
            and task.get("verdict") == "verified"]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t.get("created_at") or 0)
        return candidates[-1]

    def _goal_report(self, goal_id: str, project_id: str, goal_ref: str, goal_store,
                     last_result: dict | None, reason: str) -> dict[str, Any]:
        """Final/interim report scoped to exactly this goal's WorkItems.

        Membership is the exact ``ld_goal_ref`` of this goal (never "any LD
        task in the project"); every verdict is read from the Ledger.
        """
        goal = goal_store.get(goal_id) or {}
        decisions = goal_store.decisions(goal_id, limit=500)
        service = self._tools.service
        goal_tasks = sorted(
            ({"task_id": t.get("task_id"), "title": t.get("title"),
              "state": t.get("state"), "verdict": t.get("verdict"),
              "handoff_source": (t.get("metadata") or {}).get("source_work_item_id"),
              "checkpoint_ref": (t.get("metadata") or {}).get("checkpoint_ref"),
              "created_at": t.get("created_at")}
             for t in service.store.all()
             if t.get("project_id") == project_id
             and (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref),
            key=lambda t: t.get("created_at") or 0)
        steps = [d["step"] for d in decisions]
        return {
            "goal_id": goal_id,
            "goal_ref": goal_ref,
            "project_id": project_id,
            "status": goal.get("status"),
            "stop_reason": reason,
            "last_step_reason": next(
                (d["outcome"].get("reason") for d in reversed(decisions)
                 if d.get("state") == "committed" and d["outcome"].get("reason")), None),
            "decision_count": goal.get("decision_count"),
            "model_decision_count": goal.get("model_decision_count"),
            "human_resume_count": goal.get("resume_count"),
            "task_count": goal.get("task_count"),
            "current_task_id": goal.get("current_task_id"),
            "handoff_task_ids": goal.get("handoff_task_ids") or [],
            "goal_tasks": goal_tasks,
            "verified_tasks": [t for t in goal_tasks if t.get("verdict") == "verified"],
            "history_contiguous": steps == list(range(1, len(steps) + 1)),
            "pending_steps": [d["step"] for d in decisions if d.get("state") == "pending"],
            "last_result": last_result,
            "decision_history": [
                {"step": d["step"],
                 "state": d.get("state"),
                 "action": d["outcome"].get("action") or d["decision"].get("action"),
                 "next_state": d["outcome"].get("next_state"),
                 "task_id": d["outcome"].get("task_id"),
                 "recovered": d["outcome"].get("recovered")}
                for d in decisions],
        }


@dataclass
class _GoalLease:
    """Fenced goal lease held by one ``run_goal`` call."""

    store: Any
    goal_id: str
    owner: str
    token: int
    lost: threading.Event = None  # type: ignore[assignment]
    _stop: threading.Event = None  # type: ignore[assignment]
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.lost = threading.Event()
        self._stop = threading.Event()

    def start_heartbeat(self) -> None:
        interval = max(float(self.store.lease_s) / 3.0, 0.2)

        def beat() -> None:
            while not self._stop.wait(interval):
                try:
                    if not self.store.renew_lease(self.goal_id, self.owner, self.token):
                        self.lost.set()
                        return
                except Exception:  # noqa: BLE001 - any failure means "not proven ours"
                    self.lost.set()
                    return

        self._thread = threading.Thread(
            target=beat, name=f"ld-lease-{self.goal_id}", daemon=True)
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def check(self) -> None:
        if self.lost.is_set():
            raise GoalLeaseLost(self.goal_id, self.token, None)

    def renew_or_raise(self) -> None:
        """Synchronous fenced renewal immediately before a side effect."""
        self.check()
        if not self.store.renew_lease(self.goal_id, self.owner, self.token):
            self.lost.set()
            current = (self.store.get(self.goal_id) or {}).get("lease_token")
            raise GoalLeaseLost(self.goal_id, self.token, current)


__all__ = ["DecisionModel", "LDCoordinator"]
