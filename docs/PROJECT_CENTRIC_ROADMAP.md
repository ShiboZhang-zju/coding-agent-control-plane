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
| **M3** | Session Registry / Handoff | M1（M2 有益但非必需） | **功能已实现，未验证** |
| **M4-A** | Minimal LD（受限工具、结构化决策、单轮协调） | M1 + M2；不开放 M3 Handoff | **首轮确定性边界测试通过，未完成验收** |
| M4-B | LD 多轮推进与验收后 Handoff | M3 验收 + M4-A | 未实现 |
| M5 | Distributed Workspace | M1–M4 | 未实现 |

依赖是**功能依赖**而非排期依赖：M4-A 只调用 M1/M2 的确定性查询与
普通任务提交，因此可在 M3 验收前做功能开发；Handoff 不向 LD 开放。
M4-B 启用 Handoff 前必须先完成 M3 验收。M5 之前不得宣称支持跨机器协同。

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

## M3 — Session Registry / Handoff ⚠️ 功能已实现，未验证

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
| Snapshot 观测时登记 Session，不再写 enqueue 空占位 | `ControlPlane._save_snapshot()` | 已实现，未运行验证 |
| `node / worker_instance / session / task / attempt` 完整链 | `TaskStore.observe_session_reference()` | 已实现，未运行验证 |
| Attempt 级唯一绑定 + 异 Session 冲突显式转人工 | `session_references` 部分唯一索引 + Store/Plane | 已实现，未运行验证 |
| 只读 Handoff Prepare（来源/Session/Workspace/checkpoint/context） | `controlplane/handoff.py` | 已实现，未运行验证 |
| Handoff Submit 创建普通 WorkItem，来源与 checkpoint 原子落 metadata | `ProjectService.submit_handoff()` | 已实现，未运行验证 |
| 提交前重检、冻结 Prompt/context/workspace、内容签名幂等 | 同上 + `ControlPlane.enqueue()` | 已实现，未运行验证 |
| CLI `project handoff-prepare / handoff-submit / sessions` | `scripts/controlplane_cli.py` | 已实现，未运行验证 |

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

**当前退出判定：未满足。** 按本轮要求没有启动 CodeBuddy，也没有执行
stub/SQLite 验证，因此不存在新的 evidence 或 Pass 结论。尤其以下仍为
**未验证**：

- 真实 CodeBuddy Snapshot 的 Session ID 是否在所有路径稳定可用；
- 真实 Worker worktree 关闭后 checkpoint 的保留与可达性；
- Session A 关闭后，Session B 不读旧 transcript 完成并通过 verdict 验收；
- 新 schema 迁移、并发 Session 冲突、Handoff 幂等的实际回归。

完整待验证问题、影响与复现方法见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

### 明确不做

- 不承诺原生 Session 可跨机器直接迁移。
- 不在本阶段做自动上下文摘要压缩（超预算时先按 M1 的预算规则截断）。

---

## M4-A — Minimal LD ⚠️ 首轮确定性边界测试通过，未完成验收

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
`evidence/project/T32-m4a-deterministic.json`。

**当前退出判定：M4-A 专项确定性条件已由 T32 覆盖，但集成验收仍未满足。**
尚未运行 M1/M2 完整回归或真实模型/Worker 集成；T32 Pass 不等于 M4 MVP 验收。

### 明确不做

- 不做多 LD 协同。
- 不做自动审批。
- 不做跨项目资源抢占策略（先 FCFS + 优先级）。
- 不做无限自主循环、复杂 Goal DAG 或引入 Agent Framework。
- M3 验收前不向 LD 开放 Handoff。

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
