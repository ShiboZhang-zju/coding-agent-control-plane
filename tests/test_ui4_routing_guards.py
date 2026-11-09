"""UI-4 pre-deployment guards: the deterministic routing owns the project.

Four claims are checked, each with a *negative* that must hold in SQLite, not
just in the answer text:

1. **Model output cannot authorise a write.**  When deterministic routing says
   ``none`` or ``ambiguous``, a fake classifier returning a perfectly valid
   registered ``project_id`` must not create a Goal.  The same model claiming a
   *different* project than the one the sentence points at must be refused too,
   for writes and for reads.
2. **Deterministic clues are reconciled, not ordered.**  Two task ids from two
   projects, or a project name plus another project's task id, is ``ambiguous``.
3. **A recent ambiguous turn is not skipped over.**  After "A and B", the
   demonstrative "这个项目" must not fall back to an older A-only turn.
4. **A confirmation is bound to its project.**  Staging in A and confirming with
   B's ``project_id`` writes nothing and leaves the pending action intact.

Both paths are covered: the in-process router (no endpoint configured) and a
fake model (``intent_model=``) that is deliberately wrong.  Every rejection also
asserts that no Goal, no WorkItem and no ``needs_human`` -> resumed transition
happened, by re-reading the ledger.

No production Manager, ledger or Worker is touched.
"""
from __future__ import annotations

import sys
import threading
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

STATE = ROOT / ".poc-state" / "ui4-guards"
TOKEN = "ui4-guards-operator-token-32-char"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


#: A classifier that always claims the project it was told to claim.  It is a
#: *model*: it can say anything, and its output is never authority.
class _ClaimingModel:
    def __init__(self, intent: str, project_id: str, *, goal: str | None = None):
        self.intent = intent
        self.project_id = project_id
        self.goal = goal
        self.calls = 0

    def classify(self, payload):
        self.calls += 1
        # The full schema, so a rejection is about the *claim*, not about a
        # malformed object: task_id/goal_id/parameters are always present.
        return {"intent": self.intent, "project_id": self.project_id,
                "task_id": None, "goal_id": None,
                "reason": "fake model claim",
                "parameters": ({"goal": self.goal}
                               if self.goal is not None else {})}


class Harness:
    def __init__(self, *, intent_model=None, model_domains=None) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "guards.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
        # One task per project, so a sentence can mix a name and a foreign task
        # id without inventing anything.
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
        self.alpha_goal_text = "alpha stopped goal"
        self.alpha_goal_id, alpha_ref = goal_identity(
            self.alpha_goal_text, self.alpha["id"])
        goals.get_or_create(self.alpha_goal_id, self.alpha["id"],
                            self.alpha_goal_text, alpha_ref)
        goals.ensure_lifetime_budget(self.alpha_goal_id, max_decisions=5,
                                     max_tasks=2)
        goals.update_goal(self.alpha_goal_id, status="needs_human",
                          last_decision={"stop": "human review required"})
        self.beta_goal_text = "beta stopped goal"
        self.beta_goal_id, beta_ref = goal_identity(
            self.beta_goal_text, self.beta["id"])
        goals.get_or_create(self.beta_goal_id, self.beta["id"],
                            self.beta_goal_text, beta_ref)
        goals.ensure_lifetime_budget(self.beta_goal_id, max_decisions=5,
                                     max_tasks=2)
        goals.update_goal(self.beta_goal_id, status="needs_human",
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

    def say(self, message: str, conversation_id: str | None = None,
            project_id: str | None = None):
        import json
        import urllib.error
        import urllib.request
        payload = {"conversation_id": conversation_id,
                   "project_id": project_id, "message": message}
        request = urllib.request.Request(
            self.base + "/api/chat/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json",
                     "Authorization": "Bearer " + TOKEN,
                     "Origin": self.base,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

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

    def conversation(self, conversation_id: str) -> dict:
        chats = LDChatStore(self.ledger)
        try:
            return chats.get_conversation(conversation_id) or {}
        finally:
            chats.close()


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


def _seeded_goal_counts(harness: Harness) -> tuple[int, int]:
    return len(harness.goals_of(harness.alpha["id"])), \
        len(harness.goals_of(harness.beta["id"]))


def _paused(harness: Harness, goal_id: str) -> str:
    return str(harness.goal(goal_id).get("status"))


# --------------------------------------------------------------------------- #
# 1. model output cannot authorise a write
# --------------------------------------------------------------------------- #
def test_model_project_id_is_not_authority_when_routing_is_none():
    """route=none + a valid project id from the model -> nothing is created."""
    model = _ClaimingModel("submit_goal", "PLACEHOLDER", goal="实现 greet.py")
    harness = Harness(intent_model=model, model_domains="local")
    # A perfectly *valid, registered* project id: the claim is not malformed,
    # it is simply not evidence about what the sentence was about.
    model.project_id = harness.beta["id"]
    try:
        before = _seeded_goal_counts(harness)
        status, body = harness.say("把这个功能做完")
        assert status == 200, body
        assert body["project_routing"]["source"] == "none"
        assert "project_id" not in body["project_routing"]
        assert body["intent"]["intent"] == "clarify", body
        assert body["goal_ids"] == []
        assert body["requires_confirmation"] is False
        assert _seeded_goal_counts(harness) == before
        # Only the harness's own seed tasks exist; chat dispatched nothing.
        assert len(harness.work_items(harness.alpha["id"])) == 1
        assert len(harness.work_items(harness.beta["id"])) == 1
        # The refusal names the model's claim so the operator can see why.
        assert harness.beta["id"] in body["answer"]
    finally:
        harness.close()


def test_model_project_id_is_not_authority_when_routing_is_ambiguous():
    model = _ClaimingModel("submit_goal", "PLACEHOLDER", goal="实现 greet.py")
    harness = Harness(intent_model=model, model_domains="local")
    model.project_id = harness.beta["id"]
    try:
        before = _seeded_goal_counts(harness)
        status, body = harness.say(
            f"继续推进 {harness.alpha['id']} 和 {harness.beta['id']}")
        assert status == 200, body
        assert body["project_routing"]["source"] == "ambiguous"
        assert body["goal_ids"] == []
        assert _seeded_goal_counts(harness) == before
        assert harness.goal(harness.alpha_goal_id)["status"] == "needs_human"
        assert harness.goal(harness.beta_goal_id)["status"] == "needs_human"
    finally:
        harness.close()


def test_model_cannot_move_a_write_to_another_project(harness: Harness):
    """The sentence names alpha; a model claiming beta must be refused."""
    # The claimed id must belong to *this* harness's ledger.
    model = _ClaimingModel("submit_goal", "PLACEHOLDER", goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.beta["id"]
    try:
        before = _seeded_goal_counts(harness2)
        status, body = harness2.say(f"继续推进 {harness2.alpha['id']}")
        assert status == 200, body
        assert body["project_routing"] == {"source": "named",
                                          "project_id": harness2.alpha["id"]}
        assert body["intent"]["intent"] == "clarify", body
        assert body["goal_ids"] == []
        assert _seeded_goal_counts(harness2) == before
    finally:
        harness2.close()


def test_model_cannot_read_a_different_project_than_the_sentence(harness: Harness):
    model = _ClaimingModel("project_status", "PLACEHOLDER")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.beta["id"]
    try:
        status, body = harness2.say(f"{harness2.alpha['id']} 状态")
        assert status == 400, body
        assert body["error"]["code"] == "chat_request_rejected"
    finally:
        harness2.close()


def test_agreement_between_model_and_router_still_writes(harness: Harness):
    """The guard must not break the working case."""
    model = _ClaimingModel("submit_goal", "PLACEHOLDER", goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.alpha["id"]
    try:
        before = _seeded_goal_counts(harness2)
        status, body = harness2.say(
            f"继续推进 {harness2.alpha['id']}：实现 greet.py")
        assert status == 200, body
        assert body["intent"]["intent"] == "submit_goal"
        assert len(body["goal_ids"]) == 1
        alpha_goals = harness2.goals_of(harness2.alpha["id"])
        assert len(alpha_goals) == before[0] + 1
        assert len(harness2.goals_of(harness2.beta["id"])) == before[1]
        # The harness seeds one WorkItem per project; chat adds none.
        assert len(harness2.work_items(harness2.alpha["id"])) == 1
    finally:
        harness2.close()


# --------------------------------------------------------------------------- #
# 2. deterministic clues are reconciled, not ordered
# --------------------------------------------------------------------------- #
def test_project_name_plus_another_projects_task_id_is_ambiguous(
        harness: Harness):
    status, body = harness.say(
        f"{harness.alpha['id']} 这个项目里 {harness.beta_task} 怎么样了？")
    assert status == 200, body
    assert body["project_routing"]["source"] == "ambiguous", body
    assert sorted(body["project_routing"]["candidates"]) == sorted(
        [harness.alpha["id"], harness.beta["id"]])
    assert body["intent"]["intent"] == "clarify"
    assert body["goal_ids"] == []


def test_two_task_ids_from_two_projects_are_refused(harness: Harness):
    before = _seeded_goal_counts(harness)
    status, body = harness.say(
        f"对比 {harness.alpha_task} 和 {harness.beta_task} 的结果")
    assert status == 200, body
    assert body["project_routing"]["source"] == "ambiguous", body
    assert sorted(body["project_routing"]["candidates"]) == sorted(
        [harness.alpha["id"], harness.beta["id"]])
    # A single-project answer would be about a project the human did not name.
    assert body["intent"]["intent"] == "clarify", body
    assert body["goal_ids"] == []
    assert _seeded_goal_counts(harness) == before


def test_two_goal_ids_from_two_projects_are_refused(harness: Harness):
    status, body = harness.say(
        f"恢复 {harness.alpha_goal_id} 和 {harness.beta_goal_id}")
    assert status == 200, body
    assert body["project_routing"]["source"] == "ambiguous", body
    assert body["goal_ids"] == []
    assert harness.goal(harness.alpha_goal_id)["status"] == "needs_human"
    assert harness.goal(harness.beta_goal_id)["status"] == "needs_human"


def test_two_task_ids_from_one_project_still_resolve(harness: Harness):
    """Ambiguity is about *projects*, not about the number of ids."""
    status, body = harness.say(
        f"{harness.alpha_task} 和 {harness.alpha_goal_id} 属于哪个项目？")
    assert status == 200, body
    assert body["project_routing"]["project_id"] == harness.alpha["id"]
    assert body["project_routing"]["source"] in ("task", "goal")


# --------------------------------------------------------------------------- #
# 3. an ambiguous recent turn is not skipped
# --------------------------------------------------------------------------- #
def test_reference_does_not_skip_a_recent_ambiguous_turn(harness: Harness):
    # A first, so an older single-project turn exists to fall back to.
    _, first = harness.say(f"{harness.alpha['id']} 有哪些阻塞？")
    conversation_id = first["conversation_id"]
    assert first["project_routing"]["project_id"] == harness.alpha["id"]

    _, ambiguous = harness.say(
        f"{harness.alpha['id']} 和 {harness.beta['id']} 哪个更急？",
        conversation_id)
    assert ambiguous["project_routing"]["source"] == "ambiguous"

    before = _seeded_goal_counts(harness)
    status, body = harness.say("这个项目现在怎么样？", conversation_id)
    assert status == 200, body
    # It must not silently resolve to alpha because alpha was discussed first.
    assert body["project_routing"]["source"] == "ambiguous", body
    assert "project_id" not in body["project_routing"]
    assert body["intent"]["intent"] == "clarify"
    assert _seeded_goal_counts(harness) == before

    # And a write through the same reference cannot inherit alpha either.
    status, body = harness.say("继续推进这个项目", conversation_id)
    assert status == 200, body
    assert body["project_routing"]["source"] == "ambiguous"
    assert body["goal_ids"] == []
    assert _seeded_goal_counts(harness) == before


# --------------------------------------------------------------------------- #
# 4. a confirmation is bound to its project
# --------------------------------------------------------------------------- #
def test_confirming_with_another_project_writes_nothing(harness: Harness):
    # A write in a *global* conversation is applied directly, so stage a resume
    # to get a pending action in a conversation that is not project-bound.
    _, staged = harness.say(f"{harness.alpha['id']} 恢复 {harness.alpha_goal_id}")
    conversation_id = staged["conversation_id"]
    assert staged["pending_action"]["kind"] == "resume_goal", staged
    assert staged["pending_action"]["project_id"] == harness.alpha["id"]
    before = _seeded_goal_counts(harness)

    # Confirm, but point the request at beta.  Nothing may be resumed.
    status, body = harness.say("confirm", conversation_id,
                               project_id=harness.beta["id"])
    assert status == 200, body
    assert body["intent_source"] == "human_confirmation_project_mismatch", body
    assert body["facts"]["project_mismatch"] is True
    assert body["goal_ids"] == []
    assert harness.goal(harness.alpha_goal_id)["status"] == "needs_human"
    assert harness.goal(harness.beta_goal_id)["status"] == "needs_human"
    assert _seeded_goal_counts(harness) == before
    # The staged action survives: the operator can still confirm the right one.
    assert harness.conversation(conversation_id)["pending_action"] is not None

    # Confirming without a foreign project still works.
    status, body = harness.say("confirm", conversation_id)
    assert status == 200, body
    assert body["intent_source"] == "human_confirmation", body
    assert body["facts"]["resumed"] is True
    assert harness.goal(harness.alpha_goal_id)["status"] != "needs_human"
    assert harness.goal(harness.beta_goal_id)["status"] == "needs_human"


def test_confirmation_shows_project_name_id_goal_text_and_goal_id(
        harness: Harness):
    model = _ClaimingModel("submit_goal", "PLACEHOLDER", goal="实现 greet.py")
    harness2 = Harness(intent_model=model, model_domains="local")
    model.project_id = harness2.alpha["id"]
    try:
        # A question is not an imperative: the model's submit is staged.
        status, body = harness2.say(
            f"{harness2.alpha['id']} 能不能实现 greet.py？")
        assert status == 200, body
        assert body["requires_confirmation"] is True, body
        pending = body["pending_action"]
        assert pending["kind"] == "submit_goal"
        for expected in ("ACP-Alpha", harness2.alpha["id"],
                         "实现 greet.py", pending["goal_id"]):
            assert expected in body["answer"], (expected, body["answer"])
        assert harness2.goals_of(harness2.alpha["id"]) and len(
            harness2.goals_of(harness2.alpha["id"])) == 1

        status, confirmed = harness2.say("confirm", body["conversation_id"])
        assert status == 200, confirmed
        assert confirmed["facts"]["confirmed_by_human"] is True
        assert harness2.goal(pending["goal_id"])["project_id"] \
            == harness2.alpha["id"]
    finally:
        harness2.close()


def test_bound_conversation_confirmation_behaviour_is_unchanged(
        harness: Harness):
    """The historical path: a project-bound conversation confirms in place."""
    _, staged = harness.say(
        "恢复这个停止的 Goal", project_id=harness.alpha["id"])
    conversation_id = staged["conversation_id"]
    assert harness.conversation(conversation_id)["project_id"] \
        == harness.alpha["id"]
    assert staged["pending_action"]["project_id"] == harness.alpha["id"]

    status, body = harness.say("confirm", conversation_id)
    assert status == 200, body
    assert body["intent_source"] == "human_confirmation", body
    assert body["facts"]["resumed"] is True
    assert harness.goal(harness.alpha_goal_id)["status"] != "needs_human"
