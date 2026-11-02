# T62 Stage 2 对照实验归档（G1）

本目录是 T62 受控对照实验的**逐组证据**，每组一条归档。汇总文件仍是
`../../project/T62-stage2-memory-contrast.json`。

## 文件

| 文件 | 组 | 证据等级 | 能否独立复核 |
|---|---|---|---|
| `pair1-archived.json` | pair1（有记忆 verified / 无记忆 verified） | `historical_record_only` | **不能**：原始 Ledger 的 `-wal` 被误删，已回退为 queued |
| `pair2.json` | pair2（有记忆 rejected / 无记忆 verified） | `archived_verdict` | verdict/提交/耗时/断言可以；SessionReference 未保留 |
| `pair3.json` | pair3（有记忆 verified / 无记忆 verified） | `archived_verdict` | verdict/提交/耗时/**会话引用**均可 |

## 证据等级的含义

- `live_ledger_verified`：该组自己的 Ledger 行仍带 verdict、Session、Attempt，今天
  还能读回来。当前没有一组是这个等级。
- `archived_verdict`：verdict 只存在于运行时写下的逐组归档里（`.poc-state/t62/pairs/<组>/summary.json`），
  与当时的 Worker 提交、补丁、测试结果一起保存。
- `historical_record_only`：Ledger 已不存在，只有事故**之前**写下的同期记录。
  这是最弱的一级，**永远不会被升级**，也不会被重复运行改写。

pair1 之所以保留而不是丢弃：它双臂都是 verified，看起来"没有信息量"，但把它删掉
只会让剩下的样本更整齐，等于选择性报告。它的数据是"引用"，不是"重测"。

## 结论（不在本目录做新判断）

实验结论固定为 **INCONCLUSIVE**，分三层：

1. 记忆链路成立（candidate → 人工 CAS 应用 → verified 晋升 → 新 Session 拿到 → Prompt 可追溯）。
2. 记忆收益未证明：有记忆 2 verified / 1 rejected，无记忆 3 verified / 0 rejected。
3. Verifier 可能存在测量偏差：pair2 有记忆臂 54 项测试全过，仅
   `file_contains: normalized_1000` 失败。

唯一的分歧是 pair2，方向对记忆臂不利。**不存在"有记忆通过、无记忆失败"的组**——
旧表述 "failed in opposite directions" 与数据不符，已撤回。

`archive_completeness` 是对外单独字段：它描述证据是否还读得全，**不改变**
`result=INCONCLUSIVE`。

## 与运行时记录的关系

`../T62-stage2-real-project.json` 是 Worker 服务在线时写下的原始验收记录
（27/27 Pass，`transcripts_readable: true`）。它**不会被重跑覆盖**；
G1 复核写到 `../T62-stage2-real-project-recheck.json`，并以 `recheck_of`
引用它。同理 `../T62-stage2-pilot-transcripts.json` 会继承运行时真实读到的
tool_calls 并标注 `transcript_source`，不会因服务离线被抹成 null。

## 重新生成

```
python scripts/t62_stage2_contrast_record.py     # 归档 + 汇总
python scripts/t62_stage2_supporting_record.py   # 行为支撑记录
python scripts/t62_stage2_real_project.py        # 主验收
python -m pytest tests/test_t62_g1_evidence.py -q
```

归档一旦存在就**不会**被重写：重复运行不增加组数、不改变字节内容，
也不升级任何一组的证据等级。

读取归档时会**重新校验结构**（Arm 数量、角色、task_id 唯一性、终态、
`verdicts`/`flipped` 重算比对、`pair_id` 与文件一致），不信任文件自称的
`complete: true`。手工改坏任一归档即 BLOCKED。

`ledger_recoverable` 只回答"原始 Ledger 是否还能举证"，`archive_readable`
回答"本归档是否读得到"。pair2 的任务行与 verdict 仍在 ledger 中
（`ledger_recoverable=true`），但其 `session_references` 表为空，
因此整组仍判 INCOMPLETE。两个字段互不替代。
