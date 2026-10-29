"""T16: acceptance context must survive an adapter/plane restart.

The bug (review P0-1): `_snapshot_patch()` used `snap.task_id`. After a restart
the adapter has no in-memory job->task map, so the snapshot carries the *worker
job id*; the ledger lookup then misses and assertions/baseline are silently
dropped -> the verdict degrades (typically to `inconclusive`).

This test simulates exactly that: the stub adapter always reports the worker job
id as `task_id`, and we rebuild both objects before refreshing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (ArtifactAssertion, DispatchResult, TaskSnapshot,  # noqa: E402
                           TaskState)
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class ForgetfulAdapter:
    """Stands in for a worker adapter whose in-memory job->task map was lost."""

    name = "forgetful"

    def __init__(self, cwd: Path):
        self.cwd = str(cwd)
        self.job_id = "job-" + str(int(time.time() * 1000))[-6:]

    def dispatch(self, request) -> DispatchResult:
        return DispatchResult(task_id=request.task_id, worker_job_id=self.job_id,
                              accepted=True, warnings=[], raw={"dispatch_http": 200})

    def get_task(self, task_id_or_job: str) -> TaskSnapshot:
        # the important bit: task_id == worker job id, like a restarted adapter
        return TaskSnapshot(task_id=self.job_id, worker_job_id=self.job_id,
                            state=TaskState.SUCCEEDED, cwd=self.cwd,
                            started_at=time.time() - 5, finished_at=time.time(),
                            detail="stub finished")

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "stub", "total": 1, "new": [], "changed": [], "lost": [],
                "jobs": [{"id": self.job_id, "state": "done"}]}


def main() -> int:
    cwd = ROOT / "sandbox" / "repo-a-t11"
    target = cwd / "t16_artifact.txt"
    if target.exists():
        target.unlink()

    db = ROOT / ".poc-state" / "t16.db"
    store = TaskStore(db)
    store.clear()
    adapter = ForgetfulAdapter(cwd)
    plane = ControlPlane(adapter, store, policy=poc_shell())

    log = {"test": "T16-restart-mapping", "cwd": str(cwd)}

    # --- before restart: dispatch, then let the artifact appear -------------
    rec = plane.dispatch("echo t16 > t16_artifact.txt", str(cwd),
                         assertions=[ArtifactAssertion("file_exists", "t16_artifact.txt")],
                         title="T16-restart", idempotency_key="t16")
    task_id = rec["task_id"]
    job_id = rec["worker_job_id"]
    target.write_text("t16\n", encoding="utf-8")
    plane.refresh()
    before = store.get(task_id)
    log["before_restart"] = {"task_id": task_id, "worker_job_id": job_id,
                             "state": before["state"], "verdict": before["verdict"],
                             "assertions": len(before["assertions"]),
                             "has_baseline": bool(before["baseline"]),
                             "attempts": len(store.attempts(task_id))}

    # --- simulate a process restart: brand new store + adapter + plane ------
    store2 = TaskStore(db)
    adapter2 = ForgetfulAdapter(cwd)
    adapter2.job_id = job_id          # same worker job, but no job->task map
    plane2 = ControlPlane(adapter2, store2, policy=poc_shell())
    plane2.refresh()
    after = store2.get(task_id)
    log["after_restart"] = {"state": after["state"], "verdict": after["verdict"],
                            "assertions": len(after["assertions"]),
                            "assertion_results": after["assertion_results"],
                            "has_baseline": bool(after["baseline"]),
                            "attempts": len(store2.attempts(task_id)),
                            "current_job": after["worker_job_id"]}

    log["pass"] = {
        "verdict_kept": after["verdict"] == before["verdict"],
        "verdict_is_verified": after["verdict"] == "verified",
        "assertions_kept": len(after["assertions"]) == len(before["assertions"]) == 1,
        "baseline_kept": bool(after["baseline"]),
        "attempt_kept": len(store2.attempts(task_id)) == len(store.attempts(task_id)),
        "job_mapping_kept": after["worker_job_id"] == job_id,
        "no_duplicate_work_item": len(store2.all()) == 1,
    }
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    (ROOT / "evidence" / "recovery").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / "recovery" / "T16-restart-mapping.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
