"""T39: M4 goal reliability closure (no Worker; real process crashes).

R1  needs_human -> resume -> finish: the human resume and the next model step
    never collide (P0-1); the final report holds exactly this goal's verified
    tasks A and B, not another goal's task (P1-3); history is contiguous.
R2  crash injection at every persistence boundary of a submit step, using a
    real child process that dies with os._exit (P0-2):
      after_model_decision  - decision made, nothing persisted
      after_begin_step      - pending step persisted, no WorkItem yet
      after_execute         - WorkItem created, step not committed
      commit_mid            - inside the commit transaction (rolled back)
    After restart: exactly one WorkItem for the step, keyed by the system
    step key; pending steps are adopted/replayed without asking the model.
R3  lease fencing (P0-3): concurrent processes cannot both advance a goal; a
    holder whose lease expired and was taken over cannot write any goal state
    or submit a second task; the heartbeat keeps a lease alive through a long
    model call.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import (GoalLeaseHeld, GoalLeaseLost, LDGoalStore,  # noqa: E402
                           goal_identity, step_idempotency_key)
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-reliability"
REPO = FIXTURE / "repo-a"
WORKTREE = FIXTURE / "repo-a-wt"
STATE = ROOT / ".poc-state" / "t39"
GOAL = "实现 feature-a，再在其上实现 feature-b，两部分由独立 Session 完成"
OTHER_GOAL = "另一个目标：只实现 feature-z"
CRASH_EXIT = 86
CRASH_POINTS = ("after_model_decision", "after_begin_step", "after_execute",
                "commit_mid")


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
    git(REPO, "config", "user.email", "t39@example.invalid")
    git(REPO, "config", "user.name", "T39")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")
    git(REPO, "worktree", "add", "-q", str(WORKTREE), "-b", "feat-goal")


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


class ScriptedModel:
    """State-driven goal model; records which step each call decided."""

    def __init__(self, *, ask_human_once: bool = False, delay_s: float = 0.0,
                 goal_text: str = GOAL):
        self.ask_human_once = ask_human_once
        self.delay_s = delay_s
        self.goal_text = goal_text
        self.decided_steps: list[int] = []

    def decide(self, decision_input: dict) -> dict:
        if self.delay_s:
            time.sleep(self.delay_s)
        state = decision_input.get("goal_state") or {}
        project = decision_input.get("requested_project_id")
        self.decided_steps.append(int(state.get("decision_count", 0)) + 1)
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        current = state.get("current_task") or {}
        if len(verified) >= 2:
            if self.ask_human_once and not state.get("human_resume_count"):
                return {"action": "request_human", "project_id": project,
                        "reason": "not sure the goal is fully covered",
                        "evidence_refs": [f"task:{t}" for t in verified],
                        "parameters": {"question": "可以结束这个目标吗？"}}
            return {"action": "finish", "project_id": project,
                    "reason": "both goal tasks are verified",
                    "evidence_refs": [f"task:{t}" for t in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        if (current.get("task_id") and current.get("verdict") in (None, "pending")
                and current.get("state") not in ("succeeded", "failed")):
            return {"action": "wait", "project_id": project,
                    "reason": "wait for the in-flight task",
                    "evidence_refs": [f"task:{current['task_id']}"],
                    "parameters": {"task_id": current["task_id"]}}
        part = "b" if verified else "a"
        return {"action": "submit_task", "project_id": project,
                "reason": f"part {part} of the goal",
                "evidence_refs": [f"task:{t}" for t in verified],
                "parameters": {"task": f"part {part}: implement feature-{part}",
                               "idempotency_key": f"model-chosen-{part}",
                               "assertions": [{"kind": "file_exists",
                                               "target": f"feature-{part}.txt"}]}}


def build(ledger: Path, model, *, fault_hook=None, project_name="t39-goal"):
    store = TaskStore(ledger)
    plane = ControlPlane(AdapterRef(), store, require_registered_projects=True,
                         worker_instance_id="wi-t39")
    service = ProjectService(plane)
    project = plane.register_project(project_name, str(REPO), default_adapter="stub")
    coordinator = LDCoordinator(
        LDTools(service), model,
        model_authorized_security_domains=("local", "shareable"),
        fault_hook=fault_hook)
    return store, service, coordinator, project


def complete_task(store: TaskStore, task_id: str, session: str) -> None:
    """Harness stands in for Worker + Verifier: attempt, session, verified."""
    job = f"job-{uuid.uuid4().hex[:8]}"
    store.add_attempt(task_id, worker_job_id=job)
    store.observe_session_reference(
        work_item_id=task_id, worker_job_id=job, node_id="local",
        worker_instance_id="wi-t39", adapter="stub", session_id=session)
    (WORKTREE / f"artifact-{session}.txt").write_text("done\n", encoding="utf-8")
    git(WORKTREE, "add", "-A")
    git(WORKTREE, "-c", "user.name=T39", "-c", "user.email=t39@example.invalid",
        "commit", "-q", "-m", f"work for {task_id}")
    store.update(
        task_id, state=TaskState.SUCCEEDED.value, verdict=Verdict.VERIFIED.value,
        worktree=str(WORKTREE), head_commit=git(WORKTREE, "rev-parse", "HEAD"),
        session_id=session, finished_at=time.time(), artifacts=[],
        assertion_results=[{"kind": "file_exists", "target": "x", "passed": True}])


def goal_tasks(store: TaskStore, goal_ref: str) -> list[dict]:
    return [t for t in store.all()
            if (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref]


def r1_resume_and_complete() -> dict:
    """A, B verified -> LD asks human -> human says 'continue' -> completed."""
    ledger = fresh_ledger("r1")
    model = ScriptedModel(ask_human_once=True)
    store, service, coordinator, project = build(ledger, model)
    goal_store = LDGoalStore(ledger)
    out: dict = {}
    try:
        goal_id, goal_ref = goal_identity(GOAL, project["id"])
        # A verified task belonging to a DIFFERENT goal in the same project.
        _, other_ref = goal_identity(OTHER_GOAL, project["id"])
        other = LDTools(service).submit_project_task(
            project["id"], "feature-z for another goal",
            idempotency_key="t39-other-goal", goal_ref=other_ref,
            assertions=[{"kind": "file_exists", "target": "feature-z.txt"}])
        complete_task(store, other["task_id"], "sess-other")

        r = coordinator.run_goal(GOAL, project["id"], goal_store)       # submit A
        task_a = r.get("current_task_id")
        complete_task(store, task_a, "sess-a")
        r = coordinator.run_goal(GOAL, project["id"], goal_store)       # wait->submit B
        task_b = r.get("current_task_id")
        complete_task(store, task_b, "sess-b")
        r = coordinator.run_goal(GOAL, project["id"], goal_store)       # wait->ask human
        out["paused_needs_human"] = r.get("status") == "needs_human"
        steps_before = (goal_store.get(goal_id) or {}).get("decision_count")
        r = coordinator.run_goal(GOAL, project["id"], goal_store)       # no resume: no-op
        out["paused_goal_does_not_advance_without_human"] = (
            r.get("status") == "needs_human"
            and (goal_store.get(goal_id) or {}).get("decision_count") == steps_before)
        final = coordinator.run_goal(GOAL, project["id"], goal_store,   # "继续"
                                     resume=True, resume_note="继续")
        history = goal_store.decisions(goal_id, limit=100)
        steps = [d["step"] for d in history]
        resumes = [d for d in history if d["decision"].get("action") == "human_resume"]
        report_ids = sorted(t["task_id"] for t in final.get("verified_tasks", []))
        goal_row = goal_store.get(goal_id) or {}
        out.update({
            "final_status_completed": final.get("status") == "completed",
            "report_has_exactly_a_and_b": report_ids == sorted([task_a, task_b]),
            "report_excludes_other_goal_task": other["task_id"] not in report_ids
            and all(t["task_id"] != other["task_id"] for t in final.get("goal_tasks", [])),
            "b_is_handoff_of_a": (store.get(task_b) or {}).get("metadata", {}).get(
                "source_work_item_id") == task_a,
            "history_contiguous": steps == list(range(1, len(steps) + 1))
            and final.get("history_contiguous") is True,
            "exactly_one_human_resume_step": len(resumes) == 1,
            "resume_followed_by_model_step": bool(resumes) and any(
                d["step"] == resumes[0]["step"] + 1
                and d["outcome"].get("action") == "finish" for d in history),
            "decision_count_equals_last_step": goal_row.get("decision_count") == steps[-1],
            "model_budget_excludes_resume": goal_row.get("model_decision_count")
            == goal_row.get("decision_count") - 1,
            "no_pending_steps": not final.get("pending_steps"),
            "step_keys_are_system_generated": all(
                (store.get(t) or {}).get("idempotency_key", "").startswith(f"ldg-{goal_id}-s")
                for t in (task_a, task_b)),
        })
        out["decision_history"] = final.get("decision_history")
    finally:
        store.close()
        goal_store.close()
    return out


def child_crash(ledger: Path, point: str) -> int:
    """Child process: advance the goal and die hard at ``point``."""
    def hook(name: str) -> None:
        if name == point:
            os._exit(CRASH_EXIT)  # no finally blocks, no lease release, no commit

    store, service, coordinator, project = build(ledger, ScriptedModel(),
                                                 fault_hook=hook)
    goal_store = LDGoalStore(ledger, lease_s=1.0, fault_hook=hook)
    coordinator.run_goal(GOAL, project["id"], goal_store)
    return 0  # reaching here means the fault point was never hit


def r2_crash_injection() -> dict:
    out: dict = {}
    for point in CRASH_POINTS:
        ledger = fresh_ledger(f"r2-{point}")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child-crash", point,
             "--ledger", str(ledger)],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        store, service, coordinator, project = build(ledger, ScriptedModel())
        goal_store = LDGoalStore(ledger, lease_s=1.0)
        try:
            goal_id, goal_ref = goal_identity(GOAL, project["id"])
            pending_after_crash = goal_store.pending_step(goal_id)
            tasks_after_crash = [t["task_id"] for t in goal_tasks(store, goal_ref)]
            time.sleep(1.3)  # the dead child's lease expires; nobody released it

            recovery_model = ScriptedModel()
            coordinator._model = recovery_model
            report = coordinator.run_goal(GOAL, project["id"], goal_store)
            tasks = goal_tasks(store, goal_ref)
            history = goal_store.decisions(goal_id, limit=50)
            step1 = next((d for d in history if d["step"] == 1), {})
            expected_pending = point != "after_model_decision"
            expected_task_before_restart = point in ("after_execute", "commit_mid")
            out[point] = {
                "child_died_at_fault_point": proc.returncode == CRASH_EXIT,
                "pending_step_after_crash_as_expected": (
                    (pending_after_crash is not None) == expected_pending),
                "workitem_before_restart_as_expected": (
                    bool(tasks_after_crash) == expected_task_before_restart),
                "exactly_one_workitem_after_recovery": len(tasks) == 1,
                "workitem_keyed_by_step": bool(tasks) and tasks[0].get(
                    "idempotency_key") == step_idempotency_key(goal_id, 1),
                "crashed_child_task_adopted": (
                    not expected_task_before_restart
                    or [t["task_id"] for t in tasks] == tasks_after_crash),
                "step1_committed": step1.get("state") == "committed",
                "recovery_mode": step1.get("outcome", {}).get("recovered"),
                "recovery_mode_as_expected": step1.get("outcome", {}).get("recovered") == (
                    "adopted" if expected_task_before_restart
                    else "replayed" if expected_pending else None),
                "model_not_asked_for_recovered_step": (
                    not expected_pending or 1 not in recovery_model.decided_steps),
                "goal_waiting_on_that_task": (
                    report.get("status") == "waiting"
                    and report.get("current_task_id") == (tasks[0]["task_id"] if tasks else None)),
                "task_count_is_one": (goal_store.get(goal_id) or {}).get("task_count") == 1,
                "history_contiguous": report.get("history_contiguous") is True,
            }
        finally:
            store.close()
            goal_store.close()
    return out


def child_race(ledger: Path) -> int:
    store, service, coordinator, project = build(ledger, ScriptedModel(delay_s=4.0))
    goal_store = LDGoalStore(ledger, lease_s=30.0)
    try:
        report = coordinator.run_goal(GOAL, project["id"], goal_store)
        print(json.dumps({"result": "ok", "status": report.get("status")}))
    except GoalLeaseHeld:
        print(json.dumps({"result": "held"}))
    return 0


def r3_lease_fencing() -> dict:
    out: dict = {}

    # 3a: two real processes race on one goal.
    ledger = fresh_ledger("r3a")
    build(ledger, ScriptedModel())[0].close()  # register the project once
    procs = [subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--child-race",
         "--ledger", str(ledger)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT))
        for _ in range(2)]
    results = []
    for proc in procs:
        stdout, _ = proc.communicate(timeout=120)
        lines = [line for line in stdout.splitlines() if line.startswith("{")]
        results.append(json.loads(lines[-1]) if lines else {"result": "crashed"})
    store, service, coordinator, project = build(ledger, ScriptedModel())
    _, goal_ref = goal_identity(GOAL, project["id"])
    out["race"] = {
        "results": sorted(r["result"] for r in results),
        "exactly_one_advanced": sorted(r["result"] for r in results) == ["held", "ok"],
        "single_workitem": len(goal_tasks(store, goal_ref)) == 1,
    }
    store.close()

    # 3b: stale holder after its lease expired and was taken over.
    ledger = fresh_ledger("r3b")
    slow = ScriptedModel(delay_s=2.0)
    store_a, _, coord_a, project = build(ledger, slow)
    gs_a = LDGoalStore(ledger, lease_s=0.5)
    outcome: dict = {}

    def stale_holder() -> None:
        try:
            outcome["report"] = coord_a.run_goal(GOAL, project["id"], gs_a,
                                                 heartbeat=False)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=stale_holder)
    thread.start()
    time.sleep(0.9)  # A is inside its long model call; its lease has expired
    store_b, _, coord_b, _ = build(ledger, ScriptedModel())
    gs_b = LDGoalStore(ledger, lease_s=30.0)
    report_b = coord_b.run_goal(GOAL, project["id"], gs_b)
    token_b = (gs_b.get(goal_identity(GOAL, project["id"])[0]) or {}).get("lease_token")
    thread.join(timeout=30)
    goal_id, goal_ref = goal_identity(GOAL, project["id"])
    history = gs_b.decisions(goal_id)
    out["stale_holder"] = {
        "takeover_succeeded": report_b.get("status") == "waiting",
        "stale_holder_stopped_with_lease_lost": isinstance(outcome.get("error"), GoalLeaseLost),
        "stale_holder_model_was_called": 1 in slow.decided_steps,
        "single_workitem": len(goal_tasks(store_b, goal_ref)) == 1,
        "all_steps_written_by_new_holder": bool(history) and all(
            d.get("lease_token") == token_b for d in history),
        "goal_state_intact": (gs_b.get(goal_id) or {}).get("decision_count") == 1,
    }
    for closer in (store_a, store_b, gs_a, gs_b):
        closer.close()

    # 3c: the heartbeat keeps the lease through a model call longer than it.
    ledger = fresh_ledger("r3c")
    store, _, coordinator, project = build(ledger, ScriptedModel(delay_s=2.0))
    gs = LDGoalStore(ledger, lease_s=0.6)
    holder: dict = {}
    thread = threading.Thread(target=lambda: holder.update(
        report=coordinator.run_goal(GOAL, project["id"], gs)))
    thread.start()
    time.sleep(1.3)
    intruder = LDGoalStore(ledger, lease_s=30.0)
    refused = False
    try:
        intruder.acquire_lease(goal_identity(GOAL, project["id"])[0], "intruder")
    except GoalLeaseHeld:
        refused = True
    thread.join(timeout=30)
    out["heartbeat"] = {
        "intruder_refused_during_long_call": refused,
        "holder_completed_step": (holder.get("report") or {}).get("status") == "waiting",
    }
    for closer in (store, gs, intruder):
        closer.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child-crash", default=None)
    ap.add_argument("--child-race", action="store_true")
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args()
    if args.child_crash:
        return child_crash(Path(args.ledger), args.child_crash)
    if args.child_race:
        return child_race(Path(args.ledger))

    make_repo()
    sections = {
        "R1_resume_and_complete": r1_resume_and_complete(),
        "R2_crash_injection": r2_crash_injection(),
        "R3_lease_fencing": r3_lease_fencing(),
    }

    checks: dict[str, bool] = {}

    def collect(node, prefix):
        for key, value in node.items():
            name = f"{prefix}.{key}"
            if isinstance(value, bool):
                checks[name] = value
            elif isinstance(value, dict):
                collect(value, name)

    for name, section in sections.items():
        collect(section, name)
    log = {
        "test": "T39-m4-goal-reliability",
        "worker_started": False,
        "real_process_crashes": True,
        "sections": sections,
        "checks": checks,
        "result": "Pass" if checks and all(checks.values()) else "Fail",
    }
    write_evidence(ROOT / "evidence" / "project" / "T39-m4-goal-reliability.json", log)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
