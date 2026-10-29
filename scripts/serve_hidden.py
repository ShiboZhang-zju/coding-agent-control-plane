"""Start the CodeBuddy HTTP server on a *hidden Windows desktop*.

Why: every console child process the worker spawns (git.exe, bash.exe,
powershell.exe, cmd.exe) gets a conhost, and some of them briefly create a
visible console window on the interactive desktop. `windowsHide` inside the CLI
covers most spawns but not all (ConPTY / Git Bash), and we cannot patch the CLI.

The console window is created on the *desktop of the process that spawns it*.
So if the server itself lives on a non-interactive desktop, every child console
window is created there too and the user never sees it.

Usage:
  python scripts/serve_hidden.py start [--port 8787]
  python scripts/serve_hidden.py stop  [--port 8787]
  python scripts/serve_hidden.py status [--port 8787]

The launcher stays alive on purpose: the desktop object lives as long as some
process holds a handle to it.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".poc-state" / "hidden-serve.json"
EVID = ROOT / "evidence" / "environment"

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

GENERIC_ALL = 0x10000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def entry_js() -> Path:
    p = Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"
    if not p.exists():
        raise SystemExit("codebuddy entry script not found")
    return p


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def who_listens(port: int) -> int | None:
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == f"127.0.0.1:{port}" and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def cmd_start(port: int) -> int:
    if port_listening(port):
        print(json.dumps({"result": "already_running", "pid": who_listens(port)}))
        return 0
    EVID.mkdir(parents=True, exist_ok=True)
    desktop_name = f"poc-hidden-{port}"

    # child: the actual server, pinned to the hidden desktop
    env = dict(os.environ)
    env["CODEBUDDY_HTTP_PASSWORD"] = env.get("POC_SERVE_PASSWORD") or "hidden-" + os.urandom(8).hex()
    si = subprocess.STARTUPINFO()
    si.lpDesktop = desktop_name
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    out = open(EVID / "hidden-serve-stdout.log", "ab")
    err = open(EVID / "hidden-serve-stderr.log", "ab")
    server = subprocess.Popen(
        [shutil.which("node") or "node", str(entry_js()), "--serve", "--port", str(port),
         "--host", "127.0.0.1", "--auth", "password", "--permission-mode", "default"],
        stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
        startupinfo=si, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        close_fds=True)

    # launcher: owns the desktop handle and keeps the desktop object alive
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"port": port, "desktop": desktop_name,
                                 "server_pid": server.pid, "launcher_pid": os.getpid(),
                                 "started_at": time.time()}, indent=2), encoding="utf-8")
    deadline = time.time() + 60
    while time.time() < deadline:
        if port_listening(port):
            break
        if server.poll() is not None:
            print(json.dumps({"result": "exited", "code": server.returncode,
                              "stderr": (EVID / "hidden-serve-stderr.log").read_text(
                                  encoding="utf-8", errors="replace")[-400:]}))
            return 1
        time.sleep(0.7)
    else:
        print(json.dumps({"result": "timeout", "server_pid": server.pid}))
        return 1

    print(json.dumps({"result": "started", "port": port, "desktop": desktop_name,
                      "server_pid": server.pid, "launcher_pid": os.getpid()}))
    sys.stdout.flush()
    # hold the desktop handle until asked to stop
    desktop = user32.OpenDesktopW(desktop_name, 0, False, GENERIC_ALL)
    try:
        while True:
            time.sleep(5)
            if not Path(STATE).exists():
                break
            if (Path(STATE).stat().st_mtime < time.time() - 3600) and not port_listening(port):
                break
    finally:
        if desktop:
            user32.CloseDesktop(desktop)
    return 0


def cmd_stop() -> int:
    info = {}
    if STATE.exists():
        try:
            info = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            info = {}
    pid = info.get("server_pid") or who_listens(info.get("port", 8787))
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    STATE.unlink(missing_ok=True)
    print(json.dumps({"result": "stopped", "pid": pid, "desktop": info.get("desktop")}))
    return 0


def cmd_status(port: int) -> int:
    info = {}
    if STATE.exists():
        try:
            info = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            info = {}
    print(json.dumps({"port": port, "listening": port_listening(port),
                      "pid": who_listens(port), "desktop": info.get("desktop"),
                      "state_file": str(STATE) if STATE.exists() else None}))
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1
              else os.environ.get("POC_HIDDEN_ACTION", "status"))
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if action == "start":
        return cmd_start(port)
    if action == "stop":
        return cmd_stop()
    return cmd_status(port)


if __name__ == "__main__":
    sys.exit(main())
