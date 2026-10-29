"""Re-run the ledger refresh for the T11 run and store the verdicts.

The original test timed out in `refresh()` (job store ~30s + parallel load). Now
that every job is terminal, a refresh should be cheap; if it still fails we keep
the API-independent evidence from `t11_recover.py` and say so.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402


def main() -> int:
    store = TaskStore(ROOT / ".poc-state" / "t11.db")
    recs = store.all()
    if not recs:
        print(json.dumps({"error": "no work items in .poc-state/t11.db"}))
        return 1
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, store)
    t0 = time.time()
    try:
        plane.refresh()
        ok, err = True, None
    except Exception as exc:  # noqa: BLE001
        ok, err = False, f"{type(exc).__name__}: {exc}"
    out = {
        "refresh_ok": ok, "error": err, "seconds": round(time.time() - t0, 1),
        "tasks": [{"task_id": r["task_id"], "worker_job_id": r.get("worker_job_id"),
                   "state": r["state"], "verdict": r["verdict"],
                   "assertions": r.get("assertion_results"),
                   "worktree": r.get("worktree"), "attempts": len(store.attempts(r["task_id"]))}
                  for r in store.all()],
    }
    out["all_verified"] = all(t["verdict"] == "verified" for t in out["tasks"])
    path = ROOT / "evidence" / "isolation" / "T11-4way-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"refresh_ok": ok, "seconds": out["seconds"],
                      "all_verified": out["all_verified"],
                      "verdicts": [t["verdict"] for t in out["tasks"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
