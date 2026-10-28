# PoC 规范：通用 Coding Agent 控制平面的首个 Worker Adapter

> 本文件是本次任务的原始要求（用户提供），任何实现与报告都必须对齐它。

## 一、项目定位

项目名 `coding-agent-control-plane`，仓库
`https://github.com/ShiboZhang-zju/coding-agent-control-plane.git`。

面向多种 Coding Agent Harness 的通用控制平面，未来需兼容 CodeBuddy、Codex、
Claude Code、OpenCode、Gemini CLI 以及其他支持 CLI / REST API / ACP 或类似协议的
Coding Agent。本阶段只用 **CodeBuddy** 作为第一个 Worker Adapter，验证通用控制平面
需要的能力。

CodeBuddy 只是首个被测 Worker，**不得**把控制平面的核心领域模型、生命周期或调度逻辑
与 CodeBuddy 专有 API 耦合。

## 二、最终目标

评估 CodeBuddy 原生 Multitask、Jobs REST API、SSE、ACP、后台任务和 worktree 隔离
能力，判断能否在不自建完整进程监管系统的前提下实现：

1. 多项目任务派发；2. 并行执行和隔离；3. 集中查询所有任务状态；4. 实时进展监控；
5. 中断后的状态重建；6. 停止、恢复和重新派发；7. 权限请求和人工审核；
8. 失败识别及验收；9. 一个主 Agent 汇总全部项目并提出下一步行动；
10. 后续通过统一 Adapter 接入其他 Coding Agent。

不直接开发完整产品，只做可验证的 PoC、通用协议边界和最小测试工具。

## 三、工作方式

仅在以下节点要求人工介入：登录/凭据、权限请求人工拒绝测试、超出临时测试目录的操作、
最终报告审核。其余自主执行并记录假设。

## 四、Git 要求

1. 克隆或打开仓库；
2. 仓库为空则创建最小 `README.md`、`.gitignore` 和本任务规范，建立 `main` 初始提交，
   然后创建 `poc/codebuddy-adapter` 分支；
3. 所有 PoC 工作在该分支完成；
4. 不得把实验代码合并进 `main`；
5. 可本地提交；
6. 完成后推送分支并创建 Draft PR；登录或权限不可用时如实报告，不得伪造成功；
7. 推送前检查全仓库，确保无密码、Token、Cookie、Authorization Header 等凭据。

## 五、安全限制

- 只用临时测试仓库与临时测试目录；不访问或修改生产项目；不使用真实部署环境与生产凭据；
- 不使用 `bypassPermissions`、`--dangerously-skip-permissions`、`CODEBUDDY_IS_SANDBOX=1`；
- CodeBuddy HTTP 服务只绑 `127.0.0.1`，必须开启身份验证，不得暴露公网；
- 不关闭系统安全机制；不把未脱敏原始日志提交 Git；
- 遇到登录、配额、网络或功能不可用，记录为阻塞或未验证，不得声称测试通过。

## 六、第一阶段：环境与接口盘点

实际执行并记录：操作系统与版本、Git 版本、Node.js 版本、CodeBuddy 可执行文件路径、
CLI 精确版本、安装来源、`codebuddy --help` 输出、与 `--multitask` / `--bg` / `--serve` /
`--acp` / `--worktree` / `--resume` / `--continue` / 权限模式有关的实际帮助信息；
当前版本是否实际提供 Jobs REST API、SSE、ACP 和 worktree。

启动只监听回环且开启认证的服务，抓取 `/api/openapi.json` 保存到
`openapi/codebuddy-openapi.json`（认证信息保存前脱敏）。

不能只按在线文档判断功能存在，必须以当前安装版本的 CLI 输出、OpenAPI 和实际请求结果为准。

## 七、通用 Adapter 边界

定义与厂商无关的最小契约：`capabilities()`、`dispatch()`、`getTask()`、`listTasks()`、
`streamEvents()`、`cancel()`、`resume()`、`respawn()`、`requestApproval()`、
`collectArtifacts()`、`reconcile()`。

统一状态至少覆盖：`queued`、`starting`、`running`、`waiting_for_approval`、`succeeded`、
`failed`、`cancelled`、`lost`、`unknown`。通用模型中不得直接出现 CodeBuddy 内部类型，
CodeBuddy 专有字段只保存在 adapter metadata 中。

只实现足以支持 PoC 的最小接口和 CodeBuddy Adapter。

## 八、并发测试设计

创建 `sandbox/repo-a`、`sandbox/repo-b` 两个临时 Git 仓库，在其中启动四个独立后台任务。
每个任务必须有唯一任务 ID、可识别文件产物、可验证 Git diff/commit、足够长的运行时间、
不访问临时仓库之外的文件、不使用生产网络服务。

必须记录：Control Plane Task ID、CodeBuddy Job ID、ACP Session ID、进程 PID、工作目录、
worktree 路径、Git 分支、Git commit、启动时间、完成/失败时间、最终状态、产物路径。

验证：四任务可同时运行；repo-a 任务不能写 repo-b；不同 worktree 不能互相覆盖；
一个任务失败不影响其他；重复派发同一逻辑请求能否幂等；失败任务绝不能标记验收通过。

## 九、SSE 测试

连接 Jobs SSE 事件流并记录：连接建立时间、收到的事件类型、Job ID、Session ID、状态变化、
事件时间戳、是否具有事件 ID/游标/可恢复位置。

步骤：建立订阅 → 启动并观察任务 → 主动断开 → 断开期间任务继续变化 → 重新连接 →
判断断线期间事件能否重放 → 用 `GET /api/v1/jobs?all=1`（以 OpenAPI 为准）重新查询完整状态。

必须区分：事件流恢复、当前状态重新查询、丢失事件后的状态对账。若 SSE 不支持 replay，
实现最小 `reconcile()`，但不得伪装成原生事件恢复。

## 十、恢复测试

分别测试且不得混为一谈：

1. **对话恢复**：能否通过 Session ID / `resume` / `continue` 拿回历史上下文；
2. **进程 Respawn**：原进程结束后能否启动新进程并重连原会话；
3. **执行恢复**：服务或进程重启后，原先正在执行的命令是否继续执行；
4. **工作流恢复**：即使原进程不能继续，控制平面能否根据持久化状态判断已完成步骤、
   已存在产物、需重试任务、需人工处理任务。

实际测试：stop、cancel、respawn、resume、服务重启、控制平面测试程序重启、正在运行的
Job 在重启后的状态、已完成 Job 在重启后的可查询性、等待权限任务在重启后的状态。

## 十一、权限和人工审核测试

设计一个无害但确实会触发权限请求的任务：任务进入 `waiting_for_approval` 或对应状态 →
暂停等待人工操作 → 明确告知需要点击"拒绝" → 拒绝后验证命令确实没有执行 → 验证没有目标
文件、Git diff 或其他副作用 → 记录 Job 最终状态 → 控制平面不得把拒绝后的任务标记为成功。
不得自己模拟人工拒绝，不得绕过权限系统。

## 十二、主 Agent 汇总能力测试

验证能否让一个主 Agent 在不逐个打开四个任务窗口的情况下：查询全部任务、汇总当前进度、
识别运行中/等待审核/失败/已完成任务、列出每个项目产物、识别阻塞、提出下一步行动、
指出需人工审核事项。结论必须来自结构化状态或实际查询结果。

## 十三、证据要求

每个测试保留：测试编号、目的、前置条件、实际执行命令、HTTP 方法与路径、HTTP 状态码、
请求时间、响应时间、Job ID、Session ID、PID、cwd、worktree、Git 状态、产物、预期结果、
实际结果、`Pass / Fail / Blocked / Not Supported`。证据须可复查，但必须移除 Token、Cookie、
密码、Authorization Header、用户隐私信息、无关系统路径与凭据。

## 十四、交付目录

```
coding-agent-control-plane/
├── README.md
├── AGENTS.md
├── REPORT.md
├── capability-matrix.md
├── lifecycle.md
├── docs/{POC_SPEC.md,test-plan.md,adapter-contract.md}
├── adapters/codebuddy/
├── openapi/codebuddy-openapi.json
├── evidence/{environment,jobs,sse,recovery,permissions,isolation}/
├── scripts/
└── sandbox/
```

`sandbox/` 中的临时仓库和大体积运行产物不得提交 Git。

## 十五、能力矩阵分类

原生支持 / 需要配置 / 需要 Adapter 开发 / 需要控制平面开发 / 不支持 / 未验证。
至少覆盖：任务派发、后台运行、并发任务、worktree 隔离、状态查询、全局任务查询、SSE、
SSE 断线重连、事件 replay、状态对账、cancel、stop、resume、respawn、对话恢复、执行恢复、
工作流恢复、权限请求、人工拒绝、产物收集、失败识别、自动验收、主 Agent 汇总、Adapter
接入难度。

## 十六、Go / No-Go 标准

**Go**：四任务可靠并行；文件与 worktree 隔离有效；可集中查询状态；SSE 或轮询 + reconcile
可靠重建状态；cancel 和失败状态不误报成功；权限拒绝无副作用；服务重启后可完成状态重建；
CodeBuddy 专有逻辑被限制在 Adapter 内；无需重新实现完整进程监管器。

**Conditional Go**：基本能力可用但需少量开发（本地状态存储、幂等键、reconcile、验收器、
审批队列、事件标准化、失败重试），必须列出最小开发清单与复杂度估算。

**No-Go**：无法可靠查询后台任务；重启后任务完全不可发现；无法隔离工作目录；权限拒绝后仍
执行命令；失败任务被标记为成功；无法建立稳定 Adapter；必须自建几乎完整的进程、会话和事件
监管系统。

## 十七、最终报告必须回答

1. CodeBuddy 是否适合作为第一个 Worker Adapter？
2. 哪些能力是 CodeBuddy 原生提供的？
3. 哪些能力只是对话恢复，不是执行恢复？
4. 哪些能力需要控制平面补充？
5. 是否需要自建进程监管器？
6. CodeBuddy Job、ACP Session、CLI 对话和系统进程之间的生命周期关系？
7. 要接入 Codex、Claude Code、OpenCode、Gemini CLI，当前契约是否足够？
8. 最小下一阶段开发列表？
9. 最终结论是 Go、Conditional Go 还是 No-Go？
10. 每个结论对应的证据文件在哪里？

## 十八、执行原则

先检查再实施；先定义通用边界再写专有适配；文档宣称不等于测试通过；命令成功退出不等于
业务验收通过；对话能恢复不等于执行能恢复；SSE 重连不等于事件可重放；有 Job ID 不等于任务
可持久恢复；文件存在不等于任务通过验收；不确定时标记"未验证"；不得伪造日志、状态、接口
响应或测试结果。
