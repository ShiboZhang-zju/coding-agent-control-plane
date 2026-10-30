"""Restricted deterministic tools available to the global LD (M4-A).

This is intentionally narrower than :class:`controlplane.project_service.ProjectService`.
In particular it exposes no context writes, approvals, Handoff operations, prompt
mutation, runtime dispatch, or direct SQLite access.  Submissions are ordinary
``ProjectService.submit_project_task`` calls and therefore retain the existing
context-freeze, idempotency, policy, approval and queue semantics.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from controlplane.project_service import ProjectService

_ASSERTION_FIELDS = {"kind", "target", "value", "description"}
_ASSERTION_KINDS = {
    "file_exists", "file_contains", "git_commit", "git_clean", "path_absent",
}


class LDSubmissionIdempotencyConflict(ValueError):
    """An LD idempotency key already names a different normalized request."""


def _submission_signature(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _string_list(value: Iterable[str], field: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        out.append(item.strip())
    return out


def _assertions(value: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError("assertions must be a list of objects")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"assertions[{index}] must be an object")
        unknown = set(item) - _ASSERTION_FIELDS
        if unknown:
            raise ValueError(
                f"assertions[{index}] has unsupported fields: "
                + ", ".join(sorted(unknown)))
        kind = item.get("kind")
        target = item.get("target")
        if kind not in _ASSERTION_KINDS:
            raise ValueError(
                f"assertions[{index}].kind must be one of "
                + ", ".join(sorted(_ASSERTION_KINDS)))
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"assertions[{index}].target must be a non-empty string")
        if kind in {"file_exists", "file_contains", "path_absent"}:
            posix_target = PurePosixPath(target.strip().replace("\\", "/"))
            if (posix_target.is_absolute() or PureWindowsPath(target).is_absolute()
                    or ".." in posix_target.parts):
                raise ValueError(
                    f"assertions[{index}].target must stay inside the workspace")
        normalized = {"kind": kind, "target": target.strip()}
        if item.get("value") is not None:
            if not isinstance(item["value"], str):
                raise ValueError(f"assertions[{index}].value must be a string")
            normalized["value"] = item["value"]
        if item.get("description") is not None:
            if not isinstance(item["description"], str):
                raise ValueError(f"assertions[{index}].description must be a string")
            normalized["description"] = item["description"]
        out.append(normalized)
    return out


class LDTools:
    """The complete M4-A tool capability granted to the LD.

    ``submission_service_factory`` exists for ledger-only CLIs: it selects a
    ``ProjectService`` whose name-only Adapter reference matches the project's
    configured Adapter.  The factory still returns a ProjectService; the facade
    never dispatches a Worker itself.
    """

    def __init__(
        self,
        service: ProjectService,
        *,
        submission_service_factory: Callable[[str], ProjectService] | None = None,
    ):
        self._service = service
        self._submission_service_factory = submission_service_factory

    @property
    def service(self) -> ProjectService:
        """The wrapped service (read-side helpers for goal advancement)."""
        return self._service

    def list_projects(self) -> list[dict]:
        return self._service.list_projects()

    def get_project_brief(
        self,
        project_id: str,
        *,
        allowed_security_domains: Iterable[str] | None = None,
    ) -> dict:
        return self._service.get_project_brief(
            project_id, allowed_security_domains=allowed_security_domains)

    def get_project_progress(
        self,
        project_id: str,
        *,
        allowed_security_domains: Iterable[str] | None = None,
    ) -> dict:
        return self._service.get_project_progress(
            project_id, allowed_security_domains=allowed_security_domains)

    def get_project_tasks(self, project_id: str, limit: int = 200) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        return self._service.get_project_tasks(project_id, limit=limit)

    def get_task_status(self, task_id: str) -> dict:
        return self._service.get_task_status(task_id)

    def get_task_provenance(self, task_id: str, *,
                            allowed_security_domains: Iterable[str] | None = None
                            ) -> dict:
        """Task provenance, optionally filtered to a model-safe view.

        ``None`` keeps the full local operator view (including the frozen
        Effective Prompt).  An explicit domain set returns the model view
        (M4B-01): no Effective Prompt, no original task text, and context
        entry titles only from authorized security domains.
        """
        provenance = self._service.get_task_provenance(task_id)
        if allowed_security_domains is None:
            return provenance
        allowed = {str(item) for item in allowed_security_domains}
        filtered_entries = []
        for entry in provenance.get("context_entries") or []:
            full = self._service.store.get_context_entry(entry.get("id"))
            if full and full.get("security_domain") in allowed:
                filtered_entries.append(entry)
        return {
            "task_id": provenance.get("task_id"),
            "project_id": provenance.get("project_id"),
            "workspace_id": provenance.get("workspace_id"),
            "context_version": provenance.get("context_version"),
            "context_entry_refs": provenance.get("context_entry_refs") or [],
            "context_entries": filtered_entries,
            "task_digest": provenance.get("task_digest"),
            "state": provenance.get("state"),
            "verdict": provenance.get("verdict"),
            "ld_goal_ref": provenance.get("ld_goal_ref"),
            "ld_submission_signature": provenance.get("ld_submission_signature"),
            "handoff": provenance.get("handoff"),
            "handoff_source": provenance.get("handoff_source") or {},
            "effective_prompt_omitted": True,
            "original_task_omitted": True,
        }

    def submit_project_task(
        self,
        project_id: str,
        task: str,
        *,
        assertions: Iterable[Mapping[str, Any]] = (),
        priority: int = 0,
        idempotency_key: str,
        title: str = "",
        tags: Iterable[str] = (),
        prerequisites: Iterable[str] = (),
        require_verified_constraints: bool = False,
        workspace_id: str | None = None,
        timeout_s: int = 900,
        goal_ref: str | None = None,
    ) -> dict:
        """Submit one ordinary project task through ProjectService.

        The restricted signature is a security boundary: callers cannot supply
        ``allow_blocked``, execution policy, arbitrary metadata, extra prompt
        references, Handoff source fields, or any direct Worker instruction.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("LD submissions require a non-empty idempotency_key")
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("priority must be an integer")
        if not isinstance(require_verified_constraints, bool):
            raise ValueError("require_verified_constraints must be a boolean")
        if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) \
                or not 1 <= timeout_s <= 86_400:
            raise ValueError("timeout_s must be an integer from 1 to 86400")
        if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id.strip()):
            raise ValueError("workspace_id must be a non-empty string when supplied")
        if goal_ref is not None and (
                not isinstance(goal_ref, str) or not goal_ref.startswith("sha256:")
                or len(goal_ref) != 71):
            raise ValueError("goal_ref must be a sha256 reference when supplied")
        if goal_ref is not None:
            try:
                int(goal_ref.removeprefix("sha256:"), 16)
            except ValueError as exc:
                raise ValueError(
                    "goal_ref must be a sha256 reference when supplied") from exc

        service = (self._submission_service_factory(project_id)
                   if self._submission_service_factory else self._service)
        projects = {item["project_id"]: item for item in service.list_projects()}
        project = projects.get(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        configured_adapter = project.get("default_adapter")
        actual_adapter = getattr(service.plane.adapter, "name", None)
        if configured_adapter and actual_adapter != configured_adapter:
            raise RuntimeError(
                f"submission service adapter {actual_adapter!r} does not match "
                f"project adapter {configured_adapter!r}")

        normalized_task = task.strip()
        normalized_assertions = _assertions(assertions)
        normalized_tags = _string_list(tags, "tags")
        normalized_prerequisites = _string_list(prerequisites, "prerequisites")
        normalized_workspace = workspace_id.strip() if workspace_id else None
        brief = service.get_project_brief(project_id)
        if normalized_workspace is None:
            defaults = [item for item in brief.get("workspaces", [])
                        if item.get("is_default")]
            resolved_workspace = defaults[0].get("id") if len(defaults) == 1 else None
        else:
            resolved_workspace = normalized_workspace
        request_signature = _submission_signature({
            "schema": "m4-a.ld-submission.v1",
            "project_id": project_id,
            "task": normalized_task,
            "workspace_id": resolved_workspace,
            "assertions": normalized_assertions,
            "priority": priority,
            "title": title.strip(),
            "tags": normalized_tags,
            "prerequisites": normalized_prerequisites,
            "require_verified_constraints": require_verified_constraints,
            "timeout_s": timeout_s,
            "goal_ref": goal_ref,
        })

        record = service.submit_project_task(
            project_id,
            normalized_task,
            assertions=normalized_assertions,
            priority=priority,
            idempotency_key=idempotency_key.strip(),
            title=title.strip(),
            tags=normalized_tags,
            prerequisites=normalized_prerequisites,
            require_verified_constraints=require_verified_constraints,
            # Freeze the resolved target used by the request signature so a
            # concurrent default-workspace change cannot alter execution intent.
            workspace_id=resolved_workspace,
            timeout_s=timeout_s,
            metadata={
                "submitted_by": "ld",
                "ld_stage": "m4-a",
                "ld_submission_signature": request_signature,
                "ld_goal_ref": goal_ref,
            },
            allow_blocked=False,
        )
        if record.get("project_id") != project_id:
            raise LDSubmissionIdempotencyConflict(
                f"idempotency key {idempotency_key!r} belongs to project "
                f"{record.get('project_id')}, not {project_id}")
        stored_signature = (record.get("metadata") or {}).get(
            "ld_submission_signature")
        if stored_signature != request_signature:
            raise LDSubmissionIdempotencyConflict(
                f"idempotency key {idempotency_key!r} belongs to a different "
                "LD submission request")
        return record

    def submit_goal_handoff(self, project_id: str, source_work_item_id: str,
                            task: str, **kwargs: Any) -> dict:
        """Submit a goal-continuation handoff on the project's real Adapter.

        This is NOT a model-exposed tool: it is the control plane's
        deterministic upgrade used by ``LDCoordinator.run_goal`` when the next
        task of a goal must build on a verified prior session.  Routing through
        the submission service (never the read-only service) keeps the queued
        WorkItem bound to the project adapter so the Runtime can claim it.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        service = (self._submission_service_factory(project_id)
                   if self._submission_service_factory else self._service)
        return service.submit_handoff(source_work_item_id, task, **kwargs)


__all__ = ["LDSubmissionIdempotencyConflict", "LDTools"]
