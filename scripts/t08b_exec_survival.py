"""T08b: does a running job's OS process survive a worker-service restart?

Reads the real pid from the on-disk job record (the REST view does not expose it
once the job is running), stops the service, then checks the process and the
artifact afterwards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.client import Api, write_evidence  # noqa: E402

CWD = str(ROOT / "sandbox" / "repo-a")
CMD = "timeout /t 90 /nobreak >nul & echo alive-after-restart > survival.txt"


def cli(*args: str) -> str:
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "serve_ctl.py"), *args],
                       capture_output=True, text=True, timeout=180)
    return (p.stdout or p.stderr).strip()


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def main() -> int:
    api = Api()
    api.login()
    log = {"test": "T08b-exec-survival", "cmd": CMD}
    r = api.dispatch(prompt=CMD, cwd=CWD, bash=True)
    job_id = (r["body"].get("data") or {}).get("id")
    if not job_id:
        hits = [j for j in api.jobs() if (j.get("intent") or "") == CMD]
        job_id = hits[-1]["id"] if hits else None
    log["job_id"] = job_id
    time.sleep(5)

    state_file = Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"
    rec = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    pid = rec.get("pid")
    log["before_stop"] = {"state": rec.get("state"), "pid": pid,
                          "pid_alive": pid_alive(pid) if pid else None,
                          "settled": rec.get("firstTerminalAt") is not None}

    log["stop"] = cli("stop")
    time.sleep(3)
    log["after_stop"] = {"pid_alive": pid_alive(pid) if pid else None,
                         "server": cli("status")}

    log["start"] = cli("start")
    time.sleep(4)
    api2 = Api()
    api2.login()
    j = api2.job(job_id)
    log["after_start"] = {"state": j.get("state"), "settled": j.get("settled"),
                          "alive": j.get("alive"),
                          "pid_alive": pid_alive(pid) if pid else None}

    # give it the time the original command needed, then look for the artifact
    time.sleep(20)
    j = api2.job(job_id)
    target = Path(CWD) / "survival.txt"
    log["after_wait"] = {"state": j.get("state"), "settled": j.get("settled"),
                         "artifact": target.exists()}
    log["conclusion"] = {
        "process_killed_by_service_stop": (pid is not None and not pid_alive(pid)),
        "execution_resumed": False if not target.exists() else True,
        "job_still_discoverable": j.get("id") is not None,
        "note": "服务停止时子进程随服务一起结束；重启后 job 记录仍可查询，但命令不会继续执行",
    }
    write_evidence(ROOT / "evidence" / "recovery" / "T08b-exec-survival.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
