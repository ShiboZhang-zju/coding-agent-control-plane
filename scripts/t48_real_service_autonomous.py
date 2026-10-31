"""T48: real resident service — one goal, two dependent tasks, zero manual LD.

The M4-C closure acceptance: combine what T38b proved (real A -> B code
handoff) with what T43 proved (runtime-driven LD wakeup) **in the normal
service deployment**, and add a mid-flight service restart.

Deployment under test (exactly what an operator runs)::

    CodeBuddy HTTP service          (scripts/serve_ctl.py start)
    scripts/run_controlplane.py --advance-goals   (a real service process)
    one shared SQLite ledger
    a real, explicitly authorized LLM (LD_MODEL_*)

The test script only ever: creates the throw-away project, submits the goal
**once**, starts/stops the service process, and *reads* the ledger.  It never
calls ``run_due``/``advance_due``/``run_goal``/``run_once``, never writes a
verdict, never touches goal state and never simulates Worker success.

Scenarios
--------
R1  zero-touch: goal -> auto wake -> LD submits A -> real CodeBuddy session ->
    real verifier verifies A -> auto wake -> LD submits B as a session handoff
    of A's checkpoint -> new independent session -> real verifier verifies B
    -> auto wake -> LD finishes -> goal ``completed``.
R2  restart recovery: same flow, but the service process is killed right
    after A is verified; a freshly started service discovers the waiting goal,
    submits B and completes it — still without any manual LD invocation.

If the real model chooses ``request_human``, the actual outcome is recorded
and the strict zero-touch acceptance is judged **Fail** (never masked by an
automatic ``--resume``).

Requires: ``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME`` (and ``LD_MODEL_API_KEY``
when applicable).  Without them the run is recorded as environment-blocked.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.goal_store import goal_identity  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
FIXTURE = ROOT / "sandbox" / f"t48-real-service-{uuid.uuid4().hex[:8]}"
STATE = ROOT / ".poc-state" / "t48"
EVIDENCE = ROOT / "evidence" / "project" / "T48-real-service-autonomous.json"
RUNTIME_LOG = STATE / "runtime-stdout.log"

GOAL_TEXT = (
    "在当前项目中实现一个配置模块。第一阶段实现 configlib 包中的 "
    "load_config(config_dict) 函数，使预置测试 test_load.py 全部通过；"
    "第二阶段实现同包的 validate_config(config_dict) 函数，使预置测试 "
    "test_validate.py 全部通过且 test_load.py 不回归。两个阶段必须作为"
    "两个独立任务分别提交。每个阶段完成代码修改后必须用 git commit 提交"
    "全部修改：如当前在 main/master 检出，请先创建并切换到新的特性分支"
    "再提交（允许自建分支，不要 push，绝不要直接提交到 main/master 分支，"
    "也不要修改 main/master）。每个任务的验收断言使用 file_exists 或 "
    "file_contains（针对 configlib/__init__.py 中的函数定义），不要使用 "
    "git_clean。所有测试通过后结束目标。"
)

SEED_FILES = {
    "configlib/__init__.py": "",
    "test_load.py": (
        "from configlib import load_config\n\n\n"
        "def test_load_returns_dict():\n"
        "    cfg = load_config({\"host\": \"localhost\", \"port\": 8080})\n"
        "    assert cfg == {\"host\": \"localhost\", \"port\": 8080}\n\n\n"
        "def test_load_empty():\n"
        "    assert load_config({}) == {}\n"
    ),
    "test_validate.py": (
        "from configlib import load_config, validate_config\n\n\n"
        "def test_valid_config():\n"
        "    errors = validate_config(load_config({\"host\": \"h\", \"port\": 1}))\n"
        "    assert errors == []\n\n\n"
        "def test_port_must_be_int():\n"
        "    errors = validate_config({\"host\": \"h\", \"port\": \"x\"})\n"
        "    assert \"port\" in errors\n\n\n"
        "def test_host_required():\n"
        "    errors = validate_config({\"port\": 1})\n"
        "    assert \"host\" in errors\n"
    ),
}


def git(path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t48@example.invalid")
    git(path, "config", "user.name", "T48")
    for name, content in SEED_FILES.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Project-scoped Worker permissions (inherited by every worktree): the
    # agent must run pytest / git merge / git commit inside its own branch.
    perm = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}}, indent=1)
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(perm, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")
    return path


def ensure_service() -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "start", "--port", str(PORT)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    return info


def stop_service() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "stop", "--port", str(PORT)],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def setup_project(ledger: Path, repo: Path, name: str) -> str:
    """Registration only — no LD advancement, no dispatch."""
    from controlplane.project_service import ProjectService  # noqa: PLC0415
    adapter = CodeBuddyAdapter(Api(base=BASE), autologin=True)
    store = TaskStore(ledger)
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t48")
    service = ProjectService(plane)
    project = plane.register_project(name, str(repo), default_adapter="codebuddy")
    store.close()
    return project["id"]


def submit_goal_once(ledger: Path, goal_text: str, project_id: str) -> tuple[str, str]:
    from ld.goal_store import LDGoalStore  # noqa: PLC0415
    store = TaskStore(ledger)
    goals = LDGoalStore(store.path)
    goal_id, goal_ref = goal_identity(goal_text, project_id)
    goals.get_or_create(goal_id, project_id, goal_text, goal_ref)
    goals.close()
    store.close()
    return goal_id, goal_ref


def start_runtime(ledger: Path, log_path: Path, *, tag: str) -> subprocess.Popen:
    """Start the normal resident service (run_forever, own goal thread)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [
        sys.executable, str(ROOT / "scripts" / "run_controlplane.py"),
        "--ledger", str(ledger), "--base", BASE,
        "--advance-goals", "--model-domains", "local,shareable",
        "--goal-interval", "3",
        "--goal-max-decisions", "1", "--goal-max-tasks", "1",
        "--goal-max-model-failures", "4",
        "--goal-base-backoff", "5", "--goal-max-backoff", "60",
        "--goal-lifetime-decisions", "24", "--goal-lifetime-tasks", "4",
        "--known-interval", "5", "--dispatch-interval", "1",
        "--max-concurrent", "2",
    ]
    out = open(log_path, "ab")
    proc = subprocess.Popen(
        argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=str(ROOT), env=env,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return proc


def wait_port_gone(pid: int, timeout_s: float = 20.0) -> bool:
    def alive():
        try:
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                  capture_output=True, text=True, timeout=15)
            return str(pid) not in proc.stdout
        except Exception:  # noqa: BLE001
            return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if alive():
            return True
        time.sleep(0.5)
    return False


def read_only(ledger: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def goal_row(ledger: Path, goal_id: str) -> dict:
    rows = read_only(ledger, "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,))
    if not rows:
        return {}
    row = rows[0]
    row["handoff_task_ids"] = json.loads(row.get("handoff_task_ids") or "[]")
    if row.get("last_decision"):
        try:
            row["last_decision"] = json.loads(row["last_decision"])
        except json.JSONDecodeError:
            row["last_decision"] = None
    return row


def goal_history(ledger: Path, goal_id: str) -> list[dict]:
    rows = read_only(
        ledger,
        "SELECT step, state, decision, outcome, created_at FROM ld_goal_decisions "
        "WHERE goal_id=? ORDER BY step", (goal_id,))
    for row in rows:
        row["decision"] = json.loads(row["decision"] or "{}")
        row["outcome"] = json.loads(row["outcome"] or "{}")
    return rows


def runner_rows(ledger: Path) -> dict:
    try:
        rows = read_only(ledger, "SELECT * FROM ld_goal_runner")
    except sqlite3.OperationalError:
        return {}
    return {row["goal_id"]: row for row in rows}


def ledger_tasks(ledger: Path, goal_ref: str) -> list[dict]:
    store = TaskStore(ledger)
    tasks = [t for t in store.all()
             if (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    tasks.sort(key=lambda t: t.get("created_at") or 0)
    store.close()
    return tasks


def observe_until(ledger: Path, goal_id: str, *, terminal: tuple = (),
                  predicate=None, timeout_s: float, poll_s: float = 10.0,
                  on_poll=None) -> list[dict]:
    timeline: list[dict] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        goal = goal_row(ledger, goal_id)
        timeline.append({
            "t": round(time.time(), 1),
            "status": goal.get("status"),
            "decision_count": goal.get("decision_count"),
            "task_count": goal.get("task_count"),
            "current_task_id": goal.get("current_task_id"),
        })
        if on_poll:
            on_poll()
        if terminal and goal.get("status") in terminal:
            break
        if predicate and predicate():
            break
        time.sleep(poll_s)
    return timeline


def run_pytest(cwd, args: list) -> dict:
    proc = subprocess.run([sys.executable, "-m", "pytest", *args, "-q"],
                          cwd=str(cwd), capture_output=True, text=True,
                          timeout=600, encoding="utf-8", errors="replace")
    return {"exit": proc.returncode, "tail": (proc.stdout or "")[-600:]}


def model_from_env():
    base = os.environ.get("LD_MODEL_BASE_URL", "").strip()
    name = os.environ.get("LD_MODEL_NAME", "").strip()
    if not base or not name:
        return None
    return {"base_url": base, "model": name}


def scenario(ledger: Path, repo: Path, project_name: str, *,
             restart_after_a_verified: bool, budget_s: float) -> dict:
    project_id = setup_project(ledger, repo, project_name)
    goal_id, goal_ref = submit_goal_once(ledger, GOAL_TEXT, project_id)

    log_path = STATE / f"{project_name}-runtime.log"
    for stale in ([log_path] if log_path.exists() else []):
        stale.unlink()
    runtime = start_runtime(ledger, log_path, tag=project_name)
    runtime_pids = [runtime.pid]
    restarts = 0
    timeline: list[dict] = []
    try:
        deadline = time.time() + budget_s
        phase = "running"
        while time.time() < deadline:
            time.sleep(10)
            goal = goal_row(ledger, goal_id)
            tasks = ledger_tasks(ledger, goal_ref)
            timeline.append({
                "t": round(time.time(), 1),
                "phase": phase,
                "goal_status": goal.get("status"),
                "tasks": [{"id": t["task_id"], "state": t.get("state"),
                           "verdict": t.get("verdict"),
                           "session": bool(t.get("session_id"))}
                          for t in tasks],
            })
            if goal.get("status") in ("completed", "needs_human", "failed"):
                break
            if restart_after_a_verified and phase == "running":
                task_a_verified = any(
                    t.get("verdict") == "verified" for t in tasks)
                if task_a_verified and len(tasks) >= 1:
                    # D4: kill the service right after A is verified.
                    subprocess.run(["taskkill", "/PID", str(runtime.pid), "/F"],
                                   capture_output=True, text=True, timeout=30)
                    wait_port_gone(runtime.pid)
                    phase = "restarted"
                    time.sleep(2)
                    runtime = start_runtime(ledger, log_path, tag=project_name)
                    runtime_pids.append(runtime.pid)
                    restarts += 1
                    continue
    finally:
        subprocess.run(["taskkill", "/PID", str(runtime.pid), "/F"],
                       capture_output=True, text=True, timeout=30)
        wait_port_gone(runtime.pid)

    # ---- read-only outcome collection ---------------------------------- #
    goal = goal_row(ledger, goal_id)
    history = goal_history(ledger, goal_id)
    tasks = ledger_tasks(ledger, goal_ref)
    bookkeeping = runner_rows(ledger).get(goal_id, {})
    task_a = tasks[0] if len(tasks) > 0 else {}
    task_b = tasks[1] if len(tasks) > 1 else {}
    meta_b = (task_b.get("metadata") or {})
    meta_a = (task_a.get("metadata") or {})

    ancestor = None
    if task_b.get("worktree") and task_a.get("head_commit"):
        merged = subprocess.run(
            ["git", "-C", task_b["worktree"], "merge-base", "--is-ancestor",
             task_a["head_commit"], "HEAD"],
            capture_output=True, text=True, timeout=60)
        ancestor = merged.returncode == 0
    tests_b = run_pytest(task_b["worktree"], []) if task_b.get("worktree") else {}
    finish_steps = [d for d in history
                    if (d["decision"] or {}).get("action") == "finish"]
    finish_decision = (finish_steps[-1]["decision"] if finish_steps else {})
    submit_steps = [d for d in history
                    if (d["decision"] or {}).get("action") == "submit_task"]

    return {
        "project_id": project_id,
        "goal_id": goal_id,
        "goal_ref": goal_ref,
        "runtime_pids": runtime_pids,
        "service_restarts": restarts,
        "goal_final": {
            "status": goal.get("status"),
            "decision_count": goal.get("decision_count"),
            "model_decision_count": goal.get("model_decision_count"),
            "resume_count": goal.get("resume_count"),
            "task_count": goal.get("task_count"),
            "lifetime_decisions": goal.get("max_decisions_total"),
            "last_decision": goal.get("last_decision"),
        },
        "scheduler": {
            "runs": bookkeeping.get("runs"),
            "advances": bookkeeping.get("advances"),
            "consecutive_failures": bookkeeping.get("consecutive_failures"),
            "last_error": bookkeeping.get("last_error"),
        },
        "tasks": [{
            "task_id": t["task_id"], "state": t.get("state"),
            "verdict": t.get("verdict"),
            "worker_job_id": t.get("worker_job_id"),
            "session_id": t.get("session_id"),
            "worktree": t.get("worktree"),
            "head_commit": t.get("head_commit"),
            "idempotency_key": t.get("idempotency_key"),
            "assertion_results": t.get("assertion_results"),
            "attention_flags": (t.get("attention_flags")
                                if "attention_flags" in t else None),
            "metadata": {k: v for k, v in (t.get("metadata") or {}).items()
                         if k in ("submitted_by", "handoff",
                                  "source_work_item_id", "checkpoint_ref",
                                  "ld_goal_step")},
        } for t in tasks],
        "task_a_verified": task_a.get("verdict") == "verified",
        "task_b": {
            "submitted": bool(task_b.get("task_id")),
            "is_handoff": bool(meta_b.get("handoff")),
            "handoff_source_is_a": meta_b.get("source_work_item_id")
                                   == task_a.get("task_id"),
            "distinct_session": bool(
                task_b.get("session_id")
                and task_a.get("session_id")
                and task_b["session_id"] != task_a["session_id"]),
            "verdict": task_b.get("verdict"),
        },
        "a_commit_is_ancestor_of_b_head": ancestor,
        "all_tests_pass_in_b_worktree": tests_b.get("exit") == 0,
        "pytest_b": tests_b,
        "finish": {
            "auto_finish_decision": bool(finish_steps),
            "finish_task_ids": sorted(
                finish_decision.get("parameters", {}).get(
                    "verified_task_ids") or []),
            "goal_task_ids": sorted(t["task_id"] for t in tasks),
        },
        "submission_steps": len(submit_steps),
        "main_untouched": None,  # filled by caller (needs repo tip)
        "timeline_tail": timeline[-8:],
        "runtime_log_tail": _log_tail(log_path),
    }


def _log_tail(log_path: Path, limit: int = 2500) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    model = model_from_env()
    if model is None:
        write_evidence(EVIDENCE, {
            "test": "T48-real-service-autonomous",
            "result": "EnvironmentBlocked",
            "reason": ("LD_MODEL_BASE_URL / LD_MODEL_NAME are not configured; "
                       "the real-model resident-service acceptance was not "
                       "executed. This is NOT a Pass."),
        })
        print("EnvironmentBlocked: LD_MODEL_* not configured")
        return 2

    serve = ensure_service()
    out: dict = {"test": "T48-real-service-autonomous",
                 "worker_started": True,
                 "model": {"endpoint_configured": True},
                 "serve": serve}
    try:
        # R1 — zero-touch run.
        repo1 = make_repo(FIXTURE / "r1")
        main_tip_1 = git(repo1, "rev-parse", "main")
        ledger1 = fresh_ledger("r1")
        r1 = scenario(ledger1, repo1, "t48-r1",
                      restart_after_a_verified=False, budget_s=2.5 * 60 * 60)
        r1["main_untouched"] = git(repo1, "rev-parse", "main") == main_tip_1
        out["R1_zero_touch"] = r1

        # R2 — kill the service after A is verified, restart, complete.
        repo2 = make_repo(FIXTURE / "r2")
        main_tip_2 = git(repo2, "rev-parse", "main")
        ledger2 = fresh_ledger("r2")
        r2 = scenario(ledger2, repo2, "t48-r2",
                      restart_after_a_verified=True, budget_s=2.5 * 60 * 60)
        r2["main_untouched"] = git(repo2, "rev-parse", "main") == main_tip_2
        out["R2_restart_recovery"] = r2
    finally:
        stop_service()

    def r1_checks(r: dict, ledger: Path) -> dict:
        tasks = r["tasks"]
        pending = [d for d in goal_history(ledger, r["goal_id"])
                   if d["state"] == "pending"]
        return {
            "goal_submitted_once": r["goal_final"]["task_count"] is not None,
            "runtime_auto_woke": (r["scheduler"]["advances"] or 0) >= 2,
            "ld_dispatched_a": bool(tasks) and tasks[0]["metadata"].get(
                "submitted_by") == "ld",
            "a_real_session": bool(tasks) and bool(tasks[0]["session_id"]),
            "a_verified_by_real_verifier": r["task_a_verified"],
            "auto_woke_again": (r["scheduler"]["advances"] or 0) >= 3,
            "b_new_independent_session": r["task_b"]["distinct_session"],
            "b_handoff_of_a": (r["task_b"]["is_handoff"]
                               and r["task_b"]["handoff_source_is_a"]),
            "a_commit_ancestor_of_b": r["a_commit_is_ancestor_of_b_head"],
            "b_preset_tests_pass": r["all_tests_pass_in_b_worktree"],
            "both_verified": (r["task_a_verified"]
                              and r["task_b"]["verdict"] == "verified"),
            "ld_auto_finish": r["finish"]["auto_finish_decision"],
            "goal_completed": r["goal_final"]["status"] == "completed",
            "report_contains_exactly_a_b": (
                r["finish"]["finish_task_ids"] == r["finish"]["goal_task_ids"]
                and len(r["finish"]["goal_task_ids"]) == 2),
            "zero_human_resumes": r["goal_final"]["resume_count"] == 0,
            "no_duplicate_workitems": len(tasks) == 2,
            "no_pending_steps": not pending,
            "main_untouched": r["main_untouched"],
            "no_attention_flags": not any(
                t.get("attention_flags") for t in tasks),
            "single_goal_row": True,
        }

    def r2_checks(r: dict, ledger: Path) -> dict:
        base = r1_checks(r, ledger)
        base.update({
            "service_restarted_once": r["service_restarts"] == 1,
            "restarted_service_discovered_goal": (
                r["goal_final"]["status"] == "completed"),
        })
        return base

    checks_r1 = r1_checks(r1, ledger1)
    checks_r2 = r2_checks(r2, ledger2)
    out["checks"] = {**{f"R1.{k}": v for k, v in checks_r1.items()},
                     **{f"R2.{k}": v for k, v in checks_r2.items()}}
    out["result"] = ("Pass" if all(out["checks"].values()) else "Fail")
    write_evidence(EVIDENCE, out)
    print(json.dumps({k: v for k, v in out["checks"].items() if not v},
                     ensure_ascii=False, indent=2))
    print(out["result"])
    return 0 if out["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
