"""T24: two independent runtimes cannot dispatch the same queued work item."""
from __future__ import annotations

import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class ProcessAdapter:
    name = "process-stub"

    def __init__(self, cwd: str, dispatch_log):
        self.cwd = cwd
        self.dispatch_log = dispatch_log

    def dispatch(self, request) -> DispatchResult:
        self.dispatch_log.append({"task_id": request.task_id,
                                  "pid": __import__("os").getpid()})
        # A deterministic id makes an accidental second call visible in the log
        # while keeping both processes able to query the accepted job.
        return DispatchResult(task_id=request.task_id, worker_job_id="job-shared",
                              accepted=True, raw={"dispatch_http": 200})

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        return TaskSnapshot(task_id=job_id, worker_job_id=job_id,
                            state=TaskState.RUNNING, cwd=self.cwd, detail="stub")

    def collect_artifacts(self, _task_id):
        return []


def contender(db_path: str, cwd: str, dispatch_log, gate, results, owner: str) -> None:
    store = TaskStore(db_path)
    plane = ControlPlane(ProcessAdapter(cwd, dispatch_log), store, policy=agent_worktree())
    gate.wait(timeout=10)
    rec = plane.dispatch_next(owner, concurrency_limit=2)
    results.append({"owner": owner, "claimed": bool(rec),
                    "task_id": rec.get("task_id") if rec else None})
    store.close()


def main() -> int:
    ctx = mp.get_context("spawn")
    db = ROOT / ".poc-state" / "t24-multiprocess.db"
    store = TaskStore(db)
    store.clear()
    cwd = str(ROOT / "sandbox" / "runtime-tests" / "runtime-t24")
    Path(cwd).mkdir(parents=True, exist_ok=True)

    with ctx.Manager() as manager:
        dispatch_log = manager.list()
        results = manager.list()
        gate = ctx.Barrier(2)
        plane = ControlPlane(ProcessAdapter(cwd, dispatch_log), store, policy=agent_worktree())
        queued = plane.enqueue("one queued task", cwd, idempotency_key="t24-one")

        processes = [ctx.Process(target=contender,
                                 args=(str(db), cwd, dispatch_log, gate, results,
                                       f"runtime-{index}"))
                     for index in (1, 2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)

        process_exitcodes = [process.exitcode for process in processes]
        dispatches = list(dispatch_log)
        claims = list(results)

    final = store.get(queued["task_id"])
    attempts = store.attempts(queued["task_id"])
    checks = {
        "processes_exited_cleanly": process_exitcodes == [0, 0],
        "exactly_one_worker_dispatch": len(dispatches) == 1,
        "exactly_one_claim_winner": len([r for r in claims if r["claimed"]]) == 1,
        "one_attempt_persisted": len(attempts) == 1,
        "queue_is_running": final["queue_status"] == "running",
        "worker_job_bound": final["worker_job_id"] == "job-shared",
    }
    log = {
        "test": "T24-multiprocess-claim",
        "checks": checks,
        "process_exitcodes": process_exitcodes,
        "claims": claims,
        "dispatches": dispatches,
        "work_item": {"task_id": final["task_id"], "state": final["state"],
                      "queue_status": final["queue_status"],
                      "worker_job_id": final["worker_job_id"]},
        "attempts": [{"id": a["id"], "worker_job_id": a["worker_job_id"]}
                     for a in attempts],
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    path = ROOT / "evidence" / "jobs" / "T24-multiprocess-claim.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    store.close()
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
