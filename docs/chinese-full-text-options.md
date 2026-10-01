# 中文全文检索方案调研

状态：2026-10-01 调研结论。本文只比较当前 PostgreSQL `tsvector` 路径的中文分词实现；不比较独立搜索服务或 BM25。

## 结论

默认选择**应用侧中文分词 + PostgreSQL `simple` 配置**，不将 PostgreSQL 扩展作为 KnowOne 的必要依赖。分词器的具体选型、版本、领域词典和 query 词项组合策略必须冻结到新的 `IndexGeneration`，并以 RAV4 smoke 集及后续真实 Golden Set 验收。

`zhparser` 可作为“数据库由团队自行运维、可安装原生扩展并能持续管理服务端词典”的可选部署方案；它不适合作为本项目默认前提。当前评测中 `full_text` 为 0/25 的原因是代码将中文原文和 query 都交给 `to_tsvector('simple', ...)` / `plainto_tsquery('simple', ...)`，并非 GIN 索引本身失效。

## 共同约束

PostgreSQL 的全文配置决定 parser、词典以及哪些 token 被索引；`to_tsvector` 用该配置把文档转为含位置的 `tsvector`，query 也必须按相同配置转为 `tsquery`。因此不能只替换入库端或只替换查询端的分词。[PostgreSQL：Controlling Text Search](https://www.postgresql.org/docs/current/textsearch-controls.html)

项目的 `chunk.tsv` 是持久列，`chunk_fts_idx` 是其 GIN 索引。GIN 是 PostgreSQL 推荐的全文索引类型，但它索引的是已经写入 `tsvector` 的 lexeme；改了分词规则不会改变历史行。必须在隔离的新 `IndexGeneration` 中重新构建全部 Revision 的 `tsv`，验收后再切换。单纯 `REINDEX` 只能重建索引结构，不能把旧 lexeme 重新分词；若另有维护原因需要重建生产索引，应理解普通 `REINDEX` 会阻塞写入，而 `REINDEX CONCURRENTLY` 有额外扫描、耗时和权限限制。[PostgreSQL：全文表和索引](https://www.postgresql.org/docs/current/textsearch-tables.html)；[PostgreSQL：REINDEX](https://www.postgresql.org/docs/current/sql-reindex.html)

`plainto_tsquery` 会把存活词项以 `&` 连接，也就是“所有词都必须出现”。中文 query 经分词后常有多个词，不能默认接受这一零召回风险；必须通过受控构造的 `tsquery` 明确 OR、短语或最小匹配策略。不能把原始用户输入拼为 `to_tsquery` 语法；该函数要求输入本来就是含运算符的合法 `tsquery`。[PostgreSQL：查询解析](https://www.postgresql.org/docs/current/textsearch-controls.html#TEXTSEARCH-PARSING-QUERIES)

## 方案一：应用侧中文分词 + `simple`（推荐）

入库和 query 由同一、版本固定的中文 tokenizer 产生 token；入库端将 token 用空白连接后传给 `to_tsvector('simple', tokenized_text)`，查询端以同一 token 流安全构造 `tsquery`。`to_tsvector` 会保留词项位置，故继续兼容现有 `ts_rank_cd` 及未来的短语检索。[PostgreSQL：全文函数](https://www.postgresql.org/docs/current/functions-textsearch.html)

优点：不需要数据库超级用户、C 编译工具链或 PostgreSQL 镜像定制；分词器和领域词典可随应用代码、测试和 `IndexGeneration` 一起版本化；对本机 PostgreSQL 和大多数托管 PostgreSQL 都是同一实现。它也符合项目既有设计目标：[流水线](pipeline.md) 已规定“应用侧分词后生成带位置的 tsvector”。

代价与边界：项目会新增一个受版本锁定的 tokenizer 依赖，并须定义词典更新、token 上限、异常处理及 query 的 OR/AND 策略。不要以 `array_to_tsvector` 替代上述路径：该函数把数组元素原样当 lexeme，官方接口没有把它定义为保留原文本位置的 `to_tsvector` 等价物；本项目需要位置来维持排序与未来短语语义。[PostgreSQL：`array_to_tsvector` 与 `to_tsvector`](https://www.postgresql.org/docs/current/functions-textsearch.html)

实施检查：先为现有 25 条 smoke 样本补充分词单测；再将分词器名称、版本、词典版本和 query 策略写入索引代配置；为每个历史 Revision 重建 `tsv` 和 embedding 所在的新代；切换后分别比较 `full_text`、`vector`、`hybrid`。目标不是只让全文有结果，而是让混合结果相对当前向量 21/25 的基线有可验证提升。

## 方案二：`zhparser` PostgreSQL 扩展（可选）

`zhparser` 是维护者提供的、基于 SCWS 的中文 PostgreSQL parser。其 README 的最小配置是：

```sql
CREATE EXTENSION zhparser;
CREATE TEXT SEARCH CONFIGURATION knowone_zh (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION knowone_zh
  ADD MAPPING FOR n, v, a, i, e, l WITH simple;
```

随后文档和 query 要统一使用 `knowone_zh`，例如 `to_tsvector('knowone_zh', search_text)` 与 `plainto_tsquery('knowone_zh', query)`；自然语言 query 的词项组合策略仍须由项目明确，不能把 README 中的 `to_tsquery` 演示当作用户输入接口。[zhparser 维护者 README：用法与示例](https://github.com/amutu/zhparser#readme)

部署代价高于 SQL 迁移：维护者的安装说明要求先安装并编译 SCWS、再编译安装 zhparser；服务器需有匹配的 PostgreSQL 开发文件，多个 PostgreSQL 版本时还需用对应的 `PG_CONFIG` 编译，最后由超级用户执行 `CREATE EXTENSION`。[zhparser：安装说明](https://github.com/amutu/zhparser#install)

它还把领域词典带入数据库运维面：额外词典存放于 PostgreSQL 的 `share/tsearch_data`，`extra_dicts` 与 `dict_in_memory` 要在 backend 启动前设定，改动后要 reload 并新建连接；其数据库级自定义词典流程需要超级用户，并要求同步后断开重连。[zhparser：配置与自定义词库](https://github.com/amutu/zhparser#configuration)

适用条件：团队拥有 PostgreSQL 容器/主机和升级窗口，且接受 SCWS、扩展二进制与服务端词典的版本管理；部署平台明确提供匹配版本的扩展。即使满足这些条件，仍要用新 `IndexGeneration` 全量重建并通过同一评测集后才能激活。不得在运行中的旧代直接混写不同 parser 产生的 `tsv`。

## 推荐落地顺序

1. 新增应用侧 tokenizer 边界及固定版本配置，不改权限、发布、时效和 RRF 契约。
2. 对 token 流、受控 `tsquery` 构造和 RAV4 漏检样本先写测试；明确 query 的组合规则。
3. 建立新索引代并全量重建，分别复跑三路 smoke；保留旧代，以便结果不达标时原子回退。
4. 仅当应用侧质量或运维成本经评测证明不足，并且部署方确认能长期承担扩展运维时，再做 `zhparser` 的独立对照实验。

