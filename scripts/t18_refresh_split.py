"""T18: refresh must not let one slow query block or corrupt the whole view.

Checks:
  A. a per-job query failure is an observation anomaly (state unchanged), not `lost`
  B. a job missing from TWO consecutive full scans becomes `lost`
  C. a failing full scan leaves known-task state untouched
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class FlakyAdapter:
    """Slow for one job, healthy for the other, and can fail the full scan."""

    name = "flaky"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.jobs: dict[str, TaskState] = {}
        self.slow: set[str] = set()
        self.scan_fails = False
        self.scan_visible: set[str] | None = None       # None = report all

    def dispatch(self, request) -> DispatchResult:
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = TaskState.RUNNING
        return DispatchResult(task_id=request.task_id, worker_job_id=job_id,
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        if job_id in self.slow:
            raise TimeoutError(f"simulated slow query for {job_id}")
        state = self.jobs.get(job_id, TaskState.UNKNOWN)
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=self.cwd, started_at=time.time() - 10,
                            finished_at=time.time(), detail="stub")

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self) -> dict:
        if self.scan_fails:
            raise TimeoutError("simulated full-scan timeout (job store stall)")
        visible = self.scan_visible if self.scan_visible is not None else set(self.jobs)
        return {"source": "stub", "total": len(visible), "new": [], "changed": [], "lost": [],
                "jobs": [{"id": j, "state": "done"} for j in visible]}


def main() -> int:
    cwd = str(ROOT / "sandbox" / "repo-a-t11")
    store = TaskStore(ROOT / ".poc-state" / "t18.db")
    store.clear()
    adapter = FlakyAdapter(cwd)
    plane = ControlPlane(adapter, store, policy=poc_shell())
    log = {"test": "T18-refresh-split"}

    a = plane.dispatch("echo a", cwd, title="a", idempotency_key="t18-a")
    b = plane.dispatch("echo b", cwd, title="b", idempotency_key="t18-b")
    job_a, job_b = a["worker_job_id"], b["worker_job_id"]

    # --- A: one slow query ------------------------------------------------
    adapter.slow = {job_a}
    r1 = plane.refresh_known_jobs(timeout_s=1)
    after_a = store.get(a["task_id"])
    log["A_slow_query"] = {
        "updated": len(r1["updated"]), "anomalies": r1["anomalies"],
        "state_of_slow_task": after_a["state"],
        "observe_failures": (after_a.get("metadata") or {}).get("observe_failures"),
        "pass": after_a["state"] != TaskState.LOST.value and len(r1["anomalies"]) == 1,
    }

    # --- B: job vanishes from two consecutive scans ------------------------
    adapter.slow = set()
    adapter.scan_visible = {job_b}
    first = plane.discover_orphan_jobs()
    state_after_one = store.get(a["task_id"])["state"]
    second = plane.discover_orphan_jobs()
    state_after_two = store.get(a["task_id"])["state"]
    log["B_vanished_job"] = {
        "first_scan": first["vanished"], "state_after_one_scan": state_after_one,
        "second_scan": second["vanished"], "state_after_two_scans": state_after_two,
        "pass": state_after_one != "lost" and state_after_two == "lost",
    }

    # --- C: full scan fails ----------------------------------------------
    state_before = store.get(b["task_id"])["state"]
    adapter.scan_fails = True
    failed = plane.discover_orphan_jobs()
    state_after = store.get(b["task_id"])["state"]
    log["C_scan_failure"] = {"ok": failed["ok"], "error": failed.get("error"),
                             "state_before": state_before, "state_after": state_after,
                             "pass": failed["ok"] is False and state_before == state_after}

    log["pass"] = all(log[k]["pass"] for k in ("A_slow_query", "B_vanished_job", "C_scan_failure"))
    log["result"] = "Pass" if log["pass"] else "Fail"
    (ROOT / "evidence" / "recovery").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "recovery" / "T18-refresh-split.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: log[k] for k in ("A_slow_query", "B_vanished_job", "C_scan_failure",
                                          "result")}, ensure_ascii=False, indent=1)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
