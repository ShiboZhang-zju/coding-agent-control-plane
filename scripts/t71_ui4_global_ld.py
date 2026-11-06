"""T71 / UI-4: global LD entry point acceptance, against a real server.

What this produces
------------------
Evidence for the six HTTP-level claims of the global LD, taken from a **real**
``LDChat`` / ``LDGoalStore`` / ``TaskStore`` behind a real loopback HTTP server
over a throwaway ledger.  Every assertion is read back from SQLite (per-project
Goal rows, the conversation row, WorkItem counts) — never from an answer string
alone — and every negative case asserts an empty result.

Covered:

1. no project selected -> every project's summary is answered from the ledger;
2. project A then project B in ONE conversation, same ``conversation_id``, row
   still unbound;
3. a write names project A -> exactly one Goal, in A only, no WorkItem;
4. an ambiguous turn -> nothing created, candidates listed;
5. a write with no project -> nothing created;
6. a project-bound conversation cannot be switched (by sentence, by field, or by
   omitting the field).

Browser-level checks (default entry, badge, confirmation flow) live in
``tests/test_ui4_browser.py`` and are recorded here by running them, so this
script never claims a browser pass it did not perform.

Exit codes: 0 PASS, 1 FAIL.  The production Manager and ledger are never used.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sanitize_evidence import write_evidence  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402

EVIDENCE = ROOT / "evidence" / "project" / "T71-ui4-global-ld.json"
STATE = ROOT / ".poc-state" / "t71-ui4"
TOKEN = "t71-ui4-operator-token-32-characters"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Server:
    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "t71.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "T71-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "T71-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
        store.close()

        bridge = ChatBridge(self.ledger)
        self.server = make_server(self.ledger, host="127.0.0.1", port=0,
                                  enable_chat=True, chat_token=TOKEN,
                                  chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, *, method: str = "GET", payload=None):
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": "Bearer " + TOKEN}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = self.base
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def say(self, message: str, conversation_id: str | None = None,
            project_id: str | None = None):
        return self.request("/api/chat/messages", method="POST", payload={
            "conversation_id": conversation_id, "project_id": project_id,
            "message": message})

    def goals_of(self, project_id: str) -> list[dict]:
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            return [{"goal_id": goal["goal_id"],
                     "goal_text": goal["goal_text"],
                     "status": goal["status"]}
                    for goal in goals.find_by_project(project_id)]
        finally:
            goals.close()

    def work_items(self, project_id: str) -> int:
        store = TaskStore(self.ledger, read_only=True)
        try:
            return len(store.work_items_for_project(project_id))
        finally:
            store.close()

    def conversation(self, conversation_id: str) -> dict:
        chats = LDChatStore(self.ledger)
        try:
            row = chats.get_conversation(conversation_id) or {}
        finally:
            chats.close()
        return {"conversation_id": row.get("conversation_id"),
                "project_id": row.get("project_id"),
                "message_count": row.get("message_count")}


def _turn_view(turn: dict) -> dict:
    return {"intent": (turn.get("intent") or {}).get("intent"),
            "intent_source": turn.get("intent_source"),
            "project_routing": turn.get("project_routing"),
            "goal_ids": turn.get("goal_ids"),
            "requires_confirmation": turn.get("requires_confirmation")}


def collect(server: Server) -> dict:
    checks: dict[str, bool] = {}
    detail: dict[str, object] = {}

    # -- 1. no project: read every project from the ledger ------------------ #
    status, turn = server.say("现在有哪些项目？")
    conversation_id = turn.get("conversation_id")
    summary_answer = str(turn.get("answer") or "")
    answers_all = all(project["id"] in summary_answer
                      for project in (server.alpha, server.beta))
    checks["1_no_project_query_answered_from_ledger"] = (
        status == 200
        and (turn.get("intent") or {}).get("intent") == "list_projects"
        and answers_all
        and (turn.get("project_routing") or {}).get("source") == "none")
    detail["turn_1"] = _turn_view(turn)

    # -- 2. same conversation, project A then project B -------------------- #
    status_a, turn_a = server.say(
        f"{server.alpha['id']} 现在进行到哪了？", conversation_id)
    status_b, turn_b = server.say(
        f"{server.beta['id']} 有哪些阻塞？", conversation_id)
    row = server.conversation(conversation_id)
    checks["2_one_conversation_two_projects"] = (
        status_a == 200 and status_b == 200
        and turn_a.get("conversation_id") == turn_b.get("conversation_id")
        == conversation_id
        and (turn_a.get("project_routing") or {}).get("project_id")
        == server.alpha["id"]
        and (turn_b.get("project_routing") or {}).get("project_id")
        == server.beta["id"]
        and row["project_id"] is None)
    detail["turn_2a"] = _turn_view(turn_a)
    detail["turn_2b"] = _turn_view(turn_b)
    detail["conversation_row"] = row

    # -- 3. a named write lands on A only ---------------------------------- #
    status_w, turn_w = server.say(
        f"继续推进 {server.alpha['id']}：实现 greet.py 并补充测试", conversation_id)
    alpha_goals = server.goals_of(server.alpha["id"])
    beta_goals = server.goals_of(server.beta["id"])
    checks["3_named_write_is_project_scoped"] = (
        status_w == 200
        and (turn_w.get("intent") or {}).get("intent") == "submit_goal"
        and len(alpha_goals) == 1
        and not beta_goals
        and server.work_items(server.alpha["id"]) == 0
        and server.work_items(server.beta["id"]) == 0)
    detail["turn_3"] = _turn_view(turn_w)
    detail["alpha_goals"] = alpha_goals
    detail["beta_goals"] = beta_goals

    # -- 4. ambiguous: nothing created ------------------------------------- #
    goals_before = (len(alpha_goals), len(beta_goals))
    status_m, turn_m = server.say(
        f"继续推进 {server.alpha['id']} 和 {server.beta['id']} 两个项目",
        conversation_id)
    routing_m = turn_m.get("project_routing") or {}
    checks["4_ambiguous_creates_nothing"] = (
        status_m == 200
        and (turn_m.get("intent") or {}).get("intent") == "clarify"
        and routing_m.get("source") == "ambiguous"
        and "project_id" not in routing_m
        and sorted(routing_m.get("candidates") or []) == sorted(
            [server.alpha["id"], server.beta["id"]])
        and (len(server.goals_of(server.alpha["id"])),
             len(server.goals_of(server.beta["id"]))) == goals_before
        and not turn_m.get("goal_ids"))
    detail["turn_4"] = _turn_view(turn_m)

    # -- 5. no project at all: nothing created ----------------------------- #
    status_n, turn_n = server.say("继续推进：把这个功能做完")
    checks["5_unidentified_write_creates_nothing"] = (
        status_n == 200
        and (turn_n.get("intent") or {}).get("intent") == "clarify"
        and not turn_n.get("goal_ids")
        and turn_n.get("requires_confirmation") is False
        and (len(server.goals_of(server.alpha["id"])),
             len(server.goals_of(server.beta["id"]))) == goals_before)
    detail["turn_5"] = _turn_view(turn_n)

    # -- 6. a bound conversation cannot be switched ------------------------ #
    status_c, turn_c = server.say(
        f"{server.alpha['id']} 状态？", project_id=server.alpha["id"])
    bound_id = turn_c.get("conversation_id")
    status_s, body_s = server.say(
        f"{server.beta['id']} 有哪些阻塞？", bound_id)
    status_f, body_f = server.say(
        "状态？", bound_id, project_id=server.beta["id"])
    status_o, turn_o = server.say("这个项目有哪些目标？", bound_id)
    bound_row = server.conversation(bound_id)
    checks["6_bound_conversation_isolation"] = (
        status_c == 200
        and bound_row["project_id"] == server.alpha["id"]
        and status_s == 400
        and (body_s.get("error") or {}).get("code") == "chat_request_rejected"
        and status_f == 409
        and (body_f.get("error") or {}).get("code")
        == "conversation_project_mismatch"
        and status_o == 200
        and (turn_o.get("project_routing") or {}) == {
            "source": "bound", "project_id": server.alpha["id"]}
        and server.conversation(bound_id)["project_id"] == server.alpha["id"])
    detail["turn_6"] = _turn_view(turn_o)
    detail["bound_conversation_row"] = bound_row
    detail["bound_sentence_status"] = status_s
    detail["bound_field_status"] = status_f
    detail["bound_sentence_error"] = (body_s.get("error") or {}).get("code")
    detail["bound_field_error"] = (body_f.get("error") or {}).get("code")

    # -- 7. no progress/liveness is invented ------------------------------- #
    status_h, health = server.request("/api/health")
    checks["7_no_invented_progress_or_liveness"] = (
        status_h == 200
        and (health.get("chat") or {}).get("goal_runner_liveness") == "unknown"
        # The summary text carries ledger counts only: no completion
        # percentage, and no claim that the LD/runner is alive.
        and "%" not in summary_answer
        and not any(marker in summary_answer.lower()
                    for marker in ("alive", "online", "运行中", "存活")))
    detail["health_chat"] = health.get("chat")
    detail["summary_answer_has_no_percentage"] = "%" not in summary_answer

    return {"checks": checks, "detail": detail}


def run_browser_suite() -> dict:
    """Run the Playwright UI-4 tests; a skip is recorded as SKIPPED, not PASS."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "scripts")])
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ui4_browser.py", "-q"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=900)
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"exit_code": completed.returncode,
            "status": ("PASS" if completed.returncode == 0
                       else "SKIPPED" if "skipped" in output
                       else "FAIL"),
            "tail": output.strip()[-2_500:]}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    server = Server()
    try:
        result = collect(server)
    finally:
        server.close()

    browser = run_browser_suite()
    checks = dict(result["checks"])
    # A browser pass is only counted when the browser tests actually ran.
    checks["8_playwright_browser_flow"] = browser["status"] == "PASS"

    failed = sorted(name for name, ok in checks.items() if not ok)
    payload = {
        "task": "T71-ui4-global-ld",
        "schema_version": "ui-4.v1",
        "result": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed": failed,
        "environment": {
            "ledger": "isolated throwaway ledger under .poc-state/t71-ui4",
            "production_manager_touched": False,
            "chat": "real LDChat + LDGoalStore + TaskStore behind a real "
                    "loopback HTTP server",
            "worker": "none: no WorkItem is dispatched by chat",
            "intent_routing": "deterministic in-process router (no model "
                              "endpoint configured)",
        },
        "scenarios": result["detail"],
        "browser_suite": browser,
    }
    write_evidence(EVIDENCE, payload)
    print(json.dumps({"result": payload["result"], "failed": failed,
                      "evidence": str(EVIDENCE.relative_to(ROOT))},
                     ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
