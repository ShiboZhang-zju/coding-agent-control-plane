"""UI-2: handling a ``needs_human`` goal, and publishing to a named project.

What this file proves (deterministic, no browser):

* a stopped goal has a *read-only* handling projection: stop reason, the
  WorkItems the LD actually submitted for it, the Verifier's assertion results
  and the Ledger evidence row ids — with no prompt, task text or project root;
* a ``rejected`` task is shown with its failed assertions, and opening the view
  neither re-dispatches nor changes any verdict;
* a resume is only *staged* for the goal id the caller named, requires an exact
  confirm turn, and never touches the lifetime budget;
* with several ``needs_human`` goals alive at once, the staged action is bound
  to the requested goal — never "the first live goal of the project";
* the publication target list is authenticated and exposes exactly
  name/id/root/active, and the back end still refuses an unknown project.

The browser-level guarantees (the actual button is visible/clickable, the
confirmation dialog, project-switch invalidation) live in
``tests/test_ui2_browser.py`` and use a real DOM; they are deliberately not
approximated with HTTP calls here.
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

from adapters.base import ArtifactAssertion, TaskState  # noqa: E402
from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402

STATE = ROOT / ".poc-state" / "ui2-tests"
TOKEN = "ui2-test-operator-token-32-characters"

SECRET_PROMPT = "SECRET-EFFECTIVE-PROMPT-must-not-appear-in-handling"


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Harness:
    """One throw-away ledger, one loopback server, two stopped goals."""

    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui2.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ui2-alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ui2-beta", str(_git_repo(root / "beta")), default_adapter="stub")
        store.close()

        goals = LDGoalStore(self.ledger)
        # Two stopped goals in the same project: a resume must hit the requested
        # one, not "the first needs_human goal".
        self.first_goal_id, self.first_ref = goal_identity(
            "repair the first stopped goal", self.alpha["id"])
        self.second_goal_id, self.second_ref = goal_identity(
            "repair the second stopped goal", self.alpha["id"])
        for goal_id, ref, text in (
                (self.first_goal_id, self.first_ref, "repair the first stopped goal"),
                (self.second_goal_id, self.second_ref, "repair the second stopped goal")):
            goals.get_or_create(goal_id, self.alpha["id"], text, ref)
            goals.ensure_lifetime_budget(goal_id, max_decisions=5, max_tasks=2)
            goals.update_goal(goal_id, status="needs_human",
                              last_decision={"stop": "human review required",
                                             "action": "submit_task",
                                             "next_state": "needs_human"})
        goals.close()

        self._seed_task()
        bridge = ChatBridge(self.ledger)
        self.server = make_server(self.ledger, host="127.0.0.1", port=0,
                                  enable_chat=True, chat_token=TOKEN,
                                  chat_bridge=bridge)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()

    def _seed_task(self) -> None:
        """A rejected WorkItem linked to the *second* goal, with real evidence."""
        store = TaskStore(self.ledger)
        try:
            plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
            task = plane.enqueue(
                SECRET_PROMPT, str(self.root / "alpha"),
                assertions=[ArtifactAssertion("file_exists", "report.md")],
                project_id=self.alpha["id"],
                metadata={"ld_goal_ref": self.second_ref, "submitted_by": "ld"})
            self.task_id = task["task_id"]
            store.update(
                self.task_id,
                state=TaskState.FAILED.value, verdict="rejected",
                queue_status="complete",
                assertion_results=[{"kind": "file_exists", "target": "report.md",
                                    "value": None, "passed": False, "owned": False,
                                    "observed": "missing"}],
                warnings=["AN_ASSERTION_FAILED: report.md missing",
                          "SIDE_EFFECT:undeclared_local_change:C:\\elsewhere#abcd1234"])
            store.add_evidence(self.task_id, "verifier", {"detail": "no artifact"})
        finally:
            store.close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str, *, method: str = "GET", payload=None,
                token: str | None = TOKEN, origin: str | None = "same"
                ) -> tuple[int, dict]:
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
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def harness():
    item = Harness()
    try:
        yield item
    finally:
        item.close()


def _goal(path: Path, goal_id: str) -> dict:
    goals = LDGoalStore(path, read_only=True)
    try:
        return goals.get(goal_id) or {}
    finally:
        goals.close()


# --------------------------------------------------------------------------- #
# handling projection
# --------------------------------------------------------------------------- #
def test_handling_view_reports_stop_reason_tasks_verifier_and_evidence(
        harness: Harness):
    status, body = harness.request(
        f"/api/projects/{harness.alpha['id']}/goals/{harness.second_goal_id}/handling")
    assert status == 200
    assert body["project_id"] == harness.alpha["id"]
    assert body["goal"]["goal_id"] == harness.second_goal_id
    assert body["stop_reason"] == "human review required"
    assert body["linked_task_count"] == 1

    task = body["tasks"][0]
    assert task["task_id"] == harness.task_id
    assert task["verdict"] == "rejected"
    assert task["failed_assertions"] == [{
        "kind": "file_exists", "target": "report.md",
        "value": None, "observed": "missing"}]
    assert task["evidence_count"] == 1
    assert task["evidence"][0]["kind"] == "verifier"
    assert isinstance(task["evidence"][0]["evidence_id"], int)
    # Warnings are deduplicated but distinct ones survive.
    assert len(task["warnings"]) == len(set(task["warnings"])) == 2

    blob = json.dumps(body, ensure_ascii=False, default=str)
    for needle in (SECRET_PROMPT, "effective_prompt", str(harness.root), "prompt"):
        assert needle not in blob, needle


def test_handling_view_never_redispatches_or_changes_a_verdict(harness: Harness):
    store = TaskStore(harness.ledger, read_only=True)
    try:
        before = store.get(harness.task_id)
        tasks_before = len(store.work_items_for_project(harness.alpha["id"]))
    finally:
        store.close()

    for _ in range(3):
        status, _ = harness.request(
            f"/api/projects/{harness.alpha['id']}/goals/"
            f"{harness.second_goal_id}/handling")
        assert status == 200

    store = TaskStore(harness.ledger, read_only=True)
    try:
        after = store.get(harness.task_id)
        assert len(store.work_items_for_project(harness.alpha["id"])) == tasks_before
    finally:
        store.close()
    for field in ("state", "verdict", "queue_status", "updated_at"):
        assert before[field] == after[field], field


def test_handling_view_rejects_unknown_and_cross_project_goal(harness: Harness):
    status, body = harness.request(
        f"/api/projects/{harness.alpha['id']}/goals/goal-does-not-exist/handling")
    assert status == 404 and body["error"]["code"] == "goal_not_found"
    # A beta goal is not readable through alpha's path.
    status, body = harness.request(
        f"/api/projects/{harness.beta['id']}/goals/{harness.second_goal_id}/handling")
    assert status == 404


# --------------------------------------------------------------------------- #
# staged resume
# --------------------------------------------------------------------------- #
def test_resume_is_staged_for_the_named_goal_only(harness: Harness):
    status, body = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"],
                 "goal_id": harness.second_goal_id})
    assert status == 200
    assert body["requires_confirmation"] is True
    assert body["pending_action"]["kind"] == "resume_goal"
    assert body["pending_action"]["goal_id"] == harness.second_goal_id
    assert body["goal_ids"] == [harness.second_goal_id]
    conversation_id = body["conversation_id"]

    # Nothing has been resumed yet: both goals are still paused, budget intact.
    for goal_id in (harness.first_goal_id, harness.second_goal_id):
        goal = _goal(harness.ledger, goal_id)
        assert goal["status"] == "needs_human"
        assert goal["max_decisions_total"] == 5
        assert goal["max_tasks_total"] == 2
        assert goal["resume_count"] == 0

    # Opening the handling view again and staging the *other* goal in a new
    # conversation restages, it does not resume the first one.
    status, other = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"],
                 "goal_id": harness.first_goal_id})
    assert status == 200
    assert other["pending_action"]["goal_id"] == harness.first_goal_id
    assert other["conversation_id"] != conversation_id
    assert _goal(harness.ledger, harness.second_goal_id)["status"] == "needs_human"
    assert _goal(harness.ledger, harness.first_goal_id)["status"] == "needs_human"


def test_confirm_resumes_exactly_the_staged_goal(harness: Harness):
    _, staged = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"],
                 "goal_id": harness.second_goal_id})
    conversation_id = staged["conversation_id"]

    status, confirmed = harness.request(
        "/api/chat/messages", method="POST",
        payload={"conversation_id": conversation_id,
                 "project_id": harness.alpha["id"], "message": "confirm"})
    assert status == 200
    assert confirmed["intent_source"] == "human_confirmation"
    assert confirmed["facts"]["resumed"] is True

    resumed = _goal(harness.ledger, harness.second_goal_id)
    untouched = _goal(harness.ledger, harness.first_goal_id)
    assert resumed["status"] == "active"
    assert resumed["resume_count"] == 1
    # Budget is never enlarged by a resume.
    assert resumed["max_decisions_total"] == 5
    assert resumed["max_tasks_total"] == 2
    # The *other* stopped goal is untouched — this is the bug that matters.
    assert untouched["status"] == "needs_human"
    assert untouched["resume_count"] == 0


def test_cancel_leaves_the_goal_stopped(harness: Harness):
    _, staged = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"],
                 "goal_id": harness.second_goal_id})
    status, cancelled = harness.request(
        "/api/chat/messages", method="POST",
        payload={"conversation_id": staged["conversation_id"],
                 "project_id": harness.alpha["id"], "message": "cancel"})
    assert status == 200
    goal = _goal(harness.ledger, harness.second_goal_id)
    assert goal["status"] == "needs_human"
    assert goal["resume_count"] == 0
    assert cancelled["intent_source"] == "human_confirmation_denied"


def test_staging_a_non_paused_goal_is_refused(harness: Harness):
    goals = LDGoalStore(harness.ledger)
    try:
        goals.update_goal(harness.first_goal_id, status="active",
                          last_decision={"action": "inspect"})
    finally:
        goals.close()
    status, body = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"],
                 "goal_id": harness.first_goal_id})
    assert status == 200
    assert body["requires_confirmation"] is False
    assert "not" in body["answer"] and "needs_human" in body["answer"]
    assert _goal(harness.ledger, harness.first_goal_id)["status"] == "active"


def test_resume_request_validates_project_and_goal_ownership(harness: Harness):
    # Alpha's goal cannot be resumed through beta.
    status, body = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.beta["id"],
                 "goal_id": harness.second_goal_id})
    assert status == 400 and body["error"]["code"] == "resume_request_rejected"
    assert _goal(harness.ledger, harness.second_goal_id)["status"] == "needs_human"

    status, body = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": "prj-does-not-exist",
                 "goal_id": harness.second_goal_id})
    assert status == 400 and body["error"]["code"] == "invalid_reference"

    status, body = harness.request(
        "/api/chat/goal-resume", method="POST",
        payload={"project_id": harness.alpha["id"], "goal_id": "goal-missing"})
    assert status == 400 and body["error"]["code"] == "resume_request_rejected"

    # Extra fields are refused, not ignored: no budget, task or force flag.
    for extra in ({"max_decisions": 99}, {"max_tasks": 9},
                  {"force": True}, {"message": "confirm"}):
        payload = {"project_id": harness.alpha["id"],
                   "goal_id": harness.second_goal_id}
        payload.update(extra)
        status, body = harness.request("/api/chat/goal-resume", method="POST",
                                       payload=payload)
        assert status == 400 and body["error"]["code"] == "invalid_fields", extra
    goal = _goal(harness.ledger, harness.second_goal_id)
    assert goal["status"] == "needs_human"
    assert goal["max_decisions_total"] == 5 and goal["max_tasks_total"] == 2


def test_resume_requires_auth_origin_and_chat_enabled(harness: Harness):
    payload = {"project_id": harness.alpha["id"],
               "goal_id": harness.second_goal_id}
    status, body = harness.request("/api/chat/goal-resume", method="POST",
                                   payload=payload, token=None)
    assert status == 401 and body["error"]["code"] == "unauthorized"
    status, body = harness.request("/api/chat/goal-resume", method="POST",
                                   payload=payload,
                                   origin="https://evil.example")
    assert status == 403 and body["error"]["code"] == "bad_origin"

    server = make_server(harness.ledger, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/chat/goal-resume",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)
        assert excinfo.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert _goal(harness.ledger, harness.second_goal_id)["status"] == "needs_human"


def test_read_only_dashboard_still_exposes_no_project_root(harness: Harness):
    status, body = harness.request("/api/overview", token=None, origin=None)
    assert status == 200
    for row in body["projects"]:
        assert "root" not in row and "local_root" not in row


# --------------------------------------------------------------------------- #
# publication target (P0-2 backend half)
# --------------------------------------------------------------------------- #
def test_publish_targets_require_auth_and_expose_only_routing_fields(
        harness: Harness):
    status, body = harness.request("/api/chat/projects", token=None, origin=None)
    assert status == 401 and body["error"]["code"] == "unauthorized"

    status, body = harness.request("/api/chat/projects")
    assert status == 200
    rows = {row["project_id"]: row for row in body["projects"]}
    assert set(rows) == {harness.alpha["id"], harness.beta["id"]}
    for row in rows.values():
        assert set(row) == {"project_id", "name", "root", "active"}
    assert Path(rows[harness.alpha["id"]]["root"]).name == "alpha"


def test_publish_still_refuses_an_unregistered_project(harness: Harness):
    status, body = harness.request(
        "/api/chat/publish", method="POST",
        payload={"project_id": "prj-does-not-exist",
                 "objective": "implement greet.py"})
    assert status == 400 and body["error"]["code"] == "invalid_reference"
    goals = LDGoalStore(harness.ledger, read_only=True)
    try:
        assert [row["goal_id"] for row in goals.find_by_project(harness.alpha["id"])]
    finally:
        goals.close()
