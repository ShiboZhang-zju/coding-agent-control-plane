"""Minimal control plane facade: dispatch, reconcile, verify, summarize.

Everything the worker does not guarantee lives here:

  * dispatch **policy** (no implicit un-isolated shell execution),
  * idempotency keys and the `dispatch_unknown` state (never blind-retry),
  * a pre-dispatch baseline so acceptance can prove provenance,
  * `work_item -> run_attempt -> worker_job` mapping (resume-safe),
  * the aggregated summary a Foreman can read instead of guessing.
"""
from __future__ import annotations

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
from controlplane.store import TaskStore
from controlplane.verify import capture_baseline, check_all, side_effects, verdict_for


class ControlPlane:
    def __init__(self, adapter, store: TaskStore | None = None,
                 policy: DispatchPolicy | None = None):
        self.adapter = adapter
        self.store = store or TaskStore()
        self.policy = policy or DispatchPolicy()

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def dispatch(self, prompt: str, cwd: str, *, assertions: list[ArtifactAssertion] | None = None,
                 policy: DispatchPolicy | None = None, title: str = "",
                 idempotency_key: str = "", timeout_s: int = 900,
                 metadata: dict | None = None) -> dict:
        """Dispatch with an explicit policy. Raises ValueError on unsafe policy."""
        assertions = assertions or []
        policy = policy or self.policy
        risks = policy.validate()

        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                out = dict(existing)
                out["deduplicated"] = True
                return out

        task_id = self.store.new_task_id()
        baseline = capture_baseline(cwd, assertions)
        req = DispatchRequest(prompt=prompt, cwd=cwd, task_id=task_id, title=title,
                              mode=policy.mode, isolation=policy.isolation,
                              assertions=assertions, idempotency_key=idempotency_key,
                              timeout_s=timeout_s,
                              metadata={**(metadata or {}),
                                        "permissionMode": policy.permission_mode})

        record = {
            "task_id": task_id,
            "title": title or prompt[:60],
            "prompt": prompt,
            "cwd": cwd,
            "mode": policy.mode,
            "isolation": policy.isolation,
            "idempotency_key": idempotency_key,
            "policy": policy.to_dict(),
            "assertions": [a.to_dict() for a in assertions],
            "baseline": baseline,
            "state": TaskState.QUEUED.value,
            "verdict": Verdict.PENDING.value,
            "warnings": risks,
            "metadata": {"adapter": self.adapter.name},
        }
        self.store.create(record)
        self.store.append_event(task_id, "dispatch_requested",
                                {"policy": policy.to_dict(), "cwd": cwd})

        result = self.adapter.dispatch(req)
        patch: dict[str, Any] = {
            "warnings": risks + list(result.warnings),
            "metadata": {"adapter": self.adapter.name, "dispatch_raw": result.raw},
        }

        if result.uncertain:
            # P0: the worker may or may not have created a job. Do not retry.
            patch["state"] = TaskState.DISPATCH_UNKNOWN.value
            patch["verdict"] = Verdict.PENDING.value
            patch["warnings"] = patch["warnings"] + [
                "DISPATCH_UNKNOWN: no blind retry. Call resolve_dispatch(task_id) first; "
                "it either binds the existing job or proves none was created."]
            self.store.append_event(task_id, "dispatch_unknown", result.raw)
            return self.store.update(task_id, **patch)

        if not result.accepted or not result.worker_job_id:
            patch["state"] = TaskState.UNKNOWN.value
            patch["verdict"] = Verdict.INCONCLUSIVE.value
            self.store.append_event(task_id, "dispatch_failed", result.raw)
            return self.store.update(task_id, **patch)

        snap = self.adapter.get_task(result.worker_job_id)
        patch.update(self._snapshot_patch(snap))
        self.store.append_event(task_id, "dispatch_accepted",
                                {"worker_job_id": result.worker_job_id,
                                 "http": result.raw.get("dispatch_http")})
        return self.store.update(task_id, worker_job_id=result.worker_job_id, **patch)

    # ------------------------------------------------------------------ #
    def resolve_dispatch(self, task_id: str) -> dict:
        """Settle a `dispatch_unknown` work item against the worker's truth.

        Binds the job if one exists (matching prompt+cwd+since), otherwise marks
        the work item `unknown` and clears the way for a *new* attempt with the
        same idempotency key.
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
            snap = self.adapter.get_task(job_id)
            self.store.append_event(task_id, "dispatch_resolved",
                                    {"worker_job_id": job_id, "note": "job existed"})
            return self.store.update(task_id, worker_job_id=job_id,
                                     **self._snapshot_patch(snap))

        self.store.append_event(task_id, "dispatch_resolved",
                                {"worker_job_id": None, "note": "no job was created"})
        return self.store.update(
            task_id, state=TaskState.UNKNOWN.value, verdict=Verdict.INCONCLUSIVE.value,
            warnings=list(rec.get("warnings") or []) +
            ["DISPATCH_PROVEN_ABSENT: no job exists; a new attempt may be dispatched"])

    # ------------------------------------------------------------------ #
    def refresh(self) -> dict:
        """Reconcile worker truth into the ledger, then re-verify every task."""
        rec = self.adapter.reconcile()
        worker_ids = {j.get("id") for j in rec["jobs"]}
        for task in self.store.all():
            jid = task.get("worker_job_id")
            if not jid:
                continue
            if jid not in worker_ids:
                self.store.update(task["task_id"], state=TaskState.LOST.value,
                                  verdict=Verdict.REJECTED.value,
                                  warnings=list(task.get("warnings") or []) +
                                  ["worker no longer reports this job (lost)"])
                self.store.append_event(task["task_id"], "lost", {"worker_job_id": jid})
                continue
            snap = self.adapter.get_task(jid)
            self.store.update(task["task_id"], **self._snapshot_patch(snap))
        return {"reconcile": {k: rec[k] for k in ("source", "total", "new", "changed", "lost")},
                "tasks": self.summary()}

    def _snapshot_patch(self, snap: TaskSnapshot) -> dict:
        rec = self.store.get(snap.task_id) or {}
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
            "worker_metadata": snap.metadata.get("codebuddy", {}),
        }
        if violations:
            patch["warnings"] = list(rec.get("warnings") or []) + [
                "SIDE_EFFECT:" + v["type"] + ":" + str(v.get("repo") or v.get("cwd"))
                for v in violations]
        return patch

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
        return self.store.update(task_id, **self._snapshot_patch(snap))

    def resume(self, task_id: str) -> dict:
        """Re-attach by conversation. A new worker job becomes a new attempt."""
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.resume(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "resume", {"from_job": old_job, "to_job": new_job})
        self.store.add_attempt(task_id, new_job, reason=f"resume from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job, **self._snapshot_patch(snap))

    def respawn(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        old_job = rec.get("worker_job_id")
        snap = self.adapter.respawn(old_job or task_id)
        new_job = snap.worker_job_id
        self.store.append_event(task_id, "respawn", {"from_job": old_job, "to_job": new_job})
        if new_job and new_job != old_job:
            self.store.add_attempt(task_id, new_job, reason=f"respawn from {old_job}")
        return self.store.update(task_id, worker_job_id=new_job or old_job,
                                 **self._snapshot_patch(snap))

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        """Record a human decision in the ledger.

        The worker cannot receive a decision for a background job (see
        evidence/permissions/T09-permission-denied.json), so approval is modelled
        as "stop this attempt, record the decision, run a new attempt later".
        """
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
