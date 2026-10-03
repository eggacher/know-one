"""文本层 PDF 来源与阅读顺序解析的单元测试。"""

from pathlib import Path
import subprocess

import pytest

from know_one import KnowOne
from know_one.ingestion import PdfSource


def test_pdf_source_snapshots_bytes_without_persisting_its_path(tmp_path: Path) -> None:
    """提交 PDF 时保存字节和文件名，worker 不依赖调用方路径仍可重放。"""
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-test")

    source = PdfSource(path)

    assert source.media_type == "application/pdf"
    assert source.snapshot() == b"%PDF-test"
    assert source.describe() == {"source_name": "manual.pdf"}


def test_pdf_source_persists_an_optional_page_range(tmp_path: Path) -> None:
    """页码范围从 1 开始，并成为可供重建重放的来源快照的一部分。"""
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-test")

    source = PdfSource(path, first_page=92, last_page=102)

    assert source.describe() == {
        "source_name": "manual.pdf",
        "first_page": 92,
        "last_page": 102,
    }
    with pytest.raises(ValueError, match="正整数"):
        PdfSource(path, first_page=0)
    with pytest.raises(ValueError, match="不能晚于"):
        PdfSource(path, first_page=102, last_page=92)


def test_pdf_parser_uses_raw_reading_order_and_records_page_locator(monkeypatch) -> None:
    """PDF 解析固定使用 -raw，Chunk 定位应保留对应的 1 起始页码。"""
    captured: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, "第一页内容。\f第二页内容。", "")

    monkeypatch.setattr("know_one.core.api.subprocess.run", run)

    text = KnowOne._source_text(
        b"%PDF-test", "application/pdf", {"first_page": 92, "last_page": 102}
    )
    chunks = KnowOne._chunks_for_media_type(text, "application/pdf")

    assert "-raw" in captured[0]
    assert captured[0][:6] == ["pdftotext", "-raw", "-f", "92", "-l", "102"]
    assert [chunk[0] for chunk in chunks] == ["第一页内容。", "第二页内容。"]
    second_start = text.index("第二页")
    assert KnowOne._source_locator(
        "application/pdf", text, second_start, len(text), {"first_page": 92}
    ) == {
        "char_start": second_start,
        "char_end": len(text),
        "page": 93,
    }


def test_pdf_chunking_ignores_page_headers_and_splits_long_sections() -> None:
    """孤立页眉不应入库；超长页面应在小节处拆分且保持连续定位。"""
    page = (
        "390 8-3. 初始化\n\n"
        + "跨接起动步骤。" * 100
        + "\n■12 伏蓄电池电量耗尽时起动混合动力系统\n"
        + "利用推车起动方式无法起动混合动力系统。"
    )

    chunks = KnowOne._chunks_for_media_type(page, "application/pdf")

    assert all(chunk[0] != "390 8-3. 初始化" for chunk in chunks)
    target = next(chunk for chunk in chunks if "利用推车起动方式" in chunk[0])
    assert target[0].startswith("■12 伏蓄电池")
    assert target[1:3] == (page.index("■12 伏蓄电池"), len(page))
