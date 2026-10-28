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
| `dispatch(req)` | 起任务；返回 `worker_job_id`；幂等键不可用时在 `warnings` 里说明 | 静默重试到"看起来成功" |
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

## 4. 验收规则（`controlplane/verify.py`）

```
worker_state ∈ {failed, cancelled, lost}          → rejected
worker_state ≠ succeeded                          → pending
没有声明任何断言                                    → inconclusive
断言全部通过                                        → verified
否则                                               → rejected
```

推论：

- "命令成功退出" ≠ "通过验收"；
- "文件存在" 也不够，要断言**内容/提交/副作用不存在**；
- 隔离执行（worktree）时断言必须跑在 worktree 根目录（`evidence/isolation/T03b-worktree-agent.json`
  的 recheck 一节记录了第一次评审用错根目录导致的假失败）。

## 5. 接入下一个 Worker 的最小工作量

一个 Worker 只要满足下面 4 条，就不需要改控制平面：

1. 能非交互派发一个任务并立刻返回句柄；
2. 能按句柄查状态（含"进程是否还活着"的可判定信号）；
3. 能枚举全部任务（或至少在本地持久化 + 可重放）；
4. 有可枚举产物（文件系统 / git / 日志）。

不满足第 3 条的（例如只支持 stdio 的 ACP harness），Adapter 必须自己维护句柄表，
`reconcile()` 只能返回"未知"而不是伪装成全量已知——这时控制平面会把这些任务标成
`unknown`，而不是 `succeeded`。
