# Project-Centric LD System — Roadmap

本文档取代 [V0.2_PLAN.md](V0.2_PLAN.md) 作为后续路线图。
V0.2_PLAN 描述的是 **v0.2 阶段**（本地单用户控制平面），其范围已实现，
保留作为历史记录，不再作为前进方向。

产品定位：

> 一个 Human Owner 管理一个全局 **LD Agent**，LD 协调多个拥有独立上下文的
> **Project**，每个 Project 通过 **Coding Agent** 执行任务。
> Control Plane 是底层的执行治理层。

```text
Human Owner → LD → Project(Context/WorkItems/Sessions) → Control Plane → Worker
```

---

## 阶段总览

| 阶段 | 目标 | 依赖 | 状态 |
|---|---|---|---|
| M1 | Project Context Foundation | v0.2 已完成 | **已完成** |
| M2 | Project Context Update Loop | M1 | **已完成** |
| **M3** | Session Registry / Handoff | M1（M2 有益但非必需） | **确定性（T33）与真实 Worker（T34）验证通过；git commit checkpoint 路径未覆盖** |
| **M4-A** | Minimal LD（受限工具、结构化决策、单轮协调） | M1 + M2 | **已完成**（T32/T36 确定性 + T38a 真实模型与真实 Worker 单轮集成验收） |
| **M4-B** | 有界连续推进（持久化 Goal + 依赖任务确定性交接 + 恢复） | M3 + M4-A | **已完成**（T39/T40 确定性含崩溃注入与租约 fencing；T38b 真实模型严格闭环 25/25 Pass；T38c 真实故障恢复与副作用拒绝验证） |
| M5 | Distributed Workspace | M1–M4 | 未实现 |

依赖是**功能依赖**而非排期依赖。M4-B 的依赖任务交接走控制平面确定性
升级（`checkpoint_mode=reachable`），不把 Handoff 暴露为模型决策。
M5 之前不得宣称支持跨机器协同。

---

## M1 — Project Context Foundation ✅ 本轮完成

### 交付

| 交付物 | 实现位置 |
|---|---|
| Project Context Store（6 类知识、项目隔离、版本 CAS、revision 历史） | `controlplane/project_context.py` |
| 验证状态（`verified` / `candidate` / `unverified`）+ 证据要求 | 同上 `verify_entry()` |
| 安全域（`local` / `shareable`，无自动同步） | 同上 + `get_project_context(include_shareable=…)` |
| 来源与追溯（`source` / `evidence_refs` / 时间戳 / revision 快照） | 同上 |
| Workspace 表 + `projects.root` → 默认本地 Workspace 自动映射 | `controlplane/store.py` |
| SessionReference 表（project ↔ workspace ↔ session ↔ task ↔ attempt） | 同上 + 提交路径写入 |
| Context Builder（固定结构、字符预算、标签/关键词筛选） | `controlplane/context_builder.py` |
| 硬约束完整注入（`constraints` 不受预算截断） | 同上 |
| 前置条件缺失 → 阻塞缺口，不编造 | `ContextBuildBlocked` / `built.gaps` |
| Effective Prompt 冻结 + `original_task` / `context_version` / `context_entry_refs` 落库 | `ControlPlane.enqueue()` + `plane._new_record()` |
| 审批绑定执行内容（`digest_scope` 含 workspace + context_version） | `plane.task_digest(extra_scope=…)` + `_dispatch_record()` |
| 项目级查询 Facade | `controlplane/project_service.py` |
| CLI 项目上下文命令 | `scripts/controlplane_cli.py` |
| PID 三态判断 | `ControlPlane._process_alive()` |
| 降级标记 I/O 诚实化 | `TaskStore._write_degraded_marker()` / `recover_degraded()` |

### 可测试的退出条件（全部已实际执行）

| # | 退出条件 | 测试 | 结果 |
|---|---|---|---|
| 1 | A 项目知识不会注入 B 项目 | T27-A | Pass |
| 2 | 不同项目可同名条目；同项目同名冲突 | T27-A | Pass |
| 3 | 并发更新同版本仅一个成功，另一个得显式冲突 | T27-B | Pass |
| 4 | 版本重启后不丢失 | T27-B | Pass |
| 5 | 来源/证据/时间戳可追溯，无证据不得 verify | T27-C | Pass |
| 6 | 未验证知识不是硬约束；`local` 与 `shareable` 可区分 | T27-D | Pass |
| 7 | 新任务获得项目背景与硬约束 | T28-A | Pass |
| 8 | Effective Prompt 可从 Ledger 追溯 | T28-A | Pass |
| 9 | 入队后改上下文不改变原任务 Prompt | T28-B | Pass |
| 10 | 改已审批的实际派发内容不能复用旧审批 | T28-C | Pass |
| 11 | 缺前置条件 → 阻塞，不入队、不调 Worker | T28-D | Pass |
| 12 | 旧 enqueue/dispatch 行为与 digest 不变 | T28-E | Pass |
| 13 | `pid_alive()=None` 不判 lost、不释放容量 | T29-A | Pass |
| 14 | 降级标记写失败不宣称跨重启保护 | T29-B | Pass |
| 15 | 审计日志/标记删除失败不得解除执行禁令 | T29-C | Pass |
| 16 | 不可读标记仍降级 | T29-D | Pass |
| 17 | CLI 端到端（README 示例） | T30 | Pass |

### 明确不做（M1 范围外）

- 不接 LLM，不做任何自动规划。
- 不自动回写知识（显式调用才写）。
- 不做远程派工；非 `local` workspace 直接判为阻塞。
- 不引入向量库 / 知识图谱 / 第三方 Memory 框架。
- 不重建数据库、不删除 `projects.root`、不破坏现有 CLI。
- 不做 HTTP 接口。

### 已验证 vs 未验证

**已验证（真实执行）**：上表 17 项，证据 `evidence/project/T27|T28|T30*.json`、
`evidence/recovery/T29-reliability-edges.json`。

**未验证**：
- Project Context 与**真实 CodeBuddy Worker** 的端到端组合（T27–T30 全用 stub adapter，
  不启动 Worker）。真实派发路径本身由既有 T11/T12/T14 覆盖，但两者叠加未跑。
- 大量条目（超出预算）时的筛选质量——只验证了预算生效与硬约束不截断，
  没有评测"选中的条目是否真是最相关的"。
- 非 ASCII（中文）上下文的预算估算精度（按字符计，非 token）。

---

## M2 — Project Context Update Loop ✅ 本轮完成

### 目标

让项目知识在执行后**自动**长出来，同时不让 Agent 的猜测污染事实。

### 交付

| 交付物 | 实现位置 |
|---|---|
| 三类终态 verdict → 叙述性 `progress` 候选 | `controlplane/context_update.py` |
| 从 attempts / events / assertions / warnings 提取 `knowledge` 候选 | 同上 `_knowledge_body()` |
| 每个 task/attempt 的脱敏证据快照 | `evidence/project-updates/` + `sanitize_evidence.write_evidence()` |
| 数据库级幂等回执与跨进程 claim | `context_update_receipts` + `TaskStore.claim_context_update()` |
| CAS 冲突保留为独立 `needs_merge` 候选 | `ProjectContextUpdateLoop._preserve_conflict()` |
| Runtime 终态验收自动触发 + 显式安全重放 | `ControlPlane._sync_project_context()` / `update_project_context()` |
| verdict 已提交、回执未提交的崩溃窗口恢复 | `reconcile_project_context_updates()` |
| CLI 重放与回执查询 | `project context-sync-task` / `context-update-log` |

### 最小交付

1. **验收后生成进度更新**：`verified/rejected/inconclusive` 三类结果分别映射为
   `progress` 条目的候选草稿（`candidate`，非 `verified`）。
2. **从执行证据提取候选知识**：从 `attempts` / `events` / `assertion_results` /
   `warnings` 中提取"这次发现了什么"，写成 `knowledge` 候选条目，带
   `source=task:<id>` 与 `evidence_refs`。
3. **已验证事实与推测分离**：自动生成的一律 `candidate`；只有带证据且经人（或
   确定性规则）确认的才升 `verified`。硬约束**永不**由自动流程创建。
4. **版本冲突处理**：回写使用 CAS；冲突时保留双方，标记 `needs_merge`，不静默覆盖。
5. **同一事件幂等回写**：以 `(task_id, attempt_id, entry_kind, entry_title)` 为幂等键，
   重放同一次验收不会产生重复条目。

### 退出条件（全部已实际执行）

| # | 退出条件 | 测试 | 结果 |
|---|---|---|---|
| 1 | verified / rejected / inconclusive 均生成 progress + knowledge | T31-A | Pass |
| 2 | 自动条目全部为 candidate，且只有 progress / knowledge | T31-A、T31-D | Pass |
| 3 | evidence_refs 非空、位于 evidence/ 且文件真实存在 | T31-A | Pass |
| 4 | 同一验收重复重放不新增条目、不增加上下文版本 | T31-B | Pass |
| 5 | 两个独立 SQLite 连接并发提交同一 CAS 版本：一胜一 needs_merge | T31-C | Pass |
| 6 | 冲突双方 body 均保留，无静默覆盖 | T31-C | Pass |
| 7 | 自动 constraints、自动 verified、跨项目写入全部拒绝 | T31-D | Pass |
| 8 | verdict 已提交但无回执的任务可恢复，完成后不再重复扫描 | T31-E | Pass |

证据：`evidence/project/T31-context-update-loop.json`。

### 明确不做

- 不做 LLM 总结式回写（第一版用确定性规则）。
- 不允许自动修改 `constraints`。
- 不做跨项目知识提升（A 的结论不会自动变成 B 的结论）。

### 已验证 vs 未验证

**已验证（stub Worker）**：上表 7 项；真实运行了终态验收钩子、SQLite 幂等回执与
双连接并发冲突路径。

**未验证**：Project Context 自动回写与真实 CodeBuddy Worker 的组合尚未端到端执行；
当前知识提取只使用确定性结构化字段与警告代码，不评估自然语言总结质量。

---

## M3 — Session Registry / Handoff ✅ 确定性与真实 Worker 验证通过

### 目标

一个 Project 下可以登记多个 Session，且**新 Session 不需要旧 Session 的完整聊天记录**
就能继续工作。

### 最小交付

1. Project 下登记多个 Session（`session_references` 已有表，补齐写入点）。
2. 完整保存 `node / worker_instance / session / task / attempt` 关系链。
3. 创建新 Session 时，用 Project Context + 已派发任务的事实重建工作状态，
   而不是要求 `--resume` 旧会话。
4. 一个可执行的**交接验证**：Session A 做到一半 → 关闭 → Session B 用
   Project Context 继续 → 产出通过验收。

### 已实现的功能切片

| 交付物 | 实现位置 | 当前状态 |
|---|---|---|
| Snapshot 观测时登记 Session，不再写 enqueue 空占位 | `ControlPlane._save_snapshot()` | 已实现；T33 Pass |
| `node / worker_instance / session / task / attempt` 完整链 | `TaskStore.observe_session_reference()` | 已实现；T33 Pass |
| Attempt 级唯一绑定 + 异 Session 冲突显式转人工 | `session_references` 部分唯一索引 + Store/Plane | 已实现；T33 Pass |
| 只读 Handoff Prepare（来源/Session/Workspace/checkpoint/context） | `controlplane/handoff.py` | 已实现；T33 Pass |
| Handoff Submit 创建普通 WorkItem，来源与 checkpoint 原子落 metadata | `ProjectService.submit_handoff()` | 已实现；T33 Pass |
| 提交前重检、冻结 Prompt/context/workspace、内容签名幂等 | 同上 + `ControlPlane.enqueue()` | 已实现；T33 Pass |
| CLI `project handoff-prepare / handoff-submit / sessions` | `scripts/controlplane_cli.py` | 已实现；T33 Pass |

实现采用的最小模型是：

```text
session_references（增强现有表）
+ 来源/Session/checkpoint/context 随目标 WorkItem 原子落库
+ 复用 attempts / events / idempotency / approval / verdict
```

没有新建 `sessions + session_attempt_links + handoff_intents`，也没有新建
Handoff 状态机或第二份验收真相。

### 退出条件

- 关闭 Session A（进程退出）后，Session B 从 Project Context 能回答：
  项目目标、当前进度、上次结论、下一步。
- 交接验证任务在"无旧会话"条件下通过验收（`verified`）。
- SessionReference 中不存在指向已删除项目/工作区的悬挂记录（或悬挂被显式标记）。

**当前退出判定：核心条件已满足（T33 + T34）。** 交接验证在真实 Worker 上完成
（Session B 不读旧 transcript、不 resume，verdict=verified）；Session Registry
无悬挂歧义（T33 M3-12）。唯一未覆盖：git commit 形式的 checkpoint（worker job
不映射 `head_commit`，真实交接当前只能 `context_only`；补齐 commit 映射后需
重跑 T34 的 checkpoint 分支）。

完整待验证问题、影响与复现方法见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

### 明确不做

- 不承诺原生 Session 可跨机器直接迁移。
- 不在本阶段做自动上下文摘要压缩（超预算时先按 M1 的预算规则截断）。

---

## M4-A — Minimal LD ✅ 已完成（确定性 + 真实集成验收）

### 目标

唯一的全局 LD 接上 M1/M2 的确定性工具，完成一次可审计、可 dry-run、
默认转人工的最小协调，不扩展为自主循环。

### 已实现功能切片

| 交付物 | 实现位置 | 安全/语义边界 | 当前状态 |
|---|---|---|---|
| LD Tool Facade | `ld/tools.py` | 仅 7 个工具；不开放 context 写、approve/deny、Handoff、Policy、直接 Worker 派发；提交签名绑定 Workspace/Assertions/执行意图但不绑定 Context Version | T32 Pass |
| 任务状态确定性投影 | `ProjectService.get_task_status()` | Worker state 与 control-plane verdict 分离，不暴露/修改 Effective Prompt；投影 LD goal/signature 引用 | T32 Pass |
| 结构化决策 | `ld/decision.py` | `inspect / submit_task / wait / request_human / finish`；严格拒绝未知字段与命令参数 | T32 Pass |
| 单轮协调器 | `ld/coordinator.py::LDCoordinator.run_once()` | 可注入最小模型接口；无模型明确返回 `model_not_configured` | T32 Pass |
| 确定性人工门控 | `ld/coordinator.py` | 读取在授权项目范围内允许；新提交遇到 approval/dispatch unknown/lost/session conflict 时默认 `request_human` | T32 Pass |
| 可核验 `finish` | `ld/decision.py` + `ld/coordinator.py::_finish()` | 至少一个 `verified` WorkItem 必须带当前目标稳定指纹；项目计数只能作辅助条件 | T32 Pass |
| 最小 CLI | `scripts/ld_cli.py` | 同一 Ledger；`--decision-json` 仅读结构化数据；不调 Worker | T32 Pass |

### 决策 Schema（`m4-a.v1`）

所有决策都必须有且只有：

```json
{
  "action": "inspect | submit_task | wait | request_human | finish",
  "project_id": "prj-... or null",
  "reason": "non-empty string",
  "evidence_refs": ["ledger/context/task references"],
  "parameters": {}
}
```

- `inspect`：只能选 `list_projects / get_project_brief / get_project_progress /
  get_project_tasks / get_task_status / get_task_provenance`。
- `submit_task`：必须含 `task + idempotency_key`；可选产物断言、优先级、标签、
  前置条件、本地 Workspace 与 timeout；不允许 `allow_blocked / policy / metadata`。
- `wait`：必须含 `task_id`。
- `request_human`：必须含 `question`，可带 `blocking_reasons`。
- `finish`：必须有非空 `evidence_refs`，并明确引用至少一个 `verified_task_id`
  或 `task_verdict=verified` 条件；执行时重新查 Ledger，且至少一个任务必须携带
  当前 Human Owner 目标的稳定 SHA-256 引用。项目计数只能作为辅助条件。

`python / shell / command / code / approve / deny / handoff / effective_prompt`
等不在 Schema 内的控制字段一律拒绝。

### 安全域与模型输入

- 协调器默认不向模型开放任何 Project Context 安全域。
- 注入模型时必须显式设置 `model_authorized_security_domains`；只有获准的
  `shareable`/`local` 条目标题与进度才进入有限决策输入。
- Human Owner 目标默认标为 `local`；模型未获该域授权时不调用模型。
- 项目 root、Effective Prompt、未获准域的内容始终不进入决策输入。

### 退出条件（部分执行）

1. 7 个 LD 工具的白名单与禁止路径测试通过。
2. 五种动作、未知字段、命令注入、跨项目与 `finish` 证据校验通过。
3. `dry_run=True` 不新增 WorkItem；非 dry-run 最多提交一个普通任务。
4. 所有指定异常状态均转 `request_human`，且没有 approve/deny/Handoff 路径。
5. 未授权模型输入不含 `local` body/root/Effective Prompt。
6. 绝不仅凭 Worker state 返回 `finish`。

T32 已在不启动 Worker 的前提下覆盖七工具白名单、五种动作、四类异常门控、
未知命令字段、项目任务查询作用域、模型输入安全域、CLI、提交请求签名、
跨连接并发冲突、当前目标 finish、dry-run 与无模型路径，证据为
`evidence/project/T32-m4a-deterministic.json`。T36
（`evidence/project/T36-m4-model-boundary.json`）以本地假端点确定性覆盖真实
模型传输层：错误分类（HTTP/超时/非法响应）、fail-closed、未授权域不调用
模型、provenance 模型视图过滤（M4B-01 解决）。

**当前退出判定：M4-A 集成验收已满足。** 真实模型（OpenAI-compatible
端点，`ld/model.py::OpenAIChatModel`，无 SDK 依赖）+ 真实 CodeBuddy Worker
的单轮链路已完整执行：结构化 submit_task → context 注入 → Runtime 派发 →
Verifier 双断言 verified → M2 自动上下文更新，证据为
`evidence/project/T38a-m4a-real-single-turn.json`。M1/M2 回归（T26–T31）
复跑全部 Pass。

### 明确不做

- 不做多 LD 协同。
- 不做自动审批。
- 不做跨项目资源抢占策略（先 FCFS + 优先级）。
- 不做无限自主循环、复杂 Goal DAG 或引入 Agent Framework。
- M3 验收前不向 LD 开放 Handoff。

---

## M4-B — 最小连续推进（有界 Goal）✅ 真实闭环验证通过（T38b 25/25）

### 目标

Human Owner 只声明一次目标；LD 在有限预算内查询项目、提交任务、等待验收、
根据真实 verdict 继续派发，最终完成或显式转人工。不做无限自主循环。

### 交付

| 交付物 | 实现位置 | 语义边界 |
|---|---|---|
| 持久化 Goal 状态（`ld_goals` + `ld_goal_decisions`） | `ld/goal_store.py` | 确定性 goal_id（project+goal 派生）；状态与 Ledger 验收完全分离；追加式决策历史；跨进程时间盒租约；`resume` 仅限人工恢复 needs_human |
| 有界连续推进 `run_goal()` | `ld/coordinator.py` | 每轮恰好一个可审计转换；决策/任务预算、停滞检测、模型失败全部转显式 needs_human |
| 等待与恢复 | 同上 + CLI `run-goal` | 等待中任务让 Goal 停在 waiting，由 Runtime 或下次调用推进；进程重启后从 Ledger 恢复，不重复提交；`--resume` 人工处理阻塞后显式恢复 |
| 依赖任务确定性交接 | `LDTools.submit_goal_handoff` + `checkpoint_mode=reachable` | 同 Goal 存在 verified 前序时，新任务由控制平面（非模型）升级为 session handoff：merge 前序已验收代码到自己的工作分支，绝不触碰默认分支 |
| provenance 模型视图 | `LDTools.get_task_provenance(allowed_security_domains=…)` | Effective Prompt 与任务原文永不进入模型可见结果（M4B-01 解决） |

### 退出条件

- 一次目标声明驱动两个存在真实代码依赖的任务，由两个独立 Session 完成，
  前序验收代码被后继真实消费（merge），全部通过 Verifier 验收。
- A 完成、B 未派发时重启 LD：恢复同一 Goal，不重派 A，正确继续 B。
- 多项目隔离：A 的知识不注入 B，未授权模型读不到本地敏感内容。
- 连续推进全程不绕过审批、不改写验收、不自动合并默认分支。

### 验证状态

**当前退出判定：核心条件已满足（T37/T39/T40 确定性 + T38b 真实严格闭环 + T38c
真实故障恢复）。**

- T37/T39/T40 确定性验证（无 Worker；T39 含真实子进程 `os._exit` 崩溃注入，
  80 项全 Pass）：`evidence/project/T37-m4-goal-advancement.json`、
  `T39-m4-goal-reliability.json`、`T40-m4-checkpoint-safety.json` ——
  生命周期（submit A → wait → A verified → B 作为 handoff 提交且 prompt 含
  merge 指令 → B verified → finish completed）、needs_human → 人工"继续" →
  finish 的完整闭环（步号连续、报告恰含本 Goal 的 A/B、排除其他 Goal 任务）、
  四个持久化边界崩溃恢复（不重复派工、不调模型）、双进程租约竞争与失租
  fencing、心跳续租、7 种不可证明 checkpoint 状态零副作用阻塞。
- T38b 真实严格闭环（配置的本地 OpenAI 兼容决策端点 + 真实 CodeBuddy Worker，
  25/25 全 Pass）：`evidence/project/T38b-m4-real-e2e-strict.json` —— 一次
  目标声明：A 实现 `load_config`（agent 真实运行测试并 commit，verified）→
  重启的 LD（CLI 子进程）恢复同一 Goal 提交 B —— B 是 handoff WorkItem
  （source=A），agent 真实执行 `git merge`（A 的 checkpoint 是 B HEAD 的祖先、
  B worktree 内全部预置测试通过）、commit、verified → LD 依据两个 verified
  证据 finish，Goal `completed`；最终报告恰含本 Goal 的 A/B 两个任务、历史
  连续、无 pending 步、**Owner 介入次数 0**；main 分支 tip 全程未动；多项目
  隔离全部通过。
- T38c 真实故障恢复与副作用拒绝（13/13 Pass）：
  `evidence/project/T38c-m4-real-recovery.json` —— B 因 Bash 权限被拒卡死后
  人工取消 → LD fail-closed 转人工 → Owner "继续" ×2（含一次控制平面拒绝
  bypassPermissions 的真实拦截）→ 补交任务 B' 真实 merge + 全部测试通过 +
  commit，但因 agent 误在项目根执行 git 命令（真实越界副作用）被 Verifier
  **正确判 rejected**（断言全绿不覆盖副作用）→ 任务预算耗尽，Goal 正确
  fail-closed 到 needs_human。
- T38 首轮真实端到端（16 项 Pass，按其断言集）：
  `evidence/project/T38-m4-real-e2e.json` —— 执行与交接层的首次真实验证。
- 已知限制（`docs/KNOWN_ISSUES.md` T38-F01/F02/F05）：真实决策模型偶发保守
  转人工（`--resume` 解锁，T38b 本轮零介入）；agent job 的 worker 权限模式
  要求项目级 `permissions.allow`（T38 fixture 已内置）；首跑中模型选择
  `git_clean` 断言 + agent 未 commit 导致正确 rejected，已通过目标文本引导
  缓解。

---

## M4-C — 自动唤醒与运行时可靠性收口 ✅ 真实黑盒闭环通过（T43/T44）

### 目标

Human Owner 只提交一次 Goal，系统自动完成
`LD 决策 → 派发 → Worker → Verifier → Project Context → 唤醒 LD → 下一次决策`
直到 `completed`，**不需要人工反复执行 `ld run-goal`，也不需要测试脚本推动 LD**。

### 交付

| 能力 | 实现位置 |
|---|---|
| 确定性 Goal 调度器（不是第二个 Agent） | `ld/goal_runner.py` — `LDGoalRunner` |
| 调度簿记（last_signature / 退避 / 失败与停滞计数） | `ld/goal_runner.py` — `GoalRunnerStore` |
| Runtime 内嵌推进（独立线程，共享 Ledger） | `controlplane/runtime.py` — `goal_runner=…` + `--advance-goals` |
| 独立调度进程（Control Plane 不导入模型客户端时） | `scripts/run_ld_goal_runner.py` |
| Handoff Checkpoint 引用保护 | `controlplane/handoff.py`、`controlplane/plane.py` |
| 模型输出瑕疵 vs 策略违规分离 | `ld/decision.py` — `DecisionPolicyError`；`ld/coordinator.py` |

### 调度语义（本轮核心契约）

| Goal 状态 | Runner 行为 |
|---|---|
| `active` | 无其他执行者时推进 |
| `waiting` + WorkItem 在跑 | 不调用模型（`wait`），只记 skip |
| `waiting` + WorkItem 已验收 | 自动唤醒 LD，进入下一次决策 |
| `waiting` + 任务异常门控 | 交给既有异常门控（`needs_approval` / `suspected_lost` / …）转人工 |
| `needs_human` | 不自动恢复、不调用模型 |
| `completed` | 不再推进 |
| `failed` | 不自动重启 |

- **只在观测状态确实变化时调用模型。** `goal_wake_signature` 由 Goal 行、当前
  WorkItem 的 state/verdict/queue_status 与决策计数共同决定；签名相同即 skip。
  Pending Step 例外：它是未完成的工作，签名里不含它，崩溃不能被当成"没变化"。
  **未完成推进流例外（M4-C 收口）**：凡是留下 Goal 为 `active` 的推进（典型是
  `inspect` 查询决策、单次 Quantum 耗尽正常让出）都**不确认**签名，下一轮
  sweep 继续唤醒 LD，由无进展连击上限有界升级转人工；失租/陈旧失败路径同样
  不写签名（否则他方未处理的失败会被去重吞掉，退避策略失效）。
- **预算分两层。** 单次调度 Quantum（`--goal-max-decisions/--goal-max-tasks`，
  默认 1/1）耗尽即正常让出，绝不停 Goal；Goal 生命周期总预算
  （`ld_goals.max_decisions_total/max_tasks_total`，`--goal-lifetime-*` 指定，
  默认 24/4）首次推进一次性盖章，跨重启持久，`resume` 不重置不扩大，仅人工
  显式 `set_lifetime_budget` 可调；达到总预算进入明确 `needs_human`。
- **失败可查询、有限退避、最终转人工。** 模型传输失败/输出结构不完整按
  `base * 2^(n-1)` 退避（上限 `max_backoff_s`），达到
  `max_model_failures` 后经 `stop_goal_if` **条件升级**为
  `needs_human` 并停止调用：仅当 Goal 仍可推进、决策计数与观察一致且无其他
  存活租约时生效——陈旧 Runner 的失败升级无法覆盖他方已完成/推进中的 Goal。
- **不自动忽略 `request_human`。** 模型自己说转人工，Goal 就停在
  `needs_human`；重复无进展决策也会停滞转人工。
- **进度没有第二份真相。** Runner 只写调度簿记；任务成败一律读 Ledger，
  `ld_goals` 只记目标推进。

### Checkpoint 引用保护（任务 A）

`submit_handoff()` 现在先把 WorkItem 持久化为 `checkpoint_unprotected`（一个
`claim_next_ready` 永不领取的状态），再尝试 `update-ref`。因此
"行已提交、引用未写入"的崩溃窗口不再可派发；只有 CAS
（`promote_deferred_queue`）成功才翻到 `ready`。派发路径 `plane._dispatch_record`
在创建 attempt、抓 baseline、扣并发槽**之前**再复核一次引用，任何不一致
（引用被删、对象不可达、引用指向别的 commit）都退回
`checkpoint_unprotected` 并记录事件。重启后 `reconcile_handoff_checkpoints`
用 WorkItem 自带的 `(task_id, checkpoint_ref, cwd)` 幂等恢复。

### 验证状态

- T41 `evidence/project/T41-handoff-checkpoint-gate.json`：A1–A8 全 Pass ——
  `update-ref` 真实失败、对象不存在、保护前崩溃、重复恢复、引用被删、
  引用指向他处、两进程并发恢复、正常交接不受影响。所有失败场景断言
  `worker_not_started=true` / `no_dispatch_attempt=true` /
  `checkpoint_not_ready=true`。
- T42 `evidence/project/T42-ld-goal-auto-wakeup.json`：B1–B6 全 Pass ——
  零手工调用闭环、Runtime 集成、终态 Goal 不唤醒、失败退避与转人工、
  Pending Step 不调模型对账、模型输出瑕疵可重试但策略违规永久关闭。
- T43 `evidence/project/T43-m4c-autonomous-e2e.json`：**真实模型 + 真实
  CodeBuddy Worker** 黑盒验收。Goal 提交 1 次、手工 `run-goal` 0 次、
  手工提交任务 0 次；1 个 WorkItem verified，worktree 内 `pytest` 3 passed，
  LD 自动 `finish` → `completed`。注意：T43 由测试脚本以
  `run_due(force=True)` 驱动 Runtime 调度，属"脚本驱动"运行模式；
  "独立服务自行调度"模式由 T48 验证。
- T44 `evidence/project/T44-m4c-dual-project.json`：**双项目并行独立运行**。
  两个 Goal 同时在一个 Runtime 上推进，各自 1 个任务、全部 verified、
  worktree 在各自仓库、`pytest` 各自全绿、main 未动；任一项目的 local
  秘密条目既不出现在另一项目的 Effective Prompt，也不出现在其 model view /
  progress 投影里。
- T45 `evidence/project/T45-goal-budget-semantics.json`：预算语义 A1–A6 全
  Pass —— Quantum 耗尽不转人工、总决策/任务预算跨唤醒不可突破、重启不重置、
  `resume` 不隐式增预算（仅显式 `set_lifetime_budget` 可调）、旧版 Ledger
  迁移后行为正确。
- T46 `evidence/project/T46-inspect-continuation.json`：inspect 连续推进
  B1–B8 全 Pass —— inspect→submit、inspect→inspect→submit、inspect→
  request_human、无进展上限、重启续推、查询结果按安全域门控不泄露、
  Worker 运行期不重复调用、Pending Step 优先对账。
- T47 `evidence/project/T47-runner-concurrency-fencing.json`：多 Runner 并发
  C1–C6 全 Pass（每场景两个真实 OS 进程 + 独立 SQLite 连接）—— 并发恰一
  WorkItem、陈旧升级不覆盖 completed（含正向对照）、失租 fenced 写入被拒、
  跨进程重启续推、重复失败仅一次有效升级、单 Goal 失败不阻塞其他 Goal。
- T48 `evidence/project/T48-real-service-autonomous.json`：**真实常驻服务双任务
  自动接续**（`run_controlplane.py --advance-goals` 服务进程 + CodeBuddy HTTP
  服务 + 真实 LLM；测试脚本只创建项目/提交一次 Goal/读 Ledger，从不调用
  `run_due(force=True)` 等外部驱动）。**R1 + R2 全 Pass**：R1 零人工闭环
  （A verified → 自动唤醒 → B 以新 Session handoff 接续 A 的 checkpoint、
  A commit 为 B HEAD 祖先、B worktree 预置测试全绿 → 自动 finish 恰含 A/B →
  `completed`，人工恢复 0 次）；R2 在 A verified 后杀死 Runtime 进程，新服务
  进程自动发现 Waiting Goal 提交 B 并完成（服务重启 1 次、人工恢复 0 次）。
  真实模型为环境配置的 DeepSeek 端点；首轮暴露的模型侧缺陷（分支指令误读、
  断言缺 target）通过明确目标文本 + 校验错误反馈重试修复（见 KNOWN_ISSUES
  M4C-11）。
- T49 `evidence/project/T49-project-failure-isolation.json`：双项目失败隔离
  全 Pass（确定性、真实验证路径：A 的断言经 Verifier 真实判 rejected → LD
  request_human 停摆，B 同时自动完成；全局并发限制、退避隔离、知识隔离、
  报告隔离均成立）。真实 CodeBuddy 失败路径集成验证为已知缺口。

### 明确不做

- 不做无限自主循环（预算、停滞检测、失败转人工仍在）。
- 不做 M5 跨机器执行；`node_id != 'local'` 依旧阻塞。
- 不做第二套自动唤醒实现：Runtime 内嵌与独立进程共用同一个 `LDGoalRunner`。
- 不做 Goal DAG / 多 LD 协商 / Feature-Issue / Dashboard。

---

## M5 — Distributed Workspace

### 目标

Project 与物理 Workspace 完全解耦，支持多个 Execution Node。

### 最小交付

1. 多 Execution Node 注册（`workspaces.node_id` 已有字段，补齐注册与健康）。
2. 远程 Worker 查询与派工（`capabilities.remote_dispatch` 从 `false` 变为真实能力）。
3. 节点失联恢复（沿用既有 `lost` / `suspected_lost` 语义，不新造状态）。
4. 企业安全域下的独立上下文与受控同步：只有 `security_domain='shareable'`
   的条目允许离开本机；`local` 条目在跨节点派发时被明确排除并记录。

### 退出条件

- 任务可以被派发到非本机节点并在该节点产生被验收的产物。
- 断开节点后，任务不会在本机被"顺手"执行（不得静默降级到本地）。
- 跨节点派发时 `local` 条目数量为零（有断言检查注入内容）。
- 节点恢复后状态对账正确（不产生重复 attempt、不丢 attempt）。

### 明确不做

- 不做原生 Session 跨机器迁移。
- 不做多活控制平面（先单 Coordinator + 多 Node）。
- 不引入消息队列；继续用 SQLite + 显式轮询/租约。

---

## 后置（按真实需求再做）

以下内容**明确推迟**，不是"下个 sprint 就做"：

- 第二个 Worker Adapter（无第二家厂商需求前不写）。
- 完整 Dashboard / Web UI。
- 高级 DAG 依赖编排（当前只有优先级 + 串行/并发）。
- 复杂 Memory 检索（向量、图谱、rerank）——M1 的标签/关键词优先级足够。
- 消息队列 / 事件总线。
- OS 级执行隔离（容器/沙箱）。

---

## 跨阶段的固定约束

1. **不引入新基础设施**：继续用 SQLite；每个阶段的新增依赖必须写清理由。
2. **不改已验证行为**：T11、T17–T26 的行为不能在后续阶段被悄悄改变；
   改动必须补测试并保留旧证据。
3. **真实执行与模拟测试分开标注**：任何"通过"都要能指到实际运行的脚本。
4. **不做假的远程能力**：没有真实远程执行路径时，一律拒绝并说明。
5. **知识升级路径单向**：`unverified → candidate → verified`，降级需显式操作并留痕。
6. **审批绑定内容**：任何改变实际执行内容的变更都必须使旧审批失效。
