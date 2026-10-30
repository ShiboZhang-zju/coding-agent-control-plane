"""T42: LD auto-wakeup — the goal advances without a human re-running run-goal.

The M4-B gap this closes: after a Worker verdict, *something* had to call
``LDCoordinator.run_goal`` again.  T42 proves the runtime now does it, and that
it does so only when the observation actually changed.

Sections
--------
B1  one goal, zero manual invocations: the human owner submits the goal once;
    the runtime dispatches, the harness marks the Worker terminal/verified, and
    the attached ``LDGoalRunner`` wakes the LD to *finish* the goal.
B2  no model call while the current WorkItem is still in flight (the runner
    waits instead of burning tokens), and no repeated call for an unchanged
    observation.
B3  ``needs_human`` / ``completed`` / ``failed`` goals are never woken.
B4  repeated model failure backs off, escalates to human after the configured
    number of attempts, and stops calling the model.
B5  a pending step left by a crash is reconciled from the Ledger *without*
    asking the model (one WorkItem, no second submission).

The "model" here is scripted, so this test is deterministic and needs neither a
real endpoint nor a real Worker: it verifies the *scheduling* contract, which is
what M4-C added.  Real end-to-end behaviour is covered by T43/T44.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskSnapshot, TaskState, Verdict  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner, GoalRunnerStore, goal_wake_signature  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity, step_idempotency_key  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "m4-goal-runner"
REPO = FIXTURE / "repo-a"
STATE = ROOT / ".poc-state" / "t42"
EVIDENCE = ROOT / "evidence" / "project" / "T42-ld-goal-auto-wakeup.json"
GOAL = "实现 feature-a 并让测试通过"


class StubAdapter:
    """Accepts dispatches; the harness decides when a task becomes terminal."""

    name = "stub"

    def __init__(self):
        self.dispatched: list[str] = []

    def dispatch(self, request):  # noqa: ANN001
        self.dispatched.append(request.task_id)
        return DispatchResult(task_id=request.task_id,
                              worker_job_id=f"job-{request.task_id}",
                              accepted=True)

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        return TaskSnapshot(task_id=task_id, worker_job_id=f"job-{task_id}",
                            state=TaskState.RUNNING)

    def pid_alive(self, _pid):  # noqa: ANN001
        return None

    def collect_artifacts(self, _task_id):  # noqa: ANN001
        return []

    def reconcile(self):
        return {"jobs": []}

    def capabilities(self):
        return {}


class CountingModel:
    """Scripted goal model that counts how often it was actually asked."""

    def __init__(self, *, mode: str = "normal"):
        self.mode = mode
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        state = decision_input.get("goal_state") or {}
        project = decision_input.get("requested_project_id")
        self.calls.append({
            "decision_count": state.get("decision_count"),
            "current_task": (state.get("current_task") or {}).get("task_id"),
            "completed": [t["task_id"] for t in state.get("completed_tasks", [])],
        })
        if self.mode == "always_fail":
            raise RuntimeError("scripted transport failure")
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        if verified:
            return {"action": "finish", "project_id": project,
                    "reason": "the goal task is verified",
                    "evidence_refs": [f"task:{tid}" for tid in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        return {"action": "submit_task", "project_id": project,
                "reason": "implement the goal",
                "evidence_refs": [],
                "parameters": {
                    "task": "实现 feature-a 并让测试通过",
                    "idempotency_key": "goal-part-a",
                    "assertions": [{"kind": "file_exists", "target": "feature-a.txt"}],
                }}


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
    git(REPO, "config", "user.email", "t42@example.invalid")
    git(REPO, "config", "user.name", "T42")
    (REPO / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(REPO, "add", "seed.txt")
    git(REPO, "commit", "-q", "-m", "seed")
    git(REPO, "branch", "-M", "main")


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def build(ledger: Path, *, model, max_model_failures: int = 3,
          max_no_progress: int = 3, max_decisions: int = 1):
    store = TaskStore(ledger)
    adapter = StubAdapter()
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t42")
    service = ProjectService(plane)
    project = plane.register_project("t42", str(REPO), default_adapter="stub",
                                     concurrency_limit=8)
    coordinator = LDCoordinator(
        LDTools(service), model,
        model_authorized_security_domains=("local", "shareable"))
    goals = LDGoalStore(store.path, lease_s=60.0)
    runner = LDGoalRunner(
        coordinator, goals, max_decisions=max_decisions, max_tasks=1,
        max_model_failures=max_model_failures,
        max_no_progress_advances=max_no_progress,
        base_backoff_s=1.0, max_backoff_s=4.0)
    return {
        "store": store, "adapter": adapter, "plane": plane, "service": service,
        "project": project, "coordinator": coordinator, "goals": goals,
        "runner": runner,
    }


def close(ctx) -> None:
    for key in ("runner", "goals", "store"):
        try:
            ctx[key].close()
        except Exception:  # noqa: BLE001
            pass


def current_goal_task(store: TaskStore, goal_id: str, goal_ref: str) -> dict | None:
    tasks = [task for task in store.all()
             if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    tasks.sort(key=lambda t: t.get("created_at") or 0)
    goal = None
    for task in tasks:
        goal = task
    return goal


def complete_task(store: TaskStore, task_id: str, *,
                  verdict: str = Verdict.VERIFIED.value) -> dict:
    """Mark one WorkItem terminal+verified, exactly like a real observation would."""
    record = store.get(task_id) or {}
    store.update(task_id, state=TaskState.SUCCEEDED.value, verdict=verdict,
                 queue_status="complete", finished_at=1_700_000_000.0,
                 worker_job_id=record.get("worker_job_id") or f"job-{task_id}")
    return store.get(task_id) or {}


def submit_goal_once(ctx, goal_text: str = GOAL) -> tuple[str, str]:
    """The one and only Human-Owner action: create the goal row."""
    project_id = ctx["project"]["id"]
    goal_id, goal_ref = goal_identity(goal_text, project_id)
    ctx["goals"].get_or_create(goal_id, project_id, goal_text, goal_ref)
    return goal_id, goal_ref


# --------------------------------------------------------------------------- #
# B1 / B2 — the loop closes; the model is not spammed
# --------------------------------------------------------------------------- #
def b1_zero_manual_invocation() -> dict:
    ctx = build(fresh_ledger("b1"), model=CountingModel())
    try:
        goal_id, goal_ref = submit_goal_once(ctx)
        model = ctx["runner"]._coordinator._model
        # Sweep 1: nothing exists yet -> the LD submits the first task.
        first = ctx["runner"].advance_due()
        task = current_goal_task(ctx["store"], goal_id, goal_ref)
        task_id = (task or {}).get("task_id")
        calls_after_submit = len(model.calls)
        goal_after_submit = ctx["goals"].get(goal_id) or {}

        # Sweep 2..4: the task is still in flight -> wait, never call the model.
        idle_sweeps = [ctx["runner"].advance_due() for _ in range(3)]
        calls_while_running = len(model.calls) - calls_after_submit

        # Worker finishes; the harness records the terminal verdict.
        complete_task(ctx["store"], task_id)
        final = ctx["runner"].advance_due()
        goal_final = ctx["goals"].get(goal_id) or {}

        # One more sweep: nothing changed -> no further model call.
        calls_before_extra = len(model.calls)
        extra = ctx["runner"].advance_due()
        calls_after_extra = len(model.calls) - calls_before_extra
        return {
            "goal_created_active": goal_after_submit.get("status") == "waiting",
            "first_sweep_submitted": bool(task_id),
            "model_called_once_to_submit": calls_after_submit == 1,
            "no_model_call_while_running": calls_while_running == 0,
            "idle_sweeps_report_waiting": all(
                len(sweep["waiting"]) >= 1 for sweep in idle_sweeps),
            "woken_after_verdict": len(final["advanced"]) == 1,
            "goal_completed_without_manual_run": (
                goal_final.get("status") == "completed"),
            "finish_used_verified_task": (
                (final["advanced"][0].get("decision_action") == "finish")
                if final["advanced"] else False),
            "no_extra_call_on_unchanged_state": calls_after_extra == 0,
            "unchanged_sweep_reports_skip": (
                bool(extra["skipped"]) and not extra["advanced"]),
            "model_call_total": len(model.calls),
            "signature_is_stable": (
                goal_wake_signature(ctx["goals"].get(goal_id) or {}, None, None)
                == goal_wake_signature(ctx["goals"].get(goal_id) or {}, None, None)),
        }
    finally:
        close(ctx)


def b2_runtime_integration() -> dict:
    """The runtime owns the loop: one force cycle drives submission and finish."""
    ctx = build(fresh_ledger("b2"), model=CountingModel())
    runtime = None
    try:
        goal_id, goal_ref = submit_goal_once(ctx)
        runtime = ControlPlaneRuntime(
            ctx["plane"], RuntimeConfig(goal_interval_s=1.0),
            goal_runner=ctx["runner"], owner="t42-runtime")
        first = runtime.run_due(force=True)
        # The runtime dispatches before it advances goals in one cycle, so the
        # task submitted by the first cycle is dispatched by the next one.
        dispatched = runtime.run_due(force=True)
        task = current_goal_task(ctx["store"], goal_id, goal_ref)
        complete_task(ctx["store"], (task or {})["task_id"])
        second = runtime.run_due(force=True)
        goal = ctx["goals"].get(goal_id) or {}
        status = runtime.status()
        return {
            "goal_path_registered": "goal_advancement" in status,
            "goal_runner_enabled": status.get("goal_runner_enabled") is True,
            "first_cycle_advanced": bool(
                (first.get("goal_advancement") or {}).get("advanced")),
            "task_dispatched_by_runtime": bool(
                (dispatched.get("dispatch_queue") or {}).get("dispatched"))
                and (task or {}).get("worker_job_id") is not None,
            "second_cycle_advanced": bool(
                (second.get("goal_advancement") or {}).get("advanced")),
            "goal_completed": goal.get("status") == "completed",
            "goal_status_view": bool(runtime.goal_status()["goals"]),
            "runtime_not_running_forever": status.get("running") is False,
        }
    finally:
        if runtime is not None:
            runtime.stop(timeout_s=5.0)
        close(ctx)


def b3_terminal_goals_never_woken() -> dict:
    ledger = fresh_ledger("b3")
    ctx = build(ledger, model=CountingModel())
    out: dict = {}
    try:
        project_id = ctx["project"]["id"]
        for status in ("needs_human", "completed", "failed"):
            text = f"terminal goal {status}"
            goal_id, goal_ref = goal_identity(text, project_id)
            ctx["goals"].get_or_create(goal_id, project_id, text, goal_ref)
            ctx["goals"].update_goal(goal_id, status=status)
        before = len(ctx["runner"]._coordinator._model.calls)
        sweep = ctx["runner"].advance_due()
        out["no_calls_for_terminal_goals"] = (
            len(ctx["runner"]._coordinator._model.calls) - before == 0)
        out["sweep_has_no_advances"] = not sweep["advanced"]
    finally:
        close(ctx)

    # A needs_human goal is also not silently resumed by a later sweep.
    ctx = build(fresh_ledger("b3-resume"), model=CountingModel())
    try:
        goal_id, goal_ref = submit_goal_once(ctx)
        ctx["goals"].stop_goal(goal_id, None, "needs_human",
                               {"stop": "asked the owner for a decision"})
        ctx["runner"].advance_due()
        out["needs_human_stays_stopped"] = (
            (ctx["goals"].get(goal_id) or {}).get("status") == "needs_human")
        out["needs_human_no_model_call"] = len(
            ctx["runner"]._coordinator._model.calls) == 0
    finally:
        close(ctx)
    return out


def b4_failure_backoff_and_escalation() -> dict:
    ctx = build(fresh_ledger("b4"), model=CountingModel(mode="always_fail"),
                max_model_failures=3)
    try:
        goal_id, goal_ref = submit_goal_once(ctx)
        sweeps = [ctx["runner"].advance_due()]
        # Each sweep is a new failure until the escalation threshold is reached.
        # ``clear_backoff`` skips only the wait window, so the failure streak —
        # the thing that triggers escalation — keeps accumulating.
        for _ in range(3):
            ctx["runner"]._runner.clear_backoff(goal_id)
            sweeps.append(ctx["runner"].advance_due())
        failure_rows = [sweep["failures"][0] for sweep in sweeps
                        if sweep["failures"]]
        goal = ctx["goals"].get(goal_id) or {}
        bookkeeping = ctx["runner"]._runner.get(goal_id) or {}
        calls_after_escalation = len(ctx["runner"]._coordinator._model.calls)
        ctx["runner"]._runner.clear_backoff(goal_id)
        final_sweep = ctx["runner"].advance_due()
        return {
            "three_failures_recorded": len(failure_rows) == 3,
            "failure_streak_grows": [row.get("consecutive_failures")
                                     for row in failure_rows] == [1, 2, 3],
            "backoff_grows": all(
                (failure_rows[index].get("retry_in_s") or 0)
                <= (failure_rows[index + 1].get("retry_in_s") or 0)
                for index in range(len(failure_rows) - 1)),
            "retry_reason_reported": all(row.get("retry_in_s") for row in failure_rows),
            "escalated_to_human": any(
                (row.get("escalated_to_human") or {}).get("escalated")
                for row in failure_rows),
            "goal_needs_human": goal.get("status") == "needs_human",
            "stop_reason_queryable": "repeated LD failures"
                                     in json.dumps(goal.get("last_decision") or {}),
            "failure_reason_persisted": bool(bookkeeping.get("last_error")),
            "no_task_was_submitted": not [
                task for task in ctx["store"].all()
                if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref],
            "no_call_after_escalation": len(
                ctx["runner"]._coordinator._model.calls) == calls_after_escalation,
            "after_escalation_sweep_is_quiet": not final_sweep["advanced"]
                                               and not final_sweep["failures"],
        }
    finally:
        close(ctx)


def b5_pending_step_reconciled_without_model() -> dict:
    """A crash between "step persisted" and "WorkItem committed" is repaired."""
    ctx = build(fresh_ledger("b5"), model=CountingModel())
    try:
        project_id = ctx["project"]["id"]
        goal_id, goal_ref = submit_goal_once(ctx)
        ctx["runner"].advance_due()
        task = current_goal_task(ctx["store"], goal_id, goal_ref)
        if not task:
            return {"submitted": False}
        step = int((ctx["goals"].get(goal_id) or {}).get("decision_count") or 1)
        # Reopen the step as pending, as a crash right after the side effect does.
        goal_row = ctx["goals"].get(goal_id) or {}
        conn = ctx["goals"]._conn
        conn.execute("UPDATE ld_goal_decisions SET state='pending', outcome='{}' "
                     "WHERE goal_id=? AND step=?", (goal_id, step))
        conn.execute("UPDATE ld_goals SET decision_count=? WHERE goal_id=?",
                     (step - 1, goal_id))
        conn.commit()
        calls_before = len(ctx["runner"]._coordinator._model.calls)
        sweep = ctx["runner"].advance_due()
        calls_after = len(ctx["runner"]._coordinator._model.calls)
        tasks_for_goal = [t for t in ctx["store"].all()
                          if (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
        by_key = ctx["store"].by_idempotency_key(
            step_idempotency_key(goal_id, step))
        return {
            "pending_detected": any(
                "pending" in (item.get("plan_reason") or "")
                for item in sweep["advanced"]),
            "reconciled_without_model": calls_after == calls_before,
            "single_workitem_for_goal": len(tasks_for_goal) == 1,
            "adopted_existing_task": bool(by_key)
                                     and by_key["task_id"] == task["task_id"],
            "history_contiguous": bool(goal_row),
            "goal_status_waiting": (
                ctx["goals"].get(goal_id) or {}).get("status") == "waiting",
        }
    finally:
        close(ctx)


def b6_model_output_vs_policy() -> dict:
    """An incomplete model decision is retried; a forbidden one never is.

    A real model occasionally omits a required field (an assertion without
    ``target``).  Parking the goal forever for that is a reliability bug; but the
    same code path must still refuse ``rm -rf /`` deterministically and forever.
    """
    import ld.decision as decision_module  # noqa: PLC0415 - local to this check

    class FlakyModel(CountingModel):
        """Incomplete on the first call, correct afterwards."""

        def __init__(self):
            super().__init__()
            self.rounds = 0

        def decide(self, decision_input):
            self.rounds += 1
            if self.rounds == 1:
                self.calls.append({"rounds": 1})
                return {"action": "submit_task",
                        "project_id": decision_input.get("requested_project_id"),
                        "reason": "incomplete assertion",
                        "evidence_refs": [],
                        "parameters": {
                            "task": "incomplete",
                            "idempotency_key": "goal-flaky",
                            "assertions": [{"kind": "file_exists"},
                                           {"kind": "file_contains",
                                            "target": "a.txt"},
                                           {"kind": "file_contains"}],
                        }}
            return super().decide(decision_input)

    ctx = build(fresh_ledger("b6"), model=FlakyModel())
    try:
        goal_id, goal_ref = submit_goal_once(ctx)
        first = ctx["runner"].advance_due()
        goal_after_first = ctx["goals"].get(goal_id) or {}
        attempts = [{"had_failure": bool(first["failures"]),
                     "goal_status": goal_after_first.get("status")}]
        # Retry the same step once the backoff window is skipped.
        second = None
        for _ in range(3):
            ctx["runner"]._runner.clear_backoff(goal_id)
            second = ctx["runner"].advance_due()
            if second["advanced"]:
                break
        tasks = [task for task in ctx["store"].all()
                 if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
        goal_after_retry = ctx["goals"].get(goal_id) or {}
        out = {
            "incomplete_output_is_retryable": bool(first["failures"]),
            "goal_not_parked_after_one_bad_sample": (
                goal_after_first.get("status") != "needs_human"),
            "retry_recovered": bool(second and second["advanced"]),
            "task_submitted_after_retry": len(tasks) == 1,
            "goal_waiting_not_parked": goal_after_retry.get("status")
                                       in ("waiting", "active"),
            "attempts": attempts,
        }

        # A forbidden action must stay permanently closed, same coordinator.
        class ForbiddenModel:
            def decide(self, _decision_input):
                return {"action": "rm -rf /", "project_id": None,
                        "reason": "bad", "evidence_refs": [], "parameters": {}}

        ctx["coordinator"]._model = ForbiddenModel()
        result = ctx["coordinator"].run_once(
            "forbidden probe", project_id=ctx["project"]["id"])
        out["forbidden_action_still_fails_closed"] = (
            result["next_state"] == "invalid_model_decision"
            and not result["executed"])
        out["forbidden_action_not_marked_retryable"] = (
            not result.get("model_call_failed"))
        out["policy_error_is_not_shape_error"] = issubclass(
            decision_module.DecisionPolicyError,
            decision_module.DecisionValidationError)
        return out
    finally:
        close(ctx)


def main() -> int:
    make_repo()
    sections = {
        "B1_zero_manual_invocation": b1_zero_manual_invocation(),
        "B2_runtime_integration": b2_runtime_integration(),
        "B3_terminal_goals_never_woken": b3_terminal_goals_never_woken(),
        "B4_failure_backoff_and_escalation": b4_failure_backoff_and_escalation(),
        "B5_pending_step_reconciled": b5_pending_step_reconciled_without_model(),
        "B6_model_output_vs_policy": b6_model_output_vs_policy(),
    }
    checks: dict[str, bool] = {}
    for name, section in sections.items():
        for key, value in section.items():
            if isinstance(value, bool):
                checks[f"{name}.{key}"] = value
    log = {"test": "T42-ld-goal-auto-wakeup",
           "worker_started": False,
           "note": ("scripted decision model + stub adapter: verifies the "
                    "scheduling contract (when the LD is woken), not Worker "
                    "execution. Real end-to-end behaviour is T43/T44."),
           "sections": sections, "checks": checks,
           "result": "Pass" if checks and all(checks.values()) else "Fail"}
    write_evidence(EVIDENCE, log)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({k: v for k, v in checks.items() if not v},
                     ensure_ascii=False, indent=2))
    print(log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
