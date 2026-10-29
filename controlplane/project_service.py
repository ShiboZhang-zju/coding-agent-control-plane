"""Vendor-neutral project operations facade — the deterministic tool surface an LD
will call (M1).

Why a facade and not an agent
-----------------------------
The LD (a future global managing agent) must be able to *ask* and *act* without
holding every project's knowledge in its own context. Every method here is a
deterministic function over the ledger: same inputs, same outputs, no LLM in the
loop. That is what makes an LD's decisions auditable — "the LD said project A is
blocked" can be traced to a row, not to a model's impression.

Progress is derived, never stored
---------------------------------
:meth:`ProjectService.get_project_progress` computes counts from work items and
their acceptance verdicts. There is deliberately no `project.progress` table: a
second, hand-maintained copy of task status would drift from the ledger and become
a lie. Narrative progress (blockers, next steps) lives in project context entries
of kind ``progress`` and is returned *alongside* the derived facts, clearly
separated.

Not implemented here (on purpose)
---------------------------------
* No HTTP surface. The LD is not wired up yet (M4).
* No remote dispatch. A non-local workspace is reported as unsupported rather
  than silently executed locally (M5).
"""
from __future__ import annotations

from typing import Any, Iterable

from adapters.base import ArtifactAssertion, DispatchPolicy, TaskState, Verdict
from controlplane.context_builder import (
    BuiltContext,
    ContextBuildBlocked,
    ContextBuilder,
)
from controlplane.plane import ControlPlane
from controlplane.project_context import ProjectContextStore
from controlplane.store import TaskStore

#: Queue/state buckets used by the progress projection. Kept explicit so a new
#: TaskState cannot silently fall out of the counts.
_ACTIVE_STATES = (TaskState.QUEUED.value, TaskState.STARTING.value,
                  TaskState.RUNNING.value, TaskState.WAITING_FOR_APPROVAL.value)
_NEEDS_HUMAN_STATES = (TaskState.DISPATCH_UNKNOWN.value, TaskState.UNKNOWN.value)


class ProjectService:
    """Project-centric operations on top of an existing :class:`ControlPlane`."""

    def __init__(self, plane: ControlPlane, *,
                 budget_chars: int | None = None):
        self.plane = plane
        self.store: TaskStore = plane.store
        self.context = ProjectContextStore(self.store)
        self.builder = (ContextBuilder(self.store, budget_chars=budget_chars)
                        if budget_chars else ContextBuilder(self.store))

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def list_projects(self) -> list[dict]:
        """Every registered project with its derived task counts."""
        out = []
        for project in self.store.all_projects():
            tasks = self.store.work_items_for_project(project["id"])
            out.append({
                "project_id": project["id"],
                "name": project["name"],
                "root": project["root"],
                "active": project["active"],
                "default_adapter": project.get("default_adapter"),
                "context_version": self.store.context_version(project["id"]),
                "task_count": len(tasks),
                "verified": sum(1 for t in tasks
                                if t.get("verdict") == Verdict.VERIFIED.value),
                "rejected": sum(1 for t in tasks
                                if t.get("verdict") == Verdict.REJECTED.value),
                "active": project["active"],
            })
        return out

    def get_project_brief(self, project_id: str) -> dict:
        """Compact deterministic summary: identity + context + derived counts."""
        brief = self.context.get_project_brief(project_id)
        progress = self.get_project_progress(project_id)
        brief["tasks"] = progress["counts"]
        brief["workspaces"] = [
            {"id": ws["id"], "node_id": ws["node_id"],
             "local_root": ws["local_root"], "is_default": ws["is_default"],
             "executable": ws["node_id"] == "local"}
            for ws in self.store.workspaces(project_id)]
        return brief

    def get_project_progress(self, project_id: str) -> dict:
        """Progress derived from the ledger — never from an LLM summary.

        Returns derived ``counts`` plus the *narrative* progress entries, kept in
        separate keys so a reader can never mistake prose for a verdict.
        """
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError(f"no project {project_id}")
        tasks = self.store.work_items_for_project(project_id)

        counts = {
            "total": len(tasks),
            "active": 0, "verified": 0, "rejected": 0, "inconclusive": 0,
            "pending": 0, "needs_human": 0, "needs_approval": 0,
            "suspected_lost": 0,
        }
        for task in tasks:
            state = task.get("state")
            verdict = task.get("verdict")
            if state in _ACTIVE_STATES:
                counts["active"] += 1
            if state in _NEEDS_HUMAN_STATES:
                counts["needs_human"] += 1
            if task.get("queue_status") == "needs_approval":
                counts["needs_approval"] += 1
            if (task.get("metadata") or {}).get("suspected_lost"):
                counts["suspected_lost"] += 1
            if verdict == Verdict.VERIFIED.value:
                counts["verified"] += 1
            elif verdict == Verdict.REJECTED.value:
                counts["rejected"] += 1
            elif verdict == Verdict.INCONCLUSIVE.value:
                counts["inconclusive"] += 1
            elif verdict == Verdict.PENDING.value:
                counts["pending"] += 1

        narrative = [
            {"title": e["title"], "body": e["body"],
             "verification": e["verification"], "version": e["version"],
             "updated_at": e["updated_at"]}
            for e in self.store.context_entries(project_id, kind="progress")
        ]
        blockers = [t for t in tasks
                    if t.get("state") in _NEEDS_HUMAN_STATES
                    or t.get("queue_status") == "needs_approval"
                    or (t.get("metadata") or {}).get("suspected_lost")]
        return {
            "project_id": project_id,
            "name": project["name"],
            "context_version": self.store.context_version(project_id),
            "counts": counts,
            "derived_from": "ledger",
            "blocked_tasks": [
                {"task_id": t["task_id"], "state": t.get("state"),
                 "verdict": t.get("verdict"),
                 "queue_status": t.get("queue_status"),
                 "warnings": t.get("warnings") or []}
                for t in blockers],
            "narrative_progress": narrative,
            "recent_tasks": [
                {"task_id": t["task_id"], "title": t.get("title"),
                 "state": t.get("state"), "verdict": t.get("verdict"),
                 "queue_status": t.get("queue_status"),
                 "context_version": t.get("context_version"),
                 "created_at": t.get("created_at")}
                for t in tasks[:10]],
        }

    def get_project_tasks(self, project_id: str, limit: int = 200) -> list[dict]:
        """Work items of one project, newest first, with provenance fields."""
        if not self.store.get_project(project_id):
            raise KeyError(f"no project {project_id}")
        out = []
        for task in self.store.work_items_for_project(project_id, limit=limit):
            out.append({
                "task_id": task["task_id"],
                "title": task.get("title"),
                "state": task.get("state"),
                "verdict": task.get("verdict"),
                "queue_status": task.get("queue_status"),
                "workspace_id": task.get("workspace_id"),
                "context_version": task.get("context_version"),
                "context_entry_refs": task.get("context_entry_refs") or [],
                "has_effective_prompt": bool(task.get("effective_prompt")),
                "created_at": task.get("created_at"),
            })
        return out

    def get_task_provenance(self, task_id: str) -> dict:
        """Exactly what a task was dispatched with, and which knowledge it saw.

        This is the audit answer to "why did the agent do that": the original
        request, the frozen effective prompt, the context version, and the ids of
        every context entry that was injected.
        """
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no task {task_id}")
        refs = task.get("context_entry_refs") or []
        entries = []
        for entry_id in refs:
            entry = self.store.get_context_entry(entry_id)
            if entry:
                entries.append({"id": entry["id"], "kind": entry["kind"],
                                "title": entry["title"],
                                "verification": entry["verification"],
                                "version": entry["version"]})
        return {
            "task_id": task_id,
            "project_id": task.get("project_id"),
            "workspace_id": task.get("workspace_id"),
            "original_task": task.get("original_task"),
            "effective_prompt": task.get("effective_prompt") or task.get("prompt"),
            "context_version": task.get("context_version"),
            "context_entry_refs": refs,
            "context_entries": entries,
            "task_digest": task.get("task_digest"),
            "state": task.get("state"),
            "verdict": task.get("verdict"),
        }

    # ------------------------------------------------------------------ #
    # context writes (thin pass-through, kept here so an LD has one surface)
    # ------------------------------------------------------------------ #
    def get_project_context(self, project_id: str) -> dict:
        return self.context.get_project_context(project_id)

    def add_context_entry(self, project_id: str, entry) -> dict:
        return self.context.add_context_entry(project_id, entry)

    def update_context_entry(self, project_id: str, entry_id: str,
                             expected_version: int, changes: dict, **kwargs) -> dict:
        return self.context.update_context_entry(
            project_id, entry_id, expected_version, changes, **kwargs)

    # ------------------------------------------------------------------ #
    # task submission
    # ------------------------------------------------------------------ #
    def build_task_context(self, project_id: str, task: str, **kwargs) -> BuiltContext:
        """Build (but do not submit) the effective prompt. Useful for previews."""
        return self.builder.build(project_id, task, **kwargs)

    def submit_project_task(self, project_id: str, task: str, *,
                            assertions: Iterable[ArtifactAssertion | dict] = (),
                            priority: int = 0,
                            idempotency_key: str = "",
                            title: str = "",
                            tags: Iterable[str] = (),
                            prerequisites: Iterable[str] = (),
                            require_verified_constraints: bool = False,
                            workspace_id: str | None = None,
                            extra_references: Iterable[str] = (),
                            policy: DispatchPolicy | dict | None = None,
                            timeout_s: int = 900,
                            metadata: dict | None = None,
                            allow_blocked: bool = False) -> dict:
        """Build the project context, freeze the prompt, then enqueue.

        Ordering matters and is the whole point:

        1. build the effective prompt from the *current* context version;
        2. refuse to submit if a prerequisite is missing (unless ``allow_blocked``);
        3. enqueue with the frozen prompt, recording ``original_task``,
           ``context_version`` and the entry ids used.

        Because the prompt is materialised here, a later context edit cannot change
        what an already-queued task will run. Re-submitting with the same
        ``idempotency_key`` returns the original task instead of rebuilding.
        """
        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                out = dict(existing)
                out["deduplicated"] = True
                return out

        built = self.builder.build(
            project_id, task, tags=tags, prerequisites=prerequisites,
            require_verified_constraints=require_verified_constraints,
            workspace_id=workspace_id, extra_references=extra_references)
        if built.blocked and not allow_blocked:
            raise ContextBuildBlocked(built.gaps, built)

        assertion_objs = [
            a if isinstance(a, ArtifactAssertion) else ArtifactAssertion(**a)
            for a in assertions]
        if isinstance(policy, dict):
            policy = DispatchPolicy(**policy)

        scope = {"workspace_id": built.workspace_id,
                 "context_version": built.context_version}
        record = self.plane.enqueue(
            built.effective_prompt, built.workspace_root or "",
            assertions=assertion_objs, policy=policy,
            title=title or task[:60], idempotency_key=idempotency_key,
            timeout_s=timeout_s,
            metadata={**(metadata or {}),
                      "project_task": True,
                      "context_gaps": built.gaps,
                      "context_warnings": built.warnings},
            priority=priority, project_id=project_id,
            workspace_id=built.workspace_id,
            original_task=task,
            context_version=built.context_version,
            context_entry_refs=built.entry_refs,
            digest_scope=scope)

        # Register the session link lazily: the worker job does not exist yet, but
        # the (project, workspace, task) relation is already durable, so continuity
        # never depends on a live session.
        self.store.record_session_reference(
            project_id, workspace_id=built.workspace_id,
            worker_instance_id=None, adapter=self.plane.adapter.name,
            session_id=None, work_item_id=record["task_id"], attempt_id=None)

        out = dict(record)
        out["context"] = {
            "version": built.context_version,
            "entry_refs": built.entry_refs,
            "sections": built.sections,
            "gaps": built.gaps,
            "warnings": built.warnings,
            "blocked": built.blocked,
            "workspace_id": built.workspace_id,
        }
        out["effective_prompt"] = built.effective_prompt
        out["original_task"] = task
        return out

    # ------------------------------------------------------------------ #
    # workspaces
    # ------------------------------------------------------------------ #
    def list_workspaces(self, project_id: str) -> list[dict]:
        if not self.store.get_project(project_id):
            raise KeyError(f"no project {project_id}")
        return [{**ws, "executable": ws["node_id"] == "local"}
                for ws in self.store.workspaces(project_id)]

    def register_workspace(self, project_id: str, local_root: str, **kwargs) -> dict:
        """Register a workspace. Non-local nodes are declarations only (M5)."""
        ws = self.store.register_workspace(project_id, local_root, **kwargs)
        return {**ws, "executable": ws["node_id"] == "local"}

    def list_session_references(self, project_id: str, limit: int = 50) -> list[dict]:
        if not self.store.get_project(project_id):
            raise KeyError(f"no project {project_id}")
        return self.store.session_references(project_id, limit=limit)


def project_service(plane: ControlPlane, **kwargs: Any) -> ProjectService:
    return ProjectService(plane, **kwargs)
