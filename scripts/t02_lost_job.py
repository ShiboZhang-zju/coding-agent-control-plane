"""T02: a job whose process dies underneath the worker (the `lost` state).

Observed in practice before this test existed: two jobs stayed at state=working
with alive=false, settled=false and pid=null forever. This script reproduces it
deterministically by killing the spawned shell process, then measures how long
the adapter needs to classify the task as `lost` instead of `running`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 300 /nobreak >nul & echo never > lost_target.txt"


def main() -> int:
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t02.json"))
    log = {"test": "T02-lost-job", "samples": []}

    rec = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "lost_target.txt")],
                         mode="shell", title="T02-lost-subject", idempotency_key="t02")
    job_id = rec["worker_job_id"]
    log["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id}

    time.sleep(4)
    pid = adapter.api.job(job_id).get("pid")
    log["pid"] = pid
    killed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, text=True)
    log["kill"] = {"cmd": f"taskkill /PID {pid} /T /F",
                   "returncode": killed.returncode,
                   "stdout": (killed.stdout or "").strip(),
                   "stderr": (killed.stderr or "").strip()}

    deadline = time.time() + 200
    first_lost = None
    while time.time() < deadline:
        snap = adapter.get_task(job_id)
        meta = snap.metadata["codebuddy"]
        sample = {"t": round(time.time() - log["kill"].get("at", time.time()), 1),
                  "state": snap.state.value, "worker_state": meta["state"],
                  "alive": meta["alive"], "settled": meta["settled"],
                  "stale_s": meta["stale_s"], "pid_field": snap.pid}
        if not log["samples"] or log["samples"][-1]["state"] != sample["state"] \
                or log["samples"][-1]["worker_state"] != sample["worker_state"]:
            log["samples"].append(sample)
        if snap.state.value == "lost" and first_lost is None:
            first_lost = time.time()
            log["first_lost_detected"] = sample
            break
        time.sleep(5)

    log["detected_lost"] = first_lost is not None
    plane.refresh()
    rec_after = plane.store.get(rec["task_id"])
    log["verdict"] = rec_after["verdict"]
    log["worker_state_reported"] = snap.metadata["codebuddy"]["state"]
    log["note"] = ("worker keeps state=working forever after the process is killed; only "
                   "alive/settled/updatedAt distinguish it, so the plane must own a staleness "
                   "threshold to report `lost`")
    write_evidence(ROOT / "evidence" / "recovery" / "T02-lost-job.json", log)
    print(json.dumps({"job": log["job"], "pid": pid, "detected_lost": log["detected_lost"],
                      "samples": log["samples"], "verdict": log["verdict"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
