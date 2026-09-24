"""应用接口使用的稳定数据类型。

这些类型只描述数据形状；AccessScope 的真实性由宿主保证，约束见 docs/contracts.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping
from uuid import UUID


def _require_aware(value: datetime | None, name: str) -> None:
    """统一拒绝无时区时间，避免生效窗口在不同时区下产生歧义。"""
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AccessScope:
    """宿主根据当前身份计算的授权范围；库收到后仍须执行权限检查。"""

    principal_id: str
    namespaces: frozenset[str]
    operations: frozenset[str]
    group_ids: frozenset[str] = field(default_factory=frozenset)
    authorization_version: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class Source:
    """宿主提供的不可变来源引用；后台 worker 必须能在稍后重新读取。"""

    content_ref: str
    content_hash: str
    media_type: str
    source_snapshot: Mapping[str, str] = field(default_factory=dict)


class JobState(StrEnum):
    """入库任务状态；ready 表示构建完成，不等于已经发布。"""

    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class IngestionJobRef:
    job_id: UUID
    status: JobState


@dataclass(frozen=True, slots=True)
class IngestionStatus:
    job_id: UUID
    status: JobState
    stage: str | None = None
    completed_count: int = 0
    total_count: int | None = None
    attempt_count: int = 0
    error_code: str | None = None
    warnings: tuple[str, ...] = ()
    revision_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SourceLocator:
    """原文中的半开区间 [start, end)，可附页码和锚点。"""

    content_ref: str
    start: int
    end: int
    page: int | None = None
    anchor: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("source locator requires 0 <= start < end")


@dataclass(frozen=True, slots=True)
class EvidencePart:
    """单独定位的补充原文，避免把不连续内容伪装成一段引用。"""

    text: str
    source_locator: SourceLocator


@dataclass(frozen=True, slots=True)
class Evidence:
    """一条检索证据；text 是主原文，context_parts 各有独立定位。"""

    text: str
    document_id: UUID
    revision_id: UUID
    chunk_id: UUID
    source_locator: SourceLocator
    heading_path: tuple[str, ...] = ()
    context_parts: tuple[EvidencePart, ...] = ()
    warning_codes: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    rank_score: float | None = None
    score_type: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValueError("valid_from must precede valid_until")


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """检索结果；warnings 保留降级和证据不完整等机器可读信号。"""

    evidence: tuple[Evidence, ...]
    index_generation_id: UUID
    trace_id: str
    model_version: str | None = None
    degraded: bool = False
    warnings: tuple[str, ...] = ()
    stage_timings_ms: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """发布后的有效窗口和文档状态代数，供后续并发操作使用。"""

    publication_id: UUID
    document_id: UUID
    revision_id: UUID
    state_generation: int
    valid_from: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until is not None and self.valid_from >= self.valid_until:
            raise ValueError("valid_from must precede valid_until")
