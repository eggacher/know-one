# 存储起步使用 PostgreSQL + pgvector

首版将结构化状态、向量和中文分词后的原生全文检索放在 PostgreSQL，减少部署及发布一致性成本。关键词基线使用 tsvector、GIN 和 ts_rank_cd；它不是 BM25。需要 BM25 或独立搜索引擎时，以真实质量／性能瓶颈决定，评估扩展支持、运行成本和双存储一致性。

HNSW 没有“千万级以内够用”的通用保证；容量和召回取决于维度、硬件、并发、过滤选择性及更新负载。过滤查询要验证候选不足和执行计划，必要时使用迭代扫描、分区或精确搜索。[pgvector 官方说明](https://github.com/pgvector/pgvector#filtering)

storage 保持调用契约，但换存储仍需迁移、重建、验证和回滚。向量模型或切块策略变更通过 IndexGeneration 隔离，不能把“可替换”理解为只改配置。详细计划见 [运行保障](../operations.md)，原生全文排序语义见 [PostgreSQL 文档](https://www.postgresql.org/docs/current/textsearch-controls.html)。
