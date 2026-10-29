# coding-agent-control-plane

一个面向多种 Coding Agent Harness 的**通用控制平面**。目标是把"派发任务、并行执行、状态查询、实时监控、中断恢复、人工审批、失败验收、汇总决策"变成与厂商无关的能力，而不是绑定某一个 CLI。

当前阶段：**PoC**。只做可验证的最小实现 + 通用协议边界，不开发完整产品。

首个被测 Worker：**CodeBuddy**（`@tencent-ai/codebuddy-code` 2.169.0）。CodeBuddy 只是第一个 Worker Adapter，控制平面的领域模型、生命周期与调度逻辑不依赖它的专有 API。

## 结论速览

| 问题 | 结论 |
|---|---|
| CodeBuddy 能否作为第一个 Worker Adapter | 可以（**Conditional Go**），但必须由控制平面补状态存储、幂等、对账、验收器 |
| 原生能力 | 后台任务、Jobs REST API、全局任务列表、SSE 变更流、cancel、worktree 隔离（agent 任务） |
| 只有"对话恢复"的 | `resume` / `respawn` / `--resume`：只恢复会话上下文，不恢复被打断的命令执行 |
| 必须自建 | 任务状态持久化、幂等键、事件不可重放的对账、产物断言验收、审批队列 |
| 是否需要自建进程监管器 | 不需要完整监管器，但需要"失联判定"（`lost`）与对账循环 |
| 四路真实 Agent 并行 | **Pass**：4/4 succeeded、4 个独立 worktree、全部 `verified`（需跳过用户级 hook） |
| 审批门控 | **已实现**：风险只升不降 + 派发前原子预留 + 一次性（T20 / T21） |

完整论证见 [REPORT.md](REPORT.md)，能力矩阵见 [capability-matrix.md](capability-matrix.md)，状态机见 [lifecycle.md](lifecycle.md)。

## 目录

```
adapters/base.py             厂商无关的 Worker Adapter 契约 + 统一状态机（无任何 CodeBuddy 字段）
adapters/codebuddy/          唯一允许出现 CodeBuddy 专有知识的地方（REST/SSE/状态映射）
controlplane/store.py        控制平面自己的任务存储（重启后可恢复"工作流"）
controlplane/verify.py       基于产物断言的验收器（不看 worker 的终态）
controlplane/plane.py        门面：dispatch / refresh / cancel / resume / respawn / summary
scripts/                     最小测试工具（服务管控、各测试编号脚本、detached 运行器）
openapi/                     当前安装版本导出的 OpenAPI（认证信息已脱敏）
evidence/                    每次测试的证据（已脱敏；原始日志不入库）
sandbox/                     两个临时 Git 仓库（不入库，可随时重建）
docs/                        PoC 规范、测试计划、Adapter 契约
```

## 运行前置

```powershell
# 1) 仅监听回环 + 开启密码认证的 CodeBuddy HTTP 服务
python scripts/serve_ctl.py start        # 自动生成/读取本地密码，不落盘到证据
python scripts/serve_ctl.py status

# 1b) 不希望 worker 的控制台窗口闪屏时，用隐藏桌面启动（窗口数实测 0）
$env:POC_HIDDEN_ACTION="start"; python scripts/detached.py start hidden scripts/serve_hidden.py
python scripts/serve_hidden.py stop

# 2) 重建两个一次性沙箱仓库
python scripts/make_sandbox.py

# 3) 跑测试（长时间测试用 detached 运行器）
python scripts/detached.py start t01 scripts/t01_concurrency.py
python scripts/detached.py tail  t01

# 4) 结束时停服务，不留后台进程
python scripts/serve_ctl.py stop
```

要求：Python 3.11+（仅标准库）、Git、本机已登录的 CodeBuddy CLI。

## 安全约定

- 服务只绑 `127.0.0.1`，`--auth password`，不使用 `--auth none`；
- 不使用 `bypassPermissions` / `--dangerously-skip-permissions` / `CODEBUDDY_IS_SANDBOX=1`；
- 所有测试只写 `sandbox/` 内的临时仓库，不触碰任何生产项目；
- 密码、Token、Cookie、Authorization 头一律不写入仓库；证据文件写盘前统一脱敏；
- 无法验证的能力一律标注 `未验证 / Blocked`，不猜测、不伪造。
