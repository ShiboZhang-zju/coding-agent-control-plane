"""i18n 映射层的纯逻辑回归（不启动浏览器，快）。

覆盖三件容易出错的事：
  * 同一字符串在不同字段语境下翻译不同（succeeded ≠ verified）；
  * 未知状态保留原始值并显式标注，不猜成已知类别；
  * 中文化没有把状态判断改掉（筛选仍用原始枚举）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "dashboard" / "static" / "i18n.js"
APP = ROOT / "dashboard" / "static" / "app.js"
STYLES = ROOT / "dashboard" / "static" / "styles.css"

NODE = shutil.which("node")


def _run_js(script: str) -> dict:
    """在 node 里加载 i18n.js（补一个最小 window），再执行断言脚本。"""
    wrapper = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(I18N))}, 'utf8');
global.window = {{}};
eval(src);
const ACPI18N = global.window.ACPI18N;
{script}
"""
    # Node writes UTF-8; decoding with the console default (GBK here) would
    # mangle every Chinese label and fail for the wrong reason.
    proc = subprocess.run([NODE, "-e", wrapper], capture_output=True, timeout=60,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_succeeded_and_verified_are_not_conflated():
    """执行成功 ≠ 验收通过——这是题面明确点名的错误。"""
    out = _run_js("""
      const r = {
        taskState: ACPI18N.t('taskState', 'succeeded'),
        verdict:   ACPI18N.t('verdict', 'verified'),
        rejected:  ACPI18N.t('verdict', 'rejected'),
        failed:    ACPI18N.t('taskState', 'failed'),
        goalActive: ACPI18N.t('goalStatus', 'active'),
        projActive: ACPI18N.t('projectStatus', 'active'),
      };
      console.log(JSON.stringify(r));
    """)
    assert out["taskState"] == "执行成功"
    assert out["verdict"] == "验收通过"
    assert out["rejected"] == "验收未通过"
    assert out["failed"] == "执行失败"
    # 同一 active 在目标与项目语境下含义不同。
    assert out["goalActive"] == "进行中"
    assert out["projActive"] == "已启用"
    assert out["taskState"] != out["verdict"]


@needs_node
def test_required_vocabulary_matches_the_spec():
    out = _run_js("""
      const pairs = [
        ['goalStatus','active'], ['goalStatus','completed'],
        ['goalStatus','needs_human'], ['taskState','running'],
        ['taskState','succeeded'], ['taskState','failed'],
        ['verdict','verified'], ['verdict','rejected'], ['verdict','inconclusive'],
        ['taskState','queued'], ['taskState','dispatch_unknown'],
        ['attentionFlag','permission_blocked'],
        ['attentionFlag','worker_not_authenticated'],
      ];
      console.log(JSON.stringify(pairs.map(([k,v]) => ACPI18N.t(k,v))));
    """)
    assert out == ["进行中", "已完成", "待人工处理", "运行中", "执行成功",
                   "执行失败", "验收通过", "验收未通过", "结果不确定",
                   "排队中", "派发状态不明", "执行权限被拒绝", "Worker 未登录"]


@needs_node
def test_unknown_state_keeps_raw_value_and_is_marked():
    out = _run_js("""
      const a = ACPI18N.translate('taskState', 'vendor_new_state');
      const b = ACPI18N.translate('taskState', '');
      const c = ACPI18N.translate('taskState', 'SUCCEEDED');
      console.log(JSON.stringify({
        text: a.text, known: a.known, raw: a.raw,
        emptyText: b.text, emptyKnown: b.known,
        caseText: c.text, caseKnown: c.known,
      }));
    """)
    assert out["known"] is False
    assert out["raw"] == "vendor_new_state"
    assert "vendor_new_state" in out["text"]
    assert "未知状态" in out["text"]
    # 空值不是未知状态，只是没有值。
    assert out["emptyText"] == "—" and out["emptyKnown"] is True
    # 大小写漂移仍能识别成已知状态。
    assert out["caseKnown"] is True and out["caseText"] == "执行成功"


def test_status_logic_still_uses_raw_enums():
    """中文化不得改动状态判断：DANGER/WARN 列表与比较仍是英文枚举。"""
    src = APP.read_text(encoding="utf-8")
    assert 'const DANGER_STATES = ["failed", "lost", "dispatch_unknown", "unknown", "cancelled"];' in src
    assert 'const WARN_STATES = ["waiting_for_approval", "needs_approval", "needs_human"];' in src
    # 关键判断点仍是原始枚举比较。
    assert 'goal.status === "needs_human"' in src
    assert 'goal.status === "completed"' in src
    assert 'task.verdict === "verified"' in src
    # 不允许用中文标签去做判断。
    assert '=== "待人工处理"' not in src
    assert '=== "验收通过"' not in src


def test_app_never_rewrites_status_values_it_receives():
    """前端不写状态：没有把枚举值赋回 state / verdict 的语句。

    用正则而不是子串，否则提示文案里的 "verdict = verified" 会被误判成赋值。
    """
    import re
    src = APP.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\s*(?:[A-Za-z_$][\w$]*\.)?(?:state|verdict|queue_status)\s*=\s*[^=]",
        re.M)
    offenders = [line.strip() for line in pattern.findall(src)]
    # 允许本地变量守卫（state.loading 等 UI 状态），只禁止任务/目标枚举字段。
    bad = [line for line in src.splitlines()
           if re.match(r"\s*(task|goal|item|row)\.(state|verdict|queue_status)\s*=\s*[^=]",
                       line)]
    assert not bad, f"前端不应改写后端状态字段：{bad[:3]}"
    # 状态只能经 GET 读取，写操作仅限既有的 LDChat 端点。
    assert 'method: "PUT"' not in src and 'method: "PATCH"' not in src


def test_every_used_css_variable_is_defined():
    """`--surface` 那个 bug 的通用防线：用到就必须定义。"""
    import re
    css = STYLES.read_text(encoding="utf-8")
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    undefined = sorted(used - defined)
    assert not undefined, f"未定义的 CSS 变量：{undefined}"
    assert "--surface" not in used


def test_modal_card_does_not_reference_undefined_surface():
    css = STYLES.read_text(encoding="utf-8")
    start = css.index(".modal-card {")
    block = css[start:css.index("}", start)]
    assert "var(--surface)" not in block
    assert "background: var(--bg-raised)" in block
    assert "color: var(--text)" in block
