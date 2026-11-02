"""UI-1 Web LD Interaction acceptance and security tests.

These tests use the real LDChat/LDGoalStore/ProjectService stack behind a real
loopback HTTP server.  There is no Worker and no decision model; Goal creation
is real, while automatic advancement is intentionally reported as unknown.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge, MAX_MESSAGE_CHARS  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402


STATE = ROOT / ".poc-state" / "ui1-tests"
TOKEN = "ui1-test-operator-token-32-characters"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Harness:
    def __init__(self, *, intent_model=None, model_domains=None,
                 model_api_key=None):
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui1.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ui1-alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ui1-beta", str(_git_repo(root / "beta")), default_adapter="stub")
        store.close()

        goals = LDGoalStore(self.ledger)
        goal_text = "repair the stopped beta goal"
        self.paused_goal_id, goal_ref = goal_identity(goal_text, self.beta["id"])
        goals.get_or_create(
            self.paused_goal_id, self.beta["id"], goal_text, goal_ref)
        goals.ensure_lifetime_budget(
            self.paused_goal_id, max_decisions=7, max_tasks=2)
        goals.update_goal(
            self.paused_goal_id, status="needs_human",
            last_decision={"stop": "human review required"})
        goals.close()

        bridge = ChatBridge(
            self.ledger,
            intent_model=intent_model,
            model_domains=model_domains,
            model_api_key=model_api_key,
        )
        self.server = make_server(
            self.ledger, host="127.0.0.1", port=0,
            enable_chat=True, chat_token=TOKEN, chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, *, method: str = "GET", payload=None,
                token: str | None = TOKEN, origin: str | None = "same",
                extra_headers: dict | None = None) -> tuple[int, dict, dict]:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin == "same":
            headers["Origin"] = self.base
        elif origin is not None:
            headers["Origin"] = origin
        if payload is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), json.loads(
                    response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def message(self, project_id: str, message: str,
                conversation_id: str | None = None, **kwargs):
        return self.request(
            "/api/chat/messages", method="POST",
            payload={"conversation_id": conversation_id,
                     "project_id": project_id, "message": message}, **kwargs)


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


def _goal_rows(path: Path, project_id: str) -> list[dict]:
    goals = LDGoalStore(path, read_only=True)
    try:
        return goals.find_by_project(project_id)
    finally:
        goals.close()


def test_create_continue_persist_and_restore_conversation(harness: Harness):
    status, _, first = harness.message(
        harness.alpha["id"], "ui1-alpha 项目现在进行到哪里了？")
    assert status == 200
    assert first["intent"]["intent"] == "project_status"
    assert first["intent_source"] == "heuristic_router"
    conversation_id = first["conversation_id"]

    status, _, second = harness.message(
        harness.alpha["id"], "ui1-alpha 有哪些阻塞？", conversation_id)
    assert status == 200
    assert second["conversation_id"] == conversation_id
    assert second["intent"]["intent"] == "explain_blockers"

    status, _, history = harness.request(
        "/api/chat/conversations/" + conversation_id)
    assert status == 200
    assert history["conversation"]["project_id"] == harness.alpha["id"]
    assert [row["role"] for row in history["messages"]] == [
        "user", "ld", "user", "ld"]
    assert all(isinstance(row["created_at"], (int, float))
               for row in history["messages"])

    # A new bridge/process over the same ledger sees the same history.
    bridge = ChatBridge(harness.ledger)
    restored = bridge.get_conversation(conversation_id)
    assert restored is not None and len(restored["messages"]) == 4


def test_project_isolation_prevents_rebinding(harness: Harness):
    _, _, first = harness.message(harness.alpha["id"], "ui1-alpha 状态？")
    conversation_id = first["conversation_id"]
    status, _, body = harness.message(
        harness.beta["id"], "ui1-beta 状态？", conversation_id)
    assert status == 409
    assert body["error"]["code"] == "conversation_project_mismatch"
    _, _, history = harness.request("/api/chat/conversations/" + conversation_id)
    assert history["conversation"]["project_id"] == harness.alpha["id"]


def test_goal_creation_is_real_and_idempotent(harness: Harness):
    text = "继续推进 ui1-alpha 项目：实现 greet.py 并补充测试"
    status, _, first = harness.message(harness.alpha["id"], text)
    assert status == 200
    assert first["intent"]["intent"] == "submit_goal"
    assert len(first["goal_ids"]) == 1
    goal_id = first["goal_ids"][0]
    assert _goal_rows(harness.ledger, harness.alpha["id"])[0]["goal_id"] == goal_id

    status, _, second = harness.message(
        harness.alpha["id"], text, first["conversation_id"])
    assert status == 200 and second["goal_ids"] == [goal_id]
    rows = _goal_rows(harness.ledger, harness.alpha["id"])
    assert [row["goal_id"] for row in rows] == [goal_id]

    _, _, detail = harness.request(
        "/api/projects/" + harness.alpha["id"], token=None, origin=None)
    assert any(goal["goal_id"] == goal_id for goal in detail["goals"])


def test_no_model_fallback_and_runner_status_are_honest(harness: Harness):
    status, _, health = harness.request(
        "/api/health", token=None, origin=None)
    assert status == 200
    assert health["chat_enabled"] is True
    assert health["chat"]["intent_model_configured"] is False
    assert health["chat"]["intent_routing"] == "deterministic_local_fallback"
    assert health["chat"]["goal_runner_liveness"] == "unknown"
    assert health["chat"]["decision_model_runtime_status"] == "unknown"


class _InvalidIntentModel:
    def classify(self, _payload):
        return {"intent": "project_status"}  # required fields intentionally absent


def test_invalid_model_intent_fails_closed_to_clarify():
    harness = Harness(intent_model=_InvalidIntentModel(), model_domains="local")
    try:
        status, _, body = harness.message(
            harness.alpha["id"], "ui1-alpha 当前状态")
        assert status == 200
        assert body["intent"]["intent"] == "clarify"
        assert body["intent_source"] == "invalid_model_intent"
        assert _goal_rows(harness.ledger, harness.alpha["id"]) == []
    finally:
        harness.close()


def test_resume_requires_same_conversation_confirmation_and_preserves_budget(
        harness: Harness):
    before = _goal_rows(harness.ledger, harness.beta["id"])[0]
    status, _, staged = harness.message(
        harness.beta["id"], "恢复这个停止的 Goal")
    assert status == 200 and staged["requires_confirmation"] is True
    assert staged["pending_action"]["kind"] == "resume_goal"
    assert _goal_rows(harness.ledger, harness.beta["id"])[0]["status"] == "needs_human"

    # Confirmation in another conversation cannot consume this pending action.
    _, _, unrelated = harness.message(harness.beta["id"], "ui1-beta 状态？")
    status, _, stray = harness.message(
        harness.beta["id"], "confirm", unrelated["conversation_id"])
    assert status == 200
    assert stray["intent"]["intent"] != "resume_goal"
    assert _goal_rows(harness.ledger, harness.beta["id"])[0]["status"] == "needs_human"

    status, _, confirmed = harness.message(
        harness.beta["id"], "confirm", staged["conversation_id"])
    assert status == 200
    assert confirmed["intent_source"] == "human_confirmation"
    after = _goal_rows(harness.ledger, harness.beta["id"])[0]
    assert after["status"] == "active"
    assert after["max_decisions_total"] == before["max_decisions_total"] == 7
    assert after["max_tasks_total"] == before["max_tasks_total"] == 2
    assert after["resume_count"] == before["resume_count"] + 1


def test_cancel_leaves_goal_and_history_unchanged(harness: Harness):
    _, _, staged = harness.message(harness.beta["id"], "恢复这个停止的 Goal")
    status, _, cancelled = harness.message(
        harness.beta["id"], "cancel", staged["conversation_id"])
    assert status == 200
    assert cancelled["intent_source"] == "human_confirmation_denied"
    goal = _goal_rows(harness.ledger, harness.beta["id"])[0]
    assert goal["status"] == "needs_human"
    assert goal["resume_count"] == 0


def test_stale_resume_confirmation_is_refused(harness: Harness):
    _, _, staged = harness.message(harness.beta["id"], "恢复这个停止的 Goal")
    conn = sqlite3.connect(harness.ledger)
    conn.execute(
        "UPDATE ld_goals SET decision_count=decision_count+1, "
        "last_decision=? WHERE goal_id=?",
        (json.dumps({"stop": "a newer pause"}), harness.paused_goal_id))
    conn.commit()
    conn.close()

    status, _, result = harness.message(
        harness.beta["id"], "confirm", staged["conversation_id"])
    assert status == 200
    assert result["intent_source"] == "human_confirmation_stale"
    assert _goal_rows(harness.ledger, harness.beta["id"])[0]["status"] == "needs_human"


def test_concurrent_confirmation_applies_at_most_once(harness: Harness):
    _, _, staged = harness.message(harness.beta["id"], "恢复这个停止的 Goal")
    conversation_id = staged["conversation_id"]

    def confirm():
        return harness.message(harness.beta["id"], "confirm", conversation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(confirm), pool.submit(confirm)]]
    assert [item[0] for item in results] == [200, 200]
    sources = [item[2]["intent_source"] for item in results]
    assert sources.count("human_confirmation") == 1
    # Depending on which request reads the conversation before the atomic
    # consume, the loser either gets the explicit stale result or sees no
    # pending action and is routed as an ordinary read.  It must never apply.
    assert sum(source != "human_confirmation" for source in sources) == 1
    goal = _goal_rows(harness.ledger, harness.beta["id"])[0]
    assert goal["resume_count"] == 1
    assert goal["max_decisions_total"] == 7
    assert goal["max_tasks_total"] == 2


def test_default_ui0_still_rejects_all_writes(harness: Harness):
    server = make_server(harness.ledger, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/chat/messages", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_chat_auth_origin_and_schema_guards(harness: Harness):
    payload = {"conversation_id": None, "project_id": harness.alpha["id"],
               "message": "status"}
    status, headers, body = harness.request(
        "/api/chat/messages", method="POST", payload=payload, token=None)
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert "Access-Control-Allow-Origin" not in headers

    status, _, body = harness.request(
        "/api/chat/messages", method="POST", payload=payload,
        origin="https://evil.example")
    assert status == 403 and body["error"]["code"] == "bad_origin"

    bad = dict(payload, project_id="../../etc/passwd")
    status, _, body = harness.request(
        "/api/chat/messages", method="POST", payload=bad)
    assert status == 400 and body["error"]["code"] == "chat_request_rejected"

    bypass = dict(payload, intent="submit_goal", goal_id="goal-forged")
    status, _, body = harness.request(
        "/api/chat/messages", method="POST", payload=bypass)
    assert status == 400 and body["error"]["code"] == "invalid_fields"
    assert _goal_rows(harness.ledger, harness.alpha["id"]) == []


def test_oversize_request_and_message_are_rejected(harness: Harness):
    payload = {"conversation_id": None, "project_id": harness.alpha["id"],
               "message": "x" * 100_000}
    status, _, body = harness.request(
        "/api/chat/messages", method="POST", payload=payload)
    assert status == 413 and body["error"]["code"] == "body_too_large"

    bridge = ChatBridge(harness.ledger)
    with pytest.raises(ValueError):
        bridge.handle(message="x" * (MAX_MESSAGE_CHARS + 1),
                      conversation_id=None, project_id=harness.alpha["id"])


def test_xss_is_inert_text_and_secrets_not_exposed(harness: Harness):
    payload = '<img src=x onerror=alert("ui1-xss")> 这是什么？'
    status, _, turn = harness.message(harness.alpha["id"], payload)
    assert status == 200
    _, _, history = harness.request(
        "/api/chat/conversations/" + turn["conversation_id"])
    assert history["messages"][0]["content"] == payload

    app_js = (ROOT / "dashboard" / "static" / "app.js").read_text("utf-8")
    code_uses = [line.strip() for line in app_js.splitlines()
                 if "innerHTML" in line and not line.lstrip().startswith(("/*", "*", "//"))]
    assert code_uses == ['node.innerHTML = "";']
    blob = json.dumps(history, ensure_ascii=False)
    assert TOKEN not in blob
    assert "LD_MODEL_API_KEY" not in blob


def test_chat_get_requires_auth_and_returns_whitelisted_shape(harness: Harness):
    status, _, body = harness.request(
        "/api/chat/conversations", token=None, origin=None)
    assert status == 401 and body["error"]["code"] == "unauthorized"
    _, _, turn = harness.message(harness.alpha["id"], "ui1-alpha 状态？")
    status, _, detail = harness.request(
        "/api/chat/conversations/" + turn["conversation_id"])
    assert status == 200
    assert set(detail["conversation"]) == {
        "conversation_id", "project_id", "title", "message_count",
        "created_at", "updated_at", "requires_confirmation", "pending_action"}
    assert all(set(message) == {
        "seq", "role", "content", "intent", "goal_ids", "created_at"}
        for message in detail["messages"])
    assert "facts" not in json.dumps(detail, ensure_ascii=False)
