# 能力矩阵（CodeBuddy 2.169.0，Windows 11）

分类：**原生支持** / **需要配置** / **需要 Adapter 开发** / **需要控制平面开发** / **不支持** / **未验证**

| 能力 | 结论 | 实测证据 | 说明 |
|---|---|---|---|
| 任务派发 | 原生支持 | `evidence/jobs/T01-concurrency.json` | `POST /api/v1/jobs`，返回 `id/sessionId/cwd/state` |
| 后台运行 | 原生支持 | `evidence/jobs/T00-api-probe.json` | `kind=background`，CLI 侧 `--bg` 亦存在 |
| 并发任务（shell） | 原生支持 | `evidence/jobs/T01-concurrency.json` | 4 个 shell job 同时 `running`（peak=4），互不阻塞 |
| 并发任务（真实 Agent） | 需要配置 | `evidence/isolation/T11-4way-success.json` | 4 路真实 agent 并行：peak=4、4 个独立 worktree/分支、4 个产物全部归属正确、全部 `verified`。前提：跳过用户级 hook（`--setting-sources project,local`），否则 5s `UserPromptSubmit` 超时会让并行会话集体失败 |
| 幂等派发 | 需要控制平面开发 | `evidence/jobs/T12-idempotency.json`、`T17-concurrent-idempotency.json` | 顺序重复与 **8 线程同时**同 key 均只起 1 个 job / 1 个 work item / 1 个 attempt |
| 派发不确定（dispatch_unknown） | 需要控制平面开发 | `evidence/jobs/T17-concurrent-idempotency.json` | 查不到**不再**等于"未创建"：保持 `dispatch_unknown`，需人工 `confirm_absent` 才允许新 attempt |
| worktree 隔离 | 需要配置 | `evidence/isolation/T03b-worktree-agent.json` | agent 任务传 `bgIsolation=worktree` → `.codebuddy/worktrees/bg-<job>` + 分支 `worktree-bg-<job>` |
| 文件级隔离（shell 任务） | 不支持 | `evidence/isolation/T03-isolation.json` | repo-b 派发的 shell 任务成功写入 `../repo-a/cross_from_b.txt` |
| 状态查询（单任务） | 原生支持 | `evidence/recovery/T08-service-restart.json` | `GET /api/v1/jobs/{id}` 毫秒级 |
| 全局任务查询 | 原生支持（性能差） | `evidence/jobs/T00e-list-latency.json` | `?all=1` 每次约 30.3s；`?all=0`、`?cwd=` 同样 30s |
| SSE | 原生支持 | `evidence/sse/T00c-sse-format.json` | `event: snapshot/added/changed/keepalive`；`text/event-stream` |
| SSE 断线重连 | 原生支持 | `evidence/sse/T05-sse-reconnect.json` | 可重新订阅，但只得到当前状态 |
| 事件 replay | 不支持 | `evidence/sse/T05c-sse-isolated.json` | 无 `id:` 字段、无 Last-Event-ID 游标，断线期间事件不可补发 |
| 状态对账 | 需要控制平面开发 | `evidence/recovery/T18-refresh-split.json` | 两条路径：`refresh_known_jobs()`（逐 job、有超时、局部成功）+ `discover_orphan_jobs()`（低频全量、失败不影响已知任务） |
| Ledger 降级 fail-closed | 需要控制平面开发 | `evidence/recovery/T19-ledger-fail-closed.json` | 完整性校验失败即拒绝 dispatch/resume/respawn，仅允许查询与诊断 |
| cancel / stop | 原生支持 | `evidence/recovery/T06-cancel-stop.json` | `POST /jobs/{id}/stop`，1.9s 内变 `stopped` |
| resume | 需要 Adapter 开发 | `evidence/recovery/T06-cancel-stop.json`、`T07` | 仅对归档会话（`/jobs/resumable`）；运行中 job 会 500（锁超时） |
| respawn | 原生支持（受限） | `evidence/recovery/T07-resume-respawn.json` | `POST /jobs/{id}/respawn`，重开对话；不重启被中断的命令 |
| 对话恢复 | 原生支持 | `evidence/recovery/T07-resume-respawn.json` | `sessionId` 可复用 |
| 执行恢复 | 不支持 | `evidence/recovery/T08c-exec-survival-precise.json` | 服务停止时子进程被杀；重启后命令不会继续，artifact 永不出现 |
| 工作流恢复 | 需要控制平面开发 | `evidence/recovery/T08-service-restart.json` | 控制平面凭 `.poc-state` 重建任务表（实测重建成功） |
| 权限请求（前台/TUI） | 需要配置 | `evidence/environment/codebuddy-help.txt` | `--permission-mode` 六种模式 |
| 权限请求（background job） | 不支持 | `evidence/permissions/T09-permission-denied.json` | 不进入 `waiting_for_approval`，工具调用直接 auto-deny |
| 人工拒绝 | 未验证（无路径） | `evidence/permissions/T09-permission-denied.json` | background job 上不存在人工点击拒绝的入口；拒绝后无副作用已验证 |
| 审批门控（控制平面侧） | 需要控制平面开发 | `evidence/permissions/T20-approval-gate.json` | 高风险策略必须持有 digest 匹配且未消费的审批；内容被改即失效；一次性 |
| 审批投递 API | 不支持 | `docs/adapter-contract.md` | `/jobs/{id}/reply` 只能注入用户消息；`/permission-bridge/request` 面向 teammate→leader |
| 产物收集 | 需要 Adapter 开发 | `evidence/isolation/T03b-worktree-agent.json` | git status/log + `state.json.logPath` |
| 失败识别 | 需要控制平面开发 | `evidence/jobs/T00b-state-semantics.json` | `exit 3` 仍报 `done`，无退出码字段 |
| 自动验收 | 需要控制平面开发 | `evidence/jobs/T04-acceptance.json` | 产物断言：`verified 3 / rejected 1` |
| 产物归属（防假成功） | 需要控制平面开发 | `evidence/jobs/T13-fake-success.json` | 旧产物/他人产物/未发生的提交全部 `rejected`；两个正向对照 `verified` |
| 越界副作用否决 | 需要控制平面开发 | `evidence/isolation/T14-cross-project-rejected.json` | 自己的断言通过、worker 报 `done`，仍因跨项目写入被判 `rejected` |
| 派发策略强制 | 需要控制平面开发 | `evidence/jobs/T12-idempotency.json` | 无 `allow_unisolated_shell` 的 shell 派发直接 `ValueError` |
| 失联判定（lost） | 需要控制平面开发 | `evidence/recovery/T02-lost-job.json` | `working + alive=false + pid 死 + 停滞>90s` → `lost` |
| 主 Agent 汇总 | 需要控制平面开发 | `evidence/jobs/T10-summary.json` | `plane.summary()` 一次拿到全部任务/阻塞/下一步 |
| ACP | 未验证 | `openapi/codebuddy-openapi.json` | `/api/v1/acp`、`/api/v1/acp/connect` 存在，未做端到端会话 |
| Multitask | 未验证（不可用） | `evidence/environment/codebuddy-help.txt` | 帮助明确 "Interactive TTY only"，headless 不可用 |
| Adapter 接入难度 | 需要 Adapter 开发 | `docs/adapter-contract.md` | 单个 adapter 文件 + 状态映射 + 2 处 workaround（dispatch 500、列表 30s） |

## 环境级缺陷（不属于 CodeBuddy 设计，但会误导结论）

| 缺陷 | 现象 | 处置 |
|---|---|---|
| 桌面注入的 safe-delete shim | `POST /api/v1/jobs` 恒 500（`safe-delete ... Os code: -2147024894`），job 有时创建成功但索引不更新 | `scripts/serve_ctl.py` 启动子进程时剔除 `NODE_OPTIONS`/`CODEBUDDY_SAFE_DELETE_*`，恢复 200 |
| job store 读接口 30s | `?all=1`、`?all=0`、`?cwd=`、`/jobs/resumable` 全部 ~30.3s | adapter 单任务轮询用 `/jobs/{id}`，全量查询仅用于 reconcile 并把超时提到 180s |
| 终态写盘不稳定 | 部分 shell 任务命令已执行完，但 `state` 恒为 `working`、`firstTerminalAt=null` | 控制平面不依赖终态：`lost` 判定 + 产物断言 |
