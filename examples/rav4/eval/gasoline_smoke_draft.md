# gasoline 手册评测草稿（rav4-gasoline）

`gasoline_smoke_draft.jsonl` 首批 18 条候选，覆盖安全带/儿童座椅/行李放置/雪地模式/保养周期/胎纹/拖车/应急等章节；支持短语逐字摘自 RAV4_OM_01999（汽油版）当前索引块文本，已通过「短语为原文 + 页码命中」自动校验与人工复核（问题自然度 + 短语充分性，2026-10-06 全部通过），已升级为锁定集 **`gasoline_smoke_v1.jsonl`**（与 draft 内容一致，`review_status=approved`）。

## 版式与索引现状（2026-10-05 摸底）

- 手册为 ■/● 标记体系（314 块含 ■、205 块含 ●），与混动版同族版式；章节标题为「N-N.」数字编号。
- 当前 active 代为 v1 粗切块（fingerprint `82fc174e9646`，432 块，p50 块长 519 字，无 ■ 边界，页眉页码/章节标题混入块首）。v3-table 若未来重建可直接适用（■ 体系齐全），但本轮未动索引。

## 基线与 rerank 结论（2026-10-05）

| 模式 | 命中 |
|---|---|
| full_text | 6/18 |
| vector | 18/18 |
| hybrid | 15/18（hood r11、maint r14、tow r9） |
| hybrid+rerank（blend） | **18/18** 零降级 |

三个 miss 全部在 rerank 深度（top-16）内且 vector 路 r2-6 可见，blend 的 LLM 头部直接拉回——**不动索引、部署时组合 rerank 即满分**。full_text 路在大块下系统性弱势（12 题 r99），属 v1 粗切块固有形态；部署路径（hybrid+rerank）无需求，故 v3-table 重建降级为可选技术债：若未来出现 full_text 依赖场景（如无 embedding 降级检索）或块污染病历，再按「评测集护栏 + 全量重验」流程重建。

## 与 hybrid 口径的区别

本数据集走无词典裸基线（词典 `query_expansions.json` 为混动版手册定制，汽油版未建）。
