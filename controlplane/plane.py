"""Minimal control plane facade: dispatch, reconcile, verify, summarize.

Everything the worker does not guarantee lives here:

  * dispatch **policy** (no implicit un-isolated shell execution),
  * idempotency keys and the `dispatch_unknown` state (never blind-retry),
  * a pre-dispatch baseline so acceptance can prove provenance,
  * `work_item -> run_attempt -> worker_job` mapping (resume-safe),
  * the aggregated summary a Foreman can read instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from adapters.base import (
    ArtifactAssertion,
    DispatchPolicy,
    DispatchRequest,
    TaskSnapshot,
    TaskState,
    UnsupportedOperation,
    Verdict,
)
from controlplane.context_update import ProjectContextUpdateLoop
from controlplane.store import TaskStore
from controlplane.verify import capture_baseline, check_all, side_effects, verdict_for


class ApprovalRequired(RuntimeError):
    """High-risk dispatch without a matching, unconsumed human approval."""


#: How long a dispatch claim (queue_status='dispatching' owned by another
#: runtime) is trusted before a recovering runtime may treat it as a crash
#: leftover (review P1: multi-runtime startup race).
DISPATCH_CLAIM_LEASE_S = 300.0


def task_digest(prompt: str, policy: DispatchPolicy,
                assertions: list[ArtifactAssertion], cwd: str,
                extra_scope: dict | None = None) -> str:
    """Content digest of *exactly* what will run.

    An approval binds to this digest, so editing the prompt/assertions/target after
    the human said "yes" invalidates the approval instead of silently reusing it.

    ``extra_scope`` lets a caller bind additional execution facts into the digest
    (M1 uses it for ``workspace_id`` and ``context_version``). It is omitted from
    the payload when empty, so digests of pre-existing tasks are unchanged.
    """
    payload: dict[str, Any] = {
        "prompt": prompt.strip(),
        "cwd": str(Path(cwd).resolve()),
        "policy": policy.to_dict(),
        "assertions": [a.to_dict() for a in assertions],
    }
    if extra_scope:
        payload["scope"] = extra_scope
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


class ControlPlane:
    def __init__(self, adapter, store: TaskStore | None = None,
                 policy: DispatchPolicy | None = None, *,
                 require_registered_projects: bool = False,
                 context_updates: ProjectContextUpdateLoop | None = None):
        self.adapter = adapter
        self.store = store or TaskStore()
        self.policy = policy or DispatchPolicy()
        self.require_registered_projects = require_registered_projects
        self.context_updates = context_updates or ProjectContextUpdateLoop(self.store)

    # ------------------------------------------------------------------ #
    # projects / path boundary
    # ------------------------------------------------------------------ #
    def register_project(self, name: str, root: str, *,
                         default_adapter: str | None = None,
                         policy: DispatchPolicy | dict | None = None,
                         concurrency_limit: int = 2,
                         active: bool = True) -> dict:
        """Register one canonical Git root as an allowed project boundary."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("project name must not be empty")
        adapter = (default_adapter or self.adapter.name).strip()
        if not adapter:
            raise ValueError("project default_adapter must not be empty")
        canonical = self._canonical_git_root(root)
        if isinstance(policy, DispatchPolicy):
            policy.validate()
            policy_data = policy.to_dict()
        else:
            policy_data = dict(policy or {})
            if policy_data:
                checked = DispatchPolicy(**policy_data)
                checked.validate()
                policy_data = checked.to_dict()
        return self.store.register_project(
            clean_name, str(canonical), adapter, policy=policy_data,
            concurrency_limit=concurrency_limit, active=active)

    def project(self, project_id_or_name: str) -> dict:
        project = (self.store.get_project(project_id_or_name)
                   or self.store.project_by_name(project_id_or_name))
        if not project:
            raise KeyError(f"no project {project_id_or_name}")
        return project

    def resolve_project_for_cwd(self, cwd: str, project_id: str | None = None,
                                *, require_registered: bool | None = None
                                ) -> tuple[str, dict | None]:
        """Resolve symlinks/``..`` and bind a task to its registered root.

        An explicit project never falls through to another matching project. This
        is the boundary check that stops ``project-a/../project-b`` from being
        executed under project A's policy or concurrency budget.
        """
        path = Path(cwd).expanduser().resolve()
        required = (self.require_registered_projects if require_registered is None
                    else require_registered)
        project = self.project(project_id) if project_id else None
        if project is None:
            matches = [candidate for candidate in self.store.all_projects()
                       if self._contains(Path(candidate["root"]), path)]
            if matches:
                project = max(matches, key=lambda item: len(Path(item["root"]).parts))
        if project is None:
            if required:
                if not path.is_dir():
                    raise ValueError(f"cwd is not a directory: {path}")
                raise ValueError(f"cwd is not inside a registered project: {path}")
            return str(path), None
        if not path.is_dir():
            raise ValueError(f"cwd is not a directory: {path}")
        if not project.get("active"):
            raise ValueError(f"project is disabled: {project['name']}")
        # ``root`` was canonicalized when registered. Do not resolve it again:
        # if that directory is later replaced by a symlink, a fresh resolution
        # could silently move the trusted boundary to the symlink target.
        root = Path(project["root"])
        if not self._contains(root, path):
            raise ValueError(
                f"cwd escapes project {project['name']} boundary: {path} not under {root}")
        return str(path), project

    @staticmethod
    def _contains(root: Path, path: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _canonical_git_root(root: str) -> Path:
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"project root is not a directory: {path}")
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                check=False, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"cannot inspect Git project root {path}: {exc}") from exc
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip() or "not a Git working tree"
            raise ValueError(f"project root must be a Git working tree: {path} ({detail})")
        git_root = Path(result.stdout.strip()).resolve()
        if git_root != path:
            raise ValueError(
                f"project root must be the Git top-level directory: {path}; use {git_root}")
        return git_root

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, prompt: str, cwd: str, *, assertions: list[ArtifactAssertion] | None = None,
                 policy: DispatchPolicy | None = None, title: str = "",
                 idempotency_key: str = "", timeout_s: int = 900,
                 metadata: dict | None = None, priority: int = 0,
                 project_id: str | None = None) -> dict:
        """Persist intent and immediately perform one Worker dispatch."""
        self.store.assert_writable("dispatch")
        assertions = assertions or []
        cwd, project = self.resolve_project_for_cwd(cwd, project_id)
        policy = policy or self._project_policy(project) or self.policy
        risks = policy.validate()

        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                out = dict(existing)
                out["deduplicated"] = True
                return out

        digest = task_digest(prompt, policy, assertions, cwd)
        approval = self._matching_approval(policy, digest, cwd, required=True)
        record = self._new_record(
            self.store.new_task_id(), prompt, cwd, assertions, policy, title,
            idempotency_key, metadata, risks, queue_status="dispatching",
            priority=priority, digest=digest,
            project_id=(project or {}).get("id"))
        record, created = self.store.create_work_item(record)
        if not created:
            out = dict(record)
            out["deduplicated"] = True
            self.store.append_event(record["task_id"], "dispatch_deduplicated_at_insert",
                                    {"key": idempotency_key})
            return out
        return self._dispatch_record(record, approval=approval, timeout_s=timeout_s)

    def enqueue(self, prompt: str, cwd: str, *,
                assertions: list[ArtifactAssertion] | None = None,
                policy: DispatchPolicy | None = None, title: str = "",
                idempotency_key: str = "", timeout_s: int = 900,
                metadata: dict | None = None, priority: int = 0,
                project_id: str | None = None,
                workspace_id: str | None = None,
                original_task: str | None = None,
                context_version: int | None = None,
                context_entry_refs: list[str] | None = None,
                digest_scope: dict | None = None) -> dict:
        """Persist work for automatic Runtime dispatch without calling a Worker.

        The ``workspace_id`` / ``original_task`` / ``context_*`` / ``digest_scope``
        arguments are the project-centric seam (M1). They are all optional: a
        caller that omits them gets exactly the pre-M1 behaviour, including an
        unchanged ``task_digest``.
        """
        self.store.assert_writable("enqueue")
        assertions = assertions or []
        cwd, project = self.resolve_project_for_cwd(cwd, project_id)
        policy = policy or self._project_policy(project) or self.policy
        risks = policy.validate()
        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                out = dict(existing)
                out["deduplicated"] = True
                return out

        digest = task_digest(prompt, policy, assertions, cwd, extra_scope=digest_scope)
        approval = (self.store.valid_approval_by_digest(digest)
                    if policy.requires_approval() else None)
        queue_status = "ready" if (not policy.requires_approval() or approval) else "needs_approval"
        record = self._new_record(
            self.store.new_task_id(), prompt, cwd, assertions, policy, title,
            idempotency_key, {**(metadata or {}), "timeout_s": timeout_s}, risks,
            queue_status=queue_status, priority=priority, digest=digest,
            project_id=(project or {}).get("id"),
            workspace_id=workspace_id, original_task=original_task,
            context_version=context_version,
            context_entry_refs=context_entry_refs)
        record, created = self.store.create_work_item(record)
        if not created:
            out = dict(record)
            out["deduplicated"] = True
            return out

        task_id = record["task_id"]
        self.store.append_event(task_id, "work_enqueued", {
            "adapter": self.adapter.name, "priority": priority,
            "queue_status": queue_status, "risk": policy.effective_risk(),
            "project_id": (project or {}).get("id"),
            "workspace_id": workspace_id,
            "context_version": context_version,
            "context_entry_refs": list(context_entry_refs or []),
            "context_injected": original_task is not None,
        })
        if queue_status == "needs_approval":
            pending = [a for a in self.store.all_approvals()
                       if a.get("digest") == digest and a.get("decision") == "pending"]
            if not pending:
                scope = {"risk": policy.effective_risk(),
                         "cwd": str(Path(cwd).resolve()), "policy": policy.to_dict(),
                         "assertions": [a.to_dict() for a in assertions]}
                self.store.request_approval(task_id, digest, scope,
                                            note="automatic queue approval request")
            self.store.append_event(task_id, "approval_required", {
                "digest": digest, "risk": policy.effective_risk(), "cwd": str(cwd)})
        out = dict(self.store.get(task_id) or record)
        out["approval_required"] = queue_status == "needs_approval"
        return out

    def dispatch_next(self, owner: str, concurrency_limit: int = 4) -> dict | None:
        """Atomically claim and dispatch one queued item for this Adapter."""
        self.store.assert_writable("dispatch_next")
        rec = self.store.claim_next_ready(self.adapter.name, owner, concurrency_limit)
        if not rec:
            return None
        try:
            policy = DispatchPolicy(**(rec.get("policy") or {}))
            approval = None
            if policy.requires_approval():
                approval = self.store.valid_approval_by_digest(rec.get("task_digest") or "")
                if not approval:
                    self.store.append_event(rec["task_id"], "approval_required", {
                        "digest": rec.get("task_digest"), "source": "runtime_dispatch"})
                    return self.store.update(rec["task_id"], queue_status="needs_approval",
                                             dispatch_claim_owner=None, dispatch_claimed_at=None)
            timeout_s = int((rec.get("metadata") or {}).get("timeout_s") or 900)
            return self._dispatch_record(rec, approval=approval, timeout_s=timeout_s)
        except ApprovalRequired as exc:
            self.store.append_event(rec["task_id"], "approval_required", {
                "digest": rec.get("task_digest"), "source": "reservation_race",
                "error": str(exc)})
            return self.store.update(
                rec["task_id"], queue_status="needs_approval",
                dispatch_claim_owner=None, dispatch_claimed_at=None)
        except Exception as exc:  # noqa: BLE001 - never leave a live process stuck in claimed
            self.store.append_event(rec["task_id"], "dispatch_preparation_failed", {
                "error": f"{type(exc).__name__}: {exc}", "owner": owner})
            return self.store.update(
                rec["task_id"], queue_status="needs_human",
                dispatch_claim_owner=None, dispatch_claimed_at=None,
                warnings=list(rec.get("warnings") or []) + [
                    f"DISPATCH_PREPARATION_FAILED:{type(exc).__name__}"])

    def _new_record(self, task_id: str, prompt: str, cwd: str,
                    assertions: list[ArtifactAssertion], policy: DispatchPolicy,
                    title: str, idempotency_key: str, metadata: dict | None,
                    risks: list[str], *, queue_status: str, priority: int,
                    digest: str, project_id: str | None,
                    workspace_id: str | None = None,
                    original_task: str | None = None,
                    context_version: int | None = None,
                    context_entry_refs: list[str] | None = None) -> dict:
        return {
            "task_id": task_id, "title": title or prompt[:60], "prompt": prompt,
            "cwd": cwd, "mode": policy.mode, "isolation": policy.isolation,
            "idempotency_key": idempotency_key, "policy": policy.to_dict(),
            "assertions": [a.to_dict() for a in assertions], "baseline": None,
            "state": TaskState.QUEUED.value, "verdict": Verdict.PENDING.value,
            "warnings": risks,
            "metadata": {**(metadata or {}), "adapter": self.adapter.name},
            "adapter": self.adapter.name, "queue_status": queue_status,
            "priority": priority, "task_digest": digest,
            "project_id": project_id,
            # Project-centric provenance (M1). ``prompt`` is the frozen effective
            # prompt; ``original_task`` is what the caller actually asked for.
            "workspace_id": workspace_id,
            "original_task": original_task,
            "effective_prompt": prompt if original_task is not None else None,
            "context_version": context_version,
            "context_entry_refs": list(context_entry_refs or []),
        }

    @staticmethod
    def _digest_scope(record: dict) -> dict:
        """Execution facts that must be bound into the approval digest.

        Empty for a pre-M1 task, so its digest is byte-identical to before. For a
        project task it pins the workspace and the context version: re-pointing the
        task at another workspace, or re-injecting a different context, changes the
        digest and therefore invalidates any approval granted for the old content.
        """
        scope: dict[str, Any] = {}
        if record.get("workspace_id"):
            scope["workspace_id"] = record["workspace_id"]
        if record.get("context_version") is not None:
            scope["context_version"] = record["context_version"]
        return scope

    @staticmethod
    def _project_policy(project: dict | None) -> DispatchPolicy | None:
        raw = (project or {}).get("policy") or {}
        return DispatchPolicy(**raw) if raw else None

    def _matching_approval(self, policy: DispatchPolicy, digest: str, cwd: str,
                           *, required: bool) -> dict | None:
        if not policy.requires_approval():
            return None
        approval = self.store.valid_approval_by_digest(digest)
        if approval or not required:
            return approval
        self.store.append_event("", "approval_required", {
            "digest": digest, "risk": policy.effective_risk(), "cwd": str(cwd)})
        raise ApprovalRequired(
            f"risk={policy.effective_risk()}: dispatch requires a human approval whose "
            f"digest matches this exact task ({digest}). Call approve(digest=...) first; "
            "changing the prompt/policy/assertions invalidates the approval.")

    def _dispatch_record(self, record: dict, *, approval: dict | None,
                         timeout_s: int) -> dict:
        """Perform one Worker dispatch with a pre-POST approval reservation."""
        task_id = record["task_id"]
        canonical_cwd, project = self.resolve_project_for_cwd(
            record["cwd"], record.get("project_id"))
        if canonical_cwd != record["cwd"]:
            record = self.store.update(task_id, cwd=canonical_cwd)
        if project:
            self.store.append_event(task_id, "project_boundary_checked", {
                "project_id": project["id"], "root": project["root"],
                "cwd": canonical_cwd})
        policy = DispatchPolicy(**(record.get("policy") or {}))
        assertions = [ArtifactAssertion(**a) for a in record.get("assertions") or []]
        # Recompute the digest from what is *actually* about to run, including the
        # project-centric scope (workspace + context version). If the stored digest
        # no longer matches, the content was edited after approval: the recomputed
        # digest will not match the old approval, so the approval cannot be reused.
        scope = self._digest_scope(record)
        digest = task_digest(record["prompt"], policy, assertions, record["cwd"],
                             extra_scope=scope)
        stored_digest = record.get("task_digest")
        if stored_digest and stored_digest != digest:
            self.store.append_event(task_id, "dispatch_content_changed", {
                "stored_digest": stored_digest, "actual_digest": digest,
                "scope": scope,
                "note": "content or execution scope changed after approval; "
                        "the previous approval no longer applies"})
            self.store.update(task_id, task_digest=digest)
            record = self.store.get(task_id) or record
            approval = None
        if policy.requires_approval() and not approval:
            approval = self._matching_approval(policy, digest, record["cwd"], required=True)

        baseline = capture_baseline(record["cwd"], assertions)
        record = self.store.update(task_id, baseline=baseline,
                                   queue_status="dispatching",
                                   state=TaskState.QUEUED.value)

        # The attempt and approval reservation exist durably before the Worker
        # POST. One approved digest can therefore authorize at most one attempt,
        # even when several runtimes race.
        attempt = self.store.add_attempt(task_id, None, reason="dispatch")
        attempt_id = attempt["id"]
        if policy.requires_approval():
            approval = self.store.reserve_approval(digest, attempt_id)
            if not approval:
                # This attempt never dispatched anything: close it with an
                # explicit terminal state so it is not mistaken for execution
                # history when retry statistics are computed later (review P1).
                self.store.close_attempt(attempt_id, "blocked")
                self.store.update(
                    task_id, queue_status="needs_approval",
                    dispatch_claim_owner=None, dispatch_claimed_at=None,
                    warnings=list(record.get("warnings") or []) + [
                        "APPROVAL_RACE_LOST: no unreserved approval matches this digest"])
                self.store.append_event(task_id, "approval_reservation_failed", {
                    "digest": digest, "attempt_id": attempt_id,
                    "attempt_state": "blocked"})
                raise ApprovalRequired(
                    "another attempt reserved this approval, or no approved digest remains; "
                    "no Worker call was made")

        record = self.store.update(
            task_id,
            metadata={**(record.get("metadata") or {}),
                      "task_digest": digest,
                      "dispatch_attempt_id": attempt_id,
                      "approval_id": (approval or {}).get("id")})
        req = DispatchRequest(
            prompt=record["prompt"], cwd=record["cwd"], task_id=task_id,
            title=record.get("title") or "", mode=policy.mode,
            isolation=policy.isolation, assertions=assertions,
            idempotency_key=record.get("idempotency_key") or "",
            timeout_s=timeout_s,
            metadata={**(record.get("metadata") or {}),
                      "permissionMode": policy.permission_mode},
        )
        self.store.append_event(task_id, "dispatch_requested", {
            "policy": policy.to_dict(), "cwd": record["cwd"],
            "attempt_id": attempt_id, "approval_id": (approval or {}).get("id")})
        try:
            result = self.adapter.dispatch(req)
        except Exception as exc:  # noqa: BLE001 - POST outcome may be ambiguous
            self.store.append_event(task_id, "dispatch_unknown", {
                "exception": f"{type(exc).__name__}: {exc}",
                "approval_frozen": bool(approval), "attempt_id": attempt_id})
            return self.store.update(
                task_id, state=TaskState.DISPATCH_UNKNOWN.value,
                verdict=Verdict.PENDING.value, queue_status="needs_human",
                warnings=list(record.get("warnings") or []) + [
                    f"DISPATCH_UNKNOWN: adapter raised {type(exc).__name__}; no blind retry",
                    *( ["APPROVAL_FROZEN: reservation kept until dispatch is resolved"]
                       if approval else [])],
                dispatch_claim_owner=None, dispatch_claimed_at=None)

        patch: dict[str, Any] = {
            "warnings": list(record.get("warnings") or []) + list(result.warnings),
            "metadata": {**(record.get("metadata") or {}),
                         "adapter": self.adapter.name, "dispatch_raw": result.raw,
                         "task_digest": digest,
                         "approval_id": (approval or {}).get("id")},
            "dispatch_claim_owner": None, "dispatch_claimed_at": None,
        }
        if result.uncertain:
            patch.update(state=TaskState.DISPATCH_UNKNOWN.value,
                         verdict=Verdict.PENDING.value, queue_status="needs_human")
            patch["warnings"] += [
                "DISPATCH_UNKNOWN: no blind retry. Call resolve_dispatch(task_id) first; "
                "it either binds the existing job or proves none was created.",
                *( ["APPROVAL_FROZEN: reservation kept until dispatch is resolved"]
                   if approval else [])]
            self.store.mark_attempt(attempt_id, "dispatch_unknown")
            self.store.append_event(task_id, "dispatch_unknown", {
                **result.raw, "approval_frozen": bool(approval),
                "attempt_id": attempt_id})
            return self.store.update(task_id, **patch)

        if not result.accepted or not result.worker_job_id:
            released = (self.store.release_reservation(
                approval["id"], attempt_id) if approval else False)
            self.store.close_attempt(attempt_id, "aborted_before_dispatch")
            patch.update(state=TaskState.UNKNOWN.value,
                         verdict=Verdict.INCONCLUSIVE.value, queue_status="needs_human")
            self.store.append_event(task_id, "dispatch_failed", {
                **result.raw, "approval_released": released,
                "attempt_id": attempt_id, "attempt_state": "aborted_before_dispatch"})
            saved = self.store.update(task_id, **patch)
            self._sync_project_context(saved)
            return saved

        self.store.mark_attempt(attempt_id, "dispatched")
        self.store.append_event(task_id, "dispatch_accepted", {
            "worker_job_id": result.worker_job_id,
            "http": result.raw.get("dispatch_http")})
        out = self.store.update(
            task_id, worker_job_id=result.worker_job_id,
            state=TaskState.STARTING.value, queue_status="running", **patch)
        if approval:
            consumed = self.store.consume_reserved(
                approval["id"], attempt_id, result.worker_job_id)
            self.store.append_event(task_id, "approval_consumed", {
                "approval_id": approval["id"], "ok": consumed,
                "attempt_id": attempt_id, "consumed_by": result.worker_job_id})
            if not consumed:
                out = self.store.update(
                    task_id, warnings=list(out.get("warnings") or []) + [
                        "APPROVAL_CONSUMPTION_MISMATCH: reservation disappeared; audit attempt"])
        try:
            snap = self.adapter.get_task(result.worker_job_id)
        except Exception as exc:  # noqa: BLE001 - accepted job remains bound
            self.store.append_event(task_id, "observation_failed", {
                "phase": "post_dispatch", "error": f"{type(exc).__name__}: {exc}"})
            return self.store.update(
                task_id,
                warnings=list(out.get("warnings") or []) + [
                    f"POST_DISPATCH_OBSERVATION_FAILED:{type(exc).__name__}"],
                queue_status="running")
        return self.store.update(task_id, **self._snapshot_patch(task_id, snap))

    # ------------------------------------------------------------------ #
    # approval gating
    # ------------------------------------------------------------------ #
    def request_approval_for(self, prompt: str, cwd: str, *,
                             assertions: list[ArtifactAssertion] | None = None,
                             policy: DispatchPolicy | None = None,
                             note: str = "",
                             project_id: str | None = None,
                             digest_scope: dict | None = None) -> dict:
        """Create a pending approval for the exact content that would be dispatched.

        ``digest_scope`` must match what :meth:`enqueue` / :meth:`dispatch` will
        bind, otherwise the approval will not match the task it was meant for.
        """
        self.store.assert_writable("request_approval")
        assertions = assertions or []
        cwd, project = self.resolve_project_for_cwd(cwd, project_id)
        policy = policy or self._project_policy(project) or self.policy
        policy.validate()
        digest = task_digest(prompt, policy, assertions, cwd,
                             extra_scope=digest_scope)
        scope = {"risk": policy.effective_risk(), "cwd": str(Path(cwd).resolve()),
                 "project_id": (project or {}).get("id"),
                 "policy": policy.to_dict(),
                 "assertions": [a.to_dict() for a in assertions]}
        if digest_scope:
            scope["execution_scope"] = dict(digest_scope)
        rec = self.store.request_approval("", digest, scope, note=note)
        return {"approval_id": rec["id"], "digest": digest, "scope": scope,
                "status": "pending"}

    def _decide(self, digest: str, decision: str, note: str,
                approval_id: int | None) -> dict:
        """Decide only a matching pending approval; spent/rejected rows stay final."""
        self.store.assert_writable(decision)
        if approval_id is not None:
            row = self.store.approval_by_id(approval_id)
            if not row:
                raise KeyError(f"no approval #{approval_id}")
            if row.get("digest") != digest:
                raise ValueError(
                    f"approval #{approval_id} is bound to another content digest")
            if row.get("consumed_at"):
                raise ValueError(f"approval #{approval_id} is already consumed")
            if row.get("decision") != "pending":
                raise ValueError(
                    f"approval #{approval_id} is already '{row.get('decision')}'")
            out = self.store.decide_approval(
                approval_id, decision, note, expect_digest=digest)
        else:
            pending = [a for a in self.store.all_approvals()
                       if a.get("digest") == digest and a.get("decision") == "pending"
                       and not a.get("consumed_at")]
            if not pending:
                raise KeyError(f"no pending approval for digest {digest}")
            out = self.store.decide_approval(
                pending[-1]["id"], decision, note, expect_digest=digest)
        if not out:
            raise ValueError(f"approval for digest {digest} is no longer pending")
        return out

    def approve(self, digest: str, note: str = "", approval_id: int | None = None) -> dict:
        """Approve exact content and release matching queued work."""
        row = self._decide(digest, "approved", note, approval_id)
        released = self.store.release_approved_queue(digest)
        self.store.append_event("", "approval_granted", {
            "digest": digest, "id": row.get("id"),
            "queued_items_released": released})
        out = dict(row)
        out["queued_items_released"] = released
        return out

    def deny(self, digest: str, note: str = "", approval_id: int | None = None) -> dict:
        row = self._decide(digest, "rejected", note, approval_id)
        self.store.append_event("", "approval_denied", {
            "digest": digest, "id": row.get("id")})
        return row

    def dispatch_approved(self, prompt: str, cwd: str, *, assertions=None,
                          policy: DispatchPolicy | None = None, title: str = "",
                          idempotency_key: str = "", timeout_s: int = 900,
                          metadata: dict | None = None, priority: int = 0,
                          project_id: str | None = None,
                          approved_by: str = "harness") -> dict:
        """Auditable test helper: request, approve, then dispatch exact content."""
        assertions = assertions or []
        req = self.request_approval_for(
            prompt, cwd, assertions=assertions, policy=policy,
            project_id=project_id, note=f"approved_by={approved_by}")
        self.approve(req["digest"], note=f"approved_by={approved_by}",
                     approval_id=req["approval_id"])
        return self.dispatch(
            prompt, cwd, assertions=assertions, policy=policy, title=title,
            idempotency_key=idempotency_key, timeout_s=timeout_s,
            metadata=metadata, priority=priority, project_id=project_id)

    # ------------------------------------------------------------------ #
    def resolve_dispatch(self, task_id: str, confirm_absent: bool = False) -> dict:
        """Try to bind a `dispatch_unknown` work item to a concrete worker job.

        Honesty rule (review P0-2): a miss only proves "not visible in this query
        window with this matcher" — the global job list is ~30s slow and SSE has no
        replay. So a miss keeps the item in `dispatch_unknown`; it never becomes
        "safe to re-dispatch" automatically. Only an explicit human/operator
        confirmation (`confirm_absent=True`) closes the uncertainty, and that
        decision is recorded as an event.
        """
        rec = self.store.get(task_id)
        if not rec:
            raise KeyError(f"no work item {task_id}")
        if rec.get("state") != TaskState.DISPATCH_UNKNOWN.value:
            return rec

        find = getattr(self.adapter, "find_job", None)
        job_id = None
        if callable(find):
            job_id = find(rec["prompt"], rec["cwd"], since=rec.get("created_at") or 0)

        if job_id:
            # Persist the recovered handle before observation, just like the
            # normal post-dispatch path. A query failure must not lose the job.
            bound = self.store.update(
                task_id, worker_job_id=job_id,
                state=TaskState.STARTING.value, queue_status="running")
            approval_id, attempt_id = self._reserved_approval_for(bound)
            consumed = None
            if approval_id and attempt_id:
                consumed = self.store.consume_reserved(approval_id, attempt_id, job_id)
            self.store.append_event(task_id, "dispatch_resolved",
                                    {"worker_job_id": job_id, "note": "job existed",
                                     "approval_consumed": consumed})
            try:
                snap = self.adapter.get_task(job_id)
            except Exception as exc:  # noqa: BLE001
                self.store.append_event(task_id, "observation_failed", {
                    "phase": "dispatch_resolution",
                    "error": f"{type(exc).__name__}: {exc}"})
                return self.store.update(
                    task_id, warnings=list(bound.get("warnings") or []) + [
                        f"POST_RESOLUTION_OBSERVATION_FAILED:{type(exc).__name__}"])
            saved = self.store.update(task_id, **self._snapshot_patch(task_id, snap))
            self._sync_project_context(saved)
            return saved

        if not confirm_absent:
            self.store.append_event(task_id, "dispatch_unresolved",
                                    {"note": "not visible in the query window; still uncertain"})
            return self.store.update(
                task_id,
                warnings=list(rec.get("warnings") or []) +
                ["DISPATCH_STILL_UNKNOWN: 未发现不等于未创建（列表有 ~30s 延迟、无事件重放）；"
                 "禁止自动重派，需人工 confirm_absent"])

        approval_id, attempt_id = self._reserved_approval_for(rec)
        released = bool(
            approval_id and attempt_id
            and self.store.release_reservation(approval_id, attempt_id))
        self.store.append_event(task_id, "dispatch_absent_confirmed", {
            "by": "operator", "note": "human confirmed no job exists",
            "approval_released": released})
        saved = self.store.update(
            task_id, state=TaskState.UNKNOWN.value, verdict=Verdict.INCONCLUSIVE.value,
            queue_status="needs_human",
            warnings=list(rec.get("warnings") or []) +
            ["DISPATCH_ABSENT_CONFIRMED_BY_OPERATOR: 人工确认未创建，允许新 attempt"])
        self._sync_project_context(saved)
        return saved

    def _reserved_approval_for(self, record: dict) -> tuple[int | None, str | None]:
        """Find reservation metadata, including ledgers written before metadata binding."""
        attempt_id = record.get("current_attempt_id")
        approval_id = (record.get("metadata") or {}).get("approval_id")
        if not approval_id and attempt_id:
            reserved = next(
                (row for row in self.store.all_approvals()
                 if row.get("reserved_by") == attempt_id and not row.get("consumed_at")),
                None)
            approval_id = (reserved or {}).get("id")
        return approval_id, attempt_id

    # ------------------------------------------------------------------ #
    # M2 project context update loop
    # ------------------------------------------------------------------ #
    def update_project_context(self, task_id: str) -> dict:
        """Explicitly replay the deterministic M2 update for one task."""
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"no work item {task_id}")
        return self._sync_project_context(task)

    def reconcile_project_context_updates(self, *, limit: int = 100) -> dict:
        """Retry terminal tasks left between verdict commit and M2 receipt commit."""
        return self.context_updates.reconcile_pending(limit=limit)

    def _sync_project_context(self, task: dict) -> dict:
        if not task.get("project_id"):
            return {"task_id": task.get("task_id"), "status": "skipped",
                    "reason": "task is not bound to a project"}
        if task.get("verdict") not in {
                Verdict.VERIFIED.value, Verdict.REJECTED.value,
                Verdict.INCONCLUSIVE.value}:
            return {"task_id": task.get("task_id"), "status": "skipped",
                    "reason": "acceptance verdict is not terminal"}
        try:
            result = self.context_updates.process_task(task["task_id"])
        except Exception as exc:  # noqa: BLE001 - acceptance must remain durable
            self.store.append_event(task["task_id"], "project_context_update_failed", {
                "error": f"{type(exc).__name__}: {exc}",
                "note": "task verdict kept; replay update_project_context(task_id)"})
            return {"task_id": task["task_id"], "status": "failed",
                    "error": type(exc).__name__}
        self.store.append_event(task["task_id"], "project_context_updated", {
            "status": result.get("status"),
            "attempt_id": result.get("attempt_id"),
            "evidence_ref": result.get("evidence_ref"),
            "updates": [
                {"status": update.get("status"),
                 "entry_id": update.get("entry_id"),
                 "idempotency_key": update.get("idempotency_key")}
                for update in result.get("updates") or []],
        })
        return result

    # ------------------------------------------------------------------ #
    # refresh: two independent paths
    # ------------------------------------------------------------------ #
    def refresh_known_jobs(self, timeout_s: float = 20.0) -> dict:
        """High-frequency path: one query per ledger job, partial success allowed.

        A failed/timeout query is an *observation anomaly*, never evidence that the
        task died (`lost` needs independent process/state evidence).
        """
        updated, anomalies = [], []
        terminal_states = {state.value for state in TaskState if state.terminal}
        for task in self.store.all():
            if not self._belongs_to_adapter(task):
                continue
            jid = task.get("worker_job_id")
            if not jid:
                continue
            # Once a terminal observation has been persisted, repeatedly polling
            # it adds load but cannot improve the acceptance verdict. A manual
            # operator refresh can be added separately if a worker violates
            # terminal-state stability.
            if task.get("state") in terminal_states:
                continue
            try:
                try:
                    snap = self.adapter.get_task(jid, timeout_s=timeout_s)
                except TypeError:            # adapter without a timeout parameter
                    snap = self.adapter.get_task(jid)
            except Exception as exc:  # noqa: BLE001
                anomalies.append(self._note_observation_failure(task, exc))
                continue
            patch = self._snapshot_patch(task["task_id"], snap)
            if patch["state"] == TaskState.LOST.value:
                patch = self._confirm_lost(task, snap, patch)
            saved = self.store.update(task["task_id"], **patch)
            context_update = self._sync_project_context(saved)
            item = {"task_id": task["task_id"], "worker_job_id": jid,
                    "state": patch["state"], "verdict": patch["verdict"]}
            if context_update.get("status") != "skipped":
                item["context_update"] = context_update
            updated.append(item)
        context_updates = self.reconcile_project_context_updates()
        return {"updated": updated, "anomalies": anomalies,
                "ok": len(anomalies) == 0, "checked": len(updated) + len(anomalies),
                "context_updates": context_updates}

    def discover_orphan_jobs(self) -> dict:
        """Low-frequency full scan. Failure here must not disturb known tasks."""
        try:
            rec = self.adapter.reconcile()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "note": "full scan failed; known-task state left untouched"}
        worker_ids = {j.get("id") for j in rec["jobs"]}
        known = {t.get("worker_job_id") for t in self.store.all()
                 if t.get("worker_job_id") and self._belongs_to_adapter(t)}
        orphans = sorted(worker_ids - known)
        vanished = []
        terminal_states = {state.value for state in TaskState if state.terminal}
        for task in self.store.all():
            if not self._belongs_to_adapter(task):
                continue
            jid = task.get("worker_job_id")
            if not jid:
                continue
            if jid in worker_ids:
                # Misses must be consecutive. A single successful scan resets
                # the counter instead of letting old failures accumulate until
                # a healthy task is incorrectly declared lost.
                metadata = task.get("metadata") or {}
                if metadata.get("scan_misses"):
                    self.store.update(task["task_id"],
                                      metadata={**metadata, "scan_misses": 0})
                continue
            # A worker may garbage-collect terminal jobs. Their accepted terminal
            # observation remains authoritative; disappearance is not a new lost
            # running task.
            if task.get("state") in terminal_states:
                continue
            misses = int((task.get("metadata") or {}).get("scan_misses", 0)) + 1
            self.store.update(task["task_id"],
                              metadata={**(task.get("metadata") or {}), "scan_misses": misses})
            if misses < 2:
                vanished.append({"task_id": task["task_id"], "job": jid, "misses": misses,
                                 "action": "pending second scan"})
                continue
            # Two consecutive misses are NOT proof of death on their own: the
            # process may still be executing. Only independent liveness evidence
            # (a dead pid) may conclude `lost`; otherwise the task becomes
            # suspected_lost and keeps occupying its concurrency slot (review P0).
            patch, outcome = self._assess_lost(
                task, jid, "job id absent from two consecutive full scans")
            vanished.append({"task_id": task["task_id"], "job": jid, "misses": misses,
                             "action": outcome})
        return {"ok": True, "total": len(rec["jobs"]), "orphans": orphans[:20],
                "orphan_count": len(orphans), "vanished": vanished}

    def refresh(self, timeout_s: float = 20.0) -> dict:
        """Known jobs first (cheap, partial), then a best-effort orphan scan."""
        known = self.refresh_known_jobs(timeout_s=timeout_s)
        scan = self.discover_orphan_jobs()
        return {"known_jobs": known, "orphan_scan": scan, "tasks": self.summary()}

    def _note_observation_failure(self, task: dict, exc: Exception) -> dict:
        count = int((task.get("metadata") or {}).get("observe_failures", 0)) + 1
        self.store.update(task["task_id"],
                          metadata={**(task.get("metadata") or {}),
                                    "observe_failures": count,
                                    "last_observe_error": f"{type(exc).__name__}"})
        self.store.append_event(task["task_id"], "observation_failed",
                                {"error": f"{type(exc).__name__}: {exc}", "count": count})
        return {"task_id": task["task_id"], "error": f"{type(exc).__name__}",
                "count": count, "state_unchanged": True}

    def _confirm_lost(self, task: dict, snap: TaskSnapshot, patch: dict) -> dict:
        """Adapter says lost: demand independent evidence before accepting it.

        Review P0: a missing pid or an unavailable probe is *uncertainty*, not
        proof of death. In that case the item keeps its previous state and stays
        `suspected_lost` — capacity is not released and no retry is authorised.
        """
        alive = self._process_alive(snap.pid)
        if alive is False:
            return patch
        # cannot prove death: keep the previous state, record the doubt
        prev = task.get("state") or TaskState.UNKNOWN.value
        warning = (f"LOST_UNCONFIRMED: adapter reports lost but pid={snap.pid} "
                   f"alive={alive}; kept as suspected_lost, capacity not released")
        self.store.update(task["task_id"],
                          metadata={**(task.get("metadata") or {}),
                                    "suspected_lost": True},
                          warnings=list(task.get("warnings") or []) + [warning])
        self.store.append_event(task["task_id"], "suspected_lost",
                                {"pid": snap.pid, "alive": alive,
                                 "source": "adapter_report"})
        return {**patch, "state": prev,
                "verdict": task.get("verdict") or Verdict.PENDING.value,
                "queue_status": "running",
                "warnings": list(patch.get("warnings") or []) + [warning]}

    def _process_alive(self, pid: int | None) -> bool | None:
        """Independent liveness evidence (never from a failed query).

        Three-state contract (review P0, T27):
          True  -> the OS confirms the process is alive.
          False -> reliable evidence the process is dead.
          None  -> cannot tell (no pid, no probe, probe raised, probe returned
                   None). ``None`` must never be coerced to ``False``: doing so
                   turns "unknown" into "proven dead", which releases the
                   concurrency slot and authorises a blind retry.
        """
        probe = getattr(self.adapter, "pid_alive", None)
        if not callable(probe) or not pid:
            return None
        try:
            answer = probe(pid)
        except Exception:  # noqa: BLE001
            return None
        if answer is None:
            return None
        return bool(answer)

    def _assess_lost(self, task: dict, jid: str | None, reason: str,
                     *, snap_pid: int | None = None) -> tuple[dict, str]:
        """Unified lost verdict (review P0): absence is doubt, death is proven.

        Returns ``(patch, outcome)`` where outcome is either ``lost`` (independent
        evidence: the process is dead) or ``suspected_lost`` (no evidence: state
        kept, concurrency slot retained, human resolution required).
        """
        pid = snap_pid if snap_pid is not None else task.get("pid")
        alive = self._process_alive(pid)
        if alive is False:
            patch = self._mark_lost(task, jid, f"{reason}; pid={pid} confirmed dead")
            return patch, "lost"
        metadata = {**(task.get("metadata") or {}),
                    "suspected_lost": True, "suspected_lost_reason": reason}
        self.store.update(
            task["task_id"], metadata=metadata,
            warnings=list(task.get("warnings") or []) + [
                f"SUSPECTED_LOST: {reason}; pid={pid} alive={alive}; state kept, "
                "capacity NOT released, human resolution required"])
        self.store.append_event(task["task_id"], "suspected_lost",
                                {"worker_job_id": jid, "reason": reason,
                                 "pid": pid, "alive": alive})
        return {}, "suspected_lost"

    def _mark_lost(self, task: dict, jid: str, reason: str) -> dict:
        patch = {"state": TaskState.LOST.value, "verdict": Verdict.REJECTED.value,
                 "queue_status": "complete",
                 "warnings": list(task.get("warnings") or []) + [f"LOST: {reason}"]}
        saved = self.store.update(task["task_id"], **patch)
        self.store.append_event(task["task_id"], "lost", {"worker_job_id": jid, "reason": reason})
        self._sync_project_context(saved)
        return patch

    def recover_inflight(self, *, lease_ttl_s: float = DISPATCH_CLAIM_LEASE_S) -> dict:
        """Close the crash window between "intent persisted" and "job id stored".

        Any work item still `queued` with no worker job id means we died after
        writing intent (possibly after the POST). Treat it exactly like
        dispatch_unknown: reject blind re-dispatch, require resolve_dispatch().

        Review P1 (multi-runtime race): an item claimed by another runtime less
        than `lease_ttl_s` ago may simply be mid-POST right now. Such claims are
        skipped; only claims that are provably expired (or unowned) are recovered,
        so a starting runtime can no longer flip another instance's live dispatch
        to `dispatch_unknown`.
        """
        recovered, skipped = [], []
        now = time.time()
        for task in self.store.all():
            if not self._belongs_to_adapter(task):
                continue
            if task.get("worker_job_id") or task.get("state") != TaskState.QUEUED.value:
                continue
            # Legitimately enqueued work has not crossed the dispatch boundary.
            # Only a claimed/immediate intent with no job handle is ambiguous.
            if task.get("queue_status") in ("ready", "needs_approval"):
                continue
            owner = task.get("dispatch_claim_owner")
            claimed_at = float(task.get("dispatch_claimed_at") or 0)
            if owner and (now - claimed_at) < lease_ttl_s:
                skipped.append({"task_id": task["task_id"], "owner": owner,
                                "claim_age_s": round(now - claimed_at, 1)})
                continue
            self.store.update(
                task["task_id"], state=TaskState.DISPATCH_UNKNOWN.value,
                queue_status="needs_human",
                dispatch_claim_owner=None, dispatch_claimed_at=None,
                warnings=list(task.get("warnings") or []) +
                ["DISPATCH_INFLIGHT_AT_RESTART: intent was persisted but no job id; "
                 "run resolve_dispatch() before any new attempt"])
            self.store.append_event(task["task_id"], "recovered_inflight",
                                    {"owner": owner,
                                     "claim_age_s": round(now - claimed_at, 1)})
            recovered.append(task["task_id"])
        return {"recovered": recovered, "count": len(recovered),
                "skipped_active_claims": skipped}

    def _belongs_to_adapter(self, task: dict) -> bool:
        """Route only this Adapter's work; legacy rows without a name stay local."""
        adapter = task.get("adapter") or (task.get("metadata") or {}).get("adapter")
        return not adapter or adapter == self.adapter.name

    def _snapshot_patch(self, task_id: str, snap: TaskSnapshot) -> dict:
        # task_id is passed explicitly: after an adapter/plane restart the adapter
        # has no in-memory job->task map, so `snap.task_id` degrades to the worker
        # job id and assertions/baseline would silently be lost (review P0-1).
        rec = self.store.get(task_id) or {}
        assertions = [ArtifactAssertion(**a) for a in rec.get("assertions") or []]
        baseline = rec.get("baseline") or {}

        # Agent tasks run inside a git worktree: artifacts must be verified there,
        # not in the (possibly stale) main checkout.
        wt = snap.worktree
        in_worktree = bool(wt) and Path(wt).exists()
        eval_cwd = wt if in_worktree else snap.cwd
        eval_baseline = baseline
        if in_worktree and baseline:
            eval_baseline = {**baseline, "cwd": wt, "status": "",
                             "siblings": {}}  # peer worktrees are isolated by construction

        results = check_all(eval_cwd, assertions, eval_baseline) if eval_cwd else []
        violations: list[dict] = []
        if snap.cwd:
            declared = [a.target for a in assertions if a.kind in ("file_exists", "file_contains")]
            violations += side_effects(snap.cwd, baseline, allowed_paths=declared,
                                       allowed_prefixes=(".codebuddy/",))
        verdict = verdict_for(snap.state, snap.state is not TaskState.UNKNOWN, results, violations)

        patch = {
            "state": snap.state.value,
            "verdict": verdict.value,
            "queue_status": self._queue_status_for(snap.state),
            "session_id": snap.session_id,
            "pid": snap.pid,
            "worktree": snap.worktree,
            "branch": snap.branch,
            "head_commit": snap.commit,
            "detail": snap.detail,
            "started_at": snap.started_at,
            "finished_at": snap.finished_at,
            "artifacts": self.adapter.collect_artifacts(snap.worker_job_id or snap.task_id),
            "assertion_results": results,
            "worker_metadata": snap.metadata,
        }
        if violations:
            patch["warnings"] = list(rec.get("warnings") or []) + [
                "SIDE_EFFECT:" + v["type"] + ":" + str(v.get("repo") or v.get("cwd"))
                for v in violations]
        return patch

    @staticmethod
    def _queue_status_for(state: TaskState) -> str:
        if state.terminal:
            return "complete"
        if state in (TaskState.WAITING_FOR_APPROVAL,
                     TaskState.UNKNOWN, TaskState.DISPATCH_UNKNOWN):
            return "needs_human"
        return "running"

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        jid = rec.get("worker_job_id")
        if not jid:
            raise KeyError(f"no worker job for {task_id}")
        snap = self.adapter.cancel(jid)
        self.store.append_event(task_id, "cancel_requested", {"worker_job_id": jid})
        saved = self.store.update(task_id, **self._snapshot_patch(task_id, snap))
        self._sync_project_context(saved)
        return saved

    def resume(self, task_id: str) -> dict:
        """Re-attach by conversation. A new worker job becomes a new attempt."""
        self.store.assert_writable("resume")
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.resume(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "resume", {"from_job": old_job, "to_job": new_job})
        self.store.add_attempt(task_id, new_job, reason=f"resume from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job,
                                 **self._snapshot_patch(task_id, snap))

    def respawn(self, task_id: str) -> dict:
        self.store.assert_writable("respawn")
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.respawn(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "respawn", {"from_job": old_job, "to_job": new_job})
        if new_job and new_job != old_job:
            self.store.add_attempt(task_id, new_job, reason=f"respawn from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job or old_job,
                                 **self._snapshot_patch(task_id, snap))

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        """Record a human decision in the ledger.

        The worker cannot receive a decision for a background job (see
        evidence/permissions/T09-permission-denied.json), so approval is modelled
        as "stop this attempt, record the decision, run a new attempt later".
        """
        self.store.assert_writable("request_approval")
        rec = self.store.get(task_id) or {}
        self.store.record_approval(task_id, decision, note,
                                   attempt_id=rec.get("current_attempt_id"))
        delivered = False
        reason = None
        try:
            self.adapter.request_approval(rec.get("worker_job_id") or task_id, decision, note)
            delivered = True
        except UnsupportedOperation as exc:
            reason = str(exc)
        self.store.append_event(task_id, "approval_recorded",
                                {"decision": decision, "delivered_to_worker": delivered})
        return {"recorded": True, "delivered_to_worker": delivered, "reason": reason,
                "next_step": ("stop the current attempt, then dispatch a new attempt with the "
                              "approved scope") if not delivered else "decision delivered"}

    # ------------------------------------------------------------------ #
    def summary(self) -> dict:
        """The Foreman-facing view: one structured call, no per-task window."""
        tasks = self.store.all()
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.get("state", "unknown")] = counts.get(t.get("state", "unknown"), 0) + 1

        waiting = [t for t in tasks if t.get("state") == TaskState.WAITING_FOR_APPROVAL.value]
        rejected = [t for t in tasks if t.get("verdict") == Verdict.REJECTED.value]
        running = [t for t in tasks if t.get("state") in
                   (TaskState.RUNNING.value, TaskState.STARTING.value, TaskState.QUEUED.value)]
        verified = [t for t in tasks if t.get("verdict") == Verdict.VERIFIED.value]
        inconclusive = [t for t in tasks if t.get("verdict") == Verdict.INCONCLUSIVE.value]
        unknown_dispatch = [t for t in tasks
                            if t.get("state") == TaskState.DISPATCH_UNKNOWN.value]
        queued = [t for t in tasks if t.get("queue_status") == "ready"]
        queue_approvals = [t for t in tasks if t.get("queue_status") == "needs_approval"]
        suspected_lost = [t for t in tasks
                          if (t.get("metadata") or {}).get("suspected_lost")]

        next_actions = []
        if unknown_dispatch:
            next_actions.append("派发结果不确定，先 resolve_dispatch: "
                                + ", ".join(t["task_id"] for t in unknown_dispatch))
        if waiting:
            next_actions.append("等待人工审批: " + ", ".join(t["task_id"] for t in waiting))
        if rejected:
            next_actions.append("未通过验收: " + ", ".join(t["task_id"] for t in rejected))
        if inconclusive:
            next_actions.append("验收不确定（缺机读断言）: "
                                + ", ".join(t["task_id"] for t in inconclusive))
        if running:
            next_actions.append(f"{len(running)} 个任务仍在运行，稍后 refresh")
        if queued:
            next_actions.append(f"{len(queued)} 个任务已入队，等待 Runtime 派发")
        if queue_approvals:
            next_actions.append("队列任务等待审批: "
                                + ", ".join(t["task_id"] for t in queue_approvals))
        if suspected_lost:
            next_actions.append("疑似失联待人工裁决（容量未释放）: "
                                + ", ".join(t["task_id"] for t in suspected_lost))

        return {
            "generated_at": time.time(),
            "total": len(tasks),
            "ledger": {"path": str(self.store.path), "degraded": self.store.degraded},
            "counts": {
                "by_state": counts,
                "running": len(running),
                "waiting_for_approval": len(waiting),
                "verified": len(verified),
                "rejected": len(rejected),
                "inconclusive": len(inconclusive),
                "dispatch_unknown": len(unknown_dispatch),
                "queued_ready": len(queued),
                "queued_needs_approval": len(queue_approvals),
                "suspected_lost": len(suspected_lost),
            },
            "needs_human": [t["task_id"] for t in waiting],
            "tasks": [self._task_view(t) for t in tasks],
            "next_actions": next_actions,
        }

    def _task_view(self, t: dict) -> dict:
        attempts = self.store.attempts(t["task_id"])
        return {
            "task_id": t["task_id"],
            "worker_job_id": t.get("worker_job_id"),
            "attempts": [{"attempt_id": a["id"], "worker_job_id": a.get("worker_job_id"),
                          "reason": a.get("reason"), "at": a.get("created_at")}
                         for a in attempts],
            "title": t.get("title"),
            "cwd": t.get("cwd"),
            "mode": t.get("mode"),
            "isolation": t.get("isolation"),
            "adapter": t.get("adapter") or (t.get("metadata") or {}).get("adapter"),
            "project_id": t.get("project_id"),
            "queue_status": t.get("queue_status"),
            "priority": t.get("priority") or 0,
            "worktree": t.get("worktree"),
            "state": t.get("state"),
            "verdict": t.get("verdict"),
            "detail": t.get("detail"),
            "session_id": t.get("session_id"),
            "pid": t.get("pid"),
            "commit": t.get("head_commit"),
            "artifacts": t.get("artifacts") or [],
            "assertions": {r["kind"] + ":" + r["target"]: r["passed"]
                           for r in t.get("assertion_results") or []},
            "warnings": t.get("warnings") or [],
        }
