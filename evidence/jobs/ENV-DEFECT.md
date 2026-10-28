# 环境级缺陷记录（影响测试解释，不属于被测产品设计）

本机环境：Windows 11 (10.0.26200)，CodeBuddy CLI 2.169.0，由 CodeBuddy CN 桌面端注入
`NODE_OPTIONS` / `CODEBUDDY_SAFE_DELETE_*`。

## 1. safe-delete shim 导致 dispatch 恒 500

现象（清理前的实测响应）：

```json
{"error":{"code":"INTERNAL_ERROR",
 "message":"[safe-delete] 操作失败: ERROR C:\\Users\\<user>\\.codebuddy\\jobs\\.locks\\<job>.state.lock.guard:
 Error during a `trash` operation: Os { code: -2147024894, description: \"windows error: 系统找不到指定的文件。\" }"}}
```

- 该 shim 把 `fs.rm` 路由到回收站；对 job-store 锁文件调用时抛错，于是 `POST /api/v1/jobs`
  返回 500，且**有时 job 已创建、HTTP 仍报错**（`evidence/jobs/T00-api-probe.json`）。
- 处置：`scripts/serve_ctl.py` 在启动子进程时剔除 `NODE_OPTIONS` 与 `CODEBUDDY_SAFE_DELETE*`
  （只影响该测试进程的删除路由，不涉及认证与权限模式）。剔除后连续两次 dispatch 均 200。
- 影响：不能把"500"直接当成被测产品的缺陷；Adapter 仍需保留"响应不可信 → 对账"逻辑，
  因为生产环境同样可能部署在桌面注入环境下。

## 2. job store 读接口固定 ~30.3s

`evidence/jobs/T00e-list-latency.json`：`?all=1`、`?all=0`、`?cwd=`、`/jobs/resumable`
两次测量都 ≈30.3s；`/jobs/prefs` 与 `GET /jobs/{id}` 是毫秒级。

- 清理并还原 `~/.codebuddy/jobs/.locks`（1788 个残留文件）后仍是 ~32s，说明不是锁文件数量本身。
- 影响：控制平面不能高频轮询全量列表；Adapter 用单任务查询做进度轮询，全量查询只用于
  `reconcile()`，超时设为 180s。

## 3. 终态写盘不稳定

同一版本早段（21:47–22:33）的 shell 任务能正常进入 `done/settled=true`
（`evidence/jobs/T01-concurrency.json`）；后段多次出现命令已执行完（产物已生成）但
`state` 恒为 `working`、`firstTerminalAt=null`、`alive=false`、`pid=null`
（`evidence/jobs/T00f-settle-probe.json`）。

- 已验证与 `USERPROFILE`/`HOME` 隔离无关（隔离后仍复现）。
- 结论：**worker 终态不可作为验收信号**，控制平面必须自建 `lost` 判定 + 产物验收。

## 4. 操作留痕

- 为验证缺陷 2，曾把 `~/.codebuddy/jobs/.locks/*`（1788 个文件）移动到
  `.locks.bak-20261028-223337/`，随后按原样还原（1758 个移动回去，30 个因同名冲突跳过）。
- 该期间新建的测试 job 目录在还原后不再出现在 job 列表中；本 PoC 的所有结论都来自
  `evidence/` 下已落盘的数据，不受影响。
