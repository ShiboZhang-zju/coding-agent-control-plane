"""T08: worker (CodeBuddy HTTP service) restart.

Questions:
  * does a job that was running survive the service restart (process + execution)?
  * is a finished job still discoverable afterwards?
  * do jobs stuck at `working` (no process) ever settle?
  * can the control plane rebuild its view purely from its own store + reconcile?
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
from adapters.codebuddy.client import Api, write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 60 /nobreak >nul & echo survived > restart_target.txt"


def cli(*args: str) -> str:
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "serve_ctl.py"), *args],
                       capture_output=True, text=True, timeout=180)
    return (p.stdout or p.stderr).strip()


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def main() -> int:
    adapter = CodeBuddyAdapter()
    store = TaskStore(ROOT / ".poc-state" / "t08.json")
    plane = ControlPlane(adapter, store)
    log = {"test": "T08-service-restart"}

    rec = plane.dispatch(CMD, CWD, assertions=[ArtifactAssertion("file_exists", "restart_target.txt")],
                         mode="shell", title="T08-restart-subject", idempotency_key="t08")
    job_id = rec["worker_job_id"]
    log["job"] = {"task_id": rec["task_id"], "worker_job_id": job_id}
    time.sleep(6)
    before = adapter.get_task(job_id)
    log["before_restart"] = {"state": before.state.value, "pid": before.pid,
                             "pid_alive": pid_alive(before.pid) if before.pid else None,
                             "worker_state": before.metadata["codebuddy"]["state"]}

    # capture the pre-existing "stuck" jobs so their fate after restart is recorded
    stuck = [{"id": j["id"], "state": j.get("state"), "alive": j.get("alive"),
              "settled": j.get("settled")}
             for j in adapter.api.jobs() if j.get("state") == "working"]
    log["stuck_jobs_before"] = stuck

    log["stop"] = cli("stop")
    time.sleep(3)
    log["server_down"] = cli("status")
    log["pid_alive_while_down"] = pid_alive(before.pid) if before.pid else None

    log["start"] = cli("start")
    time.sleep(5)

    api2 = Api()
    api2.login()
    adapter2 = CodeBuddyAdapter(api2)
    log["after_restart_job_lookup"] = {"http": api2.get_job(job_id)["status"]}
    j2 = api2.job(job_id)
    log["after_restart"] = {"state": j2.get("state"), "settled": j2.get("settled"),
                            "alive": j2.get("alive"), "session_id": j2.get("sessionId")}
    log["pid_alive_after_restart"] = pid_alive(before.pid) if before.pid else None

    # let the old command finish and see whether the restarted service settles it
    t0 = time.time()
    while time.time() - t0 < 150:
        j = api2.job(job_id)
        if j.get("settled"):
            break
        time.sleep(4)
    j = api2.job(job_id)
    log["after_wait"] = {"state": j.get("state"), "settled": j.get("settled"),
                         "waited_s": round(time.time() - t0, 1)}
    write_evidence(ROOT / "evidence" / "recovery" / "T08-service-restart-partial.json", log)

    # control-plane side restart: brand new store instance reading the same file
    plane2 = ControlPlane(adapter2, TaskStore(ROOT / ".poc-state" / "t08.json"))
    snapshot = plane2.summary()
    log["control_plane_restart"] = {
        "reconstructed_tasks": snapshot["total"],
        "counts": snapshot["counts"],
        "subject": next((t for t in snapshot["tasks"] if t["worker_job_id"] == job_id), None),
    }

    log["artifact_present"] = (Path(CWD) / "restart_target.txt").exists()
    log["stuck_jobs_after"] = [{"id": j["id"], "state": j.get("state"), "settled": j.get("settled")}
                               for j in api2.jobs() if j.get("state") == "working"
                               or j["id"] in {s["id"] for s in stuck}]
    write_evidence(ROOT / "evidence" / "recovery" / "T08-service-restart.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:2600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
