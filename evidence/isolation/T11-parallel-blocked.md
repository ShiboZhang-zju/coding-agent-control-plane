# T11：多个真实 Agent 并行的实测边界（结论：被本机 hook 阻塞，不是控制平面问题）

日期：2026-10-29　分支：`poc/codebuddy-adapter`　版本：CodeBuddy 2.169.0

## 1. 已验证的部分：Job 层确实做到了"真并发"

两次并行派发都确认 **CodeBuddy 同时接受了 N 个独立的 agent 会话**（不是串行排队）：

| 轮次 | 派发方式 | 从 worker 时间戳算出的并发峰值 | 独立 Job | 结果 |
|---|---|---|---|---|
| `T11-4way-parallel-blocked.json` | 4 个线程同时 POST | **4** | 4 | 4 个全部 `failed` |
| `T11-2way-serial-blocked.json` | 串行提交、并行执行 | **2** | 2 | 2 个全部 `failed` |
| 早前一跑（控制台记录见下） | 串行提交、并行执行 | 2 | 4 | **3 succeeded / 1 failed**，3 个独立 worktree |

`peak_from_timestamps` 用每个 job 的 `createdAt` / `firstTerminalAt` 做区间扫描，不依赖采样，
所以"4 路真并发"这一点是确定的。

## 2. 阻塞点：本机 `UserPromptSubmit` hook 超时

每个失败的 job，worker 日志里都是同一行：

```
UserPromptSubmit operation blocked by hook:
["C:\Users\<user>\.codebuddy\hooks\longmemory.exe" recall]: Hook timed out after 5000ms
```

job 的 `detail` 只说 `model run failed — check the worker log for the provider error`，
**看不出真实原因**；必须去读 `~/.codebuddy/logs/job-<id>.log` 才能看到 hook 超时。

`~/.codebuddy/settings.json` 里对应配置：

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command",
  "command": "\"...\\longmemory.exe\" recall", "timeout": 5 }] }]
```

结论：**并行度上限来自这个 5 秒的记忆检索 hook**，不是 CodeBuddy 的 Job 系统，也不是模型配额。
多个 agent 会话同时触发 `longmemory.exe recall` 时几乎必然超过 5s，整个 prompt 被拒绝，
于是所有会话一起失败。

## 3. 对控制平面的要求（已在该分支体现）

1. `failed` 状态**不足以定位原因**：需要把 worker 日志里的 hook/permission/provider 错误
   归到"前置条件失败"这一类，而不是当成任务本身的失败（否则会误触发重试风暴）。
2. 并发要有**预算**：在 hook 修好之前，`max_concurrency` 实际可用值是 1–2。
3. 派发前应做**预检**（preflight）：连续两次 hook 超时的会话应立刻熔断并上报，
   而不是把 N 个任务全部派出去再一起失败。

## 4. 建议的修复（需要机器所有者操作，不在本仓库范围内）

- 把 `UserPromptSubmit` 的 `timeout` 从 5 提到 20–30，或
- 让 `longmemory.exe recall` 变成异步/带缓存（并发时只查一次），或
- 在批量派发场景下不挂该 hook。

修好后直接重跑：

```powershell
$env:POC_T11_TASKS="4"; python scripts/t11_four_agents.py
```

预期通过条件（脚本内已实现）：4 个独立 Job、4 个不同 worktree/分支、各自产物存在且不互相污染、
`all_verified=true`、主检出未被改动。

## 5. 早前成功一跑的控制台记录（原始输出，未改写）

```
dispatched agent-a1 cp-64afbe661422 8f124cdc
dispatched agent-a2 cp-baa66ae7d2df 5c015a79
dispatched agent-b1 cp-c0c5dda12b41 f9fc84f7
dispatched agent-b2 cp-ac653804c285 e8416b8a
{
 "peak_concurrent_running": 1,
 "peak_from_timestamps": 2,
 "distinct_worktrees": 3,
 "all_have_own_artifact": true,
 "no_foreign_artifacts": true,
 "all_verified": false,
 "verified_count": 3,
 "failed_count": 1,
 "main_checkout_untouched": false
}
agent-a1 succeeded verified wt= True
agent-a2 succeeded verified wt= True
agent-b1 failed rejected wt= False
agent-b2 succeeded verified wt= True
```

说明：

- 3 个 agent 各自在独立 worktree 里创建了产物并通过归属验收（`verified`）；
- `main_checkout_untouched=false` 是当轮的**误报**（`.codebuddy/` 目录被算成主检出改动），
  之后已在 `verify.repo_fingerprint()` 里过滤 `.codebuddy/`，并新增 `T11-4way-parallel-blocked`
  同轮 `main_checkout_untouched=true` 作为对照；
- `b1` 的失败同样来自 hook 超时。
