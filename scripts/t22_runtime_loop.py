"""T22: the runtime automatically recovers and reconciles without manual refresh."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class AdvancingAdapter:
    name = "advancing"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.get_calls = 0
        self.scan_calls = 0
        self.jobs = {"job-1"}
        self.fail_first_scan = True

    def dispatch(self, request) -> DispatchResult:
        return DispatchResult(task_id=request.task_id, worker_job_id="job-1",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        self.get_calls += 1
        state = TaskState.RUNNING if self.get_calls == 1 else TaskState.SUCCEEDED
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=self.cwd, detail=f"observation-{self.get_calls}")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        self.scan_calls += 1
        if self.fail_first_scan and self.scan_calls == 1:
            raise TimeoutError("simulated first orphan scan timeout")
        return {"source": "stub", "total": len(self.jobs), "new": [],
                "changed": [], "lost": [],
                "jobs": [{"id": job_id, "state": "done"} for job_id in self.jobs]}


def main() -> int:
    db = ROOT / ".poc-state" / "t22-runtime.db"
    store = TaskStore(db)
    store.clear()
    cwd = str(ROOT / "sandbox" / "runtime-t22")
    adapter = AdvancingAdapter(cwd)
    plane = ControlPlane(adapter, store, policy=agent_worktree())

    dispatched = plane.dispatch("runtime observation", cwd, idempotency_key="t22-job")
    inflight_id = store.new_task_id()
    store.create({
        "task_id": inflight_id,
        "title": "inflight before runtime start",
        "prompt": "unknown dispatch outcome",
        "cwd": cwd,
        "mode": "agent",
        "isolation": "worktree",
        "idempotency_key": "t22-inflight",
        "state": TaskState.QUEUED.value,
        "verdict": "pending",
        "metadata": {"adapter": adapter.name},
    })

    runtime = ControlPlaneRuntime(
        plane,
        RuntimeConfig(known_refresh_interval_s=0.03,
                      orphan_scan_interval_s=0.07,
                      max_backoff_s=0.2,
                      orphan_scan_on_start=True,
                      idle_sleep_cap_s=0.01),
    )
    started = runtime.start(name="t22-runtime")
    deadline = time.time() + 2
    while time.time() < deadline:
        task = store.get(dispatched["task_id"])
        status = runtime.status()
        if (task and task["state"] == TaskState.SUCCEEDED.value
                and status["orphan_scan"]["runs"] >= 2):
            break
        time.sleep(0.01)
    stopped = runtime.stop(timeout_s=2)

    final_task = store.get(dispatched["task_id"])
    recovered = store.get(inflight_id)
    status = runtime.status()

    # A miss, a successful scan, and another miss are not two consecutive
    # misses. This protects long-running automatic scans from accumulating old
    # transient failures until they falsely mark a healthy task lost.
    stable_id = store.new_task_id()
    store.create({
        "task_id": stable_id,
        "title": "scan miss reset",
        "prompt": "stay running",
        "cwd": cwd,
        "mode": "agent",
        "isolation": "worktree",
        "idempotency_key": "t22-scan-reset",
        "state": TaskState.RUNNING.value,
        "verdict": "pending",
        "worker_job_id": "job-stable",
        "metadata": {"adapter": adapter.name},
    })
    adapter.jobs.discard("job-stable")
    plane.discover_orphan_jobs()
    misses_after_first = store.get(stable_id)["metadata"].get("scan_misses")
    adapter.jobs.add("job-stable")
    plane.discover_orphan_jobs()
    misses_after_recovery = store.get(stable_id)["metadata"].get("scan_misses")
    adapter.jobs.discard("job-stable")
    plane.discover_orphan_jobs()
    stable_after_second_miss = store.get(stable_id)

    checks = {
        "background_started": started,
        "known_job_refreshed_automatically": adapter.get_calls >= 2,
        "terminal_state_observed": final_task["state"] == TaskState.SUCCEEDED.value,
        "orphan_scan_retried_after_failure": adapter.scan_calls >= 2,
        "scan_failure_did_not_stop_known_refresh": (
            adapter.get_calls >= 2 and status["known_jobs"]["runs"] >= 2),
        "scan_backoff_recovered": status["orphan_scan"]["consecutive_errors"] == 0,
        "nonconsecutive_scan_misses_reset": (
            misses_after_first == 1
            and misses_after_recovery == 0
            and stable_after_second_miss["metadata"].get("scan_misses") == 1
            and stable_after_second_miss["state"] == TaskState.RUNNING.value),
        "inflight_recovered": recovered["state"] == TaskState.DISPATCH_UNKNOWN.value,
        "stopped_cleanly": stopped and not status["running"],
    }
    log = {
        "test": "T22-runtime-loop",
        "checks": checks,
        "adapter": {"get_calls": adapter.get_calls, "scan_calls": adapter.scan_calls},
        "runtime": status,
        "task": {"task_id": final_task["task_id"], "state": final_task["state"],
                 "verdict": final_task["verdict"]},
        "inflight": {"task_id": recovered["task_id"], "state": recovered["state"],
                     "warnings": recovered["warnings"]},
        "scan_miss_reset": {
            "after_first_miss": misses_after_first,
            "after_recovery": misses_after_recovery,
            "after_second_nonconsecutive_miss":
                stable_after_second_miss["metadata"].get("scan_misses"),
            "state": stable_after_second_miss["state"],
        },
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    path = ROOT / "evidence" / "recovery" / "T22-runtime-loop.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    store.close()
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
