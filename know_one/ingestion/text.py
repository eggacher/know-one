"""首版可直接提交的纯文本来源。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSource:
    """把调用方已获得的 UTF-8 文本作为不可变入库来源。

    M1 只支持 ``text/plain``，因此不猜测文件格式，也不在这里做清洗。
    ``source_name`` 仅用于审计与排查，不参与 Document 身份；身份由
    ``ingest()`` 的 ``source_key`` 决定。
    """

    text: str
    source_name: str = "inline-text"
    media_type: str = "text/plain"

    def snapshot(self) -> bytes:
        """返回 UTF-8 编码的不可变原文快照。"""
        return self.text.encode("utf-8")

    def describe(self) -> dict[str, str]:
        """返回可存入 JSONB 的轻量来源描述。"""
        return {"source_name": self.source_name}
