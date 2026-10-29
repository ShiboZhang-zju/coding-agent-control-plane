# 生命周期：Job / Session / CLI 对话 / 系统进程

这是本 PoC 最容易被混淆的一节。四个东西**不是**同一条命：

| 实体 | 谁创建 | 结束条件 | 能否在控制平面之外存活 | 证据 |
|---|---|---|---|---|
| **系统进程**（cmd/node 子进程） | Job 启动时 spawn | 命令退出 / 被杀 | **否**：服务一停，子进程随之被杀 | `evidence/recovery/T08c-exec-survival-precise.json` |
| **CodeBuddy Job** | `POST /api/v1/jobs` | 进入 `done/failed/stopped` | 是（落盘在 `~/.codebuddy/jobs/<id>/state.json`） | `evidence/jobs/T01-concurrency.json` |
| **Session**（对话） | Job 创建时生成 `sessionId` | 归档 / 删除 | 是（可 `resume` 成新 Job） | `evidence/recovery/T07-resume-respawn.json` |
| **CLI 对话**（`codebuddy -p` / TUI） | 用户命令 | 进程退出 | 仅通过 `--resume <sessionId>` 再开 | `evidence/environment/codebuddy-help.txt` |

## 关键关系

```
control-plane task (cp-xxxx)
   └── worker job id (8 hex)               ← adapter 维护映射，落盘在 .poc-state/
         ├── sessionId (uuid)              ← 对话上下文，跨 job 可复用
         ├── pid (仅运行期有效，可能为 null)
         └── worktree (agent 任务隔离时存在)
```

- **Job 是持久实体，pid 不是**。任务结束后 `pid` 为 `null`；重启服务后已完成的 Job 依然可查
  （`GET /api/v1/jobs/{id}` 毫秒级返回）。
- **Session 可复用，执行不可复用**。`resume` / `respawn` 拿回的是对话上下文；被打断的命令不会
  从断点继续——这是"对话恢复 ≠ 执行恢复"的直接依据。
- **Job 的终态由 worker 自己写**。进程被杀时 worker 可能永远停在 `working`
  （`alive=false, settled=false, pid=null`），必须由控制平面用"进程是否存活 + 记录是否停滞"
  判定 `lost`（`evidence/recovery/T02-lost-job.json`）。
- **worktree 是 agent 任务独有的隔离**：`sandbox/repo-a/.codebuddy/worktrees/bg-<jobid>`，
  分支 `worktree-bg-<jobid>`；shell 任务没有这层隔离（`T03`）。

## 状态迁移（实测）

```
POST /api/v1/jobs                    → state=working, alive=true, pid=<n>
  ├─ 正常结束                         → state=done, settled=true, alive=∅, firstTerminalAt 写入
  ├─ POST /jobs/{id}/stop             → state=stopped（1.9s 内）
  ├─ 进程被杀（worker 不知道）         → 永远 working（需要 plane 侧 120s 停滞判定 → lost）
  └─ 服务重启                         → Job 记录仍在（可查），但子进程已死、命令不会继续，
                                        状态永久停在 working（T08c）
```

服务重启的准确结论（修正前一版）：

- `stop` / 重启服务会**连带杀死 Job 的子进程**（T08c：`pid_alive_after_stop=false`，
  30 秒后应有的产物从未出现）；
- Job 记录仍可查询（`GET /jobs/{id}` 返回 200），但**执行不会续跑**，也**不会**自动变成
  `failed` —— 它会一直停在 `working`；
- 因此"执行恢复"能力为 **不支持**，恢复必须由控制平面发起**新的 Attempt**
  （`work_item -> attempt` 映射在 `controlplane/store.py`）。

## 控制平面必须自己维护的部分

1. `work_item ↔ run_attempt ↔ worker_job ↔ session` 映射（worker 只存 job→session，
   不存"这次是第几次执行"）；
2. 幂等键与 `dispatch_unknown` 状态（worker 无 idempotency key 字段，且 500 可能已创建任务）；
3. `lost` 判定与对账周期；
4. 验收结论（产物归属断言 vs worker 终态）；
5. 审批待办队列（worker 只有 `waitingFor` 字符串，background job 甚至直接 auto-deny）。
