"""T31: M2 Project Context Update Loop.

Cases
-----
A  terminal outcomes automatically create candidate progress/knowledge entries
   with real, sanitized evidence references.
B  replaying the same acceptance never creates a second progress entry or bumps
   the project context version.
C  two proposals prepared from the same CAS version preserve the stale one as a
   separate ``needs_merge`` candidate instead of losing either body.
D  automatic writes cannot create constraints/verified facts and cannot cross a
   project boundary.
E  a terminal verdict committed before its context receipts is recovered by the
   pending-update sweep, then disappears from subsequent sweeps.

The worker is a stub. Repository mutations are confined to
``sandbox/m2-update-loop``; runtime evidence is written under that fixture's own
``evidence/`` tree so committed historical evidence is not overwritten.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (ArtifactAssertion, DispatchPolicy, DispatchResult,  # noqa: E402
                           TaskSnapshot, TaskState)
from controlplane.context_update import (ContextCandidate,  # noqa: E402
                                         ProjectContextUpdateLoop)
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextError  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m2-update-loop"
REPO_A = FIXTURE / "repo-a"
REPO_B = FIXTURE / "repo-b"
AUDIT_ROOT = FIXTURE / "audit-root"
LEDGER = ROOT / ".poc-state" / "t31-context-update.db"
LOW = DispatchPolicy(mode="agent", isolation="worktree")


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                   timeout=30)


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run("git", "init", "-q", cwd=path)
    run("git", "config", "user.email", "t31@example.invalid", cwd=path)
    run("git", "config", "user.name", "T31", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("git", "add", "seed.txt", cwd=path)
    run("git", "commit", "-q", "-m", "seed", cwd=path)


class StubAdapter:
    name = "stub"

    def __init__(self):
        self.calls = 0
        self.jobs: dict[str, dict] = {}

    def dispatch(self, request) -> DispatchResult:
        self.calls += 1
        job = f"job-{self.calls}"
        self.jobs[job] = {
            "task_id": request.task_id,
            "state": TaskState.RUNNING,
            "cwd": request.cwd,
        }
        return DispatchResult(task_id=request.task_id, worker_job_id=job,
                              accepted=True, raw={"dispatch_http": 200})

    def finish(self, job: str, state: TaskState) -> None:
        self.jobs[job]["state"] = state

    def get_task(self, job_id: str, timeout_s: float = 20.0) -> TaskSnapshot:
        job = self.jobs[job_id]
        return TaskSnapshot(
            task_id=job["task_id"], worker_job_id=job_id,
            state=job["state"], cwd=job["cwd"],
            finished_at=time.time() if job["state"].terminal else None,
            detail="t31 stub")

    def collect_artifacts(self, _task_id):
        return []

    def reconcile(self) -> dict:
        return {"source": "stub", "total": len(self.jobs), "new": [],
                "changed": [], "lost": [],
                "jobs": [{"id": job_id, "state": job["state"].value}
                         for job_id, job in self.jobs.items()]}

    def pid_alive(self, _pid):
        return None


def fresh() -> tuple[TaskStore, ControlPlane, ProjectService, StubAdapter, dict, dict]:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    make_repo(REPO_A)
    make_repo(REPO_B)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    store = TaskStore(LEDGER)
    adapter = StubAdapter()
    updates = ProjectContextUpdateLoop(store, repo_root=AUDIT_ROOT, owner="t31")
    plane = ControlPlane(adapter, store, policy=LOW, context_updates=updates)
    project_a = plane.register_project(
        "t31-a", str(REPO_A), default_adapter="stub", policy=LOW,
        concurrency_limit=4)
    project_b = plane.register_project(
        "t31-b", str(REPO_B), default_adapter="stub", policy=LOW,
        concurrency_limit=4)
    return store, plane, ProjectService(plane), adapter, project_a, project_b


def execute(service: ProjectService, plane: ControlPlane, adapter: StubAdapter,
            project_id: str, title: str, *, assertion: ArtifactAssertion | None,
            create_artifact: str | None = None) -> dict:
    rec = service.submit_project_task(
        project_id, title, title=title,
        assertions=[assertion] if assertion else [],
        idempotency_key="t31-" + title.replace(" ", "-"))
    dispatched = plane.dispatch_next("t31-runtime", concurrency_limit=4)
    if not dispatched or dispatched["task_id"] != rec["task_id"]:
        raise RuntimeError("queued task was not dispatched")
    if create_artifact:
        (REPO_A / create_artifact).write_text(
            f"artifact for {title}\n", encoding="utf-8")
    adapter.finish(dispatched["worker_job_id"], TaskState.SUCCEEDED)
    refreshed = plane.refresh_known_jobs()
    task = plane.store.get(rec["task_id"])
    return {"task": task, "refresh": refreshed}


def main() -> int:
    store, plane, service, adapter, project_a, project_b = fresh()
    try:
        verified = execute(
            service, plane, adapter, project_a["id"], "verified outcome",
            assertion=ArtifactAssertion("file_exists", "accepted.txt"),
            create_artifact="accepted.txt")
        rejected = execute(
            service, plane, adapter, project_a["id"], "rejected outcome",
            assertion=ArtifactAssertion("file_exists", "missing.txt"))
        inconclusive = execute(
            service, plane, adapter, project_a["id"], "inconclusive outcome",
            assertion=None)
        tasks = [verified["task"], rejected["task"], inconclusive["task"]]

        entries = service.get_project_context(project_a["id"])["entries"]
        auto_entries = [entry for entry in entries if "m2-auto" in entry["tags"]]
        evidence_ok = all(
            ref.startswith("evidence/")
            and AUDIT_ROOT.joinpath(*Path(ref).parts).is_file()
            for entry in auto_entries for ref in entry["evidence_refs"])
        receipts_ok = all(
            len(service.context_update_receipts(task["task_id"])) == 2
            and {row["status"] for row in service.context_update_receipts(task["task_id"])}
            == {"applied"}
            for task in tasks)
        case_a = {
            "verdicts": [task["verdict"] for task in tasks],
            "auto_entry_count": len(auto_entries),
            "all_candidate": all(entry["verification"] == "candidate"
                                 for entry in auto_entries),
            "kinds": sorted({entry["kind"] for entry in auto_entries}),
            "evidence_refs_exist": evidence_ok,
            "receipts_applied": receipts_ok,
            "pass": ([task["verdict"] for task in tasks]
                     == ["verified", "rejected", "inconclusive"]
                     and len(auto_entries) == 6
                     and all(entry["verification"] == "candidate"
                             for entry in auto_entries)
                     and {entry["kind"] for entry in auto_entries}
                     == {"progress", "knowledge"}
                     and evidence_ok and receipts_ok),
        }

        # Replaying one acceptance must be a pure receipt lookup.
        version_before = store.context_version(project_a["id"])
        replays = [plane.update_project_context(verified["task"]["task_id"])
                   for _ in range(3)]
        version_after = store.context_version(project_a["id"])
        progress_title = f"Follow-up for task {verified['task']['task_id']}"
        progress_matches = [entry for entry in service.get_project_context(
            project_a["id"])["entries"]
                            if entry["kind"] == "progress"
                            and entry["title"] == progress_title]
        case_b = {
            "version_before": version_before,
            "version_after": version_after,
            "progress_entry_count": len(progress_matches),
            "all_replayed": all(all(update.get("replayed")
                                     for update in result["updates"])
                                for result in replays),
            "pass": (version_before == version_after
                     and len(progress_matches) == 1
                     and all(all(update.get("replayed")
                                 for update in result["updates"])
                             for result in replays)),
        }

        # Model two writers that prepared proposals from the same CAS version.
        base = progress_matches[0]
        evidence_ref = auto_entries[0]["evidence_refs"][0]
        first = ContextCandidate(
            project_id=project_a["id"], task_id=verified["task"]["task_id"],
            attempt_id="concurrent-a", kind="progress", title=progress_title,
            body="Writer A next step.", evidence_refs=(evidence_ref,),
            tags=("m2-auto",), source=f"task:{verified['task']['task_id']}",
            expected_version=base["version"])
        second = ContextCandidate(
            project_id=project_a["id"], task_id=verified["task"]["task_id"],
            attempt_id="concurrent-b", kind="progress", title=progress_title,
            body="Writer B independent next step.", evidence_refs=(evidence_ref,),
            tags=("m2-auto",), source=f"task:{verified['task']['task_id']}",
            expected_version=base["version"])
        # Use independent SQLite connections, as two Runtime processes would.
        left_store, right_store = TaskStore(LEDGER), TaskStore(LEDGER)
        left_loop = ProjectContextUpdateLoop(
            left_store, repo_root=AUDIT_ROOT, owner="t31-left")
        right_loop = ProjectContextUpdateLoop(
            right_store, repo_root=AUDIT_ROOT, owner="t31-right")
        barrier = threading.Barrier(2)
        concurrent: dict[str, dict] = {}

        def apply(label: str, loop: ProjectContextUpdateLoop,
                  candidate: ContextCandidate) -> None:
            barrier.wait(timeout=10)
            concurrent[label] = loop.apply_candidate(candidate)

        threads = [
            threading.Thread(target=apply, args=("first", left_loop, first)),
            threading.Thread(target=apply, args=("second", right_loop, second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        left_store.close()
        right_store.close()
        first_result, second_result = concurrent["first"], concurrent["second"]
        after_conflict = service.get_project_context(project_a["id"])["entries"]
        canonical = next(entry for entry in after_conflict
                         if entry["kind"] == "progress"
                         and entry["title"] == progress_title)
        conflicts = [entry for entry in after_conflict
                     if "needs_merge" in entry["tags"]]
        result_by_body = {
            first.body: first_result["status"],
            second.body: second_result["status"],
        }
        winner_body = next(body for body, status in result_by_body.items()
                           if status == "applied")
        loser_body = next(body for body, status in result_by_body.items()
                          if status == "needs_merge")
        case_c = {
            "first_status": first_result["status"],
            "second_status": second_result["status"],
            "canonical_kept": canonical["body"] == winner_body,
            "conflict_preserved": any(entry["body"] == loser_body
                                      for entry in conflicts),
            "conflict_candidate": all(entry["verification"] == "candidate"
                                      for entry in conflicts),
            "pass": ({first_result["status"], second_result["status"]}
                     == {"applied", "needs_merge"}
                     and canonical["body"] == winner_body
                     and any(entry["body"] == loser_body for entry in conflicts)
                     and all(entry["verification"] == "candidate"
                             for entry in conflicts)),
        }

        stranded_id = store.new_task_id()
        store.create({
            "task_id": stranded_id, "project_id": project_a["id"],
            "workspace_id": store.default_workspace(project_a["id"])["id"],
            "title": "verdict committed before M2", "prompt": "already finished",
            "cwd": str(REPO_A), "state": TaskState.SUCCEEDED.value,
            "verdict": "inconclusive", "queue_status": "complete",
            "adapter": "stub", "idempotency_key": "t31-stranded",
        })
        store.add_attempt(
            stranded_id, "job-stranded", reason="terminal before M2",
            state="succeeded")
        recovered = plane.reconcile_project_context_updates()
        second_sweep = plane.reconcile_project_context_updates()
        stranded_receipts = service.context_update_receipts(stranded_id)
        case_e = {
            "first_sweep_checked": recovered["checked"],
            "first_sweep_applied": recovered["applied"],
            "second_sweep_checked": second_sweep["checked"],
            "receipt_statuses": [row["status"] for row in stranded_receipts],
            "pass": (recovered["checked"] == 1 and recovered["applied"] == 1
                     and second_sweep["checked"] == 0
                     and len(stranded_receipts) == 2
                     and {row["status"] for row in stranded_receipts} == {"applied"}),
        }

        try:
            plane.context_updates.apply_candidate(ContextCandidate(
                project_id=project_a["id"], task_id=verified["task"]["task_id"],
                attempt_id="bad-constraint", kind="constraints", title="automatic rule",
                body="must never happen", evidence_refs=(evidence_ref,)))
            constraint_guard = "ALLOWED"
        except ContextError:
            constraint_guard = "refused"
        try:
            plane.context_updates.apply_candidate(ContextCandidate(
                project_id=project_b["id"], task_id=verified["task"]["task_id"],
                attempt_id="cross-project", kind="knowledge", title="leak",
                body="must never cross", evidence_refs=(evidence_ref,)))
            project_guard = "ALLOWED"
        except ContextError:
            project_guard = "refused"
        final_a = service.get_project_context(project_a["id"])["entries"]
        final_b = service.get_project_context(project_b["id"])["entries"]
        case_d = {
            "automatic_constraint": constraint_guard,
            "cross_project_write": project_guard,
            "verified_auto_entries": sum(
                1 for entry in final_a
                if "m2-auto" in entry["tags"] and entry["verification"] == "verified"),
            "project_b_entries": len(final_b),
            "pass": (constraint_guard == "refused" and project_guard == "refused"
                     and not any(entry["kind"] == "constraints" for entry in final_a)
                     and not any(entry["verification"] == "verified"
                                 for entry in final_a if "m2-auto" in entry["tags"])
                     and len(final_b) == 0),
        }

        log = {
            "test": "T31-context-update-loop",
            "A_terminal_outcomes": case_a,
            "B_idempotent_replay": case_b,
            "C_conflict_preservation": case_c,
            "D_safety_boundaries": case_d,
            "E_crash_window_recovery": case_e,
        }
        log["pass"] = {"A": case_a["pass"], "B": case_b["pass"],
                       "C": case_c["pass"], "D": case_d["pass"],
                       "E": case_e["pass"]}
        log["result"] = "Pass" if all(log["pass"].values()) else "Fail"
        write_evidence(ROOT / "evidence" / "project" /
                       "T31-context-update-loop.json", log)
        print(json.dumps(log, ensure_ascii=False, indent=1))
        print("RESULT:", log["result"])
        return 0 if log["result"] == "Pass" else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
