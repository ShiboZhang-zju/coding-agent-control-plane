# 测试计划与执行记录

每条测试都有编号、脚本和证据文件。证据文件内记录：执行命令、HTTP 方法/路径/状态码、
请求与响应时间、Job ID、Session ID、PID、cwd、worktree、Git 状态、产物、预期/实际结果。

| 编号 | 目的 | 脚本 | 证据 | 结果 |
|---|---|---|---|---|
| T00 | 环境与接口盘点（OS/Git/Node/CLI/help/OpenAPI） | 人工命令 | `evidence/environment/*` | Pass |
| T00b | 非零退出码对应的 Job 状态 | `probe_state_semantics.py` | `evidence/jobs/T00b-state-semantics.json` | Fail（worker 报 done） |
| T00c | SSE 原始线格式 | `probe_sse_format.py` | `evidence/sse/T00c-sse-format.json` | Pass |
| T00d | `bash:true` 的真实执行语义（shell 分隔符） | `probe_shell_semantics.py` | `evidence/jobs/T00d-shell-semantics.json` | Pass（`;` 无效，`&` 有效） |
| T00e | Jobs 列表各变体延迟 | `probe_list_latency.py` | `evidence/jobs/T00e-list-latency.json` | Fail（~30s/次） |
| T01 | 四任务并发（两仓库） | `t01_concurrency.py` | `evidence/jobs/T01-concurrency.json` | Pass |
| T02 | 进程被杀后的 `lost` 判定 | `t02_lost_job.py` | `evidence/recovery/T02-lost-job.json` | Pass（~120s 后判 lost） |
| T03 | 跨仓库写隔离 | `t01_concurrency.py` | `evidence/isolation/T03-isolation.json` | Fail（可写穿） |
| T03b | agent 任务 worktree 隔离 | `t03b_worktree.py` + `t03b_recheck.py` | `evidence/isolation/T03b-worktree-agent.json` | Pass |
| T04 | 成功/失败任务验收 | `t01_concurrency.py` | `evidence/jobs/T04-acceptance.json` | Pass（失败任务被判 rejected） |
| T05/T05b/T05c | SSE 订阅/断线/重连/replay | `t05_sse.py`、`t05b_sse_rerun.py`、`t05c_sse_isolated.py` | `evidence/sse/T05*.json` | Fail（无 replay；连接后无事件） |
| T06 | cancel/stop + 人工拒绝前置 | `t06_cancel_stop.py` | `evidence/recovery/T06-cancel-stop.json` | Pass（cancel 1.9s 生效） |
| T07 | resume / respawn | `t07_resume_respawn.py` | `evidence/recovery/T07-resume-respawn.json` | 部分：运行中 job 返回 500 锁超时 |
| T08 | 服务重启后状态重建 | `t08_service_restart.py`、`t08b_exec_survival.py`、`t08c_exec_precise.py` | `evidence/recovery/T08*.json` | 部分：可发现/可重建，但执行不继续 |
| T09 | 权限请求 + 人工拒绝 | `t09_permission_request.py`、`t09_permission_final.py`、`t09b_permission_verify.py` | `evidence/permissions/T09*.json` | 安全行为 Pass；人工审批路径 Not Supported（background job 自动拒绝） |
| T10 | 主 Agent 汇总 | `t01_concurrency.py`（`plane.summary()`） | `evidence/jobs/T10-summary.json` | Pass |
| T11 | 多个**真实 Agent** 并行（独立 Job/worktree/产物归属） | `t11_four_agents.py`、`t11_recover.py` | `evidence/isolation/T11-4way-success.json`、`T11-4way-ledger.json` | **Pass**（4 路 peak=4、4 个 worktree、4 个产物、全部 verified） |
| T12 | 幂等派发 + 不安全策略拒绝 | `t12_idempotency.py` | `evidence/jobs/T12-idempotency.json` | Pass |
| T13 | 假成功：旧产物/他人产物/未发生提交 | `t13_fake_success.py` | `evidence/jobs/T13-fake-success.json` | Pass（3 rejected + 2 正向对照 verified） |
| T14 | 跨项目写入 → 否决 | `t14_cross_project.py` | `evidence/isolation/T14-cross-project-rejected.json` | Pass |
| T15/T15b/T15d | 控制台闪窗根因与修复 | `t15_console_flash.py`、`t15b_process_flash.py`、`t15d_selfflash.py` | `evidence/environment/T15*.json`、`CONSOLE-FLASH.md` | Pass（裸 subprocess 6/6 闪；`winproc` 0/6） |
| T16 | 重启后验收上下文不丢 | `t16_restart_mapping.py` | `evidence/recovery/T16-restart-mapping.json` | Pass |
| T17 | 并发幂等 + dispatch_unknown 诚实性 | `t17_concurrent_idempotency.py` | `evidence/jobs/T17-concurrent-idempotency.json` | Pass |
| T18 | refresh 拆分：慢查询/消失任务/全量扫描失败 | `t18_refresh_split.py` | `evidence/recovery/T18-refresh-split.json` | Pass |
| T19 | Ledger 降级 fail-closed + inflight 恢复 | `t19_ledger_failclosed.py` | `evidence/recovery/T19-ledger-fail-closed.json` | Pass |
| T20 | 审批门控：内容绑定、一次性、篡改/拒绝 | `t20_approval_gate.py` | `evidence/permissions/T20-approval-gate.json` | Pass |

## 环境级缺陷（影响所有测试解释）

1. **桌面注入的 safe-delete shim 让 dispatch 变 500**（`evidence/jobs/ENV-DEFECT.md`）：
   移除子进程的 `NODE_OPTIONS` / `CODEBUDDY_SAFE_DELETE_*` 后恢复 200。
2. **任何触碰 job store 的读接口固定 ~30s**：`GET /api/v1/jobs?all=1`、`?all=0`、`?cwd=`、
   `/jobs/resumable` 全部 ~30.3s；`/jobs/prefs` 与单任务 `GET /jobs/{id}` 是毫秒级。
   清理 `.locks`（1788 个残留文件）后仍是 ~32s，说明不是锁文件本身，而是 store 扫描路径。

## 评审后新增的环境阻塞（2026-10-29）

`UserPromptSubmit` hook（`longmemory.exe recall`，timeout=5s）在并发/连续派发时超时，
导致 agent 会话在 prompt 阶段就被拒绝，worker 只报 `model run failed`。
**这不是 CodeBuddy Jobs 的能力问题**，但它决定了当前可用的 agent 并发度（≈1–2）。
详见 `evidence/isolation/T11-parallel-blocked.md`。

## 复现方式

```powershell
python scripts/serve_ctl.py start
python scripts/make_sandbox.py
python scripts/detached.py start t01 scripts/t01_concurrency.py
python scripts/detached.py tail  t01
python scripts/serve_ctl.py stop
```

## 未完成 / 阻塞

- T09 的人工拒绝需要真人在 Web UI 点击"拒绝"；在此之前只能证明"进入等待审批态"。
- ACP：本阶段只用 REST + SSE 验证控制平面所需能力，ACP 仅做了接口存在性盘点（OpenAPI 有
  `/api/v1/acp*`，未做端到端 ACP 会话验证）→ 标记未验证。
