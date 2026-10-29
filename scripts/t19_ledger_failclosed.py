"""T19: a degraded ledger must refuse to dispatch, and in-flight intent must recover.

A. integrity_check failure -> store.degraded=True -> dispatch/resume/respawn raise
   LedgerDegraded, while queries still work (fail closed, not fail silent).
B. a work item persisted as `queued` without a worker job id (crash right after the
   POST) is recovered as `dispatch_unknown`, not re-dispatched.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import LedgerDegraded, TaskStore  # noqa: E402


class StubAdapter:
    name = "stub"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls = 0

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        return DispatchResult(task_id=request.task_id, worker_job_id="job-1",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id,
                            state=TaskState.RUNNING, cwd=self.cwd, detail="stub")

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def corrupt_file(path: Path) -> None:
    """Make the next TaskStore(path) land in the degraded path.

    A real integrity failure is what `PRAGMA integrity_check` reports on a file
    that is not a database, so we overwrite the header of an existing ledger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"NOT-A-SQLITE-HEADER" + b"\x00" * 512)


def main() -> int:
    cwd = str(ROOT / "sandbox" / "repo-a-t11")
    log = {"test": "T19-ledger-fail-closed"}

    # --- A: degraded ledger refuses mutations ----------------------------
    db = ROOT / ".poc-state" / "t19-degraded.db"
    for stale in [db, *db.parent.glob("t19-degraded.db.corrupt-*")]:
        stale.unlink(missing_ok=True)
    corrupt_file(db)
    degraded_store = TaskStore(db)
    adapter = StubAdapter(cwd)
    plane = ControlPlane(adapter, degraded_store, policy=poc_shell())

    outcomes = {}
    for name, call in (
        ("dispatch", lambda: plane.dispatch("echo x", cwd, idempotency_key="t19-x")),
        ("resume", lambda: plane.resume("cp-does-not-exist")),
        ("respawn", lambda: plane.respawn("cp-does-not-exist")),
    ):
        try:
            call()
            outcomes[name] = "ALLOWED (bad)"
        except LedgerDegraded:
            outcomes[name] = "refused"
        except Exception as exc:  # noqa: BLE001
            outcomes[name] = f"{type(exc).__name__}: {exc}"
    queries_ok = plane.summary()["total"] == 0
    log["A_degraded"] = {"degraded": degraded_store.degraded,
                         "reason": degraded_store.degraded_reason[:120],
                         "mutations": outcomes, "queries_still_work": queries_ok,
                         "stub_dispatches": adapter.calls,
                         "pass": (degraded_store.degraded
                                  and all(v == "refused" for v in outcomes.values())
                                  and queries_ok and adapter.calls == 0)}
    degraded_store.close()

    # --- B: crash between intent and job id ------------------------------
    db2 = ROOT / ".poc-state" / "t19-inflight.db"
    store2 = TaskStore(db2)
    store2.clear()
    task_id = store2.new_task_id()
    store2.create({"task_id": task_id, "title": "inflight", "prompt": "echo inflight",
                   "cwd": cwd, "mode": "shell", "isolation": "worktree",
                   "idempotency_key": "t19-inflight", "state": TaskState.QUEUED.value,
                   "verdict": "pending", "policy": poc_shell().to_dict()})
    adapter2 = StubAdapter(cwd)
    plane2 = ControlPlane(adapter2, store2, policy=poc_shell())
    recovered = plane2.recover_inflight()
    after = store2.get(task_id)
    log["B_inflight"] = {"recovered": recovered, "state": after["state"],
                         "warnings": after["warnings"],
                         "requires_resolution": after["state"] == TaskState.DISPATCH_UNKNOWN.value,
                         "pass": (after["state"] == TaskState.DISPATCH_UNKNOWN.value
                                  and recovered["count"] == 1)}

    log["pass"] = log["A_degraded"]["pass"] and log["B_inflight"]["pass"]
    log["result"] = "Pass" if log["pass"] else "Fail"
    (ROOT / "evidence" / "recovery").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "recovery" / "T19-ledger-fail-closed.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
