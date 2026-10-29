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

**Conditional Go（受限试点）**。Go 条件中已满足：后台任务可并行派发（shell 4 路、
agent 4 路在 Job 层被同时接受）、worktree 隔离有效（agent 任务）、集中查询可用、
cancel 不误报成功、服务重启后**控制平面侧**状态可重建、CodeBuddy 专有逻辑被限制在
Adapter 内、无需完整进程监管器；评审指出的验收误报（B1）已修复（T13/T14）。

未满足/需补充：真实 agent 并发目前只能跑到 1–2 路（本机 hook 阻塞，非 Jobs 能力）、
OS 级跨项目隔离未实现（只有检测+否决）、SSE 无 replay（轮询 + reconcile 兜底）、
shell 任务无文件隔离、background job 无"人工批准后执行"通道、job 列表 30s 级延迟、
终态偶发不落盘。

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

## 7. 评审后的可靠性收敛（2026-10-29，v0.1）

针对评审提出的问题逐条修复，并在同一分支上验证。

### 7.1 已修复

| 编号 | 问题 | 修复 | 证据 |
|---|---|---|---|
| P0-1 | `ControlPlane.dispatch()` 默认 `mode="shell", isolation="none"`，与 `DispatchRequest` 默认不一致 | 引入 `DispatchPolicy`：默认 `agent + worktree`；`shell + 无隔离` 必须显式 `allow_unisolated_shell=True`，否则 `ValueError`；`bypassPermissions` 直接拒；每次派发的策略写入 ledger，风险进 `warnings` | `evidence/jobs/T12-idempotency.json` |
| P0-2 | 验收只看"文件存在"，B1 越界写入仍 `verified` | dispatch 前 `capture_baseline()`（HEAD/status、断言目标 sha256、同级仓库指纹）；验收要求产物**归属本次 attempt**（新文件或内容变化、提交在 `baseline_head..HEAD`）；同级仓库指纹变化 → `rejected`；agent 任务在 worktree 根目录验收 | `evidence/jobs/T13-fake-success.json`、`evidence/isolation/T14-cross-project-rejected.json` |
| P0-3 | 派发失败会自动重 POST，可能重复起任务 | 每次 `dispatch()` 只 POST 一次；查不到就 `state=dispatch_unknown`，由 `resolve_dispatch()` 对账（绑定或证明不存在），之后才允许新 attempt | `docs/adapter-contract.md` §5（该分支本机未触发 → **未验证**） |
| P1-1 | `TaskStore.load()` JSON 损坏时静默清空 | 换成 SQLite ledger，`integrity_check` 失败即隔离到 `.corrupt-<ts>` + 写 `LEDGER_DEGRADED.txt` + stderr，绝不静默空表；旧 `tasks.json` 一次性迁移 | `controlplane/store.py` |
| P1-2 | `resume()` 创建新 Job 后没更新 `worker_job_id` | `work_item → run_attempt → worker_job` 显式映射；`resume`/`respawn` 追加 attempt 并更新当前 job | `controlplane/plane.py` |
| P1-3 | worktree ≠ OS 级文件隔离 | 明确写入能力矩阵：worktree 只隔离 Git 工作区；shell 任务无隔离（T03/T14）；OS 级边界未实现 | `capability-matrix.md` |
| 文档 | `lifecycle.md` 仍写"服务重启后子进程存活" | 已按 T08c 证据改正：停服务即杀子进程、命令不续跑 | `lifecycle.md` |

### 7.2 新增测试

- **T12 幂等 / 策略强制**：同一 idempotency key 只起 1 个 job（第二、三次 `deduplicated=true`）；
  无 `allow_unisolated_shell` 的 shell 派发被拒绝 → `Pass`。
- **T13 假成功**：旧产物、他人产物、未发生的提交 → 全部 `rejected`；两个正向对照 `verified`
  （证明验收器不是"一律拒绝"）→ `Pass`。
- **T14 越界否决**：repo-b 的任务写了 repo-a，自己的断言通过、worker 报 `done`，
  最终仍 `rejected` 并记录 `SIDE_EFFECT:cross_project_change` → `Pass`。

### 7.3 更正一条 PoC 结论

**T01 是四个 shell job 并发，不是四个 LLM Agent 并发**（评审指出，成立）。
T11 专门测 agent 并发，结果是：

- Job 层：4 个 agent 会话被同时接受（`peak_from_timestamps=4`），确属真并发；
- 但 4 路、2 路并行时所有会话都在 prompt 阶段失败，worker 只报 `model run failed`；
  读 `~/.codebuddy/logs/job-*.log` 才看到真因：
  `UserPromptSubmit operation blocked by hook: ["longmemory.exe" recall]: Hook timed out after 5000ms`；
- 串行提交 + 并行执行时曾拿到 3/4 `verified`、3 个独立 worktree（控制台原始输出见证据）。

所以当时的**可用 agent 并发度 ≈1–2，瓶颈是本机 5 秒记忆 hook，不是 CodeBuddy Jobs**，
也不是模型配额。完整记录：`evidence/isolation/T11-parallel-blocked.md`。

> **已被 §8.5 取代**：后来用 `--setting-sources project,local` 跳过用户级 hook，
> T11 四路并发一次跑通（4/4 succeeded、4 个 worktree、全部 verified）。
> 本节保留为问题定位的原始记录。

### 7.4 下一阶段验收对照

| 评审验收项 | 当前状态 |
|---|---|
| 4 个 Agent 并行（独立 Job/worktree/产物归属） | **Pass（§8.5）**：`evidence/isolation/T11-4way-success.json` |
| 项目隔离（越界被 OS 拒绝） | **未实现**：当前靠"检测并否决"（T14），不是 OS 级隔离 |
| 重复派发（同一幂等键最多一次） | **Pass**（T12） |
| 进程崩溃（无静默丢失/重复） | 部分：`lost` 判定 + 对账已验证（T02/T08c），崩溃后的自动重派未实现 |
| 假成功（旧产物/他人产物/失败测试） | **Pass**（T13） |
| 服务重启（恢复映射、标记中断） | 部分：ledger 重建已验证，执行中断只标记不重派（T08c） |
| SSE 断线（快照恢复当前可信状态） | 部分：无 replay，靠 `reconcile()` + 单任务查询（T05/T05c） |
| 人工审批（未批准无法执行） | **Pass（安全侧）**：background job 直接 auto-deny 且无副作用（T09）；但无"人工批准后执行"通道 |

## 8. 第二轮评审后的修复（2026-10-29 晚）

### 8.1 安全问题：泄露日志与历史清理

- 泄露内容：`evidence/environment/hidden-serve-stdout.log`（服务启动 banner，含该实例的随机密码）。
  它在 `d9ac5a9` 进入历史，`567f0e1` 只是把它从索引移除，历史仍可查。
- 处置：`git filter-branch --index-filter` 重写 `poc/codebuddy-adapter`（仅该分支，未动 `main`），
  删除本地备份分支、`reflog expire --expire=now --all`、`gc --prune=now`，
  最后 `push --force-with-lease`。
  校验结果：`git log --all -- evidence/environment/hidden-serve-stdout.log` 为空，
  旧 blob `2aec604…` 已不可达。
- 凭据有效性：该密码属于**已停止**的服务实例，密码本身是每次启动随机生成，下一次启动即失效；
  当前 `127.0.0.1:8787` 无监听。
- 残留风险（如实说明）：GitHub 可能仍保留未被引用的旧对象（按 SHA 直链访问），
  force-push 不等于"撤销泄露"。真正的补救是轮换凭据（已随实例停止生效）。
  自本次起 `evidence/environment/*serve*.log` 全部 gitignore。

### 8.2 P0-1 重启后验收上下文丢失

`_snapshot_patch(task_id, snap)` 现在显式接收控制平面 ID，`dispatch / resolve_dispatch /
refresh / cancel / resume / respawn` 六条路径全部改造完。
新增 T16：创建任务 → 重建 store/adapter/plane（模拟重启，adapter 只知 job id）→ `refresh()`，
断言 `verdict` 保持 `verified`、`assertions`/`baseline`/`attempt`/`job` 映射均未丢：

```json
{"verdict_kept": true, "verdict_is_verified": true, "assertions_kept": true,
 "baseline_kept": true, "attempt_kept": true, "job_mapping_kept": true}
```

### 8.3 P0-2 `dispatch_unknown` 不再把"查不到"当成"没创建"

`resolve_dispatch(task_id, confirm_absent=False)`：

- 查到 job → 绑定；
- 查不到 → **保持 `dispatch_unknown`**，追加 `DISPATCH_STILL_UNKNOWN` 警告并记录
  `dispatch_unresolved` 事件；
- 只有显式 `confirm_absent=True`（人工决定）才转为 `unknown` 并记录
  `dispatch_absent_confirmed`，此时才允许新 attempt。

T17-B 验证：`state_after_dispatch == state_after_unconfirmed_lookup == dispatch_unknown`。

### 8.4 P1-3 SQLite 并发

- 所有写方法加 `@_synchronized`（store 级 RLock），连接加 `PRAGMA busy_timeout=15000`；
- `work_items.idempotency_key` 加 **UNIQUE 索引**，新增原子方法
  `create_work_item()`：判断与插入在同一临界区，冲突则回读已有记录；
- `dispatch()` 改为以 `create_work_item()` 的结果为准，未抢到键的调用方**不再 POST**。

T17-A：8 线程同时用同一 key 派发 → `worker_dispatches=1 / work_items=1 / attempts=1 /
deduplicated_responses=7`（T12 只证明了顺序重复）。

### 8.5 T11：四路真实 Agent 并行 **Pass**

关键前提：服务用 `--setting-sources project,local` 启动，跳过用户级
`longmemory` hook（其 `UserPromptSubmit` 超时 5s 会让并行会话集体失败）。

实测（`evidence/isolation/T11-4way-success.json`）：

| 任务 | 状态 | 起止（UTC） | worktree 分支 | 产物 |
|---|---|---|---|---|
| agent-a1 | done | 06:43:24 → 06:44:59 | `worktree-bg-b3ef26ac` | `agent-a1.txt` |
| agent-a2 | done | 06:43:28 → 06:45:26 | `worktree-bg-8dc5d154` | `agent-a2.txt` |
| agent-b1 | done | 06:43:24 → 06:45:05 | `worktree-bg-36726069` | `agent-b1.txt` |
| agent-b2 | done | 06:43:24 → 06:44:43 | `worktree-bg-87c8807d` | `agent-b2.txt` |

`peak_concurrent_by_timestamps = 4`、`distinct_worktrees = 4`、四个产物均存在且互不污染。

一句免责：本轮收尾时 `refresh()` 因 job store 停顿超时（>5 分钟未返回），因此
`T11-4way-success.json` 的结论来自 **worker 状态文件 + git worktree**（与 API 无关）。
验收结论用 `t11_offline_verdicts.py` **离线重算**：从 ledger 取 baseline+assertions，
从磁盘取 worktree 与 worker 终态，四个任务全部 `verified`（`file_exists` 通过且 `owned=true`），
见 `T11-4way-ledger.json`。这也顺带证明：**验收层不依赖 worker API 可用性**。

### 8.6 控制台闪窗：根因与修复

第二轮发现的新根因：**我们自己的测试脚本**。`detached.py` 起的进程没有控制台，
于是它内部每一次 `subprocess.run(["git", ...])` 都会**新分配一个可见控制台窗口**。

- 修复：新增根目录 `winproc.py`，`run()/popen()` 强制 `CREATE_NO_WINDOW + SW_HIDE`；
  `verify/adapter/make_sandbox/detached/serve_ctl/T11/T13/T14` 全部改用；
- 对照实验（`evidence/environment/T15d-self-flash.json`）：
  **裸 subprocess 6 次 → 6 个可见窗口；`winproc` 6 次 → 0 个**；
- worker 侧的窗口用隐藏桌面方案（`serve_hidden.py`），实测 0 个；
- 剩下无法消除的是"我每执行一条命令产生的那一个 shell 窗口"（工具层，不是本仓库代码）。

### 8.7 仍未完成（下一轮）

| 项 | 状态 |
|---|---|
| 审批门控（approval → dispatch_allowed/blocked 的强制约束） | **已完成（§10）**：风险只升不降、派发前原子预留、一次性 |
| 跨项目 **OS 级**隔离 | 未实现（当前是检测+否决） |
| 环境版本锁定 | 未做；本轮记录到 CLI 已自动升级至 **2.178.1**（PoC 基线 2.169.0），
T11 证据里带 `cli_version` 字段 |
| job store 30s 级读 | 产品侧问题，未绕过（只降低调用频率） |

## 9. 第三轮：控制平面可靠性（2026-10-29 深夜）

按评审优先级只做两件 P0 + 审批门控。

### 9.1 P0 `refresh()` 拆成两条路径

| 路径 | 频率 | 行为 |
|---|---|---|
| `refresh_known_jobs(timeout_s=20)` | 高频 | 按 ledger 里的 job id **逐个**查询，单次超时；单个失败只记 `observation_failed` 事件 + `observe_failures` 计数，**状态不变** |
| `discover_orphan_jobs()` | 低频 | 全量扫描；失败直接返回 `ok=False`，**不碰任何已知任务**；连续两次扫不到才 `lost` |

`lost` 还要求独立证据：adapter 报 `working+alive=false` 时，plane 再用 `pid_alive()` 校验，
无法证明进程已死就保持原状态并记 `LOST_UNCONFIRMED`。

证据 `T18-refresh-split.json`：慢查询 → `state_unchanged=true` 且 `observe_failures=1`；
任务消失 → 第 1 次扫描仍 `running`、第 2 次才 `lost`；全量扫描抛错 → 已知任务状态不变。

### 9.2 P0 Ledger 降级改为 fail-closed

`TaskStore.assert_writable(op)` 在 `degraded=True` 时抛 `LedgerDegraded`，
`dispatch / resume / respawn / approve / deny / request_approval` 全部前置调用；
查询、`summary()`、诊断仍可用。

顺带修掉两个真实缺陷（测试时暴露，都属于"这条路径从来没被真正跑过"）：

1. `_open()` 在 `except` 分支里 `self._quarantine(exc)` **没有 return** → 降级路径返回
   `None`，随后 `executescript` 崩在 `AttributeError`；
2. `_quarantine()` 用 `os.replace` 移动损坏文件，Windows 上会失败（句柄/残留 -wal），
   失败后又把同一个损坏路径重新打开 → 再次抛错。改成"先复制副本，再原地截断"。

崩溃窗口补齐：`recover_inflight()` 把"已持久化意图但没有 job id"的 work item
在启动时置为 `dispatch_unknown`，禁止盲目重派。

证据 `T19-ledger-fail-closed.json`：三个变更操作全部 `refused`、`stub_dispatches=0`、
查询仍可用；inflight 恢复为 `dispatch_unknown` 并带 `DISPATCH_INFLIGHT_AT_RESTART` 警告。

### 9.3 审批门控（内容绑定 + 一次性）

```
policy.effective_risk() == high  →  必须存在 digest 匹配且未消费的 approval
digest = sha256(prompt + cwd + policy + assertions)   ← 内容变了就失效
approve(digest) → dispatch 成功后才 consume_approval(id, job_id)  ← 只能用一次
```

证据 `T20-approval-gate.json`（高风险策略：shell + 无隔离）：

| 场景 | 结果 |
|---|---|
| 无审批直接派发 | `ApprovalRequired`，worker 调用 0 次 |
| 请求 → 批准 → 派发 | 成功，`approval_consumed=true`（`consumed_by=job-1`） |
| 同一审批再用一次 | `ApprovalRequired`（一次性） |
| 批准后篡改 prompt | digest 不同 → `ApprovalRequired`，worker 未被调用 |
| 审批被拒绝 | `ApprovalRequired` |

注意一个符合预期的细节：批准了 A 之后，A 的那条审批**仍然有效**（未消费），
所以同内容再派发仍可执行一次；测试用的第六个用例因此换了内容。

### 9.4 仍未做

- 跨项目 **OS 级**隔离（评审指出 Job Object 只是进程/资源边界，不是文件权限边界；
  需要低权限账户/受限令牌 + NTFS ACL 实测）；
- `refresh_known_jobs` 的定时循环（目前是手动调用，scheduler 未做）；
- 多进程写 ledger 的并发验证（当前只验证了单进程多线程）。

## 10. v0.2 / T21：审批安全（风险不可降级 + 原子预留）

### 10.1 修掉的绕过

`DispatchPolicy.effective_risk()` 原来直接返回调用方声明的 `risk`，因此
`DispatchPolicy(mode="shell", isolation="none", allow_unisolated_shell=True, risk="low")`
可以跳过审批。现在改为 **只升不降**：

```python
intrinsic = intrinsic_risk()            # 未隔离 shell / dontAsk / bypass → high
declared  = intrinsic if risk == "auto" else risk
effective = max(intrinsic, declared)     # 声明只能抬高
```

`poc_shell()` 也随之不能再声明 `risk="low"`；PoC 里需要高风险任务的用例改用
`plane.dispatch_approved()`，审批记录里写明 `approved_by=harness`，与人工批准可区分。

### 10.2 审批从"派发后消费"改为"派发前原子预留"

```
计算 digest → 校验策略
  → create_work_item（幂等键唯一索引）
  → add_attempt（预建 attempt，供审批绑定）
  → reserve_approval(digest, attempt_id)   ← BEGIN IMMEDIATE + 条件 UPDATE
  → 调用一次 adapter.dispatch()
      成功 → consume_reserved(id, attempt_id, job_id)
      500/不确定 → 保持 frozen（不可再用）
      确定未创建 → release_reservation（人工批准仍可留给后续修正后的 attempt）
```

`reserved_by` 绑定 **attempt id**（不是 job id），另存 `reserved_at / consumed_at / consumed_by`。

### 10.3 `approve/deny` 的加固

显式传 `approval_id` 时逐项校验：digest 必须一致、状态必须是 `pending`、不得已消费；
`decide_approval` 的 UPDATE 带 `WHERE decision='pending' AND consumed_at IS NULL`，
所以**已拒绝/已消费的审批不可能被"重新批准"**。

### 10.4 T21 回归（`evidence/permissions/T21-approval-security.json`）

| 用例 | 结果 |
|---|---|
| 1. `risk="low"` 降低高风险 | effective=high，派发被拒，worker 调用 0 |
| 2. 8 线程 / 8 个幂等键争一个审批 | `worker_dispatches=1`、成功 1、被拒 7、审批已消费 |
| 3. approval id 与 digest 不匹配 | 拒绝（`bound to digest …, not …`） |
| 4. 已拒绝审批再次批准 | 拒绝（`already 'rejected'`） |
| 5. worker 500（可能已创建） | `dispatch_unknown` + 审批 frozen，二次派发被拒 |
| 6. 预留后控制平面崩溃 | 重启后 `reserved_by` 仍在、未消费，新派发被拒 |
| 7. 改 prompt / 仓库 / 策略 / 断言 | 四个 digest 全部变化，四次派发全部被拒，worker 调用 0 |

### 10.5 工程事实（如实记录）

SQLite 事务与 Worker HTTP 调用无法组成分布式事务，因此这里保证的是
**"同一审批绝不会启动两个 Worker"** 与 **"结果不确定时绝不自动二次执行"**，
而不是跨系统的 exactly-once。要更强保证需要 Worker 侧提供幂等键或可查询的去重记录。

## 11. 复现 / 推送

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

## 11. v0.2 功能实现起步（2026-10-29）

按“先补功能、后做工程基线”的顺序，已把原来只能手动调用的控制平面推进为可常驻运行的
功能切片。本节不改变前面 PoC 对 CodeBuddy 的结论。

### 11.1 自动 Runtime 与长期对账

新增 `controlplane/runtime.py` 与 `scripts/run_controlplane.py`：

- 启动时先 `recover_inflight()`；ledger degraded 时拒绝启动自动变更循环；
- 三条互不阻塞的定时路径：队列派发、已知 job 高频刷新、孤儿 job 低频扫描；
- 单条路径失败指数退避，不停止另外两条路径；
- 支持前台常驻、后台线程嵌入、信号停止与紧凑状态查询；
- 终态任务停止高频查询；孤儿扫描重新看到 job 后清零 miss，避免把非连续缺失累积成 `lost`。

证据 `evidence/recovery/T22-runtime-loop.json`：自动刷新观察到终态、首次孤儿扫描故障后
自动重试、扫描故障不阻断已知任务刷新、inflight 自动恢复、非连续 miss 正确清零、Runtime
干净停止，全部 Pass。

### 11.2 持久任务队列与自动派发

`ControlPlane.enqueue()` 只写 intent，不调用 Worker；Runtime 通过 SQLite 原子 claim 按
`priority DESC, created_at ASC` 派发。新增 `queue_status`、`priority`、`adapter`、
`task_digest` 和 claim 字段：

- 默认并发上限 4，可配置每轮最多派发数；
- baseline 在实际派发前捕获，不在排队时提前捕获；
- Worker job id 在 post-dispatch 状态查询前先落库，查询失败不再丢失已接受的句柄；
- Adapter 抛异常时按 `dispatch_unknown` 处理，禁止盲目重派；
- 高风险 enqueue 自动进入 `needs_approval`，批准后才变 `ready`，派发成功后单次消费审批；
- Runtime 只处理与自身 Adapter 同名的任务，避免一个 Worker 观测/派发另一个 Worker 的记录。

证据 `evidence/jobs/T23-runtime-queue.json`：优先级顺序 `high → mid → low → risky`；
并发上限 2；槽位释放后继续派发；risky 在审批前没有调用 Worker；审批后仍受并发限制，
获得槽位后派发并消费一次审批，全部 Pass。

### 11.3 多进程 claim

`TaskStore.claim_next_ready()` 使用 `BEGIN IMMEDIATE` + 条件 UPDATE。所有共享 SQLite 连接的
读写入口统一使用 store 级 RLock，修复了 Python 3.14 下 8 线程回归出现的
`sqlite3.InterfaceError`。

- T17 连续 5 次回归：每次 8 线程、worker dispatch=1、errors=0；
- `evidence/jobs/T24-multiprocess-claim.json`：两个独立 Python 进程同时竞争同一 ready item，
  只有一个 claim winner、一次 worker dispatch、一个 attempt，全部 Pass。

### 11.4 本地操作 CLI

新增 `scripts/controlplane_cli.py`：`enqueue / list / show / summary / events / approval`。
CLI 只操作 ledger，常驻 Runtime 独占 Worker 调度职责，因此可以在 Worker 暂时不可达时继续
录入任务和审批。

证据 `evidence/jobs/T25-cli-queue.json`：安全任务直接 ready；高风险任务自动进入审批箱；
批准精确 digest 后释放对应队列项；整个 CLI 流程 worker job id 始终为空，证明 CLI 没有
越过 Runtime 直接调用 Worker。

### 11.5 回归

功能实现后重新执行 T17–T25：全部 Pass。T17 的通过条件已收紧为 8 个调用方都必须返回、
`errors == []`，不再只检查“最后恰好派发一次”。当前验证使用本机可用的 Homebrew
Python 3.14；默认 Anaconda Python 的 `_sqlite3` 动态库在导入时失败，属于本机解释器环境
问题，未计为产品测试失败。

### 11.6 项目注册与调度边界

新增持久化 `projects` 实体与 CLI 的 `project add/list/show/enable/disable`。每个项目
记录 Git 顶层根目录、默认 Adapter、默认派发策略、并发上限与启用状态；
work item 持久化 `project_id`。

- CLI 和常驻 Runtime 启用严格注册模式，非注册目录不能进入 Worker；
- `cwd` 在解析软链接和 `..` 后与项目根做包含校验，显式绑定 A 项目时不能穿越到 B；
- 禁用项目拒绝新 enqueue/dispatch，已排队任务在重新启用前不会被 claim；
- `BEGIN IMMEDIATE` 原子 claim 同时应用 Adapter 全局上限与项目上限，繁忙项目不再
  占用其他项目可使用的槽位。

证据 `evidence/jobs/V02-005-project-registry.json`：两个真实临时 Git 仓库下，未注册路径、
跨项目路径和禁用项目均被拒绝；全局上限 3 时首批为 `a1 + b1 + b2`，`a2`
等待 A 的单项目槽位，`a1` 终止后立即派发，全部 Pass。T17–T25 同步回归仍全部 Pass。

## 12. 评审第 3 轮修复（T26，2026-10-29）

针对源码复核确认的 5 个问题（2×P0、3×P1）逐项修复，证据
evidence/recovery/T26-p0-fixes.json。

### 12.1 P0：Ledger 降级状态跨重启持久化

修复前，fail-closed 只对发现损坏的那个进程有效：截断后的空库在下一次启动通过
integrity_check，降级标记 LEDGER_DEGRADED.txt 从未被读回，派工禁令形同虚设。

- 降级标记改为 per-db 文件 <ledger>.DEGRADED（旧共享文件名会把同目录其他测试
  库一并降级），TaskStore.__init__ 启动时读回：只要标记存在即降级，与
  integrity_check 结果无关；
- 唯一解除入口是显式人工操作 TaskStore.recover_degraded(operator_note=...)
  （CLI：controlplane recover-ledger --note ...）。空 note 直接拒绝，恢复动作
  追加审计到 .poc-state/LEDGER_RECOVERY.log；
- legacy 	asks.json 迁移失败同样写入持久化标记。

T26-A：损坏 → 降级 → 进程退出 → 重启 → 派工仍被拒、Worker 零调用；空 note 恢复
被拒；带 note 恢复后派工恢复、标记删除、新进程不再降级。

### 12.2 P0：lost 误判统一为「缺席是疑点，死亡要证据」

- 新增统一失联判定 _assess_lost()：连续两次全量扫描缺失只产生 suspected_lost
  （状态保持、queue_status=running、并发容量不释放、metadata 记录疑点），只有
  独立证据（dapter.pid_alive() 确认进程已死）才判 lost 并释放容量；
- _confirm_lost() 同步收紧：adapter 自报 lost 但 PID 缺失或探测不可用时不再
  接受，改为 suspected_lost + LOST_UNCONFIRMED 告警；
- summary() 新增 suspected_lost 计数与人工裁决提示。

T26-B：两次扫描缺失 + PID 存活 → 不 lost、容量保留；死亡证据到达 → lost、容量
释放；adapter 报 lost 但无 PID → 保持原状态。T18 增加 B2 用例同步回归。

### 12.3 P1：多 Runtime 恢复竞争（claim 租约）


ecover_inflight() 增加 DISPATCH_CLAIM_LEASE_S = 300s 租约：另一 Runtime 在
租约内的活跃 claim 被跳过（skipped_active_claims），只有过期或无主 claim 才被
恢复为 dispatch_unknown。T26-C：新鲜 claim 原样保留，过期 claim 被接管。

### 12.4 P1：Attempt 残留中间状态

审批预留失败的 Attempt 现在写入 locked 终态（含 ended_at），Worker 明确拒绝
时写入 borted_before_dispatch，成功派发标记 dispatched（不结束）、结果不确定
标记 dispatch_unknown（不结束）。T21 并发竞争的 8 个 Attempt 中 7 个现为
locked，不再被误认为执行历史。

### 12.5 P1：证据脱敏

- scripts/sanitize_evidence.py 新增跨机器用户名兜底正则（/Users/<name>、
  C:\Users\<name>，含 JSON 转义形式），清理了 V02-005 / T25 证据中生成机
  的真实主目录路径；
- 新增 write_evidence() / sanitize_text() 可复用入口：证据序列化后、写盘前
  自动脱敏，T26 起的新证据脚本使用该入口。

### 12.6 回归

本机（Windows，慢磁盘）全量重跑：T17、T18、T19、T20、T21、T22、T23、T24、T25、
V02-005、T26 全部 Pass。附带环境修复：清理 sandbox 中 36 个历史遗留临时 Git 仓库
（每次派发的 sibling 指纹扫描因此从约 40s 降至毫秒级）；T22/T23/T24 的测试 cwd
移入 sandbox/runtime-tests/ 隔离子目录，等待窗口放宽至与磁盘速度解耦。

### 12.7 未完成（如实记录）

- Windows OS 级隔离未做：项目注册只是控制平面侧路径约束，挡不住具有宿主权限的
  Worker 进程；
- 第二 Worker Adapter、Foreman 未启动；
- Ledger 跨重启降级在多 db 文件并存时依赖 per-db 标记文件，
ecover_degraded
  不校验隔离副本内容是否已被人工真正检查，仅强制留痕。

## 13. Project Context Update Loop（M2，T31）

M2 在 M1 的 Project Context Store 之上补齐“验收后回写”闭环。终态项目任务的
`verified / rejected / inconclusive` verdict 会触发确定性更新：生成一个叙述性
`progress` 候选和一个从 attempts、events、assertion_results、warnings 提取的
`knowledge` 候选。自动内容一律为 `candidate`，不会创建或修改 `constraints`。

`context_update_receipts` 以 `(task_id, attempt_id, entry_kind, entry_title)` 的哈希为
数据库唯一键，并使用跨进程 claim 防止重复回写。已有条目的更新沿用 CAS；两个独立
SQLite 连接从同一版本并发提交时，仅一个更新规范条目，另一个被保留为独立
`needs_merge` 候选，双方内容都不丢失。

每个 attempt 首次回写前生成脱敏的 `evidence/project-updates/...` 快照；候选条目必须
引用真实存在的 evidence 文件。回写失败不会改变已经持久化的任务 verdict，而是记录
`project_context_update_failed`，可通过 `update_project_context()` 或 CLI
`project context-sync-task` 幂等重放。

轮询路径还会调用 `reconcile_project_context_updates()`：只扫描当前 attempt 缺少最终回执的
终态任务，补齐“verdict 已提交、进程在回执提交前崩溃”的窗口；完成回执不会在每轮重复处理。

证据 `evidence/project/T31-context-update-loop.json`：三类 verdict 映射、重复重放、
双连接 CAS 冲突保留、自动 verified/constraints 禁止、跨项目拒绝全部 Pass。
真实 CodeBuddy Worker 与自动回写的组合尚未端到端执行，仍标记为未验证。
