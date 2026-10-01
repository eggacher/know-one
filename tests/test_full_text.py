"""中文全文检索分词与查询构造的单元测试。"""

from know_one.full_text import build_or_tsquery, tokenize_for_search


def test_tokenize_for_search_keeps_chinese_terms_and_code_tokens() -> None:
    """搜索模式应保留中文关键词，并把连字符型号规范为一个安全词项。"""
    tokens = tokenize_for_search("轮胎漏气时，请检查 RAV4-HEV。")

    assert "轮胎" in tokens
    assert "漏气" in tokens
    assert "RAV4_HEV" in tokens


def test_build_or_tsquery_does_not_make_natural_language_terms_mandatory() -> None:
    """自然语言问题的多个有效词项以 OR 连接，避免全文召回被虚词清零。"""
    tsquery = build_or_tsquery("轮胎漏气还能继续开吗？")

    assert "轮胎" in tsquery
    assert "漏气" in tsquery
    assert " | " in tsquery
