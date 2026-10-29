# Worker Adapter 契约（厂商无关）

代码：`adapters/base.py`。本文件解释为什么这样切，以及接入下一个 Worker 时必须满足什么。

## 1. 为什么要有这层

控制平面要回答的问题是"**这个任务到底成没成**"，而不同 Harness 给出的信号质量差异极大。
如果不把这层抽象出来，控制平面就会被迫相信某个厂商的终态字符串——而实测中 CodeBuddy 对
`exit 3` 的 shell 任务照样报 `done`（`evidence/jobs/T00b-state-semantics.json`）。

因此契约把三件事分开：

| 层 | 回答什么 | 谁负责 |
|---|---|---|
| `TaskSnapshot.worker_state` | Worker 自己怎么说 | Adapter（映射厂商状态） |
| `TaskSnapshot.verdict` | 控制平面验收结论 | 控制平面（产物断言） |
| `reconcile()` | 真实状态到底是什么 | Adapter 重新查询 + 控制平面落库 |

## 2. 方法语义

| 方法 | 必须做到 | 不允许 |
|---|---|---|
| `capabilities()` | 每项能力给出等级 + 证据文件路径 | 把"文档写了"当成"已验证" |
| `dispatch(req)` | 起任务；返回 `worker_job_id`；**每次调用只 POST 一次**；结果不确定时置 `uncertain=True` | 静默重试到"看起来成功"（第二次 POST 可能产生重复任务） |
| `get_task(id)` | 单个任务的当前快照 | 用本地缓存冒充实时状态 |
| `list_tasks()` | 已知任务全集 | 把不可达任务当成不存在（应报 `lost`） |
| `stream_events()` | 事件标准化；无游标时 `event_id/cursor = None` | 把轮询包装成"事件重放" |
| `cancel(id)` | 请求停止并返回停止后的快照 | 停止后仍报 `succeeded` |
| `resume(id)` | 恢复**对话** | 声称恢复了**执行** |
| `respawn(id)` | 新进程绑定旧会话 | 伪造 pid |
| `request_approval()` | 能真正投递决策就投递 | 做不到就抛 `UnsupportedOperation` |
| `collect_artifacts()` | 枚举文件/提交/日志 | 只返回"应该存在"的路径清单 |
| `reconcile()` | 重新查询真实状态并报告变化 | 编造缺失的历史事件 |

## 3. 统一状态机

```
queued → starting → running → ┬→ waiting_for_approval → running …
                              ├→ succeeded
                              ├→ failed
                              ├→ cancelled
                              └→ lost          （进程没了但 worker 还报 running）
unknown                                      （拿不到可信信息）
```

映射要求：

- `lost` 必须由"进程是否还活着 + 记录是否还在动"联合判定，不能只看一个布尔值。
  CodeBuddy 的 `alive` 字段对健康任务也会瞬时为 `false`（`evidence/jobs/T01-concurrency.json`
  早期样本出现 `running → lost → succeeded` 抖动）。
- `waiting_for_approval` 必须有可读的阻塞原因（`waitingFor` / `needs` / `block`）。
- 任何厂商状态字符串只能出现在 adapter 内（`_STATE_MAP`），不得进入控制平面。

## 4. 派发策略（`adapters/base.py::DispatchPolicy`）

- **没有隐式默认**：`controlplane.policies.AGENT_WORKTREE` 是无人值守派发的默认策略
  （agent 会话 + 独立 worktree）；`DispatchPolicy(mode="shell", isolation="none")`
  会直接 `raise ValueError`，必须显式传 `allow_unisolated_shell=True`（只用于一次性沙箱）。
- `bypassPermissions` 在 `validate()` 里被直接拒绝，不是靠调用方自觉。
- 每次派发都把策略原文写进 ledger（`policy_json`），风险项进 `warnings`
  （如 `UNISOLATED_SHELL`）。

## 5. `dispatch_unknown`：不确定就不重试

worker 可能"报错但已创建"。因此：

```
POST → 200 + job id            → 正常绑定
POST → 非 200，但能查到匹配 job → 绑定（DISPATCH_RECOVERED_BY_LIST）
POST → 非 200，查不到           → state=dispatch_unknown（不重试）
dispatch_unknown --resolve_dispatch()--> 绑定 or 证明不存在 → 才允许新 attempt
```

匹配键是 `(cwd, prompt, startedAt >= 本次 POST 时间)`，不是"用户觉得应该只有一个"。

## 6. 验收规则（`controlplane/verify.py`）

```
side_effect_violations 非空                        → rejected（优先级最高）
worker_state ∈ {failed, cancelled, lost}          → rejected
worker_state ≠ succeeded                          → pending
没有声明任何断言                                    → inconclusive
断言全部通过（且每个产物归属本次 attempt）           → verified
否则                                               → rejected
```

**归属（provenance）规则**：dispatch 前先 `capture_baseline()`（cwd 的 HEAD/status、
每个断言目标的 sha256、所有同级仓库的指纹）。断言通过要求：

- `file_exists`：文件不存在 → 失败；基线里没有 → 通过；基线里有但内容变了 → 通过；
  基线里有且没变 → 失败（"上一次的产物"不算这次做的）；
- `git_commit`：提交必须出现在 `baseline_head..HEAD`，旧提交不算；
- 未在断言中声明的本地改动 → `undeclared_local_change`；
- 任何**同级仓库**指纹变化 → `cross_project_change`（`.codebuddy/` 工作区簿记除外），
  直接否决，哪怕自己的断言全通过。

推论：

- "命令成功退出" ≠ "通过验收"；
- "文件存在" 也不够，要证明**是这次产生的**；
- agent 任务在 worktree 里执行时，断言必须跑在 worktree 根目录（`_snapshot_patch` 已处理）。

对应证据：`evidence/jobs/T13-fake-success.json`（旧产物/他人产物/未发生的提交全部 rejected，
两个正向对照 verified）、`evidence/isolation/T14-cross-project-rejected.json`。

## 7. 轮询与对账：两条路径，绝不互相拖累

```
refresh_known_jobs(timeout_s)   高频  逐 job 查询；单次超时；失败=观测异常，状态不变
discover_orphan_jobs()          低频  全量扫描；失败=返回 ok=False，已知任务不受影响
```

- **失败 ≠ 死亡**：查询超时只记 `observation_failed` 与 `observe_failures` 计数；
  `lost` 需要独立证据（pid 不存活，或连续两次全量扫描都看不到该 job）。
- adapter 报 `working+alive=false` 时，plane 会用 `adapter.pid_alive()` 复核；
  复核不确定则保持原状态并记 `LOST_UNCONFIRMED`。

## 8. Ledger 降级 = 只读

`TaskStore.assert_writable(op)`：`degraded=True`（完整性校验失败、已从空库启动）时
`dispatch / resume / respawn / approve / deny / request_approval` 全部抛 `LedgerDegraded`。
理由：空库不知道哪些任务正在跑，继续派工会造成重复执行。
查询、`summary()`、事件与证据导出仍然可用。

启动时 `recover_inflight()` 收口另一个撞车窗口：**意图已落库但 job id 未写入**
（POST 成功后进程崩溃）→ 置 `dispatch_unknown`，禁止盲目重派。

## 9. 审批：绑定内容，派发前原子预留，一次一用

```
digest  = sha256(prompt.strip() + resolved(cwd) + policy.to_dict() + assertions)
intrinsic = intrinsic_risk()                       # 未隔离 shell / dontAsk / bypass → high
effective = max(intrinsic, declared_risk)          # 声明只能抬高，不能降低
```

派发顺序（T21 之后）：

```
create_work_item(idem key, UNIQUE) → add_attempt → reserve_approval(digest, attempt_id)
  → adapter.dispatch() 一次
      成功        → consume_reserved(attempt_id, job_id)
      不确定(500) → 保持 reserved（frozen），绝不复用
      确定失败     → release_reservation（人工批准仍可给后续修正后的 attempt）
```

`reserve_approval()` 用 `BEGIN IMMEDIATE` + 条件 UPDATE，所以**一个审批最多授权一次派发**；
`reserved_by` 记的是 attempt id，不是 job id。`approve/deny` 传入 approval id 时会校验
digest 一致、状态为 `pending`、未消费——已拒绝或已消费的记录不可能被重新批准。

| 状态 | 含义 | 允许的动作 |
|---|---|---|
| `pending` | 等待人工决定 | `approve()` / `deny()` |
| `approved` + 未消费 | 内容与 digest 一致，可执行一次 | `dispatch()` 时原子消费 |
| `consumed` | 已绑定某个 worker job | 再派发需重新审批 |
| `rejected` | 人工拒绝 | `dispatch()` 抛 `ApprovalRequired` |

关键约束：**批准某个操作 ≠ 批准 Agent 之后改过的 prompt**。
改了 prompt/policy/断言/cwd 中任何一项，digest 就变，旧审批自动失效。

## 10. 接入下一个 Worker 的最小工作量

一个 Worker 只要满足下面 4 条，就不需要改控制平面：

1. 能非交互派发一个任务并立刻返回句柄；
2. 能按句柄查状态（含"进程是否还活着"的可判定信号）；
3. 能枚举全部任务（或至少在本地持久化 + 可重放）；
4. 有可枚举产物（文件系统 / git / 日志）。

不满足第 3 条的（例如只支持 stdio 的 ACP harness），Adapter 必须自己维护句柄表，
`reconcile()` 只能返回"未知"而不是伪装成全量已知——这时控制平面会把这些任务标成
`unknown`，而不是 `succeeded`。
