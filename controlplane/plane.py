"""Minimal control plane facade: dispatch, reconcile, verify, summarize.

The plane owns everything the worker does not guarantee:
  * its own durable task ids and idempotency keys,
  * artifact-based acceptance verdicts,
  * state reconstruction after an outage (`refresh()`),
  * the aggregated view a master agent can read instead of opening 4 windows.
"""
from __future__ import annotations

import time
from typing import Any

from adapters.base import (
    ArtifactAssertion,
    DispatchRequest,
    TaskSnapshot,
    TaskState,
    UnsupportedOperation,
    Verdict,
)
from controlplane.store import TaskStore
from controlplane.verify import check_all, verdict_for


class ControlPlane:
    def __init__(self, adapter, store: TaskStore | None = None):
        self.adapter = adapter
        self.store = store or TaskStore()

    # ------------------------------------------------------------------ #
    def dispatch(self, prompt: str, cwd: str, *, assertions: list[ArtifactAssertion] | None = None,
                 mode: str = "shell", isolation: str = "none", title: str = "",
                 idempotency_key: str = "", timeout_s: int = 900,
                 metadata: dict | None = None) -> dict:
        assertions = assertions or []
        if idempotency_key:
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing:
                existing = dict(existing)
                existing["deduplicated"] = True
                return existing

        task_id = self.store.new_task_id()
        req = DispatchRequest(prompt=prompt, cwd=cwd, task_id=task_id, title=title,
                              mode=mode, isolation=isolation, assertions=assertions,
                              idempotency_key=idempotency_key, timeout_s=timeout_s,
                              metadata=metadata or {})
        record = {
            "task_id": task_id,
            "worker_job_id": None,
            "prompt": prompt,
            "title": title or prompt[:60],
            "cwd": cwd,
            "mode": mode,
            "isolation": isolation,
            "idempotency_key": idempotency_key,
            "assertions": [a.to_dict() for a in assertions],
            "state": TaskState.QUEUED.value,
            "verdict": Verdict.PENDING.value,
            "assertion_results": [],
            "artifacts": [],
            "warnings": [],
            "metadata": {},
        }
        self.store.create(record)

        result = self.adapter.dispatch(req)
        patch: dict[str, Any] = {
            "worker_job_id": result.worker_job_id,
            "warnings": result.warnings,
            "metadata": {"adapter": self.adapter.name, "dispatch_raw": result.raw},
        }
        if not result.accepted or not result.worker_job_id:
            patch["state"] = TaskState.UNKNOWN.value
            patch["verdict"] = Verdict.INCONCLUSIVE.value
        else:
            snap = self.adapter.get_task(result.worker_job_id)
            patch.update(self._snapshot_patch(snap))
        return self.store.update(task_id, **patch)

    # ------------------------------------------------------------------ #
    def refresh(self) -> dict:
        """Reconcile worker truth into the plane store, then re-verify every task."""
        rec = self.adapter.reconcile()
        for task in self.store.all():
            jid = task.get("worker_job_id")
            if not jid:
                continue
            if jid not in [j.get("id") for j in rec["jobs"]]:
                self.store.update(task["task_id"], state=TaskState.LOST.value,
                                  verdict=Verdict.REJECTED.value,
                                  warnings=list(task.get("warnings") or []) +
                                  ["worker no longer reports this job (lost)"])
                continue
            snap = self.adapter.get_task(jid)
            snap.task_id = task["task_id"]   # adapter may not know the plane id after a restart
            self.store.update(task["task_id"], **self._snapshot_patch(snap))
        return {"reconcile": {k: rec[k] for k in ("source", "total", "new", "changed", "lost")},
                "tasks": self.summary()}

    def _snapshot_patch(self, snap: TaskSnapshot) -> dict:
        assertions = [ArtifactAssertion(**a) for a in
                      (self.store.get(snap.task_id) or {}).get("assertions", [])]
        # Isolation matters: an isolated run writes into its own worktree, so the
        # assertions must be evaluated against the worktree root, not the cwd.
        check_root = snap.worktree or snap.cwd
        results = check_all(check_root, assertions) if check_root else []
        verdict = verdict_for(snap.state, snap.state is not TaskState.UNKNOWN, results)
        return {
            "state": snap.state.value,
            "verdict": verdict.value,
            "session_id": snap.session_id,
            "pid": snap.pid,
            "worktree": snap.worktree,
            "branch": snap.branch,
            "commit": snap.commit,
            "detail": snap.detail,
            "started_at": snap.started_at,
            "finished_at": snap.finished_at,
            "artifacts": self.adapter.collect_artifacts(snap.worker_job_id or snap.task_id),
            "verify_root": check_root,
            "assertion_results": results,
            "worker_metadata": snap.metadata.get("codebuddy", {}),
        }

    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        jid = rec.get("worker_job_id")
        if not jid:
            raise KeyError(f"no worker job for {task_id}")
        snap = self.adapter.cancel(jid)
        snap.task_id = task_id
        return self.store.update(task_id, **self._snapshot_patch(snap))

    def resume(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        snap = self.adapter.resume(rec.get("worker_job_id") or task_id)
        snap.task_id = task_id
        return self.store.update(task_id, **self._snapshot_patch(snap))

    def respawn(self, task_id: str) -> dict:
        rec = self.store.get(task_id) or {}
        snap = self.adapter.respawn(rec.get("worker_job_id") or task_id)
        snap.task_id = task_id
        return self.store.update(task_id, **self._snapshot_patch(snap))

    def request_approval(self, task_id: str, decision: str, note: str = "") -> dict:
        rec = self.store.get(task_id) or {}
        try:
            out = self.adapter.request_approval(rec.get("worker_job_id") or task_id, decision, note)
            return {"delivered": True, "adapter": out}
        except UnsupportedOperation as exc:
            return {"delivered": False, "reason": str(exc)}

    # ------------------------------------------------------------------ #
    def summary(self) -> dict:
        """The master-agent view: one structured call, no per-task window."""
        tasks = self.store.all()
        by_state: dict[str, list[str]] = {}
        for t in tasks:
            by_state.setdefault(t.get("state", "unknown"), []).append(t["task_id"])

        waiting = [t for t in tasks if t.get("state") == TaskState.WAITING_FOR_APPROVAL.value]
        failed = [t for t in tasks if t.get("verdict") == Verdict.REJECTED.value]
        running = [t for t in tasks if t.get("state") in (TaskState.RUNNING.value,
                                                          TaskState.STARTING.value,
                                                          TaskState.QUEUED.value)]
        verified = [t for t in tasks if t.get("verdict") == Verdict.VERIFIED.value]
        inconclusive = [t for t in tasks if t.get("verdict") == Verdict.INCONCLUSIVE.value]

        next_actions = []
        if waiting:
            next_actions.append(
                f"{len(waiting)} 个任务在等待人工审批: " + ", ".join(t["task_id"] for t in waiting))
        if failed:
            next_actions.append(
                f"{len(failed)} 个任务未通过验收: " + ", ".join(t["task_id"] for t in failed))
        if inconclusive:
            next_actions.append(
                f"{len(inconclusive)} 个任务验收不确定（缺少机读断言）: "
                + ", ".join(t["task_id"] for t in inconclusive))
        if running:
            next_actions.append(f"{len(running)} 个任务仍在运行，稍后 refresh")

        return {
            "generated_at": time.time(),
            "total": len(tasks),
            "counts": {
                "by_state": {k: len(v) for k, v in by_state.items()},
                "running": len(running),
                "waiting_for_approval": len(waiting),
                "verified": len(verified),
                "rejected": len(failed),
                "inconclusive": len(inconclusive),
            },
            "needs_human": [t["task_id"] for t in waiting],
            "tasks": [self._task_view(t) for t in tasks],
            "next_actions": next_actions,
        }

    def _task_view(self, t: dict) -> dict:
        return {
            "task_id": t["task_id"],
            "worker_job_id": t.get("worker_job_id"),
            "title": t.get("title"),
            "cwd": t.get("cwd"),
            "state": t.get("state"),
            "verdict": t.get("verdict"),
            "detail": t.get("detail"),
            "session_id": t.get("session_id"),
            "pid": t.get("pid"),
            "artifacts": t.get("artifacts") or [],
            "assertions": {r["kind"] + ":" + r["target"]: r["passed"]
                           for r in t.get("assertion_results") or []},
            "warnings": t.get("warnings") or [],
        }
