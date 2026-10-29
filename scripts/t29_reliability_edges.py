"""T29: reliability edges — PID tri-state and degraded-marker I/O honesty.

Cases
-----
A  pid_tristate   `pid_alive()` returning None must NOT be coerced to False.
                  With an unknown liveness answer the task stays `suspected_lost`:
                  state kept, concurrency slot retained, no blind retry.
                  Only an explicit False (proven dead) releases capacity.
B  marker_write   When the degraded marker cannot be written, the store must not
                  claim a cross-restart guarantee: `degraded_marker_error` is set
                  and the reason says the ban is process-local only.
C  recovery_io    `recover_degraded()` must refuse to lift the ban when the audit
                  log cannot be written, and when the marker cannot be removed.
                  In both cases the store stays degraded.
D  marker_unreadable  A marker that exists but cannot be read still degrades the
                  ledger (an unreadable marker is not evidence of health).

Faults are injected by monkeypatching pathlib.Path methods for the specific
marker/log paths, so no real filesystem permissions are needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchPolicy, DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import LedgerDegraded, TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
LOW = DispatchPolicy(mode="agent", isolation="worktree")
PID = 987654


class TriStateAdapter:
    """Liveness answers are scripted: True / False / None.

    The adapter reports a job as running until it is put into ``report_lost``, so
    a task is never marked lost during dispatch itself — only by an explicit
    adapter report, which is what `_confirm_lost` is supposed to arbitrate.
    """

    name = "tristate"

    def __init__(self):
        self.calls = 0
        self.answer: bool | None = None
        self.raise_on_probe = False
        self.report_lost: set[str] = set()
        self.pid = PID

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{self.calls}",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        state = TaskState.LOST if job_id in self.report_lost else TaskState.RUNNING
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=CWD, pid=self.pid, detail="stub")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [],
                "jobs": []}

    def pid_alive(self, pid):
        if self.raise_on_probe:
            raise TimeoutError("tasklist timed out")
        return self.answer


def fresh(name: str) -> Path:
    db = ROOT / ".poc-state" / f"t29-{name}.db"
    for stale in [db, *db.parent.glob(f"t29-{name}.db.*")]:
        stale.unlink(missing_ok=True)
    return db


# --------------------------------------------------------------------------- #
def case_a_pid_tristate() -> dict:
    db = fresh("a")
    store = TaskStore(db)
    store.clear()
    adapter = TriStateAdapter()
    plane = ControlPlane(adapter, store, policy=LOW)

    def dispatch_one(key: str) -> str:
        rec = plane.dispatch(f"echo {key}", CWD, idempotency_key=key)
        store.update(rec["task_id"], pid=PID)
        adapter.report_lost.add(rec["worker_job_id"])
        return rec["task_id"]

    # 1. probe returns None -> unknown, must not be treated as dead
    adapter.answer = None
    unknown_id = dispatch_one("t29-a-unknown")
    plane.refresh_known_jobs()
    unknown_task = store.get(unknown_id)
    unknown_capacity = store.active_count(adapter.name)

    # 2. probe raises -> also unknown
    adapter.raise_on_probe = True
    raising_id = dispatch_one("t29-a-raising")
    plane.refresh_known_jobs()
    raising_task = store.get(raising_id)
    raising_capacity = store.active_count(adapter.name)
    adapter.raise_on_probe = False

    # 3. probe returns False -> proven dead, capacity released
    adapter.answer = False
    dead_id = dispatch_one("t29-a-dead")
    plane.refresh_known_jobs()
    dead_task = store.get(dead_id)
    dead_capacity = store.active_count(adapter.name)

    store.close()
    return {
        "none_answer": {
            "state": unknown_task["state"],
            "queue_status": unknown_task["queue_status"],
            "suspected_lost": (unknown_task.get("metadata") or {}).get("suspected_lost"),
            "capacity_retained": unknown_capacity >= 1,
        },
        "raising_probe": {
            "state": raising_task["state"],
            "suspected_lost": (raising_task.get("metadata") or {}).get("suspected_lost"),
            "capacity_retained": raising_capacity >= 1,
        },
        "false_answer": {
            "state": dead_task["state"],
            "verdict": dead_task["verdict"],
            "queue_status": dead_task["queue_status"],
            "capacity_released": dead_capacity == 0,
        },
        "pass": (unknown_task["state"] != TaskState.LOST.value
                 and unknown_task["queue_status"] == "running"
                 and (unknown_task.get("metadata") or {}).get("suspected_lost") is True
                 and unknown_capacity >= 1
                 and raising_task["state"] != TaskState.LOST.value
                 and (raising_task.get("metadata") or {}).get("suspected_lost") is True
                 and raising_capacity >= 1
                 and dead_task["state"] == TaskState.LOST.value
                 and dead_task["verdict"] == "rejected"
                 and dead_capacity == 0),
    }


# --------------------------------------------------------------------------- #
def _corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"NOT-A-SQLITE-HEADER" + b"\x00" * 512)


def case_b_marker_write_failure() -> dict:
    db = fresh("b")
    _corrupt(db)
    marker = db.with_name(db.name + ".DEGRADED")

    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self.name == marker.name:
            raise OSError(28, "No space left on device")
        return original_write_text(self, *args, **kwargs)

    Path.write_text = failing_write_text
    try:
        store = TaskStore(db)
        degraded = store.degraded
        marker_error = store.degraded_marker_error
        reason = store.degraded_reason
        marker_exists = marker.exists()
        store.close()
    finally:
        Path.write_text = original_write_text

    return {
        "degraded": degraded,
        "marker_error_recorded": bool(marker_error),
        "reason_admits_process_local": "process-local" in reason,
        "marker_on_disk": marker_exists,
        "pass": (degraded is True and bool(marker_error)
                 and "process-local" in reason and marker_exists is False),
    }


# --------------------------------------------------------------------------- #
def case_c_recovery_io() -> dict:
    db = fresh("c")
    _corrupt(db)
    store = TaskStore(db)
    degraded_before = store.degraded
    marker = db.with_name(db.name + ".DEGRADED")
    audit = db.parent / "LEDGER_RECOVERY.log"

    # 1. audit log not writable -> recovery refused, still degraded
    original_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name == audit.name:
            raise OSError(13, "Permission denied")
        return original_open(self, *args, **kwargs)

    Path.open = failing_open
    try:
        try:
            store.recover_degraded("operator note")
            audit_outcome = "recovered (bad)"
        except LedgerDegraded:
            audit_outcome = "refused"
    finally:
        Path.open = original_open
    still_degraded_after_audit_failure = store.degraded

    # 2. marker cannot be removed -> recovery refused, still degraded
    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name == marker.name:
            raise OSError(13, "Permission denied")
        return original_unlink(self, *args, **kwargs)

    Path.unlink = failing_unlink
    try:
        try:
            store.recover_degraded("operator note")
            unlink_outcome = "recovered (bad)"
        except LedgerDegraded:
            unlink_outcome = "refused"
    finally:
        Path.unlink = original_unlink
    still_degraded_after_unlink_failure = store.degraded

    # 3. with I/O healthy, recovery succeeds and the ban is really gone
    recovered = store.recover_degraded("operator reviewed the quarantine copy")
    marker_gone = not marker.exists()
    store.close()
    reopened = TaskStore(db)
    clean_after_restart = not reopened.degraded
    reopened.close()

    return {
        "degraded_before": degraded_before,
        "audit_write_failure": audit_outcome,
        "still_degraded_after_audit_failure": still_degraded_after_audit_failure,
        "marker_unlink_failure": unlink_outcome,
        "still_degraded_after_unlink_failure": still_degraded_after_unlink_failure,
        "healthy_recovery": recovered.get("recovered"),
        "marker_gone": marker_gone,
        "clean_after_restart": clean_after_restart,
        "pass": (degraded_before is True
                 and audit_outcome == "refused"
                 and still_degraded_after_audit_failure is True
                 and unlink_outcome == "refused"
                 and still_degraded_after_unlink_failure is True
                 and recovered.get("recovered") is True
                 and marker_gone and clean_after_restart),
    }


# --------------------------------------------------------------------------- #
def case_d_marker_unreadable() -> dict:
    db = fresh("d")
    store = TaskStore(db)
    store.close()
    marker = db.with_name(db.name + ".DEGRADED")
    marker.write_text("previous process was degraded\n", encoding="utf-8")

    original_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self.name == marker.name:
            raise OSError(5, "I/O error")
        return original_read_text(self, *args, **kwargs)

    Path.read_text = failing_read_text
    try:
        reopened = TaskStore(db)
        degraded = reopened.degraded
        reason = reopened.degraded_reason
        reopened.close()
    finally:
        Path.read_text = original_read_text

    marker.unlink(missing_ok=True)
    return {
        "degraded": degraded,
        "reason_mentions_unreadable": "unreadable" in reason,
        "pass": degraded is True and "unreadable" in reason,
    }


def main() -> int:
    log = {
        "test": "T29-reliability-edges",
        "A_pid_tristate": case_a_pid_tristate(),
        "B_marker_write_failure": case_b_marker_write_failure(),
        "C_recovery_io": case_c_recovery_io(),
        "D_marker_unreadable": case_d_marker_unreadable(),
    }
    keys = ("A_pid_tristate", "B_marker_write_failure", "C_recovery_io",
            "D_marker_unreadable")
    log["pass"] = {k: log[k]["pass"] for k in keys}
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "recovery" / "T29-reliability-edges.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:5000])
    print("RESULT:", log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
