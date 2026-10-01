# RAV4 中文检索 smoke 标注

`smoke.jsonl` 是 25 条人工确认的中文问题，基于汽油版用户手册的文本层。`source` 与 `pages` 仅供人工回查；评估命令只使用 `id`、`query` 和 `expected_any`。

正文 Corpus 由以下页段通过 `pdftotext -raw -enc UTF-8` 提取，属于本地临时产物，不提交仓库。`-raw` 用于优先恢复双栏 PDF 的阅读顺序：

- 钥匙与车门：92–102 页
- 刮水器与燃油：158–162 页
- 保险丝与灯泡：348–352 页
- 轮胎漏气：376–383 页

先创建一个具有当前 embedding 配置的空 Namespace，再导入并发布这些页段：

```bash
python -m know_one create-namespace rav4-smoke
PYTHONPATH=. .venv/bin/python examples/rav4/scripts/ingest_smoke_corpus.py \
  --namespace rav4-smoke --principal evaluator
```

脚本直接从 `data/Rav4用户手册（汽油版）.pdf` 提取文本，需要本机已安装
`pdftotext`，并依赖已配置的 PostgreSQL 与 embedding 端点。成功后运行：

```bash
python -m know_one.eval.smoke \
  --dataset examples/rav4/eval/smoke.jsonl \
  --namespace rav4-smoke --principal evaluator --deadline-ms 15000
```

`expected_any` 对比会忽略空白字符，以适配 PDF 文本层造成的中文硬换行；这不影响
实际检索或返回的证据原文。

同一命令可安全重放。若任务已因外部 embedding 服务失败而标为 `failed`，应先修复
服务，再用递增的 `--attempt` 创建新的任务，例如 `--attempt 2`；不要在服务仍不可用
时反复递增该值。

这是一份检验中文召回配置的 smoke 集，不是商用 Golden Set。问题和支持片段均应在改动前由业务人员复核。
