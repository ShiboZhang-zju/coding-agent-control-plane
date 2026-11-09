"""UI-4 pre-deployment guards, round 2: confirmation and honest intent.

Two claims, both about *not* doing things:

1. **A global conversation always stages a ``submit_goal``.**  Even a sentence
   the deterministic router reads as an unambiguous command creates nothing
   until the human confirms the project name, project id, the *complete*
   objective and the goal id.  Confirming applies the frozen
   ``pending_action``; a second confirmation creates nothing new; cancelling,
   an expired/superseded confirmation, or a confirmation addressed to another
   project creates nothing and does not consume the pending action it did not
   match.

2. **A turn that was routed into a clarification is persisted as one.**  The
   HTTP body, the stored ``ld_messages.intent`` row and the audit line must all
   say ``clarify``; a turn that refused a write must not be recorded as
   ``submit_goal``.

Compatibility claims: a *project-bound* conversation keeps the historical
"an unambiguous imperative is applied" behaviour, and the structured
``/api/chat/publish`` form still creates the Goal directly without a chat
confirmation.

Every rejection re-reads SQLite (goal rows, goal status, pending_action,
WorkItem count).  Both routing paths are covered: the in-process router and a
fake ``intent_model``.
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

from adapters.base import ArtifactAssertion  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402

STATE = ROOT / ".poc-state" / "ui4-confirm"
TOKEN = "ui4-confirm-operator-token-32-char"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class _Model:
    """A classifier whose claim is whatever the test tells it to claim."""

    def __init__(self, intent: str = "submit_goal", goal: str = "实现 greet.py"):
        self.intent = intent
        self.goal = goal
        self.project_id: str | None = None
        self.calls = 0

    def classify(self, payload):
        self.calls += 1
        return {"intent": self.intent, "project_id": self.project_id,
                "task_id": None, "goal_id": None,
                "reason": "fake model claim",
                "parameters": {"goal": self.goal}}


class Harness:
    def __init__(self, *, intent_model=None, model_domains=None) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "confirm.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
        # A third project holds the pre-existing paused goal, so alpha and beta
        # start with **zero** goals: "nothing was created" is then a plain
        # `== []` read of SQLite rather than a comparison against a baseline.
        self.gamma = plane.register_project(
            "ACP-Gamma", str(_git_repo(root / "gamma")), default_adapter="stub")
        self.alpha_task = plane.enqueue(
            "alpha task", str(root / "alpha"),
            assertions=[ArtifactAssertion("file_exists", "a.md")],
            project_id=self.alpha["id"])["task_id"]
        self.beta_task = plane.enqueue(
            "beta task", str(root / "beta"),
            assertions=[ArtifactAssertion("file_exists", "b.md")],
            project_id=self.beta["id"])["task_id"]
        store.close()

        goals = LDGoalStore(self.ledger)
        self.paused_text = "gamma stopped goal"
        self.paused_id, paused_ref = goal_identity(
            self.paused_text, self.gamma["id"])
        goals.get_or_create(self.paused_id, self.gamma["id"],
                            self.paused_text, paused_ref)
        goals.ensure_lifetime_budget(self.paused_id, max_decisions=5,
                                     max_tasks=2)
        goals.update_goal(self.paused_id, status="needs_human",
                          last_decision={"stop": "human review required"})
        goals.close()

        self.model = intent_model
        bridge = ChatBridge(self.ledger, intent_model=intent_model,
                            model_domains=model_domains)
        self.server = make_server(self.ledger, host="127.0.0.1", port=0,
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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, path, *, method="GET", payload=None):
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json",
                   "Authorization": "Bearer " + TOKEN}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = self.base
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def say(self, message: str, conversation_id: str | None = None,
            project_id: str | None = None):
        return self._request("/api/chat/messages", method="POST", payload={
            "conversation_id": conversation_id, "project_id": project_id,
            "message": message})

    def publish(self, project_id: str, objective: str):
        return self._request("/api/chat/publish", method="POST", payload={
            "project_id": project_id, "objective": objective})

    # -- ledger readers -------------------------------------------------- #
    def goals_of(self, project_id: str) -> list[dict]:
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            return goals.find_by_project(project_id)
        finally:
            goals.close()

    def goal(self, goal_id: str) -> dict:
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            return goals.get(goal_id) or {}
        finally:
            goals.close()

    def work_items(self, project_id: str) -> list[dict]:
        store = TaskStore(self.ledger, read_only=True)
        try:
            return store.work_items_for_project(project_id)
        finally:
            store.close()

    def pending(self, conversation_id: str) -> dict | None:
        chats = LDChatStore(self.ledger)
        try:
            row = chats.get_conversation(conversation_id) or {}
        finally:
            chats.close()
        return row.get("pending_action")

    def stored_intents(self, conversation_id: str) -> list[str]:
        """The intent actually persisted for each non-user message."""
        chats = LDChatStore(self.ledger)
        try:
            rows = chats.messages(conversation_id, limit=200)
        finally:
            chats.close()
        out = []
        for row in rows:
            if row.get("role") == "user":
                continue
            intent = row.get("intent") or {}
            if isinstance(intent, str):
                intent = json.loads(intent or "{}")
            out.append(str(intent.get("intent") or "?"))
        return out


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


# --------------------------------------------------------------------------- #
# 1. global chat: every submit_goal is staged
# --------------------------------------------------------------------------- #
def test_global_command_creates_nothing_until_confirmed(harness: Harness):
    """An unmistakable imperative still has to be confirmed in a global chat."""
    status, body = harness.say(
        f"继续推进 {harness.alpha['id']}：实现 greet.py 并补充测试")
    assert status == 200, body
    assert body["intent"]["intent"] == "submit_goal"
    assert body["project_routing"]["project_id"] == harness.alpha["id"]
    assert body["staged_for_confirmation"] is True
    assert body["requires_confirmation"] is True
    pending = body["pending_action"]
    assert pending["kind"] == "submit_goal"
    assert pending["project_id"] == harness.alpha["id"]
    # The *frozen* payload in SQLite carries the complete objective; the HTTP
    # projection carries the identifiers the console renders.
    stored = harness.pending(body["conversation_id"]) or {}
    assert stored["goal_text"] == "实现 greet.py 并补充测试"
    assert stored["goal_id"] == pending["goal_id"]

    # Everything the human must be able to check is in the answer, verbatim.
    assert "ACP-Alpha" in body["answer"]
    assert harness.alpha["id"] in body["answer"]
    assert "实现 greet.py 并补充测试" in body["answer"]
    assert pending["goal_id"] in body["answer"]

    # Nothing exists yet — checked in SQLite, not in the answer.
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.goals_of(harness.beta["id"]) == []
    assert len(harness.work_items(harness.alpha["id"])) == 1  # the seeded task
    assert harness.pending(body["conversation_id"]) is not None

    status, confirmed = harness.say("confirm", body["conversation_id"])
    assert status == 200, confirmed
    assert confirmed["intent_source"] == "human_confirmation"
    assert confirmed["goal_ids"] == [pending["goal_id"]]
    goals = harness.goals_of(harness.alpha["id"])
    assert [goal["goal_id"] for goal in goals] == [pending["goal_id"]]
    assert goals[0]["goal_text"] == "实现 greet.py 并补充测试"
    assert harness.goals_of(harness.beta["id"]) == []
    assert len(harness.work_items(harness.alpha["id"])) == 1
    assert harness.pending(body["conversation_id"]) is None


def test_global_command_with_a_model_still_needs_confirmation():
    """The model path stages too: staging is not about *who* classified."""
    model = _Model(goal="实现 greet.py")
    harness = Harness(intent_model=model, model_domains="local")
    model.project_id = harness.alpha["id"]
    try:
        status, body = harness.say(
            f"继续推进 {harness.alpha['id']}：实现 greet.py")
        assert status == 200, body
        assert body["requires_confirmation"] is True
        assert harness.goals_of(harness.alpha["id"]) == []
        assert model.calls == 1

        status, confirmed = harness.say("confirm", body["conversation_id"])
        assert status == 200, confirmed
        assert len(harness.goals_of(harness.alpha["id"])) == 1
    finally:
        harness.close()


def test_repeated_confirmation_creates_only_one_goal(harness: Harness):
    _, staged = harness.say(f"继续推进 {harness.alpha['id']}：实现 greet.py")
    conversation_id = staged["conversation_id"]
    goal_id = staged["pending_action"]["goal_id"]

    _, first = harness.say("confirm", conversation_id)
    assert first["goal_ids"] == [goal_id]
    created_at = harness.goal(goal_id)["created_at"]
    assert len(harness.goals_of(harness.alpha["id"])) == 1

    # A second confirmation has nothing to consume: it is an ordinary new turn,
    # and — whatever it is classified as — it must create no goal and start no
    # new execution.
    status, second = harness.say("confirm", conversation_id)
    assert status == 200, second
    assert goal_id not in (second["goal_ids"] or [])
    assert second["intent_source"] != "human_confirmation", second
    assert [goal["goal_id"] for goal in
            harness.goals_of(harness.alpha["id"])] == [goal_id]
    assert harness.goal(goal_id)["created_at"] == created_at
    assert harness.goal(goal_id)["decision_count"] == 0
    # No dispatch: the seeded WorkItem is still the only one.
    assert len(harness.work_items(harness.alpha["id"])) == 1


def test_cancel_creates_nothing_and_clears_the_stage(harness: Harness):
    _, staged = harness.say(f"继续推进 {harness.alpha['id']}：实现 greet.py")
    conversation_id = staged["conversation_id"]
    status, cancelled = harness.say("cancel", conversation_id)
    assert status == 200, cancelled
    assert cancelled["intent_source"] == "human_confirmation_denied"
    assert cancelled["facts"]["cancelled"] is True
    # The id in goal_ids is the *declined* one, never a created goal.
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.pending(conversation_id) is None

    # Confirming after a cancel cannot resurrect the stage.
    _, after = harness.say("confirm", conversation_id)
    assert after["intent_source"] != "human_confirmation", after
    assert harness.goals_of(harness.alpha["id"]) == []


def test_superseded_confirmation_creates_nothing(harness: Harness):
    _, staged = harness.say(f"继续推进 {harness.alpha['id']}：实现 greet.py")
    conversation_id = staged["conversation_id"]
    # Any other sentence supersedes the stage; the confirmation that follows
    # must not create the abandoned goal.
    _, other = harness.say("现在有哪些项目？", conversation_id)
    assert other["intent"]["intent"] == "list_projects"
    assert harness.pending(conversation_id) is None
    _, late = harness.say("confirm", conversation_id)
    assert late["goal_ids"] == []
    assert harness.goals_of(harness.alpha["id"]) == []


def test_confirmation_for_another_project_writes_nothing(harness: Harness):
    _ = harness
    model = _Model(goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.alpha["id"]
    try:
        _, staged = harness2.say(
            f"继续推进 {harness2.alpha['id']}：实现 greet.py")
        conversation_id = staged["conversation_id"]
        assert harness2.pending(conversation_id) is not None

        status, mismatch = harness2.say(
            "confirm", conversation_id, project_id=harness2.beta["id"])
        assert status == 200, mismatch
        assert mismatch["intent_source"] == "human_confirmation_project_mismatch"
        assert mismatch["goal_ids"] == []
        assert harness2.goals_of(harness2.alpha["id"]) == []
        assert harness2.goals_of(harness2.beta["id"]) == []
        # The stage survives: it was not consumed by a mismatched confirmation.
        kept = harness2.pending(conversation_id)
        assert kept is not None
        assert kept["kind"] == "submit_goal"
        assert kept["project_id"] == harness2.alpha["id"]

        # The correct confirmation still works and creates exactly one goal.
        _, confirmed = harness2.say("confirm", conversation_id)
        assert confirmed["goal_ids"] == [staged["pending_action"]["goal_id"]]
        assert len(harness2.goals_of(harness2.alpha["id"])) == 1
        assert harness2.goals_of(harness2.beta["id"]) == []
    finally:
        harness2.close()


# --------------------------------------------------------------------------- #
# 2. a routed-away write is persisted as clarify
# --------------------------------------------------------------------------- #
def test_refused_write_is_clarify_in_body_history_and_audit(harness: Harness):
    # A model claims a valid project; the sentence names none.  The turn is a
    # clarification, so nothing may record it as a submit_goal.
    model = _Model(goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.beta["id"]
    try:
        status, body = harness2.say("把这个功能做完")
        assert status == 200, body
        assert body["intent"]["intent"] == "clarify", body
        assert body["goal_ids"] == []
        assert harness2.stored_intents(body["conversation_id"])[-1] == "clarify"
        assert harness2.goals_of(harness2.beta["id"]) == []
        assert harness2.goals_of(harness2.alpha["id"]) == []
        assert harness2.pending(body["conversation_id"]) is None
    finally:
        harness2.close()


def test_ambiguous_write_is_clarify_in_history(harness: Harness):
    status, body = harness.say(
        f"继续推进 {harness.alpha['id']} 和 {harness.beta['id']}")
    assert status == 200, body
    assert body["intent"]["intent"] == "clarify"
    assert harness.stored_intents(body["conversation_id"])[-1] == "clarify"
    assert harness.goals_of(harness.alpha["id"]) == []
    assert harness.goals_of(harness.beta["id"]) == []


def test_audit_line_reports_the_effective_intent(harness: Harness):
    """The audit event must not claim a write that the routing refused.

    The audit line is written to stderr by the HTTP layer, so this captures the
    real stream instead of reading the response body back.
    """
    import contextlib
    import io

    model = _Model(goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.beta["id"]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            status, body = harness2.say("把这个功能做完")
        assert status == 200, body
        assert body["intent"]["intent"] == "clarify"
    finally:
        harness2.close()

    audits = [json.loads(line.split("[dashboard.audit] ", 1)[1])
              for line in captured.getvalue().splitlines()
              if "[dashboard.audit]" in line]
    chat_audits = [item for item in audits if item.get("event") == "chat_turn"]
    assert chat_audits, "no chat_turn audit line was written"
    assert chat_audits[-1]["intent"] == "clarify", chat_audits[-1]
    assert chat_audits[-1]["goal_count"] == 0


# --------------------------------------------------------------------------- #
# compatibility
# --------------------------------------------------------------------------- #
def test_project_bound_conversation_still_applies_an_imperative(harness: Harness):
    """Historical behaviour: a bound conversation does not stage a clear order."""
    status, body = harness.say(
        "继续推进：实现 greet.py", project_id=harness.alpha["id"])
    assert status == 200, body
    assert body["intent"]["intent"] == "submit_goal"
    assert body["requires_confirmation"] is False
    assert len(body["goal_ids"]) == 1
    goals = harness.goals_of(harness.alpha["id"])
    assert body["goal_ids"][0] in [goal["goal_id"] for goal in goals]
    assert harness.pending(body["conversation_id"]) is None


def test_structured_publish_still_creates_the_goal_directly(harness: Harness):
    status, body = harness.publish(harness.beta["id"], "补充 beta 的测试")
    assert status == 200, body
    goal_id = body["goal_id"]
    assert body["project_id"] == harness.beta["id"]
    goals = harness.goals_of(harness.beta["id"])
    assert [goal["goal_id"] for goal in goals] == [goal_id]
    assert harness.goals_of(harness.alpha["id"]) == []
    # Publication creates a Goal and nothing else: no WorkItem, no runner claim.
    assert len(harness.work_items(harness.beta["id"])) == 1
