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
| **M1** | Project Context Foundation | v0.2 已完成 | **已完成（本轮）** |
| M2 | Project Context Update Loop | M1 | 未开始 |
| M3 | Session Registry / Handoff | M1（M2 有益但非必需） | 未开始 |
| M4 | LD MVP | M1 + M2 + M3 | 未开始 |
| M5 | Distributed Workspace | M1–M4 | 未开始 |

依赖是**功能依赖**而非排期依赖：M3 可以在 M2 之前做，但 M4 必须等三者都有。
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

## M2 — Project Context Update Loop

### 目标

让项目知识在执行后**自动**长出来，同时不让 Agent 的猜测污染事实。

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

### 退出条件

- 同一任务重复验收 N 次，`progress` 条目数量恒为 1 且版本连续。
- 自动生成的条目 100% 为 `candidate`；断言"不存在自动产生的 `verified` 条目"。
- 并发回写同一条目只有一个成功，其余进入 `needs_merge`，无丢数据。
- 从证据提取的条目全部带非空 `evidence_refs`，且路径在 `evidence/` 下。

### 明确不做

- 不做 LLM 总结式回写（第一版用确定性规则）。
- 不允许自动修改 `constraints`。
- 不做跨项目知识提升（A 的结论不会自动变成 B 的结论）。

---

## M3 — Session Registry / Handoff

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

### 退出条件

- 关闭 Session A（进程退出）后，Session B 从 Project Context 能回答：
  项目目标、当前进度、上次结论、下一步。
- 交接验证任务在"无旧会话"条件下通过验收（`verified`）。
- SessionReference 中不存在指向已删除项目/工作区的悬挂记录（或悬挂被显式标记）。

### 明确不做

- 不承诺原生 Session 可跨机器直接迁移。
- 不在本阶段做自动上下文摘要压缩（超预算时先按 M1 的预算规则截断）。

---

## M4 — LD MVP

### 目标

唯一的全局 LD 接上 M1 的确定性工具，真正开始"管理项目"。

### 最小交付

1. 唯一全局 LD（单实例，无多主竞争）。
2. 查询各项目进度：`ProjectService.list_projects / get_project_brief / get_project_progress`。
3. 按目标提交项目任务：`submit_project_task()`。
4. 按验收结果推进下一步：只依据 `verdict`，不依据 Worker 自述。
5. 异常/审批/阻塞 → 转人工决策，不自行绕过（不得自动 `approve`）。

### 退出条件

- 给定 3 个项目的目标，LD 能在无人干预下完成"查询 → 派发 → 依据验收推进"至少一轮。
- LD 的每个决策都能追溯到具体 Ledger 行或 Context 条目（无"模型说这样"的决策）。
- LD 遇到 `needs_approval` / `dispatch_unknown` / `suspected_lost` 时**停止并请求人工**，
  测试需验证它不会绕过。
- LD 不写入 `verified` 条目、不修改 `constraints`。

### 明确不做

- 不做多 LD 协同。
- 不做自动审批。
- 不做跨项目资源抢占策略（先 FCFS + 优先级）。

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
