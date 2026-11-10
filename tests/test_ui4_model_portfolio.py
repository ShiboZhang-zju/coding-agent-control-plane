"""Model-console cross-project reads: the six questions must always work.

The defect under test was found on the deployed Manager: the intent model
returned ``portfolio_status`` with ``parameters.focus=""``, ``parse_intent``
(rightly) rejected it and the console answered ``clarify`` for *every*
cross-project question.  Two things are fixed and guarded here:

* ``ld/model.py::_CHAT_SYSTEM_PROMPT`` now states the portfolio contract;
* ``LDChat._classify`` pre-routes an unambiguous, single-project-free,
  read-only aggregation request through the deterministic router, so a weak
  intent model cannot make a ledger-only question unavailable.

The pre-route is deliberately narrow.  These tests pin down that it never
becomes a bypass for writes, a fallback for bad model output, or a way to
widen a named/bound project into a global report.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))

from adapters.base import ArtifactAssertion  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.chat_intent import (  # noqa: E402
    PORTFOLIO_FOCUS, ChatPolicyError, chat_intent_schema, parse_intent)
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.model import _CHAT_SYSTEM_PROMPT  # noqa: E402
from ld.tools import LDTools  # noqa: E402

STATE = ROOT / ".poc-state" / "ui4-model-portfolio"

SIX_QUESTIONS = (
    "汇总所有项目目前的进度。",
    "所有项目有哪些未完成目标？",
    "两个项目各自有哪些需要我处理的？",
    "现在所有项目加起来有哪些未完成目标？",
    "给出各项目的优先级建议。",
    "汇总所有项目的阻塞、异常验收和待人工 Goal。",
)

#: Everything a read must not change, including approvals and resume counters.
SNAPSHOT_TABLES = (
    "projects", "workspaces", "work_items", "attempts", "ld_goals",
    "ld_goal_decisions", "approvals", "ld_goal_runner",
)


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


def _raw(intent: str = "portfolio_status", *, project_id=None,
         parameters=None, reason: str = "fake model claim") -> dict:
    """A full six-field model response, so rejections are about the claim."""
    return {"intent": intent, "project_id": project_id, "task_id": None,
            "goal_id": None, "reason": reason,
            "parameters": {} if parameters is None else dict(parameters)}


class _Model:
    """A configurable classifier that records every call."""

    def __init__(self, response=None, *, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0
        self.payloads: list[dict] = []

    def classify(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if self.raises is not None:
            raise self.raises
        return self.response if self.response is not None else _raw("clarify")


class Harness:
    """An independent test Ledger with numbers a report must reproduce."""

    def __init__(self, *, intent_model=None, model_domains: str = "local"):
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "model-portfolio.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")

        self.alpha_rejected = plane.enqueue(
            "alpha rejected", str(root / "alpha"),
            assertions=[ArtifactAssertion("file_exists", "a.md")],
            project_id=self.alpha["id"])["task_id"]
        self.beta_blocked = plane.enqueue(
            "beta inconclusive blocked", str(root / "beta"),
            assertions=[ArtifactAssertion("file_exists", "b.md")],
            project_id=self.beta["id"])["task_id"]
        self.gamma_verified = plane.enqueue(
            "beta verified", str(root / "beta"),
            assertions=[ArtifactAssertion("file_exists", "v.md")],
            project_id=self.beta["id"])["task_id"]
        store.update(self.alpha_rejected, state="succeeded",
                     verdict="rejected", queue_status="complete")
        store.update(self.beta_blocked, state="unknown",
                     verdict="inconclusive", queue_status="needs_human")
        store.update(self.gamma_verified, state="succeeded",
                     verdict="verified", queue_status="complete")
        store.close()

        goals = LDGoalStore(self.ledger)
        self.paused_text = "alpha paused goal"
        self.paused_goal, ref = goal_identity(self.paused_text,
                                             self.alpha["id"])
        goals.get_or_create(self.paused_goal, self.alpha["id"],
                            self.paused_text, ref)
        goals.update_goal(self.paused_goal, status="needs_human",
                          last_decision={"stop": "human review required"})
        goals.close()

        chats = LDChatStore(self.ledger)
        chats.close()

        self.model = intent_model
        # Pin the no-model case explicitly: an LD_MODEL_* variable left in the
        # shell must not silently turn a deterministic test into a network call.
        self.bridge = ChatBridge(
            self.ledger, intent_model=intent_model,
            model_base_url="" if intent_model is None else None,
            model_name="" if intent_model is None else None,
            model_domains=model_domains)

    def say(self, message: str, *, conversation_id=None, project_id=None) -> dict:
        return self.bridge.handle(message=message,
                                  conversation_id=conversation_id,
                                  project_id=project_id)

    def snapshot(self) -> dict:
        con = sqlite3.connect("file:" + str(self.ledger).replace("\\", "/")
                              + "?mode=ro", uri=True)
        con.execute("PRAGMA query_only=1")
        try:
            out = {}
            for table in SNAPSHOT_TABLES:
                try:
                    cur = con.execute("SELECT * FROM %s" % table)
                except sqlite3.Error as exc:
                    out[table] = "ERR:%s" % exc
                    continue
                rows = [tuple(str(value) for value in row)
                        for row in cur.fetchall()]
                blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
                out[table] = {"rows": len(rows),
                              "sha256": hashlib.sha256(
                                  blob.encode("utf-8")).hexdigest()}
            return out
        finally:
            con.close()

    def goals_of(self, project_id: str) -> list[dict]:
        store = LDGoalStore(self.ledger, read_only=True)
        try:
            return store.find_by_project(project_id)
        finally:
            store.close()


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        pass  # fixtures hold no long-lived handles


# --------------------------------------------------------------------------- #
# 1. the prompt / schema contract
# --------------------------------------------------------------------------- #
def test_prompt_states_the_portfolio_contract():
    for fragment in ("list_projects", "portfolio_status", "project_id",
                     "task_id", "goal_id", "summary", "goals", "attention",
                     "priority", "OMIT", "empty string"):
        assert fragment in _CHAT_SYSTEM_PROMPT, fragment
    assert "six top-level fields" in _CHAT_SYSTEM_PROMPT
    # The original boundaries are still stated, not dropped for the new rules.
    for fragment in ("cannot execute", "submit_goal", "resume_goal",
                     "clarify", "Never invent"):
        assert fragment in _CHAT_SYSTEM_PROMPT, fragment


def test_intent_schema_says_focus_is_optional_but_never_empty():
    note = chat_intent_schema()["intents"]["portfolio_status"]["note"]
    assert "OPTIONAL" in note and "empty" in note
    # Omitting focus is legal; an empty focus is not.
    base = {"intent": "portfolio_status", "project_id": None,
            "task_id": None, "goal_id": None, "reason": "test"}
    assert parse_intent({**base, "parameters": {}}).parameters == {}
    with pytest.raises(Exception):
        parse_intent({**base, "parameters": {"focus": ""}})
    assert set(PORTFOLIO_FOCUS) == {"summary", "goals", "attention", "priority"}


# --------------------------------------------------------------------------- #
# 2. the six questions, with and without a model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", SIX_QUESTIONS)
def test_six_questions_work_with_a_broken_model(question):
    """The exact production failure: model emits portfolio_status + focus="".

    The read must still work, must be labelled as deterministic (never as a
    model success), and must not call the broken model at all.
    """
    model = _Model(_raw("portfolio_status", parameters={"focus": ""}))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    turn = harness.say(question)

    assert turn["intent"]["intent"] == "portfolio_status", (question, turn)
    assert turn["intent_source"] == "heuristic_router", (question, turn)
    assert model.calls == 0, (question, model.calls)
    assert turn["project_routing"]["source"] == "none"
    assert turn["goal_ids"] == [] and turn["requires_confirmation"] is False
    portfolio = turn["portfolio"]
    assert portfolio is not None, (question, turn)
    totals = portfolio["totals"]
    # Numbers backed by the independent test Ledger.
    assert totals["work_items"] == 3
    assert totals["verified"] == 1 and totals["rejected"] == 1
    assert totals["inconclusive"] == 1
    assert totals["blocked"] == 1
    assert totals["needs_human_goals"] == 1
    assert "一、事实" in turn["answer"] and "二、判断" in turn["answer"]
    for project in (harness.alpha, harness.beta):
        assert project["id"] in turn["answer"]
    assert harness.snapshot() == before


@pytest.mark.parametrize("question", SIX_QUESTIONS)
def test_six_questions_still_work_without_a_model(question):
    harness = Harness(intent_model=None)
    before = harness.snapshot()
    turn = harness.say(question)
    assert turn["intent"]["intent"] == "portfolio_status", question
    assert turn["intent_source"] == "heuristic_router"
    assert turn["portfolio"]["totals"]["work_items"] == 3
    assert harness.snapshot() == before


# --------------------------------------------------------------------------- #
# 3. the pre-route stays narrow
# --------------------------------------------------------------------------- #
def test_registry_question_still_uses_the_model_and_lists_projects():
    model = _Model(_raw("list_projects"))
    harness = Harness(intent_model=model)
    turn = harness.say("现在有哪些项目？")
    assert turn["intent"]["intent"] == "list_projects", turn
    assert turn["intent_source"] == "model"
    assert model.calls == 1
    assert turn["portfolio"] is None


@pytest.mark.parametrize("message", [
    "汇总 ACP-Alpha 目前的进度",
    "给出 ACP-Beta 的优先级建议",
])
def test_named_project_is_never_widened_into_a_global_report(message):
    model = _Model(_raw("clarify", parameters={"question": "Which project?"}))
    harness = Harness(intent_model=model)
    turn = harness.say(message)
    assert turn["intent"]["intent"] != "portfolio_status", turn
    assert turn["portfolio"] is None
    assert model.calls == 1, "a named project must keep the model path"
    assert turn["project_routing"]["source"] == "named"


def test_bound_conversation_is_not_widened_into_a_global_report():
    model = _Model(_raw("clarify", parameters={"question": "Which project?"}))
    harness = Harness(intent_model=model)
    turn = harness.say("汇总所有项目目前的进度",
                       project_id=harness.alpha["id"])
    assert turn["intent"]["intent"] != "portfolio_status", turn
    assert turn["portfolio"] is None
    assert model.calls == 1


@pytest.mark.parametrize("message", [
    "汇总所有项目目前的进度，并继续推进 ACP-Alpha",
    "汇总所有项目目前的进度，并提交一个新目标",
    "汇总所有项目目前的进度，并恢复这个停止的 Goal",
    # A submit verb that is NOT in the write-marker list: only the imperative
    # guard keeps this off the read-only fast path.
    "汇总所有项目目前的进度，并完成这件事",
])
def test_mixed_write_request_does_not_borrow_the_read_fast_path(message):
    """An explicit write keeps the model + strict validation path."""
    model = _Model(_raw("clarify", parameters={"question": "Which project?"}))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    turn = harness.say(message)
    assert model.calls == 1, (message, model.calls)
    assert turn["intent"]["intent"] != "portfolio_status", (message, turn)
    assert turn["goal_ids"] == [] and not turn["requires_confirmation"]
    assert harness.snapshot() == before, message


# --------------------------------------------------------------------------- #
# 4. bad model output still fails closed (never silently repaired)
# --------------------------------------------------------------------------- #
def test_empty_focus_from_a_model_still_fails_closed():
    model = _Model(_raw("portfolio_status", parameters={"focus": ""}))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    # Not a cross-project read: the fast path must not rescue this response.
    turn = harness.say("ACP-Alpha 当前状态？")
    assert turn["intent"]["intent"] == "clarify", turn
    assert turn["intent_source"] == "invalid_model_intent", turn
    assert model.calls == 1
    assert harness.snapshot() == before


def test_illegal_focus_enum_from_a_model_is_a_policy_error():
    model = _Model(_raw("portfolio_status", parameters={"focus": "everything"}))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    with pytest.raises(ChatPolicyError):
        harness.say("ACP-Alpha 当前状态？")
    assert model.calls == 1
    assert harness.snapshot() == before


def test_unavailable_model_fails_closed_without_writes():
    model = _Model(raises=RuntimeError("endpoint down"))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    turn = harness.say("ACP-Alpha 当前状态？")
    assert turn["intent"]["intent"] == "clarify"
    assert turn["intent_source"] == "model_unavailable"
    assert harness.snapshot() == before


# --------------------------------------------------------------------------- #
# 5. existing write gates are unchanged
# --------------------------------------------------------------------------- #
def test_global_submit_goal_is_still_staged_and_confirmed():
    model = _Model()
    harness = Harness(intent_model=model)
    model.response = _raw("submit_goal", project_id=harness.alpha["id"],
                          parameters={"goal": "实现 greet.py"})
    before = harness.snapshot()
    turn = harness.say("继续推进 ACP-Alpha：实现 greet.py")
    assert turn["intent"]["intent"] == "submit_goal", turn
    assert turn["requires_confirmation"] is True
    assert turn["goal_ids"] == []
    assert harness.snapshot() == before
    # Only the seeded paused Goal exists; nothing was created by staging.
    assert len(harness.goals_of(harness.alpha["id"])) == 1

    conversation_id = turn["conversation_id"]
    # A confirmation naming another project is refused and keeps the stage.
    model.response = _raw("clarify", parameters={"question": "Which project?"})
    mismatch = harness.say("confirm", conversation_id=conversation_id,
                           project_id=harness.beta["id"])
    assert mismatch["intent_source"] == "human_confirmation_project_mismatch"
    assert mismatch["goal_ids"] == []
    assert harness.snapshot() == before

    confirmed = harness.say("confirm", conversation_id=conversation_id)
    assert confirmed["intent_source"] == "human_confirmation"
    assert len(confirmed["goal_ids"]) == 1
    assert len(harness.goals_of(harness.alpha["id"])) == 2


def test_all_six_questions_together_leave_the_ledger_unchanged():
    model = _Model(_raw("portfolio_status", parameters={"focus": ""}))
    harness = Harness(intent_model=model)
    before = harness.snapshot()
    conversation_id = None
    for question in SIX_QUESTIONS:
        turn = harness.say(question, conversation_id=conversation_id)
        conversation_id = turn["conversation_id"]
        assert turn["intent"]["intent"] == "portfolio_status", question
    # Negative cases in the same conversation must not create anything either.
    harness.say("现在有哪些项目？", conversation_id=conversation_id)
    harness.say("汇总所有项目目前的进度，并提交一个新目标",
                conversation_id=conversation_id)
    assert harness.snapshot() == before
