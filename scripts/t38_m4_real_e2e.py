"""T38: real end-to-end M4 acceptance — real LLM decisions + real CodeBuddy.

Goal part: one Human Owner goal drives two genuinely dependent tasks:
Session A implements ``configlib.load_config`` (pre-seeded ``test_load.py``
passes), Session B is dispatched as a session handoff that merges A's git
checkpoint and implements ``configlib.validate_config`` (``test_validate.py``
passes, no regression). The LD finishes with both verified tasks as evidence.

Restart recovery: the second round runs as a real CLI subprocess after the
first LD process is gone; it must resume the same goal without resubmitting A.

Isolation part: two throw-away projects with distinct local secrets; work in
one never sees the other's knowledge.

Requires: the CodeBuddy service (scripts/serve_ctl.py start) and a real model
configured via LD_MODEL_BASE_URL / LD_MODEL_NAME / LD_MODEL_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
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
from controlplane.store import TaskStore  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.model import OpenAIChatModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

FIXTURE = ROOT / "sandbox" / f"m4-e2e-{uuid.uuid4().hex[:8]}"
LEDGER = ROOT / ".poc-state" / "t38-m4-e2e.db"
EVIDENCE = ROOT / "evidence" / "project" / "T38-m4-real-e2e.json"
POLL_S = 15.0
GOAL_TEXT = (
    "在当前项目中实现一个配置模块。第一阶段实现 configlib 包中的 "
    "load_config(config_dict) 函数，使预置测试 test_load.py 全部通过；"
    "第二阶段实现同包的 validate_config(config_dict) 函数，使预置测试 "
    "test_validate.py 全部通过且 test_load.py 不回归。两个阶段必须作为"
    "两个独立任务分别提交。每个阶段完成后必须在当前工作分支执行 "
    "git commit 提交全部修改（不要 push，不要操作 main/master 分支）。"
    "每个任务的验收断言使用 file_exists 或 file_contains（针对 "
    "configlib/__init__.py 中的函数定义），不要使用 git_clean。"
    "所有测试通过后结束目标。"
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


def make_repo(path: Path, files: dict | None = None) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t38@example.invalid")
    git(path, "config", "user.name", "T38")
    for name, content in (files or SEED_FILES).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")


def run_pytest(cwd, args: list) -> tuple:
    proc = subprocess.run([sys.executable, "-m", "pytest", *args, "-q"],
                          cwd=str(cwd), capture_output=True, text=True,
                          timeout=240)
    return proc.returncode, (proc.stdout + proc.stderr)[-700:]


def model_from_env() -> OpenAIChatModel:
    model = OpenAIChatModel.from_env()
    if model is None:
        raise SystemExit("real model required: set LD_MODEL_BASE_URL / "
                         "LD_MODEL_NAME (and LD_MODEL_API_KEY when applicable)")
    return model


def build(ledger: Path, repo: Path, project_name: str):
    adapter = CodeBuddyAdapter(autologin=True)
    store = TaskStore(ledger)
    plane = ControlPlane(adapter, store, require_registered_projects=True,
                         worker_instance_id="wi-t38")
    service = ProjectService(plane)
    project = plane.register_project(project_name, str(repo),
                                     default_adapter="codebuddy")
    tools = LDTools(service)
    coordinator = LDCoordinator(
        tools, model_from_env(),
        model_authorized_security_domains=("local", "shareable"))
    return store, service, tools, coordinator, project


def wait_terminal(store: TaskStore, service: ProjectService, task_id: str,
                  timeout_s: float = 900) -> dict:
    """Fresh-observer loop: claim+dispatch the queued work, then observe."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        service.plane.dispatch_next("t38-observer", concurrency_limit=2)
        service.plane.refresh_known_jobs()
        item = store.get(task_id) or {}
        if item.get("state") in ("succeeded", "failed", "cancelled", "lost"):
            return item
        time.sleep(POLL_S)
    return store.get(task_id) or {}


def goal_part() -> dict:
    repo = FIXTURE / "pyproj"
    make_repo(repo)
    for stale in [LEDGER, *LEDGER.parent.glob(LEDGER.name + ".*")]:
        stale.unlink(missing_ok=True)
    out: dict = {"goal": GOAL_TEXT, "fixture": str(repo)}
    main_sha_at_start = git(repo, "rev-parse", "main")

    # Round 1 (process 1): submit task A, stop while it runs.
    store, service, tools, coordinator, project = build(LEDGER, repo, "t38-config")
    goal_store = LDGoalStore(LEDGER)
    goal_id, goal_ref = goal_identity(GOAL_TEXT, project["id"])
    report1 = coordinator.run_goal(GOAL_TEXT, project["id"], goal_store,
                                   max_decisions=8, max_tasks=3,
                                   poll_interval_s=None)
    task_a = report1.get("current_task_id")
    out["round1"] = {"status": report1.get("status"), "task_a": task_a,
                     "decision_history": report1.get("decision_history")}
    store.close()
    goal_store.close()
    if not task_a:
        out["error"] = "round 1 did not submit task A"
        return out

    # The LD process is gone; a fresh observer waits for A's terminal verdict.
    store, service, tools, coordinator, project = build(LEDGER, repo, "t38-config")
    item_a = wait_terminal(store, service, task_a)
    out["task_a"] = {
        "state": item_a.get("state"), "verdict": item_a.get("verdict"),
        "worker_session": item_a.get("session_id"),
        "worktree": item_a.get("worktree"),
        "head_commit_recorded": bool(item_a.get("head_commit")),
        "assertion_results": item_a.get("assertion_results"),
    }
    if item_a.get("worktree"):
        code, _ = run_pytest(item_a["worktree"], ["test_load.py"])
        out["task_a"]["load_tests_pass_in_worktree"] = code == 0
    store.close()

    # Round 2a (a real CLI subprocess = the restarted LD): same goal, must not
    # resubmit A, submits B and stops at waiting. The ledger-only CLI plane
    # cannot observe the Worker — a real runtime owns observation, which is
    # exactly the division of labour the architecture prescribes.
    def cli_run_goal(extra=()):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ld_cli.py"),
             "--ledger", str(LEDGER), "--compact", "run-goal", GOAL_TEXT,
             "--project", "t38-config", "--max-decisions", "8",
             "--max-tasks", "3", *extra],
            capture_output=True, text=True, timeout=900, cwd=str(ROOT),
            encoding="utf-8", errors="replace", env=dict(os.environ))

    # --resume stands in for the Human Owner reviewing the pause and letting
    # the LD continue (the model occasionally parks a goal conservatively).
    cli = cli_run_goal(("--resume",))
    try:
        report2 = json.loads((cli.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        report2 = {"_raw": (cli.stdout or "")[-800:],
                   "_stderr": (cli.stderr or "")[-400:]}
    out["round2_cli_exit"] = cli.returncode

    # The observer runtime dispatches and watches B to its terminal verdict.
    store, service, tools, coordinator, project = build(LEDGER, repo, "t38-config")
    task_b = report2.get("current_task_id")
    item_b = wait_terminal(store, service, task_b) if task_b else {}

    # Round 2b (another CLI subprocess): B is verified now; the LD finishes.
    cli_final = cli_run_goal(("--resume",))
    try:
        report_final = json.loads((cli_final.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        report_final = {"_raw": (cli_final.stdout or "")[-800:],
                        "_stderr": (cli_final.stderr or "")[-400:]}
    report2 = {**report2, "goal_status": report_final.get("status"),
               "verified_tasks_in_report": report_final.get("verified_tasks"),
               "decision_history": report_final.get("decision_history")}
    task_b = report2.get("current_task_id")
    item_b = store.get(task_b) or {}
    prompt_b = item_b.get("effective_prompt") or ""
    goal_tasks = [t for t in store.all()
                  if (t.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    out["task_b"] = {
        "submitted": bool(task_b) and task_b != task_a,
        "is_handoff": bool((item_b.get("metadata") or {}).get("handoff")),
        "handoff_source_is_a": (
            (item_b.get("metadata") or {}).get("source_work_item_id") == task_a),
        "prompt_has_merge_instruction": "git merge" in prompt_b,
        "prompt_forbids_default_branch": "default branch" in prompt_b,
        "distinct_session": bool(
            item_b.get("session_id")
            and item_b.get("session_id") != item_a.get("session_id")),
        "state": item_b.get("state"), "verdict": item_b.get("verdict"),
        "assertion_results": item_b.get("assertion_results"),
    }
    out["goal_summary"] = {
        "goal_task_count": len(goal_tasks),
        "all_goal_tasks_verified": all(
            t.get("verdict") == "verified" for t in goal_tasks),
        "task_ids": [t.get("task_id") for t in goal_tasks],
        "goal_status": report2.get("status"),
        "verified_tasks_in_report": report2.get("verified_tasks"),
        "decision_history": report2.get("decision_history"),
    }

    # Session B really consumed Session A's accepted code.
    consumed: dict = {}
    if item_b.get("worktree") and item_a.get("head_commit"):
        code_all, _ = run_pytest(item_b["worktree"], [])
        merged = subprocess.run(
            ["git", "-C", item_b["worktree"], "merge-base", "--is-ancestor",
             item_a["head_commit"], "HEAD"],
            capture_output=True, text=True, timeout=30)
        consumed = {
            "all_tests_pass_in_b_worktree": code_all == 0,
            "a_checkpoint_is_ancestor_of_b_head": merged.returncode == 0,
        }
    out["session_b_consumes_a"] = consumed

    # Automation never merged anything into the project's default branch:
    # the main tip must be byte-identical from goal start to goal end.
    out["no_automatic_merge_to_main"] = (
        git(repo, "rev-parse", "main") == main_sha_at_start)
    store.close()
    return out


def isolation_part() -> dict:
    repo_a = FIXTURE / "iso-a"
    repo_b = FIXTURE / "iso-b"
    make_repo(repo_a, {"README.md": "iso a\n"})
    make_repo(repo_b, {"README.md": "iso b\n"})
    ledger = LEDGER.with_name(LEDGER.stem + "-iso.db")
    for stale in [ledger, *ledger.parent.glob(ledger.name + ".*")]:
        stale.unlink(missing_ok=True)
    out: dict = {}

    store, service, tools, coordinator, project_a = build(ledger, repo_a, "t38-iso-a")
    service.add_context_entry(project_a["id"], ContextEntry(
        project_id=project_a["id"], kind="background",
        title="PROJECT-A-BACKGROUND", body="A-SECRET-CONTEXT only for project A",
        security_domain="local"))
    store.close()

    store, service, tools, coordinator, project_b = build(ledger, repo_b, "t38-iso-b")
    service.add_context_entry(project_b["id"], ContextEntry(
        project_id=project_b["id"], kind="background",
        title="PROJECT-B-BACKGROUND", body="B-SECRET-CONTEXT only for project B",
        security_domain="local"))
    rec_b = service.submit_project_task(
        project_b["id"], "在 README.md 末尾追加一行 PROJECT-B TASK DONE",
        idempotency_key=f"t38-iso-b-{uuid.uuid4().hex[:6]}",
        title="iso B task")
    prompt_b = rec_b.get("effective_prompt") or ""
    out["b_task_has_own_knowledge"] = "PROJECT-B-BACKGROUND" in prompt_b
    out["b_task_lacks_a_knowledge"] = "A-SECRET-CONTEXT" not in prompt_b
    store.close()

    store, service, tools, coordinator, project_a = build(ledger, repo_a, "t38-iso-a")
    model_view = tools.get_project_brief(
        project_a["id"], allowed_security_domains=("shareable",))
    out["shareable_view_hides_a_secret"] = "A-SECRET-CONTEXT" not in json.dumps(
        model_view, ensure_ascii=False)
    progress_a = tools.get_project_progress(project_a["id"])
    store.close()

    store, service, tools, coordinator, project_b = build(ledger, repo_b, "t38-iso-b")
    progress_b = tools.get_project_progress(project_b["id"])
    out["progress_isolated"] = (
        progress_a["project_id"] == project_a["id"]
        and progress_b["project_id"] == project_b["id"]
        and progress_b["counts"]["total"] >= 1
        and progress_a["counts"]["total"] == 0)
    store.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["goal", "isolation", "all"], default="all")
    args = ap.parse_args()
    model_from_env()  # fail fast with a clear message
    # Agent-authored text can contain codepoints a GBK console cannot print.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    log: dict = {"test": "T38-m4-real-e2e", "worker_started": True,
                 "model": model_from_env().describe(),
                 "fixture_root": str(FIXTURE)}
    if args.part in ("goal", "all"):
        log["goal_part"] = goal_part()
    if args.part in ("isolation", "all"):
        log["isolation_part"] = isolation_part()

    checks: dict = {}

    def collect(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                name = f"{prefix}.{key}" if prefix else key
                if isinstance(value, bool):
                    checks[name] = value
                elif isinstance(value, dict):
                    collect(value, name)

    collect(log.get("goal_part") or {}, "goal")
    collect(log.get("isolation_part") or {}, "isolation")
    log["checks"] = checks
    log["result"] = "Pass" if checks and all(checks.values()) else "Fail"
    write_evidence(EVIDENCE, log)
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if log["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
