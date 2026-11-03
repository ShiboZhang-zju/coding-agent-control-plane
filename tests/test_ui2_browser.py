"""UI-2 browser tests: real DOM, real HTTP, isolated ledger, test port.

These use Playwright (driving the locally installed Edge/Chromium) against the
real dashboard server, so what is asserted is what an operator actually clicks:
a visible 「处理」 entry on a ``needs_human`` goal, a second confirmation before a
resume takes effect, correct targeting when several stopped goals exist, and a
publication confirmation that cannot survive a project switch.

They are deliberately *not* HTTP-level tests: "the button exists and does what
it says" is exactly what an HTTP test cannot prove.  If no Playwright browser is
available the module skips with an explicit reason instead of pretending.

The production Manager (``C:\\ACP\\acp-manager``) is never contacted: every test
builds its own ledger under ``.poc-state/`` and binds ``127.0.0.1:0``.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controlplane.plane import ControlPlane  # noqa: E402
from controlplane.store import TaskStore  # noqa: E402
from dashboard.api import make_server  # noqa: E402
from dashboard.chat import ChatBridge  # noqa: E402
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402

STATE = ROOT / ".poc-state" / "ui2-browser"
TOKEN = "ui2-browser-operator-token-32-chars"

pytestmark = pytest.mark.smoke

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed")


class _Adapter:
    name = "stub"


def _git_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return path


class Site:
    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui2b.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Development", str(_git_repo(root / "acp-development")),
            default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-SelfTest", str(_git_repo(root / "acp-selftest")),
            default_adapter="stub")
        store.close()

        goals = LDGoalStore(self.ledger)
        # Two stopped goals in the same project: the dialog must open, and later
        # resume, the one that was clicked.
        self.first_id, first_ref = goal_identity("repair the first goal",
                                                 self.alpha["id"])
        self.second_id, self.second_ref = goal_identity("repair the second goal",
                                                        self.alpha["id"])
        for goal_id, ref, text, reason in (
                (self.first_id, first_ref, "repair the first goal",
                 "first pause: awaiting a human decision"),
                (self.second_id, self.second_ref, "repair the second goal",
                 "second pause: assertion never produced the artifact")):
            goals.get_or_create(goal_id, self.alpha["id"], text, ref)
            goals.ensure_lifetime_budget(goal_id, max_decisions=6, max_tasks=2)
            goals.update_goal(goal_id, status="needs_human",
                              last_decision={"stop": reason,
                                             "action": "submit_task",
                                             "next_state": "needs_human"})
        goals.close()

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


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch(channel="msedge", headless=True)
        except Exception:  # noqa: BLE001 - fall back to a bundled browser
            try:
                engine = playwright.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 - no browser at all
                pytest.skip(f"no Playwright browser available: {exc}")
        try:
            yield engine
        finally:
            engine.close()


@pytest.fixture()
def site():
    item = Site()
    try:
        yield item
    finally:
        item.close()


def _page(browser, site: Site, *, project_id: str | None = None):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    target = site.base + "/"
    if project_id:
        target += "#project=" + project_id
    page.goto(target, wait_until="networkidle")
    # The dashboard re-renders every 10s.  That is correct product behaviour, but
    # it detaches the row being clicked mid-test, so the interval is switched off
    # through its own control (an operator can do the same) and every assertion
    # then describes a stable DOM.
    if page.is_checked("#autoRefresh"):
        page.uncheck("#autoRefresh")
    return context, page, errors


def _connect(page, site: Site, *, keep_open: bool = False) -> None:
    page.click("#chatToggle")
    page.fill("#chatToken", TOKEN)
    page.click("#chatConnect")
    page.wait_for_selector("#chatControls:not([hidden])", timeout=10_000)
    if not keep_open:
        # The chat drawer is a fixed overlay; close it before working in the
        # table, which is what an operator does too (the token is kept in
        # sessionStorage, so the panel reopens already connected).
        page.click("#chatClose")
        page.wait_for_function("() => document.getElementById('chatPanel').hidden")


def _goal_row(page, goal_id: str):
    return page.locator("tr", has=page.locator(f'code[title="goal_id: {goal_id}"]'))


# --------------------------------------------------------------------------- #
# P0-1: the handling entry
# --------------------------------------------------------------------------- #
def test_needs_human_goal_has_a_clickable_handling_entry(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        # The detail pane is rendered after the detail response arrives; wait for
        # the control itself instead of assuming the table is already built.
        page.wait_for_selector(
            'button.goal-handle-btn[data-goal-id="%s"]' % site.second_id,
            state="visible", timeout=15_000)
        row = _goal_row(page, site.second_id)
        button = row.locator("button.goal-handle-btn")
        assert button.is_visible(), "needs_human goal must expose a visible 处理 button"
        assert button.inner_text().strip() == "处理"
        assert button.get_attribute("data-goal-id") == site.second_id

        button.click()
        page.wait_for_selector("#goalHandling:not([hidden])", timeout=10_000)
        # The dialog opens immediately with a loading placeholder; the projection
        # arrives a moment later.  Wait for the rendered detail, not the shell.
        page.wait_for_selector("#goalHandlingBody dl.kv-grid", timeout=15_000)
        body = page.inner_text("#goalHandlingBody")
        # The dialog is about the goal that was clicked, not the first one.
        assert site.second_id in body
        assert site.first_id not in body
        assert "second pause" in body
        assert "ACP-Development" in body
        assert errors == []
    finally:
        context.close()


def test_handling_dialog_shows_verifier_result_and_failure_evidence(
        browser, site: Site):
    # A rejected WorkItem, stamped with the second goal's ref.
    store = TaskStore(site.ledger)
    try:
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        task = plane.enqueue(
            "produce report.md", str(site.root / "acp-development"),
            assertions=[], project_id=site.alpha["id"],
            metadata={"ld_goal_ref": site.second_ref, "submitted_by": "ld"})
        store.update(task["task_id"], state="failed", verdict="rejected",
                     queue_status="complete",
                     assertion_results=[{"kind": "file_exists", "target": "report.md",
                                         "value": None, "passed": False,
                                         "owned": False, "observed": "missing"}])
        store.add_evidence(task["task_id"], "verifier", {"detail": "no artifact"})
        task_id = task["task_id"]
    finally:
        store.close()

    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _goal_row(page, site.second_id).locator("button.goal-handle-btn").click()
        page.wait_for_selector("#goalHandling:not([hidden])", timeout=10_000)
        page.wait_for_selector(f'.goal-task-card[data-task-id="{task_id}"]',
                               timeout=10_000)
        card = page.locator(f'.goal-task-card[data-task-id="{task_id}"]')
        text = card.inner_text()
        assert "rejected" in text
        assert "file_exists report.md" in text
        assert "missing" in text
        # Opening the dialog never re-dispatches: the verdict is unchanged.
        assert "Ledger evidence 1" in text
        assert errors == []
    finally:
        context.close()

    store = TaskStore(site.ledger, read_only=True)
    try:
        assert store.get(task_id)["verdict"] == "rejected"
    finally:
        store.close()


def test_resume_requires_second_confirmation_and_cancel_changes_nothing(
        browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _connect(page, site)
        _goal_row(page, site.second_id).locator("button.goal-handle-btn").click()
        page.wait_for_selector("#goalHandling:not([hidden])", timeout=10_000)
        # 处理 must be readable before anything is staged: the button stays
        # disabled until the projection has rendered.
        page.wait_for_selector("#goalHandlingStage:not([disabled])", timeout=15_000)
        with page.expect_response(
                lambda response: "/api/chat/goal-resume" in response.url
                and response.request.method == "POST") as staged:
            page.click("#goalHandlingStage")
        assert staged.value.status == 200, staged.value.text()
        page.wait_for_selector("#goalHandlingConfirm:not([hidden])",
                               timeout=30_000)
        confirm_text = page.inner_text("#goalHandlingConfirmText")
        assert site.second_id in confirm_text

        # Staging alone must not resume anything.
        goals = LDGoalStore(site.ledger, read_only=True)
        try:
            assert goals.get(site.second_id)["status"] == "needs_human"
        finally:
            goals.close()

        # The cancel turn is a real round trip; wait for it instead of polling.
        with page.expect_response(
                lambda response: "/api/chat/messages" in response.url
                and response.request.method == "POST") as posted:
            page.click("#goalHandlingCancelBtn")
        assert posted.value.status == 200, posted.value.text()
        page.wait_for_function(
            "() => document.getElementById('goalHandlingConfirm').hidden")
        page.wait_for_function(
            "() => document.getElementById('goalHandlingStatus')"
            ".textContent.length > 0", timeout=30_000)
        goals = LDGoalStore(site.ledger, read_only=True)
        try:
            goal = goals.get(site.second_id)
            assert goal["status"] == "needs_human"
            assert goal["resume_count"] == 0
            assert goal["max_decisions_total"] == 6
            assert goals.get(site.first_id)["status"] == "needs_human"
        finally:
            goals.close()
        status = page.inner_text("#goalHandlingStatus")
        assert "取消" in status
        assert errors == []
    finally:
        context.close()


def test_confirmation_resumes_only_the_clicked_goal(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _connect(page, site)
        _goal_row(page, site.second_id).locator("button.goal-handle-btn").click()
        page.wait_for_selector("#goalHandling:not([hidden])", timeout=10_000)
        page.wait_for_selector("#goalHandlingStage:not([disabled])", timeout=15_000)
        with page.expect_response(
                lambda response: "/api/chat/goal-resume" in response.url
                and response.request.method == "POST") as staged:
            page.click("#goalHandlingStage")
        assert staged.value.status == 200, staged.value.text()
        page.wait_for_selector("#goalHandlingConfirm:not([hidden])",
                               timeout=30_000)
        # Wait for the actual confirm round trip instead of a fixed poll window:
        # under a full-suite run the same tab can be busy with other refreshes.
        with page.expect_response(
                lambda response: "/api/chat/messages" in response.url
                and response.request.method == "POST") as posted:
            page.click("#goalHandlingConfirmBtn")
        assert posted.value.status == 200, posted.value.text()
        page.wait_for_function(
            "() => document.getElementById('goalHandlingStatus')"
            ".textContent.includes('已恢复')", timeout=30_000)

        goals = LDGoalStore(site.ledger, read_only=True)
        try:
            resumed = goals.get(site.second_id)
            untouched = goals.get(site.first_id)
        finally:
            goals.close()
        assert resumed["status"] == "active"
        assert resumed["resume_count"] == 1
        assert resumed["max_decisions_total"] == 6
        assert resumed["max_tasks_total"] == 2
        assert untouched["status"] == "needs_human"
        assert untouched["resume_count"] == 0
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# P0-2: publication target
# --------------------------------------------------------------------------- #
def test_publish_requires_confirmation_and_shows_target(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _connect(page, site, keep_open=True)
        page.select_option("#chatProject", site.alpha["id"])
        page.fill("#publishObjective", "implement greet.py")
        page.click("#publishBtn")

        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)
        text = page.inner_text("#publishConfirm")
        assert "ACP-Development" in text
        assert site.alpha["id"] in text
        assert "acp-development" in text, "the execution repository path must be shown"
        # Nothing has been published yet: it is a confirmation, not a submit.
        goals = LDGoalStore(site.ledger, read_only=True)
        try:
            texts = [goal["goal_text"] for goal in goals.find_by_project(site.alpha["id"])]
        finally:
            goals.close()
        assert "implement greet.py" not in texts

        page.click("#publishConfirmBtn")
        page.wait_for_selector("#publishResult:not([hidden])", timeout=15_000)
        goals = LDGoalStore(site.ledger, read_only=True)
        try:
            texts = [goal["goal_text"] for goal in goals.find_by_project(site.alpha["id"])]
        finally:
            goals.close()
        assert "implement greet.py" in texts
        assert errors == []
    finally:
        context.close()


def test_switching_project_invalidates_the_pending_confirmation(
        browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _connect(page, site, keep_open=True)
        page.select_option("#chatProject", site.alpha["id"])
        page.fill("#publishObjective", "implement greet.py")
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)

        # Switch the publication target to the other project.
        page.select_option("#chatProject", site.beta["id"])
        page.wait_for_function(
            "() => document.getElementById('publishConfirm').hidden")
        assert page.input_value("#publishObjective") == "implement greet.py"

        # Clicking 发布 again must re-confirm against the *new* target.
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)
        text = page.inner_text("#publishConfirm")
        assert "ACP-SelfTest" in text
        assert site.beta["id"] in text
        assert "acp-selftest" in text

        page.click("#publishConfirmBtn")
        page.wait_for_selector("#publishResult:not([hidden])", timeout=15_000)
        alpha = LDGoalStore(site.ledger, read_only=True)
        try:
            alpha_texts = [goal["goal_text"] for goal in alpha.find_by_project(site.alpha["id"])]
            beta_texts = [goal["goal_text"] for goal in alpha.find_by_project(site.beta["id"])]
        finally:
            alpha.close()
        assert "implement greet.py" not in alpha_texts, "must not publish to the old project"
        assert "implement greet.py" in beta_texts
        assert errors == []
    finally:
        context.close()


def test_project_mismatch_is_announced(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=10_000)
        _connect(page, site, keep_open=True)
        page.select_option("#chatProject", site.beta["id"])
        page.wait_for_selector("#publishProjectNotice:not([hidden])", timeout=10_000)
        notice = page.inner_text("#publishProjectNotice")
        assert "不一致" in notice
        assert site.alpha["id"] in notice and site.beta["id"] in notice

        # Viewing the project the chat already targets clears the warning.
        page.select_option("#chatProject", site.alpha["id"])
        page.wait_for_function(
            "() => document.getElementById('publishProjectNotice').hidden")
        assert errors == []
    finally:
        context.close()
