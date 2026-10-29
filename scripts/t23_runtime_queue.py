"""T23: queued work is priority-ordered, concurrency-bound and approval-gated."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchPolicy, DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class QueueAdapter:
    name = "queue-stub"

    def __init__(self, cwd: str):
        self.cwd = cwd
        self._lock = threading.Lock()
        self.dispatches: list[dict] = []
        self.states: dict[str, TaskState] = {}

    def dispatch(self, request) -> DispatchResult:
        with self._lock:
            job_id = f"job-{len(self.dispatches) + 1}"
            self.dispatches.append({"job_id": job_id, "task_id": request.task_id,
                                    "title": request.title, "prompt": request.prompt})
            self.states[job_id] = TaskState.RUNNING
        return DispatchResult(task_id=request.task_id, worker_job_id=job_id,
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        with self._lock:
            state = self.states[job_id]
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=self.cwd, detail=state.value,
                            finished_at=time.time() if state.terminal else None)

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        with self._lock:
            jobs = [{"id": job_id, "state": state.value}
                    for job_id, state in self.states.items()]
        return {"source": "stub", "total": len(jobs), "new": [],
                "changed": [], "lost": [], "jobs": jobs}

    def finish(self, job_id: str) -> None:
        with self._lock:
            self.states[job_id] = TaskState.SUCCEEDED


def wait_for(predicate, timeout_s: float = 30.0) -> bool:
    # 30s: each dispatch pays a baseline fingerprint (a couple of git child
    # processes), which costs seconds per item on slow-disk Windows machines.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    store = TaskStore(ROOT / ".poc-state" / "t23-runtime-queue.db")
    store.clear()
    # Isolated from other sandbox repos: baseline scanning cost must stay
    # machine-independent so the 3s wait_for windows below remain valid.
    cwd = str(ROOT / "sandbox" / "runtime-tests" / "runtime-t23")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    adapter = QueueAdapter(cwd)
    plane = ControlPlane(adapter, store, policy=agent_worktree())

    low = plane.enqueue("low", cwd, title="low", priority=1,
                        idempotency_key="t23-low")
    high = plane.enqueue("high", cwd, title="high", priority=100,
                         idempotency_key="t23-high")
    mid = plane.enqueue("mid", cwd, title="mid", priority=50,
                        idempotency_key="t23-mid")

    # High risk is persisted but must not enter the ready queue before approval.
    risky_policy = DispatchPolicy(mode="shell", isolation="none",
                                  allow_unisolated_shell=True)
    risky = plane.enqueue("risky", cwd, title="risky", priority=200,
                          policy=risky_policy, idempotency_key="t23-risky")
    synchronous_dispatch_count = len(adapter.dispatches)

    runtime = ControlPlaneRuntime(
        plane,
        RuntimeConfig(dispatch_interval_s=0.02,
                      known_refresh_interval_s=0.02,
                      orphan_scan_interval_s=5,
                      max_concurrent_tasks=2,
                      max_dispatch_per_cycle=4,
                      max_backoff_s=0.2,
                      idle_sleep_cap_s=0.01),
        owner="t23-runtime",
    )
    runtime.start(name="t23-runtime")
    first_wave = wait_for(lambda: len(adapter.dispatches) == 2)
    first_order = [d["title"] for d in adapter.dispatches]
    low_before_slot = store.get(low["task_id"])
    risky_before_approval = store.get(risky["task_id"])

    # Free one slot; the remaining approved low-priority item should start.
    adapter.finish(adapter.dispatches[0]["job_id"])
    second_wave = wait_for(lambda: len(adapter.dispatches) == 3)
    third_title = adapter.dispatches[2]["title"] if len(adapter.dispatches) >= 3 else None

    # Approval changes the exact queued item from needs_approval -> ready, but it
    # still waits for capacity. Its higher priority wins the next free slot.
    pending = [a for a in store.all_approvals()
               if a.get("digest") == risky.get("task_digest")
               and a.get("decision") == "pending"]
    approval = plane.approve(risky["task_digest"], approval_id=pending[-1]["id"])
    calls_after_approval_without_slot = len(adapter.dispatches)
    time.sleep(0.5)
    still_capacity_bound = len(adapter.dispatches) == calls_after_approval_without_slot
    adapter.finish(adapter.dispatches[1]["job_id"])
    risky_wave = wait_for(lambda: len(adapter.dispatches) == 4)
    fourth_title = adapter.dispatches[3]["title"] if len(adapter.dispatches) >= 4 else None
    runtime.stop(timeout_s=10)

    risky_after = store.get(risky["task_id"])
    approval_row = [a for a in store.all_approvals()
                    if a.get("id") == approval.get("id")][0]
    checks = {
        "enqueue_does_not_dispatch_synchronously": synchronous_dispatch_count == 0,
        "priority_order": first_order == ["high", "mid"],
        "concurrency_limit_holds": (
            low_before_slot["queue_status"] == "ready"
            and len(first_order) == 2),
        "approval_gate_holds": risky_before_approval["queue_status"] == "needs_approval",
        "slot_release_dispatches_next": second_wave and third_title == "low",
        "approval_releases_exact_queue_item": approval.get("queued_items_released") == 1,
        "approved_item_still_capacity_bound": still_capacity_bound,
        "approved_priority_item_dispatches": risky_wave and fourth_title == "risky",
        "approval_consumed_once": approval_row.get("consumed_at") is not None,
        "risky_item_running": risky_after["queue_status"] == "running",
    }
    log = {
        "test": "T23-runtime-queue",
        "checks": checks,
        "dispatch_order": [d["title"] for d in adapter.dispatches],
        "dispatches": adapter.dispatches,
        "queue": {t["title"]: {"state": t["state"],
                                "queue_status": t["queue_status"],
                                "priority": t["priority"]}
                  for t in store.all()},
        "approval": {"id": approval_row["id"],
                     "decision": approval_row["decision"],
                     "consumed": approval_row.get("consumed_at") is not None,
                     "consumed_by": approval_row.get("consumed_by")},
        "runtime": runtime.status(),
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    path = ROOT / "evidence" / "jobs" / "T23-runtime-queue.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    store.close()
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
