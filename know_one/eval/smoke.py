"""面向小规模人工标注集的检索 smoke 评估命令。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from know_one import AccessScope, KnowOne
from know_one.config import load_local_env
from know_one.errors import KnowOneError


RECALL_MODES = ("full_text", "vector", "hybrid")


def load_expansions(path: Path) -> dict[str, tuple[str, ...]]:
    """读取调用方查询扩展词典（口语词 → 手册侧术语）。

    契约规定 query 口语补全由调用方完成；词典把用户措辞映射到
    手册术语，评测器在检索前把命中术语追加进查询文本。
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
        raise ValueError("词典必须是 {{非空查询词: [非空手册术语, ...]}} 的 JSON 对象")
    return {key: tuple(synonyms) for key, synonyms in value.items()}


def _expand_query(query: str, expansions: Mapping[str, Sequence[str]] | None) -> str:
    """把命中的手册术语追加到查询尾部；原文与术语都保留，两路各自消化。"""
    if not expansions:
        return query
    appended = [term for key, synonyms in expansions.items() if key in query for term in synonyms]
    return f"{query} {' '.join(dict.fromkeys(appended))}" if appended else query


@dataclass(frozen=True)
class SmokeCase:
    """一条人工确认的查询、可接受原文片段与可选引用页码。

    expected_context_any 用于跨块答案：主证据只锚定题干所在段落时，
    答案原文必须出现在同一条主证据的补充上下文（context_parts）里，
    两个条件同时满足才算命中，避免借页码邻近放宽判定。
    """

    identifier: str
    query: str
    expected_any: tuple[str, ...]
    expected_pages: tuple[int, ...] | None
    expected_context_any: tuple[str, ...] | None = None


def _matches_expected(expected: str, evidence_text: str) -> bool:
    """比较原文片段时忽略空白，适配 PDF 提取造成的中文硬换行。"""
    return "".join(expected.split()) in "".join(evidence_text.split())


def load_cases(path: Path) -> tuple[SmokeCase, ...]:
    """读取 JSONL 标注集，并在运行检索前拒绝无法判定的样本。"""
    cases: list[SmokeCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            identifier = value["id"]
            query = value["query"]
            expected_any = value["expected_any"]
            expected_pages = value.get("expected_pages")
            expected_context_any = value.get("expected_context_any")
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(f"第 {line_number} 行不是有效的 smoke 样本") from error
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(expected_any, list)
            or not expected_any
            or not all(isinstance(text, str) and text.strip() for text in expected_any)
            or (
                expected_context_any is not None
                and (
                    not isinstance(expected_context_any, list)
                    or not expected_context_any
                    or not all(
                        isinstance(text, str) and text.strip()
                        for text in expected_context_any
                    )
                )
            )
            or (
                expected_pages is not None
                and (
                    not isinstance(expected_pages, list)
                    or not expected_pages
                    or not all(
                        isinstance(page, int) and not isinstance(page, bool) and page > 0
                        for page in expected_pages
                    )
                )
            )
        ):
            raise ValueError(f"第 {line_number} 行的 id、query、expected_any 和 expected_pages 无效")
        cases.append(
            SmokeCase(
                identifier,
                query,
                tuple(expected_any),
                tuple(expected_pages) if expected_pages is not None else None,
                tuple(expected_context_any) if expected_context_any is not None else None,
            )
        )
    if not cases:
        raise ValueError("标注集至少需要一条样本")
    return tuple(cases)


def _matches_case(case: SmokeCase, evidence: Evidence) -> bool:
    """匹配正文后按需校验页码与跨块答案，避免相似段落掩盖错误引用。"""
    if not any(_matches_expected(expected, evidence.text) for expected in case.expected_any):
        return False
    if case.expected_pages is not None and evidence.source_locator.get("page") not in case.expected_pages:
        return False
    if case.expected_context_any is not None:
        # 跨块答案：锚定主证据后，其补充上下文必须带出答案原文。
        part_texts = tuple(part.text for part in evidence.context_parts)
        if not any(
            _matches_expected(expected, part_text)
            for expected in case.expected_context_any
            for part_text in part_texts
        ):
            return False
    return True


def evaluate(
    know_one: KnowOne,
    cases: Sequence[SmokeCase],
    namespace: str,
    scope: AccessScope,
    *,
    at: datetime | None,
    top_k: int,
    deadline_ms: int,
    include_miss_evidence: bool = False,
    expansions: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    """逐路执行受约束检索，返回可审阅的命中率和漏检样本 ID。"""
    # 跨块答案需要补充上下文才能判定；主证据排序与数量不受其影响。
    needs_context = any(case.expected_context_any is not None for case in cases)
    expanded_queries = {
        case.identifier: _expand_query(case.query, expansions) for case in cases
    }
    expansions_applied = sum(
        expanded_queries[case.identifier] != case.query for case in cases
    )
    modes: dict[str, dict] = {}
    for recall_mode in RECALL_MODES:
        miss_ids: list[str] = []
        miss_evidence: dict[str, list[str]] = {}
        for case in cases:
            result = know_one.retrieve(
                expanded_queries[case.identifier],
                namespace,
                scope,
                at=at,
                top_k=top_k,
                deadline_ms=deadline_ms,
                recall_mode=recall_mode,
                include_context=needs_context,
            )
            evidence_texts = tuple(evidence.text for evidence in result.evidence)
            if not any(_matches_case(case, evidence) for evidence in result.evidence):
                miss_ids.append(case.identifier)
                if include_miss_evidence:
                    # 仅输出已经判定为漏检的候选，避免常规评测报告重复整批原文。
                    miss_evidence[case.identifier] = list(evidence_texts)
        hits = len(cases) - len(miss_ids)
        modes[recall_mode] = {
            "hits": hits,
            "hit_rate": hits / len(cases),
            "miss_ids": miss_ids,
        }
        if include_miss_evidence:
            modes[recall_mode]["miss_evidence"] = miss_evidence
    return {
        "total_cases": len(cases),
        "modes": modes,
        "expansions_applied": expansions_applied,
    }


def _parse_timestamp(value: str) -> datetime:
    """解析带时区的评估业务时刻，避免按本机时区静默解释。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("时间必须是 ISO 8601 格式") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须携带时区")
    return parsed


def _parser() -> argparse.ArgumentParser:
    """构造 smoke 评估命令行参数。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="KnowOne 中文检索 smoke 评估")
    parser.add_argument("--dataset", type=Path, required=True, help="JSONL 人工标注集")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--principal", required=True, help="具有目标 Namespace read 权限的评估身份")
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    parser.add_argument("--at", type=_parse_timestamp, help="按指定业务时刻检索")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--deadline-ms",
        type=int,
        default=3000,
        help="单次检索总超时；评测远程 embedding 时可适当增大",
    )
    parser.add_argument(
        "--include-miss-evidence",
        action="store_true",
        help="为漏检样本附带本次返回的候选原文，供排序诊断使用",
    )
    parser.add_argument(
        "--expansions",
        type=Path,
        help="调用方查询扩展词典 JSON（口语词 → 手册术语），检索前追加进查询",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行评估并把 JSON 汇总写到标准输出，方便保存为版本化报告。"""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not arguments.dsn:
        parser.error("需要 --dsn 或 KNOWONE_DSN")
    if not arguments.principal.strip():
        parser.error("principal 不能为空")
    if arguments.deadline_ms <= 0:
        parser.error("deadline-ms 必须大于 0")
    try:
        cases = load_cases(arguments.dataset)
        expansions = load_expansions(arguments.expansions) if arguments.expansions else None
        scope = AccessScope(
            arguments.principal,
            frozenset({arguments.namespace}),
            frozenset({"read"}),
        )
        report = evaluate(
            KnowOne(arguments.dsn),
            cases,
            arguments.namespace,
            scope,
            at=arguments.at,
            top_k=arguments.top_k,
            deadline_ms=arguments.deadline_ms,
            include_miss_evidence=arguments.include_miss_evidence,
            expansions=expansions,
        )
    except (OSError, RuntimeError, ValueError, KnowOneError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
