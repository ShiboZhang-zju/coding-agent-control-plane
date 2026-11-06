"""UI-4 browser tests: the global LD chat is the dashboard's entry point.

These use Playwright against the real dashboard server with a throwaway ledger,
so what is asserted is what an operator sees and clicks:

* opening the dashboard opens the LD chat — no project has to be selected first;
* the chat works with **no** project chosen (the selector no longer exists);
* a global conversation answers about project A and then project B under one
  conversation id, and the 「当前识别的目标项目」 badge follows the *server's*
  routing decision;
* an ambiguous request produces a question and creates no Goal;
* a written Goal requires an explicit project choice plus a confirmation that
  names that project (and the confirmation dies if the target is switched).

The production Manager and the production ledger are never touched.
"""
from __future__ import annotations

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
from ld.goal_store import LDGoalStore  # noqa: E402

STATE = ROOT / ".poc-state" / "ui4-browser"
TOKEN = "ui4-browser-operator-token-32-chars"

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
        self.ledger = root / "ui4b.db"
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        self.alpha = plane.register_project(
            "ACP-Alpha", str(_git_repo(root / "alpha")), default_adapter="stub")
        self.beta = plane.register_project(
            "ACP-Beta", str(_git_repo(root / "beta")), default_adapter="stub")
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

    def goals_of(self, project_id: str) -> list[dict]:
        goals = LDGoalStore(self.ledger, read_only=True)
        try:
            return goals.find_by_project(project_id)
        finally:
            goals.close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch(channel="msedge", headless=True)
        except Exception:  # noqa: BLE001 - fall back to the bundled browser
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


def _open(browser, site: Site):
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(site.base + "/", wait_until="networkidle")
    if page.is_checked("#autoRefresh"):
        page.uncheck("#autoRefresh")
    return context, page, errors


def _connect(page) -> None:
    """The chat is already open; only the operator token is needed."""
    page.wait_for_selector("#chatPanel:not([hidden])", timeout=15_000)
    page.fill("#chatToken", TOKEN)
    page.click("#chatConnect")
    page.wait_for_selector("#chatComposer:not([hidden])", timeout=15_000)


def _say(page, message: str) -> None:
    with page.expect_response(
            lambda response: "/api/chat/messages" in response.url
            and response.request.method == "POST") as posted:
        page.fill("#chatInput", message)
        page.click("#chatSend")
    assert posted.value.status == 200, posted.value.text()


# --------------------------------------------------------------------------- #
def test_dashboard_opens_with_the_global_chat_and_no_project_choice(
        browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        # 1. The chat is the entry point: it is open before any interaction.
        page.wait_for_selector("#chatPanel:not([hidden])", timeout=15_000)
        assert page.is_visible("#chatInput") or page.is_visible("#chatToken")
        # 2. The project list is still there, but it is no longer a prerequisite
        #    for talking: there is no chat-scoped project selector any more.
        assert page.locator("#chatProject").count() == 1
        assert page.inner_text("#publishBox label[for=chatProject]")
        assert "发布目标项目" in page.inner_text("#publishBox label[for=chatProject]")
        # 3. No conversation exists yet — it starts with the first message.
        assert "尚未创建会话" in page.inner_text("#chatConversationId")
        _connect(page)
        page.wait_for_selector("#chatMessages .placeholder", timeout=15_000)
        assert errors == []
    finally:
        context.close()


def test_global_chat_reads_projects_and_switches_between_them(
        browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        _connect(page)
        _say(page, "现在有哪些项目？")
        page.wait_for_function(
            "() => document.querySelectorAll('#chatMessages article').length >= 2",
            timeout=20_000)
        text = page.inner_text("#chatMessages")
        assert site.alpha["id"] in text and site.beta["id"] in text
        conversation_text = page.inner_text("#chatConversationId")
        assert "会话 ID：conv-" in conversation_text
        conversation_id = conversation_text.split("会话 ID：")[1].strip()

        # Project A, then project B, in the same conversation.
        _say(page, f"{site.alpha['id']} 现在进行到哪了？")
        page.wait_for_function(
            "() => document.getElementById('chatIdentifiedProject')"
            ".textContent.includes('%s')" % site.alpha["id"], timeout=20_000)
        _say(page, f"{site.beta['id']} 有哪些阻塞？")
        page.wait_for_function(
            "() => document.getElementById('chatIdentifiedProject')"
            ".textContent.includes('%s')" % site.beta["id"], timeout=20_000)

        # The conversation id did not change, and the badge is not the *publish*
        # target: nothing was selected for publication, so no mismatch warning
        # can appear, and no Goal was created by reading.
        assert conversation_id in page.inner_text("#chatConversationId")
        assert page.locator("#publishProjectNotice:not([hidden])").count() == 0
        assert site.goals_of(site.alpha["id"]) == []
        assert site.goals_of(site.beta["id"]) == []
        assert errors == []
    finally:
        context.close()


def test_ambiguous_request_asks_and_creates_no_goal(browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        _connect(page)
        _say(page, f"继续推进 {site.alpha['id']} 和 {site.beta['id']} 两个项目")
        page.wait_for_function(
            "() => document.getElementById('chatMessages')"
            ".textContent.includes('多个项目')", timeout=20_000)
        badge = page.inner_text("#chatIdentifiedProject")
        assert "待澄清" in badge
        # No Goal row exists in either project: the turn only asked.
        assert site.goals_of(site.alpha["id"]) == []
        assert site.goals_of(site.beta["id"]) == []
        assert errors == []
    finally:
        context.close()


def test_write_without_a_project_asks_instead_of_creating(browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        _connect(page)
        _say(page, "继续推进：把这个功能做完")
        page.wait_for_function(
            "() => document.getElementById('chatMessages')"
            ".textContent.includes('已注册项目')", timeout=20_000)
        assert "尚未识别" in page.inner_text("#chatIdentifiedProject")
        assert site.goals_of(site.alpha["id"]) == []
        assert site.goals_of(site.beta["id"]) == []
        assert errors == []
    finally:
        context.close()


def test_named_write_needs_explicit_target_and_confirmation(
        browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        _connect(page)
        # Publication (a project-level write) starts with no target chosen.
        assert page.input_value("#chatProject") == ""
        assert page.is_disabled("#publishBtn")

        page.select_option("#chatProject", site.beta["id"])
        page.fill("#publishObjective", "实现 greet.py 并补充测试")
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)
        confirm = page.inner_text("#publishConfirm")
        assert "ACP-Beta" in confirm and site.beta["id"] in confirm
        assert "执行仓库路径" in confirm
        # Nothing exists until the confirm button is clicked.
        assert site.goals_of(site.beta["id"]) == []

        page.click("#publishConfirmBtn")
        page.wait_for_selector("#publishResult:not([hidden])", timeout=20_000)
        beta_goals = site.goals_of(site.beta["id"])
        assert [goal["goal_text"] for goal in beta_goals] == [
            "实现 greet.py 并补充测试"]
        assert site.goals_of(site.alpha["id"]) == []
        assert errors == []
    finally:
        context.close()


def test_switching_the_target_invalidates_the_pending_confirmation(
        browser, site: Site):
    context, page, errors = _open(browser, site)
    try:
        _connect(page)
        page.select_option("#chatProject", site.alpha["id"])
        page.fill("#publishObjective", "实现 greet.py")
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)

        page.select_option("#chatProject", site.beta["id"])
        page.wait_for_function(
            "() => document.getElementById('publishConfirm').hidden")
        # Re-confirming shows the *new* project.
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)
        assert "ACP-Beta" in page.inner_text("#publishConfirm")
        page.click("#publishConfirmBtn")
        page.wait_for_selector("#publishResult:not([hidden])", timeout=20_000)
        assert site.goals_of(site.alpha["id"]) == []
        assert [goal["goal_text"] for goal in site.goals_of(site.beta["id"])] == [
            "实现 greet.py"]
        assert errors == []
    finally:
        context.close()
