# AGENTS.md

给在这个仓库里工作的 AI Agent（以及人）的硬性约定。

## 1. 边界

- `adapters/base.py` **不得**出现任何厂商名、厂商状态字符串或厂商字段。
- 厂商专有知识只允许出现在 `adapters/<vendor>/` 内（状态映射、REST 路径、磁盘布局、已知缺陷）。
- 控制平面自身的东西（任务存储、幂等、验收、汇总）放在 `controlplane/`。
- 新接一个 Worker 时，只允许新增 `adapters/<vendor>/`，不允许改 `controlplane/` 的接口语义。

## 2. 证据

- 每条结论都要能指到一个 `evidence/**/*.json` 文件；没有证据的结论写"未验证"。
- 证据文件写盘前必须脱敏（`password` / `token` / `authorization` / `cookie`）。
- 原始服务日志、完整 transcript 不入库（`.gitignore` 已排除）。
- 不得手写、编造或"修复"证据内容；失败就是失败。

## 3. 术语（不可混用）

| 说法 | 含义 |
|---|---|
| 对话恢复 | 能拿回历史上下文（session resume） |
| 进程 respawn | 新进程绑定旧会话 |
| 执行恢复 | 中断的命令继续跑 |
| 工作流恢复 | 控制平面根据持久化状态判断"哪些步骤完成、哪些要重试、哪些要人工" |
| 状态对账 | 重新查询真实状态并修复本地视图 |
| 事件重放 | worker 侧能补发断线期间的事件 |

## 4. 状态判定

- Worker 的终态（`done/failed/...`）只是**它自己的说法**，不是验收结论。
- 验收必须基于 dispatch 时声明的产物断言（`ArtifactAssertion`）。
- 没有断言的成功任务 → `inconclusive`，不许当通过。
- 被拒绝/被停止/失联的任务 → 一律 `rejected`。

## 5. 运行纪律

- 长时间测试用 `scripts/detached.py`，不要占用交互式前台。
- 不启动可见窗口的进程；服务用 `scripts/serve_ctl.py`（`DETACHED_PROCESS`）。
- 测试结束必须停掉服务：`python scripts/serve_ctl.py stop`。
- 不在 `sandbox/` 之外创建任何测试产物。
