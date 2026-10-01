"""可持久化提交的文本层 PDF 来源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PdfSource:
    """把本地 PDF 文件快照为 worker 可重放的 ``application/pdf`` 来源。

    仅承诺文本层 PDF；扫描件会在 worker 解析阶段失败，不静默 OCR 或发布。
    """

    path: Path
    first_page: int | None = None
    last_page: int | None = None
    media_type: str = "application/pdf"

    def __post_init__(self) -> None:
        """拒绝无法明确解释的页码范围，页码与 pdftotext 一样从 1 开始。"""
        if not self._is_positive_page(self.first_page):
            raise ValueError("first_page 必须为正整数")
        if not self._is_positive_page(self.last_page):
            raise ValueError("last_page 必须为正整数")
        if self.first_page is not None and self.last_page is not None and self.first_page > self.last_page:
            raise ValueError("first_page 不能晚于 last_page")

    @staticmethod
    def _is_positive_page(page: int | None) -> bool:
        """``bool`` 虽是 ``int`` 子类，但不能作为有业务含义的页码。"""
        return page is None or (isinstance(page, int) and not isinstance(page, bool) and page > 0)

    def snapshot(self) -> bytes:
        """读取完整 PDF 字节，提交后 worker 不再依赖原始路径。"""
        return self.path.read_bytes()

    def describe(self) -> dict[str, str | int]:
        """记录仅供审计的文件名，不把本机绝对路径写入数据库。"""
        description: dict[str, str | int] = {"source_name": self.path.name}
        if self.first_page is not None:
            description["first_page"] = self.first_page
        if self.last_page is not None:
            description["last_page"] = self.last_page
        return description
