"""Run a long PoC test detached from the calling shell.

Long tests (multi-minute dispatches) must survive an interactive shell being
closed or interrupted; every run writes its own log under `.poc-state/logs/`.

Usage:
  python scripts/detached.py start t01 scripts/t01_concurrency.py
  python scripts/detached.py status t01
  python scripts/detached.py tail t01
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
__import__("sys").path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # repo root on path
import winproc  # noqa: E402  (hidden console children)

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".poc-state" / "logs"


def pid_file(name: str) -> Path:
    return LOGS / f"{name}.pid"


def log_file(name: str) -> Path:
    return LOGS / f"{name}.log"


def is_alive(pid: int) -> bool:
    out = winproc.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out and "python" in out.lower()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    action, name = sys.argv[1], sys.argv[2]
    LOGS.mkdir(parents=True, exist_ok=True)

    if action == "start":
        script = sys.argv[3] if len(sys.argv) > 3 else ""
        target = ROOT / script
        if not target.exists():
            print(json.dumps({"error": f"script not found: {target}"}))
            return 1
        log = open(log_file(name), "wb")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        si = None
        desktop = os.environ.get("POC_HIDDEN_DESKTOP")
        if desktop and os.name == "nt":
            # Run the test script on the same hidden desktop as the worker: every
            # git.exe / tasklist.exe it spawns then creates its console there too,
            # instead of flashing on the user's screen.
            flags |= subprocess.CREATE_NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.lpDesktop = desktop
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
        proc = winproc.popen([sys.executable, "-u", str(target)], cwd=str(ROOT),
                                stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags,
                                startupinfo=si)
        pid_file(name).write_text(str(proc.pid), encoding="utf-8")
        print(json.dumps({"result": "started", "name": name, "pid": proc.pid,
                          "log": str(log_file(name))}))
        return 0

    if action == "status":
        pf = pid_file(name)
        pid = int(pf.read_text(encoding="utf-8")) if pf.exists() else None
        print(json.dumps({"name": name, "pid": pid,
                          "alive": bool(pid and is_alive(pid)),
                          "log_bytes": log_file(name).stat().st_size if log_file(name).exists() else 0}))
        return 0

    if action == "tail":
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 40
        text = log_file(name).read_text(encoding="utf-8", errors="replace") if log_file(name).exists() else ""
        chunk = "\n".join(text.splitlines()[-n:])
        sys.stdout.buffer.write(chunk.encode("utf-8", errors="replace") + b"\n")
        return 0

    print(json.dumps({"error": f"unknown action {action}"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
