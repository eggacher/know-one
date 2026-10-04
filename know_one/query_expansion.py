"""调用方查询扩展：把用户口语措辞映射到手册侧术语。

契约规定 query 口语补全由调用方完成（docs/contracts.md）；
本模块提供共享实现，供评测器与 answer 示例在检索前扩展查询：
命中词典的口语词后，把手册术语追加到查询原文尾部，全文与
向量两路各自消化追加内容，不生成额外子查询。
"""

import json
from pathlib import Path
from typing import Mapping, Sequence


def load_expansions(path: Path) -> dict[str, tuple[str, ...]]:
    """读取查询扩展词典 JSON（口语词 → 手册侧术语列表）。

    词典格式错误必须显式失败，不允许静默丢弃映射导致评测口径失真。
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取查询扩展词典 {path}") from error
    if (
        not isinstance(value, dict)
        or not value
        or not all(
            isinstance(key, str)
            and key.strip()
            and isinstance(synonyms, list)
            and synonyms
            and all(isinstance(text, str) and text.strip() for text in synonyms)
            for key, synonyms in value.items()
        )
    ):
        raise ValueError("词典必须是 {非空查询词: [非空手册术语, ...]} 的 JSON 对象")
    return {key: tuple(synonyms) for key, synonyms in value.items()}


def expand_query(query: str, expansions: Mapping[str, Sequence[str]] | None) -> str:
    """把命中的手册术语追加到查询尾部；原文与术语都保留，两路各自消化。"""
    if not expansions:
        return query
    appended = [term for key, synonyms in expansions.items() if key in query for term in synonyms]
    return f"{query} {' '.join(dict.fromkeys(appended))}" if appended else query
