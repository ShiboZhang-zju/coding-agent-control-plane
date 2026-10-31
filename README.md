# coding-agent-control-plane

**一个正在演进为 Project-Centric LD System 的 Coding Agent 控制平面。**

最终目标是：一个 Human Owner 管理一个全局 **LD Agent**，由 LD 协调多个拥有独立上下文的
**Project**，每个 Project 通过 **Coding Agent** 执行具体任务。Control Plane 是这个系统的
**基础设施层**——它不再是产品本身，而是让上述结构可靠运转的执行治理层。

当前阶段：**M4 核心闭环已成立（M4-A 单轮与 M4-B 连续推进均通过真实集成验收）**。
M1–M3 均有真实测试证据；M4-A 跑通"真实 LLM 结构化决策 → context 注入 →
真实 CodeBuddy Worker → Verifier 验收 → M2 自动上下文更新"完整单轮链路
（`evidence/project/T38a-m4a-real-single-turn.json`）；M4-B 严格闭环
（`T38b-m4-real-e2e-strict.json`，25/25 Pass）：一次目标声明 → A 实现
`load_config` 并真实运行测试与 commit（verified）→ 重启的 LD 恢复同一 Goal 提交
B（handoff，agent 真实 `git merge` A 的已验收 checkpoint，全部测试通过，verified）
→ LD 依据两个 verified 证据 finish，Goal `completed`，全程 **Owner 介入 0 次**；
故障恢复与副作用拒绝路径另由 T38c 真实验证（含 Verifier 在断言全绿时正确拒绝
有越界副作用的任务）。可靠性收口（步号冲突、崩溃窗口、租约 fencing、
checkpoint 安全、安全域默认值）由 T39/T40 确定性验证（含真实子进程崩溃注入）。
模型身份（端点/模型 id）不属于测试结果，不出现在代码默认值、日志或证据中。
已知限制：真实决策模型偶发保守转人工（`--resume` 解锁）；agent job 的 worker
权限要求项目级 `permissions.allow`（T38 fixture 已内置，见
`docs/KNOWN_ISSUES.md` T38-F05）；M5 跨机器远程执行尚未实现，
见 [路线图](docs/PROJECT_CENTRIC_ROADMAP.md)。

---

## 1. 为什么需要它

同时推进多个项目时，今天的做法通常是——人盯着若干个 Agent Session。这有几个结构性问题：

- **上下文随 Session 死亡。** Session 结束（进程退出、重启、换机器），历史对话就没了。
  新 Session 不会记得上次已经查清楚的事实，于是重新探索一遍已知结论。
- **知识无法复用。** A 项目踩过的坑，B 项目不知道；同一个项目第二次做同类任务，
  仍然要从零开始。
- **人是瓶颈。** 人必须同时扮演"项目经理"和"上下文搬运工"：记住每个项目到哪了、
  告诉每个新 Session 该注意什么。
- **项目不是一个目录。** 一个逻辑项目可能跨多个仓库、在开发机和测试机分别有工作区、
  使用多个 Session。把它绑死在一个本机 Git 路径上，架构就提前封顶了。
- **LD 不该持有全部知识。** 一个全局管理者 Agent 如果要把所有项目背景都塞进自己的
  上下文，既昂贵又不可靠，而且一旦它出错，所有项目一起出错。

因此本项目做三件事：
1. **Control Plane**（已实现）：把派发、幂等、审批、验收、恢复变成厂商无关的确定性能力。
2. **Project Context / Handoff**（M1–M3 功能已实现）：把项目知识变成独立于 Session 的持久实体，
   在任务执行前注入，并在验收后以确定性规则回写候选进度与知识。
3. **Minimal LD**（M4-A 功能已实现、确定性边界测试部分通过）：用受限工具面和结构化决策做一次
   查询/提交/等待/转人工/完成判定，不做无限自主循环。

---

## 2. 目标架构

```text
Human Owner（项目老板：目标 / 优先级 / 关键审批）
    │
    ▼
LD Agent（唯一的全局管理 Agent：查询、协调、拆解、分发、处理阻塞）
    │
    ├── Project A ──────────────┐
    │   ├── ProjectContext      │   背景 / 架构 / 硬约束 / 进度 / 知识 / 决策
    │   ├── Progress (derived)  │   由 Ledger 派生，不是第二份真相
    │   ├── WorkItems           │   具体任务
    │   ├── SessionReferences   │   project ↔ workspace ↔ session ↔ task ↔ attempt
    │   └── Workspace(s)        │   node_id + local_root + capabilities
    │
    ├── Project B ── (同上)
    └── Project C ── (同上)

Control Plane（公共执行治理层，与厂商无关）
    ├── Scheduler            队列 / 优先级 / 并发 / 原子 claim
    ├── Approval / Policy    风险只升不降 + 内容绑定 digest + 一次性预留
    ├── Reconciler           refresh_known_jobs / discover_orphan_jobs
    ├── Verifier             产物断言 + baseline 归属 + 跨项目副作用检测
    ├── SQLite Ledger        唯一持久化真相
    └── Worker Adapter
         └── CodeBuddy / future workers
```

**关键澄清：Project Context 不是一个额外的常驻 Agent。**
它只是一张可版本化、可追溯的表。没有"每个项目一个 LLM 在后台跑"这种东西。
项目 A 的连续性来自 **Context + Ledger**，不来自"某个 Session ID 恰好还活着"。

---

## 3. 核心设计原则

| 原则 | 具体含义 |
|---|---|
| **Project-centric** | 任务是项目的任务。项目是第一类实体，Session 只是执行载体。 |
| **Durable project context** | 背景/约束/知识/决策持久化在 SQLite，重启后仍在，不依赖会话内存。 |
| **Context injection before execution** | 派发前由 `ContextBuilder` 生成 Effective Prompt；Coding Agent 一上岗就带着项目知识。 |
| **Evidence-based progress** | `get_project_progress()` 从 Ledger 的任务与验收状态派生，不靠 LLM 总结判断成败。 |
| **Session-independent continuity** | 项目状态持久在 Context + Ledger；SessionReference 只记录关系，不承载真相。真实换 Session 接续待 M3 验证。 |
| **Separation of decision and execution** | LD 决定"做什么/优先级"，Control Plane 决定"怎么可靠地做/是否算做成功"。 |
| **Structured LD decisions** | 模型只能返回五种结构化动作；未知字段与 Python/Shell 控制命令在工具调用前被拒绝。 |
| **Security-domain awareness** | 条目区分 `local` / `shareable`；模型必须显式获得安全域授权，未授权内容不进入决策输入。 |
| **Freeze what you dispatch** | 入队即冻结 Effective Prompt；之后改上下文不会静默改写已排队任务。 |
| **Fail-closed on prerequisites** | 缺前置条件 → 返回缺口并阻塞，不编造、不猜测、不带病派发。 |

---

## 4. 当前已实现能力

已验证项只引用真实 `evidence/**/*.json`；M3 仍统一标为“功能已实现、未验证”；
M4-A 只把 T32 实际覆盖的确定性边界标为 Pass，整体仍未完成验收。

### 4.1 Control Plane（PoC / v0.2，已验证）

| 能力 | 状态 | 证据 |
|---|---|---|
| CodeBuddy Adapter（真实 Worker） | 已实现 | `evidence/sse/`、`evidence/jobs/` |
| 4 路真实 Agent 并发 | Pass（4/4 succeeded，4 个独立 worktree，全部 verified） | `evidence/isolation/T11-4way-ledger.json` |
| SQLite Ledger（唯一真相，含 fail-closed） | 已实现 | `evidence/recovery/T19-ledger-fail-closed.json` |
| 派发幂等（同 key 至多一个 job） | 已实现 | `evidence/jobs/T12*`、`T17-concurrent-idempotency.json` |
| 人工审批门控（内容 digest 绑定 + 一次性预留） | 已实现 | `evidence/permissions/T20*`、`T21-approval-security.json` |
| Runtime 常驻循环 + 持久队列 | 已实现 | `evidence/recovery/T22-runtime-loop.json`、`T23-runtime-queue.json` |
| 多进程原子 claim | 已实现 | `T24-multiprocess-claim.json` |
| 基础产物验收（断言 + baseline + 副作用） | 已实现 | `evidence/isolation/T14*` |
| 项目注册与目录边界校验 | 已实现 | `evidence/v02*`、`T14*` |
| T26 恢复修复（降级跨重启、lost 统一判定、claim 租约、attempt 终态） | 已实现 | `evidence/recovery/T26-p0-fixes.json` |

### 4.2 Project-Centric（M1–M3）

| 能力 | 状态 | 证据 |
|---|---|---|
| Project Context Store（6 类知识、项目隔离、版本 CAS、来源与证据） | 已实现并测试 | `evidence/project/T27-project-context.json` |
| Workspace 表 + `projects.root` 自动映射为默认本地 Workspace | 已实现并测试 | `T27`、`T28` |
| SessionReference（node↔worker↔session↔task↔attempt） | 已改为真实 Snapshot 观测登记；未验证 | 无新证据 |
| Context Builder（有限预算、相关性筛选、硬约束完整注入） | 已实现并测试 | `T27`、`T28` |
| Effective Prompt 冻结 + 可追溯（original_task / context_version / entry_refs） | 已实现并测试 | `T28`、`T30` |
| 审批绑定执行内容（上下文版本或工作区变化即失效） | 已实现并测试 | `T28` |
| 项目级查询 Facade（brief / progress / tasks / provenance / workspaces） | 已实现并测试 | `T30-project-cli.json` |
| PID 三态判断（True / False / None，None 不判 lost） | 已实现并测试 | `T29-reliability-edges.json` |
| 降级标记 I/O 诚实化（写失败不再宣称跨重启保护） | 已实现并测试 | `T29-reliability-edges.json` |
| 旧流程兼容（无上下文的 enqueue/dispatch 行为与 digest 不变） | 已验证 | `T28` case E |
| 终态验收后自动生成 `progress` / `knowledge` 候选条目 | 已实现并测试 | `evidence/project/T31-context-update-loop.json` |
| 回写幂等回执（task + attempt + kind + title） | 已实现并测试 | `T31` case B |
| CAS 冲突保留双方并标记 `needs_merge` | 已实现并测试 | `T31` case C |
| 自动写入安全边界（仅 candidate、禁止 constraints、禁止跨项目） | 已实现并测试 | `T31` case D |
| verdict/回执间崩溃窗口恢复 | 已实现并测试 | `T31` case E |
| Handoff 只读准备（来源/Session/checkpoint/Workspace/context） | 已实现，未验证 | 无新证据 |
| Handoff 提交（普通 WorkItem + 原子来源 metadata + 签名幂等） | 已实现，未验证 | 无新证据 |

### 4.3 Minimal LD（M4-A / M4-B 已验收；M4-C 自动唤醒已真实闭环）

| 能力 | 实现位置 | 状态 |
|---|---|---|
| 受限 LD Tool Facade（7 个工具）+ 提交请求签名 | `ld/tools.py` | T32 Pass |
| 五种结构化动作 + 严格字段/参数校验 | `ld/decision.py` | T32 Pass |
| 单轮 `run_once(goal, project_id=None, dry_run=False)` | `ld/coordinator.py` | T32 Pass |
| 项目作用域、转人工门控 + 当前目标 `finish` 二次核验 | `ld/coordinator.py` | T32 Pass（本地确定性） |
| 真实决策模型（OpenAI-compatible，无 SDK） | `ld/model.py` | T36 边界全 Pass；T38a 真实单轮 Pass |
| provenance 模型视图（M4B-01：Prompt/原文不进模型输入） | `ld/tools.py` | T36 Pass |
| 持久化 Goal 状态（确定性 goal_id、追加式决策历史、跨进程租约 + fencing token） | `ld/goal_store.py` | T37/T39 Pass |
| 有界连续推进 `run_goal()` + 依赖任务确定性 handoff 交接 | `ld/coordinator.py` | T37/T39 确定性 Pass；T38b 真实严格闭环 25/25 Pass（completed，Owner 介入 0 次）；T38c 真实故障恢复/副作用拒绝 Pass |
| 自动唤醒调度器 `LDGoalRunner`（状态变化才调模型、有限退避、失败转人工） | `ld/goal_runner.py` | T42 确定性 Pass（B1–B6） |
| Runtime 内嵌 Goal 推进（`--advance-goals`，独立线程 + 同一 Ledger） | `controlplane/runtime.py`、`scripts/run_controlplane.py` | T42 B2 Pass；T43 真实闭环 Pass；T48 真实常驻服务双任务（含重启恢复） |
| 独立 Goal Scheduler 进程（Control Plane 不导入模型客户端时） | `scripts/run_ld_goal_runner.py` | T42/T43 同源实现 |
| Handoff Checkpoint 引用保护（排队期阻断 + 派发前复核 + 重启恢复） | `controlplane/handoff.py`、`controlplane/plane.py` | T41 A1–A8 Pass |
| 双项目并行独立运行 | `controlplane/plane.py`（项目作用域） | T44 Pass |
| Goal 预算：单次 Quantum 与持久化 Lifetime 总预算分离（resume 不增预算，仅人工 `set_lifetime_budget` 可调） | `ld/goal_store.py`、`ld/coordinator.py` | T45 Pass（含旧库迁移/重启/恢复语义） |
| inspect 连续推进（查询结果持久化 + 未完成流不确认签名 + 安全域门控） | `ld/coordinator.py`、`ld/goal_runner.py` | T46 Pass |
| 多 Runner 并发 / 租约丢失 / fenced 失败升级（两真实进程 + 独立连接） | `ld/goal_store.py::stop_goal_if`、`ld/goal_runner.py` | T47 Pass（C1–C6） |
| 双项目失败隔离（A 拒绝转人工不阻塞 B） | `ld/goal_runner.py`（按 Goal 独立簿记） | T49 Pass（确定性，真实验证路径） |
| 同 Ledger CLI（查询/单轮/run-goal/--resume） | `scripts/ld_cli.py` | T32/T38 Pass |

M4 未引入任何 Agent Framework。模型输出必须通过严格 schema 校验，模型永远
不直接接触 Worker、SQLite、文件系统或 Shell；所有派工经
`LDTools → ProjectService → ControlPlane`，不绕过审批。Human Owner 目标默认按
`local` 域处理，模型未获得 `local` 授权时协调器不会调用它。已知限制：真实
模型在收尾阶段倾向保守转人工（T38-F01），由 `run-goal --resume` 显式解锁。

**M4-C：一次提交目标，自动推进到完成。** Human Owner 只声明一次 Goal
（`LDGoalStore.get_or_create`），之后由 Runtime 的 Goal 推进路径自动完成
"派发 → Worker → Verifier → Project Context → 唤醒 LD → 下一次决策"。
它不是第二个 Agent：`LDGoalRunner` 是确定性调度组件，只在观测状态**确实变化**、
存在可恢复的 Pending Step、或上一决策留下未完成推进流（如 inspect 之后）时才
调用 `LDCoordinator.run_goal()`，否则只记 skip。终态 Goal
（`needs_human` / `completed` / `failed`）永不被自动唤醒；模型连续失败按
指数退避重试，超过阈值经条件升级（fenced，陈旧观察不覆盖他方状态）转人工并
停止调用；崩溃遗留的 Pending Step 先从 Ledger 对账（不调模型）再继续。
验收：T43 黑盒（单任务真实闭环）；T48 真实常驻服务双任务——Goal 提交 1 次、
`run_due(force=True)` 等外部驱动 0 次、A verified → 自动唤醒 → B 以 handoff
接续 A 的 checkpoint（新 Session、HEAD 祖先可证）→ B verified → LD 自动
`finish`（`completed`），外加服务进程重启后自动发现 Waiting Goal 续推。

---

## 5. 当前未实现能力

**明确未实现，不要假设存在：**

- **无限自主的完整 LD Agent。** M4-B/M4-C 是有界推进（决策/任务预算、停滞检测、
  失败退避与转人工），不是无人值守的自主循环；真实模型收尾阶段仍可能转人工。
- **M5 跨机器执行 / 分布式 Runtime。** 本轮明确不做；`node_id != 'local'` 仍然阻塞。
- **可靠跨机器远程执行。** `workspaces.node_id` 字段已存在，但非 `local` 节点会被
  `ContextBuilder` 判为阻塞缺口并拒绝派发。不存在"看起来能远程、实际本地跑"的假实现。
- **跨机器原生 Session 迁移。** 未实现，也不承诺。
- **第二 Worker Adapter。** 只有 CodeBuddy。
- **OS 级执行隔离。** `worktree` 只是 Git 工作树隔离，不是文件系统/内核级沙箱。
- **HTTP / API 层的项目接口。** 本轮只有 CLI 与 Python API。
- **矢量检索 / 知识图谱 / Agent Memory 框架。** 明确不做，见路线图。

---

## 6. Quick Start

前置：Python 3.11+（仅标准库）、Git、本机已登录的 CodeBuddy CLI（仅真实派发需要）。

### 6.1 启动 Worker 与 Runtime

```powershell
# 仅监听回环 + 密码认证的 CodeBuddy HTTP 服务
python scripts/serve_ctl.py start
python scripts/serve_ctl.py status

# 一次性沙箱仓库（测试用，可随时重建）
python scripts/make_sandbox.py

# 常驻 Runtime（另一个终端）
python scripts/run_controlplane.py

# 结束时停服务，不留后台进程
python scripts/serve_ctl.py stop
```

**M4-C 自动推进目标（推荐部署形态）：** 加 `--advance-goals` 即可。Human
Owner 之后只需提交一次 Goal，不需要再反复执行 `ld run-goal`。

```powershell
$env:LD_MODEL_BASE_URL = "https://your-endpoint/v1/chat/completions"
$env:LD_MODEL_NAME     = "your-model"
$env:LD_MODEL_API_KEY  = "..."            # 端点需要时
python scripts/run_controlplane.py --advance-goals --model-domains local,shareable `
    --goal-interval 5 --goal-max-decisions 1 --goal-max-tasks 1 `
    --goal-max-model-failures 3 --goal-base-backoff 5 --goal-max-backoff 300 `
    --goal-lifetime-decisions 24 --goal-lifetime-tasks 4
```

这是**真正常驻的服务进程**：`run_forever()` 自带独立的 goal 推进线程，
派发、观测与 LD 推进互不阻塞；测试脚本 `t43/t48` 之外没有任何路径需要
`run_due(force=True)` 之类的外部驱动。

如果 Control Plane 所在机器不应导入任何模型客户端，可改由独立进程推进（同一
Ledger、同一 `LDGoalRunner`，只有一套实现）：

```powershell
python scripts/run_ld_goal_runner.py --interval 5 --model-domains local,shareable
python scripts/run_ld_goal_runner.py --once       # 一次 sweep，便于排障
python scripts/run_ld_goal_runner.py --status     # 每个 Goal 的调度视图
```

**预算语义（M4-C 可靠性收口）：** 单次调度预算（Quantum）与 Goal 生命周期
总预算（Lifetime）是两个独立概念：

- Quantum：`--goal-max-decisions` / `--goal-max-tasks`（默认 1/1），每次唤醒
  最多执行 N 次决策/提交；耗尽即正常让出执行权，**绝不**视为 Goal 失败。
- Lifetime：`--goal-lifetime-decisions` / `--goal-lifetime-tasks`（默认
  24/4），整个 Goal 累计的模型决策数与 WorkItem 提交数上限，持久化在
  `ld_goals.max_decisions_total` / `max_tasks_total`，首次推进时一次性盖章；
  反复唤醒、进程重启、人工 `--resume` 都**不会**重置或扩大它，只有人工显式
  调用 `LDGoalStore.set_lifetime_budget` 才能调整。达到总预算进入明确的
  `needs_human`（stop reason `lifetime decision budget exhausted` /
  `goal task budget exhausted`）。

**inspect 连续推进：** 查询型决策（`inspect`）不会终结 Goal 的推进流——凡是
留下 Goal 为 `active` 的推进都不确认 wake signature，下一轮 sweep 会继续唤醒
LD，直到它提交任务、等待验收或 finish；检查结果以有界快照持久化在步 outcome，
下一次决策可通过 `goal_state.last_step.inspection_result` 使用（仅对授权
`local` 域的模型可见）。连续无进展的查询由无进展连击上限（默认 3）有界升级转
人工。

调度语义（`ld/goal_runner.py`）：只有观测状态**确实变化**、存在可恢复的
Pending Step、或上一决策留下未完成推进流时才调用模型；`needs_human` /
`completed` / `failed` 永不自动唤醒；模型连续失败按指数退避（上限
`--goal-max-backoff`），超过 `--goal-max-model-failures` 转人工并停止调用。

**多 Runner 并发安全：** Goal 租约携带单调递增 fencing token，所有目标状态
写入都在事务内校验；失败升级走条件更新（`stop_goal_if`）——仅当 Goal 仍可推
进、决策计数与观察一致且无其他存活租约时才落 `needs_human`，陈旧 Runner 无法
覆盖他方已完成/推进中的状态；租约被夺走的执行者其 fenced 写入会被
`GoalLeaseLost` 拒绝；失租/陈旧路径不写入调度签名，不会让"未处理的失败"被
去重误判为"观测无变化"。

**查询 Goal 状态与人工恢复：**

```powershell
python scripts/run_ld_goal_runner.py --ledger <ledger> --status
# goal 状态、累计决策/任务数、lifetime 预算、失败退避与上次错误都在输出里

# needs_human 的 Goal 由人工处理后显式恢复（记录为 human_resume 决策；
# resume 不会增加任何预算）
python scripts/ld_cli.py --ledger <ledger> run-goal "<目标文本>" `
    --project <project-id> --resume
```

M5（跨机器执行）仍未实现：`workspaces.node_id != 'local'` 一律判为阻塞/不支
持，绝不在本机静默执行。

### 6.2 最小可运行的 Project Context 示例

以下命令与 `scripts/t30_project_cli.py` 完全一致，并且该脚本会实际执行它们
（`evidence/project/T30-project-cli.json` 就是它的输出）。

```powershell
$L = ".poc-state\demo.db"

# 1) 注册项目（自动创建默认本地 Workspace）
python scripts/controlplane_cli.py --ledger $L project add demo .\sandbox\repo-a `
  --mode agent --isolation worktree

# 2) 添加项目背景 / 硬约束 / 候选知识
python scripts/controlplane_cli.py --ledger $L project context-add <project-id> `
  --kind background --title purpose `
  --body "Sandbox repository used by the control-plane tests." `
  --verification verified --source human:owner

python scripts/controlplane_cli.py --ledger $L project context-add <project-id> `
  --kind constraints --title "no network" `
  --body "Do not add network calls to this repository." `
  --verification verified --source human:owner --tag safety

python scripts/controlplane_cli.py --ledger $L project context-add <project-id> `
  --kind knowledge --title "test command" `
  --body "Run ``python -m pytest`` before claiming success." `
  --verification candidate --source agent:session-1

# 3) 先预览将被注入的 Effective Prompt（不入队）
python scripts/controlplane_cli.py --ledger $L project submit <project-id> `
  "add a health endpoint" --preview

# 4) 提交任务：构建上下文 → 冻结 Prompt → 入队
python scripts/controlplane_cli.py --ledger $L project submit <project-id> `
  "add a health endpoint" --title "health endpoint" `
  --idempotency-key health-1 --tag safety

# 5) 查看这个任务最终用了什么 Prompt、哪个上下文版本、哪些条目
python scripts/controlplane_cli.py --ledger $L provenance <task-id>

# 6) LD 视角：项目摘要与派生进度
python scripts/controlplane_cli.py --ledger $L project brief <project-id>
python scripts/controlplane_cli.py --ledger $L project progress <project-id>

# 7) M2 自动回写通常由 Runtime 在终态验收后触发；也可安全重放并查看回执
python scripts/controlplane_cli.py --ledger $L project context-sync-task <project-id> <task-id>
python scripts/controlplane_cli.py --ledger $L project context-update-log <project-id> <task-id>

# 8) M3：先做只读交接检查（不入队、不调 Worker）
python scripts/controlplane_cli.py --ledger $L project handoff-prepare `
  <project-id> <source-task-id> "continue the implementation"

# 9) 建议携带 prepare 返回的签名，防止准备后上下文/checkpoint 漂移
python scripts/controlplane_cli.py --ledger $L project handoff-submit `
  <project-id> <source-task-id> "continue the implementation" `
  --expected-signature <prepared-handoff-signature> `
  --idempotency-key handoff-1 --assert-file <expected-artifact>
```

M3 命令只是当前功能接口示例，本轮未运行；它们不构成真实
CodeBuddy 双 Session 验收证据。

`--preview` 的输出形如（节选，真实输出来自 T30）：

```text
# TASK
add a health endpoint

# PROJECT BACKGROUND
- purpose: Sandbox repository used by the control-plane tests.

# HARD CONSTRAINTS (must be obeyed)
- no network: Do not add network calls to this repository.

# VERIFIED KNOWLEDGE
- test command [CANDIDATE]: Run `python -m pytest` before claiming success.

# RULES
- Obey every HARD CONSTRAINT above; if one conflicts with the task, stop and report the conflict instead of working around it.
- Entries marked [CANDIDATE] or [UNVERIFIED] are unconfirmed: verify them before relying on them.
- Do not invent project facts that are not stated above.
```

### 6.3 LD：真实模型单轮决策与有界连续目标（M4-A / M4-B）

```powershell
# 只读查询，与 Runtime 使用同一 Ledger
python scripts/ld_cli.py --ledger $L list-projects
python scripts/ld_cli.py --ledger $L brief <project-id>

# 没有模型/决策注入时明确返回 model_not_configured
python scripts/ld_cli.py --ledger $L preview "继续实现当前项目" --project <project-id>

# 使用本地 JSON 结构化决策做 dry-run（不入队）
python scripts/ld_cli.py --ledger $L preview "实现健康检查" `
  --project <project-id> --decision-json .\decision.json

# 接入真实模型（OpenAI-compatible /chat/completions；无 SDK 依赖）
$env:LD_MODEL_BASE_URL = "https://your-endpoint/v1"
$env:LD_MODEL_NAME     = "your-model"
$env:LD_MODEL_API_KEY  = "sk-..."   # 本地免鉴权端点可省略
$env:LD_MODEL_TIMEOUT_S = "180"

# 真实模型单轮：结构化决策 -> context 注入 -> Runtime 派发 -> 验收
python scripts/ld_cli.py --ledger $L run-once "实现健康检查" --project <project-id>

# M4-B 有界连续目标：一次目标声明，LD 依次查询/提交/等待/交接/完成，
# 遇到等待中的任务即返回 waiting；由 Runtime 或下次调用继续推进
python scripts/ld_cli.py --ledger $L run-goal `
  "实现功能 A，再在其上实现功能 B，两部分独立提交" `
  --project <project-id> --max-decisions 10 --max-tasks 3

# 模型把目标停在 needs_human（预算耗尽/模型转人工/停滞）后，
# 人工处理完阻塞可用 --resume 显式恢复（记录 human_resume 决策）
python scripts/ld_cli.py --ledger $L run-goal "……" --project <project-id> --resume
```

真实模型接入的安全边界：

- 端点、凭据、超时全部显式配置（参数或 `LD_MODEL_*` 环境变量）；
  未配置时 `model_not_configured`，绝不伪造决策。
- 模型输出必须通过 `ld/decision.py` 的严格 schema 校验；非法动作、未知字段、
  命令参数一律拒绝并转人工（fail-closed）。
- 模型只能看到授权安全域内的项目内容；`get_task_provenance` 的模型视图不含
  Effective Prompt 与任务原文；项目路径与未授权本地知识永不进入模型输入。
- 日志与 `describe()` 只记录 host/model/耗时；密钥与完整输入输出永不落盘。
- `run_goal` 每轮恰好一个可审计转换（决策→动作→持久化→状态迁移），
  有决策/任务预算与停滞检测；依赖任务由控制平面确定性升级为
  reachable-checkpoint 交接（Session B merge 前序已验收代码，绝不触碰默认分支）。

`decision.json` 的最小提交决策：

```json
{
  "action": "submit_task",
  "project_id": "prj-example",
  "reason": "项目状态可执行，提交一个可验收的普通任务",
  "evidence_refs": ["ledger:project:prj-example:progress"],
  "parameters": {
    "task": "实现健康检查并保留现有行为",
    "idempotency_key": "ld-health-v1",
    "assertions": [
      {"kind": "file_exists", "target": "src/health.py"}
    ]
  }
}
```

LD 只接受 `inspect` / `submit_task` / `wait` / `request_human` / `finish`。
完整 Schema 由 `ld.decision.decision_schema()` 生成；未知字段会被拒绝。

### 6.3b M4-D 对话入口（Chat Quick Start）

`ld chat` 是**交互与意图路由层**，不是第二个 LD：它只读 Ledger / Goal /
Project Context，写操作只有两个（提交 Goal、人工确认后恢复 Goal），且都走
既有机制。没有模型端点时用确定性关键词路由，答案仍全部由 Ledger 渲染。

```powershell
$L = ".poc-state/ledger.db"

# 开一个新会话（一次性）
python scripts/ld_cli.py --ledger $L chat --new "现在有哪些项目？"

# 指定会话继续（推荐：多终端/多人时不要猜）
python scripts/ld_cli.py --ledger $L chat --conversation <conv-id> "t50 有哪些阻塞？"

# 单操作员图省事：继续最近更新的那个会话
python scripts/ld_cli.py --ledger $L chat --continue-latest "t50 项目进行到哪了？"

# 交互式（提示符 ld>；exit 退出）
python scripts/ld_cli.py --ledger $L chat --new

# 看看有哪些会话 / 某个会话说了什么
python scripts/ld_cli.py --ledger $L conversations
python scripts/ld_cli.py --ledger $L conversation <conv-id>
```

四类对话：

```text
查询      "现在有哪些项目？" / "t50 进行到哪了？" / "有哪些阻塞？"
知识      "我们为什么用 load_config 读取配置？"
提交目标  "继续推进 t50 项目：把包测试补齐"   # 冒号后是目标本身
恢复      "恢复 t50 那个停下来的目标" -> 再回 "确认"
```

安全语义（都不是"尽力而为"，有测试守着）：

- **疑问句不会变成写操作。** "这个项目的任务完成了吗？" 只读状态；即使模型
  判断成 `submit_goal`，确定性路由器不认可时会先暂存并要求 `confirm`。
- **确认绑定到"那一次暂停"。** 恢复暂存时记录 `decision_count`，期间被别人
  推进/重新暂停则拒绝，需重新暂存；并发确认最多生效一次。
- **聊天不能改预算。** `submit_goal` 不接受 `max_decisions/max_tasks`（属于
  策略违规）；预算只能由 runner 首次盖章或人工管理命令调整。
- **模型看不到 LD 自己的回答。** 分类器输入里 LD/系统回复只保留意图标签和 ID，
  未授权 `local` 时连用户原句也只留引用，避免知识正文跨轮外发。
- **未注册项目不会创建 Goal**：写意图解析失败即 fail-closed（策略错误）。

配置（可选，不配就用关键词路由）：

```powershell
$env:LD_MODEL_BASE_URL = "https://.../v1"
$env:LD_MODEL_NAME     = "<chat model>"
$env:LD_MODEL_API_KEY  = "..."
$env:LD_MODEL_DOMAINS  = "shareable"      # 想让分类器看到 local 内容才加 local
```

`--no-model` 强制走关键词路由。每次回答都带 `intent_source`（`model` /
`heuristic_router` / `local_only` / `human_confirmation` /
`human_confirmation_stale` / `invalid_model_intent`），便于分辨"模型判断"与
"确定性规则"。

关于 `local_only`：分类模型若**没有** `local` 授权，而当前这句话看起来会引用本地
内容（密钥、内网地址、`.env`、连接串……），这一句**根本不发出去**，改用本机
关键词路由回答。不做"猜测式脱敏"——脱敏后的问题会被答错，而且让人看不出内容
其实没发出去。想让分类器看到原文，显式授予 `local`；不想，就不要在对话里贴
密钥。

注意：M4-D 的形状校验失败**不会自动重试**，而是返回 `clarify` 让用户换句话
（见 `ld/chat_intent.py` 模块说明）。真实模型 + 真实 Worker 的对话闭环见
`scripts/t53_real_chat_e2e.py`（需 `LD_MODEL_*` 与 `serve_ctl.py start`；
未配置时记录为 BLOCKED，不算通过）。

### 6.4 传统队列流程（仍然支持，未改变）

```powershell
python scripts/controlplane_cli.py project add my-repo C:\path\to\repo --concurrency 2
python scripts/controlplane_cli.py enqueue "实现指定功能并提交" `
  --project my-repo --cwd C:\path\to\repo --title feature-a --priority 100 `
  --idempotency-key feature-a-v1 --assert-file src/result.py
python scripts/controlplane_cli.py list
python scripts/controlplane_cli.py summary
python scripts/controlplane_cli.py approval list
python scripts/controlplane_cli.py approval approve <approval-id>
```

### 6.5 测试

```powershell
# M1–M4 新增（离线确定性测试）
python scripts/t27_project_context.py      # 隔离 / 版本 CAS / 来源 / 安全域
python scripts/t28_project_dispatch.py     # 注入 / 冻结 / 审批绑定 / 阻塞 / 兼容
python scripts/t29_reliability_edges.py    # PID 三态 / 降级标记 I/O
python scripts/t30_project_cli.py          # CLI 端到端（README 示例来源）
python scripts/t31_context_update_loop.py  # 自动回写 / 幂等 / needs_merge / 安全边界
python scripts/t32_ld_m4a.py               # M4-A 动作 / 作用域 / 幂等 / finish / dry-run
python scripts/t33_m3_handoff.py           # M3 checkpoint / 迁移 / 并发 / TOCTOU / 投影
python scripts/t35_m4_fixes.py             # shell 拒绝 / session 一致性 / checkpoint 采集
python scripts/t36_m4_model.py             # 模型边界 / fail-closed / 模型视图过滤 / 身份不落盘
python scripts/t37_m4_goal.py              # Goal 生命周期 / 恢复 / 预算 / 租约 / 停滞
python scripts/t39_m4_goal_reliability.py  # 崩溃注入恢复 / 租约 fencing / resume 步号
python scripts/t40_m4_checkpoint_safety.py # checkpoint 安全 / CLI 安全域默认值
python scripts/t50_conversational_ld.py    # M4-D 对话闭环 / 无第二真相 / 确认门控
python scripts/t51_intent_safety.py        # M4-D 意图安全：疑问不误提交 / 预算不可改
python scripts/t52_conversation_security.py # M4-D 历史投影 / 暂存版本绑定 / 并发确认
python scripts/t53_chat_outbound_safety.py   # M4-D 当前消息门控 / 长目标身份 / 签名稳定

# 既有回归
python scripts/t26_p0_fixes.py
python scripts/t20_approval_gate.py
python scripts/t21_approval_security.py
python scripts/t19_ledger_failclosed.py
python scripts/t17_concurrent_idempotency.py
python scripts/t18_refresh_split.py
python scripts/t22_runtime_loop.py
python scripts/t23_runtime_queue.py
python scripts/t24_multiprocess_claim.py
python scripts/t25_cli_queue.py
```

真实集成测试（需先 `python scripts/serve_ctl.py start`；T38 另需 `LD_MODEL_*`）：

```powershell
python scripts/t34_m3_real_worker.py --part all   # M3-01/02 真实 Worker
python scripts/t38_m4_real_e2e.py --part all      # M4 端到端：双依赖任务/恢复/隔离
```

T38 首轮证据 `evidence/project/T38-m4-real-e2e.json` 覆盖（按其断言集）：一次目标
声明 → 两个真实依赖任务 → 双双 verified → 重启恢复（CLI 子进程续跑同一 Goal，
不重派 A）→ 多项目隔离。严格版 `evidence/project/T38b-m4-real-e2e-strict.json`
（25/25 Pass）对**最终 Goal 状态**断言（`completed`、报告恰含 A/B、历史连续、
系统步键、Owner 介入与 human_resume 一致），且要求 Session B 真实 merge
（A 的 checkpoint 为 B HEAD 祖先）并真实运行测试。故障恢复与副作用拒绝见
`evidence/project/T38c-m4-real-recovery.json`。运行前置（T38-F05）：决策端点
`LD_MODEL_*`；handoff 任务的 agent 需要 shell（`git merge`/pytest/commit），
fixture 已内置项目级 `permissions.allow`。已知限制见
`docs/KNOWN_ISSUES.md` T38-F01/F02/F04/F05。

---

## 7. 目录

```
adapters/base.py                厂商无关的 Worker Adapter 契约 + 统一状态机
adapters/codebuddy/             唯一允许出现 CodeBuddy 专有知识的地方
controlplane/store.py           SQLite Ledger：work items / approvals / context / workspaces
controlplane/plane.py           门面：dispatch / enqueue / refresh / cancel / resume / summary
controlplane/verify.py          产物断言验收（不看 worker 终态）
controlplane/runtime.py         常驻循环：队列派发 / 刷新 / 孤儿扫描 / 退避
controlplane/project_context.py 【M1】版本化项目知识存储（CAS + 来源 + 验证状态 + 安全域）
controlplane/context_builder.py 【M1】Effective Prompt 构建（预算 / 筛选 / 阻塞缺口）
controlplane/context_update.py  【M2】验收后确定性候选回写（幂等 + 冲突保留）
controlplane/handoff.py         【M3】只读交接准备与 checkpoint 校验
controlplane/project_service.py 【M1–M4-A】项目确定性查询与提交服务
ld/tools.py                    【M4-A/B】受限 LD Tool Facade + 目标交接提交
ld/decision.py                 【M4-A】结构化决策 Schema 与确定性校验
ld/model.py                    【M4-A】OpenAI-compatible 真实决策模型（无 SDK）
ld/goal_store.py               【M4-B】持久化 Goal 状态与决策历史（同 Ledger）
ld/coordinator.py              【M4-A/B】单轮 run_once + 有界连续 run_goal
scripts/ld_cli.py              【M4-A/B】查询/单轮/run-goal/--resume
docs/KNOWN_ISSUES.md           已知问题、修复状态与复现方法
docs/PROJECT_CENTRIC_ROADMAP.md M1–M5 路线图与退出条件
docs/V0.2_PLAN.md               历史计划（v0.2 阶段，已完成其范围）
scripts/                        服务管控、测试脚本、CLI、detached 运行器
evidence/                       每次测试的证据（已脱敏；原始日志不入库）
sandbox/                        两个一次性 Git 仓库（不入库）
```

---

## 8. Roadmap

完整路线图见 **[docs/PROJECT_CENTRIC_ROADMAP.md](docs/PROJECT_CENTRIC_ROADMAP.md)**。

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | Project Context Foundation | **已完成** |
| M2 | Project Context Update Loop（验收后回写、候选知识、幂等） | **已完成** |
| M3 | Session Registry / Handoff（单机多 Session） | **已完成**（T33 确定性 + T34 真实 Worker；git commit checkpoint 的"worktree 关闭后可达"未覆盖） |
| M4-A | Minimal LD（受限工具 + 结构化决策 + 单轮协调） | **已完成**（T32/T36 确定性 + T38a 真实模型/Worker 单轮） |
| M4-B | 有界连续推进（持久化 Goal + 依赖任务确定性交接 + 恢复） | **已完成**（T39/T40 确定性含崩溃注入与租约 fencing；T38b 真实严格闭环 25/25 Pass；T38c 真实故障恢复验证） |
| M5 | Distributed Workspace（多节点、远程派工、受控同步） | 未实现 |

历史计划 [docs/V0.2_PLAN.md](docs/V0.2_PLAN.md) 描述的是 v0.2 阶段（本地单用户
控制平面），其范围已实现，不再作为后续路线图使用。

---

## 9. 安全与限制

- **worktree 不等于文件隔离。** `isolation=worktree` 只是 Git 工作树分离，
  进程仍以当前用户身份访问整台机器。没有任何 OS 级沙箱。
- **企业内网数据默认不出网。** 项目上下文条目带 `security_domain`（`local` /
  `shareable`）。M4-A 模型接口默认无任何安全域授权；只有显式声明的域会进入
  有限决策输入，项目路径与 Effective Prompt 始终排除。外部模型未授权
  `local` 时不得向其传输该域内容。
- **不自动跨机器合并。** 不存在"不同安全域信息自动合并"的逻辑。
- **验收独立于 Worker 自述。** Worker 报 `done` 不算通过；只有产物断言 + baseline
  归属 + 副作用检查通过才是 `verified`；无断言的成功任务是 `inconclusive`。
- **服务只绑 `127.0.0.1`**，`--auth password`，不使用 `--auth none`。
- **不写凭据。** 密码、Token、Cookie、Authorization 头不入库；证据写盘前统一脱敏。
- **真实执行与模拟测试必须分开。** 本文档与证据中，只有脚本实际跑出的结果才标记为
  Pass；未验证的一律写"未验证"。

## 10. 参考

- [REPORT.md](REPORT.md) — PoC 完整论证
- [capability-matrix.md](capability-matrix.md) — 能力矩阵
- [lifecycle.md](lifecycle.md) — 状态机
- [AGENTS.md](AGENTS.md) — 在本仓库工作的硬性约定
