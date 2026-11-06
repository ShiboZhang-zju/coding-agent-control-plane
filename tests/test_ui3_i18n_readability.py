"""UI 验收：人工处理弹窗可读性 + 全局中文化。

这些测试用 Playwright 驱动本机真实浏览器，断言的是**计算样式**和**渲染后的
DOM 文本**，不是 CSS 文件的字符串。只读 CSS 源码无法证明弹窗不透明，也无法
证明某个标签真的显示成中文。

隔离性：每个测试自建 Ledger（``.poc-state/ui3-browser/<uuid>``），
``127.0.0.1:0`` 随机端口，从不接触生产 Manager、生产 Ledger 或 8766/8789。
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
from ld.goal_store import LDGoalStore, goal_identity  # noqa: E402

STATE = ROOT / ".poc-state" / "ui3-browser"
TOKEN = "ui3-browser-operator-token-32-chars"
SHOTS = ROOT / "evidence" / "ui" / "ui3"

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
    """一套独立的 Ledger + Dashboard（含一个 needs_human 目标）。"""

    def __init__(self) -> None:
        root = STATE / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.ledger = root / "ui3.db"
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
        self.first_id, first_ref = goal_identity("修复第一个目标", self.alpha["id"])
        self.second_id, second_ref = goal_identity("修复第二个目标", self.alpha["id"])
        for goal_id, ref, text, reason in (
                (self.first_id, first_ref, "修复第一个目标",
                 "第一次暂停：等待人工决策"),
                (self.second_id, second_ref, "修复第二个目标",
                 "第二次暂停：断言从未产出产物")):
            goals.get_or_create(goal_id, self.alpha["id"], text, ref)
            goals.ensure_lifetime_budget(goal_id, max_decisions=6, max_tasks=2)
            goals.update_goal(goal_id, status="needs_human",
                              last_decision={"stop": reason,
                                             "action": "submit_task",
                                             "next_state": "needs_human"})
        goals.close()

        # 一个被拒的 WorkItem，用来验证弹窗里的 Verifier 失败详情可读。
        store = TaskStore(self.ledger)
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        task = plane.enqueue(
            "produce report.md", str(root / "acp-development"),
            assertions=[], project_id=self.alpha["id"],
            metadata={"ld_goal_ref": second_ref, "submitted_by": "ld"})
        store.update(task["task_id"], state="failed", verdict="rejected",
                     queue_status="complete",
                     assertion_results=[{"kind": "file_exists",
                                         "target": "report.md", "value": None,
                                         "passed": False,
                                         "observed": "missing"}],
                     warnings=["PERMISSION_DENIED: 非交互模式下无法审批"])
        self.task_id = task["task_id"]
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


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch(channel="msedge", headless=True)
        except Exception:  # noqa: BLE001
            try:
                engine = playwright.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
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


def _page(browser, site: Site, *, theme: str = "light", width: int = 1440,
          height: int = 1000, project_id: str | None = None):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    target = site.base + "/"
    if project_id:
        target += "#project=" + project_id
    page.goto(target, wait_until="networkidle")
    # 冻结自动刷新，否则每 10s 重渲染会打断断言。
    if page.is_checked("#autoRefresh"):
        page.uncheck("#autoRefresh")
    page.evaluate("(t) => { document.documentElement.setAttribute('data-theme', t); }",
                  theme)
    return context, page, errors


def _open_handling(page, site: Site, goal_id: str) -> None:
    page.goto(site.base + "/#project=" + site.alpha["id"], wait_until="networkidle")
    if page.is_checked("#autoRefresh"):
        page.uncheck("#autoRefresh")
    page.wait_for_selector('#detailView:not([hidden])', timeout=15_000)
    page.wait_for_selector('button.goal-handle-btn[data-goal-id="%s"]' % goal_id,
                          state="visible", timeout=20_000)
    page.click('button.goal-handle-btn[data-goal-id="%s"]' % goal_id)
    page.wait_for_selector("#goalHandling:not([hidden])", timeout=15_000)
    page.wait_for_selector("#goalHandlingBody dl.kv-grid", timeout=20_000)


# --------------------------------------------------------------------------- #
# 1. 弹窗背景真的不透明（计算样式），且明暗主题都可读
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_handling_dialog_background_is_opaque(browser, site: Site, theme: str):
    context, page, errors = _page(browser, site, theme=theme)
    try:
        _open_handling(page, site, site.second_id)
        style = page.evaluate(
            """() => {
                 const card = document.querySelector('#goalHandling .modal-card');
                 const cs = getComputedStyle(card);
                 return { bg: cs.backgroundColor, opacity: cs.opacity,
                          color: cs.color, visibility: cs.visibility };
               }""")
        # 关键断言：不能是 transparent / rgba(0,0,0,0) / 半透明。
        bg = style["bg"]
        assert bg not in ("transparent", "rgba(0, 0, 0, 0)"), \
            f"modal background is not opaque: {bg}"
        assert "rgba" not in bg or bg.rstrip().endswith(", 1)"), \
            f"modal background is semi-transparent: {bg}"
        assert style["opacity"] == "1"
        assert style["visibility"] == "visible"
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_handling_dialog_text_meets_contrast_floor(browser, site: Site, theme: str):
    """用浏览器解析后的实际颜色计算对比度，而不是读 CSS 文本。"""
    context, page, errors = _page(browser, site, theme=theme)
    try:
        _open_handling(page, site, site.second_id)
        result = page.evaluate(
            """() => {
                 function parse(c) {
                   const m = c.match(/[\\d.]+/g).map(Number);
                   return m.slice(0, 3);
                 }
                 function lum(rgb) {
                   const f = (v) => { v = v / 255;
                     return v <= 0.03928 ? v / 12.92
                                         : Math.pow((v + 0.055) / 1.055, 2.4); };
                   return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1])
                        + 0.0722 * f(rgb[2]);
                 }
                 function ratio(a, b) {
                   const la = lum(a), lb = lum(b);
                   const hi = Math.max(la, lb), lo = Math.min(la, lb);
                   return (hi + 0.05) / (lo + 0.05);
                 }
                 const card = document.querySelector('#goalHandling .modal-card');
                 const cardBg = parse(getComputedStyle(card).backgroundColor);
                 const out = {};
                 const probes = {
                   body: '#goalHandlingBody',
                   kv_label: '#goalHandlingBody .kv dt',
                   kv_value: '#goalHandlingBody .kv dd',
                   section: '#goalHandlingBody .modal-section h3',
                   modal_head: '#goalHandling .modal-head h2',
                 };
                 for (const [key, sel] of Object.entries(probes)) {
                   const node = document.querySelector(sel);
                   if (!node) { out[key] = null; continue; }
                   const fg = parse(getComputedStyle(node).color);
                   out[key] = Number(ratio(fg, cardBg).toFixed(2));
                 }
                 return out;
               }""")
        checked = {k: v for k, v in result.items() if v is not None}
        assert checked, result
        low = {k: v for k, v in checked.items() if v < 4.5}
        assert not low, f"{theme} 主题下弹窗文字对比度不足 4.5:1：{low}（全部：{checked}）"
        assert errors == []
    finally:
        context.close()


def test_handling_dialog_shows_distinct_stop_state_and_verdict(browser, site: Site):
    """停止原因 / 执行状态 / 验收结论必须分开呈现，不能混成一句“失败”。"""
    context, page, errors = _page(browser, site)
    try:
        _open_handling(page, site, site.second_id)
        body = page.inner_text("#goalHandlingBody")
        assert "停止原因" in body
        assert "第二次暂停" in body, body[:400]
        # 执行状态与验收结论用的是不同中文词，且原始值仍可见。
        assert "执行状态" in body
        assert "验收结论" in body
        assert "执行失败" in body, "taskState=failed 应显示为“执行失败”"
        assert "验收未通过" in body, "verdict=rejected 应显示为“验收未通过”"
        # 证据原文不得被翻译改写。
        assert "PERMISSION_DENIED" in body
        assert "file_exists" in body
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 2. 主要界面标签都是中文
# --------------------------------------------------------------------------- #
def test_main_labels_are_chinese(browser, site: Site):
    context, page, errors = _page(browser, site)
    try:
        summary = page.inner_text("#summaryGrid")
        for word in ("项目总数", "活跃", "待人工处理", "验收未通过", "结果不确定"):
            assert word in summary, f"摘要区缺少中文标签 {word}：{summary}"
        nav = page.inner_text("#projectNav")
        assert "全部项目" in nav
        assert "个任务" in nav
        assert "已启用" in nav, f"Project.active 应显示“已启用”：{nav}"
        table = page.inner_text(".table-projects")
        for word in ("工作项目", "当前", "目标状态", "最近活动"):
            assert word in table, f"项目表格缺少中文表头 {word}"
        assert "无目标" in table or "目标" in table
        assert errors == []
    finally:
        context.close()


def test_project_detail_labels_are_chinese(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=15_000)
        page.wait_for_selector("#detailView .table", timeout=15_000)
        text = page.inner_text("#detailView")
        for word in ("项目概要", "工作目标", "工作任务", "上下文版本",
                     "默认适配器", "目标状态分布", "待人工处理"):
            assert word in text, f"项目详情缺少中文标签 {word}"
        assert errors == []
    finally:
        context.close()


def test_task_list_states_are_chinese_but_raw_is_preserved(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=15_000)
        page.wait_for_selector("#detailView .table", timeout=15_000)
        text = page.inner_text("#detailView")
        assert "执行状态" in text and "验收结论" in text
        # 中文展示 + 原始枚举（title 或括号里）都必须在 DOM 里可查。
        html = page.content()
        assert "执行失败" in text
        assert 'title="failed"' in html, "原始状态值应保留在 title 上"
        assert 'title="rejected"' in html, "原始判定应保留在 title 上"
        # 执行状态与验收结论文案不得互换：按原始枚举找对应药丸，
        # 逐个核对它自己的文案，而不是在整页文本里做宽松包含判断。
        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll('#detailView .pill'))
                       .filter(n => n.title)
                       .map(n => [n.title, n.textContent.trim()])""")
        by_raw = {}
        for raw, shown in labels:
            by_raw.setdefault(raw, []).append(shown)
        assert "failed" in by_raw, by_raw
        assert "rejected" in by_raw, by_raw
        for shown in by_raw["failed"]:
            assert "执行失败" in shown, f"执行状态文案错误：{shown}"
            assert "验收" not in shown, f"执行状态被写成了验收结论：{shown}"
        for shown in by_raw["rejected"]:
            assert "验收未通过" in shown, f"验收结论文案错误：{shown}"
            assert "执行" not in shown, f"验收结论被写成了执行状态：{shown}"
        # 页面上的枚举都是已知的：任何一个药丸落到“未知状态”都说明
        # 它被套了错误的语境（例如拿 taskState 表去翻 verdict）。
        for raw, showns in by_raw.items():
            for shown in showns:
                assert "未知状态" not in shown, (
                    f"已知枚举 {raw} 被翻成了未知状态：{shown}")
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 3. 状态中文显示正确，但原始状态机逻辑不变（筛选仍按枚举工作）
# --------------------------------------------------------------------------- #
def test_goal_filter_options_show_chinese_but_filter_by_enum(browser, site: Site):
    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#goalFilter", timeout=15_000)
        options = page.evaluate(
            """() => Array.from(document.querySelectorAll('#goalFilter option'))
                       .map(o => ({value: o.value, text: o.textContent.trim()}))""")
        values = [item["value"] for item in options]
        assert "all" in values
        assert "needs_human" in values, f"option 的 value 必须是原始枚举：{values}"
        texts = " ".join(item["text"] for item in options)
        assert "待人工处理" in texts, f"可见文字应为中文：{texts}"

        # 选中中文选项后，页面上只应剩下该状态的目标行。
        page.select_option("#goalFilter", "needs_human")
        page.wait_for_timeout(200)
        rows = page.locator("#detailView table tbody tr").count()
        assert rows >= 1
        body = page.inner_text("#detailView")
        assert "第二次暂停" in body or "第一次暂停" in body
        assert errors == []
    finally:
        context.close()


def test_unknown_state_is_shown_raw_and_not_misclassified(browser, site: Site):
    """把未知状态写进 Ledger 后，界面必须显示原始值 + 未知提示，不得归入已知类。"""
    store = TaskStore(site.ledger)
    try:
        plane = ControlPlane(_Adapter(), store, require_registered_projects=True)
        task = plane.enqueue("odd state", str(site.root / "acp-development"),
                            assertions=[], project_id=site.alpha["id"])
        store.update(task["task_id"], state="quarantined_by_vendor",
                     queue_status="complete")
        odd_id = task["task_id"]
    finally:
        store.close()

    context, page, errors = _page(browser, site, project_id=site.alpha["id"])
    try:
        page.wait_for_selector("#detailView:not([hidden])", timeout=15_000)
        page.wait_for_selector("#detailView .table", timeout=15_000)
        html = page.content()
        text = page.inner_text("#detailView")
        assert "quarantined_by_vendor" in text, "未知状态必须显示原始值"
        assert "未知状态" in text, "未知状态必须带“未知状态”提示"
        assert 'pill-unknown' in html, "未知状态应使用虚线样式以便区分"
        # 不能被当成“执行成功”或“验收通过”。
        assert 'title="quarantined_by_vendor"' in html
        assert errors == []
        # 任务确实在 Ledger 里是那个原始值（没有被前端改动）。
        store2 = TaskStore(site.ledger)
        try:
            assert store2.get(odd_id)["state"] == "quarantined_by_vendor"
        finally:
            store2.close()
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 4. 打开 / 关闭 / 切换目标 / 恢复二次确认
# --------------------------------------------------------------------------- #
def test_dialog_open_close_and_target_switching(browser, site: Site):
    context, page, errors = _page(browser, site)
    try:
        _open_handling(page, site, site.first_id)
        assert site.first_id in page.inner_text("#goalHandlingBody")
        page.click("#goalHandlingClose")
        page.wait_for_function(
            "() => document.getElementById('goalHandling').hidden",
            timeout=10_000)

        # 换一个目标再打开：内容必须是新目标，不能残留上一个。
        _open_handling(page, site, site.second_id)
        body = page.inner_text("#goalHandlingBody")
        assert site.second_id in body
        assert site.first_id not in body
        assert "第二次暂停" in body
        assert errors == []
    finally:
        context.close()


def test_resume_requires_the_existing_second_confirmation(browser, site: Site):
    context, page, errors = _page(browser, site)
    try:
        # 暂存恢复走 /api/chat/goal-resume，需要令牌；先像操作员那样连一次。
        page.click("#chatToggle")
        page.fill("#chatToken", TOKEN)
        page.click("#chatConnect")
        page.wait_for_selector("#chatControls:not([hidden])", timeout=15_000)
        page.click("#chatClose")
        page.wait_for_function(
            "() => document.getElementById('chatPanel').hidden", timeout=10_000)

        _open_handling(page, site, site.second_id)
        assert page.is_hidden("#goalHandlingConfirm"), \
            "打开弹窗时不得预置恢复确认区"
        page.click("#goalHandlingStage")
        page.wait_for_selector("#goalHandlingConfirm:not([hidden])", timeout=15_000)
        text = page.inner_text("#goalHandlingConfirmText")
        assert site.second_id in text
        assert "确认恢复" in page.inner_text("#goalHandlingConfirm")
        # 二次确认里的“取消”不应恢复任何东西。
        page.click("#goalHandlingCancelBtn")
        page.wait_for_function(
            "() => document.getElementById('goalHandlingConfirm').hidden",
            timeout=10_000)
        assert errors == []
    finally:
        context.close()


def test_escape_closes_the_dialog(browser, site: Site):
    context, page, errors = _page(browser, site)
    try:
        _open_handling(page, site, site.second_id)
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.getElementById('goalHandling').hidden",
            timeout=10_000)
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 5. 不同宽度下无文字重叠 / 溢出
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("width", [1440, 1024, 768, 520])
def test_no_horizontal_overflow_at_narrow_widths(browser, site: Site, width: int):
    context, page, errors = _page(browser, site, width=width, height=1000)
    try:
        _open_handling(page, site, site.second_id)
        metrics = page.evaluate(
            """() => {
                 const card = document.querySelector('#goalHandling .modal-card');
                 const body = document.querySelector('#goalHandlingBody');
                 return {
                   cardW: card.getBoundingClientRect().width,
                   viewportW: window.innerWidth,
                   scrollW: body.scrollWidth,
                   clientW: body.clientWidth,
                   docScrollW: document.documentElement.scrollWidth,
                 };
               }""")
        assert metrics["cardW"] <= metrics["viewportW"] + 1, metrics
        # 正文容器内部可纵向滚动，但不应出现横向溢出。
        assert metrics["scrollW"] <= metrics["clientW"] + 2, (
            f"弹窗正文横向溢出：{metrics}")
        assert metrics["docScrollW"] <= metrics["viewportW"] + 2, (
            f"页面出现横向滚动条：{metrics}")
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 6. 截图证据（浅色 + 深色）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_capture_handling_dialog_screenshots(browser, site: Site, theme: str):
    context, page, errors = _page(browser, site, theme=theme)
    try:
        _open_handling(page, site, site.second_id)
        SHOTS.mkdir(parents=True, exist_ok=True)
        modal = page.locator("#goalHandling .modal-card")
        out = SHOTS / f"handling-{theme}.png"
        modal.screenshot(path=str(out))
        assert out.is_file() and out.stat().st_size > 2000, out
        # 整页也留一张，便于看遮罩与背景关系。
        page.screenshot(path=str(SHOTS / f"handling-{theme}-full.png"))
        assert errors == []
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# 7. 回归：发布二次确认与聊天鉴权行为未变
# --------------------------------------------------------------------------- #
def test_publish_confirmation_still_requires_explicit_confirm(browser, site: Site):
    context, page, errors = _page(browser, site)
    try:
        page.click("#chatToggle")
        page.fill("#chatToken", TOKEN)
        page.click("#chatConnect")
        page.wait_for_selector("#publishBox:not([hidden])", timeout=15_000)
        # 发布是项目级写操作，UI-4 起必须先显式选择目标项目（不再默认第一个）。
        page.select_option("#chatProject", site.alpha["id"])
        page.fill("#publishObjective", "最小目标：读取 HEAD")
        page.click("#publishBtn")
        page.wait_for_selector("#publishConfirm:not([hidden])", timeout=10_000)
        text = page.inner_text("#publishConfirm")
        # 目标必须显式展示，且按钮是中文的“确认发布到此项目”。
        assert site.alpha["id"] in text or "ACP" in text
        assert "确认发布到此项目" in text
        # 取消后不应发布。
        page.get_by_role("button", name="取消").first.click()
        page.wait_for_function(
            "() => document.getElementById('publishConfirm').hidden",
            timeout=10_000)
        assert errors == []
    finally:
        context.close()
