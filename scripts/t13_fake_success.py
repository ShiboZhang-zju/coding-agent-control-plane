"""T13: acceptance must reject three kinds of "looks successful" attempts.

Design note: this test drives the verifier deterministically instead of waiting
for worker terminal states. Earlier session showed shell jobs can stay at
`working` forever even after their command completed (ENV-DEFECT.md §3), which
would make a worker-driven test inconclusive rather than wrong. The verifier is
fed a `state=succeeded` snapshot — the most favourable case a worker can claim —
and must still reject:

  1. an artifact that already existed before the attempt (old artifact),
  2. an artifact produced by another attempt (foreign artifact),
  3. a commit that was never created.

Plus one positive control so the checker is not "always reject".
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion, TaskSnapshot, TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from controlplane.verify import capture_baseline  # noqa: E402
__import__("sys").path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))  # repo root on path
import winproc  # noqa: E402  (hidden console children)

CWD = ROOT / "sandbox" / "repo-a"


class StubAdapter:
    """No worker involved: only collects artifacts."""

    name = "stub"

    def collect_artifacts(self, task_id):
        return []

    def reconcile(self):
        return {"source": "stub", "total": 0, "new": [], "changed": [], "lost": [], "jobs": []}


def snapshot(task_id: str, state: TaskState = TaskState.SUCCEEDED) -> TaskSnapshot:
    return TaskSnapshot(task_id=task_id, worker_job_id="stub-" + task_id, state=state,
                        cwd=str(CWD), detail="stub")


def run_case(plane: ControlPlane, store: TaskStore, name: str,
             assertions: list[ArtifactAssertion], prepare_before=None,
             prepare_during=None) -> dict:
    """baseline -> (attempt runs) -> verify.

    `prepare_before` simulates state that existed before the attempt started;
    `prepare_during` simulates what the attempt itself produced.
    """
    if prepare_before:
        prepare_before()
    task_id = store.new_task_id()
    store.create({
        "task_id": task_id, "title": name, "prompt": "stub", "cwd": str(CWD),
        "mode": "agent", "isolation": "none", "idempotency_key": f"t13-{name}",
        "assertions": [a.to_dict() for a in assertions],
        "baseline": capture_baseline(str(CWD), assertions),
        "state": TaskState.QUEUED.value, "verdict": Verdict.PENDING.value,
    })
    if prepare_during:
        prepare_during()
    store.update(task_id, **plane._snapshot_patch(task_id, snapshot(task_id)))
    rec = store.get(task_id)
    return {"case": name, "verdict": rec["verdict"],
            "assertions": [{k: r[k] for k in ("kind", "target", "passed", "owned", "observed")}
                           for r in rec["assertion_results"]]}


def git(*args: str) -> str:
    p = winproc.run(["git", "-C", str(CWD), *args], capture_output=True, text=True, timeout=25)
    return (p.stdout or p.stderr).strip()


def main() -> int:
    winproc.run([sys.executable, str(ROOT / "scripts" / "make_sandbox.py")], check=True)
    store = TaskStore(ROOT / ".poc-state" / "t13.db")
    store.clear()
    plane = ControlPlane(StubAdapter(), store)
    log = {"test": "T13-fake-success", "cwd": str(CWD), "cases": []}

    # 1) old artifact: file written *before* the baseline is captured
    stale = CWD / "stale.txt"
    stale.write_text("from an earlier run\n", encoding="utf-8")
    log["cases"].append(run_case(plane, store, "old_artifact",
                                 [ArtifactAssertion("file_exists", "stale.txt")]))

    # 2) foreign artifact: produced by another attempt, then a new work item
    #    declares it as its own output (baseline already contains it)
    foreign = run_case(plane, store, "foreign_artifact",
                       [ArtifactAssertion("file_exists", "stale.txt")])
    log["cases"].append(foreign)

    # 3) commit that never existed
    log["cases"].append(run_case(plane, store, "missing_commit",
                                 [ArtifactAssertion("git_commit", "HEAD", "T13_never_made")]))

    # 4) positive control: artifact really produced after the baseline
    def make_new() -> None:
        (CWD / "fresh.txt").write_text("produced now\n", encoding="utf-8")

    log["cases"].append(run_case(plane, store, "fresh_artifact",
                                 [ArtifactAssertion("file_exists", "fresh.txt")],
                                 prepare_during=make_new))

    # 5) positive control for commits: commit made after the baseline
    def make_commit() -> None:
        (CWD / "committed.txt").write_text("committed\n", encoding="utf-8")
        git("add", "committed.txt")
        git("-c", "user.name=poc", "-c", "user.email=poc@local", "commit", "-q", "-m", "T13_real_commit")

    log["cases"].append(run_case(plane, store, "fresh_commit",
                                 [ArtifactAssertion("git_commit", "HEAD", "T13_real_commit")],
                                 prepare_during=make_commit))

    by_name = {c["case"]: c for c in log["cases"]}
    log["pass"] = {
        "old_artifact_rejected": by_name["old_artifact"]["verdict"] == "rejected",
        "foreign_artifact_rejected": by_name["foreign_artifact"]["verdict"] == "rejected",
        "missing_commit_rejected": by_name["missing_commit"]["verdict"] == "rejected",
        "fresh_artifact_verified": by_name["fresh_artifact"]["verdict"] == "verified",
        "fresh_commit_verified": by_name["fresh_commit"]["verdict"] == "verified",
    }
    log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
    log["note"] = ("所有 case 都以 worker 自报 state=succeeded 喂给验收器；"
                   "拒绝只可能来自产物归属判定，而不是 worker 状态")
    write_evidence(ROOT / "evidence" / "jobs" / "T13-fake-success.json", log)
    print(json.dumps(log, ensure_ascii=False, indent=1)[:2400])
    return 0


def write_evidence(path: Path, payload) -> None:
    from adapters.codebuddy.client import write_evidence as _w

    _w(path, payload)


if __name__ == "__main__":
    sys.exit(main())
