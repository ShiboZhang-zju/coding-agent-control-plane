# Known Issues

本文档记录已知问题、修复状态与专项验证缺口。当前状态：M3 已由 T33（确定性）与
T34（真实 CodeBuddy Worker）验证；M4-A 已由 T32/T36（确定性）与 T38a（真实模型 +
真实 Worker 单轮）验证；M4-B 已由 T39/T40 确定性验证（含真实子进程崩溃注入），
且**真实模型严格闭环复跑通过（T38b 25/25 Pass，配置的本地 OpenAI 兼容端点 +
真实 CodeBuddy Worker：A verified → B handoff 真实 merge → B verified → finish
completed，零人工介入）**；故障恢复与副作用拒绝路径另由 T38c 真实验证。
**"任务执行与交接成功"与"Goal 自动闭环完成"是两个不同的验收结论**，本文档分开
记录。决策模型端点与模型 id 属于环境配置，不出现在任何代码默认值、日志或证据中。

## UI-1 Web LD Interaction（2026-11-03）

| # | 已知限制 / 风险 | 影响 / 当前处理 | 状态 |
|---|---|---|---|
| UI1-R01 | Ledger 没有 Runtime/Runner 心跳；`ld_goal_runner` 行只是调度簿记，不能证明进程在线 | 页面把 Runner liveness 固定显示为 `unknown`。Goal 无决策记录时只能说“等待 Runner”；绝不显示 online。 | 设计限制 |
| UI1-R02 | Dashboard 与 Runtime 是独立进程，二者看到的 `LD_MODEL_*` 环境可能不同 | 页面只报告 Dashboard 进程的 Chat Intent 配置；Runtime Decision Model 状态保持 `unknown`。自动推进以 Runner/Ledger 的真实结果为准。 | 设计限制 |
| UI1-R03 | UI-1 没有账号体系、TLS 或远程访问设计 | 只允许 loopback；显式 `--enable-chat`；Bearer Token + 精确 Origin/Host + 无 CORS。禁止端口转发暴露。 | 安全边界 |
| UI1-R04 | 只支持 M4-D 已有的 `submit_goal` / `resume_goal` 写意图 | 审批、Task cancel、Context/Memory 编辑、预算调整仍走 CLI，避免 Web 层扩成第二条执行路径。 | 范围限制 |
| UI1-R05 | ~~当前环境未配置 `LD_MODEL_BASE_URL/LD_MODEL_NAME` 时无法验收真实 `LD → CodeBuddy` 自动闭环~~ 已在本机配置的 DeepSeek 端点上真实跑通 | 真实闭环已由 T66 覆盖（34/34 Pass，零人工介入）。无端点时 T66 仍记录 `EnvironmentBlocked`，不是 Pass。 | 已验证（T66） |
| UI1-R06 | **验收规则缺陷（T66 真实 E2E 发现，已修复）**：`controlplane/verify.py::verdict_for()` 原先只要求全部断言 `passed`，不要求有断言 `owned`。若 LD 只声明否定型断言（`path_absent` / `git_clean`），一个**什么都没做**的任务会全部"通过"并被判 `verified`，Goal 进而 `completed` | 影响面：只读型目标（"分析并汇报，不要改文件"）会被整条链路误报为已完成，Dashboard 同步显示完成。修复：`VERIFIED` 额外要求至少一条断言 `owned=True`（正向证据），否则降级 `inconclusive`——与"没有断言"同等对待。确定性回归：`tests/test_verifier_negative_assertions.py`（13 项，含 `passed=true/owned=false` 非空结果集、`git_branch` 单独、状态断言混入，以及一次经 `ControlPlane` 全链路的集成用例）。**注意 T66 的 S3 并未触达该规则**（其 `assertion_results` 为空，走的是原有"无断言"分支），真正证明新规则有效的是这些测试 + T66b 的变异验证。 | 已修复（T66b 巩固） |
| UI1-R09 | **验收范围依赖 LD 声明的断言**：`owned=True` 只证明"被声明的产物相对基线发生了可归因变化"，不证明自然语言目标整体完成。T66 的 S1 只声明了两条文件断言，因此"文件含 `def load_config`"曾被当成"`test_load.py` 通过" | 示例风险：函数体是 `raise NotImplementedError` 也能通过两条文件断言。处理：T66 脚本现独立执行预置 `test_load.py` 并记录退出码（`S1.seeded_tests_actually_pass`）；T66b 对真实运行留下的 worktree 补跑了该测试（3/3 `2 passed`）。**这仍是"补一条断言"而非架构保证**：谁的断言不充分，谁的验收就不充分。后续应在 LD 提交阶段评估断言充分性，而不是新增验收状态机。 | 部分缓解（暴露为已知缺口） |
| UI1-R07 | 真实目标文本的**范围歧义**会让模型在首个任务 verified 后再次提交同类冗余任务（run2 实证：第二个任务因在项目根产生未声明本地改动被判 `rejected`，Goal 最终 `needs_human`） | 这不是控制平面缺陷：Verifier 正确拒绝了副作用。处理：T66 目标文本显式声明"只需一个任务、verified 后立即结束"（同 M4C-11 的教训）。真实模型仍可能自行改选任务粒度，故 T66 只对"链路成立"下断言，不对模型的 git 风格下断言。 | 已缓解（fixture 质量） |
| UI1-R08 | 真实 CodeBuddy session 偶发卡在 `starting…`（run2 实证：一个任务挂起逾 20 分钟，worker 侧无 job 记录） | worker 侧已知缺陷，控制平面无法自愈。T66 观察循环新增**停滞检测**（无可观测变化超过 1500s 即停止并记录 `stalled=true`），使运行诚实失败而非挂到预算耗尽。 | 已缓解（检测，非修复） |

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

## M4-E 关联召回的剩余限制（2026-11-01）

| # | 位置 | 影响 / 当前处理 | 复现 / 验证 | 状态 |
|---|---|---|---|---|
| M4E-R01 | `project_context_entries.evidence_refs` / `ld/recall.py` | Context 条目的 `evidence_refs` 历史上是任意路径/URI，不是 Ledger `evidence.id`。T55 后 Level 1/模型视图只返回定长指纹；本地 operator Level 2 可在全局预算内审计原值，仍不猜测其与 Ledger Evidence 的对应。 | 新建含超长本地路径的 Context 条目，比较 shareable 与 operator 视图。 | 设计如此；T55 Pass |
| M4E-R02 | `ld/recall.py` / `scripts/t54_relational_recall.py` / `scripts/t55_recall_resource_closure.py` | T54/T55 只证明确定性 ID 展开、硬预算、真分页、项目/安全域隔离与重启恢复；没有证明真实 LLM 会选对展开路径，也没有评测语义相关性。 | 配置真实模型，给定需要两次以上展开才可回答的历史问题，断言模型仅根据授权召回结果回答，且不泄露 `local`。 | BLOCKED：本轮未运行真实模型/Worker |
| M4E-R03 | `ld/recall.py::_project_tasks()` | Goal → WorkItem 仍先按项目读取再过滤；T55 后扫描最多 10,000 行，超限显式拒绝，不再做无界读放大。大型 Ledger 上的性能仍未评测。 | 构造大型 Ledger 测查询时延/内存；若真实规模需要，优先增加可派生的 goal-ref 索引投影，不新建第二份任务真相。 | P2 性能边界；已有 fail-closed 扫描上限 |

## M4-F Markdown Project Memory 的剩余限制（2026-11-01）

本轮 T60 已收口三个日常使用风险：普通重导出不再覆盖 divergent Entry
（只有显式 `--force` 才丢弃）；默认路径绑定 `<ledger-dir>/projects`；
Knowledge 章节不再将 candidate/unverified 条目误称为 verified。
证据：`evidence/project/T60-memory-export-safety.json`（16/16 Pass）。
后续 T61 又收口 apply 后的单文件版本同步：只在该 Entry 未被再次修改时刷新
`version`；若已变化则保留文件并明确回执，不会强制重导出其他 Entry。
证据：`evidence/project/T61-memory-apply-refresh.json`（10/10 Pass）。

| # | 位置 | 影响 / 当前处理 | 复现 / 验证 | 状态 |
|---|---|---|---|---|
| M4F-R01 | `controlplane/project_memory_md.py` | MVP 一次只预览/应用一条 Entry，不提供整项目多文件原子事务或后台 watch；这是避免把 Markdown 变成第二份状态。 | 同时编辑两条文件，分别 preview/apply；第二条若遇到 Context version 漂移，应重新导出/预览。 | 设计边界；T58 单条 CAS Pass |
| M4F-R02 | `ProjectMemoryMarkdown.preview/apply()` | verified Entry 的实质修改被拒绝；当前没有“修改后降级、重新验证、重新晋升”的完整工作流。 | 编辑 verified Entry body 后 preview，检查 `can_apply=false`；若确需修改，仍需未来独立审核设计。 | 未实现；T58 已证明 fail-closed |
| M4F-R03 | `scripts/t59_memory_injection_acceptance.py` | T59 证明 ContextBuilder 的两个/多个独立 WorkItem Prompt 正确冻结与更新，但没有启动真实 CodeBuddy、真实模型或两个真实 Session。 | 在真实本地项目执行“LD 对话 → Worker → M2 candidate → Human Markdown edit/apply → 新 Session”，核对 provenance 和 verifier。 | BLOCKED：本轮按要求未运行真实模型/Worker |
| M4F-R04 | 严格 Front Matter 子集 | 为避免危险反序列化和新增依赖，只接受导出器产生的根级标量/字符串列表子集，不是通用 YAML 编辑器。 | 使用 anchor、tag、block scalar、重复/未知字段；应明确拒绝。 | 设计边界；T58 Pass |
| M4F-R05 | M2 自动候选治理 | 自动条目现已明确是 task-specific execution evidence，但尚无质量评分、自动去重、合并、失效或删除。历史 Ledger/Evidence 均保留。 | 在真实项目观察多任务 candidate 质量与膨胀，再决定最小治理，不先引入 Memory Agent。 | 待真实项目试用 |
| M4F-R06 | `controlplane/project_memory_md.py::export()` | 导出器以 LF 落盘，`Path.write_text`（Windows 默认换行转换）或编辑器会把同一 Entry 改写为 CRLF；原实现按**原始字节**比较，于是把纯换行差异判为"本地编辑"，一次 apply 之后普通 `memory-export` 永久拒绝导出（T61 在 e681521 上实际回归失败）。现改用换行无关的内容比较（`_same_entry_content`）。 | `python scripts/t61_memory_apply_refresh.py`：apply 后普通导出必须成功（`ordinary_export_after_apply_succeeds`）。 | 已修复；T61 10/10 Pass |
| M4F-R07 | `scripts/t58_markdown_memory.py` / `scripts/t60_memory_export_safety.py` | `file_snapshot()` 用 `str(path.relative_to(dir))` 作键，Windows 产出 `entries\\x.md` 而断言查 `entries/x.md`，两个套件在 Windows 上恒定 `KeyError` 失败——不是产品缺陷，但会掩盖真实回归。现改为 `as_posix()`。 | 在 Windows 上直接运行 T58/T60，均应 Pass。 | 已修复；T58 26/26、T60 16/16 Pass |

## T62 Real Project Memory Pilot（2026-11-02）

T62 分两阶段：Stage 1 只读预演（不动真实仓库、不起 Worker），Stage 2 真实项目
两任务记忆闭环（需真实仓库 + 真实 Worker）。Stage 1 已完成：

- 脚本 `scripts/t62_real_project_pilot.py`；证据
  `evidence/project/T62-real-project-pilot-stage1.json`（17 项检查，真实模型）
  与首次失败 `evidence/project/T62a-stage1-initial-fail.json`（真实复跑，非手写）。
- 部署形态：真实 intent 模型（`LD_MODEL_*`）驱动的 `LDChat` + 真实 `LDGoalStore`；
  Worker 只有一个名字引用（`AdapterRef('stub')`），**全程未派发任何 WorkItem**。
- 已验证：只读三问零副作用（WorkItem/Goal/context_version 增量均为 0）；verified
  background / verified constraint 完整进入 Effective Prompt 且约束在强制区块；
  candidate 标 `[CANDIDATE]` 且不满足 verified 前置；`entry_refs` 可追溯到三个
  条目 ID；对话提交 Goal 只产生 1 个 Goal 且 `current_task_id` 为空；Goal 状态由
  LD Chat 从 Ledger 读回；人工 Markdown 编辑经 `memory-preview` + CAS
  `memory-apply` 后只影响**新** Prompt，旧 Prompt 冻结；未写入任何 verdict。
- 首次失败的两项均为**验收脚本自身缺陷**（`turn['intent']` 实为 dict、
  `BuiltContext.entry_refs` 是字符串列表），未触及 `controlplane/` 与 `ld/`；
  修复前先真实复跑留证（`T62a`），再以修复后的脚本复跑（`T62-...stage1`）。
- 未证明（不能算 Pass）：真实 Worker 执行与 Verifier 判定、Session B 继承 A 的
  已验收工作、记忆注入对真实任务是否有可量化收益。这三项属 Stage 2，
  `M4F-R03` 仍为 BLOCKED。

### Stage 2 结果（同一日完成）

脚本 `scripts/t62_stage2_real_project.py`。**两个文件，不互相覆盖**：

- `evidence/project/T62-stage2-real-project.json`：运行时（Worker 服务在线）写下的
  原始验收记录，27/27 Pass，`transcripts_readable: true`，A/B 的 tool_calls 为 7/8。
  这是历史证据，**不再被重跑改写**。
- `evidence/project/T62-stage2-real-project-recheck.json`：G1 复核记录，34/34 自动
  检查通过，但服务离线导致 A/B transcript 不可读，整体为 **INCOMPLETE**；它以
  `recheck_of` + `historical_record` 引用上一份，而不是替换它。

支撑记录 `evidence/project/T62-stage2-pilot-transcripts.json` 同样保留历史真值：
运行时真实读到的 A/B_rejected/B 三条观测（tool_calls 7/7/8）逐字段继承并标注
`transcript_source`，不会被一次离线重跑改成 null。
逐组归档：`evidence/project/t62/pair1-archived.json`、`pair2.json`、`pair3.json`。

真实项目：`ShiboZhang-zju/gui-agent-post-training` 的本地隔离克隆，分支
`pilot/t62-memory`。`main`（4a1cdc02）未被改动；"未 push" 是操作员人工声明
（`human_declarations_not_automated`），不属于自动验证项。

| 环节 | 结果 |
|---|---|
| A：真实 Session 修 `evaluation/parsing.py` 三类边界缺陷 | `verified`，提交 5a215fc |
| M2 候选回写 | 2 条 `candidate`（确定性） |
| 人工改 Markdown + `memory-preview`/`memory-apply`（CAS v2→v3） | 成功 |
| `context-verify` + 2 条 evidence refs 晋升 | v4 `verified` |
| B：全新 Session 用该知识做 `metrics.py` 后续任务 | `verified`，66eb601，Prompt 内条目 ID 与版本一致 |
| 受控对照 C1/C0（同提交 f882885、同指令、同断言，仅 context_version 1 vs 0） | **INCONCLUSIVE**：3 组中 2 组双 verified、1 组有记忆被拒无记忆通过 |

### G1 证据治理（2026-11-02）

修复 T62 Stage 2 的证据一致性、历史归档与验收脚本可靠性，不新增架构模块：

| 改动 | 位置 | 说明 |
|---|---|---|
| 统一归档模型 | `scripts/t62_evidence_model.py`（新） | 每组实验一条稳定归档，带 `evidence_level`；`complete` 要求恰好两个 Arm、一记忆一控制、task_id 互不相同、verdict 为终态。空集/缺组/重复 task_id → `BLOCKED`；verdict 在但 SessionReference 丢 → `INCOMPLETE`。 |
| Pair1 独立归档 | `evidence/project/t62/pair1-archived.json` | 从事故前同期记录（旧 `T62-stage2-memory-contrast.json` 的 `pair_detail`）一次性 seed，`evidence_level=historical_record_only`、`ledger_recoverable=false`、`reexecuted=false`。**不会因重跑被升级，也不会被重写。** |
| Pair2/Pair3 独立归档 | `evidence/project/t62/pair2.json` / `pair3.json` | 读 `.poc-state/t62/pairs/<pair>/summary.json` + 存活 Ledger 的只读 SessionReference 查找，`evidence_level=archived_verdict`。pair2 的 Session 已不保留，故归档附注明确标注 transcript 不可得。 |
| 汇总不再读旧验收文件 | `scripts/t62_stage2_contrast_record.py` | 删除 `earlier_pair()` 从 `T62-stage2-real-project.json` 取 C1/C0 的逻辑（该文件已无这两个字段，旧代码实际返回 `None` 却仍 PASS）。三组按 `PAIR_IDS` 固定顺序汇总，`archives_seeded_this_run` 记录本次 seed 了哪些。 |
| Supporting Record 重写 | `scripts/t62_stage2_supporting_record.py` | 不再把 pair1 当实时任务查询（旧代码得到 queued/pending 仍输出 PASS + "Both arms reached the same verdict"）。改为消费归档；transcript 不可读时记 `unavailable` + 原因，`tool_calls` 为 `null`，**绝不推断**。结果只能是 `COMPLETE`/`INCOMPLETE`/`BLOCKED`。 |
| 三层结论 | `T62-stage2-memory-contrast.json::three_layer_reading` | ① 记忆链路成立 ② 记忆收益未证明 ③ Verifier 可能测量偏差（pair2 54 tests passed，仅 `file_contains: normalized_1000` 失败）。`direction_of_disagreement` 显式记录 `memory_passed_control_failed=[]`、`control_passed_memory_failed=["pair2"]`，并撤回旧 "failed in opposite directions"。 |
| 验收脚本 fail-closed | `scripts/t62_stage2_real_project.py` | 新增检查：任务齐全、task_id 唯一、Session/Attempt/Verdict 三者同链、终态状态、断言结果集非空（`all([])==True` 修正）、对照归档非 BLOCKED 且恰 3 组 6 个 task_id。缺 transcript 时结果为 `INCOMPLETE` 而不是 `PASS`/`FAIL`。 |
| 硬编码项移出自动验收 | 同上 | 删除 `"no_push_to_github": True`，改为 `human_declarations_not_automated`（`verified_automatically: false`），`observed.pilot.pushed` 改为 `declared_false_by_operator_not_measured`。 |
| 复核不覆盖历史 | 同上（`T62_RECHECK_EVIDENCE`） | 复核写 `T62-stage2-real-project-recheck.json`，用 `recheck_of` + `historical_record` 引用运行时记录。原脚本直接写回运行时文件，会把只能当时观测到的事实（transcript 可读、tool_calls 7/8）替换成 `unavailable` —— 属于篡改历史，已避免。 |
| 运行时观测继承 | `scripts/t62_stage2_supporting_record.py` | 服务离线时不再把已观测到的 `tool_calls`/`reads`/`edits`/`shell_commands` 抹成 null，而是逐字段继承并标 `transcript_source`；仅当从未观测过才记 `unavailable`。同时补回 `stage2.db` 里 A/B_rejected/B 三个 pilot 任务（记忆链路结论的依据）。 |
| 脱敏补 git-bash 路径 | `scripts/sanitize_evidence.py` | Worker 的 shell 命令常用 `/f/coding-agent-control-plane/...` 形式，原有脱敏只匹配 Windows 反斜杠路径，导致本机绝对路径入库（G1.6 覆盖不足）。新增 `/f/...` 与通用盘符路径规则，并保证不误伤 URL 与相对路径。 |
| 脱敏补结构化字段 | 同上 | 旧 `SENSITIVE_KEYS` 没有裸 `token`；更严重的是 `re.sub(r"[^a-z0-9]+","_",key.lower())` 把 `accessToken` 压成 `accesstoken`，匹配不到任何白名单项，**所有 camelCase 字段都绕过了结构化脱敏**。现先拆 camelCase 再归一化（`normalize_key`），按词根判定（含 `_token`/`_secret`/`_password` 等后缀），递归到任意嵌套对象与列表；`token_count`/`max_tokens` 这类度量值不当密钥处理，否则会删掉真实证据。 |
| 归档读取重新校验 | `scripts/t62_evidence_model.py` | 旧 `validate_archives()` 只读归档自报的 `problems`，等于相信写入时的结论：把 `arms` 删空、翻转 `verdicts`、改 `pair_id` 都不会被发现。新增 `audit_archive()`，从 `arms` **重算** Arm 数量、角色分布、task_id 唯一性、终态、`verdicts`、`flipped`，并与落盘值比对；`complete`、`archive_readable`、`ledger_recoverable` 也复核。任何不符即 BLOCKED。 |
| `ledger_recoverable` 语义拆分 | 同上 | 该字段被误用来同时表示"原始 Ledger 可恢复"和"归档可读"。现拆成 `ledger_recoverable`（原始 Ledger 是否还能举证）与 `archive_readable`（本文件是否读得到）。 |
| `ledger_recoverable` 按 **verdict** 判定 | 同上 | 旧实现只查 `select 1 ... where id=?`，行在就算可举证。实测 `stage2c-pair2*.db` 里 pair2 两行是 `state='queued', verdict='pending'`——行还在但 verdict 已回退，与 T62-F07 同一形态。现 `_still_in_a_ledger()` 比对归档记录的 verdict，且要求**同一份 ledger** 同时举证该组两条任务。因此 **pair2 的 `ledger_recoverable` 从 true 改为 false**（归档 verdict 未变：有记忆 rejected / 无记忆 verified），pair3 仍为 true。 |
| 复核不再硬编码 | `scripts/t62_stage2_real_project.py` | `worker_started`/`real_model_called` 原为裸 `True`，但复核脚本并不启动 Worker、不调模型。现从运行时记录继承并标 `measured_by` + `measured_in_this_run: false`。`ledger_holds_no_duplicate_rows` 原实现是 `count(*) >= len(rows)`（只能说明表不比任务列表小），改为真正查询重复 id。 |
| transcript 统计口径 | `scripts/t62_stage2_supporting_record.py` | `arms_with_readable_transcript` 会把"本次重读"和"历史继承"混成一个数。拆成 `arms_with_live_transcript` / `arms_with_historical_observation` / `arms_without_transcript`，当前为 0 / 3 / 6。 |
| `status` fail-closed | `scripts/t62_stage2_contrast.py` | 原 `all(...)` 在空列表上为 True，ledger 静默回退到 queued 时也报 `done`。现要求恰好 2 行、1 记忆臂 + 1 控制臂、task_id 不重复、state 与 verdict 均终态，否则 `BLOCKED`。 |
| SQLite 清理守卫 | `scripts/t62_sqlite_cleanup.py`（新） | 替代 `LEDGER.unlink()`。`inspect_ledger()` 只读普查每行 state/verdict/**queue_status**：有 verdict（含 inconclusive）、有 `running`/`starting`/`waiting_for_approval`/`dispatch_unknown`、有 `queue_status='dispatching'`、或状态不可识别，一律拒绝；**拒绝无参数可放宽**。**任何路径都不手工删 `-wal`/`-shm`。** checkpoint 的 busy 只证明"此刻无连接"，从不作为唯一依据。 |
| `setup()` 不再删 Ledger | `scripts/t62_stage2_contrast.py` | 判断"是不是 scratch"本身就是错的问题：`claim_next_ready()` 把 `queue_status` 设为 `dispatching` 时 `state` 仍是 `queued`/`verdict='pending'`，行级普查会把它当成可删。与其继续加状态判断，不如取消删除：每轮 `fresh_ledger_path()` 开新文件，旧文件原样保留为证据。守卫仍在，作为任何其他调用方的硬拦截。 |
| 新 Ledger 可被归档发现 | `scripts/t62_evidence_model.py` | 归档只认 `("stage2c.db","stage2c-pair2.db","stage2c-pair2-final.db")`，而 `setup()` 现在建 `stage2c-runN.db`——新一轮实验即使成功也会被判 `ledger_recoverable=false`、Session/Attempt 找不回、完整证据降级为 INCOMPLETE。新增 `candidate_ledgers()`：显式 hint → `contrast-ledgers.txt` 运行日志（新→旧）→ `stage2c*.db` 通配 → 历史固定名。`setup()` 现在把每轮 ledger 追加进该日志。 |
| 归档按记录路径读 Ledger | 同上 | `recorded_ledger()` 优先读 `pairs/<pair>/ledger.json`（或 summary 内的 `ledger_path`），归档 `source` 记录 `ledger_path` + `ledger_from`。路径不存在即视为未记录，绝不跟着悬空路径走。`read_summary()` 同时接受 list 与 `{"arms": [...]}` 两种形态。 |
| `active_ledger()` 不静默回退 | `scripts/t62_stage2_contrast.py` | 旧实现指针缺失或目标不存在时 `return LEDGER`，会把 `run()` 指向已结束的实验、`status()` 报告旧数据。现改为 `NoActiveLedger` fail-closed：无指针 / 空指针 / 目标不存在一律报错，`status()` 返回 BLOCKED(2)，`run()` 在打开日志和启动 Runtime **之前**就返回，绝不启动。 |
| 回归测试 | `tests/test_t62_g1_evidence.py`（新） | **68 项**（`pytest -q` 实测；含 `queue_status='dispatching'` 不算 scratch、`setup()` 不含删除调用、`ledger_recoverable` 必须比对 verdict、同一 ledger 才能举证整组、`stage2c-runN.db` 可被发现、运行日志排序、pair 元数据路径优先、悬空路径不跟随、无指针/指针目标不存在/不回退历史 ledger 三种 fail-closed 场景）。归档侧：空集/缺 Arm/同角色双 Arm/重复 task_id/第四组/非终态 → BLOCKED，缺 Session → INCOMPLETE，历史组不得声称可恢复，pair1 不得升级；**读盘复核**：删 Arm、单 Arm、verdicts 与 arms 不符、flipped 不符、pair_id 不符、历史组声称可恢复、pair1 改等级 → 全部 BLOCKED。清理侧：默认参数拒绝有 verdict 的 ledger、单条 verdict 也拒绝、inconclusive 也拒绝、running/dispatch_unknown/终态无 verdict/未知状态 → BLOCKED、纯 queued 才可删、拒删时 ledger 与数据完好、源码中无 sidecar 删除。脱敏侧：`token`/`accessToken`/`clientSecret`/`Access-Token`/`apiKey`/`github_token`/嵌套/列表全部脱敏，`token_count`/`max_tokens` 不误伤，`write_evidence()` 落盘后文件不含密钥。另有幂等（两次字节一致、不重复累计）与不加新硬编码检查。 |

复现：

```
python scripts/t62_stage2_contrast_record.py      # 生成/校验 t62 归档 + 汇总
python scripts/t62_stage2_supporting_record.py    # 行为支撑记录（服务离线则为 INCOMPLETE）
python scripts/t62_stage2_real_project.py         # 主验收（缺 transcript 则为 INCOMPLETE）
python -m pytest tests/test_t62_g1_evidence.py -q
```

| # | 位置 | 影响 / 当前处理 | 复现 / 验证 | 状态 |
|---|---|---|---|---|
| M4F-R03 | 真实双 Session 记忆闭环 | 已不再是 BLOCKED：A/B 为两个不同的真实 Session，verdict 均来自正常 Verifier，知识经 Markdown 审核与 `context-verify` 晋升后进入 B 的冻结 Prompt。 | `python scripts/t62_stage2_real_project.py`（需 `.poc-state/t62/stage2*.db`）。 | Stage 2 33/33 自动检查通过，整体 INCOMPLETE（transcript 不可读） |
| T62-G01 | Supporting Record（旧脚本） | pair1 的两个 task 属已损坏 Ledger，旧脚本读成 queued/pending 仍生成 PASS 并写 "Both arms reached the same verdict"，与三组数据矛盾；空的 live ledger 也会被并成第四组。 | `scripts/t62_stage2_supporting_record.py`。 | **已修复**：改消费归档，pair1 标 `historical_record_only`，缺 transcript 记 `unavailable`，结果只能 COMPLETE/INCOMPLETE/BLOCKED |
| T62-G02 | `earlier_pair()` | 从 `T62-stage2-real-project.json` 读 C1/C0，但该文件已无这两个字段，实际恒返回 `None`；pair1 一度只因 "旧文件还在" 而占用汇总位置。 | 读旧脚本 + 现有证据文件。 | **已修复**：pair1 有独立归档，汇总不再依赖验收文件 |
| T62-G03 | `all([]) == True` | 空任务列表/空断言结果被当作"全部完成"，可产出 PASS。 | `scripts/t62_stage2_contrast.py status`、`real_project.py` 断言检查。 | **已修复**：显式数量/唯一性/角色/终态/链路检查，不足即 BLOCKED |
| T62-F01 | LD 决策模型 | 用**逐字节相同**的 Goal 文本创建两个对照 Goal，决策模型把它们改写成了中英两种不同任务描述，并各自附加了不同断言。这让"记忆是否有用"的对照被改写差异污染。 | 对照 `ld_goals` 原文与 `work_items.original_task`/`assertions`。 | P1：对照类实验不能用 LD 模型生成任务文本；已改用 `submit_project_task` 直接提交同指令 |
| T62-F02 | CodeBuddy Worker worktree | worktree 恒定基于**远程默认分支**（`origin/HEAD`），不跟随本地检出分支。首次把 pilot 分支检出在本地时，Worker 实际在 `main`（4a1cdc02）上作业，Verifier 因找不到测试文件而正确拒绝。 | 在带 remote 的仓库检出 feature 分支后派发，比较 worktree HEAD 与 `origin/main`。 | P1：已记录；Pilot 需让默认分支即为目标分支（本次用本地 bare origin 重定向） |
| T62-F03 | CodeBuddy Worker | Worker 会主动 `cd` 出 worktree、在**项目根目录**编辑并提交（B 的第一次尝试），导致 Verifier 看到的 worktree HEAD 不变而被拒。Verifier 的拒绝是正确行为；风险是它绕过了隔离。 | 检查 Worker transcript 的 `cd`/`Edit` 绝对路径与提交落点。 | P1：隔离未被强制；需后续在 adapter 层约束，本轮不改架构 |
| T62-F04 | `tests/test_dataset.py` | 基线提交 f882885 上已有 4 个失败（与本次改动无关）。任务 A/B 的验收断言不覆盖该文件，故未影响 verdict；但"整套测试全绿"不能作为本轮结论。 | 在未修改的 f882885 上直接跑 `pytest -q tests`。 | P2：既有失败，非本轮引入 |
| T62-F05 | 记忆收益 | 受控对照跑了 3 组：pair1 双 verified（有记忆 297.7s，无记忆 320.0s）、pair2 有记忆 rejected/无记忆 verified、pair3 双 verified。累计 有记忆 2 verified 1 rejected、无记忆 3 verified 0 rejected。**分歧只有 pair2，且方向对记忆臂不利——不存在"有记忆通过、无记忆失败"的 pair。** 差异落在 Worker 单次采样波动量级内。 | `scripts/t62_stage2_contrast_record.py` → `evidence/project/T62-stage2-memory-contrast.json`（逐组 `evidence/project/t62/`）。 | **P1 结论：记忆未被证明有收益，也不能证明有害**；旧表述 "failed in opposite directions" 与数据不符，已在 G1 撤回 |
| T62-F07 | 对照实验清理 | 清理时手动删除 `stage2c.db-wal`，导致已完成的对照 ledger 静默回退到 queued 状态：证据文件还在，但 ledger 已不能自证。worktree 提交与 transcript 仍在，故 pair1 可凭记录引用并明确标注来源（`historical_record_only`，永久最低等级）。 | `scripts/t62_sqlite_cleanup.py`：checkpoint 由 SQLite 自己执行并检查 busy 标志，有 verdict 的 ledger 一律拒绝删除，改开新文件。 | P1 操作事故（本人造成，已记录并重跑 2 组重建记录）；清理路径已加守卫与回归测试 |
| T62-G04 | Supporting Record 完整性 | 本地 CodeBuddy 服务离线、pair2 的 SessionReference 已随 ledger 重置丢失、pair1 的 session 从未保留，因此 6 个 Arm 中当前 0 个可读 transcript；行为对比（读取文件数/工具调用数/耗时差）无法复算。 | `python scripts/t62_stage2_supporting_record.py`。 | **未解决（环境限制）**：如实输出 `INCOMPLETE` + 每个 Arm 的 `unavailable` 原因；不估算、不复用他臂数字。不影响三组 verdict 与记忆链路结论 |
| T62-G05 | pair2 的 Verifier 断言 | 有记忆臂 54 项测试全过，仅 `file_contains: normalized_1000` 失败，判为 rejected。该断言检查字面量出现，可能过严（实现可用等价命名表达同一语义）。 | 对照 `.poc-state/t62/pairs/pair2/` 两份 patch 与断言结果。 | **未解决**：已作为第三层结论记录；重跑时应改用行为断言，不能把此 rejected 当功能失败 |
| T62-F06 | `ld_goals` 写门控 | 项目内存在 `needs_human` 任务后，该项目的后续 Goal 提交被 M4-A 保守门控拦住（`M4A-01` 设计如此）。本轮因此改用干净项目承载对照臂。 | 在有 rejected 任务的项目再提交新 Goal。 | 设计边界；建议日常使用提供显式解除入口 |

## 处理原则

- 任何"已修复/Pass"结论都必须指向可重现测试与脱敏证据；stub/确定性 Pass 不等于
  真实集成 Pass。
- "两个任务都 verified、代码真实接续"与"Goal 自动进入 completed 并生成只含本
  Goal 任务的最终报告"分别断言，不得用前者代替后者。
- Handoff 不作为模型可选动作开放；依赖任务交接由控制平面确定性执行。
