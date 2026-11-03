"""T66 / UI-1 final acceptance: console -> real LD -> real Worker -> Verifier.

The gap this closes
-------------------
T65 proved the *forwarding chain* with a scripted decision model and a stub
Worker, so its evidence records ``real_llm_ld_decision = BLOCKED_ENVIRONMENT``.
That proves "the console cannot dispatch by itself", not "a real CodeBuddy
worker finishes what the console published".  This producer runs the normal
deployment and nothing else:

    CodeBuddy HTTP service          scripts/serve_ctl.py start
    Dashboard (UI-1)                scripts/dashboard_server.py --enable-chat
    Resident runtime                scripts/run_controlplane.py --advance-goals
    real LLM decision model         LD_MODEL_* (never written to evidence)
    one shared SQLite ledger

The operator acts only through the console's HTTP API: publish an objective,
then read.  The script never calls ``run_due``/``advance_due``/``run_goal``/
``run_once``, never writes a verdict, never edits goal state and never
simulates Worker success.

Scenarios
---------
S1  happy path, zero touch: publish from the console -> resident runtime wakes
    the real LLM -> LD submits a WorkItem -> ControlPlane dispatches -> a real
    CodeBuddy session edits the throw-away repo -> the real Verifier derives
    ``verified`` -> the dashboard shows Goal -> WorkItem -> Verdict with the
    ledger's own ``ld_goal_ref`` link.
S2  restart: kill the Dashboard *and* the runtime, start both again, then
    assert the history/links survived and no second WorkItem was dispatched.
S3  no false success: a read-only objective (the agent must not modify
    anything) cannot satisfy an artifact assertion, so the verdict must not be
    ``verified`` and the goal must not be ``completed``.  The dashboard must
    report that state as-is.  Anything that looks like "finished" here is a
    Fail, not a pass with a caveat.

Requires ``LD_MODEL_BASE_URL`` / ``LD_MODEL_NAME`` (+ ``LD_MODEL_API_KEY`` when
applicable).  Without them the run is recorded as ``EnvironmentBlocked`` and is
**not** a Pass.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.codebuddy.adapter import CodeBuddyAdapter  # noqa: E402
from adapters.codebuddy.client import Api  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from ld.goal_store import goal_identity  # noqa: E402
from sanitize_evidence import write_evidence  # noqa: E402

WORKER_PORT = 8787
WORKER_BASE = f"http://127.0.0.1:{WORKER_PORT}"
DASH_PORT = 8792
DASH_BASE = f"http://127.0.0.1:{DASH_PORT}"
TOKEN = "t66-console-operator-token-32-characters"
STAMP = uuid.uuid4().hex[:8]
FIXTURE = ROOT / "sandbox" / f"t66-real-e2e-{STAMP}"
STATE = ROOT / ".poc-state" / "t66"
EVIDENCE = ROOT / "evidence" / "project" / "T66-ui1-real-e2e.json"

#: Read-only objective for the failure scenario.  It asks the agent to inspect
#: and explicitly forbids editing, so the artifact assertions the LD declares
#: cannot be satisfied by a legitimate read-only session.
READONLY_GOAL = (
    "只分析本项目结构，汇报 configlib 目前缺哪些函数。"
    "不要创建任何文件，不要修改任何文件，不要执行 git commit。"
)

HAPPY_GOAL = (
    "在当前项目中实现 configlib 包的 load_config(config_dict) 函数，"
    "使预置测试 test_load.py 全部通过。如当前在 main/master 检出，"
    "请先创建并切换到新的特性分支再修改（允许自建分支，不要 push，"
    "绝不要修改 main/master）。验收断言使用 file_exists 或 file_contains，"
    "针对 configlib/__init__.py，不要使用 git_clean。"
    # Binding statement of scope.  Without it a real model can read the sentence
    # above as two deliverables (implement + commit-on-a-branch) and submit a
    # second, redundant task after the first one is verified — the same class of
    # objective ambiguity recorded as M4C-11.  This is fixture quality, not an
    # assertion being relaxed: one artifact, one task.
    "这个目标只需要一个任务完成：configlib/__init__.py 中出现 def load_config "
    "且 test_load.py 通过即视为目标达成。一旦该任务被 Verifier 判定为 "
    "verified，请立即结束目标，不要再次提交同类任务。"
)

SEED_FILES = {
    "configlib/__init__.py": "",
    "README.md": "t66 real end-to-end fixture\n",
    "test_load.py": (
        "from configlib import load_config\n\n\n"
        "def test_load_returns_dict():\n"
        "    cfg = load_config({\"host\": \"localhost\", \"port\": 8080})\n"
        "    assert cfg == {\"host\": \"localhost\", \"port\": 8080}\n\n\n"
        "def test_load_empty():\n"
        "    assert load_config({}) == {}\n"
    ),
}


# --------------------------------------------------------------------------- #
# fixture / ledger helpers
# --------------------------------------------------------------------------- #
def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(name: str) -> Path:
    repo = FIXTURE / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", f"t66-{name}@example.invalid")
    git(repo, "config", "user.name", f"T66-{name}")
    for rel, content in SEED_FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Project-scoped Worker permissions, inherited by every worktree: the agent
    # needs pytest/git inside its own branch.  Same mechanism T38/T48 use.
    perm = json.dumps({"permissions": {"allow": ["Bash", "PowerShell"]}}, indent=1)
    for rel in (".claude/settings.json", ".codebuddy/settings.json"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(perm, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "branch", "-M", "main")
    return repo


def fresh_ledger() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"t66-{STAMP}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


def read_only(ledger: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def goal_row(ledger: Path, goal_id: str) -> dict:
    """One raw goal row plus the projections the store derives from it.

    ``model_decision_count`` is not a column: ``LDGoalStore._row_to_goal``
    derives it as ``decision_count - resume_count`` (a human resume appends a
    decision without spending the model).  Computing it here keeps this probe
    read-only while using the same definition, instead of inventing a field a
    raw ``SELECT *`` never returns.
    """
    rows = read_only(ledger, "SELECT * FROM ld_goals WHERE goal_id=?", (goal_id,))
    if not rows:
        return {}
    row = rows[0]
    row["model_decision_count"] = (int(row.get("decision_count") or 0)
                                   - int(row.get("resume_count") or 0))
    return row


def goal_steps(ledger: Path, goal_id: str) -> list[dict]:
    try:
        rows = read_only(
            ledger,
            "SELECT step, state, decision, outcome FROM ld_goal_decisions "
            "WHERE goal_id=? ORDER BY step", (goal_id,))
    except sqlite3.OperationalError:
        return []
    for row in rows:
        for key in ("decision", "outcome"):
            try:
                row[key] = json.loads(row[key] or "{}")
            except json.JSONDecodeError:
                row[key] = {}
    return rows


def goal_tasks(ledger: Path, goal_ref: str) -> list[dict]:
    store = TaskStore(ledger, read_only=True)
    try:
        tasks = [task for task in store.all()
                 if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
    finally:
        store.close()
    tasks.sort(key=lambda task: task.get("created_at") or 0)
    return tasks


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #
def start_worker() -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "start", "--port", str(WORKER_PORT)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {"result": "no_output"}


def stop_worker() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_ctl.py"),
         "stop", "--port", str(WORKER_PORT)],
        capture_output=True, text=True, timeout=90, cwd=str(ROOT))


def start_dashboard(ledger: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["DASHBOARD_CHAT_TOKEN"] = TOKEN
    log = open(STATE / "dashboard.log", "ab")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "dashboard_server.py"),
         "--ledger", str(ledger), "--host", "127.0.0.1", "--port", str(DASH_PORT),
         "--enable-chat", "--model-domains", "local,shareable"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=str(ROOT), env=env,
        creationflags=(subprocess.CREATE_NO_WINDOW
                       | subprocess.CREATE_NEW_PROCESS_GROUP))


def start_runtime(ledger: Path, *, tag: str) -> subprocess.Popen:
    """The normal resident service: it schedules, it does not decide."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(STATE / f"runtime-{tag}.log", "ab")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_controlplane.py"),
         "--ledger", str(ledger), "--base", WORKER_BASE,
         "--advance-goals", "--model-domains", "local,shareable",
         "--goal-interval", "3", "--goal-max-decisions", "1",
         "--goal-max-tasks", "1", "--goal-max-model-failures", "4",
         "--goal-base-backoff", "5", "--goal-max-backoff", "60",
         "--goal-lifetime-decisions", "30", "--goal-lifetime-tasks", "4",
         "--known-interval", "5", "--dispatch-interval", "1",
         "--max-concurrent", "2"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=str(ROOT), env=env,
        creationflags=(subprocess.CREATE_NO_WINDOW
                       | subprocess.CREATE_NEW_PROCESS_GROUP))


def kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"],
                   capture_output=True, text=True, timeout=60)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def wait_http(url: str, *, timeout_s: float = 90.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    return False


def wait_worker(*, timeout_s: float = 180.0) -> bool:
    """Wait until the CodeBuddy service answers an authenticated health call.

    The plain ``/api/v1/health`` path needs the adapter's request header, so a
    bare ``urlopen`` would report a false negative.  This uses the same client
    the adapter uses, including its login, and never prints the password.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            api = Api(base=WORKER_BASE)
            api.login()
            api.health()
            return True
        except Exception:  # noqa: BLE001 - a not-yet-ready server is expected
            time.sleep(2.0)
    return False


# --------------------------------------------------------------------------- #
# console client (the operator's only interface in this test)
# --------------------------------------------------------------------------- #
def console(path: str, *, method: str = "GET", payload=None,
            token: str | None = TOKEN) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "Origin": DASH_BASE}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(DASH_BASE + path, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": "non-json error body"}


def publish(objective: str, project_id: str) -> tuple[int, dict]:
    return console("/api/chat/publish", method="POST",
                   payload={"project_id": project_id, "objective": objective})


def project_detail(project_id: str) -> dict:
    status, payload = console("/api/projects/" + project_id)
    if status != 200:
        return {"_status": status}
    return payload


# --------------------------------------------------------------------------- #
# project registration
# --------------------------------------------------------------------------- #
def register(ledger: Path, name: str, repo: Path) -> str:
    adapter = CodeBuddyAdapter(Api(base=WORKER_BASE), autologin=True)
    store = TaskStore(ledger)
    try:
        plane = ControlPlane(adapter, store, require_registered_projects=True,
                             worker_instance_id=f"wi-t66-{STAMP}")
        project = plane.register_project(name, str(repo),
                                         default_adapter="codebuddy",
                                         concurrency_limit=2)
    finally:
        store.close()
    return project["id"]


# --------------------------------------------------------------------------- #
# observation
# --------------------------------------------------------------------------- #
TERMINAL = ("completed", "needs_human", "failed")


def observe_goal(ledger: Path, goal_id: str, goal_ref: str, *,
                 deadline_s: float, poll_s: float = 10.0,
                 stop_when=None, stall_after_s: float = 1500.0) -> dict:
    """Poll the ledger until the goal is terminal, the budget ends or it stalls.

    A real CodeBuddy session can hang in "starting…" (a known worker-side
    defect).  Rather than burn the whole budget and report nothing, the loop
    stops after ``stall_after_s`` with no observable change and records
    ``stalled=True`` — the run then fails honestly instead of hanging.
    """
    started = time.time()
    timeline: list[dict] = []
    last_change = time.time()
    fingerprint = None
    stalled = False
    while time.time() - started < deadline_s:
        goal = goal_row(ledger, goal_id)
        tasks = goal_tasks(ledger, goal_ref)
        snapshot = {
            "elapsed_s": round(time.time() - started, 1),
            "goal_status": goal.get("status"),
            "decision_count": goal.get("decision_count"),
            "model_decision_count": goal.get("model_decision_count"),
            "task_count": goal.get("task_count"),
            "current_task_id": goal.get("current_task_id"),
            "resume_count": goal.get("resume_count"),
            "tasks": [{"task_id": task["task_id"], "state": task.get("state"),
                       "verdict": task.get("verdict"),
                       "session": bool(task.get("session_id"))}
                      for task in tasks],
        }
        timeline.append(snapshot)
        if goal.get("status") in TERMINAL:
            break
        if stop_when is not None and stop_when(goal, tasks):
            break
        now_fingerprint = (
            goal.get("status"), goal.get("decision_count"), goal.get("task_count"),
            tuple((task["task_id"], task.get("state"), task.get("verdict"))
                  for task in tasks))
        if now_fingerprint != fingerprint:
            fingerprint = now_fingerprint
            last_change = time.time()
        elif time.time() - last_change > stall_after_s:
            stalled = True
            break
        time.sleep(poll_s)
    return {"timeline": timeline, "goal": goal_row(ledger, goal_id),
            "tasks": goal_tasks(ledger, goal_ref), "stalled": stalled,
            "idle_s": round(time.time() - last_change, 1)}


def tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    STATE.mkdir(parents=True, exist_ok=True)

    model_ready = bool(os.environ.get("LD_MODEL_BASE_URL", "").strip()
                       and os.environ.get("LD_MODEL_NAME", "").strip())
    if not model_ready:
        write_evidence(EVIDENCE, {
            "test": "T66 UI-1 real end-to-end",
            "result": "EnvironmentBlocked",
            "reason": ("LD_MODEL_BASE_URL / LD_MODEL_NAME are not configured. "
                       "The real console -> LLM -> CodeBuddy -> Verifier "
                       "acceptance was NOT executed. This is not a Pass."),
        })
        print("EnvironmentBlocked: LD_MODEL_* not configured")
        return 2

    shutil.rmtree(FIXTURE, ignore_errors=True)
    repo = make_repo("happy")
    readonly_repo = make_repo("readonly")
    ledger = fresh_ledger()
    main_tip = git(repo, "rev-parse", "main")

    checks: dict[str, object] = {}
    evidence: dict = {
        "test": "T66 UI-1 real end-to-end (console -> real LLM -> real Worker)",
        "mode": ("normal deployment: serve_ctl worker + dashboard_server "
                 "--enable-chat + run_controlplane --advance-goals, one shared "
                 "ledger; the operator only publishes and reads via HTTP"),
        "model": {"configured": True, "id_persisted": False},
    }
    dashboard = runtime = None
    worker = {}
    try:
        worker = start_worker()
        evidence["worker_start"] = {k: v for k, v in worker.items()
                                    if k in ("result", "port", "bind", "auth")}
        if not wait_worker(timeout_s=180):
            raise RuntimeError("worker service did not become healthy")

        project_id = register(ledger, f"t66-happy-{STAMP}", repo)
        readonly_project = register(ledger, f"t66-readonly-{STAMP}", readonly_repo)
        evidence["projects"] = {"happy": project_id, "readonly": readonly_project}

        dashboard = start_dashboard(ledger)
        if not wait_http(DASH_BASE + "/api/health", timeout_s=90):
            raise RuntimeError("dashboard did not become healthy")

        # ---- S1: publish from the console, zero touch afterwards ---------- #
        status, published = publish(HAPPY_GOAL, project_id)
        checks["S1.publish_http_200"] = status == 200
        goal_id = published.get("goal_id")
        goal_ref = published.get("goal_ref")
        checks["S1.publish_created_one_goal"] = bool(goal_id) and published.get(
            "created") is True and published.get("next_step") == "waiting_for_ld_runner"
        st, detail = console("/api/projects/" + project_id)
        checks["S1.publish_did_not_create_work_item"] = (
            st == 200 and detail.get("tasks") == [])

        runtime = start_runtime(ledger, tag="s1")
        s1 = observe_goal(ledger, goal_id, goal_ref, deadline_s=3.2 * 3600)
        tasks = s1["tasks"]
        goal = s1["goal"]
        evidence["S1"] = {
            "goal": {k: goal.get(k) for k in (
                "status", "decision_count", "model_decision_count",
                "task_count", "resume_count", "max_decisions_total",
                "max_tasks_total", "last_decision")},
            "tasks": [{"task_id": t["task_id"], "state": t.get("state"),
                       "verdict": t.get("verdict"),
                       "session_id": bool(t.get("session_id")),
                       "worktree": bool(t.get("worktree")),
                       "head_commit": bool(t.get("head_commit")),
                       "assertion_results": t.get("assertion_results"),
                       "assertion_owned": [bool(result.get("owned"))
                                           for result in
                                           (t.get("assertion_results") or [])],
                       "metadata": {k: v for k, v in (t.get("metadata") or {}).items()
                                    if k in ("submitted_by", "ld_goal_ref")}}
                      for t in tasks],
            "steps": [{k: step.get(k) for k in ("step", "state")}
                      | {"action": (step.get("decision") or {}).get("action"),
                         "reason": (step.get("decision") or {}).get("reason")}
                      for step in goal_steps(ledger, goal_id)],
            "stalled": s1.get("stalled"),
            "idle_s": s1.get("idle_s"),
            "timeline_tail": s1["timeline"][-6:],
            "runtime_log_tail": tail(STATE / "runtime-s1.log"),
        }
        checks["S1.did_not_stall"] = s1.get("stalled") is False
        checks["S1.runtime_woke_the_ld"] = int(goal.get("model_decision_count") or 0) >= 1
        # Independent of that projection: exactly one committed step exists for
        # this goal, and its decision is an action only the LD can produce.
        steps = goal_steps(ledger, goal_id)
        submit_steps = [step for step in steps
                        if (step.get("decision") or {}).get("action") == "submit_task"]
        checks["S1.exactly_one_submit_step"] = len(submit_steps) == 1
        checks["S1.llm_produced_the_decision"] = bool(submit_steps) and bool(
            (submit_steps[0].get("decision") or {}).get("reason"))
        checks["S1.ld_submitted_exactly_one_task"] = len(tasks) == 1
        checks["S1.task_submitted_by_ld"] = bool(tasks) and all(
            (t.get("metadata") or {}).get("submitted_by") == "ld" for t in tasks)
        checks["S1.real_codebuddy_session"] = bool(tasks) and all(
            bool(t.get("session_id")) for t in tasks)
        checks["S1.real_verifier_verified"] = bool(tasks) and all(
            t.get("verdict") == "verified" for t in tasks)
        # A worktree/head_commit only exists when the agent explicitly committed.
        # The objective asks for a branch + commit only when the checkout is
        # main/master, so those fields are reported, not required: requiring them
        # would grade the model's git style rather than the acceptance chain.
        checks["S1.task_changed_the_repo"] = bool(tasks) and all(
            any(bool(result.get("owned"))
                for result in (t.get("assertion_results") or [])) for t in tasks)
        checks["S1.goal_completed_by_ld"] = goal.get("status") == "completed"
        checks["S1.zero_human_resumes"] = int(goal.get("resume_count") or 0) == 0
        checks["S1.main_branch_untouched"] = git(repo, "rev-parse", "main") == main_tip
        # The artifact must really exist on disk where the verifier looked, not
        # merely be reported as verified.  Worktree first (when the agent
        # committed), then the project tree itself.
        candidates = [Path(task["worktree"]) / "configlib" / "__init__.py"
                      for task in tasks if task.get("worktree")]
        candidates.append(repo / "configlib" / "__init__.py")
        loaded = ""
        for candidate in candidates:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                if "def load_config" in text:
                    loaded = text
                    break
        checks["S1.load_config_really_written"] = "def load_config" in loaded

        # ---- S1b: dashboard shows Goal -> WorkItem -> Verdict ------------- #
        detail = project_detail(project_id)
        goal_view = next((g for g in detail.get("goals", [])
                          if g.get("goal_id") == goal_id), {})
        task_views = {t["task_id"]: t for t in detail.get("tasks", [])}
        linked = goal_view.get("linked_task_ids") or []
        checks["S1b.dashboard_lists_the_goal"] = bool(goal_view)
        checks["S1b.dashboard_goal_status_is_ledger_status"] = (
            goal_view.get("status") == goal.get("status"))
        checks["S1b.dashboard_links_the_forwarded_task"] = (
            linked == [t["task_id"] for t in tasks])
        checks["S1b.dashboard_task_shows_verified_verdict"] = bool(linked) and all(
            task_views.get(tid, {}).get("verdict") == "verified" for tid in linked)
        checks["S1b.dashboard_task_carries_the_goal_ref"] = bool(linked) and all(
            task_views.get(tid, {}).get("goal_ref") == goal_ref for tid in linked)
        checks["S1b.no_invented_progress_percentage"] = (
            "progress_percent" not in detail and "completion" not in detail.get("project", {}))
        evidence["S1b_dashboard"] = {
            "goal": {k: goal_view.get(k) for k in (
                "goal_id", "status", "linked_task_count", "linked_task_ids",
                "linked_task_counts", "linked_verified_count", "task_count")},
            "tasks": [{k: task_views.get(tid, {}).get(k) for k in (
                "task_id", "state", "verdict", "goal_ref", "goal_id")}
                for tid in linked],
        }

        # ---- S3: a failure must never look like success ------------------- #
        # Published and advanced by the same real stack; the objective forbids
        # editing, so the declared artifact assertions cannot pass.
        status, ro_published = publish(READONLY_GOAL, readonly_project)
        ro_goal_id = ro_published.get("goal_id")
        ro_goal_ref = ro_published.get("goal_ref")
        checks["S3.readonly_publish_accepted"] = status == 200 and bool(ro_goal_id)
        ro = observe_goal(ledger, ro_goal_id, ro_goal_ref, deadline_s=1.6 * 3600)
        ro_goal = ro["goal"]
        ro_tasks = ro["tasks"]
        ro_detail = project_detail(readonly_project)
        ro_view = next((g for g in ro_detail.get("goals", [])
                        if g.get("goal_id") == ro_goal_id), {})
        evidence["S3_failure_path"] = {
            "goal": {k: ro_goal.get(k) for k in (
                "status", "decision_count", "task_count", "last_decision")},
            "tasks": [{"task_id": t["task_id"], "state": t.get("state"),
                       "verdict": t.get("verdict"),
                       "assertion_results": t.get("assertion_results")}
                      for t in ro_tasks],
            "dashboard_goal": {k: ro_view.get(k) for k in (
                "goal_id", "status", "linked_task_count", "linked_verified_count")},
            "timeline_tail": ro["timeline"][-5:],
        }
        checks["S3.did_not_stall"] = ro.get("stalled") is False
        checks["S3.no_task_reached_verified"] = all(
            t.get("verdict") != "verified" for t in ro_tasks)
        # With only negative assertions the attempt cannot prove it produced
        # anything, so the shared acceptance rule must degrade it explicitly
        # (inconclusive) instead of accepting it.  A rejected verdict is also
        # acceptable: both refuse to call it done.
        checks["S3.negative_only_evidence_not_accepted"] = all(
            t.get("verdict") in ("inconclusive", "rejected") for t in ro_tasks)
        checks["S3.goal_not_completed"] = ro_goal.get("status") != "completed"
        checks["S3.dashboard_does_not_claim_completion"] = (
            bool(ro_view) and ro_view.get("status") != "completed"
            and int(ro_view.get("linked_verified_count") or 0) == 0)

        # ---- S2: restart both processes; history and links must survive --- #
        tasks_before = len(goal_tasks(ledger, goal_ref))
        kill(runtime)
        kill(dashboard)
        runtime = dashboard = None
        time.sleep(3)
        dashboard = start_dashboard(ledger)
        dashboard_back = wait_http(DASH_BASE + "/api/health", timeout_s=90)
        runtime = start_runtime(ledger, tag="s2")
        time.sleep(45)  # let the restarted runtime sweep at least once

        after_detail = project_detail(project_id)
        after_goal = next((g for g in after_detail.get("goals", [])
                           if g.get("goal_id") == goal_id), {})
        after_tasks = goal_tasks(ledger, goal_ref)
        evidence["S2_restart"] = {
            "dashboard_restarted": bool(dashboard_back),
            "goal_status_after_restart": after_goal.get("status"),
            "linked_task_ids_after_restart": after_goal.get("linked_task_ids"),
            "task_count_after_restart": len(after_tasks),
            "task_verdicts_after_restart": [t.get("verdict") for t in after_tasks],
            "goal_row_after_restart": {
                key: goal_row(ledger, goal_id).get(key)
                for key in ("status", "decision_count", "task_count")},
        }
        checks["S2.dashboard_came_back"] = bool(dashboard_back)
        checks["S2.goal_status_survived"] = after_goal.get("status") == "completed"
        checks["S2.links_survived"] = (
            after_goal.get("linked_task_ids") == [t["task_id"] for t in tasks])
        checks["S2.verdicts_survived"] = all(
            t.get("verdict") == "verified" for t in after_tasks)
        checks["S2.no_duplicate_dispatch"] = len(after_tasks) == tasks_before == 1
        checks["S2.ledger_truth_unchanged"] = (
            goal_row(ledger, goal_id).get("status") == "completed")
    except Exception as exc:  # noqa: BLE001 - the evidence must record the failure
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        checks["run.completed_without_exception"] = False
    finally:
        kill(runtime)
        kill(dashboard)
        stop_worker()

    evidence["checks"] = checks
    evidence["patched_env"] = False
    evidence["not_covered"] = [
        "browser click-through (this run drives the same HTTP API the page uses)",
        "Runner heartbeat/liveness (still design-limited, see UI1-R01)",
    ]
    evidence["result"] = "Pass" if all(bool(v) for v in checks.values()) else "Fail"
    evidence["ledger"] = {"path_relative": str(ledger.relative_to(ROOT)),
                          "sha256": (__import__("hashlib").sha256(
                              ledger.read_bytes()).hexdigest()
                              if ledger.is_file() else None)}
    write_evidence(EVIDENCE, evidence)
    failed = [name for name, value in checks.items() if not value]
    print(json.dumps({"result": evidence["result"], "failed": failed,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False, indent=2))
    return 0 if evidence["result"] == "Pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
