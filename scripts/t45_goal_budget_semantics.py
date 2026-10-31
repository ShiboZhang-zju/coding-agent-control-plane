"""T45: goal budget semantics — per-call quantum vs persisted lifetime budget.

The M4-C closure claim under test: a scheduler may wake the LD many times,
each wake-up granting a small decision/task quantum, while the *goal itself*
carries a persisted lifetime budget that no amount of scheduling, restarting
or human "continue" can enlarge.

Sections
--------
A1  quantum exhaustion is a normal yield: after an ``inspect`` decision with a
    quantum of 1, the goal stays advanceable (never ``needs_human``) and the
    next sweep continues the flow.
A2  the lifetime decision budget bounds every wake-up combined: repeated
    sweeps can never spend more model decisions than the stamped lifetime.
A3  the lifetime task budget bounds total WorkItem submissions across sweeps.
A4  the lifetime budget survives a process restart (fresh store + runner on
    the same ledger) and is not reset.
A5  a human ``resume`` never mints budget; only the explicit human-only
    ``set_lifetime_budget`` can raise it.
A6  a legacy ledger (pre-budget schema) migrates in place and then behaves
    like A2 (the budget applies to pre-existing goals too).

The model is scripted and the adapter is a stub: this test is deterministic
and verifies the *budget contract*, not Worker execution.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import DispatchResult, TaskState, TaskSnapshot  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / "t45-goal-budget"
REPO = FIXTURE / "repo"
STATE = ROOT / ".poc-state" / "t45"
EVIDENCE = ROOT / "evidence" / "project" / "T45-goal-budget-semantics.json"


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


class ScriptedModel:
    """Counts real model calls; behaviour driven by ``mode``.

    * ``progress``: inspect once, then submit one task, then wait for it, then
      finish when it is verified (the canonical bounded flow).
    * ``inspect_forever``: always inspect (never submits) — burns decisions.
    * ``submit_forever``: always submit a fresh task; never finishes.
    """

    def __init__(self, mode: str = "progress"):
        self.mode = mode
        self.calls: list[dict] = []

    def decide(self, decision_input: dict) -> dict:
        state = decision_input.get("goal_state") or {}
        project = decision_input.get("requested_project_id")
        self.calls.append({
            "decision_count": state.get("decision_count"),
            "tasks_remaining": state.get("tasks_remaining"),
            "decisions_remaining": state.get("decisions_remaining"),
        })
        verified = [t["task_id"] for t in state.get("completed_tasks", [])
                    if t.get("verdict") == "verified"]
        if self.mode == "inspect_forever":
            return {"action": "inspect", "project_id": project,
                    "reason": "look at the project progress",
                    "evidence_refs": [], "parameters": {"tool": "get_project_progress"}}
        if self.mode == "submit_forever":
            submitted = len(state.get("all_goal_tasks") or [])
            return {"action": "submit_task", "project_id": project,
                    "reason": "another task",
                    "evidence_refs": [],
                    "parameters": {
                        "task": f"task number {submitted + 1}",
                        "idempotency_key": f"t45-part-{submitted + 1}",
                        "assertions": [{"kind": "file_exists",
                                        "target": f"part{submitted + 1}.txt"}],
                    }}
        # progress mode
        if verified:
            return {"action": "finish", "project_id": project,
                    "reason": "the goal task is verified",
                    "evidence_refs": [f"task:{tid}" for tid in verified],
                    "parameters": {"verified_task_ids": verified,
                                   "completion_conditions": []}}
        current = state.get("current_task") or {}
        if current.get("task_id") and current.get("verdict") != "verified":
            return {"action": "wait", "project_id": project,
                    "reason": "wait for the running task",
                    "evidence_refs": [f"task:{current['task_id']}"],
                    "parameters": {"task_id": current["task_id"]}}
        if state.get("all_goal_tasks"):
            return {"action": "wait", "project_id": project,
                    "reason": "task settled; continue next sweep",
                    "evidence_refs": [], "parameters": {"task_id": ""}}
        if not self.calls[:-1]:
            return {"action": "inspect", "project_id": project,
                    "reason": "inspect progress before submitting",
                    "evidence_refs": [], "parameters": {"tool": "get_project_progress"}}
        return {"action": "submit_task", "project_id": project,
                "reason": "implement the goal",
                "evidence_refs": [],
                "parameters": {
                    "task": "实现 feature-a 并让测试通过",
                    "idempotency_key": "t45-part-a",
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
    git(REPO, "config", "user.email", "t45@example.invalid")
    git(REPO, "config", "user.name", "T45")
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


def build(ledger: Path, model, *, lifetime_decisions=None, lifetime_tasks=None,
          max_decisions: int = 1, max_model_failures: int = 3):
    store = TaskStore(ledger)
    plane = ControlPlane(StubAdapter(), store, require_registered_projects=True,
                         worker_instance_id="wi-t45")
    service = ProjectService(plane)
    project = plane.register_project("t45", str(REPO), default_adapter="stub",
                                     concurrency_limit=8)
    coordinator = LDCoordinator(
        LDTools(service), model,
        model_authorized_security_domains=("local", "shareable"))
    goals = LDGoalStore(store.path, lease_s=30.0)
    runner = LDGoalRunner(
        coordinator, goals, max_decisions=max_decisions, max_tasks=1,
        max_model_failures=max_model_failures,
        base_backoff_s=0.2, max_backoff_s=1.0,
        lifetime_max_decisions=lifetime_decisions,
        lifetime_max_tasks=lifetime_tasks)
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


def complete_task(store: TaskStore, task_id: str, *,
                  verdict: str = "verified") -> None:
    record = store.get(task_id) or {}
    store.update(task_id, state=TaskState.SUCCEEDED.value, verdict=verdict,
                 queue_status="complete", finished_at=1_700_000_000.0,
                 worker_job_id=record.get("worker_job_id") or f"job-{task_id}")


def submit_goal(ctx, goal_text: str) -> tuple[str, str]:
    goal_id, goal_ref = goal_identity(goal_text, ctx["project"]["id"])
    ctx["goals"].get_or_create(goal_id, ctx["project"]["id"], goal_text, goal_ref)
    return goal_id, goal_ref


def a1_quantum_yield() -> dict:
    """Quantum exhaustion after an inspect must not park the goal."""
    ctx = build(fresh_ledger("a1"), ScriptedModel("progress"),
                lifetime_decisions=8, lifetime_tasks=2)
    try:
        goal_id, goal_ref = submit_goal(ctx, "A1 预算语义：单次额度用完不转人工")
        first = ctx["runner"].advance_due()
        goal_after_first = ctx["goals"].get(goal_id) or {}
        bookkeeping = ctx["runner"]._runner.get(goal_id) or {}
        # Sweep 2: the quantum was spent on an inspect; the goal must still be
        # advanceable and the next sweep must continue (submit).
        second = ctx["runner"].advance_due()
        tasks = goal_tasks(ctx, goal_ref)
        goal_after_second = ctx["goals"].get(goal_id) or {}
        complete_task(ctx["store"], tasks[0]["task_id"])
        ctx["runner"].advance_due()  # wait/observe
        ctx["runner"].advance_due()  # finish
        goal_final = ctx["goals"].get(goal_id) or {}
        return {
            "first_sweep_inspected": (
                first["advanced"][0].get("decision_action") == "inspect"
                if first["advanced"] else False),
            "goal_active_after_quantum": goal_after_first.get("status") == "active",
            "not_needs_human_after_quantum":
                goal_after_first.get("status") != "needs_human",
            "continuation_pending_flag": bool(
                first["advanced"][0].get("continuation_pending")),
            "not_acknowledged": not bookkeeping.get("last_signature"),
            "second_sweep_submitted": (
                second["advanced"][0].get("decision_action") == "submit_task"
                if second["advanced"] else False),
            "goal_waiting_after_submit":
                goal_after_second.get("status") == "waiting",
            "goal_completed": goal_final.get("status") == "completed",
            "model_calls_total": len(ctx["model"].calls),
        }
    finally:
        close(ctx)


def a2_lifetime_decision_budget() -> dict:
    """No number of sweeps can spend more decisions than the lifetime."""
    ctx = build(fresh_ledger("a2"), ScriptedModel("inspect_forever"),
                lifetime_decisions=3)
    try:
        goal_id, _ = submit_goal(ctx, "A2 预算语义：总决策预算不可突破")
        sweeps = 0
        while ((ctx["goals"].get(goal_id) or {}).get("status")
               in ("active", "waiting") and sweeps < 12):
            sweeps += 1
            ctx["runner"].advance_due()
        goal = ctx["goals"].get(goal_id) or {}
        return {
            "model_calls_equal_lifetime": len(ctx["model"].calls) == 3,
            "goal_needs_human": goal.get("status") == "needs_human",
            "stop_reason_is_lifetime": "lifetime" in json.dumps(
                goal.get("last_decision") or {}, ensure_ascii=False),
            "budget_stamped_once": goal.get("max_decisions_total") == 3,
            "sweeps_ran": sweeps,
            "no_task_submitted": not [
                task for task in ctx["store"].all()
                if (task.get("metadata") or {}).get("ld_goal_ref")],
        }
    finally:
        close(ctx)


def a3_lifetime_task_budget() -> dict:
    """Total WorkItem submissions across sweeps are bounded.

    Tasks are marked ``rejected`` (a genuine verification failure) so the
    control plane never upgrades later submissions to session handoffs — the
    point here is the *budget*, not the handoff path (T38/T48 cover that).
    """
    ctx = build(fresh_ledger("a3"), ScriptedModel("submit_forever"),
                lifetime_decisions=24, lifetime_tasks=2)
    try:
        goal_id, goal_ref = submit_goal(ctx, "A3 预算语义：总任务预算不可突破")
        for _ in range(20):
            goal = ctx["goals"].get(goal_id) or {}
            if goal.get("status") not in ("active", "waiting"):
                break
            tasks = goal_tasks(ctx, goal_ref)
            pending = [t for t in tasks
                       if t.get("state") not in ("succeeded", "failed")]
            if pending:
                complete_task(ctx["store"], pending[0]["task_id"],
                              verdict="rejected")
            ctx["runner"].advance_due()
        goal = ctx["goals"].get(goal_id) or {}
        tasks = goal_tasks(ctx, goal_ref)
        history = ctx["goals"].decisions(goal_id, limit=100)
        return {
            "exactly_lifetime_tasks_submitted": len(tasks) == 2,
            "goal_parked_on_task_budget":
                goal.get("status") == "needs_human",
            "task_budget_reason_persisted": "task budget exhausted" in str(
                (history[-1].get("outcome") or {}).get("reason")
                if history else ""),
            "task_budget_stamped": goal.get("max_tasks_total") == 2,
            "all_submitted_tasks_counted":
                goal.get("task_count") == 2,
        }
    finally:
        close(ctx)


def a4_budget_survives_restart() -> dict:
    """The stamped lifetime budget binds a fresh process on the same ledger."""
    ledger = fresh_ledger("a4")
    ctx = build(ledger, ScriptedModel("inspect_forever"), lifetime_decisions=3)
    goal_text = "A4 预算语义：重启后总预算不重置"
    goal_id, _ = submit_goal(ctx, goal_text)
    ctx["runner"].advance_due()  # spend 1 decision
    spent_before = len(ctx["model"].calls)
    stamped = (ctx["goals"].get(goal_id) or {}).get("max_decisions_total")
    close(ctx)

    # Fresh store/runner (a "restarted" process) on the same ledger; it tries
    # to configure a *larger* lifetime, which must be ignored.
    ctx2 = build(ledger, ScriptedModel("inspect_forever"), lifetime_decisions=99)
    try:
        goal2 = ctx2["goals"].get(goal_id) or {}
        sweeps = 0
        while ((ctx2["goals"].get(goal_id) or {}).get("status")
               in ("active", "waiting") and sweeps < 12):
            sweeps += 1
            ctx2["runner"].advance_due()
        goal_final = ctx2["goals"].get(goal_id) or {}
        return {
            "budget_persisted": stamped == 3,
            "restarted_goal_kept_budget":
                goal2.get("max_decisions_total") == 3,
            "restart_cannot_enlarge": goal_final.get("max_decisions_total") == 3,
            "total_calls_bounded_by_lifetime":
                spent_before + len(ctx2["model"].calls) == 3,
            "goal_needs_human_after_restart":
                goal_final.get("status") == "needs_human",
        }
    finally:
        close(ctx2)


def a5_resume_does_not_mint_budget() -> dict:
    """resume() never resets or enlarges the budget; only an explicit human
    ``set_lifetime_budget`` can, and that is a different, audited action."""
    ledger = fresh_ledger("a5")
    ctx = build(ledger, ScriptedModel("inspect_forever"), lifetime_decisions=2)
    goal_text = "A5 预算语义：人工恢复不隐式增加预算"
    goal_id, _ = submit_goal(ctx, goal_text)
    while ((ctx["goals"].get(goal_id) or {}).get("status")
           in ("active", "waiting")):
        ctx["runner"].advance_due()
    goal = ctx["goals"].get(goal_id) or {}
    resume_allowed = True
    try:
        ctx["goals"].resume(goal_id, note="owner says continue")
    except Exception:  # noqa: BLE001
        resume_allowed = False
    goal_after_resume = ctx["goals"].get(goal_id) or {}
    ctx["runner"].advance_due()  # budget already spent: re-parks immediately
    calls_after_resume = len(ctx["model"].calls)
    goal_after_retry = ctx["goals"].get(goal_id) or {}
    # The explicit human-only raise:
    ctx["goals"].set_lifetime_budget(goal_id, max_decisions=10)
    ctx["runner"].advance_due()
    goal_after_raise = ctx["goals"].get(goal_id) or {}
    close(ctx)
    return {
        "parked_on_lifetime": goal.get("status") == "needs_human",
        "resume_worked": resume_allowed
                         and goal_after_resume.get("status") == "active",
        "resume_kept_budget": goal_after_resume.get("max_decisions_total") == 2,
        "retry_spends_no_new_budget": calls_after_resume == 2,
        "re_parked_on_lifetime": goal_after_retry.get("status") == "needs_human",
        "budget_unchanged_after_retry":
            goal_after_retry.get("max_decisions_total") == 2,
        "explicit_raise_enlarges": goal_after_raise.get("max_decisions_total") == 10,
    }


def a6_legacy_ledger_migration() -> dict:
    """A pre-budget-schema ledger migrates in place and budgets still bind."""
    ledger = fresh_ledger("a6")
    legacy = sqlite3.connect(ledger)
    legacy.executescript("""
    CREATE TABLE ld_goals (
        goal_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        goal_text TEXT NOT NULL,
        goal_ref TEXT NOT NULL,
        status TEXT NOT NULL,
        current_task_id TEXT,
        decision_count INTEGER NOT NULL DEFAULT 0,
        task_count INTEGER NOT NULL DEFAULT 0,
        last_decision TEXT,
        handoff_task_ids TEXT NOT NULL DEFAULT '[]',
        claim_owner TEXT,
        claim_until REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE ld_goal_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id TEXT NOT NULL,
        step INTEGER NOT NULL,
        decision TEXT NOT NULL,
        outcome TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(goal_id, step)
    );
    """)
    legacy.commit()
    legacy.close()

    ctx = build(ledger, ScriptedModel("inspect_forever"), lifetime_decisions=3)
    try:
        goal_id, goal_ref = submit_goal(ctx, "A6 预算语义：旧库迁移后生效")
        goal = ctx["goals"].get(goal_id) or {}
        migrated = (goal.get("max_decisions_total") is None
                    and goal.get("resume_count") == 0)
        while ((ctx["goals"].get(goal_id) or {}).get("status")
               in ("active", "waiting")):
            ctx["runner"].advance_due()
        goal_final = ctx["goals"].get(goal_id) or {}
        return {
            "legacy_columns_nullable_before_advance": migrated,
            "budget_stamped_after_first_advance":
                goal_final.get("max_decisions_total") == 3,
            "legacy_history_kept": goal_final.get("decision_count", 0) >= 3,
            "lifetime_binds_migrated_goal":
                goal_final.get("status") == "needs_human"
                and len(ctx["model"].calls) == 3,
        }
    finally:
        close(ctx)


def main() -> int:
    make_repo()
    sections = {
        "A1_quantum_yield": a1_quantum_yield(),
        "A2_lifetime_decision_budget": a2_lifetime_decision_budget(),
        "A3_lifetime_task_budget": a3_lifetime_task_budget(),
        "A4_budget_survives_restart": a4_budget_survives_restart(),
        "A5_resume_does_not_mint_budget": a5_resume_does_not_mint_budget(),
        "A6_legacy_ledger_migration": a6_legacy_ledger_migration(),
    }
    checks: dict[str, bool] = {}
    for name, section in sections.items():
        for key, value in section.items():
            if isinstance(value, bool):
                checks[f"{name}.{key}"] = value
    log = {"test": "T45-goal-budget-semantics",
           "worker_started": False,
           "note": ("scripted decision model + stub adapter: verifies the "
                    "budget contract (per-call quantum vs persisted lifetime "
                    "budget), not Worker execution."),
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
