# coding-agent-control-plane

**一个正在演进为 Project-Centric LD System 的 Coding Agent 控制平面。**

最终目标是：一个 Human Owner 管理一个全局 **LD Agent**，由 LD 协调多个拥有独立上下文的
**Project**，每个 Project 通过 **Coding Agent** 执行具体任务。Control Plane 是这个系统的
**基础设施层**——它不再是产品本身，而是让上述结构可靠运转的执行治理层。

当前阶段：**M4-A（最小单轮 LD）功能开发完成，但未验证**。
M1/M2 有已保存的测试证据；M3 代码存在但尚未通过验收。本轮未执行
CodeBuddy、stub/SQLite 实验或完整回归，不宣称 M3 交接或 M4 MVP 通过。
自主循环式完整 LD Agent 与 M5 跨机器远程执行尚未实现，
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
3. **Minimal LD**（M4-A 功能已实现、未验证）：用受限工具面和结构化决策做一次
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

已验证项只引用真实 `evidence/**/*.json`；M3 与 M4-A 都统一显式标为
“功能已实现/开发完成，未验证”。

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

### 4.3 Minimal LD（M4-A，功能开发完成、未验证）

| 能力 | 实现位置 | 状态 |
|---|---|---|
| 受限 LD Tool Facade（7 个工具） | `ld/tools.py` | 功能已实现，未测试 |
| 五种结构化动作 + 严格字段/参数校验 | `ld/decision.py` | 功能已实现，未测试 |
| 单轮 `run_once(goal, project_id=None, dry_run=False)` | `ld/coordinator.py` | 功能已实现，未测试 |
| 确定性转人工门控 + `finish` 二次核验 | `ld/coordinator.py` | 功能已实现，未测试 |
| 同 Ledger CLI（列表/概况/预览/单轮执行） | `scripts/ld_cli.py` | 功能已实现，未测试 |

M4-A 未引入任何 Agent Framework，也没有内置或伪造模型。模型未注入时返回
`model_not_configured`；CLI 的 `--decision-json` 只是本地结构化决策注入口，
不会加载 Python 或 Shell。Human Owner 目标默认按 `local` 域处理，模型未获得
`local` 授权时协调器不会调用它。

---

## 5. 当前未实现能力

**明确未实现，不要假设存在：**

- **循环式完整 LD Agent。** 当前只有单轮、唯一全局 LD 协调器；没有内置 LLM、
  自动多轮规划、多 LD、复杂 Goal DAG 或 Agent 协商。
- **经真实 Worker 验证的 Session 自动交接。** prepare/submit 功能已实现，
  但“关闭 Session A 后由 Session B 不读旧 transcript 继续并通过验收”
  本轮未执行，仍属未验证。
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

### 6.3 M4-A 最小 LD CLI（功能示例，未验证）

```powershell
# 只读查询，与 Runtime 使用同一 Ledger
python scripts/ld_cli.py --ledger $L list-projects
python scripts/ld_cli.py --ledger $L brief <project-id>

# 没有模型/决策注入时明确返回 model_not_configured
python scripts/ld_cli.py --ledger $L preview "继续实现当前项目" --project <project-id>

# 使用本地 JSON 结构化决策做 dry-run（不入队）
python scripts/ld_cli.py --ledger $L preview "实现健康检查" `
  --project <project-id> --decision-json .\decision.json

# 执行一轮；submit_task 仍只是走 ProjectService 入队，不绕过审批
python scripts/ld_cli.py --ledger $L run-once "实现健康检查" `
  --project <project-id> --decision-json .\decision.json
```

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
# M1–M3 新增
python scripts/t27_project_context.py      # 隔离 / 版本 CAS / 来源 / 安全域
python scripts/t28_project_dispatch.py     # 注入 / 冻结 / 审批绑定 / 阻塞 / 兼容
python scripts/t29_reliability_edges.py    # PID 三态 / 降级标记 I/O
python scripts/t30_project_cli.py          # CLI 端到端（README 示例来源）
python scripts/t31_context_update_loop.py  # 自动回写 / 幂等 / needs_merge / 安全边界

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

M4-A 本轮没有运行任何测试，也没有生成新 evidence。次日应先补决策
Schema/Tool Facade/单轮 dry-run 的离线测试，再执行 M1/M2 回归，最后单独验收 M3。

真实 Worker 集成测试（`t11_four_agents.py`、`t12`、`t14`、`t08*`）需要先启动
CodeBuddy 服务；它们不是离线可跑的，脚本会直接报连接错误。

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
ld/tools.py                    【M4-A】受限 LD Tool Facade
ld/decision.py                 【M4-A】结构化决策 Schema 与确定性校验
ld/coordinator.py              【M4-A】最小可注入模型的单轮协调器
scripts/ld_cli.py              【M4-A】列表/概况/预览/执行单轮 LD
docs/KNOWN_ISSUES.md           M3 待验证问题、影响与复现方法
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
| M3 | Session Registry / Handoff（单机多 Session） | **功能已实现，未验证** |
| M4-A | Minimal LD（受限工具 + 结构化决策 + 单轮协调） | **功能开发完成，未验证** |
| M4-B | LD 多轮推进/经验证的 Handoff 工具 | 未实现 |
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
