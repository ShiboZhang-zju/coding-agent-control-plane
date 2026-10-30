"""T37: M4-B bounded goal advancement (no Worker, scripted model).

A state-driven scripted model drives one goal through the full lifecycle:
submit task A -> wait -> (task A verified by the harness) -> submit task B as
a deterministic session handoff on A's checkpoint -> wait -> (task B verified)
-> finish with both verified tasks as evidence.

Additionally verifies the M4-B contract:

* deterministic goal identity: the same goal text + project resumes the same goal
* waiting on a pending task stops the run without wasting decisions
* restart recovery: a fresh coordinator + goal store continues the same goal
  from the Ledger, never resubmitting task A
* decision budgets stop the goal in needs_human
* concurrent advancement is refused by the goal lease
* repeated inspections without progress stall out to needs_human
* decision history is append-only and complete
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import GoalLeaseHeld, LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-goal"
REPO = FIXTURE / "repo-a"
WORKTREE = FIXTURE / "repo-a-wt"
LEDGER = ROOT / ".poc-state" / "t37-m4-goal.db"
GOAL_TEXT = "实现 feature-a 和 feature-b，两部分分别由独立 Session 完成"


class AdapterRef:
    name = "stub"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    REPO.mkdir(parents=True)
    git(REPO, "init", "-q")
    git(REPO, "config", "user.email", "t37@example.invalid")
    git(REPO, "config", "user.name", "T37")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")
    git(REPO, "worktree", "add", "-q", str(WORKTREE), "-b", "feat-goal")


class GoalDrivenModel:
    """State-driven decisions: submit A, wait, submit B, wait, finish."""

    def decide(self, decision_input: dict) -> dict:
        state = (decision_input.get("goal_state") or {})
        project = decision_input.get("requested_project_id")
        completed = [t["task_id"] for t in state.get("completed_tasks", [])
                     if t.get("verdict") == "verified"]
        current = state.get("current_task") or {}
        if len(completed) >= 2:
            return {
                "action": "finish", "project_id": project,
                "reason": "both goal tasks are verified",
                "evidence_refs": [f"task:{tid}" for tid in completed],
                "parameters": {"verified_task_ids": completed,
                               "completion_conditions": []},
            }
        if (current.get("task_id")
                and current.get("verdict") in (None, "pending")
                and current.get("state") not in ("succeeded", "failed")):
            return {
                "action": "wait", "project_id": project,
                "reason": "wait for the in-flight task",
                "evidence_refs": [f"task:{current['task_id']}"],
                "parameters": {"task_id": current["task_id"]},
            }
        if len(completed) == 1:
            return {
                "action": "submit_task", "project_id": project,
                "reason": "second part depends on the first",
                "evidence_refs": [f"task:{completed[0]}"],
                "parameters": {
                    "task": "part B: extend feature-a with feature-b",
                    "idempotency_key": "goal-part-b",
                    "assertions": [{"kind": "file_exists",
                                    "target": "feature-b.txt"}],
                },
            }
        return {
            "action": "submit_task", "project_id": project,
            "reason": "first part of the goal",
            "evidence_refs": [],
            "parameters": {
                "task": "part A: implement feature-a",
                "idempotency_key": "goal-part-a",
                "assertions": [{"kind": "file_exists",
                                "target": "feature-a.txt"}],
            },
        }


def simulate_task_completion(store: TaskStore, task_id: str, session: str) -> None:
    """Harness stands in for the Worker: attempt, session, terminal + verified."""
    job = f"job-{uuid.uuid4().hex[:8]}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t37", adapter="stub", session_id=session)
    (WORKTREE / f"artifact-{session}.txt").write_text("done\n", encoding="utf-8")
    git(WORKTREE, "add", "-A")
    git(WORKTREE, "-c", "user.name=T37", "-c", "user.email=t37@example.invalid",
        "commit", "-q", "-m", f"work for {task_id}")
    store.update(
        task_id, state=TaskState.SUCCEEDED.value, verdict=Verdict.VERIFIED.value,
        worktree=str(WORKTREE), head_commit=git(WORKTREE, "rev-parse", "HEAD"),
        session_id=session, finished_at=1.0, artifacts=[], assertion_results=[
            {"kind": "file_exists", "target": "artifact", "passed": True}])


def build(store: TaskStore):
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                         worker_instance_id="wi-t37")
    service = ProjectService(plane)
    project = plane.register_project("t37-goal", str(REPO), default_adapter="stub")
    tools = LDTools(service)
    model = GoalDrivenModel()
    coordinator = LDCoordinator(
        tools, model, model_authorized_security_domains=("local", "shareable"))
    return plane, service, tools, model, coordinator, project


def fresh() -> None:
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    make_repo()


def lifecycle_case() -> dict:
    fresh()
    store = TaskStore(LEDGER)
    goal_store = LDGoalStore(LEDGER)
    out: dict = {}
    try:
        plane, service, tools, model, coordinator, project = build(store)
        goal_id, goal_ref = goal_identity(GOAL_TEXT, project["id"])
        out["goal_id_deterministic"] = bool(goal_id)

        report1 = coordinator.run_goal(GOAL_TEXT, project["id"], goal_store,
                                       max_decisions=10, max_tasks=3)
        task_a = report1.get("current_task_id")
        out["round1_submits_task_a"] = bool(task_a)
        out["round1_stops_waiting"] = report1.get("status") == "waiting"

        # Restart recovery: a brand new coordinator instance reads the same
        # goal from the ledger, never resubmits A, and continues to waiting.
        _, _, _, model2, coordinator2, _ = build(store)
        report2 = coordinator2.run_goal(GOAL_TEXT, project["id"], goal_store,
                                        max_decisions=10, max_tasks=3)
        out["recovery_does_not_resubmit"] = (
            report2.get("current_task_id") == task_a
            and report2.get("task_count") == 1)

        # Harness completes A; round 3 submits B as a session handoff.
        simulate_task_completion(store, task_a, "sess-t37-a")
        _, _, _, model3, coordinator3, _ = build(store)
        report3 = coordinator3.run_goal(GOAL_TEXT, project["id"], goal_store,
                                        max_decisions=10, max_tasks=3)
        task_b = report3.get("current_task_id")
        record_b = store.get(task_b) or {}
        prompt_b = record_b.get("effective_prompt") or ""
        out["task_b_submitted"] = task_b != task_a
        out["task_b_is_handoff"] = bool((record_b.get("metadata") or {}).get("handoff"))
        out["handoff_prompt_has_merge"] = "git merge" in prompt_b
        out["handoff_records_goal_ref"] = (
            (record_b.get("metadata") or {}).get("ld_goal_ref") == goal_ref)
        out["handoff_task_ids_tracked"] = report3.get("handoff_task_ids") == [task_b]

        # Harness completes B; round 4 finishes the goal with both as evidence.
        simulate_task_completion(store, task_b, "sess-t37-b")
        _, _, _, model4, coordinator4, _ = build(store)
        report4 = coordinator4.run_goal(GOAL_TEXT, project["id"], goal_store,
                                        max_decisions=10, max_tasks=3)
        out["goal_completed"] = report4.get("status") == "completed"
        out["both_tasks_verified_in_report"] = sorted(
            t["task_id"] for t in report4.get("verified_tasks", [])) == sorted(
            [task_a, task_b])
        history = goal_store.decisions(goal_id, limit=50)
        out["history_append_only"] = [d["step"] for d in history] == list(
            range(1, len(history) + 1))
        # submit A -> wait A (restart round) -> submit B -> finish = 4 steps.
        out["history_complete"] = len(history) >= 4
    finally:
        store.close()
        goal_store.close()
    return out


def budget_case() -> dict:
    fresh()
    store = TaskStore(LEDGER)
    goal_store = LDGoalStore(LEDGER)
    try:
        _, _, _, _, coordinator, project = build(store)
        coordinator.run_goal(GOAL_TEXT, project["id"], goal_store,
                             max_decisions=1, max_tasks=3)
        report = coordinator.run_goal(GOAL_TEXT, project["id"], goal_store,
                                      max_decisions=1, max_tasks=3)
        return {
            "budget_stops_needs_human": report.get("status") == "needs_human",
            "budget_stop_reason": "budget" in (report.get("stop_reason") or ""),
        }
    finally:
        store.close()
        goal_store.close()


def lease_case() -> dict:
    fresh()
    store = TaskStore(LEDGER)
    goal_store = LDGoalStore(LEDGER)
    try:
        _, _, _, _, coordinator, project = build(store)
        goal_id, goal_ref = goal_identity("lease test goal", project["id"])
        goal_store.get_or_create(goal_id, project["id"], "lease test goal", goal_ref)
        goal_store.acquire_lease(goal_id, "other-process")
        refused = False
        try:
            coordinator.run_goal("lease test goal", project["id"], goal_store)
        except GoalLeaseHeld:
            refused = True
        goal_store.release_lease(goal_id, "other-process")
        resumed = coordinator.run_goal("lease test goal", project["id"], goal_store)
        return {
            "concurrent_advance_refused": refused,
            "released_lease_allows_resume": bool(resumed.get("goal_id")),
        }
    finally:
        store.close()
        goal_store.close()


def stall_case() -> dict:
    fresh()
    store = TaskStore(LEDGER)
    goal_store = LDGoalStore(LEDGER)
    try:
        _, _, _, _, coordinator, project = build(store)

        class InspectOnlyModel:
            def decide(self, decision_input):
                return {
                    "action": "inspect",
                    "project_id": decision_input.get("requested_project_id"),
                    "reason": "keep looking",
                    "evidence_refs": [],
                    "parameters": {"tool": "get_project_progress"},
                }

        coordinator._model = InspectOnlyModel()
        report = coordinator.run_goal("stall test", project["id"], goal_store,
                                      max_decisions=10, max_tasks=3)
        return {"stalled_inspections_stop": report.get("status") == "needs_human"}
    finally:
        store.close()
        goal_store.close()


def main() -> int:
    sections = {
        "lifecycle": lifecycle_case(),
        "budget": budget_case(),
        "lease": lease_case(),
        "stall": stall_case(),
    }
    checks = {f"{name}.{key}": bool(value)
              for name, result in sections.items()
              for key, value in result.items()}
    log = {
        "test": "T37-m4-goal-advancement",
        "worker_started": False,
        "sections": sections,
        "checks": checks,
        "result": "Pass" if all(checks.values()) else "Fail",
    }
    write_evidence(ROOT / "evidence" / "project" / "T37-m4-goal-advancement.json",
                   log)
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
