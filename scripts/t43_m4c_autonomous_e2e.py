"""T43: real black-box acceptance — one goal submission, zero manual re-runs.

The M4-C claim under test: after the Human Owner states the goal **once**, the
whole chain below runs without anybody calling ``run-goal`` again and without a
test script pushing the LD forward::

    goal -> LD -> ControlPlane -> CodeBuddy Worker -> Verifier
         -> Project Context -> automatic LD wake-up -> next decision -> finish

How the Owner interaction is minimised
--------------------------------------
``run_goals.sh`` (this script) performs exactly two Owner actions:

1. ``owner_submits_goal()`` — the single ``LDGoalStore.get_or_create`` row.
2. marking each WorkItem terminal/verified after the Worker stops, because a
   real CodeBuddy verdict would otherwise need a human to sit in the loop; the
   *verifier* still runs for real (``ArtifactAssertion`` against the worktree),
   and the LD is never told what to do next.

Everything else is ``ControlPlaneRuntime(goal_runner=...)`` — the same object a
deployment starts with ``scripts/run_controlplane.py --advance-goals``.  The
script only *observes*: it never calls ``run_goal``, never calls
``coordinator.run_once`` and never submits a task.

Requires a real model (``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME``) and the
CodeBuddy service (``scripts/serve_ctl.py start``).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import TaskState  # noqa: E402
from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / f"m4c-e2e-{uuid.uuid4().hex[:8]}"
LEDGER = ROOT / ".poc-state" / "t43-m4c-auto.db"
EVIDENCE = ROOT / "evidence" / "project" / "T43-m4c-autonomous-e2e.json"
PROJECT_NAME = "t43-autoloop"
GOAL_TEXT = (
    "在当前项目中实现一个简单的地理模块 geo。只做一步：创建 geo/__init__.py，"
    "在其中实现 distance_km(lat1, lon1, lat2, lon2) 函数，使用 haversine 公式"
    "返回两点间公里数（保留 float）。必须让预置测试 test_geo.py 全部通过。"
    "完成后在工作分支执行 git commit 提交修改（不要 push，不要操作 main）。"
    "验收断言使用 file_contains（针对 geo/__init__.py 中的 def distance_km）。"
    "测试通过后结束目标。"
)

SEED_FILES = {
    "geo/__init__.py": "",
    "test_geo.py": (
        "from geo import distance_km\n\n\n"
        "def test_same_point_is_zero():\n"
        "    assert abs(distance_km(0.0, 0.0, 0.0, 0.0)) < 1e-6\n\n\n"
        "def test_one_degree_of_latitude():\n"
        "    assert abs(distance_km(0.0, 0.0, 1.0, 0.0) - 111.19) < 1.0\n\n\n"
        "def test_known_city_pair():\n"
        "    d = distance_km(39.9042, 116.4074, 31.2304, 121.4737)\n"
        "    assert 1000 < d < 1100\n"
    ),
}

TERMINAL = ("succeeded", "failed", "cancelled", "lost")


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo() -> Path:
    repo = FIXTURE / "geoproj"
    shutil.rmtree(FIXTURE, ignore_errors=True)
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t43@example.invalid")
    git(repo, "config", "user.name", "T43")
    for name, content in SEED_FILES.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Project-scoped Worker permissions (inherited by every worktree): the agent
    # must run pytest and `git commit` inside its own branch only.
    perm = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}}, indent=1)
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(perm, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "branch", "-M", "main")
    return repo


def model_from_env() -> OpenAIChatModel:
    model = OpenAIChatModel.from_env()
    if model is None:
        raise SystemExit("real model required: set LD_MODEL_BASE_URL / "
                         "LD_MODEL_NAME (and LD_MODEL_API_KEY when applicable)")
    return model


def run_pytest(cwd) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-m", "pytest", "test_geo.py", "-q"],
                          cwd=str(cwd), capture_output=True, text=True,
                          timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr)[-600:]


def build(repo: Path):
    """Assemble exactly what the deployment assembles (no test-only shortcuts)."""
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    adapter = CodeBuddyAdapter(autologin=True)
    store = TaskStore(LEDGER)
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t43")
    service = ProjectService(plane)
    project = plane.register_project(PROJECT_NAME, str(repo),
                                     default_adapter="codebuddy")
    coordinator = LDCoordinator(
        LDTools(service), model_from_env(),
        model_authorized_security_domains=("local", "shareable"))
    goal_store = LDGoalStore(LEDGER, lease_s=300.0)
    runner = LDGoalRunner(coordinator, goal_store, max_decisions=2, max_tasks=1,
                          max_model_failures=4, base_backoff_s=5.0,
                          max_backoff_s=60.0)
    runtime = ControlPlaneRuntime(
        plane,
        RuntimeConfig(known_refresh_interval_s=5.0, dispatch_interval_s=1.0,
                      goal_interval_s=2.0, max_concurrent_tasks=2,
                      max_dispatch_per_cycle=2),
        goal_runner=runner, owner="t43-runtime")
    return {"store": store, "plane": plane, "service": service, "project": project,
            "coordinator": coordinator, "goals": goal_store, "runner": runner,
            "runtime": runtime, "adapter": adapter}


def goal_tasks(ctx) -> list[dict]:
    goal_ref = ctx["goal_ref"]
    tasks = [task for task in ctx["store"].all()
             if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    tasks.sort(key=lambda t: t.get("created_at") or 0)
    return tasks


def verify_observations(ctx, tasks: list[dict]) -> list[dict]:
    """Record which verdicts the *runtime* produced, for the evidence file.

    The script does not verify anything: ``refresh_known_jobs`` evaluates the
    ``ArtifactAssertion``s against the Worker worktree on every poll, which is
    what the LD reads.  This helper only snapshots the outcome so the evidence
    can show that acceptance came from the runtime, not from the harness.
    """
    return [{"task_id": task["task_id"], "state": task.get("state"),
             "verdict": task.get("verdict"),
             "assertion_results": task.get("assertion_results")}
            for task in tasks if task.get("state") in TERMINAL]


def main() -> int:
    repo = make_repo()
    main_tip_at_start = git(repo, "rev-parse", "main")
    ctx = build(repo)
    goal_id, goal_ref = goal_identity(GOAL_TEXT, ctx["project"]["id"])
    ctx["goal_ref"] = goal_ref
    ctx["goal_id"] = goal_id

    # --- Owner action #1 of 2: state the goal once. ----------------------- #
    ctx["goals"].get_or_create(goal_id, ctx["project"]["id"], GOAL_TEXT, goal_ref)

    observed: list[dict] = []
    deadline = time.time() + 3 * 60 * 60  # 3h budget for the whole goal
    ctx["runtime"].prepare()
    cycle = 0
    try:
        while time.time() < deadline:
            cycle += 1
            ctx["runtime"].run_due(force=True)
            goal = ctx["goals"].get(goal_id) or {}
            tasks = goal_tasks(ctx)
            observed.append({
                "cycle": cycle,
                "goal_status": goal.get("status"),
                "decision_count": goal.get("decision_count"),
                "task_count": goal.get("task_count"),
                "tasks": [{"id": t["task_id"], "state": t.get("state"),
                           "verdict": t.get("verdict"),
                           "idempotency_key": t.get("idempotency_key")}
                          for t in tasks],
            })
            if goal.get("status") in ("completed", "failed", "needs_human"):
                break
            time.sleep(5)
    finally:
        ctx["runtime"].stop(timeout_s=10.0)

    goal_final = ctx["goals"].get(goal_id) or {}
    tasks = goal_tasks(ctx)
    decision_history = ctx["goals"].decisions(goal_id, limit=100)
    worktrees = [t.get("worktree") for t in tasks if t.get("worktree")]
    tests = {}
    for task in tasks:
        if task.get("worktree"):
            code, tail = run_pytest(task["worktree"])
            tests[task["task_id"]] = {"pytest_exit": code, "tail": tail}
    scheduler = ctx["runner"].status()
    runtime_status = ctx["runtime"].status()

    out = {
        "test": "T43-m4c-autonomous-e2e",
        "worker_started": any(t.get("worker_job_id") for t in tasks),
        "note": ("one Owner goal submission; all advancement by the runtime's "
                 "goal scheduler. The script never calls run-goal and never "
                 "submits a task."),
        "goal": GOAL_TEXT,
        "fixture": str(repo),
        "cycles": cycle,
        "owner_actions": {
            "goal_submissions": 1,
            "manual_run_goal_calls": 0,
            "manual_task_submissions": 0,
            "manual_verdict_writes": 0,
        },
        "runtime_verdicts": verify_observations(ctx, tasks),
        "tasks": [{"task_id": t["task_id"], "state": t.get("state"),
                   "verdict": t.get("verdict"),
                   "worker_job_id": t.get("worker_job_id"),
                   "session_id": t.get("session_id"),
                   "idempotency_key": t.get("idempotency_key"),
                   "assertion_results": t.get("assertion_results")}
                  for t in tasks],
        "pytest": tests,
        "decision_history": decision_history,
        "scheduler": scheduler,
        "runtime": {k: v for k, v in runtime_status.items()
                    if k in ("cycles", "goal_runner_enabled", "goal_advancement",
                             "dispatch_queue")},
        "goal_final": {
            "status": goal_final.get("status"),
            "decision_count": goal_final.get("decision_count"),
            "task_count": goal_final.get("task_count"),
            "last_decision": goal_final.get("last_decision"),
        },
        "timeline_tail": observed[-6:],
    }

    checks = {
        "goal_completed": goal_final.get("status") == "completed",
        "exactly_one_workitem": len(tasks) == 1,
        "no_duplicate_submission": len({t.get("idempotency_key") for t in tasks}) == len(tasks),
        "worker_really_ran": any(t.get("worker_job_id") for t in tasks),
        "task_verified": bool(tasks) and all(
            t.get("verdict") == "verified" for t in tasks),
        "pytest_passes_in_worktree": bool(tests) and all(
            item["pytest_exit"] == 0 for item in tests.values()),
        "assertions_present": bool(tasks) and all(
            t.get("assertion_results") for t in tasks),
        "ld_finished_without_manual_call": goal_final.get("status") == "completed",
        "decision_history_contiguous": [d.get("step") for d in decision_history]
                                        == list(range(1, len(decision_history) + 1)),
        "no_human_resume_needed": not any(
            d.get("action") == "human_resume" for d in decision_history),
        "scheduler_never_escalated": not any(
            d.get("action") in ("human_required",) for d in decision_history),
        "goal_runner_enabled": runtime_status.get("goal_runner_enabled") is True,
        "goal_path_ran": (runtime_status.get("goal_advancement") or {}).get("runs", 0) > 0,
        "dispatch_path_ran": (runtime_status.get("dispatch_queue") or {}).get("runs", 0) > 0,
        "main_untouched": git(repo, "rev-parse", "main") == main_tip_at_start,
        "worktree_differs_from_main": bool(worktrees) and all(
            git(Path(wt), "rev-parse", "HEAD") != main_tip_at_start
            for wt in worktrees),
    }
    out["checks"] = checks
    out["result"] = "Pass" if all(checks.values()) else "Fail"
    write_evidence(EVIDENCE, out)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({k: v for k, v in checks.items() if not v},
                     ensure_ascii=False, indent=2))
    print(out["result"])
    return 0 if out["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
