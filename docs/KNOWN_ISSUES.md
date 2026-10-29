# Known Issues

本文档记录当前已知但尚未经专项验收的问题。本轮按计划只完成 M4-A
功能开发，**未启动 CodeBuddy Worker、未运行 M3/M4-A 测试，以下项均为
“待验证”，未在本轮修复**。

## M3 待验证清单

| # | 源码位置 | 影响 | 最小复现 / 验证方法 | 状态 |
|---|---|---|---|---|
| M3-01 | `adapters/codebuddy/adapter.py::_snapshot()` / `stream_events()`；`controlplane/plane.py::_save_snapshot()` | 真实 CodeBuddy 在启动、刷新、终态、重启后对账等路径是否稳定返回 `session_id` 尚不明确；缺失时 SessionReference 不会登记。 | 启动真实 Worker，覆盖 dispatch、refresh、terminal、Worker restart 四条路径，逐次对比 Snapshot 与 `session_references`。 | 待验证 |
| M3-02 | `controlplane/handoff.py::HandoffPlanner.prepare()` / `_render_prompt()` | Session B 是否可在不读 Session A transcript、不 resume 旧会话的前提下，仅依据 Project Context + checkpoint 继续并通过验收未验证。 | Session A 产生中间 checkpoint 后关闭；创建全新 Session B，禁止 transcript/resume，执行 Handoff WorkItem 并检查 `verdict=verified`。 | 待验证 |
| M3-03 | `controlplane/plane.py::_snapshot_patch()`；`controlplane/handoff.py::_checkpoint()` | Worker worktree 关闭或清理后，记录的 `head_commit` 是否仍在目标 Workspace 可达不确定，可能使合法交接变成阻塞。 | 让 Session A commit 后关闭/清理 worktree，再调用 `handoff-prepare`，检查 commit object 、可达性和 blocker。 | 待验证 |
| M3-04 | `controlplane/store.py::TaskStore.__init__()` / `_migrate_columns()` / `_migrate_legacy_json()` / SessionReference 索引与触发器 | 历史 Ledger 的 schema 迁移、重复 SessionReference 清理、部分唯一索引/触发器创建尚未在旧库样本上回归；可能导致启动失败或遗留歧义绑定。 | 备份一份 M3 前 Ledger，插入重复/缺字段历史行，用新版 `TaskStore` 打开，检查迁移、索引、触发器和数据投影。 | 待验证 |
| M3-05 | `controlplane/store.py::observe_session_reference()` / `_insert_session_reference()` | 多进程/多线程同时绑定同一 Attempt 时，首个绑定是否稳定获胜、其余是否全部显式冲突未验证。 | 用两个独立 SQLite 连接对同一 Attempt 并发提交不同 Session，检查唯一绑定和冲突事件。 | 待验证 |
| M3-06 | `controlplane/plane.py::_save_snapshot()` | `_save_snapshot()` 先更新 WorkItem，再登记 SessionReference，不是单一事务。绑定冲突时 WorkItem `session_id` 可能与首个不可变 SessionReference 不一致。 | 先为 Attempt 登记 Session A，再保存指向 Session B 的 Snapshot；检查 WorkItem、SessionReference、`session_binding_conflict` 事件三者是否一致。 | 待验证（已知原子性窗口） |
| M3-07 | `controlplane/plane.py::_save_snapshot()`；`controlplane/store.py::append_event()` | Session 冲突事件使用当前 WorkItem Attempt ID；如果是迟到的旧 Attempt Snapshot，事件可能关联到错误 Attempt。 | 创建 Attempt A/B，在 B 成为 current 后送达 A 的冲突 Snapshot，检查 event `attempt_id`。 | 待验证（已知关联窗口） |
| M3-08 | `controlplane/handoff.py::HandoffPlanner.prepare()`；`controlplane/project_service.py::submit_handoff()` | 交接的 Git/context 校验与 enqueue/真实 dispatch 之间仍有 TOCTOU 窗口；入队后 Workspace HEAD 可再次漂移。 | prepare 通过后修改目标 HEAD，分别在 submit 前和 submit 后/dispatch 前复现，检查哪个窗口被签名拦截。 | 待验证（已知 TOCTOU） |
| M3-09 | `controlplane/handoff.py::_checkpoint()` | 当 source worktree 存在时严格要求 `git status --porcelain` 为空；真实 Worker 生成未跟踪运行产物时，可能阻塞本可安全交接的 checkpoint。 | 在 source worktree 留下与交付无关的 untracked 文件，运行 `handoff-prepare`，评估 blocker 是否过于保守。 | 待验证（已知保守防护） |
| M3-10 | `controlplane/handoff.py::handoff_signature()`；`controlplane/project_service.py::submit_handoff()` | 同一 idempotency key 在 Context/target HEAD 漂移后会显式签名冲突；语义正确，但实际恢复/重试的操作体验未验证。 | prepare 后更新 Context 或 target HEAD，用同 key 重提，确认返回 `HandoffStale`/`HandoffIdempotencyConflict` 而不是新任务。 | 待验证 |
| M3-11 | `scripts/controlplane_cli.py::_run_project_command()` | Project 子命令已改为按 `default_adapter` 创建只有名称的 Service Plane，但尚未回归；如回归失败，项目任务可被错误标记为 `ledger-only` 而不被 Runtime claim。 | 用非 `ledger-only` 的 `default_adapter` 注册项目，CLI submit/handoff-submit 后检查 WorkItem `adapter`，再运行对应 Runtime claim。 | 待验证 |
| M3-12 | `controlplane/store.py::session_references()` | 旧 Ledger 中 `legacy_placeholder` 与项目/工作区/任务删除后的悬挂状态投影尚未用历史数据验证，可能误报“可交接”或隐藏旧脏数据。 | 构造 NULL 历史占位、缺失 Workspace/WorkItem 的 reference，查询 Session Registry 投影并检查显式状态。 | 待验证 |

## 本轮处理原则

- 上述项未阻塞 M4-A 的受限查询、普通项目任务提交和单轮协调，因此本轮不修复。
- LD 工具面未开放 Handoff，避免在 M3 验收前扩大上述未确定性。
- 任何后续“已修复/Pass”结论都必须指向新增的可重现测试与脱敏证据。
