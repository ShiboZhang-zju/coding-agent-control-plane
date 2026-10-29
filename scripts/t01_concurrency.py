"""T01 + T03 + T04: four concurrent background tasks, isolation and acceptance.

Products:
  evidence/jobs/T01-concurrency.json     dispatch/observe four tasks in two repos
  evidence/isolation/T03-isolation.json  cross-repo write attempt + worktree check
  evidence/jobs/T04-acceptance.json      success/failure acceptance verdicts
  evidence/jobs/T10-summary.json         aggregated master-agent view

Shell-job semantics discovered in T00d: `bash: true` sends the prompt to the
platform shell (cmd.exe here), so commands must be chained with `&`, not `;`,
and `sleep` must be `timeout /t N /nobreak >nul`.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane
from controlplane.policies import agent_worktree, poc_shell  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402

D = "timeout /t {} /nobreak >nul & "
REPO_A = str(ROOT / "sandbox" / "repo-a")
REPO_B = str(ROOT / "sandbox" / "repo-b")

TASKS = [
    dict(key="A1", cwd=REPO_A, title="A1-commit-artifact",
         prompt=(D.format(30) + "echo a1-ok > a1.txt & git add a1.txt & "
                 "git commit -m A1_artifact"),
         assertions=[ArtifactAssertion("file_exists", "a1.txt"),
                     ArtifactAssertion("file_contains", "a1.txt", "a1-ok"),
                     ArtifactAssertion("git_commit", "HEAD", "A1_artifact")]),
    dict(key="A2", cwd=REPO_A, title="A2-intentional-failure",
         prompt=D.format(32) + "echo partial > a2_partial.txt & exit 3",
         assertions=[ArtifactAssertion("file_exists", "a2_expected.txt")]),
    dict(key="B1", cwd=REPO_B, title="B1-cross-repo-write",
         prompt=D.format(35) + "echo b1-ok > b1.txt & "
                "echo cross > ../repo-a/cross_from_b.txt",
         assertions=[ArtifactAssertion("file_exists", "b1.txt")]),
    dict(key="B2", cwd=REPO_B, title="B2-commit-artifact",
         prompt=(D.format(40) + "echo b2-ok > b2.txt & git add b2.txt & "
                 "git commit -m B2_artifact"),
         assertions=[ArtifactAssertion("file_exists", "b2.txt"),
                     ArtifactAssertion("git_commit", "HEAD", "B2_artifact")]),
]


def main() -> int:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sandbox.py")], check=True)
    adapter = CodeBuddyAdapter()
    store = TaskStore(ROOT / ".poc-state" / "t01.json")
    store.clear()
    store.save()
    plane = ControlPlane(adapter, store)
    log = {"test": "T01-concurrency", "started_at": time.time(), "dispatches": [], "timeline": []}

    for spec in TASKS:
        t0 = time.time()
        rec = plane.dispatch(spec["prompt"], spec["cwd"], assertions=spec["assertions"],
                             policy=poc_shell(), title=spec["title"],
                             idempotency_key=f"t01-{spec['key']}")
        jid = rec.get("worker_job_id")
        snap = adapter.get_task(jid) if jid else None
        log["dispatches"].append({
            "key": spec["key"], "task_id": rec["task_id"], "worker_job_id": jid,
            "control_plane_task_id": rec["task_id"],
            "dispatch_http": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("dispatch_http"),
            "recovered_by": (rec.get("metadata") or {}).get("dispatch_raw", {}).get("recovered_by"),
            "warnings": rec.get("warnings"),
            "cwd": spec["cwd"],
            "worktree": snap.worktree if snap else None,
            "session_id": snap.session_id if snap else None,
            "pid": snap.pid if snap else None,
            "branch": _git(spec["cwd"], "rev-parse", "--abbrev-ref", "HEAD"),
            "dispatch_at": t0, "dispatch_returned": time.time(),
        })
        print(f"dispatched {spec['key']} task={rec['task_id']} job={jid} "
              f"http={(rec.get('metadata') or {}).get('dispatch_raw', {}).get('dispatch_http')}")
        time.sleep(1.0)     # serialized dispatch: avoids the worker's lock-contention 500s

    # ---- observe until every task is terminal --------------------------
    deadline = time.time() + 240
    sequences: dict[str, list[str]] = {}
    while time.time() < deadline:
        rows, all_terminal = [], True
        for d in log["dispatches"]:
            if not d["worker_job_id"]:
                all_terminal = False
                continue
            snap = adapter.get_task(d["worker_job_id"])
            d["state"] = snap.state.value
            d["detail"] = snap.detail
            all_terminal = all_terminal and snap.state.terminal
            rows.append({"t": round(time.time() - log["started_at"], 1), "key": d["key"],
                         "state": snap.state.value,
                         "alive": snap.metadata["codebuddy"]["alive"]})
            sequences.setdefault(d["key"], []).append(snap.state.value)
        if not log["timeline"] or log["timeline"][-1]["rows"] != rows:
            log["timeline"].append({"rows": rows})
        if all_terminal and len(log["dispatches"]) == len(TASKS):
            break
        time.sleep(1.0)

    log["state_sequences"] = {k: _dedupe(v) for k, v in sequences.items()}
    log["overlap"] = _overlap(log["timeline"])

    for d in log["dispatches"]:
        if not d["worker_job_id"]:
            d["final_state"] = "not_created"
            continue
        snap = adapter.get_task(d["worker_job_id"])
        d["final_state"] = snap.state.value
        d["finished_at"] = snap.finished_at
        d["duration_s"] = (round(snap.finished_at - snap.started_at, 1)
                           if snap.started_at and snap.finished_at else None)
        d["commit"] = _git(d["cwd"], "log", "-1", "--oneline")

    refreshed = plane.refresh()
    for d in log["dispatches"]:
        rec = plane.store.get(d["task_id"])
        d["verdict"] = rec["verdict"]
        d["assertion_results"] = rec["assertion_results"]
        d["artifacts"] = rec["artifacts"]
        d["git_after"] = {"log": _git(d["cwd"], "log", "--oneline", "-3"),
                          "status": _git(d["cwd"], "status", "--porcelain")}

    cross = Path(REPO_A) / "cross_from_b.txt"
    isolation = {
        "test": "T03-isolation",
        "repos": {"repo_a": REPO_A, "repo_b": REPO_B},
        "cross_repo_write_attempted_by": "B1 (dispatched in repo-b)",
        "cross_repo_write_landed": cross.exists(),
        "cross_repo_path": str(cross),
        "verdict": ("file isolation between two projects is NOT enforced for shell jobs: "
                    "a job dispatched in repo-b wrote into repo-a" if cross.exists()
                    else "cross-repo write was blocked by the worker"),
        "repo_a_status": _git(REPO_A, "status", "--porcelain"),
        "repo_b_status": _git(REPO_B, "status", "--porcelain"),
        "repo_a_log": _git(REPO_A, "log", "--oneline", "-3"),
        "repo_b_log": _git(REPO_B, "log", "--oneline", "-3"),
        "worktree_isolated": [{"key": d["key"], "worktree": d["worktree"]} for d in log["dispatches"]],
    }
    log["isolation"] = isolation
    log["acceptance"] = [{"key": d["key"], "worker_state": d["final_state"],
                          "verdict": d["verdict"], "assertions": d["assertion_results"]}
                         for d in log["dispatches"]]

    write_evidence(ROOT / "evidence" / "jobs" / "T01-concurrency.json", log)
    write_evidence(ROOT / "evidence" / "isolation" / "T03-isolation.json", isolation)
    write_evidence(ROOT / "evidence" / "jobs" / "T04-acceptance.json",
                   {"test": "T04-acceptance", "tasks": log["acceptance"],
                    "counts": refreshed["tasks"]["counts"]})
    write_evidence(ROOT / "evidence" / "jobs" / "T10-summary.json", refreshed)

    for d in log["dispatches"]:
        print(f"{d['key']}: job={d['worker_job_id']} pid={d['pid']} state={d['final_state']} "
              f"verdict={d['verdict']} dur={d['duration_s']}s "
              f"asserts={[(r['kind'], r['passed']) for r in d['assertion_results']]}")
    print("overlap:", log["overlap"])
    print("sequences:", log["state_sequences"])
    print("cross_repo_write_landed:", cross.exists())
    print("counts:", refreshed["tasks"]["counts"])
    print("next_actions:", refreshed["tasks"]["next_actions"])
    return 0


def _dedupe(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(s)
    return out


def _overlap(timeline: list[dict]) -> dict:
    peak = 0
    for frame in timeline:
        n = len([r for r in frame["rows"] if r["state"] in ("running", "queued", "starting")])
        peak = max(peak, n)
    return {"peak_concurrent_running": peak, "concurrent": peak >= 2, "frames": len(timeline)}


def _git(cwd: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20)
    return (p.stdout or p.stderr).strip()


if __name__ == "__main__":
    sys.exit(main())
