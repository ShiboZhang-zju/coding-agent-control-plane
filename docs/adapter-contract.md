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

### 4.1 无人值守会话的权限（CodeBuddy 2.179.3 实测）

`permission_mode` 不是"能不能用 shell"的开关。实测结论（`evidence/permissions/T68-worker-permissions.json`）：

| 层 | 是否影响 HTTP 派发的 job |
|---|---|
| job 请求体 `permissionMode` | 只自动批准文件编辑（`acceptEdits`）；Bash/PowerShell 仍要审批 |
| `--serve --allowedTools/--disallowedTools` | **不影响**（进程级 CLI 选项） |
| `--serve --settings <file>` | **不影响** |
| `$CODEBUDDY_CONFIG_DIR/settings.json` 的 `permissions.allow/deny` | **生效**，且 `deny` 压过 `allow` |

因此 Worker 的最小权限必须写成 settings 里的显式 allow 清单
（`adapters/codebuddy/permissions.py` + `scripts/serve_ctl.py --worker-profile`），
而不是放宽 `permissionMode`。未列出的命令一律拒绝；不带任何规则时默认分类器
是**间歇性**的（同一命令可能一次被拒一次放行），不可作为生产依据。

规则按单条命令匹配：`git add -A && git commit -m x` 会被拒绝，两条分开发起才放行，
所以 Effective Prompt 要求一次 shell 调用只跑一条命令。

被拒绝时 job 仍报 `state=done`，拒绝只出现在 `detail` / `output` 里
（该版本 transcript 端点返回 0 条更新）。适配器已把这种情况归一化为
`failed` + `PERMISSION_DENIED:` warning；同样是假成功形状的
`Authentication required` 归一化为 `failed` + `WORKER_NOT_AUTHENTICATED:`。
两者都会进入 Ledger `warnings`，但**人工处理标记不同**：

| warning | `attention_flags` | 人工要做什么 |
|---|---|---|
| `PERMISSION_DENIED:` | `permission_blocked` | 决定是否授予该能力、改任务，还是放弃 |
| `WORKER_NOT_AUTHENTICATED:` | `worker_not_authenticated` | 修复 Worker 登录/凭据；改任务无意义 |

#### 4.1.1 这是审批策略，不是安全沙箱

allow/deny 决定的是"无人值守时 Agent 可以请求哪些工具调用"，**不限制获批命令
实际能做什么**。有两点不能含糊：

- `Bash(python:*)` / `PowerShell(python3:*)` 在清单里，因为无人值守任务必须能跑
  自己的测试。一旦放行，任意 Python 程序就拥有 Worker 账户的全部权限：可以写
  仓库以外的路径、读该账户能读的任何文件、发起网络连接。**任何命令白名单都无法
  约束任意解释型代码**，宣称可以约束是错的。
- `Write` / `Edit` / `NotebookEdit` 同样是宽泛的：目标路径不受检出目录限制。

因此生产环境的真实边界在进程之外：

1. **独立 Windows 账户**（专用低权账户，非交互登录，无本机管理员、无 `SeDebug`、
   无 `Backup Operators`），Worker 进程只以该账户运行。
2. **目录 ACL**：该账户仅对隔离检出目录有写权限；工具链目录只读；`~/.codebuddy`
   与 Ledger 目录对该账户不可写（Worker 只通过 HTTP 被派发，不直接触账本）。
3. **凭据隔离**：只提供 Worker 自己所需的模型访问凭据；不放长期高权 token，不给
   其它 SaaS 登录态，凭据文件 ACL 限该账户。
4. **网络出口**：出站仅允许模型端点（域名/IP + TLS），屏蔽任意入站；禁止把 8787
   端口转发出去（本就只绑 loopback）。
5. **可抛弃性**：检出目录与账户状态可整机销毁重建；任何跨任务状态都通过控制平面，
   不留在 Worker 账户里。

`bypassPermissions` / `-y` 在任何情况下都不是替代品。

#### 4.1.1b 执行契约必须与最终 DispatchPolicy 一致

Effective Prompt 里"你的工作目录已经隔离"这句只在 `isolation="worktree"` 时成立。
`isolation="none"` 的任务跑在**真实检出**里，对它说"你在一个可抛弃的 worktree 里"
会让 Agent 放心修改真的仓库。所以：

| 最终 policy 的 isolation | Prompt 里的声明 |
|---|---|
| `worktree` | 当前目录已是隔离检出，不要另建 worktree |
| `none` / 空 / 未知值 | **未隔离**，这就是项目检出本身，不会被回滚 |

"最终"指 `ControlPlane.enqueue` 真正会用的那个：显式参数 → 项目注册的 policy →
控制平面默认。`ProjectService._effective_policy()` 用同一套优先级解析后再构建
Prompt，因此项目默认 policy 覆盖调用方参数时，Prompt 也跟着改口；预览
（`build_task_context`）与提交（`submit_project_task`）对同一 policy 产出同样字节。
两段共用的其余契约（拒绝即停、一次一条命令、不要 push）与 isolation 无关，两种
模式下都在。Prompt 仍然在提交时冻结，digest 与幂等键不变。

#### 4.1.2 配置目录的写入边界

`--worker-profile` 只允许写入**经过校验的隔离目录**，写入前必须：

- 绝对路径、经 `os.path.realpath` 解析（挡住 `..` 与 NTFS junction）；
- 不等于、不在其内、也不是 `~/.codebuddy`（或本进程 `$CODEBUDDY_CONFIG_DIR`）的
  父目录，否则拒绝启动（`WorkerConfigError`）；
- **先校验、后创建**：`serve_ctl start` 先跑 `_plan_config_dir()`（含 profile 名校验），
  只有全部通过才 `mkdir` 并写 settings。非法路径不会留下空目录；
- profile 名必须命中 `permissions.known_profiles()`（或显式 `none`）。写错名字直接
  拒绝——静默退回默认分类器等于没有策略；
- 任何配置问题统一输出 `{"result":"refused","reason":...}` 并以非零码退出，不启动
  服务、不部分生效；
- 目标 settings 文件存在但**解析失败时 fail-closed**：不重写、不清空，直接报错
  退出。重写会同时丢掉 `gateway` 凭据并把"未知策略"换成"我想要的新策略"；
- 写入用同目录临时文件 + `os.replace` 原子替换，`fsync` 后改名，失败清理临时文件；
- `permissions.allow` 由已审核清单**替换**而非并集继承。继承旧 allow 会把
  `Bash` / `Bash(*)` / `mcp__*` 之类宽泛授权原样留下，等于没做最小权限；
  被丢弃的旧规则记入 `dropped_allow_rules` 供审计。`deny` 只做并集。

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

- **失败 ≠ 死亡，缺席 ≠ 死亡**：查询超时只记 `observation_failed` 与 `observe_failures` 计数；
  连续两次全量扫描缺失只产生 `suspected_lost`（状态保持、并发容量不释放），
  `lost` 只能由独立死亡证据（`adapter.pid_alive()` 确认进程已死）得出。
- adapter 报 `working+alive=false` 时，plane 会用 `adapter.pid_alive()` 复核；
  复核不确定或无 PID 可查时保持原状态并记 `LOST_UNCONFIRMED` / `suspected_lost`，
  等待人工裁决。

## 8. Ledger 降级 = 只读（跨重启）

`TaskStore.assert_writable(op)`：`degraded=True`（完整性校验失败、已从空库启动）时
`dispatch / resume / respawn / approve / deny / request_approval` 全部抛 `LedgerDegraded`。
理由：空库不知道哪些任务正在跑，继续派工会造成重复执行。
查询、`summary()`、事件与证据导出仍然可用。

降级状态持久化在 per-db 标记文件 `<ledger>.DEGRADED`：重启时空库虽然能通过
integrity_check，标记会把禁令重新装上。唯一解除入口是人工操作
`TaskStore.recover_degraded(operator_note=...)`（CLI `recover-ledger --note ...`），
必须留下可审计的说明，动作追加到 `.poc-state/LEDGER_RECOVERY.log`。

启动时 `recover_inflight()` 收口另一个撞车窗口：**意图已落库但 job id 未写入**
（POST 成功后进程崩溃）→ 置 `dispatch_unknown`，禁止盲目重派。多 Runtime 并存时，
其他实例在租约期（默认 300s）内的活跃 dispatch claim 会被跳过，只有过期或无主的
claim 才被接管。

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

Attempt 状态（T26 之后）区分"执行历史"与"从未执行的竞争残留"：

| attempt 状态 | 含义 | `ended_at` |
|---|---|---|
| `dispatched` | 已绑定 worker job，正在/已经执行 | 空（未结束） |
| `dispatch_unknown` | POST 结果不确定，等待 resolve | 空（未结束） |
| `blocked` | 审批预留竞争失败，**从未调用 Worker** | 已结束 |
| `aborted_before_dispatch` | Worker 明确拒绝，未创建 job | 已结束 |

## 10. 接入下一个 Worker 的最小工作量

一个 Worker 只要满足下面 4 条，就不需要改控制平面：

1. 能非交互派发一个任务并立刻返回句柄；
2. 能按句柄查状态（含"进程是否还活着"的可判定信号）；
3. 能枚举全部任务（或至少在本地持久化 + 可重放）；
4. 有可枚举产物（文件系统 / git / 日志）。

不满足第 3 条的（例如只支持 stdio 的 ACP harness），Adapter 必须自己维护句柄表，
`reconcile()` 只能返回"未知"而不是伪装成全量已知——这时控制平面会把这些任务标成
`unknown`，而不是 `succeeded`。
