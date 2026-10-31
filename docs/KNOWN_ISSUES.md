# Known Issues

本文档记录已知问题、修复状态与专项验证缺口。当前状态：M3 已由 T33（确定性）与
T34（真实 CodeBuddy Worker）验证；M4-A 已由 T32/T36（确定性）与 T38a（真实模型 +
真实 Worker 单轮）验证；M4-B 已由 T39/T40 确定性验证（含真实子进程崩溃注入），
且**真实模型严格闭环复跑通过（T38b 25/25 Pass，配置的本地 OpenAI 兼容端点 +
真实 CodeBuddy Worker：A verified → B handoff 真实 merge → B verified → finish
completed，零人工介入）**；故障恢复与副作用拒绝路径另由 T38c 真实验证。
**"任务执行与交接成功"与"Goal 自动闭环完成"是两个不同的验收结论**，本文档分开
记录。决策模型端点与模型 id 属于环境配置，不出现在任何代码默认值、日志或证据中。

## M4-C 可靠性收口轮（2026-10-31，预算 / inspect 续推 / 并发 / 常驻服务）

| # | 源码位置 | 影响 / 处理 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| M4C-06 | `ld/coordinator.py::run_goal(budget_is_per_call=True)` | 单次额度叠加在累计计数上等效于"每次唤醒都发新预算"：连续多次成功派工可无限突破预算；且单次额度耗尽会被误判为 `decision budget exhausted` 直接停成 `needs_human`（inspect 后无法继续）。现拆分为两层：Quantum（每次唤醒的额度，耗尽即正常让出，报告 `per-call decision budget exhausted`，Goal 保持可推进）与 Lifetime（`ld_goals.max_decisions_total/max_tasks_total`，首次推进盖章，跨重启持久，达到才 `needs_human`，stop reason `lifetime decision budget exhausted` / `goal task budget exhausted`）。 | T45 A1–A6：Quantum 耗尽不错误转人工；多轮唤醒总决策/总任务预算不可突破；重启不重置；`resume` 不隐式增加（仅人工显式 `set_lifetime_budget`）；旧库迁移后生效。 | 已修复；T45 Pass |
| M4C-07 | `ld/goal_runner.py`（wake signature 去重） | `inspect` 决策不改变任何可观测事实：签名去重把"尚未完成的查询流程"当作已处理，LD 查询后永久跳过，无法进入 submit/finish。现引入最小"待继续决策"语义：凡留下 Goal 为 `active` 的推进不确认签名（`record_success(acknowledge=False)`），下一轮 sweep 继续唤醒；检查结果以有界快照（1200 字符）持久化到步 outcome，下一次决策经 `goal_state.last_step.inspection_result` 可用，且仅对授权 `local` 域的模型展示。 | T46 B1–B8：inspect→submit、inspect×2→submit、inspect→request_human、无进展连击上限转人工、查询后重启续推、未授权模型不可见查询结果、Worker 运行期 0 次重复调用、Pending Step 仍优先确定性对账。 | 已修复；T46 Pass |
| M4C-08 | `ld/goal_store.py::stop_goal`（token=None） / `ld/goal_runner.py::_escalate()` | 失败升级不带 fencing：Runner A 模型调用失败期间 Runner B 已完成 Goal 时，A 的升级会把 `completed` 覆盖成 `needs_human`。新增 `stop_goal_if`（单事务条件更新：Goal 仍可推进 + decision_count 与观察一致 + 无其他存活租约才生效）；失败/升级前增加陈旧观察检测（`_observation_is_stale`），陈旧失败不计数、不退避、不升级。 | T47 C2：A 观察后睡眠，B 完成目标，A 升级必须 no-op（`completed` 保持），正向对照（真实卡死 Goal）仍能升级。 | 已修复；T47 Pass |
| M4C-09 | `ld/goal_runner.py::record_skip`（GoalLeaseHeld/Lost 路径） | 失租竞争者的 skip 会写入当前 `last_signature`，把"另一执行者未处理完的失败状态"标记为已处理；下一轮 sweep 判定"观测无变化"而 skip，**退避重试策略被静默禁用**（T47 压测复现：双 Runner 失败场景偶发永不升级）。现失租/陈旧路径 `record_skip(acknowledge=False)`，只有真正处理了该状态的执行者才确认签名。 | T47 C5：双真实进程持续失败，失败计数 1→2→3 且恰一次有效升级；连续三次全量运行稳定。 | 已修复；T47 Pass |
| M4C-10 | 测试约定（非产品缺陷） | stub 测试若从 harness 直接写终态/判定，会与 Runtime 的 `refresh_known_jobs` 读-改-写竞争（终态被回写 running/pending）。产品契约中判定只能来自 adapter 观测 + Verifier；T49 改为 stub adapter 报告终态、由真实 Verifier 路径评估断言产生 rejected/verified。 | T49：A 的 `file_exists never-created.txt` 经 Verifier 真实判 rejected → LD request_human 停摆；B 的工件真实产出后 verified → completed。 | 已按契约修正测试；T49 Pass |
| M4C-11 | T48 首轮真实运行暴露的两个模型侧缺陷（非控制平面缺陷） | (a) Worker agent 对"在当前工作分支提交（不要操作 main/master）"产生合规性误读：检出仅 main 时拒绝执行任何写操作（无 worktree 产生、断言全部"pre-existing unchanged"判 rejected）。修复：T48 目标文本显式允许自建特性分支。(b) LD 模型提交第二任务时连续 4 次产出缺 `target` 的断言，而重试是**盲重试**（同样输入再采样），必然重复失败至升级转人工。修复：新增校验错误反馈重试——Runner 把上一次 `model output rejected` 的校验错误经 `run_goal(retry_feedback=...)` 注入下一次模型输入（`previous_attempt_error` 字段 + 修正规则；仅首次调用生效、传输错误不反馈、输出仍走同一严格校验），模型据此自我纠正。 | T46 B9：无效断言 → 重试输入含校验错误与修正规则 → 任务成功提交、失败计数清零。T48 真实服务 R1/R2 双任务全 Pass。 | 已修复；T46 B9 + T48 Pass |
| M4C-R03 | 剩余风险：T48（真实常驻服务双任务自动接续 + 重启恢复）依赖真实决策模型端点（`LD_MODEL_*`）与本机 CodeBuddy 服务 | 环境不可用时 T48 记录 `EnvironmentBlocked`，**不构成 Pass**；也不得用 stub 结果冒充真实服务验收。运行模式区分：T43 是"测试脚本以 `run_due(force=True)` 驱动 Runtime 调度"，T48 才是"独立服务进程自行调度"，只有 T48 通过才能标记后者已验证。 | `python scripts/t48_real_service_autonomous.py`（需要 `LD_MODEL_BASE_URL/LD_MODEL_NAME`，先 `serve_ctl.py start`）。 | 本轮已用环境配置的 DeepSeek 端点实跑通过（R1+R2 全 Pass） |
| M4C-R04 | 剩余风险：T49 的失败路径为确定性验证（stub Worker + 真实 Verifier 判定路径） | 真实 CodeBuddy Worker 上"任务被拒 → LD 转人工 → 另一项目继续推进"的完整集成未跑（T44 覆盖双项目正常路径，T38c 覆盖真实拒绝路径，但未组合双项目）。 | 环境允许时在 T44 结构上加一个必败断言项目复跑。 | 已知缺口 |
| M4C-R05 | 剩余风险：`ld_goal_runner` 簿记与 Ledger 仍分属不同连接，`record_success/record_skip` 与 WorkItem 终态不同事务 | 与 M4C-R01 同源：签名多写/漏写最坏造成一次冗余幂等决策；步幂等键 + Pending Step 对账保证不重复派工。 | T42/T46/T47 覆盖进程内、重启后与双进程路径。 | 设计如此，已验证 |

## M4-C 轮新发现与处理（2026-10-30，自动唤醒 + 双项目）

| # | 源码位置 | 影响 / 处理 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| M4C-01 | `controlplane/handoff.py::reconcile_handoff_checkpoint()` | 原实现无条件 `store.update(queue_status='ready')`。一旦某进程已释放并 claim（`running`），另一进程的迟到 sweep 会把该行**改回 `ready`**，第二个 Worker 会重复启动同一任务。现改为 CAS `store.promote_deferred_queue(task_id, 'checkpoint_unprotected')`，只有赢得 `checkpoint_unprotected → ready` 转换的进程算释放；重复 sweep 报 `already_ready` 而非 `released`。 | T41 A7：两进程在 ref 不可写时并发 sweep（均 blocked、零 dispatch），解除阻塞后再并发 sweep + claim，断言恰好 1 次 dispatch、attempt ≤ 1。 | 已修复；T41 A4/A7 Pass |
| M4C-02 | `ld/goal_runner.py::GoalRunnerStore.record_failure()` | 原来失败时也写入 `last_signature`，使下一次 sweep 判定为"观测无变化"而 skip，**退避重试策略形同失效**（模型失败一次后永不重试）。现失败只写 `last_observed_at` / `next_attempt_at` / 失败计数，不动 `last_signature`。 | T42 B4：连续 3 次失败须记录 1→2→3 且退避递增，第三次升级为 `needs_human` 并停止调用模型。 | 已修复；T42 B4 Pass |
| M4C-03 | `ld/goal_runner.py::advance_due()` | "观测无变化"去重会吞掉 Pending Step（崩溃遗留的未完成步），导致该步永不 reconcile。现 Pending Step 显式豁免去重（签名本身不含 pending，崩溃不等于没变化）。 | T42 B5：把已提交步改回 `pending` 后 sweep，断言不调用模型、恰好 1 个 WorkItem、adopt 既有任务。 | 已修复；T42 B5 Pass |
| M4C-04 | `ld/coordinator.py::run_goal()` / `LDGoalRunner._advance_one()` | 单次调用预算与 Goal 累计预算语义混用：调度器每轮只给 1 次决策，若直接叠加在累计计数上会立刻"预算耗尽"。新增显式 `budget_is_per_call`（默认 False 保持 M4-B 契约），仅调度器分享用。 | T37 budget 用例（Owner 重复 `run-goal` 仍受绝对预算约束）必须继续 Pass；T42 B1 多次唤醒须能推进。 | 已修复；T37 + T42 同时 Pass |
| M4C-05 | `ld/decision.py`、`ld/coordinator.py::run_once()` | 真实模型偶发输出**结构不完整**决策（如 assertion 缺 `target`），原逻辑等同策略违规，直接把 Goal 永久停在 `needs_human`（T44 首跑真实复现）。现拆分为：`DecisionPolicyError`（未知动作/未知字段/越权工具/`finish` 无 verified 证据 → 永久 fail-closed、不重试）与普通 `DecisionValidationError`（缺字段/类型错 → `next_state=model_output_invalid`、`model_call_failed=True`、按同一步重试）。 | T42 B6：首个样本缺字段须可重试并最终提交 1 个任务；同进程的 `rm -rf /` 仍必须 `invalid_model_decision`、`executed=false`、不被标记可重试。T32/T36 全部旧断言须保持。 | 已修复；T32/T36/T42 全 Pass |
| M4C-R01 | 剩余风险：Goal 调度簿记（`ld_goal_runner`）与 Ledger 仍分属不同 SQLite 连接，`last_signature` 的写入不是与 WorkItem 终态同一事务 | 依赖"签名只由可查询的 Ledger 事实派生"来收敛：即使多写/漏写一次签名，下一轮 observe 会重新算出相同或不同签名，最坏多一次模型调用，不会丢任务也不会重复提交（幂等键绑定 goal+step）。 | T42 B2/B5 覆盖进程内与重启后两种路径 | 设计如此，已验证 |
| M4C-R02 | 剩余风险：自动推进仍受单个 Goal 预算与失败阈值约束，长目标可能多次触发 `needs_human` | 属有界自主的**预期行为**，不是缺陷；`--resume` 或新的 Human Owner 决策显式解锁。调度器永不自行放宽预算。 | — | 已知行为 |

## M4 可靠性收口（评审 ee539ee 的处理，2026-10-30）

| # | 问题 | 处理 | 验证 | 状态 |
|---|---|---|---|---|
| P0-1 | `resume()` 写入新步但不递增 `decision_count`，下一模型步 `UNIQUE(goal_id, step)` 冲突 | resume 的决策行与计数递增在同一事务提交；human_resume 是同一连续序列中的一步；模型预算不计人工恢复（`model_decision_count`） | T39 R1：needs_human → resume → finish，步号 1..5 连续、仅一个 human_resume、其后紧跟 finish | 已修复 |
| P0-2 | 执行动作—记录决策—更新状态非原子，崩溃可致重复派工或重复步号 | 两阶段步：副作用前持久化 pending（含已校验决策）；结果与状态迁移单事务提交；派工键由系统按 (goal, step) 生成；重启先对账 pending 步（adopt 已有 WorkItem 或重放已存决策），不再调用模型 | T39 R2：真实子进程在 4 个边界 `os._exit` 后重启，均恰好 1 个 WorkItem、键为步键、恢复方式符合预期（replayed/adopted）、模型未被询问 | 已修复 |
| P0-3 | 租约 120s < 模型超时 180s，失租持有者仍可写 | 递增 `lease_token` fencing，所有写入事务内校验；心跳续租；副作用前同步 fenced 续租；owner 改为 pid+uuid | T39 R3：双进程竞争恰一者推进；失租持有者在模型调用后被 `GoalLeaseLost` 拦截、零写入；心跳使入侵者在长调用期间被拒 | 已修复 |
| P1-1 | `capture_source_checkpoint()` 执行 `git add -A && git commit`；被拒请求也可能已产生 commit | 删除自动提交。Verifier 首次 verified 时固化 `acceptance_snapshot`；checkpoint 仅从与该快照一致的状态在对象库组装（临时 index + commit-tree），源 worktree/分支/main 不变；状态变化、敏感文件、未验收、无快照的脏状态、HEAD 漂移均阻塞；policy 先校验；保护 ref 仅在 WorkItem 成功持久化后创建 | T40 C1–C3：7 种不可证明状态全部阻塞且零副作用；被拒请求零 Git 对象；幂等冲突无第二 ref | 已修复 |
| P1-2 | CLI 在配置端点后默认授予 `local,shareable` | 真实端点默认不授予任何域，需 `--model-domains` 或 `LD_MODEL_DOMAINS` 显式授权；`--decision-json` 本地静态决策不变 | T40 C4：默认不调用端点（`security_domain_unauthorized:local`），显式授权（参数/环境变量）才调用 | 已修复 |
| P1-3 | `_goal_report()` 按"存在 ld_goal_ref"过滤，混入其他 Goal 任务；T38 证据中的 `goal_status` 取自中间轮次 | 报告按精确 `goal_ref` 过滤，新增 `goal_tasks`/`history_contiguous`/`pending_steps`；T38 改为从**最终**报告断言 `completed`、恰含 A/B、历史连续、resume 次数与 Owner "继续"次数一致 | T39 R1（报告排除同项目另一 Goal 的 verified 任务）；T38b 严格复跑 | 已修复；真实复跑结论见 T38-F04（阻塞） |
| R-01 | 剩余风险：Ledger 与 Goal 表分属两个 SQLite 连接，WorkItem 创建与 pending 步提交不在同一事务 | 由"pending 先于副作用 + 系统步键 + 重启对账"保证收敛，而非单事务；T39 R2 覆盖了该窗口 | T39 R2 `after_execute` / `commit_mid` | 设计如此，已验证 |
| R-02 | 剩余风险：checkpoint 只校验验收后的状态一致性，不审查验收时的内容本身 | 验收时已在 worktree 中的文件即被视为"已验收状态"的一部分（敏感文件名除外）；内容审查仍依赖 Verifier 断言与人工评审 | — | 已知限制 |

## M4-A 评审处理与剩余问题

| # | 源码位置 | 影响 / 处理 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| M4A-R01 | `ld/coordinator.py::_resolve_decision_project()` / `_inspect()` | Human Owner 选定项目后，模型省略 `project_id` 的任务查询会固化到该项目；读取完整 provenance 前先查状态归属，避免跨项目 Effective Prompt 泄露。 | 在 Project A 作用域请求 Project B 的 provenance；确认返回转人工且 provenance 工具调用次数为 0。 | 已修复；T32 Pass |
| M4A-R02 | `ld/decision.py::_validate_finish()` / `ld/coordinator.py::_finish()` | 项目历史 `verified >= 1` 不再能单独完成新目标；至少一个 verified WorkItem 必须带当前目标稳定引用。 | 用纯项目计数、旧目标 verified task、无 goal 引用历史 task 分别 finish；均须拒绝；同目标 verified task 才允许。 | 已修复；T32 Pass |
| M4A-R03 | `ld/tools.py::submit_project_task()` | LD 提交签名绑定 task、实际目标 Workspace、Assertions、标签/前置条件及执行参数；同 key 异请求冲突，Context Version 不入签名。 | 首次提交后更新 Context 并原样重试应 deduplicate；改 Workspace 或 Assertions 应冲突。 | 已修复；T32 Pass |
| M4A-01 | `ld/coordinator.py::run_once()` | 项目存在异常任务时，只读动作现已允许；但任何新提交仍会被项目内任一 attention flag 阻塞，即使它可能与新任务独立。这是安全但偏保守的写门控。 | 在项目中放一个 `needs_approval` 任务：inspect 应成功，独立 submit 仍应 `request_human`；再评估是否需要显式依赖关系后细化。 | 已知保守策略；T32 覆盖当前行为 |
| M4B-01 | `ProjectService.get_task_provenance()` / 未来模型工具回馈层 | provenance 包含完整 Effective Prompt。M4-B 多轮前必须分离本地操作者视图与按安全域过滤的模型视图。 | 接入任何多轮模型前，用 `local` 项目任务调用 provenance，断言模型可见响应不含 Prompt、路径或未授权内容。 | 已解决（T36）：`LDTools.get_task_provenance(allowed_security_domains=...)` 模型视图移除 Effective Prompt 与任务原文，context entry 标题仅暴露授权域；coordinator inspect 自动应用 |
| M4A-V01 | `scripts/t32_ld_m4a.py` | T32 已覆盖七工具、五种动作、四类异常状态、作用域、模型输入安全域、CLI、顺序/并发幂等、finish、dry-run、命令拒绝和无模型路径；M1/M2 关键回归（T26–T31）已于 2026-10-30 在本地复跑全部 Pass（T27/T28/T30 证据已按规则恢复为历史版本，不覆盖）。真实模型或 Worker 集成结论仍缺失。 | 真实 Worker 环境可用后再单独做集成验收，不复用 T32/T33 的 stub 结论。 | 回归已跑；真实集成待验证 |

## T34 轮新发现（2026-10-30，真实 CodeBuddy Worker）

| # | 源码位置 | 影响 / 处理 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| T34-F01 | `controlplane/plane.py::enqueue()` / `_dispatch_record()` | 裸 `enqueue(workspace_id=...)` 不传 `digest_scope` 时，审批绑定的 digest 与 dispatch 时 `_digest_scope` 重算必然不一致：任务审批通过后永远派发不出去，且 `task_digest` 被改写并记录误导性的 `dispatch_content_changed` 事件。M1 引入的回归。 | `enqueue(..., policy=poc_shell(), project_id=..., workspace_id=...)` → `approve(digest)` → `dispatch_next()`：修复前返回 needs_approval 且 digest 变化；修复后正常派发。 | 已修复；T26–T33 回归全 Pass，T34 M3-01 Pass |
| T34-F02 | `controlplane/project_service.py::submit_project_task()`；`adapters/codebuddy/adapter.py::dispatch()` | shell 任务执行的是冻结 prompt 本身。`submit_project_task` 注入多行项目上下文后，bash job 把 prompt 首行（`#` 开头的标题行）当命令执行而失败，任务静默变 `rejected`。提交层没有拒绝该组合。 | `submit_project_task(..., policy=poc_shell())` → exec log 首行报 `'#' 不是内部或外部命令`，verdict=rejected；改用裸 `plane.enqueue`（无 context 注入）则正常。 | 已修复（T35）：`submit_project_task` / `submit_handoff` 显式拒绝 shell policy；裸 `plane.enqueue` 的 shell 路径保持可用 |
| T34-F03 | `scripts/serve_ctl.py::cmd_stop()` | 原 `taskkill /PID <pid> /T /F` 会连带杀掉 serve 进程 fork 的 npm 自动更新子进程，导致全局 npm 安装半损坏（bin shim 丢失、CLI 变 unknown，需 `npm install -g @tencent-ai/codebuddy-code@<版本>` 修复）。 | `serve_ctl.py stop` 后 `codebuddy --version` 报命令不存在。 | 已修复（stop 只杀监听 PID，不带 /T） |

## T38 轮新发现（2026-10-30，真实模型 + 真实 Worker 端到端）

| # | 现象 | 影响 / 处理 | 状态 |
|---|---|---|---|
| T38-F01 | 真实决策模型（OpenAI 兼容端点，按环境配置）在目标推进的收尾阶段倾向保守转人工：三次真实运行中两次在应 `finish` 或继续提交时选择 `request_human`（决策本身合法）。 | 目标停在 needs_human；需要 `run-goal --resume` 显式解锁（该动作本身被记录为 `human_resume` 决策）。这不是缺陷而是安全设计内的行为：人工只说"继续"，无需传递任何项目状态或任务内容。`finish` 的确定性校验由 T32/T37 覆盖。 | 已知行为；`--resume` 机制已提供 |
| T38-F02 | 首次真实运行中模型为任务选择了 `git_clean` 断言，而 agent 完成后未 commit，验收正确判 rejected（Verifier 行为正确）。 | 属于目标文本引导问题而非系统缺陷：在目标中明确要求"完成后 git commit"且建议使用 `file_exists`/`file_contains` 断言后，后续运行任务全部 verified。未提交的已验收改动现由 `acceptance_snapshot` + 对象库 checkpoint 接续（不再自动 `git add -A`，见 P1-1）。 | 已通过目标文本引导缓解；Verifier 语义未放宽 |
| T38-F03 | ee539ee 版 T38 证据的 `goal_status` 取自中间轮次（`report2.status`），并未断言最终 Goal 状态；最终报告的 `verified_tasks` 只含 A。 | 该版 16/16 Pass 只证明执行与交接，**不证明** Goal 自动闭环（评审 P1-3 指出）。T38 脚本已改为对最终报告做严格断言，严格复跑证据另存为 `T38b-m4-real-e2e-strict.json`（首轮证据保留原文件）。 | 已更正；首轮证据仍有效于其断言范围，严格复跑结论见 T38-F04 |
| T38-F04 | 严格复跑（T38b，对最终 Goal 状态断言）首次真实执行后 **Fail**：真实模型把任务 A 的 id 写进 `prerequisites`（该字段语义是 Project Context 条目标题/标签），handoff 准备被 "prerequisite ... has no verified project context entry" 正确阻塞，LD fail-closed 转人工 ×3。LD 行为正确（不绕过闸门）；缺陷在字段语义衔接。 | 根因已修复：目标派工时任务 id 前置条件按 Ledger 判定（该 WorkItem verified 且属于本 Goal 才满足，跨 Goal/未验收则拒绝），非任务 id 原样传给 Context 条目校验；决策 schema 补充 `prerequisites` 语义说明；失败原因持久化到步 outcome 并作为 `last_step` 展示给下一次决策。确定性回归 T39 R4 全 Pass。**修复后真实复跑通过**：T38b（配置的本地 OpenAI 兼容决策端点 + 真实 CodeBuddy Worker）25/25 Pass——A verified → B 以 handoff 提交并真实 merge A 的 checkpoint（HEAD 祖先证明、B worktree 内全部测试通过）→ finish completed，Owner 介入次数 0。 | 已修复；真实复跑通过（T38b Pass） |
| T38-F05 | 真实环境运行要求（T38b/T38c 轮实证）：CodeBuddy agent job 默认 `permissionMode=acceptEdits` 下非交互模式 Bash/PowerShell 一律被拒；handoff 会话必须执行 `git merge`/pytest/`git commit`，纯文件编辑不足以完成交接（T38c 中任务 B 因此卡死后被人工取消）。`dontAsk` 全部拒绝；`bypassPermissions` 被控制平面 `DispatchPolicy.validate` 拒绝（两次真实提交尝试均被拦下）。 | 采纳 CodeBuddy 自身建议的机制：**项目级 settings `permissions.allow: ["Bash","PowerShell"]`**（`.claude/settings.json` 与 `.codebuddy/settings.json`，随 fixture 仓库提交、被每个 worktree 继承，授权范围仅限一次性沙箱仓库）。T38 脚本 `make_repo` 已内置该文件；实证探针验证 agent 真实执行 Bash 且断言核对真实 commit hash。T38c 另记录：B' agent 曾把 git 命令误打入项目根（留下 stray 分支与 marker），Verifier 副作用检测在断言全绿、测试全过的情况下**正确判 rejected**，任务预算耗尽后 Goal 正确 fail-closed 到 needs_human。 | 已固化；环境要求已写入测试 fixture |

## M3 待验证清单

| # | 源码位置 | 影响 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| M3-01 | `adapters/codebuddy/adapter.py::_snapshot()` / `stream_events()`；`controlplane/plane.py::_save_snapshot()` | 真实 CodeBuddy 在启动、刷新、终态、重启后对账等路径是否稳定返回 `session_id` 尚不明确；缺失时 SessionReference 不会登记。 | 启动真实 Worker，覆盖 dispatch、refresh、terminal、Worker restart 四条路径，逐次对比 Snapshot 与 `session_references`。 | 已验证；T34 Pass（含运行中重启服务后 in-flight 对账）。注意：真实 job 从不携带 `head_commit`（adapter 不映射 commit 字段），checkpoint 只能走 `context_only` 路径 |
| M3-02 | `controlplane/handoff.py::HandoffPlanner.prepare()` / `_render_prompt()` | Session B 是否可在不读 Session A transcript、不 resume 旧会话的前提下，仅依据 Project Context + checkpoint 继续并通过验收未验证。 | Session A 产生中间 checkpoint 后关闭；创建全新 Session B，禁止 transcript/resume，执行 Handoff WorkItem 并检查 `verdict=verified`。 | 已验证；T34 Pass（真实 agent：Session A verified → context_only handoff → Session B 全新 session、prompt 含 handoff 段且不 resume、verdict verified） |
| M3-03 | `controlplane/plane.py::_snapshot_patch()`；`controlplane/handoff.py::_checkpoint()` | Worker worktree 关闭或清理后，记录的 `head_commit` 是否仍在目标 Workspace 可达不确定，可能使合法交接变成阻塞。 | 让 Session A commit 后关闭/清理 worktree，再调用 `handoff-prepare`，检查 commit object 、可达性和 blocker。 | 已验证；T33 Pass |
| M3-04 | `controlplane/store.py::TaskStore.__init__()` / `_migrate_columns()` / `_migrate_legacy_json()` / SessionReference 索引与触发器 | 历史 Ledger 的 schema 迁移、重复 SessionReference 清理、部分唯一索引/触发器创建尚未在旧库样本上回归；可能导致启动失败或遗留歧义绑定。 | 备份一份 M3 前 Ledger，插入重复/缺字段历史行，用新版 `TaskStore` 打开，检查迁移、索引、触发器和数据投影。 | 已验证；T33 Pass |
| M3-05 | `controlplane/store.py::observe_session_reference()` / `_insert_session_reference()` | 多进程/多线程同时绑定同一 Attempt 时，首个绑定是否稳定获胜、其余是否全部显式冲突未验证。 | 用两个独立 SQLite 连接对同一 Attempt 并发提交不同 Session，检查唯一绑定和冲突事件。 | 已验证；T33 Pass |
| M3-06 | `controlplane/plane.py::_save_snapshot()` | `_save_snapshot()` 先更新 WorkItem，再登记 SessionReference，不是单一事务。绑定冲突时 WorkItem `session_id` 可能与首个不可变 SessionReference 不一致。 | 先为 Attempt 登记 Session A，再保存指向 Session B 的 Snapshot；检查 WorkItem、SessionReference、`session_binding_conflict` 事件三者是否一致。 | T33 Pass（已知原子性窗口，冲突被显式暴露：事件 + needs_human，首个绑定不可变） |
| M3-07 | `controlplane/plane.py::_save_snapshot()`；`controlplane/store.py::append_event()` | Session 冲突事件使用当前 WorkItem Attempt ID；如果是迟到的旧 Attempt Snapshot，事件可能关联到错误 Attempt。 | 创建 Attempt A/B，在 B 成为 current 后送达 A 的冲突 Snapshot，检查 event `attempt_id`。 | T33 Pass（已知关联窗口确认：事件挂 current Attempt，旧 Attempt 绑定不变） |
| M3-08 | `controlplane/handoff.py::HandoffPlanner.prepare()`；`controlplane/project_service.py::submit_handoff()` | 交接的 Git/context 校验与 enqueue/真实 dispatch 之间仍有 TOCTOU 窗口；入队后 Workspace HEAD 可再次漂移。 | prepare 通过后修改目标 HEAD，分别在 submit 前和 submit 后/dispatch 前复现，检查哪个窗口被签名拦截。 | T33 Pass（submit 重检拦截 `HandoffNotReady`/`HandoffStale`；入队后漂移不改冻结内容，dispatch 前残留窗口已知） |
| M3-09 | `controlplane/handoff.py::_checkpoint()` | 当 source worktree 存在时严格要求 `git status --porcelain` 为空；真实 Worker 生成未跟踪运行产物时，可能阻塞本可安全交接的 checkpoint。 | 在 source worktree 留下与交付无关的 untracked 文件，运行 `handoff-prepare`，评估 blocker 是否过于保守。 | T33 Pass（保守防护确认：untracked 与 modified 均阻塞） |
| M3-10 | `controlplane/handoff.py::handoff_signature()`；`controlplane/project_service.py::submit_handoff()` | 同一 idempotency key 在 Context/target HEAD 漂移后会显式签名冲突；语义正确，但实际恢复/重试的操作体验未验证。 | prepare 后更新 Context 或 target HEAD，用同 key 重提，确认返回 `HandoffStale`/`HandoffIdempotencyConflict` 而不是新任务。 | 已验证；T33 Pass |
| M3-11 | `scripts/controlplane_cli.py::_run_project_command()` | Project 子命令已改为按 `default_adapter` 创建只有名称的 Service Plane，但尚未回归；如回归失败，项目任务可被错误标记为 `ledger-only` 而不被 Runtime claim。 | 用非 `ledger-only` 的 `default_adapter` 注册项目，CLI submit/handoff-submit 后检查 WorkItem `adapter`，再运行对应 Runtime claim。 | 已验证；T33 Pass（adapter=stub、Runtime claim 可取、ledger-only 不能 claim） |
| M3-12 | `controlplane/store.py::session_references()` | 旧 Ledger 中 `legacy_placeholder` 与项目/工作区/任务删除后的悬挂状态投影尚未用历史数据验证，可能误报“可交接”或隐藏旧脏数据。 | 构造 NULL 历史占位、缺失 Workspace/WorkItem 的 reference，查询 Session Registry 投影并检查显式状态。 | 已验证；T33 Pass |

## 处理原则

- 任何"已修复/Pass"结论都必须指向可重现测试与脱敏证据；stub/确定性 Pass 不等于
  真实集成 Pass。
- "两个任务都 verified、代码真实接续"与"Goal 自动进入 completed 并生成只含本
  Goal 任务的最终报告"分别断言，不得用前者代替后者。
- Handoff 不作为模型可选动作开放；依赖任务交接由控制平面确定性执行。
