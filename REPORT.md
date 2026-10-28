# PoC 报告：CodeBuddy 作为首个 Worker Adapter

日期：2026-10-28　分支：`poc/codebuddy-adapter`　被测版本：CodeBuddy CLI **2.169.0**

## 0. 一句话结论

**Conditional Go**：CodeBuddy 的 Jobs REST API 足以支撑"派发 + 并行 + 集中查询 + 停止"，
但它的**终态、失败信号、审批通道、事件重放**都不可依赖，控制平面必须自己补
状态存储、幂等、失联判定、产物验收与对账。不需要自建完整进程监管器。

## 1. 环境与接口盘点（实测）

| 项 | 值 |
|---|---|
| OS | Windows 11 专业版 10.0.26200 |
| Git | 2.53.0.windows.3 |
| Node.js | v24.16.0（npm 11.13.0） |
| CodeBuddy 可执行 | `C:\Users\<user>\AppData\Roaming\npm\codebuddy.cmd` / `.ps1` → `@tencent-ai/codebuddy-code\bin\codebuddy` |
| 版本 | 2.169.0（`codebuddy --version`） |
| 安装来源 | `npm install -g @tencent-ai/codebuddy-code@2.169.0` |
| Help 全文 | `evidence/environment/codebuddy-help.txt` |
| OpenAPI | `openapi/codebuddy-openapi.json`（126 个路径，`openapi 3.1.0`，`info.version=2.169.0`） |

与该 PoC 相关的 CLI 能力（来自本机 `--help`）：

- `--serve`：HTTP 服务（Web UI + REST + ACP over HTTP SSE），`--host` 默认 `127.0.0.1`，
  `--auth password|none` 默认 `password`；
- `--bg` / `--background` / `--name` / `--exec`：后台会话；
- `--worktree [-w]`、`--worktree-branch`：git worktree 会话；
- `--acp` + `--acp-transport stdio|streamable-http`：ACP 模式；
- `--resume [sessionId]` / `--continue` / `--resume-create-missing` / `--fork-session`；
- `--permission-mode acceptEdits|bypassPermissions|default|plan|dontAsk|auto`；
- `--multitask`：**帮助明确 "Interactive TTY only"**，headless 不可用；
- 后台会话管理子命令：`ps` / `logs` / `attach` / `kill` / `stop` / `respawn` / `agents --jobs`。

服务启动（本 PoC 使用）：`python scripts/serve_ctl.py start` →
`node bin/codebuddy --serve --port 8787 --host 127.0.0.1 --auth password --permission-mode default`。
唯一监听 `127.0.0.1:8787`，密码由服务随机生成并只存在于被 `.gitignore` 排除的 banner 日志中。

`/api/openapi.json` 无需认证即可读（属于 OpenAPI 声明的豁免路径），其余 API 需要
`Authorization: Bearer <token>` 且必须携带 `X-CodeBuddy-Request: 1`。

## 2. 交付物

```
adapters/base.py                 厂商无关契约 + 9 态统一状态机
adapters/codebuddy/adapter.py    CodeBuddy 专有映射（唯一出现厂商知识的地方）
adapters/codebuddy/client.py     REST/SSE 传输层（token 不落盘）
controlplane/store.py            任务存储（重启后可重建）
controlplane/verify.py           产物断言验收器
controlplane/plane.py            dispatch/refresh/cancel/resume/respawn/summary
scripts/                         serve_ctl / make_sandbox / detached / t01…t09 / probe_*
openapi/codebuddy-openapi.json   当前版本导出的 OpenAPI
evidence/                        environment / jobs / sse / recovery / permissions / isolation
docs/                            POC_SPEC / test-plan / adapter-contract
capability-matrix.md, lifecycle.md, AGENTS.md, README.md, REPORT.md
```

## 3. 测试结果

| 编号 | 结论 | 关键数据 | 证据 |
|---|---|---|---|
| T00 环境盘点 | Pass | CLI/OpenAPI 全部实取 | `evidence/environment/`、`openapi/` |
| T00b 失败语义 | **Fail（产品侧）** | `exit 3` 的任务仍报 `state=done`，API 无退出码字段 | `evidence/jobs/T00b-state-semantics.json` |
| T00d shell 语义 | Pass | `bash:true` 是"平台 shell 单行命令"：`;` 无效，`&` 有效；无 `sleep` | `evidence/jobs/T00d-shell-semantics.json` |
| T00e 列表延迟 | **Fail（产品侧）** | `?all=1/?all=0/?cwd=` 均 ≈30.3s，单任务毫秒级 | `evidence/jobs/T00e-list-latency.json` |
| T01 四任务并发 | Pass | 4 任务 peak=4 同时 running，耗时 31.8/32.1/35.7/41.8s | `evidence/jobs/T01-concurrency.json` |
| T02 失联判定 | Pass | 杀进程后 worker 恒 `working`；平面 121s 后判 `lost` | `evidence/recovery/T02-lost-job.json` |
| T03 跨仓库隔离 | **Fail（产品侧）** | repo-b 的 shell 任务成功写入 `repo-a/cross_from_b.txt` | `evidence/isolation/T03-isolation.json` |
| T03b worktree 隔离 | Pass | agent 任务落在 `.codebuddy/worktrees/bg-<job>`（分支 `worktree-bg-<job>`），主检出未污染 | `evidence/isolation/T03b-worktree-agent.json` |
| T04 验收 | Pass | 3 verified / 1 rejected（`state=done` 但断言失败） | `evidence/jobs/T04-acceptance.json` |
| T05 SSE | **Fail（产品侧）** | 有 `snapshot/added/changed/keepalive`，无 `id:`，断线期间事件不可重放；后段出现"连上但零事件" | `evidence/sse/T05-sse-reconnect.json`、`T05c-sse-isolated.json` |
| T06 cancel | Pass | `POST /jobs/{id}/stop` → 1.9s 变 `stopped`，产物不存在，判 `rejected` | `evidence/recovery/T06-cancel-stop.json` |
| T07 resume/respawn | 部分 | 运行中 job 的 resume/respawn 返回 500（`Timed out acquiring job lock`）；归档会话 resume 才是正路 | `evidence/recovery/T07-resume-respawn.json` |
| T08 服务重启 | 部分 | 重启后 job 仍可查（200），控制平面从 `.poc-state` 重建任务表成功 | `evidence/recovery/T08-service-restart.json` |
| T08c 执行恢复 | **Fail（产品侧）** | 停服务即杀子进程；重启后命令不继续，artifact 永不出现 | `evidence/recovery/T08c-exec-survival-precise.json` |
| T09 权限 | 安全行为 Pass / 审批路径 Not Supported | background job 不进入 `waiting_for_approval`，工具调用 auto-deny；命令未执行、无 git diff；但 worker 仍报 `done` | `evidence/permissions/T09-permission-denied.json` |
| T10 主 Agent 汇总 | Pass | 一次结构化查询拿到 4 任务状态/验收/产物/阻塞/下一步 | `evidence/jobs/T10-summary.json` |

环境级干扰（safe-delete shim、30s 读接口、终态偶发不写盘）单独记录在
`evidence/jobs/ENV-DEFECT.md`，不混入产品结论。

## 4. 逐条回答

### 1) CodeBuddy 是否适合作为第一个 Worker Adapter？

适合作为**第一个**，因为它提供了同代产品里少见的完整 HTTP 面（Jobs/SSE/stop/respawn/
worktree），能让控制平面在**不写进程监管器**的前提下把主链路跑通。但必须接受三个前提：
终态不可信、事件不可重放、失败不可从 API 观测。

### 2) 哪些能力是 CodeBuddy 原生提供的？

后台任务派发、并发执行、单任务状态查询、全量任务列表、SSE 变更流、stop/cancel、
respawn（重开对话）、agent 任务的 worktree 隔离、会话持久化与 resume、CLI 侧 `ps/logs/kill`。

### 3) 哪些能力只是对话恢复，不是执行恢复？

`resume` / `respawn` / `--resume` / `--continue` 全部只恢复**对话上下文**。
`T08c` 证明：服务停止时子进程随之被杀，重启后命令不会继续执行，产物不会再出现。
"Job 还在列表里" ≠ "任务还在跑"。

### 4) 哪些能力需要控制平面补充？

本地任务存储与 `cp-id ↔ job-id ↔ session-id` 映射、幂等键（API 无此字段）、
失联判定（`lost`）、产物断言验收、全量对账（因为无 replay）、审批待办队列、
失败重试、事件标准化。

### 5) 是否需要自建进程监管器？

**不需要完整监管器**。需要的是"判活 + 对账"：`GET /jobs/{id}` + 进程存在性检查 +
停滞阈值。进程生命周期本身仍归 CodeBuddy 管理。

### 6) Job / Session / CLI 对话 / 系统进程的生命周期关系？

详见 `lifecycle.md`。要点：Job 是持久实体（`~/.codebuddy/jobs/<id>/state.json`），
`pid` 只在运行期有效；Session 可跨 Job 复用；CLI 对话只能靠 `--resume <sessionId>` 重开；
系统进程是 Job 的子进程，服务一停就死。

### 7) 当前 Adapter 契约能否覆盖 Codex / Claude Code / OpenCode / Gemini CLI？

基本够用，但需要两处扩展：

1. **`capabilities()` 要能表达"降级"**：例如某 harness 没有全量列表，只能常驻进程内维护
   句柄表 —— 契约里 `reconcile()` 允许返回 `unknown` 而不是伪装全量。
2. **`stream_events()` 需要显式的 `replayable: bool`**：目前的 `cursor=None` 表达力不足，
   应在返回值里带上"能否重放"的能力声明，避免上层误用。

其余 9 个方法对 stdio-only 的 harness（Claude Code / Gemini CLI 的 ACP 模式）同样成立，
因为它们都能提供"派发 + 句柄 + 会话 + 产物"。

### 8) 最小下一阶段开发列表

| # | 事项 | 复杂度 |
|---|---|---|
| 1 | 任务存储升级为 SQLite（当前 JSON 文件，单进程安全） | S（1 人日） |
| 2 | 幂等键 + 派发重试（含"500 但已创建"的对账） | S（已实现雏形，补测试 0.5 人日） |
| 3 | 定时 `reconcile()` 循环 + `lost` 通知 | M（1–2 人日） |
| 4 | 验收器扩展：more assertion kinds（schema/json、命令退出码代理、diff 白名单） | M（2 人日） |
| 5 | 审批队列（当前 background job 无人工审批路径，需要"前台会话 + Web UI"或自建审批代理） | L（3–5 人日，且依赖产品侧提供 API） |
| 6 | 事件标准化 + 快照补偿（SSE 无 replay → 用轮询补齐） | M（2 人日） |
| 7 | 第二个 Adapter（建议 Claude Code ACP）验证契约普适性 | M（2–3 人日） |

### 9) 最终结论

**Conditional Go**。Go 条件中已满足：四任务可靠并行、worktree 隔离有效（agent 任务）、
集中查询可用、cancel 不误报成功、服务重启后状态可重建、CodeBuddy 专有逻辑被限制在
Adapter 内、无需完整进程监管器。

未满足/需补充：SSE 无 replay（可用轮询 + reconcile 兜底）、shell 任务无文件隔离、
background job 无人工审批路径、job 列表 30s 级延迟、终态偶发不落盘。

No-Go 条件均未触发：后台任务可查、重启后任务仍可发现、工作目录可隔离（agent 任务）、
权限拒绝无副作用、失败任务没有被判成通过（验收器拦住了）。

### 10) 证据索引

见 `docs/test-plan.md` 的表格；能力与证据的逐条对应见 `capability-matrix.md`。

## 5. 未验证 / 阻塞

| 项 | 原因 | 状态 |
|---|---|---|
| ACP 端到端 | 本阶段只用 REST + SSE；`/api/v1/acp*` 仅做接口盘点 | 未验证 |
| 人工点击"拒绝" | background job 不产生审批请求（auto-deny），无 UI 入口可点 | Not Supported |
| Multitask 并行面板 | 帮助声明 "Interactive TTY only" | 未验证（headless 不可用） |
| 运行中 job 的 resume/respawn | 返回 500 `Timed out acquiring job lock` | 部分失败 |
| shell 任务终态稳定性 | 后段多次复现"命令完成但 state 恒 working" | 缺陷已记录，未定位产品侧根因 |
| Draft PR | 本机无 `gh` CLI，且无可用 GitHub token（不使用生产凭据） | 阻塞，见 §7 |

## 6. 安全与脱敏

- 服务只绑 `127.0.0.1`，`--auth password`；密码随机生成、只存在被忽略的 banner 日志；
- 未使用 `bypassPermissions` / `--dangerously-skip-permissions` / `CODEBUDDY_IS_SANDBOX=1`；
- 所有测试产物限于 `sandbox/repo-a`、`sandbox/repo-b`（可随时 `scripts/make_sandbox.py` 重建）；
- 证据写盘前统一走 `redact()`，剔除 `password/token/authorization/cookie`；
- 原始服务日志、job 日志、`.poc-state/` 全部 git 忽略；
- 提交前用 `git grep` 校验过凭据字样。

## 7. 复现 / 推送

```powershell
python scripts/serve_ctl.py start
python scripts/make_sandbox.py
python scripts/detached.py start t01 scripts/t01_concurrency.py && python scripts/detached.py tail t01
python scripts/detached.py start t05c scripts/t05c_sse_isolated.py
python scripts/detached.py start t08c scripts/t08c_exec_precise.py
python scripts/detached.py start t09f scripts/t09_permission_final.py
python scripts/serve_ctl.py stop
```

推送结果：

- `main`（初始提交 `be02d2e`）→ 已推送；
- `poc/codebuddy-adapter`（`def2eed`）→ 已推送，upstream 已设置。

Draft PR：本机**没有 `gh` CLI**，也没有可用的 GitHub token（不使用生产凭据），因此
**未**自动创建。请用下面的 compare 链接一键创建 Draft PR：

`https://github.com/ShiboZhang-zju/coding-agent-control-plane/compare/main...poc/codebuddy-adapter?expand=1`

状态：**Blocked（缺工具/凭据）**，不是测试失败。
