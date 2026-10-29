"""T14: an attempt that writes into another project must never be `verified`.

A shell job dispatched in repo-b writes its own b-side file **and** touches the
sibling project. Its own artifact assertion passes and the worker reports
`done` — the verdict must still be `rejected` because of the cross-project
side effect.
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
from controlplane.policies import poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
__import__("sys").path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # repo root on path
import winproc  # noqa: E402  (hidden console children)


def git(cwd: Path, *args: str) -> str:
    p = winproc.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=25)
    return (p.stdout or p.stderr).strip()


def main() -> int:
    suffix = "t14-" + time.strftime("%H%M%S")
    winproc.run([sys.executable, str(ROOT / "scripts" / "make_sandbox.py"), suffix], check=True)
    sb = json.loads((ROOT / ".poc-state" / "sandbox.json").read_text(encoding="utf-8"))
    repo_a, repo_b = Path(sb["repo_a"]), Path(sb["repo_b"])
    intruder = repo_a / "intruder.txt"
    cmd = (f'timeout /t 4 /nobreak >nul & echo mine > b_own.txt '
           f'& echo illegal > "{intruder}"')

    adapter = CodeBuddyAdapter()
    store = TaskStore(ROOT / ".poc-state" / "t14.json")
    store.clear()
    plane = ControlPlane(adapter, store, policy=poc_shell())
    log = {"test": "T14-cross-project", "sandbox": sb, "cmd": cmd}

    rec = plane.dispatch_approved(cmd, str(repo_b),
                                  assertions=[ArtifactAssertion("file_exists", "b_own.txt")],
                                  title="T14-cross-project", idempotency_key="t14")
    job_id = rec["worker_job_id"]
    t0 = time.time()
    while time.time() - t0 < 120:
        snap = adapter.get_task(job_id)
        if snap.state.terminal:
            break
        time.sleep(2)
    plane.refresh()
    out = store.get(rec["task_id"])

    log["task"] = {"task_id": rec["task_id"], "worker_job_id": job_id,
                   "worker_state": out["state"], "verdict": out["verdict"],
                   "own_assertions": out["assertion_results"],
                   "warnings": out["warnings"]}
    log["observed"] = {
        "own_file_in_repo_b": (repo_b / "b_own.txt").exists(),
        "intruder_file_in_repo_a": intruder.exists(),
        "repo_a_status": git(repo_a, "status", "--porcelain"),
        "repo_b_status": git(repo_b, "status", "--porcelain"),
    }
    log["pass"] = {
        "own_assertion_passed": all(r["passed"] for r in out["assertion_results"]),
        "cross_project_write_happened": intruder.exists(),
        "verdict_rejected": out["verdict"] == "rejected",
        "violation_recorded": any("SIDE_EFFECT" in w for w in (out["warnings"] or [])),
    }
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    write_evidence(ROOT / "evidence" / "isolation" / "T14-cross-project-rejected.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
