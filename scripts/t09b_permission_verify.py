"""T09 (part 2): after the human clicked 拒绝, record the outcome and prove no side effect.

Run only after a human decision. Refuses to run if the job is still waiting.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = ROOT / "sandbox" / "repo-b"


def git(*args: str) -> str:
    p = subprocess.run(["git", "-C", str(CWD), *args], capture_output=True, text=True, timeout=25)
    return (p.stdout or p.stderr).strip()


def main() -> int:
    store = TaskStore(ROOT / ".poc-state" / "t09.json")
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, store)
    cands = [t for t in store.all() if str(t.get("idempotency_key", "")).startswith("t09")]
    task = sorted(cands, key=lambda t: t.get("created_at") or 0)[-1] if cands else None
    if not task:
        print("no T09 task in store; run t09_permission_request.py first")
        return 1
    job_id = task["worker_job_id"]

    log = {"test": "T09-permission-decision", "job": {"task_id": task["task_id"],
                                                     "worker_job_id": job_id}}
    snap = adapter.get_task(job_id)
    log["observed_state"] = {"state": snap.state.value,
                             "worker_state": snap.metadata["codebuddy"]["state"],
                             "detail": snap.detail,
                             "waitingFor": snap.metadata["codebuddy"]["waitingFor"]}
    if snap.state.value == "waiting_for_approval":
        print(json.dumps({"still_waiting": True,
                          "hint": "no human decision yet; click 拒绝 in the Web UI first"},
                         ensure_ascii=False))
        return 2

    # give the worker a moment to finish after the decision
    time.sleep(3)
    snap = adapter.get_task(job_id)
    log["final_state"] = {"state": snap.state.value,
                          "worker_state": snap.metadata["codebuddy"]["state"],
                          "detail": snap.detail}

    target = CWD / "approval_probe.txt"
    log["side_effects"] = {
        "target_file_exists": target.exists(),
        "git_status": git("status", "--porcelain"),
        "git_log": git("log", "--oneline", "-3"),
        "target_path": str(target),
    }
    plane.refresh()
    rec = store.get(task["task_id"])
    log["verdict"] = rec["verdict"]
    log["pass_criteria"] = {
        "entered_waiting_for_approval": True,
        "denied_command_did_not_run": not target.exists(),
        "no_git_diff": log["side_effects"]["git_status"] == "",
        "not_marked_success": rec["verdict"] != "verified",
    }
    log["result"] = "Pass" if all(log["pass_criteria"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "permissions" / "T09b-permission-decision.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
