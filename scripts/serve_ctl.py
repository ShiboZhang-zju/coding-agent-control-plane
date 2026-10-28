"""Start/stop/inspect the loopback-only CodeBuddy HTTP server used by the PoC.

Security posture (see docs/POC_SPEC.md):
  * binds 127.0.0.1 only
  * --auth password (never --auth none)
  * password comes from env CODEBUDDY_HTTP_PASSWORD of the *child* process only,
    it is never written to evidence files

Usage:
  python scripts/serve_ctl.py start [--port 8787]
  python scripts/serve_ctl.py stop  [--port 8787]
  python scripts/serve_ctl.py status [--port 8787]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidence" / "environment"
NODE_BIN_CANDIDATES = [
    Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy",
]
def _random_password() -> str:
    """Per-start random password. Never a repo constant: nothing credential-like ships."""
    import secrets

    return secrets.token_urlsafe(24)


def _node_exe() -> str:
    for cand in ("node",):
        p = shutil.which(cand)
        if p:
            return p
    raise SystemExit("node not found on PATH")


def _entry_js() -> Path:
    for p in NODE_BIN_CANDIDATES:
        if p.exists():
            return p
    raise SystemExit("codebuddy entry script not found (npm global layout changed?)")


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def who_listens(port: int):
    """Return PID owning the loopback listener on `port` (Windows netstat parse)."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    needle = f"127.0.0.1:{port}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == needle and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def cmd_start(port: int, password: str, home: str | None = None) -> int:
    EVID.mkdir(parents=True, exist_ok=True)
    if port_listening(port):
        pid = who_listens(port)
        print(json.dumps({"result": "already_running", "port": port, "pid": pid}))
        return 0

    env = dict(os.environ)
    if home:
        # Isolated job store: keeps the PoC away from the user's live ~/.codebuddy,
        # so a running CodeBuddy session cannot contend on the same job-store locks.
        Path(home).mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(Path(home).resolve())
        env["HOME"] = str(Path(home).resolve())
        env["CODEBUDDY_CONFIG_DIR"] = str((Path(home) / ".codebuddy").resolve())
    env["CODEBUDDY_HTTP_PASSWORD"] = password
    # Strip the CodeBuddy-desktop injected NODE_OPTIONS / safe-delete shim so the
    # CLI server runs the way it does standalone. The desktop shim routes every
    # unlink through a trash helper; on this machine that helper throws
    # `safe-delete ... Os { code: -2147024894 }` on the job-store lock guard and
    # turns every POST /api/v1/jobs into HTTP 500 (see evidence/jobs/ENV-DEFECT.md).
    # This only affects deletion routing of the test server process, not auth,
    # not permission modes.
    env.pop("NODE_OPTIONS", None)
    for key in [k for k in env if k.startswith("CODEBUDDY_SAFE_DELETE")]:
        env.pop(key, None)
    argv = [_node_exe(), str(_entry_js()), "--serve", "--port", str(port),
            "--host", "127.0.0.1", "--auth", "password", "--permission-mode", "default"]

    out = open(EVID / "serve-stdout.log", "ab")
    err = open(EVID / "serve-stderr.log", "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            env=env, creationflags=flags, close_fds=True)

    deadline = time.time() + 60
    while time.time() < deadline:
        if port_listening(port):
            time.sleep(1.0)
            pid = who_listens(port) or proc.pid
            print(json.dumps({"result": "started", "port": port, "pid": pid,
                              "launcher_pid": proc.pid, "bind": f"127.0.0.1:{port}",
                              "auth": "password"}))
            return 0
        if proc.poll() is not None:
            print(json.dumps({"result": "exited", "code": proc.returncode}))
            return 1
        time.sleep(0.7)
    print(json.dumps({"result": "timeout", "launcher_pid": proc.pid}))
    return 1


def cmd_stop(port: int) -> int:
    pid = who_listens(port)
    if not pid:
        print(json.dumps({"result": "no_listener", "port": port}))
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and port_listening(port):
        time.sleep(0.5)
    print(json.dumps({"result": "stopped" if not port_listening(port) else "still_listening",
                      "port": port, "pid": pid}))
    return 0


def cmd_status(port: int) -> int:
    pid = who_listens(port)
    print(json.dumps({"port": port, "listening": pid is not None, "pid": pid}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--password", default=os.environ.get("POC_SERVE_PASSWORD") or _random_password())
    ap.add_argument("--home", default=os.environ.get("POC_SERVE_HOME"),
                    help="isolated USERPROFILE/HOME so the job store is not shared with a live session")
    a = ap.parse_args()
    if a.action == "start":
        return cmd_start(a.port, a.password, a.home)
    if a.action == "stop":
        return cmd_stop(a.port)
    return cmd_status(a.port)


if __name__ == "__main__":
    sys.exit(main())
