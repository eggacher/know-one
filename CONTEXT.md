# KnowOne

为智能客服、游戏客服、企业知识库等场景提供统一入库与检索能力的知识库业务模块。
本文件是术语表：只定义概念，不记录实现细节。

## Language

**Namespace**:
知识的隔离单位；一个调用方业务（如「游戏A客服」）拥有的独立知识空间。
_Avoid_: Tenant, Workspace, Space

**Document**:
由稳定来源标识识别的知识单元；内容修订不会改变其身份。
_Avoid_: Article, File, Page

**DocumentRevision**:
Document 的一次不可变内容修订，保留当时的原文及其来源快照。
_Avoid_: 用 Validity Window 代替内容版本

**Publication**:
将一个 DocumentRevision 在指定生效时间窗内提供给检索的发布记录。

**Chunk**:
检索的最小单元，属于唯一一个 DocumentRevision，能够定位到对应原文。
_Avoid_: Segment, Passage, Slice

**IndexGeneration**:
使用同一套解析、切块和向量化配置构建的检索索引代次。

**Evidence**:
检索返回的原文依据及其来源定位、内容修订和相关度信息。

**AccessScope**:
调用方根据当前身份授予的知识访问范围。

**Source**:
入库时脏数据的来源（文件、API 导出、工单记录等）。
_Avoid_: Connector, DataSource

**Ingestion**:
把 Source 变为可检索 Chunk 的流水线：解析、清洗、切块、向量化、存储。

**Retrieval**:
把自然语言 query 变为按相关度排序的 Chunk 列表的流水线：改写、混合检索、重排。

**Query Rewriting**:
检索前对 query 的规范化与扩展（如口语黑话 → 标准术语），是 KnowOne 的内部 stage，
不对外暴露。
_Avoid_: 与 Intent Routing 混用

**Intent Routing**:
对话系统对 query 的分类路由（FAQ / 转工单 / 转人工）。**KnowOne 明确不做**，
属于调用方的职责。
_Avoid_: 与 Query Rewriting 混用

**Validity Window**:
Publication 的业务生效时间区间，用来判断某时刻适用哪一份内容修订。
_Avoid_: Version, TTL

**Golden Set**:
由问题、检索条件和人工确认的原文依据或无答案标记组成的评测集合。
_Avoid_: Test Set, Benchmark Dataset
