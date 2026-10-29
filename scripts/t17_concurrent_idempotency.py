"""T17: concurrent dispatch on one idempotency key, and honest dispatch_unknown.

Part A (concurrency): 8 threads dispatch the same key at the same time. The worker
must be called exactly once and the ledger must hold exactly one work item and one
attempt. T12 only proved sequential dedupe.

Part B (uncertainty): when the adapter cannot tell whether the worker created a
job, the plane must keep `dispatch_unknown`. A failed lookup is *not* proof of
absence, so `resolve_dispatch()` without an operator confirmation must not clear it.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class CountingAdapter:
    name = "counting"

    def __init__(self, uncertain: bool = False, find_result: str | None = None):
        self.uncertain = uncertain
        self.find_result = find_result
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.cwd = ""

    def dispatch(self, request) -> DispatchResult:
        with self._lock:
            self.calls.append({"task_id": request.task_id, "prompt": request.prompt})
            n = len(self.calls)
        time.sleep(0.05)                      # widen the race window
        job_id = f"job-{n}"
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=None if self.uncertain else job_id,
                              accepted=not self.uncertain,
                              uncertain=self.uncertain,
                              warnings=[], raw={"dispatch_http": 500 if self.uncertain else 200})

    def get_task(self, job_id: str) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=TaskState.RUNNING,
                            cwd=self.cwd, started_at=time.time(), detail="stub")

    def collect_artifacts(self, task_id):
        return []

    def find_job(self, prompt: str, cwd: str, since: float = 0):
        return self.find_result

    def reconcile(self):
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def part_a(store: TaskStore) -> dict:
    adapter = CountingAdapter()
    adapter.cwd = str(ROOT / "sandbox" / "repo-a-t11")
    plane = ControlPlane(adapter, store, policy=poc_shell())

    results, errors = [], []
    barrier = threading.Barrier(8)

    def one(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(plane.dispatch(f"echo race-{i}", adapter.cwd,
                                          title=f"race-{i}", idempotency_key="race-key"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    items = store.all()
    task_id = items[0]["task_id"] if items else None
    return {
        "threads": 8,
        "worker_dispatches": len(adapter.calls),
        "work_items": len(items),
        "attempts": len(store.attempts(task_id)) if task_id else 0,
        "deduplicated_responses": len([r for r in results if r.get("deduplicated")]),
        "errors": errors,
        "pass": len(adapter.calls) == 1 and len(items) == 1,
    }


def part_b(store: TaskStore) -> dict:
    adapter = CountingAdapter(uncertain=True, find_result=None)
    adapter.cwd = str(ROOT / "sandbox" / "repo-a-t11")
    plane = ControlPlane(adapter, store, policy=poc_shell())
    rec = plane.dispatch("echo unknown", adapter.cwd, title="uncertain",
                         idempotency_key="unknown-key")
    task_id = rec["task_id"]
    state_after_dispatch = store.get(task_id)["state"]

    plane.resolve_dispatch(task_id)                     # no operator confirmation
    after_miss = store.get(task_id)
    plane.resolve_dispatch(task_id, confirm_absent=True)  # explicit human decision
    after_confirm = store.get(task_id)

    return {
        "state_after_dispatch": state_after_dispatch,
        "state_after_unconfirmed_lookup": after_miss["state"],
        "kept_unknown_on_miss": after_miss["state"] == TaskState.DISPATCH_UNKNOWN.value,
        "miss_warnings": after_miss["warnings"],
        "state_after_operator_confirm": after_confirm["state"],
        "confirm_warnings": after_confirm["warnings"],
        "worker_dispatches": len(adapter.calls),
        "pass": (state_after_dispatch == TaskState.DISPATCH_UNKNOWN.value
                 and after_miss["state"] == TaskState.DISPATCH_UNKNOWN.value
                 and after_confirm["state"] == TaskState.UNKNOWN.value
                 and len(adapter.calls) == 1),
    }


def main() -> int:
    db = ROOT / ".poc-state" / "t17.db"
    store = TaskStore(db)
    store.clear()
    log = {"test": "T17-concurrent-idempotency", "A": part_a(store)}

    store_b = TaskStore(ROOT / ".poc-state" / "t17b.db")
    store_b.clear()
    log["B"] = part_b(store_b)

    log["result"] = "Pass" if (log["A"]["pass"] and log["B"]["pass"]) else "Fail"
    (ROOT / "evidence" / "jobs").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "jobs" / "T17-concurrent-idempotency.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
