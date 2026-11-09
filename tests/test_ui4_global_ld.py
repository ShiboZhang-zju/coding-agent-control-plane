"""UI-4: the single global LD as the dashboard's default entry point.

What is under test
------------------
Not "a chat endpoint exists".  The claims are:

1. a chat turn can be sent **without a project** and answered from the ledger;
2. one *global* conversation can discuss project A and then project B under the
   same ``conversation_id`` — the row stays unbound, so "we talked about A" never
   becomes "this conversation belongs to A";
3. a write (``submit_goal``) lands on exactly the project that was named, and a
   turn that does not identify one creates **nothing**;
4. an ambiguous turn (two projects in one sentence) creates nothing either;
5. a *project-bound* conversation keeps its isolation: it cannot be switched by
   a sentence, and omitting ``project_id`` is not a way out of it;
6. the routing decision is reported as data (``project_routing``), so the UI can
   show "identified project" without inventing it.

Everything runs against a real ``LDChat`` / ``LDGoalStore`` / ``TaskStore`` over
a real loopback HTTP server, with a throwaway ledger under ``.poc-state/``.  No
production Manager, no production ledger, no Worker.

These are not tautologies: each assertion reads back SQLite (goals per project,
the conversation row) rather than trusting the answer string, and the negative
cases assert an *empty* result.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore  # noqa: E402

STATE = ROOT / ".poc-state" / "ui4-global"
TOKEN = "ui4-global-operator-token-32-chars"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Harness:
    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui4.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
        store.close()

        bridge = ChatBridge(self.ledger)
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
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, dict(response.headers), json.loads(
                    response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def say(self, message: str, conversation_id: str | None = None,
            project_id: str | None = None, **kwargs):
        """One turn through the real HTTP chat route."""
        return self.request(
            "/api/chat/messages", method="POST",
            payload={"conversation_id": conversation_id,
                     "project_id": project_id, "message": message}, **kwargs)

    # -- ledger readers (never trust the answer text) -------------------- #
    def goals_of(self, project_id: str) -> list[dict]:
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            return goals.find_by_project(project_id)
        finally:
            goals.close()

    def conversation_row(self, conversation_id: str) -> dict:
        chats = LDChatStore(self.ledger)
        try:
            row = chats.get_conversation(conversation_id)
        finally:
            chats.close()
        assert row is not None, "the conversation must be persisted"
        return row

    def work_items(self, project_id: str) -> list[dict]:
        store = TaskStore(self.ledger, read_only=True)
        try:
            return store.work_items_for_project(project_id)
        finally:
            store.close()


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


# --------------------------------------------------------------------------- #
# 1 + 2: global conversation, no project required, project switch keeps the id
# --------------------------------------------------------------------------- #
def test_global_chat_needs_no_project_and_reads_the_ledger(harness: Harness):
    status, _, turn = harness.say("现在有哪些项目？")
    assert status == 200, turn
    assert turn["intent"]["intent"] == "list_projects"
    # The summary is derived from the ledger: both registered projects appear
    # with their ids, and no project was picked for the turn.
    assert turn["project_routing"]["source"] == "none"
    assert "project_id" not in turn["project_routing"]
    for project in (harness.alpha, harness.beta):
        assert project["id"] in turn["answer"]
    assert harness.conversation_row(turn["conversation_id"])["project_id"] is None


def test_one_global_conversation_switches_projects_without_rebinding(
        harness: Harness):
    _, _, first = harness.say(f"{harness.alpha['id']} 现在进行到哪了？")
    conversation_id = first["conversation_id"]
    assert first["intent"]["intent"] == "project_status"
    assert first["project_routing"] == {"source": "named",
                                       "project_id": harness.alpha["id"]}

    status, _, second = harness.say(
        f"{harness.beta['id']} 有哪些阻塞？", conversation_id)
    assert status == 200, second
    assert second["conversation_id"] == conversation_id
    assert second["intent"]["intent"] == "explain_blockers"
    assert second["project_routing"]["project_id"] == harness.beta["id"]

    # The conversation still has ONE id and no project binding: mentioning a
    # project did not move it.
    row = harness.conversation_row(conversation_id)
    assert row["project_id"] is None
    _, _, history = harness.request(
        "/api/chat/conversations/" + conversation_id)
    assert history["conversation"]["project_id"] is None
    assert [row["role"] for row in history["messages"]] == [
        "user", "ld", "user", "ld"]


def test_project_name_also_routes(harness: Harness):
    _, _, turn = harness.say("ACP-Beta 的目标现在怎么样了？")
    assert turn["project_routing"] == {"source": "named",
                                      "project_id": harness.beta["id"]}
    assert turn["intent"]["intent"] == "goal_status"


# --------------------------------------------------------------------------- #
# 3: a write lands on exactly the named project
# --------------------------------------------------------------------------- #
def test_named_write_creates_a_goal_for_that_project_only(harness: Harness):
    _, _, turn = harness.say(
        f"继续推进 {harness.alpha['id']}：实现 greet.py 并补充测试")
    # In a global conversation every submit_goal is staged, even when the
    # routing is unambiguous: the operator confirms the project and objective.
    assert turn["intent"]["intent"] == "submit_goal"
    assert turn["project_routing"]["project_id"] == harness.alpha["id"]
    assert turn["requires_confirmation"] is True
    assert turn["goal_ids"] == []
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.work_items(harness.alpha["id"]) == []

    _, _, confirmed = harness.say("confirm", turn["conversation_id"])
    assert confirmed["goal_ids"] == [turn["pending_action"]["goal_id"]]

    alpha_goals = harness.goals_of(harness.alpha["id"])
    assert [goal["goal_id"] for goal in alpha_goals] == confirmed["goal_ids"]
    assert harness.goals_of(harness.beta["id"]) == []
    # A Goal is all that was written: no WorkItem is submitted from chat.
    assert harness.work_items(harness.alpha["id"]) == []
    assert harness.work_items(harness.beta["id"]) == []


def test_bound_conversation_write_stays_in_its_project(harness: Harness):
    # Creating a conversation *with* a project still binds it (historical rule).
    _, _, turn = harness.say("继续推进：实现 greet.py", project_id=harness.beta["id"])
    assert turn["intent"]["intent"] == "submit_goal"
    assert turn["project_routing"] == {"source": "bound",
                                      "project_id": harness.beta["id"]}
    assert harness.conversation_row(turn["conversation_id"])["project_id"] \
        == harness.beta["id"]
    assert harness.goals_of(harness.beta["id"]) != []
    assert harness.goals_of(harness.alpha["id"]) == []


# --------------------------------------------------------------------------- #
# 4: no project identified -> no Goal, no WorkItem
# --------------------------------------------------------------------------- #
def test_write_without_a_project_creates_nothing(harness: Harness):
    status, _, turn = harness.say("继续推进：把这个功能做完")
    assert status == 200, turn
    assert turn["intent"]["intent"] == "clarify"
    assert turn["goal_ids"] == []
    assert turn["requires_confirmation"] is False
    assert turn["project_routing"]["source"] == "none"
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.goals_of(harness.beta["id"]) == []
    assert harness.work_items(harness.alpha["id"]) == []
    assert harness.work_items(harness.beta["id"]) == []
    # The question names the candidates instead of silently choosing one.
    assert harness.alpha["id"] in turn["answer"]
    assert harness.beta["id"] in turn["answer"]


def test_ambiguous_project_creates_nothing_and_lists_candidates(
        harness: Harness):
    status, _, turn = harness.say(
        f"继续推进 {harness.alpha['id']} 和 {harness.beta['id']} 两个项目")
    assert status == 200, turn
    assert turn["intent"]["intent"] == "clarify"
    routing = turn["project_routing"]
    assert routing["source"] == "ambiguous"
    assert "project_id" not in routing
    assert sorted(routing["candidates"]) == sorted(
        [harness.alpha["id"], harness.beta["id"]])
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.goals_of(harness.beta["id"]) == []


def test_demonstrative_reference_resolves_only_from_the_conversation(
        harness: Harness):
    # Nothing has been discussed yet: "这个项目" has no referent.
    _, _, orphan = harness.say("这个项目现在怎么样？")
    assert orphan["intent"]["intent"] == "clarify"
    assert orphan["project_routing"]["source"] == "ambiguous"

    # After discussing exactly one project, the reference resolves to it.
    _, _, first = harness.say(f"{harness.alpha['id']} 有哪些阻塞？")
    conversation_id = first["conversation_id"]
    _, _, second = harness.say("这个项目现在怎么样？", conversation_id)
    assert second["project_routing"] == {"source": "reference",
                                        "project_id": harness.alpha["id"]}
    assert second["intent"]["intent"] == "project_status"

    # Discussing B, then a reference, follows the latest topic — and still does
    # not write anything or rebind the conversation.
    _, _, third = harness.say(f"{harness.beta['id']} 有哪些阻塞？", conversation_id)
    assert third["project_routing"]["project_id"] == harness.beta["id"]
    _, _, fourth = harness.say("这个项目的目标呢？", conversation_id)
    assert fourth["project_routing"] == {"source": "reference",
                                        "project_id": harness.beta["id"]}
    assert harness.conversation_row(conversation_id)["project_id"] is None


# --------------------------------------------------------------------------- #
# 5: a bound conversation is still an isolation boundary
# --------------------------------------------------------------------------- #
def test_bound_conversation_cannot_be_switched_by_sentence_or_by_null(
        harness: Harness):
    _, _, first = harness.say(
        f"{harness.alpha['id']} 状态？", project_id=harness.alpha["id"])
    conversation_id = first["conversation_id"]
    assert harness.conversation_row(conversation_id)["project_id"] \
        == harness.alpha["id"]

    # (a) asking about another project *without* the field is refused, not
    # silently answered about alpha.
    status, _, body = harness.say(
        f"{harness.beta['id']} 有哪些阻塞？", conversation_id)
    assert status == 400 and body["error"]["code"] == "chat_request_rejected"
    assert harness.conversation_row(conversation_id)["project_id"] \
        == harness.alpha["id"]

    # (b) passing the other project id is refused by the bridge.
    status, _, body = harness.say(
        "状态？", conversation_id, project_id=harness.beta["id"])
    assert status == 409
    assert body["error"]["code"] == "conversation_project_mismatch"

    # (c) omitting the field does not unbind it either.
    status, _, body = harness.say("这个项目有哪些目标？", conversation_id)
    assert status == 200
    assert body["project_routing"] == {"source": "bound",
                                      "project_id": harness.alpha["id"]}
    assert harness.conversation_row(conversation_id)["project_id"] \
        == harness.alpha["id"]


# --------------------------------------------------------------------------- #
# schema / compatibility
# --------------------------------------------------------------------------- #
def test_null_project_is_accepted_but_the_field_must_be_present(
        harness: Harness):
    status, _, body = harness.request(
        "/api/chat/messages", method="POST",
        payload={"conversation_id": None, "message": "现在有哪些项目？"})
    assert status == 400 and body["error"]["code"] == "invalid_fields"

    # An empty string is not "global": it is a malformed project id.
    status, _, body = harness.request(
        "/api/chat/messages", method="POST",
        payload={"conversation_id": None, "project_id": "",
                 "message": "现在有哪些项目？"})
    assert status == 400 and body["error"]["code"] == "invalid_project"

    # An unknown project id is still an error, never "fall back to global".
    status, _, body = harness.request(
        "/api/chat/messages", method="POST",
        payload={"conversation_id": None, "project_id": "prj-does-not-exist",
                 "message": "现在有哪些项目？"})
    assert status == 400 and body["error"]["code"] == "invalid_reference"

    # The write schema is still a closed set of three keys.
    for extra in ({"intent": "submit_goal"}, {"goal_id": "goal-forged"},
                  {"max_decisions": 99}):
        payload = {"conversation_id": None, "project_id": None,
                   "message": "现在有哪些项目？"}
        payload.update(extra)
        status, _, body = harness.request(
            "/api/chat/messages", method="POST", payload=payload)
        assert status == 400 and body["error"]["code"] == "invalid_fields", extra
    assert harness.goals_of(harness.alpha["id"]) == []


def test_global_chat_still_requires_auth_and_same_origin(harness: Harness):
    status, _, body = harness.say("现在有哪些项目？", token=None)
    assert status == 401 and body["error"]["code"] == "unauthorized"
    status, _, body = harness.say("现在有哪些项目？",
                                  origin="https://evil.example")
    assert status == 403 and body["error"]["code"] == "bad_origin"


def test_conversation_list_is_global_and_includes_unbound_rows(
        harness: Harness):
    _, _, global_turn = harness.say("现在有哪些项目？")
    _, _, bound_turn = harness.say("状态？", project_id=harness.alpha["id"])
    status, _, payload = harness.request("/api/chat/conversations?limit=100")
    assert status == 200
    ids = [row["conversation_id"] for row in payload["conversations"]]
    assert global_turn["conversation_id"] in ids
    assert bound_turn["conversation_id"] in ids
    unbound = next(row for row in payload["conversations"]
                   if row["conversation_id"] == global_turn["conversation_id"])
    assert unbound["project_id"] is None


def test_publish_route_still_requires_an_explicit_project(harness: Harness):
    """The global chat does not loosen the project-level publication path."""
    status, _, body = harness.request(
        "/api/chat/publish", method="POST",
        payload={"project_id": None, "objective": "实现 greet.py"})
    assert status == 400 and body["error"]["code"] == "invalid_project"
    assert harness.goals_of(harness.alpha["id"]) == []


def test_runtime_status_is_not_invented(harness: Harness):
    """No progress percentage, no runner liveness claim."""
    _, _, health = harness.request("/api/health", token=None, origin=None)
    assert health["chat"]["goal_runner_liveness"] == "unknown"
    _, _, turn = harness.say("现在有哪些项目？")
    lowered = turn["answer"].lower()
    for forbidden in ("%", "alive", "online", "存活"):
        assert forbidden not in lowered, turn["answer"]
