# Known Issues

本文档记录当前已知问题与专项验证缺口。M3 本地可确定性验证项（M3-03~M3-12）
已由 T33 覆盖（不启动 Worker）；M3-01/M3-02 与全部真实 CodeBuddy 集成项仍待
真实环境验收。M4-A 已完成 T32 确定性边界测试，且 M1/M2 回归（T26–T31）已在
本地复跑全部通过；真实模型/Worker 集成结论仍缺失。T32/T33 均未启动 Worker。

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
| T38-F01 | 真实模型（GPT-5.5）在目标推进的收尾阶段倾向保守转人工：三次真实运行中两次在应 `finish` 或继续提交时选择 `request_human`（决策本身合法）。 | 目标停在 needs_human；需要 `run-goal --resume` 显式解锁（该动作本身被记录为 `human_resume` 决策）。这不是缺陷而是安全设计内的行为：人工只说"继续"，无需传递任何项目状态或任务内容。`finish` 的确定性校验由 T32/T37 覆盖。 | 已知行为；`--resume` 机制已提供 |
| T38-F02 | 首次真实运行中模型为任务选择了 `git_clean` 断言，而 agent 完成后未 commit，验收正确判 rejected（Verifier 行为正确）。 | 属于目标文本引导问题而非系统缺陷：在目标中明确要求"完成后 git commit"且建议使用 `file_exists`/`file_contains` 断言后，后续运行任务全部 verified。控制平面的 `capture_source_checkpoint` 仍作为 dirty worktree 的确定性兜底。 | 已通过目标文本引导缓解；Verifier 语义未放宽 |

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

## 本轮处理原则

- M3-01/M3-02 需要真实 CodeBuddy Worker，保持待验证；其余 M3 项已由 T33
  （`scripts/t33_m3_handoff.py`，证据 `evidence/project/T33-m3-handoff-deterministic.json`）
  以不启动 Worker 的确定性方式验证。
- LD 工具面未开放 Handoff，避免在 M3 真实集成验收前扩大上述未确定性。
- 任何后续“已修复/Pass”结论都必须指向新增的可重现测试与脱敏证据。
