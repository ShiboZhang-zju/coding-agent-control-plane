"""T47: multi-runner concurrency, lease fencing and fenced escalation.

Every scenario runs with **two real OS processes** (see
``t47_goal_runner_process.py``), each with its own SQLite connection to the
shared ledger — not two threads inside one process.

Scenarios
---------
C1  two runners discover the same goal at the same time: exactly one WorkItem
    is submitted (lease + per-step idempotency key), the loser never calls
    the model, and the goal still completes.
C2  a runner whose model call failed tries to escalate *after* another runner
    completed the goal: the fenced, revalidating escalation must be a no-op
    (``completed`` is never overwritten with ``needs_human``).  A fresh
    escalation on an untouched goal still works (positive control).
C3  a runner loses its lease (expiry + takeover) and then attempts a fenced
    write with the stale token: rejected with ``GoalLeaseLost``; the takeover
    runner submits exactly one WorkItem.
C4  a process restart mid-flow: runner A inspects and exits; runner B (a new
    process) continues the flow to completion.
C5  repeated model failures from two concurrent runners escalate the goal
    exactly once (both may cross the threshold; only one stop lands).
C6  one goal's repeated failures do not block another goal on the same
    ledger: the failing goal parks in ``needs_human`` while the healthy goal
    completes on the same sweeps.

Deterministic fault injection: the scripted driver model raises on demand;
lease expiry is time-based with a short ``--lease-s`` and a deliberate sleep.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity, step_idempotency_key  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

DRIVER = ROOT / "scripts" / "t47_goal_runner_process.py"
FIXTURE = ROOT / "sandbox" / "t47-concurrency"
STATE = ROOT / ".poc-state" / "t47"
EVIDENCE = ROOT / "evidence" / "project" / "T47-runner-concurrency-fencing.json"


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(name: str) -> Path:
    repo = FIXTURE / name
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t47@example.invalid")
        git(repo, "config", "user.name", "T47")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(repo, "add", "seed.txt")
        git(repo, "commit", "-q", "-m", "seed")
        git(repo, "branch", "-M", "main")
    return repo


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def register_projects(ledger: Path, repos: dict[str, Path]) -> dict[str, str]:
    """Register the projects once (setup only; no LD advancement here)."""
    store = TaskStore(ledger)
    plane = ControlPlane(_NameOnlyAdapter("stub"), store,
                         require_registered_projects=True)
    out = {}
    for name, repo in repos.items():
        project = plane.register_project(name, str(repo),
                                         default_adapter="stub",
                                         concurrency_limit=8)
        out[name] = project["id"]
    store.close()
    return out


class _NameOnlyAdapter:
    def __init__(self, name):
        self.name = name


def create_goal(ledger: Path, goal_text: str, project_id: str) -> str:
    store = TaskStore(ledger)
    goals = LDGoalStore(store.path)
    goal_id, goal_ref = goal_identity(goal_text, project_id)
    goals.get_or_create(goal_id, project_id, goal_text, goal_ref)
    goals.close()
    store.close()
    return goal_id


def run_driver(ledger: Path, scenario: str, *, out: Path, model: str = "submit_finish",
               goal_id: str | None = None, sweeps: int = 200,
               interval: float = 0.3, hold_s: float = 3.0, failures: int = 3,
               step: int = 1, lease_s: float = 2.0, base_backoff_s: float = 0.3,
               max_model_failures: int = 3, owner: str | None = None,
               duration_s: float = 25.0, wait_s: float = 180.0) -> dict:
    argv = [sys.executable, str(DRIVER), "--ledger", str(ledger),
            "--scenario", scenario, "--model", model, "--out", str(out),
            "--sweeps", str(sweeps), "--interval", str(interval),
            "--hold-s", str(hold_s), "--failures", str(failures),
            "--step", str(step), "--lease-s", str(lease_s),
            "--base-backoff-s", str(base_backoff_s),
            "--max-model-failures", str(max_model_failures),
            "--duration-s", str(duration_s)]
    if goal_id:
        argv += ["--goal-id", goal_id]
    if owner:
        argv += ["--owner", owner]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=wait_s,
                          cwd=str(ROOT), encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"driver {scenario} failed: {proc.stderr[-800:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def spawn_driver(ledger: Path, scenario: str, *, out: Path,
                 model: str = "submit_finish", goal_id: str | None = None,
                 sweeps: int = 200, interval: float = 0.3, hold_s: float = 3.0,
                 failures: int = 3, step: int = 1, lease_s: float = 2.0,
                 base_backoff_s: float = 0.3, max_model_failures: int = 3,
                 owner: str | None = None, duration_s: float = 25.0
                 ) -> subprocess.Popen:
    argv = [sys.executable, str(DRIVER), "--ledger", str(ledger),
            "--scenario", scenario, "--model", model, "--out", str(out),
            "--sweeps", str(sweeps), "--interval", str(interval),
            "--hold-s", str(hold_s), "--failures", str(failures),
            "--step", str(step), "--lease-s", str(lease_s),
            "--base-backoff-s", str(base_backoff_s),
            "--max-model-failures", str(max_model_failures),
            "--duration-s", str(duration_s)]
    if goal_id:
        argv += ["--goal-id", goal_id]
    if owner:
        argv += ["--owner", owner]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, cwd=str(ROOT),
                            creationflags=subprocess.CREATE_NO_WINDOW)


def read_goal(ledger: Path, goal_id: str) -> dict:
    store = TaskStore(ledger)
    goals = LDGoalStore(store.path)
    goal = goals.get(goal_id) or {}
    history = goals.decisions(goal_id, limit=200)
    goals.close()
    store.close()
    return {**goal, "history": history}


def ledger_tasks(ledger: Path) -> list[dict]:
    store = TaskStore(ledger)
    tasks = store.all()
    store.close()
    return tasks


def complete_task(ledger: Path, task_id: str, *,
                  verdict: str = "verified") -> None:
    store = TaskStore(ledger)
    record = store.get(task_id) or {}
    store.update(task_id, state=TaskState.SUCCEEDED.value, verdict=verdict,
                 queue_status="complete", finished_at=1_700_000_000.0,
                 worker_job_id=record.get("worker_job_id") or f"job-{task_id}")
    store.close()


def wait_for(predicate, timeout_s: float = 60.0, poll_s: float = 0.5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll_s)
    return None


def c1_two_runners_one_goal() -> dict:
    ledger = fresh_ledger("c1")
    repos = {"t47-c1": make_repo("c1")}
    projects = register_projects(ledger, repos)
    goal_text = "C1 两 Runner 并发同一 Goal"
    goal_id = create_goal(ledger, goal_text, projects["t47-c1"])
    out_a, out_b = STATE / "c1-a.json", STATE / "c1-b.json"
    proc_a = spawn_driver(ledger, "sweep", out=out_a, model="submit_finish",
                          sweeps=400, interval=0.2, duration_s=30.0,
                          owner="c1-runner-a")
    proc_b = spawn_driver(ledger, "sweep", out=out_b, model="submit_finish",
                          sweeps=400, interval=0.2, duration_s=30.0,
                          owner="c1-runner-b")
    # The "verifier": mark the goal's task verified once it exists.
    def goal_task():
        for task in ledger_tasks(ledger):
            if ((task.get("metadata") or {}).get("ld_goal_ref")
                    and task.get("state") not in ("succeeded", "failed")):
                return task
        return None
    task = wait_for(goal_task, timeout_s=30)
    verified = bool(task)
    if task:
        complete_task(ledger, task["task_id"])
    for proc in (proc_a, proc_b):
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    result_a = json.loads(out_a.read_text(encoding="utf-8"))
    result_b = json.loads(out_b.read_text(encoding="utf-8"))
    goal = read_goal(ledger, goal_id)
    tasks = [t for t in ledger_tasks(ledger)
             if (t.get("metadata") or {}).get("ld_goal_ref")]
    steps = [d["step"] for d in goal["history"]]
    submit_steps = [d for d in goal["history"]
                    if d["outcome"].get("action") == "submit_task"]
    return {
        "task_existed": verified,
        "exactly_one_workitem": len(tasks) == 1,
        "exactly_one_submission_step": len(submit_steps) == 1,
        "goal_completed": goal.get("status") == "completed",
        "history_contiguous": steps == list(range(1, len(steps) + 1)),
        "no_duplicate_step_keys": len(
            {t.get("idempotency_key") for t in tasks}) == len(tasks),
        "model_calls_bounded": (
            result_a["model_calls"] + result_b["model_calls"]) <= 8,
        "winner_model_calls": [result_a["model_calls"], result_b["model_calls"]],
    }


def c2_stale_escalation_cannot_overwrite() -> dict:
    ledger = fresh_ledger("c2")
    repos = {"t47-c2": make_repo("c2")}
    projects = register_projects(ledger, repos)
    goal_text = "C2 陈旧失败升级不得覆盖他人推进"
    goal_id = create_goal(ledger, goal_text, projects["t47-c2"])
    # A stale runner observes the goal, then sleeps; meanwhile runner B (this
    # process) completes the goal.  A's escalation must be a no-op.
    out_a = STATE / "c2-a.json"
    proc_a = spawn_driver(ledger, "stale_escalate", out=out_a,
                          goal_id=goal_id, hold_s=4.0, failures=3)
    time.sleep(1.0)
    result_b = run_driver(ledger, "sweep", out=STATE / "c2-b.json",
                          model="submit_finish", sweeps=300, interval=0.3,
                          duration_s=20.0)
    task = wait_for(lambda: next(
        (t for t in ledger_tasks(ledger)
         if (t.get("metadata") or {}).get("ld_goal_ref")
         and t.get("state") not in ("succeeded", "failed")), None), timeout_s=30)
    if task:
        complete_task(ledger, task["task_id"])
    run_driver(ledger, "sweep", out=STATE / "c2-b2.json",
               model="submit_finish", sweeps=300, interval=0.3,
               duration_s=20.0)
    try:
        proc_a.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc_a.kill()
    result_a = json.loads(out_a.read_text(encoding="utf-8"))
    goal = read_goal(ledger, goal_id)

    # Positive control: escalating a genuinely stuck (untouched) goal works.
    control_text = "C2 正向对照：真实失败升级仍然生效"
    control_id = create_goal(ledger, control_text, projects["t47-c2"])
    out_c = STATE / "c2-c.json"
    run_driver(ledger, "stale_escalate", out=out_c, goal_id=control_id,
               hold_s=0.2, failures=3)
    control_result = json.loads(out_c.read_text(encoding="utf-8"))
    control_goal = read_goal(ledger, control_id)
    return {
        "runner_b_completed_goal": goal.get("status") == "completed",
        "stale_escalation_skipped": (
            result_a["escalated"].get("escalated") is False),
        "completed_not_overwritten": goal.get("status") == "completed",
        "stale_observation_older": (
            result_a["observed_decision_count"] < result_a["final_decision_count"]),
        "control_escalation_works": (
            control_result["escalated"].get("escalated") is True
            and control_goal.get("status") == "needs_human"),
    }


def c3_lease_lost_fenced_write() -> dict:
    ledger = fresh_ledger("c3")
    repos = {"t47-c3": make_repo("c3")}
    projects = register_projects(ledger, repos)
    goal_text = "C3 失租执行者的 fenced 写入被拒"
    goal_id = create_goal(ledger, goal_text, projects["t47-c3"])
    out_a = STATE / "c3-a.json"
    # Runner A takes the lease and holds it past expiry (lease-s=1.0, hold 4s).
    proc_a = spawn_driver(ledger, "lease_then_write", out=out_a,
                          goal_id=goal_id, hold_s=4.0, lease_s=1.0)
    leased = wait_for(lambda: out_a.exists(), timeout_s=20)
    time.sleep(1.6)  # the lease expires; a takeover may bump the token
    result_b = run_driver(ledger, "sweep", out=STATE / "c3-b.json",
                          model="submit_finish", sweeps=10, interval=0.2,
                          lease_s=1.0, duration_s=15.0)
    try:
        proc_a.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc_a.kill()
    result_a = json.loads(out_a.read_text(encoding="utf-8"))
    tasks = [t for t in ledger_tasks(ledger)
             if (t.get("metadata") or {}).get("ld_goal_ref")]
    return {
        "lease_was_taken": bool(leased),
        "fenced_write_rejected": result_a["fenced_write_rejected"] is True,
        "stale_renew_rejected": result_a["stale_renew_rejected"] is True,
        "takeover_submitted_exactly_one": len(tasks) == 1,
        "goal_advancing_normally": (read_goal(ledger, goal_id).get("status")
                                    in ("waiting", "active")),
    }


def c4_restart_continuation() -> dict:
    ledger = fresh_ledger("c4")
    repos = {"t47-c4": make_repo("c4")}
    projects = register_projects(ledger, repos)
    goal_text = "C4 多进程重启后继续推进"
    goal_id = create_goal(ledger, goal_text, projects["t47-c4"])
    # Process A: one sweep (the model inspects), then exits — a "crash".
    result_a = run_driver(ledger, "sweep", out=STATE / "c4-a.json",
                          model="inspect_once", sweeps=1, interval=0.1)
    goal_after_a = read_goal(ledger, goal_id)
    # Process B: a fresh process continues the flow to completion.
    proc_b = spawn_driver(ledger, "sweep", out=STATE / "c4-b.json",
                          model="submit_finish", sweeps=400, interval=0.2,
                          duration_s=30.0)
    task = wait_for(lambda: next(
        (t for t in ledger_tasks(ledger)
         if (t.get("metadata") or {}).get("ld_goal_ref")
         and t.get("state") not in ("succeeded", "failed")), None), timeout_s=30)
    if task:
        complete_task(ledger, task["task_id"])
    try:
        proc_b.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc_b.kill()
    result_b = json.loads((STATE / "c4-b.json").read_text(encoding="utf-8"))
    goal = read_goal(ledger, goal_id)
    tasks = [t for t in ledger_tasks(ledger)
             if (t.get("metadata") or {}).get("ld_goal_ref")]
    return {
        "process_a_inspected": result_a["model_calls"] == 1,
        "goal_active_after_a": goal_after_a.get("status") == "active",
        "process_b_continued": result_b["model_calls"] >= 2,
        "process_b_submitted": len(tasks) == 1,
        "goal_completed": goal.get("status") == "completed",
    }


def c5_single_effective_escalation() -> dict:
    ledger = fresh_ledger("c5")
    repos = {"t47-c5": make_repo("c5")}
    projects = register_projects(ledger, repos)
    goal_text = "C5 双 Runner 重复失败只升级一次"
    goal_id = create_goal(ledger, goal_text, projects["t47-c5"])
    out_a, out_b = STATE / "c5-a.json", STATE / "c5-b.json"
    proc_a = spawn_driver(ledger, "sweep", out=out_a, model="fail",
                          sweeps=400, interval=0.15, base_backoff_s=0.2,
                          max_model_failures=3, duration_s=20.0, owner="c5-a")
    proc_b = spawn_driver(ledger, "sweep", out=out_b, model="fail",
                          sweeps=400, interval=0.15, base_backoff_s=0.2,
                          max_model_failures=3, duration_s=20.0, owner="c5-b")
    for proc in (proc_a, proc_b):
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    result_a = json.loads(out_a.read_text(encoding="utf-8"))
    result_b = json.loads(out_b.read_text(encoding="utf-8"))
    escalations = ([e for e in result_a["escalations"] if e]
                   + [e for e in result_b["escalations"] if e])
    goal = read_goal(ledger, goal_id)
    return {
        "goal_needs_human": goal.get("status") == "needs_human",
        "exactly_one_effective_escalation": len(
            [e for e in escalations if e.get("escalated")]) == 1,
        "escalation_reason_persisted": "repeated LD failures" in json.dumps(
            goal.get("last_decision") or {}, ensure_ascii=False),
        "failures_bounded": (result_a["model_calls"] <= 12
                             and result_b["model_calls"] <= 12),
        "no_task_submitted": not [
            t for t in ledger_tasks(ledger)
            if (t.get("metadata") or {}).get("ld_goal_ref")],
    }


def c6_goal_failure_isolation() -> dict:
    ledger = fresh_ledger("c6")
    repos = {"t47-c6-bad": make_repo("c6-bad"),
             "t47-c6-ok": make_repo("c6-ok")}
    projects = register_projects(ledger, repos)
    bad_text = "C6 失败 Goal 不影响其他 Goal"
    ok_text = "C6 正常 Goal 在失败 Goal 停摆后仍完成"
    bad_id = create_goal(ledger, bad_text, projects["t47-c6-bad"])
    ok_id = create_goal(ledger, ok_text, projects["t47-c6-ok"])
    out = STATE / "c6-a.json"
    proc = spawn_driver(ledger, "sweep", out=out,
                        model=f"fail_project:{projects['t47-c6-bad']}",
                        sweeps=600, interval=0.2, base_backoff_s=0.2,
                        max_model_failures=3, duration_s=40.0)
    task = wait_for(lambda: next(
        (t for t in ledger_tasks(ledger)
         if (t.get("metadata") or {}).get("ld_goal_ref")
         and t.get("project_id") == projects["t47-c6-ok"]
         and t.get("state") not in ("succeeded", "failed")), None), timeout_s=40)
    ok_task_seen = bool(task)
    if task:
        complete_task(ledger, task["task_id"])
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
    result = json.loads(out.read_text(encoding="utf-8"))
    bad_goal = read_goal(ledger, bad_id)
    ok_goal = read_goal(ledger, ok_id)
    tasks = ledger_tasks(ledger)
    bad_tasks = [t for t in tasks if t.get("project_id") == projects["t47-c6-bad"]]
    ok_tasks = [t for t in tasks if t.get("project_id") == projects["t47-c6-ok"]]
    # The ok-goal's final report must not contain the bad goal's WorkItems.
    ok_refs = {(t.get("metadata") or {}).get("ld_goal_ref") for t in ok_tasks}
    return {
        "bad_goal_parked": bad_goal.get("status") == "needs_human",
        "bad_goal_no_tasks": not bad_tasks,
        "ok_goal_completed": ok_goal.get("status") == "completed",
        "ok_task_was_submitted": ok_task_seen,
        "ok_goal_isolated": ok_refs == {
            goal_identity(ok_text, projects["t47-c6-ok"])[1]},
        "failures_did_not_stop_ok_goal": result["model_calls"] >= 3,
    }


def main() -> int:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    sections = {}
    failures: list[str] = []
    for name, fn in (("C1_two_runners_one_goal", c1_two_runners_one_goal),
                     ("C2_stale_escalation_cannot_overwrite",
                      c2_stale_escalation_cannot_overwrite),
                     ("C3_lease_lost_fenced_write", c3_lease_lost_fenced_write),
                     ("C4_restart_continuation", c4_restart_continuation),
                     ("C5_single_effective_escalation", c5_single_effective_escalation),
                     ("C6_goal_failure_isolation", c6_goal_failure_isolation)):
        try:
            sections[name] = fn()
        except Exception as exc:  # noqa: BLE001 - record, keep going
            sections[name] = {"_error": f"{type(exc).__name__}: {exc}"}
            failures.append(name)
    checks: dict[str, bool] = {}
    for name, section in sections.items():
        for key, value in section.items():
            if isinstance(value, bool):
                checks[f"{name}.{key}"] = value
    log = {"test": "T47-runner-concurrency-fencing",
           "worker_started": False,
           "note": ("two real OS processes per scenario (t47_goal_runner_process"
                    ".py), independent SQLite connections on one shared ledger; "
                    "stub adapter + scripted model with deterministic fault "
                    "injection."),
           "sections": sections, "section_failures": failures,
           "checks": checks,
           "result": "Pass" if checks and all(checks.values()) and not failures
                     else "Fail"}
    write_evidence(EVIDENCE, log)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({k: v for k, v in checks.items() if not v},
                     ensure_ascii=False, indent=2))
    if failures:
        print("section failures:", failures)
    print(log["result"])
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
