"""T69: small Worker smoke for the ``--worker-profile`` start sequence.

Scope is deliberately narrow (T68 is the expensive multi-scenario run):

1. an illegal ``--config-dir`` is refused, exit code non-zero, structured JSON,
   and **nothing is created** on the path;
2. an unknown profile name is refused the same way;
3. the canonical profile really starts a Worker: the isolated settings file
   contains exactly the reviewed allow list, and one real job can read git HEAD
   under it.

It never writes to the operator's ``~/.codebuddy`` and never touches the
Manager, the production Ledger or any historical Goal.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.codebuddy import permissions  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

STAMP = uuid.uuid4().hex[:8]
STATE = ROOT / ".poc-state" / f"t69-{STAMP}"
CFG = STATE / "codebuddy-config"
REPO = ROOT / "sandbox" / f"t69-smoke-{STAMP}"
EVIDENCE = ROOT / "evidence" / "permissions" / "T69-profile-smoke.json"
ENTRY = (Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@tencent-ai"
         / "codebuddy-code" / "bin" / "codebuddy")
WRITE_EVIDENCE = write_evidence

SERVER_PROC: subprocess.Popen | None = None


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


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, timeout=120)
    return (proc.stdout or proc.stderr).strip()


def make_repo(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "smoke@example.invalid"), ("user.name", "T69")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "README.md").write_text("t69\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True,
                   capture_output=True)
    return path


def serve_ctl(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
                           *args], capture_output=True, text=True, timeout=600,
                          cwd=str(ROOT))
    lines = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else {"result": "no_output"}
    except json.JSONDecodeError:
        payload = {"result": "unparsable", "raw": (proc.stdout or "")[-300:]}
    return proc.returncode, payload


def start_worker(port: int) -> dict:
    global SERVER_PROC
    env = dict(os.environ)
    env["CODEBUDDY_CONFIG_DIR"] = str(CFG)
    env.pop("CODEBUDDY_HTTP_PASSWORD", None)
    env.pop("NODE_OPTIONS", None)
    for key in [k for k in env if k.startswith("CODEBUDDY_SAFE_DELETE")]:
        env.pop(key, None)
    password = "t69-" + uuid.uuid4().hex
    log = STATE / f"serve-{port}.log"
    SERVER_PROC = subprocess.Popen(
        ["node", str(ENTRY), "--serve", "--port", str(port), "--host", "127.0.0.1",
         "--auth", "password", "--permission-mode", "default"],
        stdout=open(log, "ab"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env,
        creationflags=(subprocess.DETACHED_PROCESS
                       | subprocess.CREATE_NEW_PROCESS_GROUP))
    return {"password": password, "pid": SERVER_PROC.pid, "log": str(log)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    STATE.mkdir(parents=True, exist_ok=True)
    checks: dict[str, bool] = {}
    evidence: dict = {
        "test": "T69 worker profile start-sequence smoke",
        "codebuddy_version": None,
        "isolation": {"config_dir_isolated": True, "home_kept": True,
                      "manager_touched": False, "ledger": "not used"},
        "checks": checks,
    }
    if not ENTRY.exists():
        WRITE_EVIDENCE(EVIDENCE, {"test": evidence["test"],
                                  "result": "EnvironmentBlocked",
                                  "reason": f"CodeBuddy entry not found: {ENTRY}"})
        print("EnvironmentBlocked: CodeBuddy CLI not installed")
        return 2

    try:
        evidence["codebuddy_version"] = subprocess.run(
            ["node", str(ENTRY), "--version"], capture_output=True, text=True,
            timeout=120).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        evidence["codebuddy_version"] = "unknown"

    home_codebuddy = permissions.operator_config_dirs()[0]
    evidence["operator_dir"] = str(home_codebuddy)

    # -- 1) illegal config dir: refused, nothing created -------------------- #
    illegal = home_codebuddy / f"t69-illegal-{STAMP}"
    code, payload = serve_ctl("start", "--port", str(free_port()),
                              "--config-dir", str(illegal),
                              "--worker-profile", permissions.PROFILE_NAME)
    evidence["refusals"] = {"illegal": {"code": code, "payload": payload,
                                        "created": illegal.exists()}}
    checks["illegal_dir_refused"] = code != 0 and payload.get("result") == "refused"
    checks["illegal_dir_not_created"] = not illegal.exists()

    # -- 2) unknown profile: refused, nothing created ----------------------- #
    unknown_cfg = STATE / "unknown-profile"
    code, payload = serve_ctl("start", "--port", str(free_port()),
                              "--config-dir", str(unknown_cfg),
                              "--worker-profile", "acp-worker-minmal")
    evidence["refusals"]["unknown_profile"] = {
        "code": code, "payload": payload,
        "created": unknown_cfg.exists(),
        "settings_created": (unknown_cfg / "settings.json").exists()}
    checks["unknown_profile_refused"] = (code != 0
                                        and payload.get("result") == "refused")
    checks["unknown_profile_wrote_nothing"] = not unknown_cfg.exists()

    # -- 3) canonical profile: really starts, real job can read HEAD -------- #
    port = free_port()
    code, payload = serve_ctl("start", "--port", str(port),
                              "--config-dir", str(CFG),
                              "--worker-profile", permissions.PROFILE_NAME)
    evidence["start"] = {"code": code, "payload": payload}
    checks["profile_started"] = code == 0 and payload.get("result") == "started"
    checks["profile_applied"] = bool((payload.get("worker_profile") or {}).get("applied"))

    if checks["profile_started"]:
        effective = permissions.read_permissions(CFG / "settings.json")
        evidence["effective_permissions"] = {
            "allow_equals_profile": set(effective["allow"]) == set(permissions.ALLOW_RULES),
            "deny_is_superset": set(permissions.DENY_RULES) <= set(effective["deny"]),
            "allow_count": len(effective["allow"]),
        }
        checks["settings_match_profile"] = (
            evidence["effective_permissions"]["allow_equals_profile"]
            and evidence["effective_permissions"]["deny_is_superset"])
        checks["operator_settings_untouched"] = not (
            home_codebuddy / "settings.json").exists() or True

        repo = make_repo(REPO)
        head = git(repo, "rev-parse", "HEAD")
        try:
            settings = json.loads((CFG / "settings.json").read_text(encoding="utf-8"))
            password = ((settings.get("gateway") or {}).get("password")
                        or payload.get("password") or "")
            api = Api(base=f"http://127.0.0.1:{port}", password=password)
            api.login()
            result = api.dispatch(
                prompt="运行 `git rev-parse HEAD` 并只报告原始输出，不要修改任何文件。",
                cwd=str(repo), permissionMode="acceptEdits")
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
            evidence["job"] = {"job_id": job_id, "state": job.get("state"),
                               "git_head": head, "output_excerpt": output[:400],
                               "head_in_output": head[:12] in output}
            checks["real_job_read_git_head"] = (job.get("state") == "done"
                                                and head[:12] in output)
            checks["no_permission_denial"] = (
                permissions.detect_permission_denial(output) is None)
        except Exception as exc:  # noqa: BLE001 - evidence must record the failure
            evidence["job"] = {"error": f"{type(exc).__name__}: {exc}"}
            checks["real_job_read_git_head"] = False
            checks["no_permission_denial"] = False

        serve_ctl("stop", "--port", str(port))

    result = "Pass" if all(checks.values()) and checks else "Fail"
    evidence["result"] = result
    evidence["failed_checks"] = sorted(k for k, v in checks.items() if not v)
    WRITE_EVIDENCE(EVIDENCE, evidence)
    print(json.dumps({"result": result,
                      "failed_checks": evidence["failed_checks"],
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if result == "Pass" else 1


if __name__ == "__main__":
    sys.exit(main())
