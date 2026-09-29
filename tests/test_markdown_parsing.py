"""Markdown 解析边界的纯函数级测试。

完整入库链路（标题路径写入 chunk、检索返回 Evidence）由
tests/test_ingestion_postgres.py 在真实 PostgreSQL 环境验证；
本文件只覆盖解析器本身的边界行为，便于本地快速回归。
"""

from __future__ import annotations

from know_one.core.api import KnowOne


def test_hash_lines_inside_code_fences_do_not_become_headings() -> None:
    """围栏代码块内的 # 行是代码注释，不得改变其后续正文的标题路径。"""
    text = "# 安装指南\n步骤说明\n\n```bash\n# 修改配置文件\nexport FOO=1\n```\n\n后续正文\n"
    chunks = KnowOne._markdown_paragraphs(text)

    assert [(chunk[0], chunk[3]) for chunk in chunks] == [
        ("步骤说明", ("安装指南",)),
        # 围栏标记与围栏内容作为正文连续片段整体保留，便于证据回溯原文。
        ("```bash\n# 修改配置文件\nexport FOO=1\n```", ("安装指南",)),
        ("后续正文", ("安装指南",)),
    ]
    for raw_text, start, end, _ in chunks:
        assert text[start:end] == raw_text


def test_trailing_hash_without_leading_space_stays_in_title() -> None:
    """以 # 结尾的标题（如 C#、F#）不能被截断。

    CommonMark 规定：闭合 # 序列前必须有空白，否则 # 属于标题文字。
    """
    chunks = KnowOne._markdown_paragraphs("# 语言：C#\n内容A\n\n# 语言：F# 指南 #\n内容B\n")

    assert [(chunk[0], chunk[3]) for chunk in chunks] == [
        ("内容A", ("语言：C#",)),
        # 末尾前置空格的 # 序列是闭合标记，应被剥离。
        ("内容B", ("语言：F# 指南",)),
    ]


def test_unclosed_fence_runs_to_end_of_document() -> None:
    """未闭合围栏延伸到文档结束，期间的 # 行一律视为正文。"""
    text = "## 配置说明\n```\n# 不是标题\n正文收尾\n"
    chunks = KnowOne._markdown_paragraphs(text)

    assert [(chunk[0], chunk[3]) for chunk in chunks] == [
        ("```\n# 不是标题\n正文收尾", ("配置说明",)),
    ]


def test_setext_headings_stay_in_body_text() -> None:
    """Setext（下划线式）标题当前不解析，标题文字与下划线一起进入正文。

    这是有意保守的首版边界；若后续支持，应连同标题路径一并实现并更新本钉子。
    """
    text = "标题甲\n=====\n\n正文C\n"
    chunks = KnowOne._markdown_paragraphs(text)

    assert [(chunk[0], chunk[3]) for chunk in chunks] == [
        ("标题甲\n=====", ()),
        ("正文C", ()),
    ]


def test_lone_hash_line_is_body_not_empty_heading() -> None:
    """孤立的 # 行是 CommonMark 合法空标题，当前按正文保留。"""
    text = "#\n正文D\n"
    chunks = KnowOne._markdown_paragraphs(text)

    assert [(chunk[0], chunk[3]) for chunk in chunks] == [("#\n正文D", ())]
