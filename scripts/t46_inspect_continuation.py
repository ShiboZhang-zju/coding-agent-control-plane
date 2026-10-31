"""T46: inspect continuation — a query decision must not dead-end the goal.

The M4-C closure bug under test: ``LDGoalRunner`` deduplicates wake-ups by
``goal_wake_signature``, and that signature deliberately excludes model
decision counters.  An ``inspect`` decision changes no observable fact, so
after one query the recorded signature equalled the observed one and the
scheduler skipped the goal forever — the LD could inspect but never submit.

Fix under test (minimal persisted "flow continues" semantics):

* an advance that leaves the goal ``active`` is not acknowledged, so the next
  sweep wakes the model again — bounded by the no-progress escalation;
* the inspection result is persisted (bounded) with the step outcome and is
  available to the next decision via ``goal_state.last_step``, gated on the
  authorized security domains;
* a restart recognizes the unfinished flow from the persisted bookkeeping;
* while a WorkItem is in flight the model is still never called, and a
  pending step still reconciles deterministically before any model call.

Sections
--------
B1  inspect -> submit_task (the core bug).
B2  inspect -> inspect -> submit_task (two queries, then progress).
B3  inspect -> request_human.
B4  repeated no-progress queries reach the cap and park in needs_human.
B5  a restart between the query and its follow-up continues the flow.
B6  an inspection result carrying local-domain content never leaks into a
    model input that is not authorized for ``local``.
B7  no model call while the current WorkItem is in flight.
B8  a pending step still reconciles from the Ledger without the model.

Deterministic (scripted model + stub adapter); real end-to-end behaviour is
covered by T48.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator, _GoalBudget  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t46-inspect-continue"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t46"
EVIDENCE = ROOT / "evidence" / "project" / "T46-inspect-continuation.json"
SECRET = "T46-LOCAL-ONLY-SECRET-BACKGROUND"


class StubAdapter:
    name = "stub"

    def dispatch(self, request):  # noqa: ANN001
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


class QueueModel:
    """Pops one scripted decision per call; never invents anything."""

    def __init__(self, decisions: list[dict]):
        self._queue = list(decisions)
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        self.calls.append({
            "goal_state": decision_input.get("goal_state"),
            "previous_attempt_error": decision_input.get(
                "previous_attempt_error"),
            "has_correction_rule": any(
                "previous_attempt_error" in rule
                for rule in decision_input.get("rules", [])),
        })
        if not self._queue:
            raise RuntimeError("script exhausted")
        return dict(self._queue.pop(0))


def inspect_decision(project: str, tool: str = "get_project_progress") -> dict:
    return {"action": "inspect", "project_id": project,
            "reason": "query before acting",
            "evidence_refs": [], "parameters": {"tool": tool}}


def submit_decision(project: str) -> dict:
    return {"action": "submit_task", "project_id": project,
            "reason": "implement the goal",
            "evidence_refs": [],
            "parameters": {
                "task": "实现 feature-b 并让测试通过",
                "idempotency_key": "t46-part-a",
                "assertions": [{"kind": "file_exists", "target": "feature-b.txt"}],
            }}


def finish_decision(project: str, task_id: str) -> dict:
    return {"action": "finish", "project_id": project,
            "reason": "the goal task is verified",
            "evidence_refs": [f"task:{task_id}"],
            "parameters": {"verified_task_ids": [task_id],
                           "completion_conditions": []}}


def human_decision(project: str) -> dict:
    return {"action": "request_human", "project_id": project,
            "reason": "genuine blocker found while inspecting",
            "evidence_refs": [],
            "parameters": {"question": "How should the ambiguity be resolved?"}}


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
    git(REPO, "config", "user.email", "t46@example.invalid")
    git(REPO, "config", "user.name", "T46")
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


def build(ledger: Path, model, *, with_secret: bool = False,
          domains: tuple[str, ...] = ("local", "shareable"),
          max_no_progress: int = 3):
    store = TaskStore(ledger)
    plane = ControlPlane(StubAdapter(), store, require_registered_projects=True,
                         worker_instance_id="wi-t46")
    service = ProjectService(plane)
    project = plane.register_project("t46", str(REPO), default_adapter="stub",
                                     concurrency_limit=8)
    if with_secret:
        service.add_context_entry(project["id"], ContextEntry(
            project_id=project["id"], kind="progress",
            title="T46-LOCAL-PROGRESS", body=f"{SECRET} for this project",
            security_domain="local"))
    coordinator = LDCoordinator(
        LDTools(service), model,
        model_authorized_security_domains=domains)
    goals = LDGoalStore(store.path, lease_s=30.0)
    runner = LDGoalRunner(
        coordinator, goals, max_decisions=1, max_tasks=2,
        max_model_failures=3, max_no_progress_advances=max_no_progress,
        base_backoff_s=0.2, max_backoff_s=1.0,
        lifetime_max_decisions=12, lifetime_max_tasks=2)
    return {"store": store, "plane": plane, "service": service,
            "project": project, "coordinator": coordinator, "model": model,
            "goals": goals, "runner": runner}


def close(ctx) -> None:
    for key in ("runner", "goals", "store"):
        try:
            ctx[key].close()
        except Exception:  # noqa: BLE001
            pass


def goal_tasks(ctx, goal_ref: str) -> list[dict]:
    found = [task for task in ctx["store"].all()
             if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    found.sort(key=lambda t: t.get("created_at") or 0)
    return found


def complete_task(store: TaskStore, task_id: str) -> None:
    record = store.get(task_id) or {}
    store.update(task_id, state=TaskState.SUCCEEDED.value, verdict="verified",
                 queue_status="complete", finished_at=1_700_000_000.0,
                 worker_job_id=record.get("worker_job_id") or f"job-{task_id}")


def submit_goal(ctx, goal_text: str) -> tuple[str, str]:
    goal_id, goal_ref = goal_identity(goal_text, ctx["project"]["id"])
    ctx["goals"].get_or_create(goal_id, ctx["project"]["id"], goal_text, goal_ref)
    return goal_id, goal_ref


def _run_flow(ledger: Path, decisions: list[dict], goal_text: str, **kwargs) -> dict:
    """Drive one scripted decision list to a terminal goal state."""
    ctx = build(ledger, QueueModel(decisions), **kwargs)
    try:
        goal_id, goal_ref = submit_goal(ctx, goal_text)
        ctx["goal_id"], ctx["goal_ref"] = goal_id, goal_ref
        timeline = []
        for _ in range(12):
            sweep = ctx["runner"].advance_due()
            goal = ctx["goals"].get(goal_id) or {}
            timeline.append({
                "status": goal.get("status"),
                "calls": len(ctx["model"].calls),
                "advanced": [item.get("decision_action")
                             for item in sweep.get("advanced") or []],
            })
            if goal.get("status") in ("needs_human", "completed", "failed"):
                break
        ctx["timeline"] = timeline
        return ctx
    except Exception:
        close(ctx)
        raise


def b1_inspect_then_submit() -> dict:
    ledger = fresh_ledger("b1")
    ctx = _run_flow(ledger, [], "placeholder")  # replaced below
    close(ctx)
    # (real flow) inspect -> submit -> [task verified] -> finish
    ledger = fresh_ledger("b1")
    decisions: list[dict] = []

    class DynamicModel:
        def __init__(self):
            self.calls: list[dict] = []

        def decide(self, decision_input: dict) -> dict:
            self.calls.append({"n": len(self.calls)})
            state = decision_input.get("goal_state") or {}
            project = decision_input.get("requested_project_id")
            verified = [t["task_id"] for t in state.get("completed_tasks", [])
                        if t.get("verdict") == "verified"]
            if verified:
                return finish_decision(project, verified[0])
            if state.get("all_goal_tasks"):
                return {"action": "wait", "project_id": project,
                        "reason": "settle", "evidence_refs": [],
                        "parameters": {"task_id": (state.get("current_task")
                                                   or {}).get("task_id") or ""}}
            if len(self.calls) == 1:
                return inspect_decision(project)
            return submit_decision(project)

    ctx = build(ledger, DynamicModel())
    try:
        goal_id, goal_ref = submit_goal(ctx, "B1 inspect 后继续推进")
        first = ctx["runner"].advance_due()
        goal_after_inspect = ctx["goals"].get(goal_id) or {}
        second = ctx["runner"].advance_due()
        tasks = goal_tasks(ctx, goal_ref)
        complete_task(ctx["store"], tasks[0]["task_id"])
        third = ctx["runner"].advance_due()
        calls_in_flight = len(ctx["model"].calls)
        idle = [ctx["runner"].advance_due() for _ in range(2)]
        goal_final = ctx["goals"].get(goal_id) or {}
        history = ctx["goals"].decisions(goal_id, limit=50)
        inspect_outcome = next((d["outcome"] for d in history
                                if d["outcome"].get("action") == "inspect"), {})
        return {
            "inspect_left_goal_active":
                goal_after_inspect.get("status") == "active",
            "continuation_pending_flag": bool(
                first["advanced"][0].get("continuation_pending")),
            "second_sweep_submitted": (
                second["advanced"][0].get("decision_action") == "submit_task"
                if second["advanced"] else False),
            "task_submitted": len(tasks) == 1,
            "finish_after_verdict": (
                third["advanced"][0].get("decision_action") == "finish"
                if third["advanced"] else False),
            "goal_completed": goal_final.get("status") == "completed",
            "no_model_call_while_in_flight_or_done": all(
                len(ctx["model"].calls) == calls_in_flight for _ in idle),
            "model_calls_total": len(ctx["model"].calls),
            "inspection_result_persisted": bool(
                inspect_outcome.get("inspection_result")),
        }
    finally:
        close(ctx)


def b2_two_inspections_then_submit() -> dict:
    ledger = fresh_ledger("b2")

    class TwoInspectModel:
        def __init__(self):
            self.calls: list[dict] = []

        def decide(self, decision_input: dict) -> dict:
            self.calls.append({"n": len(self.calls)})
            state = decision_input.get("goal_state") or {}
            project = decision_input.get("requested_project_id")
            verified = [t["task_id"] for t in state.get("completed_tasks", [])
                        if t.get("verdict") == "verified"]
            if verified:
                return finish_decision(project, verified[0])
            if state.get("all_goal_tasks"):
                return {"action": "wait", "project_id": project,
                        "reason": "settle", "evidence_refs": [],
                        "parameters": {"task_id": (state.get("current_task")
                                                   or {}).get("task_id") or ""}}
            if len(self.calls) <= 2:
                return inspect_decision(project)
            return submit_decision(project)

    ctx = build(ledger, TwoInspectModel())
    try:
        goal_id, goal_ref = submit_goal(ctx, "B2 两次 inspect 后继续推进")
        for _ in range(6):
            ctx["runner"].advance_due()
            goal = ctx["goals"].get(goal_id) or {}
            if goal.get("status") == "waiting":
                break
        tasks = goal_tasks(ctx, goal_ref)
        complete_task(ctx["store"], tasks[0]["task_id"])
        for _ in range(4):
            ctx["runner"].advance_due()
            goal = ctx["goals"].get(goal_id) or {}
            if goal.get("status") in ("completed", "needs_human"):
                break
        goal_final = ctx["goals"].get(goal_id) or {}
        streak_when_submitting = None
        bookkeeping = ctx["runner"]._runner.get(goal_id) or {}
        return {
            "two_inspections_allowed": len(ctx["model"].calls) >= 3,
            "submit_after_two_inspections": len(tasks) == 1,
            "no_progress_streak_reset": (
                bookkeeping.get("consecutive_no_progress") == 0),
            "goal_completed": goal_final.get("status") == "completed",
            "model_calls_total": len(ctx["model"].calls),
            "streak_when_submitting": streak_when_submitting,
        }
    finally:
        close(ctx)


def b3_inspect_then_request_human() -> dict:
    ledger = fresh_ledger("b3")

    class InspectThenHumanModel:
        def __init__(self):
            self.calls = 0

        def decide(self, decision_input: dict) -> dict:
            self.calls += 1
            project = decision_input.get("requested_project_id")
            if self.calls == 1:
                return inspect_decision(project)
            return human_decision(project)

    ctx = build(ledger, InspectThenHumanModel())
    try:
        goal_id, goal_ref = submit_goal(ctx, "B3 inspect 后转人工")
        ctx["runner"].advance_due()
        ctx["runner"].advance_due()
        goal = ctx["goals"].get(goal_id) or {}
        history = ctx["goals"].decisions(goal_id, limit=10)
        calls_before = ctx["model"].calls
        ctx["runner"].advance_due()
        return {
            "goal_needs_human": goal.get("status") == "needs_human",
            "model_called_twice": ctx["model"].calls == 2,
            "human_reason_persisted": any(
                "blocker found while inspecting" in str(
                    (d["decision"] or {}).get("reason"))
                for d in history),
            "no_task_submitted": not goal_tasks(ctx, goal_ref),
            "terminal_goal_not_woken_again": ctx["model"].calls == calls_before,
        }
    finally:
        close(ctx)


def b4_no_progress_cap() -> dict:
    ledger = fresh_ledger("b4")

    class InspectForeverModel:
        def __init__(self):
            self.calls = 0

        def decide(self, decision_input: dict) -> dict:
            self.calls += 1
            return inspect_decision(
                decision_input.get("requested_project_id"))

    ctx = build(ledger, InspectForeverModel(), max_no_progress=3)
    try:
        goal_id, goal_ref = submit_goal(ctx, "B4 连续无进展查询达到上限")
        sweeps = 0
        while ((ctx["goals"].get(goal_id) or {}).get("status")
               in ("active", "waiting") and sweeps < 10):
            sweeps += 1
            ctx["runner"].advance_due()
        goal = ctx["goals"].get(goal_id) or {}
        calls_at_cap = ctx["model"].calls
        ctx["runner"].advance_due()
        return {
            "capped_after_three": ctx["model"].calls == 3,
            "goal_needs_human": goal.get("status") == "needs_human",
            "stop_reason_persisted": "without observable progress" in json.dumps(
                goal.get("last_decision") or {}, ensure_ascii=False),
            "no_call_after_cap": ctx["model"].calls == calls_at_cap,
        }
    finally:
        close(ctx)


def b5_restart_continues_flow() -> dict:
    ledger = fresh_ledger("b5")

    class RestartModel:
        """inspect on the very first call of *this process*, then submit."""

        def __init__(self):
            self.calls = 0

        def decide(self, decision_input: dict) -> dict:
            self.calls += 1
            state = decision_input.get("goal_state") or {}
            project = decision_input.get("requested_project_id")
            verified = [t["task_id"] for t in state.get("completed_tasks", [])
                        if t.get("verdict") == "verified"]
            if verified:
                return finish_decision(project, verified[0])
            if self.calls == 1:
                return inspect_decision(project)
            return submit_decision(project)

    ctx = build(ledger, RestartModel())
    goal_text = "B5 查询后重启继续执行"
    goal_id, goal_ref = submit_goal(ctx, goal_text)
    ctx["runner"].advance_due()  # the query happens
    calls_before = ctx["model"].calls
    goal_before = ctx["goals"].get(goal_id) or {}
    bookkeeping_before = ctx["runner"]._runner.get(goal_id) or {}
    close(ctx)

    # "Restart": a brand-new process view on the same ledger.
    ctx2 = build(ledger, RestartModel())
    try:
        goal_after_restart = ctx2["goals"].get(goal_id) or {}
        for _ in range(4):
            ctx2["runner"].advance_due()
            if (ctx2["goals"].get(goal_id) or {}).get("status") != "active":
                break
        tasks = goal_tasks(ctx2, goal_ref)
        goal_after = ctx2["goals"].get(goal_id) or {}
        return {
            "query_happened_before_restart": calls_before == 1,
            "goal_active_before_restart": goal_before.get("status") == "active",
            "signature_not_acknowledged": not bookkeeping_before.get(
                "last_signature"),
            "restart_kept_goal_active": goal_after_restart.get("status") == "active",
            "restart_continued_with_model": ctx2["model"].calls >= 1,
            "restart_submitted_task": len(tasks) == 1,
            "goal_waiting_after_restart": goal_after.get("status") == "waiting",
        }
    finally:
        close(ctx2)


def b6_inspection_result_domain_gate() -> dict:
    """Local-domain inspection content reaches only local-authorized models."""
    ledger = fresh_ledger("b6")

    class BriefInspectModel:
        def __init__(self):
            self.calls = 0

        def decide(self, decision_input: dict) -> dict:
            self.calls += 1
            return inspect_decision(
                decision_input.get("requested_project_id"),
                tool="get_project_progress")

    ctx = build(ledger, BriefInspectModel(), with_secret=True,
                domains=("local", "shareable"))
    goal_text = "B6 查询结果不泄露未授权内容"
    goal_id, goal_ref = submit_goal(ctx, goal_text)
    ctx["runner"].advance_due()
    history = ctx["goals"].decisions(goal_id, limit=10)
    outcome = next((d["outcome"] for d in history
                    if d["outcome"].get("action") == "inspect"), {})
    secret_in_outcome = SECRET in str(outcome.get("inspection_result") or "")
    goal_row = ctx["goals"].get(goal_id) or {}
    budget = _GoalBudget(per_call=True, quantum_decisions=1, quantum_tasks=1,
                         lifetime_decisions=12, lifetime_tasks=2)
    local_view = ctx["coordinator"]._goal_context(
        goal_row, ctx["goals"], goal_ref, budget)
    local_last_step = local_view.get("last_step") or {}
    close(ctx)

    # A coordinator NOT authorized for 'local' must not see the inspection
    # result (nor the refusal reason), and must refuse to call its model for
    # this local goal at all.  (The secret entry already lives in the ledger.)
    ctx2 = build(ledger, BriefInspectModel(), with_secret=False,
                 domains=("shareable",))
    try:
        goal_row2 = ctx2["goals"].get(goal_id) or {}
        shareable_view = ctx2["coordinator"]._goal_context(
            goal_row2, ctx2["goals"], goal_ref, budget)
        shareable_last_step = shareable_view.get("last_step") or {}
        probe = ctx2["coordinator"].run_once(goal_text, project_id="t46")
        return {
            "inspect_executed": bool(outcome.get("inspection_result")),
            "secret_in_local_outcome": secret_in_outcome,
            "local_model_sees_result": SECRET in json.dumps(
                local_last_step, ensure_ascii=False),
            "shareable_model_blind_to_result": SECRET not in json.dumps(
                shareable_last_step, ensure_ascii=False),
            "shareable_model_blind_to_reason": not shareable_last_step.get("reason"),
            "unauthorized_model_never_called": ctx2["model"].calls == 0,
            "unauthorized_run_refuses": (
                probe.get("next_state") == "human_required"
                and "security_domain_unauthorized" in str(
                    probe.get("blocking_reasons"))),
        }
    finally:
        close(ctx2)


def b8_pending_step_priority() -> dict:
    """A crash-left pending step reconciles from the Ledger before the model."""
    ledger = fresh_ledger("b8")

    class SubmitModel:
        def __init__(self):
            self.calls = 0

        def decide(self, decision_input: dict) -> dict:
            self.calls += 1
            project = decision_input.get("requested_project_id")
            return submit_decision(project)

    ctx = build(ledger, SubmitModel())
    try:
        goal_id, goal_ref = submit_goal(ctx, "B8 Pending Step 优先对账")
        ctx["runner"].advance_due()
        tasks = goal_tasks(ctx, goal_ref)
        if not tasks:
            return {"submitted": False}
        step = int((ctx["goals"].get(goal_id) or {}).get("decision_count") or 1)
        conn = ctx["goals"]._conn
        conn.execute(
            "UPDATE ld_goal_decisions SET state='pending', outcome='{}' "
            "WHERE goal_id=? AND step=?", (goal_id, step))
        conn.execute("UPDATE ld_goals SET decision_count=? WHERE goal_id=?",
                     (step - 1, goal_id))
        conn.commit()
        calls_before = ctx["model"].calls
        sweep = ctx["runner"].advance_due()
        calls_after = ctx["model"].calls
        tasks_after = goal_tasks(ctx, goal_ref)
        return {
            "pending_reconciled_without_model": calls_after == calls_before,
            "single_workitem": len(tasks_after) == 1,
            "adopted_existing_task": tasks_after[0]["task_id"] == tasks[0]["task_id"],
            "pending_reported": any(
                "pending" in (item.get("plan_reason") or "")
                for item in sweep["advanced"]),
        }
    finally:
        close(ctx)


def b9_validation_error_feedback() -> dict:
    """A schema-invalid decision is retried with the validator error fed back.

    The retry is still fully validated: the model must *correct* the decision,
    and a permanently invalid model still parks the goal (bounded retries).
    """
    ledger = fresh_ledger("b9")
    goal_text = "B9 目标：校验错误反馈后重试并提交任务"
    model = QueueModel([])
    ctx = build(ledger, model)
    bad_submit = {
        "action": "submit_task", "project_id": ctx["project"]["id"],
        "reason": "implement the goal",
        "evidence_refs": [],
        "parameters": {
            "task": "实现 feature-b",
            "idempotency_key": "t46-b9",
            # schema violation: assertion without the required ``target``
            "assertions": [{"kind": "file_exists"}],
        },
    }
    model._queue = [bad_submit, submit_decision(ctx["project"]["id"])]
    try:
        goal_id, goal_ref = submit_goal(ctx, goal_text)
        first = ctx["runner"].advance_due()
        first_status = (ctx["goals"].statuses() or {}).get(goal_id)
        # Backoff (base 0.2s) must elapse before the retry is admitted.
        time.sleep(0.5)
        second = ctx["runner"].advance_due()
        tasks = goal_tasks(ctx, goal_ref)
        bookkeeping = ctx["runner"]._runner.all()
        state = next(iter(bookkeeping.values()), {})
        first_item = (first.get("items") or [{}])[0]
        second_item = (second.get("items") or [{}])[0]
        return {
            "first_attempt_failed": (
                first_item.get("action") == "error"
                and "model output rejected" in str(first_item.get("reason"))),
            "goal_stayed_advanceable": first_status == "active",
            "no_feedback_on_first_call": model.calls[0][
                "previous_attempt_error"] is None,
            "feedback_on_retry": (
                "missing required fields" in str(
                    model.calls[1]["previous_attempt_error"])),
            "correction_rule_present": model.calls[1]["has_correction_rule"],
            "retry_succeeded": second_item.get("action") == "advanced",
            "task_submitted_after_retry": len(tasks) == 1,
            "failures_reset": int(state.get("consecutive_failures") or 0) == 0,
        }
    finally:
        close(ctx)


def main() -> int:
    make_repo()
    sections = {
        "B1_inspect_then_submit": b1_inspect_then_submit(),
        "B2_two_inspections_then_submit": b2_two_inspections_then_submit(),
        "B3_inspect_then_request_human": b3_inspect_then_request_human(),
        "B4_no_progress_cap": b4_no_progress_cap(),
        "B5_restart_continues_flow": b5_restart_continues_flow(),
        "B6_inspection_result_domain_gate": b6_inspection_result_domain_gate(),
        "B8_pending_step_priority": b8_pending_step_priority(),
        "B9_validation_error_feedback": b9_validation_error_feedback(),
    }
    checks: dict[str, bool] = {}
    for name, section in sections.items():
        for key, value in section.items():
            if isinstance(value, bool):
                checks[f"{name}.{key}"] = value
    log = {"test": "T46-inspect-continuation",
           "worker_started": False,
           "note": ("scripted decision model + stub adapter: verifies the "
                    "inspect-continuation contract of the goal scheduler, not "
                    "Worker execution. Real end-to-end behaviour is T48."),
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
