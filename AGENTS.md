# AGENTS.md

给在这个仓库里工作的 AI Agent（以及人）的硬性约定。

产品定位：**Project-Centric LD System**。Control Plane 是执行治理层，不是最终产品。
分层见 [docs/PROJECT_CENTRIC_ROADMAP.md](docs/PROJECT_CENTRIC_ROADMAP.md)。

## 1. 边界

- `adapters/base.py` **不得**出现任何厂商名、厂商状态字符串或厂商字段。
- 厂商专有知识只允许出现在 `adapters/<vendor>/` 内（状态映射、REST 路径、磁盘布局、已知缺陷）。
- 控制平面自身的东西（任务存储、幂等、验收、汇总、项目上下文）放在 `controlplane/`。
- 新接一个 Worker 时，只允许新增 `adapters/<vendor>/`，不允许改 `controlplane/` 的接口语义。

### 1.1 分层职责（M1 引入，后续阶段沿用）

| 层 | 允许做什么 | 不允许做什么 |
|---|---|---|
| `controlplane/project_context.py` | 存取项目知识、版本 CAS、验证状态、安全域 | 调用 LLM；读写 Worker；跨项目共享条目 |
| `controlplane/context_builder.py` | 把知识渲染成 Effective Prompt；报告缺口 | import 任何 `adapters/<vendor>`；静默补全缺失事实 |
| `controlplane/handoff.py` | 只读准备交接；校验来源/Session/checkpoint/目标 Workspace | 调用 Worker；写 Ledger；自动 merge/rebase |
| `controlplane/project_service.py` | 对 LD 暴露确定性查询/提交/工作区接口 | 判断任务成败（必须读 Ledger 的 verdict）；自动审批 |
| `controlplane/plane.py` | 派发治理、审批、幂等、恢复 | 存业务知识；解释"项目目标" |

- **Project Context 不是常驻 Agent。** 不得为每个项目起后台 LLM 进程。
- **进度不得有第二份真相。** 任务状态只能从 Ledger 派生；`progress` 条目只能写
  叙述性内容（阻塞、下一步），不得复制任务状态。
- **不得假装支持远程执行。** `workspaces.node_id != 'local'` 时只能判为阻塞/不支持，
  不得静默在本机执行。

## 2. 证据

- 每条结论都要能指到一个 `evidence/**/*.json` 文件；没有证据的结论写"未验证"。
- 证据文件写盘前必须脱敏（`password` / `token` / `authorization` / `cookie`）。
  统一用 `sanitize_evidence.write_evidence()`，不要裸写 `json.dumps`。
- 原始服务日志、完整 transcript 不入库（`.gitignore` 已排除）。
- 不得手写、编造或"修复"证据内容；失败就是失败。
- 重跑旧测试**覆盖**旧证据属于篡改历史：跑之前先备份，或跑完用 git 恢复旧文件，
  只保留本轮新增的证据。

## 3. 术语（不可混用）

| 说法 | 含义 |
|---|---|
| 对话恢复 | 能拿回历史上下文（session resume） |
| 进程 respawn | 新进程绑定旧会话 |
| 执行恢复 | 中断的命令继续跑 |
| 工作流恢复 | 控制平面根据持久化状态判断"哪些步骤完成、哪些要重试、哪些要人工" |
| 状态对账 | 重新查询真实状态并修复本地视图 |
| 事件重放 | worker 侧能补发断线期间的事件 |
| **项目上下文** | 独立于 Session 的持久知识（`project_context_entries`），不是聊天记录 |
| **Effective Prompt** | 注入项目上下文后、真正派发给 Worker 的最终 Prompt |
| **上下文版本** | 项目级单调递增计数器，派发时冻结，用于追溯"这个任务看到了哪些知识" |
| **Session 交接** | 新 Session 依靠 Project Context 继续工作，而非复用旧会话 |

## 4. 状态判定

- Worker 的终态（`done/failed/...`）只是**它自己的说法**，不是验收结论。
- 验收必须基于 dispatch 时声明的产物断言（`ArtifactAssertion`）。
- 没有断言的成功任务 → `inconclusive`，不许当通过。
- 被拒绝/被停止/失联的任务 → 一律 `rejected`。
- `pid_alive()` 返回 `None` 是"无法确认"，**不得**当作死亡：不释放并发容量、
  不触发重试。三态（True/False/None）不可压缩成两态。
- 持久化降级标记失败时，不得宣称"禁令跨重启生效"。

## 5. 知识写入规则（M1 引入）

- 条目必须指定 `kind`（`background` / `architecture` / `constraints` / `progress` /
  `knowledge` / `decisions`）与 `verification`（`unverified` / `candidate` / `verified`）。
- **Agent 自动产生的内容一律 `candidate`**，不得直接写 `verified`。
- **提升为 `verified` 必须有 `evidence_refs`**，且每条都能指到实际存在的证据文件。
- **`constraints` 的 `verified` 状态只能由人确认。** M2 自动回写不得创建或修改
  `constraints`。未验证的约束可以被展示，但不得当作硬性安全策略执行。
- 更新条目必须带 `expected_version`（CAS）；冲突时抛 `ContextVersionConflict`，
  不得回退成静默覆盖。
- 跨项目读写一律拒绝：`get_entry()` 发现 `project_id` 不匹配必须抛
  `ContextError`，不得返回空结果。
- `security_domain='local'` 的内容不得写入任何外部服务/外部模型调用。

### 5.1 自动回写规则（M2 引入）

- 自动回写只允许 `progress` 与 `knowledge`，且 `verification` 必须为 `candidate`。
- 每个自动条目必须有真实存在、位于 `evidence/` 下的 `evidence_refs`，写盘前统一脱敏。
- 幂等键固定为 `(task_id, attempt_id, entry_kind, entry_title)`；重放同一验收不得增加
  条目或上下文版本。
- CAS 冲突不得自动重试覆盖。保留输家为独立 `needs_merge` 候选，等待人工合并。
- `progress` 只写阻塞与下一步等叙述，不复制 Ledger 的 state/verdict；权威进度仍由
  `ProjectService.get_project_progress()` 从 Ledger 派生。
- 自动回写失败不得回滚或改写已持久化的验收 verdict；记录失败事件，允许显式重放。

### 5.2 Session 登记与交接规则（M3 引入）

- SessionReference 只能来自含真实 `session_id + worker_job_id` 的 Snapshot；禁止在
  enqueue 时写 NULL 占位行。
- Attempt 必须通过 Snapshot 的 `worker_job_id` 查找，不得盲用
  `current_attempt_id`；同一 Attempt 观测到不同 Session 必须显式冲突，不得覆盖。
- Handoff 是一个带来源、checkpoint 和冻结上下文的普通 WorkItem；不得新建
  第二套 handoff 任务表、状态机或验收真相。
- `prepare_handoff()` 必须只读、确定性、不调 Worker；`submit_handoff()` 提交前
  重做关键校验，且来源信息必须随 WorkItem 创建原子落入 metadata。
- `needs_merge` 不得自动选边；checkpoint 不得自动 merge/rebase。目标 Workspace
  未包含 checkpoint 时必须阻塞或由人显式选择兼容 Workspace。
- Handoff 幂等键同时绑定 `handoff_signature`：同 key + 异 signature 必须报冲突。
- 不得把 stub/SQLite 行为宣称为真实 Worker 交接成功。真实双 Session 接续
  未执行前，M3 只能写“功能已实现、未验证”。

### 5.3 最小 LD 边界（M4-A 引入）

- LD 只能走 `LDTools → ProjectService → ControlPlane/Ledger`；不得直读写
  SQLite，不得绕过 `ProjectService` 直接派发 Worker。
- LD 只允许 `inspect` / `submit_task` / `wait` / `request_human` / `finish`
  五种结构化动作；模型输出的 Python、Shell、`command` 或未知字段不是
  控制平面命令。
- LD 不得自动 `approve` / `deny`，不得写 `constraints` 或 `verified`
  知识，不得修改已入队任务的 Effective Prompt。
- M3 验收前 LD 不开放 Handoff 工具；`needs_merge` 涉及当前任务前置条件时
  必须转 Human Owner，不得自动选边。
- `needs_approval` / `dispatch_unknown` / `suspected_lost` /
  `session_binding_conflict` 以及不明确的执行位置默认 `request_human`。
- 模型调用必须显式声明允许的安全域；未授权模型输入不得包含
  `security_domain='local'` 的项目内容、项目路径或 Effective Prompt。
- `finish` 必须重新查询 Ledger 中的 `verified` verdict 或确定性可验证条件，
  不得仅依据 Worker 终态/自述。
- M4-A 只是唯一全局 LD 的单轮协调；不得自行扩成无限循环、多 LD、
  复杂 Goal DAG 或 Agent 协商。未执行专项测试前只能标记“功能开发完成、未验证”。

## 6. 派发与审批规则（M1 补充）

- 入队即冻结 Effective Prompt。已入队任务的内容不得因上下文更新而改变。
- `task_digest` 必须绑定实际执行内容；项目任务的 digest 还包含
  `workspace_id` 与 `context_version`，Handoff 任务还要包含 `handoff_signature`。
- 任何改变实际派发内容（Prompt / 断言 / cwd / workspace / context_version）的改动，
  必须使旧审批失效，不得复用。
- 前置条件缺失时必须阻塞并报告缺口（`ContextBuildBlocked` / `built.gaps`），
  不得编造缺失事实继续派发。

## 7. 运行纪律

- 长时间测试用 `scripts/detached.py`，不要占用交互式前台。
- 不启动可见窗口的进程；服务用 `scripts/serve_ctl.py`（`DETACHED_PROCESS`）。
- 测试结束必须停掉服务：`python scripts/serve_ctl.py stop`。
- 不在 `sandbox/` 之外创建任何测试产物（临时 ledger 放 `.poc-state/`）。
- 真实 Worker 集成测试与 stub 测试必须分开标注；stub 通过不代表真实派发通过。
