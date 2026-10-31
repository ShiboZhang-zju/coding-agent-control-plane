"""T53 (real part): can a human actually *talk* to CodeBuddy and get work done?

This is the acceptance the M4-D audit asked for and the deterministic suites
cannot give: a real intent classifier reading real sentences, a real resident
Runtime, a real CodeBuddy Worker, real verification — and every fact reported
back in the conversation read from the ledger.

Deployment under test (exactly what an operator runs)::

    CodeBuddy HTTP service                       (scripts/serve_ctl.py start)
    scripts/run_controlplane.py --advance-goals  (the normal resident service)
    one shared SQLite ledger
    a real, explicitly authorized LLM            (LD_MODEL_*)

The script only ever:

* talks to the LD through :class:`ld.chat.LDChat` (four ordinary sentences),
* starts/stops the service process,
* *reads* the ledger.

It never calls ``run_due`` / ``run_once`` / ``run_goal`` directly, never writes a
verdict, never edits goal state and never simulates Worker success.  The single
non-conversational write is the project registration every scenario needs.

Conversation under test
-----------------------
A1  "现在有哪些项目？"                      -> list_projects, answered from the ledger
A2  "<proj> 现在进行到哪了？"                -> project_status
B1  "在 <proj> 完成 <objective>"             -> submit_goal -> Goal created
    .. the resident service advances it: LD decides, Worker runs, Verifier judges
B2  "<proj> 的目标现在怎么样了？"            -> goal_status, reports ledger verdicts
C   "刚才那个任务完成了吗？"                  -> task/goal status, no new Goal

Strictness (what makes a run FAIL rather than "partially pass"):

* exactly one Goal exists for the project at the end, and it is the one the
  conversation created — no extra goals from any turn;
* exactly the number of WorkItems the goal actually needed — a chat turn that
  quietly dispatched work fails the run;
* the reported verdict must equal the ledger verdict (not merely be non-empty);
* read turns must not have changed the work-item count;
* if the real model answers a read question with ``submit_goal``, that is a
  finding: the turn is staged by design (never applied), and it is recorded.

Requires ``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME`` (and ``LD_MODEL_API_KEY``
when applicable).  Without them the run is recorded as environment-blocked,
never as a pass.
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
from ld.chat import LDChat  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.model import OpenAIChatIntentModel  # noqa: E402
from ld.tools import LDTools  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
FIXTURE = ROOT / "sandbox" / f"t53-real-chat-{uuid.uuid4().hex[:8]}"
STATE = ROOT / ".poc-state" / "t53"
EVIDENCE = ROOT / "evidence" / "project" / "T53-real-chat-e2e.json"
RUNTIME_LOG = STATE / "runtime-stdout.log"

PROJECT_NAME = "t53chat"

#: One bounded objective.  Kept deliberately small: this test is about the
#: conversation path, not about a long multi-stage engineering goal (T48 owns
#: that).  The owner states it in plain language, exactly once.
OBJECTIVE = (
    "实现 configlib 包中的 load_config(config_dict) 函数，使预置测试 "
    "test_load.py 全部通过。验收断言用 file_contains 检查 "
    "configlib/__init__.py 中 load_config 的定义，不要用 git_clean。"
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
    git(path, "config", "user.email", "t53@example.invalid")
    git(path, "config", "user.name", "T53")
    for name, content in SEED_FILES.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    perm = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}}, indent=1)
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(perm, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")
    return path


def fresh_ledger(name: str) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"{name}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def ensure_service() -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "start", "--port", str(PORT)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {"error": (proc.stderr or "")[-400:]}


def stop_service() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "stop", "--port", str(PORT)],
        capture_output=True, text=True, timeout=90, cwd=str(ROOT))


def read_only(ledger: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def goal_row(ledger: Path, goal_id: str) -> dict:
    rows = read_only(ledger, "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,))
    return rows[0] if rows else {}


def goal_history(ledger: Path, goal_id: str) -> list[dict]:
    return read_only(
        ledger,
        "SELECT step, state, decision, outcome FROM ld_goal_decisions "
        "WHERE goal_id=? ORDER BY step", (goal_id,))


def ledger_tasks(ledger: Path, goal_ref: str) -> list[dict]:
    store = TaskStore(ledger)
    try:
        tasks = [task for task in store.all()
                 if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    finally:
        store.close()
    tasks.sort(key=lambda task: task.get("created_at") or 0)
    return tasks


def start_runtime(ledger: Path, log_path: Path) -> subprocess.Popen:
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
        "--goal-lifetime-decisions", "12", "--goal-lifetime-tasks", "2",
        "--known-interval", "5", "--dispatch-interval", "1",
        "--max-concurrent", "1",
    ]
    out = open(log_path, "ab")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    return subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(ROOT), env=env,
                            creationflags=flags)


def stop_runtime(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_goal_settled(ledger: Path, goal_id: str, *, timeout_s: float,
                      poll_s: float = 8.0) -> list[dict]:
    """Observe only.  Terminal goal states are the runner's decision, not ours."""
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
        if goal.get("status") in ("completed", "needs_human", "failed"):
            break
        time.sleep(poll_s)
    return timeline


def register_project(ledger: Path, repo: Path) -> str:
    adapter = CodeBuddyAdapter(Api(base=BASE), autologin=True)
    store = TaskStore(ledger)
    try:
        plane = ControlPlane(adapter, store, require_registered_projects=True,
                             worker_instance_id="wi-t53")
        service_plane = plane
        from controlplane.project_service import ProjectService  # noqa: PLC0415
        ProjectService(service_plane)
        project = plane.register_project(PROJECT_NAME, str(repo),
                                         default_adapter="codebuddy")
        return project["id"]
    finally:
        store.close()


def build_chat(ledger: Path, model) -> tuple[LDChat, LDGoalStore, LDChatStore,
                                             TaskStore]:
    store = TaskStore(ledger)
    from controlplane.project_service import ProjectService  # noqa: PLC0415

    class _StubDispatch:
        """Placeholder adapter: the chat layer must never dispatch anything."""

        name = "chat-only"

        def dispatch(self, request):  # noqa: ANN001
            raise AssertionError(
                "the chat layer must never dispatch work directly")

    plane = ControlPlane(_StubDispatch(), store, require_registered_projects=True,
                         worker_instance_id="wi-t53-chat")
    tools = LDTools(ProjectService(plane))
    goals = LDGoalStore(store.path, lease_s=30.0)
    chats = LDChatStore(store.path)
    chat = LDChat(tools, goals, chats, model,
                  model_authorized_security_domains=("local", "shareable"))
    return chat, goals, chats, store


def task_truth(ledger: Path, goal_ref: str) -> dict:
    tasks = ledger_tasks(ledger, goal_ref)
    return {
        "count": len(tasks),
        "items": [{"task_id": task.get("task_id"),
                   "state": task.get("state"),
                   "verdict": task.get("verdict"),
                   "queue_status": task.get("queue_status"),
                   "session_id": (task.get("metadata") or {}).get("session_id")
                   or (task.get("metadata") or {}).get("worker_job_id"),
                   "assertions": len(task.get("assertions") or [])}
                  for task in tasks],
    }


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("LD_MODEL_BASE_URL", "").strip()
    model_name = os.environ.get("LD_MODEL_NAME", "").strip()
    if not base_url or not model_name:
        evidence = {
            "test": "T53 real chat end-to-end (M4-D)",
            "result": "BLOCKED",
            "blocked_reason": ("LD_MODEL_BASE_URL / LD_MODEL_NAME not configured; "
                               "no real intent classifier available. This run "
                               "proves nothing about the chat path."),
            "note": ("Environment-blocked runs are recorded as BLOCKED, never as "
                     "a pass."),
        }
        write_evidence(EVIDENCE, evidence)
        print(json.dumps({"result": "BLOCKED", "reason": evidence["blocked_reason"]},
                         ensure_ascii=False))
        return 2

    model = OpenAIChatIntentModel.from_env()
    turns: list[dict] = []
    runtime = None
    evidence: dict = {}
    try:
        repo = make_repo(FIXTURE / "repo")
        ledger = fresh_ledger("real-chat")
        service = ensure_service()
        project_id = register_project(ledger, repo)
        chat, goals, chats, store = build_chat(ledger, model)
        try:
            conversation_id = None

            def say(text: str) -> dict:
                nonlocal conversation_id
                turn = chat.handle(text, conversation_id=conversation_id)
                conversation_id = turn["conversation_id"]
                tasks_before = task_truth(ledger, "")["count"]
                turns.append({
                    "message": text,
                    "intent": (turn.get("intent") or {}).get("intent"),
                    "intent_source": turn.get("intent_source"),
                    "requires_confirmation": turn.get("requires_confirmation"),
                    "answer_head": (turn.get("answer") or "")[:400],
                    "goal_ids": turn.get("goal_ids"),
                    "staged": turn.get("staged_for_confirmation"),
                    "work_items_seen_before": tasks_before,
                })
                return turn

            # A1: registry, from the ledger.
            a1 = say("现在有哪些项目？")
            # A2: project progress.
            a2 = say(f"{PROJECT_NAME} 现在进行到哪了？")
            tasks_after_reads = len(store.all())

            # B1: the actual goal, stated once, in plain language.
            b1 = say(f"在 {PROJECT_NAME} 完成这件事：{OBJECTIVE}")
            goal_id = (b1.get("facts") or {}).get("goal_id")
            staged = bool(b1.get("requires_confirmation"))
            if staged:
                # The deterministic second opinion did not read the sentence as
                # an imperative; confirm exactly as a human would.
                b1_confirm = say("确认")
                goal_id = goal_id or (b1_confirm.get("facts") or {}).get("goal_id")
            tasks_after_submit = len(store.all())

            # The resident service does all the advancing.
            runtime = start_runtime(ledger, RUNTIME_LOG)
            timeline = wait_goal_settled(ledger, goal_id, timeout_s=3600.0)
            goal = goal_row(ledger, goal_id)
            _goal_id2, goal_ref = goal_identity(
                (goal.get("goal_text") or "").strip(), project_id)
            truth = task_truth(ledger, goal_ref)

            # B2 + C: report back from the ledger.
            b2 = say(f"{PROJECT_NAME} 的目标现在怎么样了？")
            c1 = say("刚才那个任务完成了吗？")
            tasks_after_reports = len(store.all())

            goals_for_project = goals.find_by_project(project_id)
            history = goal_history(ledger, goal_id)
            final_status = goal.get("status")
            verdicts = [item["verdict"] for item in truth["items"]]
            verified = [v for v in verdicts if v == "verified"]

            reported_verified = any(
                "verified" in json.dumps(turn.get("answer") or "").lower()
                for turn in (b2, c1))

            checks = {
                "A1_intent_is_list_projects":
                    (a1.get("intent") or {}).get("intent") == "list_projects",
                "A1_names_the_project":
                    PROJECT_NAME in (a1.get("answer") or "")
                    or project_id in (a1.get("answer") or ""),
                "A2_intent_is_a_read":
                    (a2.get("intent") or {}).get("intent") in (
                        "project_status", "goal_status", "clarify"),
                "reads_created_no_work_item":
                    tasks_after_reads == 0,
                "B1_created_some_goal": bool(goal_id),
                "B1_intent_is_submit_goal":
                    (b1.get("intent") or {}).get("intent") == "submit_goal",
                "B1_was_not_needlessly_staged": not staged,
                "B1_created_no_work_item_itself":
                    tasks_after_submit == 0,
                "goal_reached_a_terminal_state":
                    final_status in ("completed", "needs_human", "failed"),
                "goal_completed": final_status == "completed",
                "exactly_one_goal_for_project": len(goals_for_project) == 1,
                "goal_is_the_one_conversation_created":
                    goals_for_project and goals_for_project[0]["goal_id"] == goal_id,
                "real_worker_produced_a_task": truth["count"] >= 1,
                "worker_state_is_succeeded":
                    all(item["state"] == "succeeded" for item in truth["items"])
                    if truth["items"] else False,
                "verifier_verified_the_task": bool(verified),
                "decision_history_is_contiguous":
                    [row["step"] for row in history]
                    == list(range(1, len(history) + 1)),
                "B2_reported_a_ledger_fact":
                    (b2.get("intent") or {}).get("intent") in (
                        "goal_status", "project_status")
                    and (b2.get("facts") or {}).get("project_id") == project_id,
                "B2_reported_the_real_verdict":
                    reported_verified,
                "C_no_extra_goal_created":
                    len(goals.find_by_project(project_id)) == 1,
                "reports_created_no_work_item":
                    tasks_after_reports == truth["count"],
                "no_turn_was_silently_staged_and_forgotten":
                    all(turn.get("intent") is not None for turn in turns),
                "real_model_was_used":
                    all(turn.get("intent_source") in ("model", "human_confirmation",
                                                      "local_only")
                        for turn in turns if turn.get("intent_source")),
            }
            evidence = {
                "test": "T53 real chat end-to-end (M4-D)",
                "deployment": {
                    "service": service,
                    "base": BASE,
                    "model": {"base_url": base_url, "name": model_name},
                },
                "fixture": "<REPO>/sandbox/t53-real-chat-*",
                "objective": OBJECTIVE,
                "conversation": turns,
                "goal": {
                    "goal_id": goal_id,
                    "status": final_status,
                    "decision_count": goal.get("decision_count"),
                    "task_count": goal.get("task_count"),
                    "max_decisions_total": goal.get("max_decisions_total"),
                    "max_tasks_total": goal.get("max_tasks_total"),
                    "goals_in_project": len(goals_for_project),
                    "history_steps": len(history),
                },
                "work_items": truth,
                "timeline_tail": timeline[-6:],
                "checks": checks,
            }
            failed = [key for key, value in checks.items()
                      if isinstance(value, bool) and not value]
            evidence["failed"] = failed
            evidence["result"] = "PASS" if not failed else "FAIL"
        finally:
            chats.close()
            goals.close()
            store.close()
    except Exception as exc:  # noqa: BLE001 - a crashed run is evidence
        evidence = {"test": "T53 real chat end-to-end (M4-D)",
                    "result": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "conversation": turns}
    finally:
        stop_runtime(runtime)
        stop_service()

    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence.get("result"),
                      "failed": evidence.get("failed", []),
                      "error": evidence.get("error")}, ensure_ascii=False))
    return 0 if evidence.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
