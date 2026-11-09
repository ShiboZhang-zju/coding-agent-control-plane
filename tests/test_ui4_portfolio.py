"""UI-4 cross-project portfolio: one metric definition, one read-only answer.

Two defects are under test, both found by actually using the deployed global LD:

1. **A global list that under-reported.** ``_list_projects`` counted only
   ``progress['blocked_tasks']``, so a ``rejected`` task and a ``needs_human``
   goal were invisible on the cross-project overview even though the
   single-project view reported them.  On the real ledger that meant
   "0 needing attention" for a project with one rejected task and one stopped
   goal.  The fix classifies every work item into exactly one of two buckets
   (blocked vs abnormal-verdict) and counts goals separately, from the full task
   list rather than the 10-row ``recent_tasks`` window.

2. **No cross-project answer at all.** "汇总所有项目目前的进度" was answered with
   the project list, because such a request resolves to no single project and the
   only project-free intents were ``list_projects`` and ``clarify``.  A
   ``portfolio_status`` intent now answers it from the ledger, and is exempt from
   the ``source=none`` → ``clarify`` rule *because* its subject is every project.

What each assertion is allowed to rest on
-----------------------------------------
Every number is read back from SQLite (work-item states/verdicts, goal rows) and
compared with what the answer claims.  The read-only claim is proven by
snapshotting the business tables before and after the turn — not by trusting the
absence of a write path in the code.

Nothing here asserts a *specific* production figure: the fixtures build their own
ledger, and the production cross-check compares against a fresh direct read.
"""
from __future__ import annotations

import json
import re
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
from controlplane.project_service import ProjectService  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.chat import MAX_REPORTED_WORK_ITEMS, LDChat  # noqa: E402
from ld.chat_store import LDChatStore  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402
from ld.tools import LDTools  # noqa: E402

STATE = ROOT / ".poc-state" / "ui4-portfolio"
TOKEN = "ui4-portfolio-operator-token-32-ch"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Harness:
    """Four projects covering the states a portfolio report must distinguish."""

    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "portfolio.db"
        self._open: list = []
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)

        # 1. rejected but NOT blocked (the case the old count dropped)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        # 2. inconclusive AND blocked (the overlap that must not double count)
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
        # 3. completed goal + verified task
        self.gamma = plane.register_project(
            "ACP-Gamma", str(_git_repo(root / "gamma")), default_adapter="stub")
        # 4. no work items at all
        self.delta = plane.register_project(
            "ACP-Delta", str(_git_repo(root / "delta")), default_adapter="stub")
        # 5. needs_human goals whose stop reason is *not* budget exhaustion
        self.epsilon = plane.register_project(
            "ACP-Epsilon", str(_git_repo(root / "epsilon")),
            default_adapter="stub")

        self.alpha_rejected = plane.enqueue(
            "alpha rejected task", str(root / "alpha"),
            assertions=[ArtifactAssertion("file_exists", "a.md")],
            project_id=self.alpha["id"])["task_id"]
        self.beta_blocked = plane.enqueue(
            "beta inconclusive blocked", str(root / "beta"),
            assertions=[ArtifactAssertion("file_exists", "b.md")],
            project_id=self.beta["id"])["task_id"]
        self.beta_rejected = plane.enqueue(
            "beta rejected task", str(root / "beta"),
            assertions=[ArtifactAssertion("file_exists", "b2.md")],
            project_id=self.beta["id"])["task_id"]
        self.gamma_verified = plane.enqueue(
            "gamma verified task", str(root / "gamma"),
            assertions=[ArtifactAssertion("file_exists", "g.md")],
            project_id=self.gamma["id"])["task_id"]

        # Verdicts are written straight to the ledger: this test is about how
        # the LD *reads* them, not about how the Verifier produces them.
        store.update(self.alpha_rejected, state="succeeded", verdict="rejected",
                     queue_status="complete")
        store.update(self.beta_rejected, state="succeeded", verdict="rejected",
                     queue_status="complete")
        store.update(self.beta_blocked, state="unknown", verdict="inconclusive",
                     queue_status="needs_human")
        store.update(self.gamma_verified, state="succeeded", verdict="verified",
                     queue_status="complete")
        store.close()

        goals = LDGoalStore(self.ledger)
        # alpha: needs_human, budget exhausted
        self.alpha_goal_text = "alpha goal exhausted"
        self.alpha_goal, ref = goal_identity(self.alpha_goal_text, self.alpha["id"])
        goals.get_or_create(self.alpha_goal, self.alpha["id"],
                            self.alpha_goal_text, ref)
        goals.update_goal(self.alpha_goal, status="needs_human",
                          last_decision={"stop": "lifetime decision budget "
                                                 "exhausted",
                                         "model_decision_count": 2,
                                         "max_decisions_total": 2})
        # beta: needs_human, waiting for a human decision
        self.beta_goal_text = "beta goal needs a decision"
        self.beta_goal, ref = goal_identity(self.beta_goal_text, self.beta["id"])
        goals.get_or_create(self.beta_goal, self.beta["id"],
                            self.beta_goal_text, ref)
        goals.update_goal(self.beta_goal, status="needs_human",
                          last_decision={"step": 2, "action": "request_human",
                                         "next_state": "human_required"})
        # gamma: completed
        gamma_text = "gamma goal finished"
        self.gamma_goal, ref = goal_identity(gamma_text, self.gamma["id"])
        goals.get_or_create(self.gamma_goal, self.gamma["id"], gamma_text, ref)
        goals.update_goal(self.gamma_goal, status="completed")
        # Epsilon carries the goals whose stop reason is *not* a budget stop.
        # Keeping them off ``delta`` preserves that project as the genuinely
        # empty one ("no tasks, no goals") used by other tests.
        # ``invalid_model_decision``: the case the old rule mislabelled as
        # "budget exhausted".
        self.epsilon_goal_text = "epsilon goal invalid model decision"
        self.epsilon_goal, ref = goal_identity(self.epsilon_goal_text,
                                               self.epsilon["id"])
        goals.get_or_create(self.epsilon_goal, self.epsilon["id"],
                            self.epsilon_goal_text, ref)
        # Recorded as a *stop*, not as an action=request_human: this is the goal
        # the old rule swept into "budget exhausted" purely because it was not
        # request_human.
        goals.update_goal(self.epsilon_goal, status="needs_human",
                          last_decision={"stop": "invalid_model_decision"})
        # And one with no recognisable reason at all.
        self.epsilon_goal2_text = "epsilon goal unrecognised stop"
        self.epsilon_goal2, ref = goal_identity(self.epsilon_goal2_text,
                                                self.epsilon["id"])
        goals.get_or_create(self.epsilon_goal2, self.epsilon["id"],
                            self.epsilon_goal2_text, ref)
        goals.update_goal(self.epsilon_goal2, status="needs_human",
                          last_decision={"stop": "something_new_entirely"})
        # And one that records *no* reason at all: the ledger says it is parked
        # but not why, so the only honest label is "未记录停止原因".
        self.epsilon_goal3_text = "epsilon goal with no recorded reason"
        self.epsilon_goal3, ref = goal_identity(self.epsilon_goal3_text,
                                                self.epsilon["id"])
        goals.get_or_create(self.epsilon_goal3, self.epsilon["id"],
                            self.epsilon_goal3_text, ref)
        goals.update_goal(self.epsilon_goal3, status="needs_human")
        goals.close()

        bridge = ChatBridge(self.ledger)
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

    def close_all(self) -> None:
        """Release the stores a metric test opened directly."""
        while self._open:
            store, goals, chats = self._open.pop()
            for handle in (chats, goals, store):
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

    def close(self) -> None:
        self.close_all()
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
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def say(self, message, conversation_id=None, project_id=None):
        return self._request("/api/chat/messages", method="POST", payload={
            "conversation_id": conversation_id, "project_id": project_id,
            "message": message})

    # -- direct ledger reads, used to check the answer ------------------ #
    def work_items(self, project_id):
        conn = TaskStore(self.ledger, read_only=True)
        try:
            return conn.work_items_for_project(project_id)
        finally:
            conn.close()

    def goals(self, project_id):
        store = LDGoalStore(self.ledger, read_only=True)
        try:
            return store.find_by_project(project_id)
        finally:
            store.close()

    def chat(self) -> "LDChat":
        """A real ``LDChat`` over this ledger, wired like ``ChatBridge`` does.

        Constructed directly (not through HTTP) so the metric tests can call
        ``_project_metrics`` itself rather than infer it from prose.
        """
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        tools = LDTools(ProjectService(plane))
        goals = LDGoalStore(self.ledger)
        chats = LDChatStore(self.ledger)
        chat = LDChat(tools, goals, chats, None,
                      model_authorized_security_domains=("local",))
        self._open.append((store, goals, chats))
        return chat

    def business_snapshot(self) -> dict:
        """Row counts *and* ids of everything a read must not change."""
        conn = TaskStore(self.ledger, read_only=True)
        try:
            items = sorted((task["task_id"], task.get("state"),
                            task.get("verdict"), task.get("queue_status"))
                           for task in conn.all())
        finally:
            conn.close()
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            rows = sorted((goal["goal_id"], goal["status"],
                           goal.get("decision_count"), goal.get("resume_count"),
                           goal.get("task_count"))
                          for project in (self.alpha, self.beta, self.gamma,
                                          self.delta, self.epsilon)
                          for goal in goals.find_by_project(project["id"]))
        finally:
            goals.close()
        return {"work_items": items, "goals": rows}


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


# --------------------------------------------------------------------------- #
# Task 1: one metric definition shared by both views
# --------------------------------------------------------------------------- #
def test_rejected_task_is_counted_even_when_not_blocked(harness: Harness):
    """The exact defect: rejected + not blocking used to be invisible."""
    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.alpha["id"])
    finally:
        harness.close_all()
    assert metrics["blocked_workitems"] == [], (
        "a rejected task is not 'blocked': nothing is queued")
    assert [task["task_id"] for task in metrics["abnormal_verdict_workitems"]] \
        == [harness.alpha_rejected]
    assert metrics["abnormal_verdict_workitems"][0]["verdict"] == "rejected"
    # The goal is counted separately, not as a work item.
    assert [goal["goal_id"] for goal in metrics["needs_human_goals"]] \
        == [harness.alpha_goal]


def test_blocked_and_abnormal_overlap_is_counted_once(harness: Harness):
    """``inconclusive`` + ``needs_human`` is one row, not two."""
    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.beta["id"])
    finally:
        harness.close_all()
    blocked_ids = [task["task_id"] for task in metrics["blocked_workitems"]]
    abnormal_ids = [task["task_id"]
                    for task in metrics["abnormal_verdict_workitems"]]
    assert blocked_ids == [harness.beta_blocked]
    assert abnormal_ids == [harness.beta_rejected]
    # The overlap is reported as blocked *and* keeps its bad verdict visible.
    assert metrics["blocked_workitems"][0]["verdict"] == "inconclusive"
    assert not set(blocked_ids) & set(abnormal_ids), (
        "a work item must not appear in both buckets")
    assert len(blocked_ids) + len(abnormal_ids) == 2


def test_metric_excludes_completed_goal_from_unfinished(harness: Harness):
    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.gamma["id"])
    finally:
        harness.close_all()
    assert metrics["goal_status_counts"] == {"completed": 1}
    assert metrics["unfinished_goals"] == []
    assert metrics["blocked_workitems"] == []
    assert metrics["abnormal_verdict_workitems"] == []


def test_project_with_no_tasks_reports_zero_not_absent(harness: Harness):
    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.delta["id"])
    finally:
        harness.close_all()
    assert metrics["work_items_total"] == 0
    assert metrics["blocked_workitems"] == []
    assert metrics["abnormal_verdict_workitems"] == []
    assert metrics["goal_status_counts"] == {}


def test_project_metrics_reads_full_task_list_not_recent_window(
        harness: Harness):
    """An old rejection must survive; ``recent_tasks`` is capped at 10."""
    # Add 15 newer verified tasks to alpha so the rejected one is older than the
    # 10-row ``recent_tasks`` window.  Created through the plane, so the records
    # are the same shape production produces.
    store = TaskStore(harness.ledger)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    try:
        for index in range(15):
            task_id = plane.enqueue(
                f"filler {index}", str(harness.root / "alpha"),
                assertions=[ArtifactAssertion("file_exists", f"f{index}.md")],
                project_id=harness.alpha["id"])["task_id"]
            store.update(task_id, state="succeeded", verdict="verified",
                         queue_status="complete")
    finally:
        store.close()

    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.alpha["id"])
    finally:
        harness.close_all()
    ids = [task["task_id"] for task in metrics["abnormal_verdict_workitems"]]
    assert harness.alpha_rejected in ids, (
        "the older rejected task must still be counted, not dropped by a "
        "10-row recent window")
    # The rejected one is now the 16th newest, i.e. outside that window.
    assert len(metrics["abnormal_verdict_workitems"]) == 1


def test_task_read_failure_is_reported_not_silently_zero(harness: Harness):
    """A failed task read must not look like "this project has no tasks".

    This is the bug that made the first version of the report wrong: asking the
    tools layer for more rows than its maximum raised ``ValueError``, a
    ``_safe`` wrapper turned it into ``[]``, and the report stated "0 tasks" for
    a project that had rejected work.  A wrong number is worse than an error, so
    the failure has to surface.
    """
    chat = harness.chat()
    try:
        original = chat._tools.get_project_tasks

        def broken(project_id, *, limit=200):
            raise ValueError("limit must be an integer from 1 to 200")

        chat._tools.get_project_tasks = broken
        try:
            metrics = chat._project_metrics(harness.alpha["id"])
        finally:
            chat._tools.get_project_tasks = original
    finally:
        harness.close_all()
    assert metrics["read_error"], "a failed read must be recorded"
    assert "ValueError" in metrics["read_error"]
    # The counts still come from the progress projection, so they are not lost.
    assert metrics["counts"]["total"] == 1
    # But the item lists are honestly empty rather than fabricated.
    assert metrics["abnormal_verdict_workitems"] == []


def test_task_read_failure_marks_unknown_not_zero(harness: Harness):
    """A task-detail failure must surface as 未知, never as an exact zero.

    The report has to say *which* quantity is unknown and must not present the
    abnormal-verdict total as a precise number, because that total was never
    read.  ``unknown`` names the missing quantity so a console can refuse to
    render a zero.
    """
    chat = harness.chat()
    try:
        chat._tools.get_project_tasks = lambda project_id, *, limit=200: \
            (_ for _ in ()).throw(ValueError("boom"))
        turn = chat.handle("汇总所有项目目前的进度")
    finally:
        harness.close_all()
    assert any("boom" in line for line in turn["facts"]["unknowns"])
    assert "数据不完整" in turn["answer"]
    # The per-project line says the detail is unknown rather than "无".
    assert "异常验收：未知" in turn["answer"]
    # And the headline total is a lower bound, not an exact figure.
    assert "异常验收 ≥" in turn["answer"] or "≥0" in turn["answer"]
    # The overall summary is declared incomplete.
    assert turn["facts"]["complete"] is False
    assert turn["facts"]["totals"]["incomplete_projects"]


def test_progress_read_failure_is_not_reported_as_zero(harness: Harness):
    """A failed progress read must not become "0 tasks, 0 rejected".

    ``get_project_progress`` supplies both the counters and the blocking flags.
    Silently defaulting it to ``{}`` made every derived number a confident zero
    for a project that actually has rejected work — a precise-looking figure
    nobody measured.
    """
    chat = harness.chat()
    try:
        original = chat._tools.get_project_progress

        def broken(project_id, **kwargs):
            raise RuntimeError("progress unavailable")

        chat._tools.get_project_progress = broken
        try:
            metrics = chat._project_metrics(harness.alpha["id"])
        finally:
            chat._tools.get_project_progress = original
    finally:
        harness.close_all()

    assert metrics["counts_known"] is False
    assert metrics["blocking_known"] is False
    assert metrics["complete"] is False
    assert "task_counts" in metrics["unknown"]
    assert "blocked" in metrics["unknown"]
    assert metrics["read_error"] and "progress unavailable" in metrics["read_error"]
    # No fabricated zero: the counters are simply absent.
    assert metrics["counts"] == {}
    # The full task list was still readable, so the physical work items are
    # still reported — it is the *counts* and *blocking* set that are unknown.
    assert metrics["tasks_known"] is True


def test_blocked_survives_a_task_list_failure(harness: Harness):
    """A known blocker must not be erased by an unrelated read failure.

    ``get_project_progress`` *is* the authority on what is blocked, and it
    succeeded — it named ``cp-...`` as blocked.  ``get_project_tasks`` then
    failed.  The old code built the blocked list by iterating the task list, so
    that failure silently turned a known blocker into "阻塞：无".  The blocking
    facts the authoritative read already returned (id, state, verdict,
    queue_status, attention_flags) have to survive; only the title, which lives
    exclusively on the task list, may be missing.
    """
    chat = harness.chat()
    try:
        # The progress projection is real; only the detail scan breaks.
        chat._tools.get_project_tasks = lambda project_id, **kwargs: \
            (_ for _ in ()).throw(RuntimeError("task detail unavailable"))
        metrics = chat._project_metrics(harness.beta["id"])
        turn = chat.handle("汇总所有项目目前的进度")
    finally:
        harness.close_all()

    # 1. The blocker is still there, with its authoritative facts intact.
    blocked_ids = [task["task_id"] for task in metrics["blocked_workitems"]]
    assert blocked_ids == [harness.beta_blocked], (
        "a failed task read must not erase a blocker the progress read returned")
    blocked = metrics["blocked_workitems"][0]
    assert blocked["state"] == "unknown"
    assert blocked["verdict"] == "inconclusive"
    assert blocked["queue_status"] == "needs_human"
    assert blocked["attention_flags"], "attention flags come from the blocker row"
    # The title is the only casualty, because only the task list carries it.
    assert blocked["title"] is None

    # 2. ``blocking_known`` describes the *authoritative* read, which succeeded.
    assert metrics["blocking_known"] is True
    assert "blocked" not in metrics["unknown"]
    # The task detail is still honestly reported as unread.
    assert metrics["tasks_known"] is False
    assert "work_items" in metrics["unknown"]
    # So the project is incomplete overall, but not because of the blockers.
    assert metrics["complete"] is False

    # 3. The report shows the blocker rather than "阻塞：无".  Locate the project
    # *heading* (not just its id, which also appears in the leading notice).
    answer = turn["answer"]
    assert harness.beta_blocked in answer
    heading = f"【ACP-Beta】{harness.beta['id']}"
    assert heading in answer, answer[:400]
    beta_section = answer.split(heading, 1)[1].split("【", 1)[0]
    assert "阻塞：无" not in beta_section, beta_section
    assert harness.beta_blocked in beta_section


def test_bounded_scan_does_not_claim_exact_abnormal_detail(harness: Harness):
    """Past the read bound the abnormal *detail* cannot be called exact.

    The project-level counts are confirmed by ``get_project_progress`` and stay
    exact; what is uncertain is the enumeration of individual abnormal verdicts,
    because rows older than the bound were never scanned.  An older rejection is
    therefore allowed to be absent — but the report must say so instead of
    presenting the shorter list as complete.
    """
    # Fill alpha past the bound with newer verified tasks, so the seeded
    # rejected task is older than anything the scan can reach.
    store = TaskStore(harness.ledger)
    plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
    try:
        for index in range(MAX_REPORTED_WORK_ITEMS):
            task_id = plane.enqueue(
                f"filler {index}", str(harness.root / "alpha"),
                assertions=[ArtifactAssertion("file_exists", f"f{index}.md")],
                project_id=harness.alpha["id"])["task_id"]
            store.update(task_id, state="succeeded", verdict="verified",
                         queue_status="complete")
    finally:
        store.close()

    chat = harness.chat()
    try:
        metrics = chat._project_metrics(harness.alpha["id"])
        turn = chat.handle("汇总所有项目目前的进度")
    finally:
        harness.close_all()

    # The scan really was bounded.
    if not metrics["truncated"]:
        pytest.skip("the fixture did not exceed the scan bound on this backend")
    assert metrics["abnormal_detail_complete"] is False
    assert metrics["abnormal_detail_note"]
    # The old rejection fell outside the scan, which is precisely why the detail
    # cannot be called complete.
    assert harness.alpha_rejected not in [
        task["task_id"] for task in metrics["abnormal_verdict_workitems"]]
    # The progress projection still answered, so its role is unchanged: what it
    # reports is retained, and the *detail* is what gets qualified.
    assert metrics["counts_known"] is True

    answer = turn["answer"]
    # Split on the project *heading*, not on its id: the id also appears in the
    # leading truncation notice, which already says "可能不完整".  Splitting on
    # the id let this assertion pass without ever inspecting the section whose
    # completeness is in question.
    heading = f"【ACP-Alpha】{harness.alpha['id']}"
    assert heading in answer, answer[:400]
    alpha_section = answer.split(heading, 1)[1].split("【", 1)[0]
    # The abnormal detail in that section is qualified, not stated as exact.
    assert "明细可能不完整" in alpha_section, alpha_section
    # The cross-project headline must not print the abnormal total as exact.
    assert "异常验收 ≥" in answer, answer[:400]
    # The summary projection carries the qualification too, so a console cannot
    # render the bounded enumeration as a complete count.
    alpha_row = next(row for row in turn["facts"]["projects"]
                     if row["project_id"] == harness.alpha["id"])
    assert alpha_row["abnormal_detail_complete"] is False
    assert alpha_row["truncated"] is True
    # And the qualified wording is specifically about the abnormal detail, not
    # a blanket claim that every number is wrong.
    assert metrics["abnormal_detail_complete"] is False


def test_progress_read_failure_in_answer_says_unknown_not_zero(
        harness: Harness):
    chat = harness.chat()
    try:
        chat._tools.get_project_progress = lambda project_id, **kwargs: \
            (_ for _ in ()).throw(RuntimeError("progress unavailable"))
        turn = chat.handle("汇总所有项目目前的进度")
    finally:
        harness.close_all()
    answer = turn["answer"]
    # The project block declines to print counts it does not have.
    assert "任务计数：未知" in answer
    assert "阻塞：未知" in answer
    # And it does not print the fabricated "0 个" for tasks.
    assert "任务 0 个" not in answer
    # The summary is explicitly declared partial, with the project named.
    assert "本汇总不完整" in answer
    assert harness.alpha["id"] in answer
    assert turn["facts"]["complete"] is False
    assert harness.alpha["id"] in turn["facts"]["totals"]["incomplete_projects"]
    # A lower bound, not an exact total.
    assert "≥" in answer


def test_progress_read_failure_in_list_projects(harness: Harness):
    """``list_projects`` must show the same unknown, not a clean zero line."""
    chat = harness.chat()
    try:
        chat._tools.get_project_progress = lambda project_id, **kwargs: \
            (_ for _ in ()).throw(RuntimeError("progress unavailable"))
        answer, facts, _goal_ids, _pending = chat._list_projects(
            None, {}, "现在有哪些项目？")
    finally:
        harness.close_all()
    rows = {row["project_id"]: row for row in facts["projects"]}
    alpha = rows[harness.alpha["id"]]
    assert alpha["complete"] is False
    assert alpha["counts_known"] is False
    assert alpha["blocking_known"] is False
    assert alpha["unknown"]
    assert alpha["read_error"]
    assert "未知" in answer
    assert "数据不完整" in answer


def test_read_failure_exposed_through_http_projection(harness: Harness):
    """The console must be able to tell "0" from "could not read".

    The restricted HTTP projection forwards completeness flags and a truncated
    error string — never the full ``facts`` payload.
    """
    status, body = harness.say("现在有哪些项目？")
    assert status == 200, body
    rows = {row["project_id"]: row for row in body["project_rows"]}
    healthy = rows[harness.alpha["id"]]
    # A healthy project is explicitly complete, so a console can trust the zeros.
    assert healthy["complete"] is True
    assert healthy["counts_known"] is True
    assert healthy["unknown"] == []
    assert healthy["read_error"] is None
    # The full facts payload is still withheld.
    assert "goals" not in healthy


def test_budget_stop_is_classified_as_budget(harness: Harness):
    """Case 1: an explicit lifetime-budget stop is called budget exhaustion."""
    chat = harness.chat()
    try:
        turn = chat.handle("给出各项目的优先级建议")
        answer, facts = turn["answer"], turn["facts"]
    finally:
        harness.close_all()
    priorities = {item["project_id"]: item
                  for item in facts["priorities"]}
    alpha = priorities[harness.alpha["id"]]
    assert "预算耗尽" in alpha["why"], alpha
    assert harness.alpha_goal in alpha["why"]
    assert "budget" in alpha["basis"]


def test_request_human_stop_is_classified_as_awaiting_decision(
        harness: Harness):
    """Case 2: ``request_human`` is a pending decision, not a budget stop."""
    chat = harness.chat()
    try:
        turn = chat.handle("给出各项目的优先级建议")
        answer, facts = turn["answer"], turn["facts"]
    finally:
        harness.close_all()
    priorities = {item["project_id"]: item
                  for item in facts["priorities"]}
    beta = priorities[harness.beta["id"]]
    # Beta's most urgent finding is its unknown-state task (P1), so the pending
    # decision appears alongside rather than as the headline reason.  Search the
    # whole entry, not just ``why``.
    entry_text = beta["why"] + " " + " ".join(beta.get("also") or [])
    assert "等一个具体决定" in entry_text, beta
    assert harness.beta_goal in entry_text
    assert beta["rank"] == "P1"
    assert "预算耗尽" not in entry_text, (
        "a request_human stop must not be described as budget exhaustion")


@pytest.mark.parametrize("goal_attr", ["epsilon_goal", "epsilon_goal2",
                                       "epsilon_goal3"])
def test_non_budget_needs_human_is_not_labelled_budget(goal_attr, harness: Harness):
    """Case 3: an unrecognised / non-budget reason is 待核查, never budget.

    ``invalid_model_decision`` and a wholly unknown stop reason are both
    ``needs_human`` without a budget cause.  The old rule put every goal that
    was not ``request_human`` into the budget bucket, which told the operator to
    weigh a budget that was never the reason.
    """
    goal_id = getattr(harness, goal_attr)
    chat = harness.chat()
    try:
        turn = chat.handle("给出各项目的优先级建议")
        answer, facts = turn["answer"], turn["facts"]
    finally:
        harness.close_all()

    assert "待核查" in answer
    # The goal is named with its recorded reason, not with an invented one.
    assert goal_id in answer
    # This goal is never *attributed* to budget exhaustion.  The wording may
    # still mention the budget to say the opposite ("不要假定为预算耗尽"), so the
    # check is that no line attributes the stop to a budget cause.
    for line in answer.splitlines():
        if goal_id not in line:
            continue
        assert "因预算耗尽停止" not in line, line
        assert "恢复会再次耗尽同一预算" not in line, line
    # And the goal is not listed under the budget reason at all.
    budget_lines = [line for line in answer.splitlines()
                    if "因预算耗尽停止" in line]
    assert not any(goal_id in line for line in budget_lines), budget_lines


def test_budget_and_non_budget_goals_are_not_merged(harness: Harness):
    """The two causes must stay distinguishable in one project's answer."""
    chat = harness.chat()
    try:
        turn = chat.handle("给出各项目的优先级建议")
        answer, facts = turn["answer"], turn["facts"]
    finally:
        harness.close_all()
    # alpha (budget) and epsilon (non-budget) are separate project entries, so a
    # reader can see which cause belongs to which goal.
    priorities = {item["project_id"]: item for item in facts["priorities"]}
    # alpha is *attributed* to a budget stop; epsilon is not.  Matching on the
    # attribution phrase keeps this honest even though the epsilon text mentions
    # the budget to say the opposite ("不要假定为预算耗尽").
    assert "因预算耗尽停止" in priorities[harness.alpha["id"]]["why"]
    epsilon_text = (priorities[harness.epsilon["id"]]["why"] + " "
                    + " ".join(priorities[harness.epsilon["id"]].get("also") or []))
    assert "因预算耗尽停止" not in epsilon_text
    assert "待核查" in epsilon_text
    assert "待核查" in answer


def test_global_list_uses_the_same_metrics(harness: Harness):
    status, body = harness.say("现在有哪些项目？")
    assert status == 200, body
    assert body["intent"]["intent"] == "list_projects"
    projects = {row["project_id"]: row for row in body["project_rows"]}
    alpha = projects[harness.alpha["id"]]
    beta = projects[harness.beta["id"]]
    gamma = projects[harness.gamma["id"]]
    delta = projects[harness.delta["id"]]
    # alpha: 1 rejected, not blocked, 1 needs_human goal
    assert alpha["counts"]["rejected"] == 1
    assert alpha["blocked"] == 0
    assert alpha["abnormal_verdict"] == 1
    assert alpha["needs_human_goals"] == 1
    # beta: 1 blocked (inconclusive) + 1 rejected, 1 needs_human goal
    assert beta["blocked"] == 1
    assert beta["abnormal_verdict"] == 1
    assert beta["needs_human_goals"] == 1
    # gamma: clean
    assert gamma["blocked"] == 0 and gamma["abnormal_verdict"] == 0
    assert gamma["needs_human_goals"] == 0
    # delta: no tasks, no goals
    assert delta["counts"]["total"] == 0
    assert delta["blocked"] == 0 and delta["abnormal_verdict"] == 0


def test_single_project_and_global_agree(harness: Harness):
    """The two views must not disagree about the same project."""
    _, listed = harness.say("现在有哪些项目？")
    row = {item["project_id"]: item for item in listed["project_rows"]}[
        harness.beta["id"]]
    _, single = harness.say("ACP-Beta 有哪些阻塞？")
    blocked_ids = re.findall(r"\bcp-[0-9a-f]{12}\b", single["answer"])
    assert row["blocked"] == 1
    assert harness.beta_blocked in blocked_ids


# --------------------------------------------------------------------------- #
# Task 2: the portfolio answer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "汇总所有项目目前的进度",
    "所有项目有哪些未完成目标？",
    "现在有什么需要我处理",
    "给出各项目的优先级建议",
    "跨项目汇总一下风险",
])
def test_portfolio_request_is_answered_not_downgraded(question, harness: Harness):
    status, body = harness.say(question)
    assert status == 200, body
    assert body["intent"]["intent"] == "portfolio_status", body
    assert body["project_routing"]["source"] == "none"
    assert body["goal_ids"] == []
    assert body["requires_confirmation"] is False
    answer = body["answer"]
    # Every registered project appears, including the empty one.
    for project in (harness.alpha, harness.beta, harness.gamma, harness.delta):
        assert project["id"] in answer, project


def test_portfolio_states_are_separated_in_the_answer(harness: Harness):
    _, body = harness.say("汇总所有项目目前的进度")
    answer = body["answer"]
    # The facts section separates the three kinds of thing.
    assert "阻塞" in answer and "异常验收" in answer and "未完成 Goal" in answer
    # The rejected-but-not-blocked task is described as history, not as blocked.
    alpha_part = answer.split("【ACP-Alpha】")[1].split("【ACP-Beta】")[0]
    assert "阻塞：无" in alpha_part
    assert harness.alpha_rejected in alpha_part
    # The overlap appears once, under blocked, and not again as abnormal.
    beta_part = answer.split("【ACP-Beta】")[1].split("【ACP-Gamma】")[0]
    assert beta_part.count(harness.beta_blocked) == 1, beta_part


def test_portfolio_separates_fact_from_judgement(harness: Harness):
    _, body = harness.say("汇总所有项目目前的进度")
    answer = body["answer"]
    assert "一、事实" in answer
    assert "二、判断" in answer
    # The ranking rule is stated, so the reader can audit the recommendation.
    assert "规则：" in answer
    # A dependency between projects is not recorded in the ledger, and the
    # answer says so instead of inventing one.
    assert "未记录" in answer or "不做跨项目依赖" in answer


def test_portfolio_priority_ordering_is_evidence_based(harness: Harness):
    _, body = harness.say("给出各项目的优先级建议")
    priorities = body["portfolio"]["priorities"]
    ranks = [item["rank"] for item in priorities]
    assert ranks == sorted(ranks), priorities
    # The unknown-state task outranks the budget-exhausted goal.
    first = priorities[0]
    assert first["rank"] == "P1"
    assert first["project_id"] == harness.beta["id"]
    assert harness.beta_blocked in first["why"]
    by_project = {item["project_id"]: item for item in priorities}
    # alpha's exhausted goal is flagged as "do not simply resume"
    assert by_project[harness.alpha["id"]]["rank"] == "P3"
    assert "不要直接恢复" in by_project[harness.alpha["id"]]["basis"] or \
           "再次耗尽" in by_project[harness.alpha["id"]]["why"]


def test_portfolio_states_unknown_instead_of_guessing(harness: Harness):
    _, body = harness.say("汇总所有项目目前的进度")
    unknowns = body["portfolio"]["unknowns"]
    assert unknowns, "an unknown-state dispatch must be declared undecidable"
    assert any(harness.beta_blocked in line for line in unknowns)
    assert "Worker" in unknowns[0] or "未记录" in unknowns[0]


def test_portfolio_numbers_match_the_ledger(harness: Harness):
    """No number in the totals may be invented."""
    _, body = harness.say("汇总所有项目目前的进度")
    totals = body["portfolio"]["totals"]
    all_items = []
    goals = []
    for project in (harness.alpha, harness.beta, harness.gamma, harness.delta,
                    harness.epsilon):
        all_items.extend(harness.work_items(project["id"]))
        goals.extend(harness.goals(project["id"]))
    assert totals["work_items"] == len(all_items)
    assert totals["verified"] == sum(1 for item in all_items
                                     if item.get("verdict") == "verified")
    assert totals["rejected"] == sum(1 for item in all_items
                                     if item.get("verdict") == "rejected")
    assert totals["inconclusive"] == sum(1 for item in all_items
                                         if item.get("verdict") == "inconclusive")
    assert totals["needs_human_goals"] == sum(
        1 for goal in goals if goal["status"] == "needs_human")
    assert totals["unfinished_goals"] == sum(
        1 for goal in goals if goal["status"] not in ("completed", "cancelled",
                                                      "failed"))


def test_portfolio_is_read_only(harness: Harness):
    before = harness.business_snapshot()
    conversation = None
    for question in ("汇总所有项目目前的进度", "所有项目有哪些未完成目标？",
                     "现在有什么需要我处理", "给出各项目的优先级建议"):
        status, body = harness.say(question, conversation)
        assert status == 200, body
        conversation = body["conversation_id"]
        assert body["goal_ids"] == []
        assert body["pending_action"] is None
    after = harness.business_snapshot()
    assert after == before, "a read-only report changed business state"


def test_portfolio_does_not_need_a_project_and_does_not_bind_one(
        harness: Harness):
    _, body = harness.say("汇总所有项目目前的进度")
    conversation_id = body["conversation_id"]
    chats = LDChatStore(harness.ledger)
    try:
        row = chats.get_conversation(conversation_id) or {}
    finally:
        chats.close()
    assert row.get("project_id") is None, (
        "a portfolio turn must not bind the conversation to a project")


def test_naming_a_project_keeps_it_a_single_project_read(harness: Harness):
    """Portfolio detection must not swallow an explicit single-project ask."""
    status, body = harness.say("ACP-Beta 现在进行到哪了？")
    assert status == 200, body
    assert body["intent"]["intent"] in ("project_status", "goal_status",
                                        "explain_blockers"), body
    assert body["project_routing"]["project_id"] == harness.beta["id"]


def test_portfolio_rejects_a_project_id():
    """``portfolio_status`` must not be usable as a single-project report."""
    from ld.chat_intent import ChatPolicyError, parse_intent
    with pytest.raises(ChatPolicyError):
        parse_intent({"intent": "portfolio_status", "project_id": "prj-x",
                      "task_id": None, "goal_id": None,
                      "reason": "test", "parameters": {}})


def test_portfolio_focus_is_a_closed_set():
    from ld.chat_intent import ChatPolicyError, parse_intent
    with pytest.raises(ChatPolicyError):
        parse_intent({"intent": "portfolio_status", "project_id": None,
                      "task_id": None, "goal_id": None,
                      "reason": "test", "parameters": {"focus": "everything"}})


def test_ambiguity_does_not_block_a_portfolio_report(harness: Harness):
    """Two projects named + "汇总" is a portfolio request, not a refusal."""
    status, body = harness.say(
        f"汇总 {harness.alpha['id']} 和 {harness.beta['id']} 的进度")
    assert status == 200, body
    assert body["intent"]["intent"] == "portfolio_status", body
    assert body["goal_ids"] == []


def test_ambiguity_still_refuses_a_single_project_read(harness: Harness):
    """The portfolio exemption must not leak into ordinary reads."""
    status, body = harness.say(
        f"对比 {harness.alpha_rejected} 和 {harness.beta_rejected} 的结果")
    assert status == 200, body
    assert body["project_routing"]["source"] == "ambiguous"
    assert body["intent"]["intent"] == "clarify", body
    assert body["goal_ids"] == []
