# 调用方回答集成契约

状态：当前可用。本文约束 [`examples/answer_with_evidence.py`](../examples/answer_with_evidence.py) 这个调用方示例的输入和输出；它不是 `KnowOne` 核心库新增的 `answer()` 接口。核心库继续只负责受权限、业务时间和适用范围约束的 Evidence 检索。

## 调用前提

调用方必须在可信后端构造 `principal` 与 Namespace，不能让浏览器提交自声明的身份或 Namespace。示例从 `--dsn`／`KNOWONE_DSN` 读取数据库连接；LLM 端点、模型和可选令牌分别由 `KNOWONE_ANSWER_BASE_URL`、`KNOWONE_ANSWER_MODEL`、`KNOWONE_ANSWER_API_TOKEN` 提供。

同步调用最小形式：

```bash
PYTHONPATH=. .venv/bin/python examples/answer_with_evidence.py \
  --namespace rav4-smoke-pdf --principal evaluator \
  --query "油枪自动跳枪后还要继续加油吗？" \
  --deadline-ms 15000
```

`top_k` 默认是 3。当前 RAV4 PDF 回归验证表明，`top_k=1` 会丢失跨页依据，调用方不应为缩短上下文擅自降为 1。

## 最终结果 JSON

非流式调用、以及流式调用的最后一行，均为以下形状：

```json
{
  "answer": "油枪自动跳枪后应立即停止加油。 [证据 1]",
  "citations": [
    {
      "evidence_index": 1,
      "source_locator": {"page": 161, "char_start": 1171, "char_end": 1857}
    }
  ],
  "status": "answered"
}
```

`source_locator` 是程序从 Evidence 复制的权威定位；不要采信模型在正文中自行声称的页码。`evidence_index` 从 1 开始，对应本次检索结果的顺序，仅在本次响应内有效。

| status | answer | citations | 调用方动作 |
|---|---|---|---|
| `answered` | 非空且带有效 `[证据 N]` | 至少一条 | 展示回答和 locator；业务若保存回答，保存最终行及 Evidence 身份／版本 |
| `insufficient_evidence` | null | 空数组 | 明确告知资料不足；不要展示相近候选作为依据 |
| `no_evidence` | null | 空数组 | 告知未检索到依据；本次不会调用 LLM |
| `uncited_answer` | null | 空数组 | 作为失败关闭处理；模型虽生成正文但未给可验证引用，不能展示正文 |

脚本参数错误、检索错误或 LLM HTTP／网络／超时错误会以非零退出码结束，错误信息输出到 stderr；调用方不能把该情况转换成 `no_evidence` 或资料不足。

## 流式 JSON Lines

追加 `--stream` 后，stdout 是 JSON Lines，不再是单一 JSON。消费端按行解析，并只把最后一行作为最终决定：

| event | 含义 | 展示规则 |
|---|---|---|
| `stream_stage` | LM Studio 模型加载或 prompt 处理边界 | 仅用于监控，不展示给终端用户 |
| `first_delta` | 收到第一个模型正文增量 | 仅用于首 token 延迟指标，不包含正文 |
| `verified_citation` | 有效 Evidence 编号及其 `source_locator` 已先到达 | 缓存为后续正文的依据，不单独当作回答展示 |
| `verified_text` | 在有效前置引用之后到达的正文增量 | 可拼接并临时展示 |

流式协议要求模型先输出 `[证据 N]`，再输出一句结论。脚本只在 `N` 落入当前 Evidence 范围时发出 `verified_citation`，随后才发出正文增量。最终 `chat.end` 聚合结果仍会重新执行拒答和引用校验，并将引用整理为句尾形式。

若最终行不是 `answered`，调用方必须立即清除本次已拼接的 `verified_text`，以最终状态替换；不得保存或转发临时文本。断流、超时或非零退出同样清除临时文本并显示可重试错误。

## 性能与可观测性

`--include-timings` 在最终 JSON 中增加 `timings_ms.retrieval` 和 `timings_ms.llm`。LM Studio 支持时还会返回 `llm_stats`：`input_tokens`、`total_output_tokens`、`reasoning_output_tokens`、`tokens_per_second`、`time_to_first_token_seconds` 与可选的 `model_load_time_seconds`。

这些字段用于同机、同模型、同数据集的回归比较，不是线上 SLA。当前 RAV4 三轮、15 条回答稳定性样本中，首 token P50 为 0.553 秒、P95 为 0.567 秒；输出速度约 5.3 token/s。调用方应在自身日志中记录请求 ID、模型配置和超时配置；若需要 KnowOne 的 `trace_id` 与 IndexGeneration，可先调用 `query_evidence.py` 或直接调用 `KnowOne.retrieve()` 获取。
