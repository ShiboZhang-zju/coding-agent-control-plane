"""Compute T11 verdicts without the worker API (it stalled during refresh).

The acceptance layer only needs three things: the ledger's baseline+assertions,
the worktree the agent actually ran in, and the worker's terminal state (read
from its own state file). So the verdict is reproducible offline - which is
itself a useful property to demonstrate.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskSnapshot, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


class OfflineAdapter:
    name = "offline"

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "offline", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def job_state(job_id: str) -> dict:
    p = Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def worktree_for(job_id: str) -> Path | None:
    for base in (ROOT / "sandbox").glob("repo-*/"):
        wt = base / ".codebuddy" / "worktrees" / f"bg-{job_id}"
        if wt.exists():
            return wt
    return None


def main() -> int:
    store = TaskStore(ROOT / ".poc-state" / "t11.db")
    plane = ControlPlane(OfflineAdapter(), store)
    rows = []
    for rec in store.all():
        job_id = rec.get("worker_job_id")
        if not job_id:
            continue
        st = job_state(job_id)
        wt = worktree_for(job_id)
        state = {"done": TaskState.SUCCEEDED, "failed": TaskState.FAILED,
                 "stopped": TaskState.CANCELLED}.get(st.get("state"), TaskState.UNKNOWN)
        snap = TaskSnapshot(task_id=job_id, worker_job_id=job_id, state=state,
                            cwd=str(wt) if wt else rec.get("cwd") or "",
                            started_at=time.time() - 60, finished_at=time.time(),
                            detail=(st.get("detail") or "")[:100])
        patch = plane._snapshot_patch(rec["task_id"], snap)
        store.update(rec["task_id"], **patch)
        rows.append({"task_id": rec["task_id"], "worker_job_id": job_id,
                     "worker_state": st.get("state"), "state": patch["state"],
                     "verdict": patch["verdict"],
                     "assertions": patch["assertion_results"],
                     "cwd_used": snap.cwd})

    out = {"test": "T11-four-agents-ledger", "mode": "offline verdict (no worker API)",
           "tasks": rows,
           "all_verified": all(r["verdict"] == "verified" for r in rows) and len(rows) == 4}
    path = ROOT / "evidence" / "isolation" / "T11-4way-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"tasks={len(rows)} all_verified={out['all_verified']}")
    for r in rows:
        print(f"  {r['task_id']} job={r['worker_job_id']} {r['verdict']} "
              f"{[(a['kind'], a['passed'], a['owned']) for a in r['assertions']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
