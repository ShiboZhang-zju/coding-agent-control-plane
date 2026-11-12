"""Does ``--no-user-settings`` disable the permissions written into config-dir?

Production starts the Worker through ``serve_ctl.py start --no-user-settings``,
which appends ``--setting-sources project,local``.  The least-privilege profile
lives in ``$CODEBUDDY_CONFIG_DIR/settings.json``.  If that file counts as a
"user" setting source, disabling user settings would silently drop the profile
and every job would fall back to the intermittent default classifier.

Two runs, same isolated config dir, same job text:
  A: with    --setting-sources project,local
  B: without (user settings enabled)

The job asks for one allow-listed command and one denied command, so the output
says which layer was in force.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from adapters.codebuddy import permissions  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402

ENTRY = (Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@tencent-ai"
         / "codebuddy-code" / "bin" / "codebuddy")
BASE = ROOT / ".poc-state" / "setting-sources-probe"

PROMPT = (
    "依次单独执行下面两条命令，每条单独一次调用，不要用 && 或 ; 串联，"
    "并原样报告每条的结果（成功或拒绝原文）：\n"
    "1. `git rev-parse --short HEAD`\n"
    "2. `git clean -fdx`\n"
    "不要把两条命令合并，不要变通。"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, timeout: float = 180.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(1.0)
    return False


def make_repo(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "probe@example.invalid"), ("user.name", "Probe")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "README.md").write_text("probe\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True,
                   capture_output=True)
    return path


def run(tag: str, no_user_settings: bool) -> dict:
    cfg = BASE / tag / "cfg"
    repo = make_repo(BASE / tag / "repo")
    shutil.rmtree(cfg, ignore_errors=True)
    cfg.mkdir(parents=True)
    # Exactly what serve_ctl --worker-profile does.
    permissions.merge_into_settings(permissions.settings_path(cfg))
    port = free_port()
    env = dict(os.environ)
    env["CODEBUDDY_CONFIG_DIR"] = str(cfg)
    env.pop("CODEBUDDY_HTTP_PASSWORD", None)
    env.pop("NODE_OPTIONS", None)
    for key in [k for k in env if k.startswith("CODEBUDDY_SAFE_DELETE")]:
        env.pop(key, None)
    argv = ["node", str(ENTRY), "--serve", "--port", str(port), "--host", "127.0.0.1",
            "--auth", "password", "--permission-mode", "default"]
    if no_user_settings:
        argv += ["--setting-sources", "project,local"]
    log = cfg.parent / "serve.log"
    proc = subprocess.Popen(argv, stdout=open(log, "ab"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env,
                            creationflags=(subprocess.DETACHED_PROCESS
                                           | subprocess.CREATE_NEW_PROCESS_GROUP))
    try:
        if not wait_port(port):
            return {"tag": tag, "error": "server did not start"}
        password = None
        for _ in range(120):
            try:
                password = (json.loads((cfg / "settings.json").read_text(
                    encoding="utf-8")).get("gateway") or {}).get("password")
            except (OSError, ValueError):
                password = None
            if password:
                break
            time.sleep(1.0)
        api = Api(base="http://127.0.0.1:%d" % port, password=password or "")
        api.login()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        result = api.dispatch(prompt=PROMPT, cwd=str(repo), permissionMode="acceptEdits")
        job_id = ((result.get("body") or {}).get("data") or {}).get("id")
        if not job_id:
            jobs = api.jobs()
            job_id = jobs[-1]["id"] if jobs else None
        job: dict = {}
        deadline = time.time() + 900
        while time.time() < deadline and job_id:
            job = api.job(job_id)
            if job.get("state") in ("done", "failed", "stopped"):
                break
            time.sleep(5)
        output = str((job.get("output") or {}).get("result") or "")
        return {
            "tag": tag,
            "no_user_settings": no_user_settings,
            "job_id": job_id,
            "state": job.get("state"),
            "git_head": head,
            "allowlisted_git_ran": head in output,
            "denied_git_clean_warned": "git clean" in output.lower()
            and any(word in output.lower() for word in
                    ("denied", "refused", "拒绝", "not allowed")),
            "denial_detected": permissions.detect_permission_denial(output) is not None,
            "output_excerpt": output[:900],
        }
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"],
                       capture_output=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    evidence = {}
    for tag, flag in (("with-no-user-settings", True), ("with-user-settings", False)):
        print("running", tag, flush=True)
        evidence[tag] = run(tag, flag)
        print(json.dumps({k: v for k, v in evidence[tag].items()
                          if k != "output_excerpt"}, ensure_ascii=False, indent=1),
              flush=True)
    out = ROOT / "evidence" / "permissions" / "T70-setting-sources-probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(evidence, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print("evidence:", out)
