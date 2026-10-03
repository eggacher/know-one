# RAV4 混动版 Golden Set 草案

此文件对应 `hybrid_golden_draft.jsonl` 的首批 30 条候选。每一条的支持短语和页码均从 `RAV4HEV用户手册（混动版）.pdf` 逐字摘取，但 `review_status` 为 `pending`，因此不能替代 `hybrid_smoke.jsonl` 作为锁定验收集。一个问题可列出多个等价的原文支持短语及页码，避免手册在不同章节重复表达同一安全要求时造成误判。

人工复核时，应确认问题确实代表目标用户表达、短语足以支持问题结论、页码适用于当前发布版本；通过后将 `review_status` 改为 `approved`，并复制到版本化的锁定集。拒答问题应单独加入回答层数据集，不应放入检索 smoke。

## 跨块答案口径（expected_context_any）

答案原文与题干锚点不在同一 Chunk 时（如 key-battery：查询命中 p315 更换步骤，型号「锂电池 CR2032」在 p314 相邻块），样本可声明 `expected_context_any`。判定为附加条件：主证据仍必须命中 `expected_any` 锚文本与 `expected_pages`，且同一条主证据的 `context_parts` 必须带出答案原文，二者缺一即判漏检——不会因页码邻近而放宽。评测器检测到该字段时才向检索传入 `include_context`；主证据排序与数量不受影响。
