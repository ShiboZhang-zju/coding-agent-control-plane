"""T11: four *agent* jobs in parallel, each in its own worktree.

This is the acceptance item the first PoC could not claim: T01 proved four
concurrent **shell** jobs, not four concurrent LLM coding agents. Requirements:

  * 4 independent worker jobs (agent sessions, not shell one-liners),
  * 4 independent git worktrees / branches,
  * 4 independent artifacts whose ownership is verifiable,
  * no cross-worktree contamination.
"""
from __future__ import annotations

import json
import os
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
from controlplane.policies import agent_worktree  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
__import__("sys").path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # repo root on path
import winproc  # noqa: E402  (hidden console children)

def job_spec(name: str, cwd: str, model: str | None = None) -> dict:
    return dict(
        name=name,
        cwd=cwd,
        model=model,
        prompt=(f"请在这个仓库的根目录创建一个新文件 {name}.txt，内容为一行 {name}。"
                f"只做这一步，不要修改其他文件，完成后结束任务。"),
        assertions=[ArtifactAssertion("file_exists", f"{name}.txt")],
    )


def git(cwd: str, *args: str) -> str:
    p = winproc.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=25)
    return (p.stdout or p.stderr).strip()


def compute_overlap(tasks: list[dict]) -> dict:
    """Concurrency from worker timestamps (immune to sampling gaps)."""
    spans = [(t["name"], t.get("started_at"), t.get("finished_at") or time.time())
             for t in tasks if t.get("started_at")]
    events = []
    for name, start, end in spans:
        events.append((start, +1, name))
        events.append((end, -1, name))
    events.sort()
    cur = peak = 0
    peak_at = None
    for ts, delta, _ in events:
        cur += delta
        if cur > peak:
            peak, peak_at = cur, ts
    return {"peak": peak, "peak_at": peak_at,
            "spans": [{"name": n, "start": s, "end": e} for n, s, e in spans]}


def main() -> int:
    suffix = "t11-" + time.strftime("%H%M%S")
    winproc.run([sys.executable, str(ROOT / "scripts" / "make_sandbox.py"), suffix], check=True)
    sb = json.loads((ROOT / ".poc-state" / "sandbox.json").read_text(encoding="utf-8"))
    repo_a, repo_b = sb["repo_a"], sb["repo_b"]
    model = os.environ.get("POC_T11_MODEL") or None
    specs = [job_spec("agent-a1", repo_a, model), job_spec("agent-a2", repo_a, model),
             job_spec("agent-b1", repo_b, model), job_spec("agent-b2", repo_b, model)]
    lanes = int(os.environ.get("POC_T11_TASKS", "4"))
    if lanes < 4:
        # provider concurrency on this account is ~2 (see T11-4way-provider-blocked),
        # so the reproducible concurrency test runs 2 lanes
        specs = [specs[0], specs[2]][:lanes]

    adapter = CodeBuddyAdapter()
    store = TaskStore(ROOT / ".poc-state" / "t11.json")
    store.clear()
    plane = ControlPlane(adapter, store, policy=agent_worktree())

    log = {"test": "T11-four-agents", "started_at": time.time(), "sandbox": sb, "tasks": []}
    try:
        log["cli_version"] = winproc.run(["codebuddy", "--version"], capture_output=True,
                                            text=True, timeout=30).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        log["cli_version"] = f"unknown ({type(exc).__name__})"
    log["model"] = os.environ.get("POC_T11_MODEL") or "default"

    # Dispatch in parallel: a serial loop would let the first agent finish before
    # the last one starts, which would prove nothing about concurrency.
    from concurrent.futures import ThreadPoolExecutor

    def one(spec: dict) -> dict:
        rec = plane.dispatch(spec["prompt"], spec["cwd"], assertions=spec["assertions"],
                             title=f"T11-{spec['name']}",
                             metadata={"model": spec["model"]} if spec.get("model") else None,
                             idempotency_key=f"t11-{spec['name']}-{suffix}")
        return {"name": spec["name"], "task_id": rec["task_id"],
                "worker_job_id": rec.get("worker_job_id"),
                "dispatch_http": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("dispatch_http"),
                "policy": rec.get("policy"), "state": rec.get("state"),
                "dispatched_at": time.time()}

    if os.environ.get("POC_T11_SERIAL") == "1":
        # Serial *submission*, concurrent *execution*: this is the shape that works
        # on this machine while the UserPromptSubmit hook has a 5s timeout
        # (evidence/isolation/T11-parallel-blocked.md).
        log["submission"] = "serial"
        for spec in specs:
            item = one(spec)
            log["tasks"].append(item)
            print("dispatched", item["name"], item["task_id"], item.get("worker_job_id"))
    else:
        log["submission"] = "parallel"
        with ThreadPoolExecutor(max_workers=4) as pool:
            for item in pool.map(one, specs):
                log["tasks"].append(item)
                print("dispatched", item["name"], item["task_id"], item.get("worker_job_id"))

    # ---- observe -------------------------------------------------------
    deadline = time.time() + 330
    frames: list[dict] = []
    while time.time() < deadline:
        rows, all_terminal = [], True
        for t in log["tasks"]:
            if not t["worker_job_id"]:
                all_terminal = False
                continue
            snap = adapter.get_task(t["worker_job_id"])
            t["state"] = snap.state.value
            t["worktree"] = snap.worktree
            t["session_id"] = snap.session_id
            t["pid"] = snap.pid
            t["started_at"] = snap.started_at
            t["finished_at"] = snap.finished_at
            t["detail"] = snap.detail
            all_terminal = all_terminal and snap.state.terminal
            rows.append({"name": t["name"], "state": snap.state.value})
        frames.append({"t": round(time.time() - log["started_at"], 1),
                       "states": {r["name"]: r["state"] for r in rows},
                       "running": len([r for r in rows if r["state"] == "running"])})
        if all_terminal:
            break
        time.sleep(2)

    log["timeline"] = frames
    plane.refresh()

    # ---- per-task facts ------------------------------------------------
    worktrees = []
    for t in log["tasks"]:
        rec = store.get(t["task_id"]) or {}
        wt = rec.get("worktree") or t.get("worktree")
        t.update(verdict=rec.get("verdict"), state=rec.get("state"),
                 worktree=wt, assertions=rec.get("assertion_results"),
                 artifacts=rec.get("artifacts"), warnings=rec.get("warnings"),
                 attempts=[a["worker_job_id"] for a in store.attempts(t["task_id"])])
        if wt:
            worktrees.append({"name": t["name"], "worktree": wt,
                              "branch": git(wt, "rev-parse", "--abbrev-ref", "HEAD"),
                              "files": sorted(p.name for p in Path(wt).iterdir()
                                              if p.is_file() and p.name.endswith(".txt")),
                              "own_artifact_present": (Path(wt) / f"{t['name']}.txt").exists(),
                              "foreign_artifacts": [p.name for p in Path(wt).iterdir()
                                                    if p.is_file() and p.name.endswith(".txt")
                                                    and not p.name.startswith(t["name"])]})
    log["worktrees"] = worktrees
    log["main_checkouts"] = {
        "repo_a_status": git(repo_a, "status", "--porcelain"),
        "repo_b_status": git(repo_b, "status", "--porcelain"),
        "repo_a_branch": git(repo_a, "rev-parse", "--abbrev-ref", "HEAD"),
        "repo_b_branch": git(repo_b, "rev-parse", "--abbrev-ref", "HEAD"),
    }

    running_states = [f["running"] for f in frames]
    independent = {w["worktree"] for w in worktrees}
    overlap = compute_overlap(log["tasks"])
    log["overlap"] = overlap
    def tracked(status: str) -> str:
        return "\n".join(ln for ln in status.splitlines()
                         if not ln[3:].strip().replace("\\", "/").startswith(".codebuddy/"))

    log["result"] = {
        "peak_concurrent_running": max(running_states) if running_states else 0,
        "peak_from_timestamps": overlap["peak"],
        "distinct_worktrees": len(independent),
        "all_have_own_artifact": all(w["own_artifact_present"] for w in worktrees) if worktrees else False,
        "no_foreign_artifacts": all(not w["foreign_artifacts"] for w in worktrees),
        "all_verified": all(t.get("verdict") == "verified" for t in log["tasks"]),
        "verified_count": len([t for t in log["tasks"] if t.get("verdict") == "verified"]),
        "failed_count": len([t for t in log["tasks"] if t.get("state") == "failed"]),
        "main_checkout_untouched": (tracked(log["main_checkouts"]["repo_a_status"]) == ""
                                    and tracked(log["main_checkouts"]["repo_b_status"]) == ""),
    }
    write_evidence(ROOT / "evidence" / "isolation" / "T11-four-agents.json", log)
    write_evidence(ROOT / "evidence" / "jobs" / "T11-four-agents-summary.json", plane.summary())
    print(json.dumps(log["result"], ensure_ascii=False, indent=1))
    for t in log["tasks"]:
        print(t["name"], t["state"], t["verdict"], "wt=", bool(t["worktree"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
