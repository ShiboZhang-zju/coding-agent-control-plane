"""T08c: precise execution-survival test.

The T08b first attempt was inconclusive: killing the process tree killed
`timeout.exe` but the surrounding cmd continued, so the artifact appeared anyway.
This version uses a 30s `ping` delay and records *when* the artifact first appears
relative to dispatch, which distinguishes:
  * artifact at ~30s  -> the command kept running across the service restart
  * artifact right after restart -> the delay process was killed, rest kept going
  * never -> execution stopped
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
CMD = "ping -n 31 127.0.0.1 >nul & echo late > survival3.txt"


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
    target = Path(CWD) / "survival3.txt"
    if target.exists():
        target.unlink()
    log = {"test": "T08c-exec-survival-precise", "cmd": CMD, "target": str(target)}

    t0 = time.time()
    r = api.dispatch(prompt=CMD, cwd=CWD, bash=True)
    job_id = (r["body"].get("data") or {}).get("id")
    log["job_id"] = job_id
    time.sleep(5)
    state_file = Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"
    rec = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    pid = rec.get("pid")
    log["before_stop"] = {"pid": pid, "pid_alive": pid_alive(pid) if pid else None,
                          "artifact": target.exists(), "t": round(time.time() - t0, 1)}

    log["stop"] = cli("stop")
    log["after_stop"] = {"pid_alive": pid_alive(pid) if pid else None,
                         "t": round(time.time() - t0, 1)}
    log["start"] = cli("start")

    first_seen = None
    while time.time() - t0 < 60:
        if target.exists() and first_seen is None:
            first_seen = round(time.time() - t0, 1)
            break
        time.sleep(1.5)
    log["artifact_first_seen_s"] = first_seen
    time.sleep(2)
    api2 = Api()
    api2.login()
    j = api2.job(job_id)
    log["job_after"] = {"state": j.get("state"), "settled": j.get("settled"),
                        "alive": j.get("alive")}
    log["pid_alive_final"] = pid_alive(pid) if pid else None

    if first_seen is None:
        verdict = "execution stopped (artifact never appeared)"
    elif first_seen >= 25:
        verdict = "execution continued across the restart (artifact at the original delay)"
    else:
        verdict = ("the delay process was killed but the surrounding shell kept going; "
                   "this is not execution recovery")
    log["verdict"] = verdict
    write_evidence(ROOT / "evidence" / "recovery" / "T08c-exec-survival-precise.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
