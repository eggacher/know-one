# KnowOne

可嵌入 Python 业务系统的知识入库与检索库，首个落地场景是中文客服 FAQ 和公告。

**当前状态：已具备纯文本、Markdown 与文本层 PDF 入库、IndexGeneration 重建切换、发布、受约束的全文/向量混合检索，以及 Document 撤回、ACL 更新和立即删除。HTML 解析与重排尚未实现，也没有商用性能或质量验证。**

## 范围与职责

KnowOne 负责知识构建、版本发布、受约束的混合检索及证据返回。调用方负责身份认证、授权范围的计算、对话上下文、生成、拒答和转人工；KnowOne 负责执行调用方传入的可信授权范围。

当前接收审核过的 FAQ 文本、Markdown 和文本层 PDF；工单应先脱敏、提炼、审核。HTML、扫描 PDF、复杂表格和图片解析是后续能力，不在首版自动处理承诺内。首版仍需保证失败可见、来源可追溯。

核心库可被在线进程和后台 worker 共同引用。耗时入库在后台执行，模型使用共享推理端点或批准的数据处理 API。无需先建设独立 SaaS。

## 目标接口

`KnowOne` 可从根包导入。以下入库、发布和检索流程均可运行；参数、错误和一致性以 [接口契约](docs/contracts.md) 为准。

```python
# 调用方完成身份认证并计算可信 scope；客户端不能自行指定授权范围。
from know_one import AccessScope, KnowOne
from know_one.ingestion import TextSource

kb = KnowOne("postgresql://knowone:knowone@localhost:5432/knowone")
editor_scope = AccessScope("editor-1", frozenset({"game-a-cs"}), frozenset({"ingest"}))
publisher_scope = AccessScope("publisher-1", frozenset({"game-a-cs"}), frozenset({"publish"}))
source = TextSource("问题：怎么找回密码？\n\n答案：请在登录页选择“忘记密码”。")
job = kb.ingest(source, namespace="game-a-cs",
                source_key="official/faq/password", access_scope=editor_scope,
                idempotency_key="import-20260923-001")
kb.process_job(job.job_id)  # M1 worker 入口；生产环境由后台 worker 调用。
status = kb.get_ingestion(job.job_id, access_scope=editor_scope)
# ready 表示构建完成、尚未发布。运营预览通过后：
kb.publish(status.revision_id, namespace="game-a-cs",
           valid_from=effective_time, valid_until=None,
           expected_generation=state_generation,
           access_scope=publisher_scope, idempotency_key="publish-001")

result = kb.retrieve(query="怎么找回密码啊", namespace="game-a-cs",
                     access_scope=reader_scope, top_k=8)
# result.evidence：原文、来源定位、revision、排序信息
# result.degraded / warnings / trace_id：运行状态
```

文本层 PDF 可通过 `PdfSource` 入库；页码范围会随原文快照保存，后续
IndexGeneration 重建仍只处理相同页段，Evidence 的 `source_locator.page` 为原始
PDF 页码。该能力依赖系统的 `pdftotext`；macOS 可执行 `brew install poppler`：

```python
from pathlib import Path

from know_one.ingestion import PdfSource

source = PdfSource(Path("data/manual.pdf"), first_page=92, last_page=102)
```

扫描件、图片和复杂表格不自动 OCR，解析失败时任务会明确失败而不会发布不完整内容。

当前保留原始 query，不做 LLM 改写。检索时限定 Namespace、当前权限、发布状态、生效时间和业务适用范围，再执行向量与关键词召回及 RRF 融合；尚未实现 rerank。生成示例放在调用方；首版不实现 `answer()`。

## 起步选型

- Python ≥ 3.12；目前使用标准库数据类型、psycopg 和 pytest。
- PostgreSQL + pgvector + PostgreSQL 原生全文检索；原生全文排序不称为 BM25。
- BGE-M3 作为向量基线候选，reranker 固定具体模型版本后评测；不预设其为业务最优。
- 结构优先切块，按中文句子边界组合，token 上限兜底；embedding 语义切块作为对照实验。
- 复用宿主任务机制；没有现成机制时用 PostgreSQL 持久任务表和 worker，首版不强制增加消息队列。
- InMemory 实现仅用于流程测试，真实检索验收使用 PostgreSQL。

## 本地骨架验证

```bash
python -m pip install -e '.[dev]'
cp .env.example .env
python -m pytest -q
docker compose up -d db
python -m know_one init-db
python -m know_one create-namespace game-a-cs
```

`know_one/model/__init__.py` 定义公开数据类型，`know_one/core/api.py` 定义 `KnowOne` 业务门面；`python -m know_one --help` 可查看本地管理命令。复制 [`.env.example`](.env.example) 为 `.env` 后可统一设置 `KNOWONE_DSN`、`KNOWONE_EMBEDDING_ENDPOINT`、`KNOWONE_EMBEDDING_MODEL` 和 `KNOWONE_EMBEDDING_DIMENSIONS`。默认模型为 `Qwen/Qwen3-Embedding-0.6B`、1024 维；显式参数和进程环境变量优先于 `.env`。`create-namespace` 会将当前模型、维度和切块配置冻结到首个 active IndexGeneration。`init-db` 使用 `know_one/storage/schema.sql` 初始化空数据库，只应执行一次。已有旧 schema 的开发库需先人工审阅并执行 `know_one/storage/migrations/0001_persist_ingestion_source.sql`；该迁移拒绝为旧任务伪造来源快照。

升级 embedding 或切块配置时，先将 `.env` 改为目标配置，再依次执行：

```bash
python -m know_one create-index-generation game-a-cs
python -m know_one rebuild-index-generation game-a-cs <generation-id>
python -m know_one activate-index-generation game-a-cs <generation-id>
```

重建在隔离的 building generation 中进行；最后一个命令仅在该 Namespace 的全部 Revision 都已完成构建时才会原子切换，新入库随后写入新代。

发布和生命周期变更也只通过受控的本地管理员进程执行；`--actor` 只记录审计操作者，不能自选权限。不要将这些命令直接暴露给浏览器或公共 HTTP 接口。

```bash
python -m know_one publish game-a-cs <revision-id> 2026-09-29T09:00:00+08:00 \
  --expected-generation 0 --idempotency-key publish-001
python -m know_one withdraw game-a-cs <document-id> \
  --expected-generation 1 --idempotency-key withdraw-001
python -m know_one set-access game-a-cs <document-id> '{"principals":["operator"]}' \
  --expected-generation 2 --idempotency-key acl-001
python -m know_one delete game-a-cs <document-id> --idempotency-key delete-001
```

`delete` 会立即清理该 Document 的正文、Revision、Chunk 和 Publication，并保留最小删除审计记录。

## 文档入口

| 文档 | 负责回答的问题 |
|---|---|
| [术语表](CONTEXT.md) | Document、Revision、Publication、Chunk 的含义 |
| [架构](docs/architecture.md) | 运行形态、职责、数据关系、演进方式 |
| [接口契约](docs/contracts.md) | 权限、发布、幂等、时点查询、失败语义 |
| [流水线](docs/pipeline.md) | 解析、召回、过滤、重排和降级 |
| [切块策略](docs/chunking.md) | NLP 是否更好、默认规则、对照实验 |
| [运行保障](docs/operations.md) | 任务恢复、观测、容量、成本、备份 |
| [验收计划](docs/evaluation.md) | Golden Set、指标口径、上线门槛 |
| [架构决策](docs/adr/) | 关键取舍及原因 |

## 实施顺序

1. 真实样本与正确性基线：确认数据使用范围，构建人工标注集；实现版本模型、权限、原文定位和单库检索。
2. 受控试点：完成后台入库、发布撤回、结构切块、混合检索、可选重排；业务人员审核答案。
3. 商用放量：通过质量、权限、故障、性能、成本及恢复验收，再逐步扩大流量。
4. 按失败样例演进：语义切块、复杂格式、BM25 或独立搜索引擎，都需要对照评测支持。

默认参数是实验起点。业务数据、部署资源和流量尚未确定，不能据此承诺准确率、容量或响应时间。
