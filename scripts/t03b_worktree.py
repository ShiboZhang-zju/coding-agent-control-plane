"""T03b: does an *agent* (model) job run isolated in its own git worktree?

Shell jobs are not sandboxed (T03). Agent jobs can request `bgIsolation=worktree`;
this checks whether a worktree is really created, whether the main checkout stays
clean, and whether the artifact can still be collected from it.
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

REPO_A = str(ROOT / "sandbox" / "repo-a")
PROMPT = ("在仓库根目录创建文件 agent_marker.txt，内容为 agent-ok（一行），"
          "然后立刻结束任务，不要做其他改动，不要提交 git。")


def git(cwd: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=25)
    return (p.stdout or p.stderr).strip()


def main() -> int:
    adapter = CodeBuddyAdapter()
    plane = ControlPlane(adapter, TaskStore(ROOT / ".poc-state" / "t03b.json"))
    log = {"test": "T03b-worktree-isolation", "repo": REPO_A,
           "worktrees_before": git(REPO_A, "worktree", "list"), "samples": []}

    rec = plane.dispatch(PROMPT, REPO_A,
                         assertions=[ArtifactAssertion("file_exists", "agent_marker.txt")],
                         mode="agent", isolation="worktree", title="T03b-worktree-agent",
                         idempotency_key="t03b",
                         metadata={"permissionMode": "acceptEdits"})
    job_id = rec.get("worker_job_id")
    log["dispatch"] = {"task_id": rec["task_id"], "worker_job_id": job_id,
                       "warnings": rec.get("warnings"),
                       "dispatch_http": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("dispatch_http")}

    deadline = time.time() + 420
    while time.time() < deadline:
        snap = adapter.get_task(job_id)
        meta = snap.metadata["codebuddy"]
        row = {"t": round(time.time() - (rec.get("created_at") or time.time()), 1),
               "state": snap.state.value, "worker_state": meta["state"],
               "detail": (snap.detail or "")[:120], "worktree": snap.worktree,
               "pid": snap.pid}
        if not log["samples"] or log["samples"][-1]["worker_state"] != row["worker_state"]:
            log["samples"].append(row)
        if snap.state.terminal:
            break
        time.sleep(4)

    snap = adapter.get_task(job_id)
    log["final"] = {"state": snap.state.value, "detail": snap.detail,
                    "worktree_field": snap.worktree, "session_id": snap.session_id,
                    "pid": snap.pid,
                    "worker_meta": snap.metadata["codebuddy"]}
    log["worktrees_after"] = git(REPO_A, "worktree", "list")
    log["main_checkout_status"] = git(REPO_A, "status", "--porcelain")
    log["main_checkout_has_marker"] = (Path(REPO_A) / "agent_marker.txt").exists()
    log["worktree_paths"] = [ln.split()[0] for ln in log["worktrees_after"].splitlines()[1:]
                             if ln.strip()]

    hit = []
    for wt in log["worktree_paths"]:
        p = Path(wt) / "agent_marker.txt"
        if p.exists():
            hit.append({"path": str(p), "content": p.read_text(encoding="utf-8", errors="replace")[:80]})
    log["marker_in_worktrees"] = hit
    for hp in [Path.home() / ".codebuddy" / "jobs" / job_id / "state.json"]:
        if hp.exists():
            st = json.loads(hp.read_text(encoding="utf-8"))
            log["state_json_worktree_keys"] = {k: v for k, v in st.items()
                                              if "worktree" in k.lower() or k == "cwd"}

    plane.refresh()
    log["verdict"] = plane.store.get(rec["task_id"])["verdict"]
    log["assertion_results"] = plane.store.get(rec["task_id"])["assertion_results"]
    write_evidence(ROOT / "evidence" / "isolation" / "T03b-worktree-agent.json", log)

    print(json.dumps({k: log[k] for k in ("dispatch", "final", "verdict")}, ensure_ascii=False)[:1500])
    print("worktrees_after:", log["worktrees_after"].replace("\n", " | "))
    print("main_checkout_has_marker:", log["main_checkout_has_marker"],
          "marker_in_worktrees:", log["marker_in_worktrees"])
    print("assertions:", log["assertion_results"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
