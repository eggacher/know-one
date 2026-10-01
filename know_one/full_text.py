"""中文全文检索的应用侧分词与安全 tsquery 构造。"""

from __future__ import annotations

from collections.abc import Iterable
import re

import jieba


# 此版本同时进入 IndexGeneration 配置；调整模式、词典或规则必须创建新代并重建。
FULL_TEXT_CONFIG_VERSION = f"jieba-{jieba.__version__}-search-or-v1"
_CODE_PATTERN = re.compile(r"[A-Za-z]+[A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+")
_SAFE_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


def _natural_language_tokens(text: str) -> Iterable[str]:
    """以搜索模式切分自然语言；该模式会为长词补充可召回的短词。"""
    yield from jieba.cut_for_search(text, HMM=False)


def tokenize_for_search(text: str) -> tuple[str, ...]:
    """返回可安全传给 PostgreSQL ``simple`` 配置的稳定词项序列。

    连字符型号和错误码改写为下划线，避免 ``simple`` parser 将同一标识拆散；
    文档与 query 必须调用同一函数，原文 ``raw_text`` 不受影响。
    """
    tokens: list[str] = []
    cursor = 0
    for code in _CODE_PATTERN.finditer(text):
        tokens.extend(_valid_tokens(_natural_language_tokens(text[cursor : code.start()])))
        tokens.append(code.group().replace("-", "_"))
        cursor = code.end()
    tokens.extend(_valid_tokens(_natural_language_tokens(text[cursor:])))
    return tuple(tokens)


def _valid_tokens(tokens: Iterable[str]) -> Iterable[str]:
    """丢弃标点与空白，只保留不会改变 ``to_tsquery`` 语法的词项。"""
    for token in tokens:
        normalized = token.strip()
        if _SAFE_TOKEN_PATTERN.fullmatch(normalized):
            yield normalized


def tokenize_text(text: str) -> str:
    """把 token 流以空白连接，供 ``to_tsvector('simple', ...)`` 保留位置。"""
    return " ".join(tokenize_for_search(text))


def build_or_tsquery(query: str) -> str | None:
    """把自然语言 query 变成受控 OR tsquery；无有效词项时返回 ``None``。"""
    terms = tuple(dict.fromkeys(tokenize_for_search(query)))
    return " | ".join(terms) if terms else None
