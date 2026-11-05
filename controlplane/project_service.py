"""Vendor-neutral project operations facade — deterministic project operations
used by the restricted M4-A LD tool surface (M1–M4-A).

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
* No HTTP surface. M4-A has only a local single-turn coordinator and CLI.
* No remote dispatch. A non-local workspace is reported as unsupported rather
  than silently executed locally (M5).
* M2 context updates are deterministic candidate writes; they do not plan the
  next task or promote knowledge to verified.
"""
from __future__ import annotations

from typing import Any, Iterable

from adapters.base import ArtifactAssertion, DispatchPolicy, TaskState, Verdict
from controlplane.context_builder import (
    BuiltContext,
    ContextBuildBlocked,
    ContextBuilder,
)
from controlplane.handoff import (
    HandoffIdempotencyConflict,
    HandoffNotReady,
    HandoffPlanner,
    HandoffStale,
    handoff_signature,
    reconcile_handoff_checkpoint,
    reconcile_handoff_checkpoints,
    verify_checkpoint_ref,
)
from controlplane.plane import ControlPlane
from controlplane.project_context import ProjectContextStore
from controlplane.store import TaskStore

#: Queue/state buckets used by the progress projection. Kept explicit so a new
#: TaskState cannot silently fall out of the counts.
_ACTIVE_STATES = (TaskState.QUEUED.value, TaskState.STARTING.value,
                  TaskState.RUNNING.value, TaskState.WAITING_FOR_APPROVAL.value)
_NEEDS_HUMAN_STATES = (TaskState.DISPATCH_UNKNOWN.value, TaskState.UNKNOWN.value)

#: Attention flags that make a *settled* task a project-level blocker.
#:
#: A refused task ends ``failed``, not ``dispatch_unknown``, so selecting
#: blockers by state alone left it out of ``blocked_tasks`` — and therefore out
#: of the LD blocker list — even though the Ledger said exactly why it stopped.
#: Only flags whose remedy requires a human belong here; a merely rejected task
#: (bad artifact) is still reported through counts and recent_tasks.
_HUMAN_BLOCKER_FLAGS = frozenset({
    "permission_blocked",
    "worker_not_authenticated",
})

#: Queue status of a handoff whose Git checkpoint is not (yet) kept alive by a
#: private ref.  It is deliberately NOT ``ready``: the dispatcher only ever
#: claims ``ready`` work, so an unprotected checkpoint can never reach a Worker.
CHECKPOINT_BLOCKED_QUEUE = "checkpoint_unprotected"


def _has_warning(task: dict, prefix: str) -> bool:
    return any(str(item).startswith(prefix)
               for item in (task.get("warnings") or []))


def _task_attention_flags(task: dict) -> list[str]:
    """Deterministic human-attention reasons for one ledger work item."""
    flags: list[str] = []
    state = task.get("state")
    if task.get("queue_status") == "needs_approval":
        flags.append("needs_approval")
    if state == TaskState.DISPATCH_UNKNOWN.value:
        flags.append("dispatch_unknown")
    if (task.get("metadata") or {}).get("suspected_lost"):
        flags.append("suspected_lost")
    if _has_warning(task, "SESSION_BINDING_CONFLICT:"):
        flags.append("session_binding_conflict")
    if _has_warning(task, "PERMISSION_DENIED:"):
        # The Worker was refused a tool and no approval prompt could be
        # answered. Only a human can decide (grant the permission, change the
        # task, or drop it) — an automatic retry would be refused identically.
        flags.append("permission_blocked")
    if _has_warning(task, "WORKER_NOT_AUTHENTICATED:"):
        # The Worker had no usable credentials, so nothing was executed. A retry
        # is futile and would burn budget: the human must repair the Worker's
        # login (or point it at a different account) before this task makes any
        # sense. It is a *distinct* flag from permission_blocked because the
        # remedy is different — granting a tool is not the fix for a logged-out
        # Worker, and a logged-out Worker is not fixed by editing the task.
        flags.append("worker_not_authenticated")
    if (task.get("queue_status") == "needs_human"
            and not flags and state == TaskState.UNKNOWN.value):
        flags.append("unknown")
    return flags


class ProjectService:
    """Project-centric operations on top of an existing :class:`ControlPlane`."""

    def __init__(self, plane: ControlPlane, *,
                 budget_chars: int | None = None):
        self.plane = plane
        self.store: TaskStore = plane.store
        self.context = ProjectContextStore(self.store)
        self.builder = (ContextBuilder(self.store, budget_chars=budget_chars)
                        if budget_chars else ContextBuilder(self.store))
        self.handoffs = HandoffPlanner(self.store, self.builder)

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
            })
        return out

    def get_project_brief(self, project_id: str, *,
                          allowed_security_domains: Iterable[str] | None = None
                          ) -> dict:
        """Compact deterministic summary: identity + context + derived counts.

        ``allowed_security_domains`` is used by M4-A before constructing model
        input.  ``None`` preserves the original local/operator view; an explicit
        iterable exposes titles only from those domains.  Entry bodies are never
        part of this brief.
        """
        brief = self.context.get_project_brief(project_id)
        if allowed_security_domains is not None:
            allowed = {str(item) for item in allowed_security_domains}
            entries = [
                entry for entry in self.context.list_entries(project_id)
                if entry.get("security_domain") in allowed
            ]

            def titles(kind: str, limit: int) -> list[str]:
                return [entry["title"] for entry in entries
                        if entry.get("kind") == kind][:limit]

            brief.update({
                "entry_count": len(entries),
                "verified_count": sum(
                    1 for entry in entries
                    if entry.get("verification") == "verified"),
                "background": titles("background", 3),
                "constraints": titles("constraints", 10),
                "progress": titles("progress", 5),
                "open_questions": [
                    entry["title"] for entry in entries
                    if entry.get("verification") != "verified"
                ][:5],
                "context_visibility": {
                    "security_domains": sorted(allowed),
                    "omitted_entry_count": (
                        len(self.context.list_entries(project_id)) - len(entries)),
                },
            })
        progress = self.get_project_progress(project_id)
        brief["tasks"] = progress["counts"]
        brief["workspaces"] = [
            {"id": ws["id"], "node_id": ws["node_id"],
             "local_root": ws["local_root"], "is_default": ws["is_default"],
             "active": ws["active"],
             "executable": ws["node_id"] == "local"}
            for ws in self.store.workspaces(project_id)]
        return brief

    def get_project_progress(self, project_id: str, *,
                             allowed_security_domains: Iterable[str] | None = None
                             ) -> dict:
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
            "dispatch_unknown": 0, "suspected_lost": 0,
            "session_binding_conflict": 0,
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
            if state == TaskState.DISPATCH_UNKNOWN.value:
                counts["dispatch_unknown"] += 1
            if (task.get("metadata") or {}).get("suspected_lost"):
                counts["suspected_lost"] += 1
            if _has_warning(task, "SESSION_BINDING_CONFLICT:"):
                counts["session_binding_conflict"] += 1
            if verdict == Verdict.VERIFIED.value:
                counts["verified"] += 1
            elif verdict == Verdict.REJECTED.value:
                counts["rejected"] += 1
            elif verdict == Verdict.INCONCLUSIVE.value:
                counts["inconclusive"] += 1
            elif verdict == Verdict.PENDING.value:
                counts["pending"] += 1

        allowed = (None if allowed_security_domains is None
                   else {str(item) for item in allowed_security_domains})
        progress_entries = self.store.context_entries(project_id, kind="progress")
        narrative = [
            {"title": e["title"], "body": e["body"],
             "verification": e["verification"], "version": e["version"],
             "security_domain": e["security_domain"],
             "updated_at": e["updated_at"]}
            for e in progress_entries
            if allowed is None or e.get("security_domain") in allowed
        ]
        needs_merge = [
            {"entry_id": entry["id"], "kind": entry["kind"],
             "title": entry["title"], "tags": entry.get("tags") or [],
             "verification": entry["verification"],
             "security_domain": entry["security_domain"]}
            for entry in self.store.context_entries(project_id)
            if "needs_merge" in (entry.get("tags") or [])
            and (allowed is None or entry.get("security_domain") in allowed)
        ]
        blockers = [t for t in tasks
                    if t.get("state") in _NEEDS_HUMAN_STATES
                    or t.get("queue_status") == "needs_approval"
                    or (t.get("metadata") or {}).get("suspected_lost")
                    or _has_warning(t, "SESSION_BINDING_CONFLICT:")
                    # A settled task blocked on a human-only remedy.  Its state
                    # is `failed`, so the state test above never selected it.
                    or _HUMAN_BLOCKER_FLAGS.intersection(_task_attention_flags(t))]
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
                 "attention_flags": _task_attention_flags(t),
                 "warnings": t.get("warnings") or []}
                for t in blockers],
            "narrative_progress": narrative,
            "needs_merge_context": needs_merge,
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

    def get_task_status(self, task_id: str) -> dict:
        """Read-only task state suitable for LD decisions.

        The projection deliberately keeps Worker state and control-plane verdict
        separate.  It does not expose or mutate the frozen Effective Prompt.
        """
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no task {task_id}")
        attempts = self.store.attempts(task_id)
        current_attempt = next(
            (attempt for attempt in reversed(attempts)
             if attempt.get("id") == task.get("current_attempt_id")), None)
        metadata = task.get("metadata") or {}
        return {
            "task_id": task_id,
            "project_id": task.get("project_id"),
            "title": task.get("title"),
            "state": task.get("state"),
            "verdict": task.get("verdict"),
            "queue_status": task.get("queue_status"),
            "attention_flags": _task_attention_flags(task),
            "workspace_id": task.get("workspace_id"),
            "context_version": task.get("context_version"),
            "current_attempt_id": task.get("current_attempt_id"),
            "current_attempt": current_attempt,
            "worker_job_id": task.get("worker_job_id"),
            "assertions": task.get("assertions") or [],
            "assertion_results": task.get("assertion_results") or [],
            "warnings": task.get("warnings") or [],
            "ld_goal_ref": metadata.get("ld_goal_ref"),
            "ld_submission_signature": metadata.get("ld_submission_signature"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }

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
            "ld_goal_ref": (task.get("metadata") or {}).get("ld_goal_ref"),
            "ld_submission_signature": (
                task.get("metadata") or {}).get("ld_submission_signature"),
            "handoff": bool((task.get("metadata") or {}).get("handoff")),
            "handoff_source": {
                key: (task.get("metadata") or {}).get(key)
                for key in ("source_work_item_id", "source_attempt_id",
                            "source_session_reference_id", "checkpoint_ref",
                            "handoff_signature")
                if (task.get("metadata") or {}).get(key) is not None
            },
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

    def update_context_from_task(self, task_id: str) -> dict:
        """Replay M2's deterministic terminal-task update (idempotent)."""
        return self.plane.update_project_context(task_id)

    def context_update_receipts(self, task_id: str) -> list[dict]:
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no task {task_id}")
        return self.store.context_update_receipts(task_id)

    # ------------------------------------------------------------------ #
    # task submission
    # ------------------------------------------------------------------ #
    def _effective_policy(self, policy: DispatchPolicy | None,
                          project_id: str | None) -> DispatchPolicy:
        """The policy ``ControlPlane.enqueue`` would actually use.

        Same precedence, resolved through the same helper, so a preview and a
        submission cannot disagree about whether the task is isolated: explicit
        argument, then the project's registered policy, then the control plane
        default.  Duplicating the order here (rather than assuming
        ``isolation="worktree"``) is what makes the prompt honest.
        """
        if isinstance(policy, dict):
            policy = DispatchPolicy(**policy)
        if policy is not None:
            return policy
        project = self.store.get_project(project_id) if project_id else None
        return ControlPlane._project_policy(project) or self.plane.policy

    def build_task_context(self, project_id: str, task: str, *,
                           isolation: str | None = None,
                           **kwargs) -> BuiltContext:
        """Build (but do not submit) the effective prompt. Useful for previews.

        ``isolation`` defaults to the policy the project would really dispatch
        with, so a preview shows the same contract a submission freezes.
        """
        if isolation is None:
            isolation = self._effective_policy(None, project_id).isolation
        kwargs.pop("isolation", None)
        return self.builder.build(project_id, task, isolation=isolation, **kwargs)

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

        if isinstance(policy, dict):
            policy = DispatchPolicy(**policy)
        # T34-F02: a shell Worker executes the *frozen prompt itself*.  Context
        # injection builds a multi-line structured prompt whose first lines are
        # not valid shell, so this combination can only produce a rejected task.
        # Refuse it at submission time instead of letting it fail silently.
        if policy is not None and policy.mode == "shell":
            raise ValueError(
                "submit_project_task refuses shell policy: the context-injected "
                "multi-line effective prompt would be executed as a shell script "
                "and fail; use ControlPlane.enqueue with a raw command for shell "
                "jobs without project context injection")

        # The contract in the prompt must describe the policy that will really be
        # used, and that is not necessarily the caller's argument: ``enqueue``
        # falls back to the project's own policy, then the control plane default.
        # Resolving it here (before the prompt is built) is what keeps an
        # isolation=none project task from claiming it runs in a worktree.
        effective_policy = self._effective_policy(policy, project_id)
        built = self.builder.build(
            project_id, task, tags=tags, prerequisites=prerequisites,
            require_verified_constraints=require_verified_constraints,
            workspace_id=workspace_id, extra_references=extra_references,
            isolation=effective_policy.isolation)
        if built.blocked and not allow_blocked:
            raise ContextBuildBlocked(built.gaps, built)

        assertion_objs = [
            a if isinstance(a, ArtifactAssertion) else ArtifactAssertion(**a)
            for a in assertions]

        scope = {"workspace_id": built.workspace_id,
                 "context_version": built.context_version}
        record = self.plane.enqueue(
            built.effective_prompt, built.workspace_root or "",
            assertions=assertion_objs, policy=effective_policy,
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
    # M3 handoff: prepare is read-only; submit creates a normal WorkItem
    # ------------------------------------------------------------------ #
    def prepare_handoff(self, source_work_item_id: str, task: str, **kwargs) -> dict:
        """Preview a new-Session handoff without writing or calling a Worker.

        Read-only with respect to the Ledger, the source worktree and every
        branch.  In ``reachable`` mode the accepted-state checkpoint is
        assembled in the object store (unreferenced, deterministic), so the
        preview signature equals what ``submit_handoff`` will freeze.
        """
        return self.handoffs.prepare(source_work_item_id, task, **kwargs).to_dict()

    def submit_handoff(self, source_work_item_id: str, task: str, *,
                       assertions: Iterable[ArtifactAssertion | dict] = (),
                       priority: int = 0,
                       idempotency_key: str = "",
                       title: str = "",
                       tags: Iterable[str] = (),
                       prerequisites: Iterable[str] = (),
                       require_verified_constraints: bool = False,
                       target_workspace_id: str | None = None,
                       extra_references: Iterable[str] = (),
                       require_checkpoint: bool = True,
                       checkpoint_mode: str = "integrated",
                       expected_handoff_signature: str | None = None,
                       policy: DispatchPolicy | dict | None = None,
                       timeout_s: int = 900,
                       metadata: dict | None = None) -> dict:
        """Atomically persist a prepared handoff as an ordinary WorkItem.

        The readiness checks are rerun here to close the prepare/submit gap.  The
        handoff source and checkpoint live in WorkItem metadata at insert time;
        the audit event is useful history, never the recovery source of truth.

        ``checkpoint_mode='reachable'`` hands over a commit assembled from the
        source's *accepted* state (see ``HandoffPlanner.build_source_checkpoint``):
        nothing in the source worktree, index or branches changes, and the
        checkpoint is protected by a private ref only after the WorkItem is
        durably created.  ``'integrated'`` keeps the strict M3 semantics: the
        checkpoint must already be contained in the target workspace HEAD.
        """
        # Validate the caller-controlled policy before any Git or Ledger work:
        # a request that will be refused must not have produced anything.
        if isinstance(policy, dict):
            policy = DispatchPolicy(**policy)
        # Same refusal as submit_project_task: a handoff prompt is a multi-line
        # context-injected document, never a valid shell script.
        if policy is not None and policy.mode == "shell":
            raise ValueError(
                "submit_handoff refuses shell policy: the context-injected "
                "multi-line handoff prompt would be executed as a shell script "
                "and fail")
        if policy is not None:
            policy.validate()
        # Resolve the policy *before* building the prompt: the execution contract
        # in the frozen prompt must describe how the handoff will really run, not
        # a hard-coded "you are already isolated".
        source = self.store.get(source_work_item_id) or {}
        effective_policy = self._effective_policy(policy, source.get("project_id"))
        prepared = self.handoffs.prepare(
            source_work_item_id, task,
            target_workspace_id=target_workspace_id,
            tags=tags, prerequisites=prerequisites,
            require_verified_constraints=require_verified_constraints,
            extra_references=extra_references,
            require_checkpoint=require_checkpoint,
            checkpoint_mode=checkpoint_mode,
            isolation=effective_policy.isolation)
        prepared_dict = prepared.to_dict()
        if not prepared.ready:
            raise HandoffNotReady(prepared.blockers, prepared_dict)
        if (expected_handoff_signature
                and expected_handoff_signature != prepared.handoff_signature):
            raise HandoffStale(
                "prepared handoff changed before submit: expected "
                f"{expected_handoff_signature}, got {prepared.handoff_signature}")

        assertion_objs = [
            item if isinstance(item, ArtifactAssertion) else ArtifactAssertion(**item)
            for item in assertions]
        effective_policy.validate()
        submission_signature = handoff_signature({
            "prepared_handoff_signature": prepared.handoff_signature,
            "assertions": [item.to_dict() for item in assertion_objs],
            "policy": effective_policy.to_dict(),
            "timeout_s": timeout_s,
        })

        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                existing_signature = (existing.get("metadata") or {}).get(
                    "handoff_signature")
                if existing_signature != submission_signature:
                    raise HandoffIdempotencyConflict(
                        f"idempotency key {idempotency_key!r} already belongs to "
                        f"handoff signature {existing_signature!r}, not "
                        f"{submission_signature!r}")
                out = dict(existing)
                out["deduplicated"] = True
                out["handoff_context"] = prepared_dict
                return out

        compact_context = {
            "prepared_handoff_signature": prepared.handoff_signature,
            "source_work_item_id": prepared.source_work_item_id,
            "source_attempt_id": prepared.source_attempt_id,
            "source_session_reference_id": (
                prepared.source_session_reference or {}).get("id"),
            "source_facts": prepared.source_facts,
            "checkpoint": prepared.checkpoint,
            "checkpoint_ref": prepared.checkpoint_ref,
            "target_workspace_id": prepared.target_workspace_id,
            "context_version": prepared.context_version,
            "context_entry_refs": prepared.context_entry_refs,
            "unresolved": prepared.unresolved,
        }
        handoff_metadata = {
            **(metadata or {}),
            "handoff": True,
            "source_work_item_id": prepared.source_work_item_id,
            "source_attempt_id": prepared.source_attempt_id,
            "source_session_reference_id": (
                prepared.source_session_reference or {}).get("id"),
            "checkpoint_ref": prepared.checkpoint_ref,
            "handoff_signature": submission_signature,
            "handoff_context": compact_context,
        }
        # A reachable handoff is enqueued *deferred*: the row must be durable
        # before the protective ref is written (a rejected submission leaves no
        # ref), but it must not be claimable in that window either.  The
        # dispatcher only claims 'ready', so a failing ``git update-ref`` can no
        # longer reach a Worker — the row stays visible and blocked instead.
        needs_protection = bool(
            checkpoint_mode == "reachable" and prepared.checkpoint_ref
            and prepared.target_workspace_root)
        record = self.plane.enqueue(
            prepared.effective_prompt, prepared.target_workspace_root or "",
            assertions=assertion_objs, policy=effective_policy,
            title=title or task[:60], idempotency_key=idempotency_key,
            timeout_s=timeout_s, metadata=handoff_metadata,
            priority=priority, project_id=prepared.project_id,
            workspace_id=prepared.target_workspace_id,
            original_task=task,
            context_version=prepared.context_version,
            context_entry_refs=prepared.context_entry_refs,
            digest_scope={
                "workspace_id": prepared.target_workspace_id,
                "context_version": prepared.context_version,
                "handoff_signature": submission_signature,
            },
            deferred_queue_status=(
                CHECKPOINT_BLOCKED_QUEUE if needs_protection else None))

        # create_work_item() is the cross-process race boundary.  A loser must
        # verify the winning row instead of trusting ControlPlane's generic
        # idempotency return path.
        stored_signature = (record.get("metadata") or {}).get("handoff_signature")
        if stored_signature != submission_signature:
            raise HandoffIdempotencyConflict(
                f"idempotency key {idempotency_key!r} was concurrently claimed by "
                f"handoff signature {stored_signature!r}")
        protection = {"ok": True, "written": False, "error": None}
        if not record.get("deduplicated"):
            if needs_protection:
                protection = self.handoffs.protect_checkpoint_verified(
                    prepared.target_workspace_root, record["task_id"],
                    prepared.checkpoint_ref)
                self.store.append_event(
                    record["task_id"],
                    "handoff_checkpoint_protected" if protection["ok"]
                    else "handoff_checkpoint_unprotected", {
                        "checkpoint_ref": prepared.checkpoint_ref,
                        "ref": protection.get("ref"),
                        "ref_commit": protection.get("ref_commit"),
                        "written": protection.get("written"),
                        "error": protection.get("error")})
                # Readiness is granted only by the deterministic reconciler,
                # which re-proves the same three facts inside the dispatch path.
                reconcile_handoff_checkpoint(self.store, record["task_id"])
                record = self.store.get(record["task_id"]) or record
            self.store.append_event(record["task_id"], "handoff_submitted", {
                "source_work_item_id": prepared.source_work_item_id,
                "source_attempt_id": prepared.source_attempt_id,
                "source_session_reference_id": (
                    prepared.source_session_reference or {}).get("id"),
                "checkpoint_ref": prepared.checkpoint_ref,
                "checkpoint_capture": (prepared.checkpoint or {}).get("capture"),
                "checkpoint_protected": bool(protection["ok"]),
                "checkpoint_ref_protected": bool(protection["ok"]),
                "checkpoint_ready": record.get("queue_status") == "ready",
                "handoff_signature": submission_signature,
            })
        out = dict(record)
        out["handoff_context"] = prepared_dict
        out["checkpoint_ready"] = record.get("queue_status") == "ready"
        return out

    def reconcile_handoff_checkpoints(self, *, limit: int = 50) -> dict:
        """Retry/verify protective checkpoint refs and release ready handoffs."""
        return reconcile_handoff_checkpoints(self.store, limit=limit)

    def handoff_checkpoint_status(self, task_id: str) -> dict:
        """Queryable readiness of one handoff's checkpoint (never a guess)."""
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no task {task_id}")
        metadata = task.get("metadata") or {}
        if not metadata.get("handoff"):
            return {"task_id": task_id, "handoff": False,
                    "checkpoint_ready": False,
                    "reason": "task is not a handoff"}
        verified = verify_checkpoint_ref(
            task.get("cwd"), task_id, metadata.get("checkpoint_ref"))
        return {
            "task_id": task_id,
            "handoff": True,
            "queue_status": task.get("queue_status"),
            "checkpoint_ref": metadata.get("checkpoint_ref"),
            "checkpoint_ready": verified["ok"],
            "ref": verified["ref"],
            "ref_commit": verified["ref_commit"],
            "object_exists": verified["object_exists"],
            "reason": verified["error"],
        }

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

    def list_project_sessions(self, project_id: str, limit: int = 50) -> list[dict]:
        """M3 name for the observed Session Registry projection."""
        return self.list_session_references(project_id, limit=limit)


def project_service(plane: ControlPlane, **kwargs: Any) -> ProjectService:
    return ProjectService(plane, **kwargs)
