"""以直接 Python 调用方式检索 KnowOne Evidence。

从仓库根目录运行：
PYTHONPATH=. .venv/bin/python examples/query_evidence.py \\
  --namespace rav4-smoke-pdf --principal evaluator --query "油枪自动跳枪后还要继续加油吗？"
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from typing import Sequence

from know_one import AccessScope, KnowOne
from know_one.config import load_local_env
from know_one.errors import KnowOneError


def _parse_timestamp(value: str) -> datetime:
    """解析带时区的业务时点，避免按本机时区静默查询历史内容。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("时间必须是 ISO 8601 格式") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须携带时区")
    return parsed


def _parser() -> argparse.ArgumentParser:
    """构造直接检索 Evidence 的最小调用参数。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="直接调用 KnowOne.retrieve 并输出 Evidence JSON")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--principal", required=True, help="由业务系统认证后的可信身份")
    parser.add_argument("--query", required=True)
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    parser.add_argument("--at", type=_parse_timestamp, help="可选业务查询时点")
    parser.add_argument("--applicability", default="{}", help="产品等适用条件 JSON 对象")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--deadline-ms", type=int, default=3000)
    return parser


def _evidence_json(evidence) -> dict:
    """仅序列化可交给调用方生成层的 Evidence 与可追溯定位。"""
    return {
        "text": evidence.text,
        "document_id": evidence.document_id,
        "revision_id": evidence.revision_id,
        "chunk_id": evidence.chunk_id,
        "source_locator": evidence.source_locator,
        "heading_path": list(evidence.heading_path),
        "publication_valid_from": evidence.publication_valid_from.isoformat(),
        "publication_valid_until": (
            evidence.publication_valid_until.isoformat()
            if evidence.publication_valid_until is not None
            else None
        ),
        "rank_score": evidence.rank_score,
        "score_type": evidence.score_type,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """调用检索库；不生成答案，保留回答与拒答给业务调用方。"""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not arguments.dsn:
        parser.error("需要 --dsn 或 KNOWONE_DSN")
    if not arguments.principal.strip() or not arguments.query.strip():
        parser.error("principal 和 query 不能为空")
    try:
        applicability = json.loads(arguments.applicability)
    except json.JSONDecodeError as error:
        parser.error("applicability 必须是 JSON 对象")
    if not isinstance(applicability, dict):
        parser.error("applicability 必须是 JSON 对象")

    scope = AccessScope(arguments.principal, frozenset({arguments.namespace}), frozenset({"read"}))
    try:
        result = KnowOne(arguments.dsn).retrieve(
            arguments.query,
            arguments.namespace,
            scope,
            at=arguments.at,
            applicability=applicability,
            top_k=arguments.top_k,
            deadline_ms=arguments.deadline_ms,
        )
    except (RuntimeError, ValueError, KnowOneError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "evidence": [_evidence_json(evidence) for evidence in result.evidence],
                "index_generation": result.index_generation,
                "model_version": result.model_version,
                "trace_id": result.trace_id,
                "degraded": result.degraded,
                "warnings": list(result.warnings),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
