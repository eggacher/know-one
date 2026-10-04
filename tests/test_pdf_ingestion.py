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


def test_pdf_chunking_splits_at_subheading_to_protect_table_semantics() -> None:
    """无空行分段的页面里，次级标题短行应开新块，避免表格被前置段污染。

    p160 实际形态：「■驾驶时的注意事项」正文后直接接「混合动力变速器」
    子标题与换挡表格，pdftotext 不输出空行导致整片成一个段落；
    块的嵌入语义被注意事项主导（transmission-001 病历）。
    """
    page = (
        "■驾驶时的注意事项\n"
        "在 EV 驱动模式下驾驶时，应特别注意车辆周围的区域。由于无发动机\n"
        "噪音，行人、骑自行车的人或周围的其他车辆和人员可能不会注意到\n"
        "车辆在起步或正在靠近，因此驾驶时应特别注意。\n"
        "混合动力变速器\n"
        "请根据用途和具体情况选择档位。\n"
        "档位 目的或功能\n"
        "P 驻车 / 起动混合动力系统\n"
        "R 倒车\n"
        "N 空档\n"
        "D 正常驾驶*1\n"
        "S S 模式驾驶*2"
    )

    chunks = KnowOne._chunks_for_media_type(page, "application/pdf")
    texts = [chunk[0] for chunk in chunks]

    notice = next(text for text in texts if "■驾驶时的注意事项" in text)
    table = next(text for text in texts if "档位 目的或功能" in text)
    # 表格块以次级标题开头，不再携带「驾驶注意事项」语义。
    assert table.startswith("混合动力变速器")
    assert "■驾驶时的注意事项" not in table
    assert "注意事项" not in notice or "目的或功能" not in notice
    # 表格数据行保持完整（含空白的行不触发分块）。
    assert "R 倒车" in table and "S S 模式驾驶*2" in table
    # 切分保持字符区间连续且可回查原文。
    starts = sorted(chunk[1] for chunk in chunks)
    ends = sorted(chunk[2] for chunk in chunks)
    assert starts[0] == 0 and ends[-1] == len(page)
    assert all(a == b for a, b in zip(ends, starts[1:]))


def test_pdf_chunking_keeps_plain_prose_unsplit_without_subheadings() -> None:
    """普通短行（含空白的数据行、纯数字页码）不触发分块，防止误切。"""
    page = (
        "灯泡规格表如下。\n"
        "前照灯（远光） LED\n"
        "前照灯（近光） LED\n"
        "制动灯 LED\n"
        "161\n"
        "后续说明正文。"
    )

    chunks = KnowOne._chunks_for_media_type(page, "application/pdf")

    # 无纯词次级标题短行 → 整段保持一块。
    assert [chunk[0] for chunk in chunks] [0] == page
