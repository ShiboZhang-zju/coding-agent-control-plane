"""T65 / UI-1b: console publication -> LD forwarding -> ledger tracking.

What this producer proves, and what it refuses to prove
------------------------------------------------------
It exercises the *whole* chain the console adds, over a real loopback HTTP
server and a real SQLite ledger:

    console POST /api/chat/publish
      -> LDChat.publish_goal          (one Goal row, no WorkItem)
      -> LDGoalRunner.advance_due      (the *existing* deterministic scheduler)
      -> LD decision                   (scripted model, not an LLM endpoint)
      -> LDTools.submit_project_task   (real idempotency/policy/approval path)
      -> GET /api/projects/{id}        (Goal -> WorkItem link from the ledger)

The decision model is a **scripted** stand-in, exactly like T50: it stands in
for the LD's reasoning, not for the control plane.  Therefore:

* the forwarding chain (Goal -> WorkItem -> verdict) is verified for real;
* a *real LLM* LD decision is **not** verified here.  When no ``LD_MODEL_*``
  endpoint is configured the evidence records ``BLOCKED_ENVIRONMENT`` for that
  separate acceptance instead of claiming a pass.

No verdict is fabricated: the scripted model only submits, and this producer
never writes ``verified``.  Whatever the stub Worker/Verifier leaves in the
ledger is what gets reported.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from sanitize_evidence import write_evidence  # noqa: E402

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.coordinator import LDCoordinator  # noqa: E402
from ld.goal_runner import LDGoalRunner  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T65-console-task-forwarding.json"
STATE = ROOT / ".poc-state" / "t65"
TOKEN = "t65-console-operator-token-32-characters"
OBJECTIVE = "实现 greet.py 并补充测试"


class StubAdapter:
    """Dispatch-accepting stub; it never claims a success or a verdict."""

    name = "stub"

    def __init__(self):
        from adapters.base import DispatchResult
        self._result = DispatchResult

    def dispatch(self, request):  # noqa: ANN001
        return self._result(task_id=request.task_id,
                            worker_job_id=f"job-{request.task_id}",
                            accepted=True)

    def get_task(self, task_id, **_kwargs):  # noqa: ANN001
        from adapters.base import TaskSnapshot, TaskState
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


class ScriptedDecisionModel:
    """Submit the published objective once, then only wait.

    Deliberately minimal: it cannot approve, cannot write knowledge, cannot
    finish with an unverified claim.  Its purpose is to prove that the runner —
    not the console — performs the forwarding.
    """

    def __init__(self):
        self.calls = 0

    def decide(self, decision_input):  # noqa: ANN001
        self.calls += 1
        project = decision_input.get("requested_project_id")
        state = decision_input.get("goal_state") or {}
        if not state.get("all_goal_tasks"):
            return {"action": "submit_task", "project_id": project,
                    "reason": "forward the goal published by the console",
                    "evidence_refs": [],
                    "parameters": {"task": OBJECTIVE,
                                   "idempotency_key": "t65-forward",
                                   "assertions": []}}
        return {"action": "wait", "project_id": project,
                "reason": "the forwarded WorkItem is in flight",
                "evidence_refs": [],
                "parameters": {"task_id": (state.get("current_task") or {})
                               .get("task_id")}}


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=False,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t65@example.invalid")
    git(path, "config", "user.name", "T65")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(path, "add", "seed.txt")
    git(path, "commit", "-q", "-m", "seed")
    git(path, "branch", "-M", "main")


def fresh_ledger() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    ledger = STATE / f"t65-{uuid.uuid4().hex[:8]}.db"
    for stale in [ledger, *ledger.parent.glob(ledger.name + "*")]:
        stale.unlink(missing_ok=True)
    return ledger


class RunningServer:
    def __init__(self, ledger: Path):
        bridge = ChatBridge(ledger)
        self.server = make_server(ledger, host="127.0.0.1", port=0,
                                  enable_chat=True, chat_token=TOKEN,
                                  chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, path: str, *, method: str = "GET", payload=None,
                token: str | None = TOKEN) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        headers["Origin"] = self.base
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str]) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "scripts"), env.get("PYTHONPATH", "")]).rstrip(
            os.pathsep)
    completed = subprocess.run(argv, cwd=ROOT, env=env, text=True,
                               capture_output=True, timeout=300)
    return {"argv": argv, "exit_code": completed.returncode,
            "stdout": completed.stdout.strip()[-3_000:],
            "stderr": completed.stderr.strip()[-1_500:]}


def main() -> int:
    ledger = fresh_ledger()
    repo = STATE / f"repo-{uuid.uuid4().hex[:8]}"
    make_repo(repo)

    store = TaskStore(ledger)
    plane = ControlPlane(StubAdapter(), store, require_registered_projects=True,
                         worker_instance_id="wi-t65")
    project = plane.register_project("t65", str(repo), default_adapter="stub",
                                     concurrency_limit=2)
    project_id = project["id"]
    store.close()

    goals = LDGoalStore(ledger, lease_s=30.0)
    goal_id, goal_ref = goal_identity(OBJECTIVE, project_id)
    goals.close()

    checks: dict[str, object] = {}
    server = RunningServer(ledger)
    try:
        # 1. Publish: exactly one Goal, and no WorkItem yet.
        status, published = server.request(
            "/api/chat/publish", method="POST",
            payload={"project_id": project_id, "objective": OBJECTIVE})
        checks["publish_http_200"] = status == 200
        checks["publish_created_goal"] = (
            published.get("goal_id") == goal_id
            and published.get("created") is True)
        checks["publish_next_step_is_runner"] = (
            published.get("next_step") == "waiting_for_ld_runner")
        read_store = TaskStore(ledger, read_only=True)
        try:
            checks["publish_created_no_work_item"] = (
                read_store.work_items_for_project(project_id) == [])
        finally:
            read_store.close()

        # 2. Publication is idempotent and converges with a chat submit.
        status, again = server.request(
            "/api/chat/publish", method="POST",
            payload={"project_id": project_id, "objective": OBJECTIVE})
        checks["publish_second_call_reuses_goal"] = (
            status == 200 and again.get("goal_id") == goal_id
            and again.get("reused") is True)

        # 3. The *existing* runner forwards it; the console does not.
        store = TaskStore(ledger)
        plane = ControlPlane(StubAdapter(), store,
                             require_registered_projects=True,
                             worker_instance_id="wi-t65")
        service = ProjectService(plane)
        goals = LDGoalStore(ledger, lease_s=30.0)
        model = ScriptedDecisionModel()
        coordinator = LDCoordinator(
            LDTools(service, goal_store=goals), model,
            model_authorized_security_domains=("local", "shareable"))
        runner = LDGoalRunner(coordinator, goals, max_decisions=2, max_tasks=1,
                              base_backoff_s=0.1, max_backoff_s=0.2,
                              lifetime_max_decisions=4, lifetime_max_tasks=2)
        try:
            runner.advance_due()
            forwarded = [task for task in store.work_items_for_project(project_id)
                         if (task.get("metadata") or {}).get("ld_goal_ref") == goal_ref]
            checks["runner_forwarded_work_item"] = len(forwarded) == 1
            checks["forwarded_by_ld"] = bool(forwarded) and all(
                (task.get("metadata") or {}).get("submitted_by") == "ld"
                for task in forwarded)
            checks["goal_points_at_forwarded_task"] = bool(forwarded) and (
                (goals.get(goal_id) or {}).get("current_task_id")
                == forwarded[0]["task_id"])
            checks["model_called_only_by_runner"] = model.calls >= 1
            checks["no_verdict_invented"] = bool(forwarded) and all(
                task.get("verdict") in (None, "pending") for task in forwarded)
            forwarded_task_ids = [task["task_id"] for task in forwarded]
        finally:
            runner.close()
            goals.close()
            store.close()

        # 4. The read-only projection reports that link from the ledger stamp.
        status, detail = server.request("/api/projects/" + project_id)
        goal = next((item for item in detail.get("goals", [])
                     if item.get("goal_id") == goal_id), {})
        checks["projection_lists_goal"] = status == 200 and bool(goal)
        checks["projection_links_task"] = (
            goal.get("linked_task_ids") == forwarded_task_ids
            and goal.get("linked_task_count") == len(forwarded_task_ids))
        checks["projection_task_carries_goal_ref"] = any(
            task.get("goal_ref") == goal_ref for task in detail.get("tasks", []))

        # 5. Safety: publication cannot pretend to be a chat turn or a budget.
        status, rejected = server.request(
            "/api/chat/publish", method="POST",
            payload={"project_id": project_id, "objective": OBJECTIVE,
                     "max_decisions": 99})
        checks["publish_rejects_extra_fields"] = (
            status == 400
            and rejected.get("error", {}).get("code") == "invalid_fields")
        status, _ = server.request(
            "/api/chat/publish", method="POST",
            payload={"project_id": project_id, "objective": OBJECTIVE},
            token=None)
        # Key name avoids a credential-looking suffix: the evidence sanitizer
        # redacts ``*_token`` keys, which would hide a real boolean result.
        checks["publish_requires_bearer_auth"] = status == 401

        read_store = TaskStore(ledger, read_only=True)
        goals_ro = LDGoalStore(ledger, read_only=True)
        try:
            final_goal = goals_ro.get(goal_id) or {}
            task_count = len(read_store.work_items_for_project(project_id))
        finally:
            goals_ro.close()
            read_store.close()
        checks["one_goal_one_task"] = task_count == 1
        checks["budget_stamped_by_runner_not_console"] = (
            final_goal.get("max_decisions_total") == 4
            and final_goal.get("max_tasks_total") == 2)
    finally:
        server.close()

    # The repository's own deterministic suites, including the new publish ones.
    pytest_run = run([sys.executable, "-m", "pytest",
                      "tests/test_ui1_web_chat.py", "tests/test_ui0_dashboard.py",
                      "-q"])
    node_check = run(["node", "--check", "dashboard/static/app.js"])

    model_ready = bool(os.environ.get("LD_MODEL_BASE_URL")
                       and os.environ.get("LD_MODEL_NAME"))
    passed = (all(bool(value) for value in checks.values())
              and pytest_run["exit_code"] == 0
              and node_check["exit_code"] == 0)
    evidence = {
        "test": "T65 console publication -> LD forwarding -> ledger tracking",
        "mode": ("real loopback HTTP + real LDChat/Goal/Runner/ProjectService "
                 "over a real SQLite ledger; scripted LD decision model; stub "
                 "Worker adapter"),
        "checks": checks,
        "commands": {"pytest": pytest_run, "javascript_syntax": node_check},
        "ledger": {"path_relative": str(ledger.relative_to(ROOT)),
                   "sha256": digest(ledger) if ledger.is_file() else None},
        "forwarding_chain": [
            "console POST /api/chat/publish",
            "LDChat.publish_goal -> one Goal row (deterministic goal_id)",
            "LDGoalRunner.advance_due -> wakes the LD",
            "LD decision -> LDTools.submit_project_task -> ControlPlane",
            "WorkItem.metadata.ld_goal_ref is the only Goal/Task link",
            "GET /api/projects/{id} projects that link back to the console",
        ],
        "real_llm_ld_decision": "NOT_RUN" if model_ready else "BLOCKED_ENVIRONMENT",
        "real_llm_environment": {
            "base_url_configured": bool(os.environ.get("LD_MODEL_BASE_URL")),
            "model_name_configured": bool(os.environ.get("LD_MODEL_NAME")),
            "domains_configured": bool(os.environ.get("LD_MODEL_DOMAINS")),
            "note": ("No endpoint URL, model id or key is written here. A "
                     "scripted decision model proves the forwarding path, not "
                     "an LLM's judgment."),
        },
        "not_covered": [
            "real LLM LD decision quality",
            "real CodeBuddy Worker execution and Verifier verdict",
            "browser visual walkthrough (separate GUI step)",
        ],
        "result": "Pass" if passed else "Fail",
    }
    write_evidence(EVIDENCE, evidence)
    print(json.dumps({"result": evidence["result"],
                      "failed_checks": [name for name, value in checks.items()
                                        if not value],
                      "pytest_exit": pytest_run["exit_code"],
                      "real_llm_ld_decision": evidence["real_llm_ld_decision"],
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
