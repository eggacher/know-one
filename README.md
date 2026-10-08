# KnowOne

可嵌入 Python 业务系统的知识入库与检索库。将 FAQ、公告和文本层 PDF 构建成可检索的证据，支持版本发布、权限过滤和原文定位；当前以 RAV4 中文手册验证完整流程。

## 当前能力

- **入库**：纯文本、Markdown、文本层 PDF；来源快照、幂等提交、持久化任务和 worker 租约恢复。
- **版本与权限**：Namespace 隔离、Document ACL、生效时间和业务适用范围过滤；Revision 发布、撤回和删除。
- **检索**：jieba 中文分词 + PostgreSQL 原生全文检索、pgvector 向量检索，以及 RRF 混合排序。
- **证据**：返回原文、PDF 原始页码、字符区间、标题路径和版本；按需补充相邻块上下文。
- **索引升级**：在独立 IndexGeneration 中重建，完成后原子切换。
- **调用方增强**：词典查询扩展、可选 LLM rerank，以及带证据引用和拒答状态的 LM Studio 回答示例。

HTML、扫描 PDF 的自动 OCR、通用复杂表格解析尚未作为核心入库能力提供。RAV4 目录中的表格抽取和 VLM 校验属于专项实验。项目已有小规模回归记录，尚无商用容量、延迟或全面质量验收结论。

## 工作流程

```text
资料 → 来源快照与入库任务 → 解析、切块 → 全文索引与 embedding
     → ready（待审核）→ 发布 → 可检索证据

用户问题 → 可选词典扩展 → 全文召回 + 向量召回 → RRF 融合
         → 可选 rerank → 原文证据 → 调用方生成带引用的回答
```

核心 `retrieve()` 返回证据。调用方负责身份认证、可信 AccessScope、对话上下文和回答生成。查询扩展与 rerank 由调用方显式启用。

当前 PDF 切块保留页边界，识别小节和表格前标题，并对长段按句末标点拆分。400 字符是拆分阈值；没有合适句界时块可能超过该值。它不是固定 token 切块，也不是每页只生成一个向量。标题路径目前单独存储，未额外拼入 embedding 文本。

## 本地启动

需要 Python 3.12+、Docker Compose，以及提供 OpenAI 兼容 `/v1/embeddings` 的模型服务。PDF 解析另需 Poppler 的 `pdftotext`，macOS 可通过 `brew install poppler` 安装。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
docker compose up -d db
```

修改 `.env`，将 embedding 服务地址与模型名设为服务实际提供的值：

```dotenv
KNOWONE_DSN=postgresql://knowone:knowone@localhost:5432/knowone
KNOWONE_EMBEDDING_ENDPOINT=http://localhost:1234/v1
KNOWONE_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
KNOWONE_EMBEDDING_DIMENSIONS=1024
```

示例数据库账号仅供本地开发。`.env` 不提交到 Git；已有进程环境变量优先于 `.env`。模型名和维度会冻结到索引代，必须与模型服务一致。

```bash
python -m know_one init-db
python -m know_one create-namespace demo
python -m know_one --help
```

`init-db` 只用于空数据库。已有数据库升级需先审阅 [迁移文件](know_one/storage/migrations/)。

## Python 示例：入库、发布、检索

在完成上述初始化后运行：

```python
from datetime import datetime, timezone
import os

from know_one import AccessScope, KnowOne
from know_one.config import load_local_env
from know_one.ingestion import TextSource

load_local_env()
kb = KnowOne(os.environ["KNOWONE_DSN"])
# 本地演示身份；业务系统需在认证后由服务端计算可信权限。
scope = AccessScope(
    "demo-operator", frozenset({"demo"}), frozenset({"ingest", "publish", "read"})
)
job = kb.ingest(
    TextSource("找回密码：请在登录页选择“忘记密码”，按提示重置。"),
    namespace="demo",
    source_key="faq/password",
    access_scope=scope,
    idempotency_key="demo-import-001",
)
# 演示同步构建；部署时由后台 worker 领取和处理任务。
kb.process_job(job.job_id)
status = kb.get_ingestion(job.job_id, access_scope=scope)
if status.status != "ready":
    raise RuntimeError(f"入库未完成：{status.status}")
# 首次发布新 Document 的状态代号为 0；后续更新需使用实际状态代号。
kb.publish(
    status.revision_id,
    namespace="demo",
    valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_until=None,
    expected_generation=0,
    access_scope=scope,
    idempotency_key="demo-publish-001",
)
result = kb.retrieve(
    "忘记密码怎么办？", namespace="demo", access_scope=scope,
    recall_mode="hybrid", top_k=8, deadline_ms=15000,
)
for evidence in result.evidence:
    print(evidence.text, evidence.source_locator)
```

`ready` 表示构建完成，发布后才能检索。相同业务请求重试应复用幂等键；修改内容需新键。`result.degraded`、`warnings` 和 `trace_id` 用于诊断失败与降级。接口边界见 [接口契约](docs/contracts.md)。

## PDF 与回答示例

PDF 可直接使用 `PdfSource(Path("data/manual.pdf"), first_page=1, last_page=20)`，也可提交后台任务：

```bash
PYTHONPATH=. python examples/submit_pdf.py \
  --namespace demo --principal demo-operator \
  --source-key manual/example --pdf data/manual.pdf \
  --idempotency-key manual-import-001
python -m know_one process-next-job
```

提交脚本输出 `job_id`；通过 `get_ingestion()` 获取任务状态和 `revision_id`，审核 ready 版本后调用 `publish()`。该流程保留 PDF 原始页码；选择页段入库不会将页码重编号。

已发布资料可通过命令行检索：

```bash
PYTHONPATH=. python examples/query_evidence.py \
  --namespace demo --principal demo-operator \
  --query "忘记密码怎么办？" --deadline-ms 15000
```

回答示例使用 LM Studio 原生 `/api/v1/chat`；其地址与模型通过参数显式指定：

```bash
PYTHONPATH=. python examples/answer_with_evidence.py \
  --namespace demo --principal demo-operator \
  --query "忘记密码怎么办？" --deadline-ms 15000 \
  --llm-base-url http://localhost:1234/api/v1 --llm-model qwen3.5-9b
```

可添加 `--stream` 输出流式事件，添加 `--rerank-base-url http://localhost:1234/v1` 启用重排。回答脚本还支持 `KNOWONE_ANSWER_BASE_URL`、`KNOWONE_ANSWER_MODEL` 和可选的 `KNOWONE_ANSWER_API_TOKEN`。

引用页码由 Evidence 的定位信息构造。无证据、资料不足或回答未绑定有效证据时，脚本分别返回 `no_evidence`、`insufficient_evidence` 或 `uncited_answer`。集成方式见 [回答集成契约](docs/answer-integration.md)；依赖检查可运行 `PYTHONPATH=. python examples/preflight.py --llm-base-url http://localhost:1234/api/v1`。

## 检索与评测

`recall_mode` 支持 `full_text`、`vector`、`hybrid`。hybrid 当前采用加权 RRF，全文权重 0.25、向量权重 1.0，并保留向量首位候选。全文使用 PostgreSQL 原生排序，不是 BM25。

查询扩展在原问题末尾追加命中词典的术语，例如“转向灯”追加“转向信号灯”。rerank 默认取 16 条候选，结合 LLM 前 4 条和输入排序前 4 条保底，再截回 top-k；失败时保留输入顺序并报告降级。

RAV4 资料需自行放入被 Git 忽略的 `data/`，准备流程见 [RAV4 示例](examples/rav4/README.md)。在资料已经入库并发布后运行裸检索回归：

```bash
python -m know_one.eval.smoke \
  --dataset examples/rav4/eval/hybrid_smoke.jsonl \
  --namespace rav4-hybrid --principal evaluator \
  --top-k 8 --deadline-ms 15000
```

带词典和 rerank 的评测另行指定配置：

```bash
python -m know_one.eval.smoke \
  --dataset examples/rav4/eval/hybrid_golden_v1.jsonl \
  --namespace rav4-hybrid --principal evaluator \
  --expansions examples/rav4/eval/query_expansions.json \
  --rerank-base-url http://localhost:1234/v1 --rerank-model qwen3.5-9b \
  --top-k 8 --deadline-ms 15000
```

评测按题计算 Hit@k：忽略空白后，`expected_any` 中任一短语出现在某条 Evidence 正文即可；指定 `expected_pages` 时须同时命中页码。`expected_context_any` 是同一条 Evidence 的补充上下文条件。该指标不衡量全部相关证据的召回率，也不直接衡量答案正确率。

[历史评测记录](docs/dev-journey.md)中：混动裸 hybrid 为 22/22，混动词典 + rerank 为 30/30，汽油 hybrid + rerank 为 18/18，回答层 smoke 为 5/5。这些是特定索引、模型和小型样本集下的历史结果；回答层 smoke 校验引用页码与拒答状态。复测时应固定资料版本、索引代、模型和配置。

## 索引升级与维护

更换 embedding 模型、维度或切块配置时，先调整配置，再创建独立索引代：

```bash
python -m know_one create-index-generation demo
python -m know_one rebuild-index-generation demo <generation-id>
python -m know_one activate-index-generation demo <generation-id>
```

只有完成全量 Revision 构建的索引代才能激活。发布、撤回、ACL 更新和删除可通过 CLI 执行；`--actor` 是审计身份，本地管理命令不能直接暴露成公共接口。具体一致性与删除边界见 [接口契约](docs/contracts.md)。

开发检查：

```bash
python -m pytest -q
```

依赖 PostgreSQL 或模型服务的集成检查需要对应环境；跳过的检查不代表已完成真实部署验证。

## 代码与文档

| 入口 | 内容 |
|---|---|
| [know_one/core/api.py](know_one/core/api.py) | KnowOne 业务门面、入库与检索流程 |
| [know_one/model](know_one/model/) | AccessScope、Evidence 等公开数据类型 |
| [know_one/cli.py](know_one/cli.py) | 本地管理命令 |
| [examples](examples/) | PDF 提交、检索、回答、预检和 RAV4 实验 |
| [术语与领域模型](CONTEXT.md) | Document、Revision、Publication、Chunk |
| [架构](docs/architecture.md) / [接口契约](docs/contracts.md) | 职责、权限、版本、生效时间、幂等 |
| [流水线](docs/pipeline.md) / [切块策略](docs/chunking.md) | 当前实现与后续设计；以各文档状态说明为准 |
| [Golden Set 编写](docs/golden-set-authoring.md) / [验收计划](docs/evaluation.md) | 标注口径与后续验收指标 |
| [开发历程](docs/dev-journey.md) | 切块、融合、词典和重排的历史排查与实验 |
| [运行保障](docs/operations.md) / [架构决策](docs/adr/) | 运维与设计取舍 |
