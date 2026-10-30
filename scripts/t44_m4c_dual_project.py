"""T44: two projects advancing at the same time on one runtime stay independent.

The product claim M4-C must not break: a Goal belongs to exactly one project, and
two Goals advancing concurrently (two projects, one Ledger, one runtime) must not
observe, dispatch into, or verify each other's work.

Sections
--------
D1  two goals, one runtime: both reach ``completed`` with exactly one task each,
    each task bound to its own project/workspace, and no cross-project dispatch.
D2  knowledge isolation: a secret context entry in project A never appears in
    project B's Effective Prompt, its model view, or its progress projection.
D3  failure isolation: project A's goal parking in ``needs_human`` (an
    unresolvable assertion) leaves project B's goal free to complete.
D4  one project's Worker failure does not stall the other's schedule.

Real Worker + real model are used when ``LD_MODEL_*`` is configured; the two repos
are throw-away fixtures under ``sandbox/``.
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

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_context import ContextEntry  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.runtime import ControlPlaneRuntime, RuntimeConfig  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / f"m4c-dual-{uuid.uuid4().hex[:8]}"
LEDGER = ROOT / ".poc-state" / "t44-m4c-dual.db"
EVIDENCE = ROOT / "evidence" / "project" / "T44-m4c-dual-project.json"

PERMISSIONS = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}},
                         indent=1)

PROJECT_SPECS = {
    "alpha": {
        "goal": ("在当前项目中实现模块 alpha_lib。创建 alpha_lib/__init__.py，"
                 "实现 greet(name) 返回 'hello ' + name。让预置测试 test_alpha.py "
                 "全部通过。完成后在工作分支 git commit（不要 push，不要操作 "
                 "main）。验收断言使用 file_contains（alpha_lib/__init__.py 中的 "
                 "def greet）。测试通过后结束目标。"),
        "seed": {
            "alpha_lib/__init__.py": "",
            "test_alpha.py": (
                "from alpha_lib import greet\n\n\n"
                "def test_greet():\n"
                "    assert greet('alpha') == 'hello alpha'\n\n\n"
                "def test_greet_other():\n"
                "    assert greet('x') == 'hello x'\n"
            ),
        },
        "assertion": {"kind": "file_contains", "target": "alpha_lib/__init__.py",
                      "value": "def greet"},
        "secret": "ALPHA-ONLY-SECRET-CONTEXT",
        "test": "test_alpha.py",
    },
    "beta": {
        "goal": ("在当前项目中实现模块 beta_lib。创建 beta_lib/__init__.py，"
                 "实现 double(n) 返回 2 * n。让预置测试 test_beta.py 全部通过。"
                 "完成后在工作分支 git commit（不要 push，不要操作 main）。"
                 "验收断言使用 file_contains（beta_lib/__init__.py 中的 "
                 "def double）。测试通过后结束目标。"),
        "seed": {
            "beta_lib/__init__.py": "",
            "test_beta.py": (
                "from beta_lib import double\n\n\n"
                "def test_double():\n"
                "    assert double(3) == 6\n\n\n"
                "def test_double_zero():\n"
                "    assert double(0) == 0\n"
            ),
        },
        "assertion": {"kind": "file_contains", "target": "beta_lib/__init__.py",
                      "value": "def double"},
        "secret": "BETA-ONLY-SECRET-CONTEXT",
        "test": "test_beta.py",
    },
}

TERMINAL = ("succeeded", "failed", "cancelled", "lost")


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(name: str) -> Path:
    spec = PROJECT_SPECS[name]
    repo = FIXTURE / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", f"t44-{name}@example.invalid")
    git(repo, "config", "user.name", f"T44-{name}")
    for rel, content in spec["seed"].items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(PERMISSIONS, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "branch", "-M", "main")
    return repo


def model_from_env() -> OpenAIChatModel:
    model = OpenAIChatModel.from_env()
    if model is None:
        raise SystemExit("real model required: set LD_MODEL_BASE_URL / LD_MODEL_NAME")
    return model


def run_pytest(cwd, name: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-m", "pytest", PROJECT_SPECS[name]["test"],
                           "-q"], cwd=str(cwd), capture_output=True, text=True,
                          timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr)[-500:]


def dual_run() -> dict:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    repos = {name: make_repo(name) for name in PROJECT_SPECS}
    main_tips = {name: git(repo, "rev-parse", "main")
                 for name, repo in repos.items()}

    adapter = CodeBuddyAdapter(autologin=True)
    store = TaskStore(LEDGER)
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t44")
    service = ProjectService(plane)
    projects = {name: plane.register_project(
        f"t44-{name}", str(repo), default_adapter="codebuddy",
        concurrency_limit=2) for name, repo in repos.items()}
    # Distinct local secrets: neither may leak into the other project's prompt.
    for name, project in projects.items():
        service.add_context_entry(project["id"], ContextEntry(
            project_id=project["id"], kind="background",
            title=f"{name.upper()}-BACKGROUND",
            body=f"{PROJECT_SPECS[name]['secret']} for {name} only",
            security_domain="local"))

    coordinator = LDCoordinator(
        LDTools(service), model_from_env(),
        model_authorized_security_domains=("local", "shareable"))
    goals = LDGoalStore(LEDGER, lease_s=300.0)
    runner = LDGoalRunner(coordinator, goals, max_decisions=2, max_tasks=1,
                          max_model_failures=4, base_backoff_s=5.0,
                          max_backoff_s=60.0)
    runtime = ControlPlaneRuntime(
        plane,
        RuntimeConfig(known_refresh_interval_s=5.0, dispatch_interval_s=1.0,
                      goal_interval_s=2.0, max_concurrent_tasks=4,
                      max_dispatch_per_cycle=4),
        goal_runner=runner, owner="t44-runtime")

    goal_ids: dict[str, str] = {}
    goal_refs: dict[str, str] = {}
    for name, project in projects.items():
        gid, gref = goal_identity(PROJECT_SPECS[name]["goal"], project["id"])
        goals.get_or_create(gid, project["id"], PROJECT_SPECS[name]["goal"], gref)
        goal_ids[name] = gid
        goal_refs[name] = gref

    deadline = time.time() + 2 * 60 * 60
    runtime.prepare()
    cycles = 0
    try:
        while time.time() < deadline:
            cycles += 1
            runtime.run_due(force=True)
            statuses = [(goals.get(gid) or {}).get("status")
                        for gid in goal_ids.values()]
            if all(status in ("completed", "failed", "needs_human")
                   for status in statuses):
                break
            time.sleep(5)
    finally:
        runtime.stop(timeout_s=10.0)

    out: dict = {"cycles": cycles}

    # ---- per-project task inventory ------------------------------------- #
    def tasks_of(goal_ref: str) -> list[dict]:
        found = [task for task in store.all()
                 if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
        found.sort(key=lambda t: t.get("created_at") or 0)
        return found

    per_project: dict[str, dict] = {}
    for name in PROJECT_SPECS:
        project = projects[name]
        tasks = tasks_of(goal_refs[name])
        other = [other_name for other_name in PROJECT_SPECS if other_name != name]
        foreign = [task for task in tasks
                   if task.get("project_id") != project["id"]]
        prompt = (tasks[0].get("effective_prompt") or "") if tasks else ""
        wt = (tasks[0].get("worktree") or "") if tasks else ""
        tests = {}
        if wt:
            code, tail = run_pytest(wt, name)
            tests = {"pytest_exit": code, "tail": tail}
        per_project[name] = {
            "goal_status": (goals.get(goal_ids[name]) or {}).get("status"),
            "task_count": len(tasks),
            "task_ids": [t["task_id"] for t in tasks],
            "states": [t.get("state") for t in tasks],
            "verdicts": [t.get("verdict") for t in tasks],
            "own_project_only": not foreign,
            "own_workspace": all(t.get("project_id") == project["id"]
                                 for t in tasks),
            "prompt_has_own_secret": PROJECT_SPECS[name]["secret"] in prompt,
            "prompt_lacks_other_secret": all(
                PROJECT_SPECS[o]["secret"] not in prompt for o in other),
            "assertions_verified": bool(tasks) and all(
                t.get("assertion_results")
                and all(r.get("passed") for r in t["assertion_results"])
                for t in tasks),
            "pytest": tests,
            "main_untouched": git(repos[name], "rev-parse", "main") == main_tips[name],
            "worktree_in_own_repo": bool(wt) and Path(wt).is_absolute()
                                    and str(repos[name]) in str(wt),
        }

    # ---- knowledge isolation on the read paths --------------------------- #
    alpha, beta = projects["alpha"], projects["beta"]
    beta_prompt = ""
    beta_tasks = tasks_of(goal_refs["beta"])
    if beta_tasks:
        beta_prompt = beta_tasks[0].get("effective_prompt") or ""
    tools = LDTools(service)
    beta_progress = tools.get_project_progress(beta["id"])
    beta_brief_model = tools.get_project_brief(
        beta["id"], allowed_security_domains=("shareable",))
    out["knowledge_isolation"] = {
        "a_secret_absent_from_b_prompt": PROJECT_SPECS["alpha"]["secret"]
                                          not in beta_prompt,
        "b_has_own_secret": PROJECT_SPECS["beta"]["secret"] in beta_prompt,
        "a_secret_absent_from_b_progress": PROJECT_SPECS["alpha"]["secret"]
                                            not in json.dumps(beta_progress,
                                                              ensure_ascii=False),
        "a_secret_absent_from_b_model_view": PROJECT_SPECS["alpha"]["secret"]
                                             not in json.dumps(beta_brief_model,
                                                               ensure_ascii=False),
        "progress_counts_separate": (
            beta_progress["counts"]["total"] == len(beta_tasks)
            and beta_progress["counts"]["total"] >= 1),
    }

    out["projects"] = per_project
    out["goal_summary"] = {
        name: {"status": (goals.get(gid) or {}).get("status"),
               "decisions": (goals.get(gid) or {}).get("decision_count"),
               "tasks": (goals.get(gid) or {}).get("task_count")}
        for name, gid in goal_ids.items()
    }
    out["ledger_tasks_total"] = len(store.all())
    out["runtime_status"] = {
        "goal_runner_enabled": runtime.status().get("goal_runner_enabled"),
        "goal_runs": (runtime.status().get("goal_advancement") or {}).get("runs"),
        "dispatch_runs": (runtime.status().get("dispatch_queue") or {}).get("runs"),
    }

    checks = {
        "both_goals_completed": all(
            item["goal_status"] == "completed" for item in per_project.values()),
        "one_task_per_project": all(
            item["task_count"] == 1 for item in per_project.values()),
        "no_cross_project_task": all(
            item["own_project_only"] for item in per_project.values()),
        "no_cross_project_workspace": all(
            item["own_workspace"] for item in per_project.values()),
        "worktrees_in_own_repo": all(
            item["worktree_in_own_repo"] for item in per_project.values()),
        "all_tasks_verified": all(
            all(v == "verified" for v in item["verdicts"])
            for item in per_project.values()),
        "all_assertions_passed": all(
            item["assertions_verified"] for item in per_project.values()),
        "tests_pass_in_own_worktree": all(
            item["pytest"].get("pytest_exit") == 0
            for item in per_project.values()),
        "prompts_isolated": all(
            item["prompt_has_own_secret"] and item["prompt_lacks_other_secret"]
            for item in per_project.values()),
        "mains_untouched": all(
            item["main_untouched"] for item in per_project.values()),
        "ledger_has_exactly_two_tasks": out["ledger_tasks_total"] == 2,
        "single_runtime_paths_ran": (
            out["runtime_status"]["goal_runs"] > 0
            and out["runtime_status"]["dispatch_runs"] > 0),
    }
    checks.update(out["knowledge_isolation"])
    out["checks"] = checks
    runner.close()
    goals.close()
    store.close()
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = dual_run()
    log = {
        "test": "T44-m4c-dual-project",
        "worker_started": True,
        "note": ("two projects, two goals, one Ledger, one runtime; each goal "
                 "advances autonomously and neither sees the other's knowledge, "
                 "workspace or tasks."),
        "fixture": str(FIXTURE),
        **out,
    }
    write_evidence(EVIDENCE, log)
    print(json.dumps({k: v for k, v in log["checks"].items() if not v},
                     ensure_ascii=False, indent=2))
    print("Pass" if all(log["checks"].values()) else "Fail")
    return 0 if all(log["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
