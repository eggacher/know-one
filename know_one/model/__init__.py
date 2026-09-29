"""领域模型：接口出入参的数据结构。

架构约定（docs/architecture.md「模块与依赖」）：core 提供业务接口，
本包只定义领域数据，不包含行为；存储表结构见 docs/architecture.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


# 操作权限清单：read、ingest、publish、withdraw、manage_acl、delete 分别授权
# （contracts「共同约束」）。
PERMISSIONS = frozenset(
    {"read", "ingest", "publish", "withdraw", "manage_acl", "delete"}
)


@dataclass(frozen=True)
class AccessScope:
    """调用方根据当前身份授予的知识访问范围。

    由宿主后端构造并保证新鲜度；库不承担用户认证，只执行授权条件
    （contracts「共同约束」）。浏览器或终端用户不得直接提交自声明的 scope。
    """

    principal_id: str
    """操作者身份，写入 operation_receipt / audit_event 的 actor。"""

    namespaces: frozenset[str]
    """允许访问的 Namespace name 集合；空集表示无任何 Namespace 访问权。"""

    permissions: frozenset[str] = frozenset()
    """授予的操作权限，取值见 PERMISSIONS；各操作分别检查。"""

    def allows(self, namespace: str, permission: str) -> bool:
        """scope 是否允许对某 Namespace 执行某操作。"""
        return namespace in self.namespaces and permission in self.permissions


@dataclass(frozen=True)
class IngestionJobRef:
    """ingest() 的返回值：持久化任务引用。

    对应 ingestion_job 表一行；不承诺调用返回时内容已可检索。
    """

    job_id: str
    """任务 ID（ingestion_job.id，UUID）。"""

    namespace: str
    """目标 Namespace name。"""

    status: str
    """提交时的任务状态，通常为 queued；复用已有 revision 时可能直接 ready。"""


@dataclass(frozen=True)
class IngestionStatus:
    """get_ingestion() 的返回值：任务阶段与结果（contracts「接口字段速查」）。"""

    job_id: str
    namespace: str
    status: str
    """queued / running / ready / failed / needs_review / cancelled。"""

    stage: str | None = None
    """当前处理阶段：parsing / cleaning / chunking / embedding / validating 等。"""

    progress_done: int = 0
    """已完成计数（如已写入 chunk 数），配合 progress_total 展示进度。"""

    progress_total: int | None = None
    """预期总数；未知阶段（如解析前）为 None。"""

    error_code: str | None = None
    """失败或需人工介入的错误码；正常时为 None。"""

    attempt_count: int = 0
    """已尝试次数；worker 重试会递增。"""

    warnings: tuple[str, ...] = ()
    """非致命问题（如解析降级、tokenizer 近似计数）。"""

    revision_id: str | None = None
    """成功构建的 DocumentRevision ID；ready 之前为空。"""

    preview_url: str | None = None
    """可预览来源（同样需要鉴权后访问）。"""


@dataclass(frozen=True)
class Evidence:
    """检索命中的可引用原文及其不可变定位。"""

    text: str
    document_id: str
    revision_id: str
    chunk_id: str
    source_locator: dict
    publication_valid_from: datetime
    publication_valid_until: datetime | None
    heading_path: tuple[str, ...] = ()
    rank_score: float = 0.0
    score_type: str = "rrf"


@dataclass(frozen=True)
class RetrievalResult:
    """一次受约束检索的结果；空 evidence 不表示知识库不存在答案。"""

    evidence: tuple[Evidence, ...]
    index_generation: str
    model_version: str
    trace_id: str
    degraded: bool = False
    warnings: tuple[str, ...] = ()


class Source(Protocol):
    """入库来源协议（contracts：source 必须能被 worker 持久读取）。

    库在 ingest() 提交时调用 snapshot() 保存不可变快照（含内容摘要），
    不接受只有临时文件路径的来源。实现方保证多次 snapshot() 返回
    字节一致的内容，否则视为不同请求指纹。
    """

    media_type: str
    """来源媒体类型，如 application/pdf；决定使用哪个解析器。"""

    def snapshot(self) -> bytes:
        """返回完整的来源内容字节；worker 将据此计算 content_hash 并存原文。"""
        ...

    def describe(self) -> dict:
        """返回来源快照元数据（URL、抓取时间、账号等），存入
        document_revision.source_snapshot (jsonb)。"""
        ...
