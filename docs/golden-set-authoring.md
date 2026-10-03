# Golden Set 编写指南

状态：用于接入真实 FAQ、公告或手册后的人工标注。没有真实资料时不要填写看似合理但无法回溯的答案；RAV4 样本只能作为格式和技术基线。

## 最小检索集格式

现有 `python -m know_one.eval.smoke` 使用 JSONL。每行必须是一个 JSON 对象：

```json
{"id":"billing-refund-001","query":"退款到账一般要多久？","expected_any":["退款将在 3 至 5 个工作日内原路退回"],"source":"faq/refund-v2026-10"}
```

字段含义：

| 字段 | 要求 |
|---|---|
| `id` | 稳定且唯一；推荐 `主题-序号`，不能随文案改动 |
| `query` | 用户会实际输入的问题；保留口语、简称、错别字或否定表达 |
| `expected_any` | 一条或多条人工确认的原文短语；任一短语命中即可，不写模型生成的概括 |
| `expected_pages` | PDF 可选，填写允许命中的原始页码数组；非 PDF 省略 |
| `source` | 建议填写来源标识，当前 smoke 不读取它，但便于人工排查与按来源分组 |

`expected_any` 必须能在已批准、已发布的原文中逐字找到。它不是期望回答，也不应包含调用方编造的解释。

## 首批样本构成

每份真实来源先选 20–30 条，再逐步扩大。至少覆盖：

- 高频直接问法，以及同一问题的一条口语改写；
- 否定、限制、例外、单位和时间条件；
- 容易被相似条款干扰的问题；
- 版本替换前后的业务时间边界；
- 不同 Namespace、ACL 或适用范围下的隔离验证；
- 10–20% 无答案问题。这些不放进检索 smoke，而放进回答层 `answer_smoke`，期望 `insufficient_evidence` 或 `no_evidence`。

不要把同一 FAQ 的十种同义改写全部放入验收集；保留一两条在开发集，其余留给锁定验收集，避免调参时记住题目。

## 标注与运行流程

1. 从已审核的原始资料复制支持短语，记录来源、PDF 页码和适用条件。
2. 第二位业务人员复核问题、短语和页码；分歧以原文和业务规则解决。
3. 入库、处理并发布资料到独立评估 Namespace。
4. 运行：

   ```bash
   .venv/bin/python -m know_one.eval.smoke \
     --dataset data/golden/retrieval.jsonl \
     --namespace business-eval --principal evaluator \
     --deadline-ms 15000
   ```

5. 对漏检附加 `--include-miss-evidence`，只在具备资料访问权限的环境审阅候选原文。
6. 调整词典、切块或排序前，先保留原始报告；修改后同时跑开发集和锁定验收集。

RAG 回答评测另建 `answer_smoke.jsonl`，正例标注 `expected_pages`，无答案标注 `expected_status: "insufficient_evidence"`。详见 [evaluation.md](evaluation.md)。
