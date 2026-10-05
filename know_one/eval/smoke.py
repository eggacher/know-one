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
from know_one.query_expansion import expand_query, load_expansions
from know_one.rerank import RerankConfig, Reranker


RECALL_MODES = ("full_text", "vector", "hybrid")


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
    reranker: Reranker | None = None,
) -> dict:
    """逐路执行受约束检索，返回可审阅的命中率和漏检样本 ID。

    传入 reranker 时，每路额外产生 ``{mode}+rerank`` 模式：检索取更深的
    候选池（rerank 深度），重排后只截回 top_k 判定——模拟真实部署中
    「融合 top-16 → 重排 → 取 top-8」的组合层行为。
    """
    # 跨块答案需要补充上下文才能判定；主证据排序与数量不受其影响。
    needs_context = any(case.expected_context_any is not None for case in cases)
    expanded_queries = {
        case.identifier: expand_query(case.query, expansions) for case in cases
    }
    expansions_applied = sum(
        expanded_queries[case.identifier] != case.query for case in cases
    )
    modes: dict[str, dict] = {}
    for recall_mode in RECALL_MODES:
        # rerank 层需要更深的候选池；普通模式维持调用方 top_k。
        retrieve_k = max(top_k, reranker.max_candidates) if reranker else top_k
        # 未提供 reranker 时只跑基础层；提供时基础层与 rerank 层并列报告。
        layers: list[tuple[str, Reranker | None]] = [("", None)]
        if reranker is not None:
            layers.append(("+rerank", reranker))
        for suffix, rerank_layer in layers:
            miss_ids: list[str] = []
            miss_evidence: dict[str, list[str]] = {}
            degraded_count = 0
            for case in cases:
                result = know_one.retrieve(
                    expanded_queries[case.identifier],
                    namespace,
                    scope,
                    at=at,
                    top_k=retrieve_k,
                    deadline_ms=deadline_ms,
                    recall_mode=recall_mode,
                    include_context=needs_context,
                )
                if rerank_layer is not None:
                    outcome = rerank_layer.rerank(
                        expanded_queries[case.identifier], result.evidence
                    )
                    degraded_count += int(outcome.degraded)
                    # 两个层级都只看前 top_k：基础层不因深池放水（候选深度
                    # 仅为 rerank 提供输入），rerank 层在重排后截取头部。
                    ranked = outcome.evidence[:top_k]
                else:
                    ranked = result.evidence[:top_k]
                if not any(_matches_case(case, evidence) for evidence in ranked):
                    miss_ids.append(case.identifier)
                    if include_miss_evidence:
                        # 仅输出已经判定为漏检的候选，避免常规评测报告重复整批原文。
                        miss_evidence[case.identifier] = [
                            evidence.text for evidence in ranked[:top_k]
                        ]
            hits = len(cases) - len(miss_ids)
            mode_key = f"{recall_mode}{suffix}"
            modes[mode_key] = {
                "hits": hits,
                "hit_rate": hits / len(cases),
                "miss_ids": miss_ids,
            }
            if suffix:
                modes[mode_key]["degraded"] = degraded_count
            if include_miss_evidence:
                modes[mode_key]["miss_evidence"] = miss_evidence
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
    parser.add_argument(
        "--rerank-base-url",
        help="提供时为每路追加 rerank 层（OpenAI 兼容 /chat/completions），如 http://host:1234/v1",
    )
    parser.add_argument(
        "--rerank-model",
        default="qwen3.5-9b",
        help="rerank 层使用的 LLM 模型名",
    )
    parser.add_argument(
        "--rerank-timeout-seconds",
        type=float,
        default=180.0,
        help="单次 rerank LLM 调用超时；超时降级为 RRF 顺序",
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
        reranker = (
            Reranker(
                RerankConfig(
                    base_url=arguments.rerank_base_url,
                    model=arguments.rerank_model,
                    timeout_seconds=arguments.rerank_timeout_seconds,
                )
            )
            if arguments.rerank_base_url
            else None
        )
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
            reranker=reranker,
        )
    except (OSError, RuntimeError, ValueError, KnowOneError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
