"""提取 RAV4 小语料并以四个独立 Document 入库、发布。

从仓库根目录运行：
PYTHONPATH=. .venv/bin/python examples/rav4/scripts/ingest_smoke_corpus.py \\
  --namespace rav4-smoke --principal evaluator
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Sequence

from know_one import AccessScope, KnowOne
from know_one.config import load_local_env
from know_one.ingestion import PdfSource


REPO = Path(__file__).resolve().parents[3]
DEFAULT_PDF = REPO / "data" / "Rav4用户手册（汽油版）.pdf"
VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)

# 每段单独成为 Document，避免长手册在 smoke 评估中掩盖切块与排序问题。
CORPUS_PARTS = (
    ("keys-and-doors", 92, 102),
    ("wipers-and-fuel", 158, 162),
    ("fuses-and-bulbs", 348, 352),
    ("emergency-tire", 376, 383),
)


def _parser() -> argparse.ArgumentParser:
    """构造小语料导入命令行参数。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="导入并发布 RAV4 中文检索 smoke 小语料")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--principal", required=True, help="写入审计的本地评估身份")
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="本次导入批次号；仅在前次任务已失败后递增",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """在已有 Namespace 中处理四段文本，并发布为长期有效的评测资料。"""
    arguments = _parser().parse_args(argv)
    if not arguments.dsn:
        raise SystemExit("需要 --dsn 或 KNOWONE_DSN")
    if not arguments.principal.strip():
        raise SystemExit("principal 不能为空")
    if not arguments.pdf.is_file():
        raise SystemExit(f"找不到 PDF：{arguments.pdf}")
    if arguments.attempt <= 0:
        raise SystemExit("attempt 必须是正整数")

    scope = AccessScope(
        arguments.principal,
        frozenset({arguments.namespace}),
        frozenset({"ingest", "publish", "read"}),
    )
    know_one = KnowOne(arguments.dsn)
    for name, first_page, last_page in CORPUS_PARTS:
        # 让库保存原始 PDF 与页码范围；重建会重新使用 -raw 解析相同页段，
        # Evidence 也因此能指回手册的真实页码，而非脚本临时提取文本的偏移。
        source = PdfSource(arguments.pdf, first_page=first_page, last_page=last_page)
        job = know_one.ingest(
            source,
            arguments.namespace,
            f"rav4-smoke-{name}",
            scope,
            # 同一批次重放仍复用同一幂等键；已失败任务不能被 worker 重新领取时，
            # 操作者在排除外部依赖故障后才显式递增批次号创建新任务。
            f"rav4-smoke-ingest-{name}-attempt-{arguments.attempt}",
        )
        know_one.process_job(job.job_id)
        status = know_one.get_ingestion(job.job_id, scope)
        if status.revision_id is None:
            raise RuntimeError(f"{name} 未生成可发布 Revision")
        # 新语料使用独立 source_key；首次发布的 Document generation 固定为 0。
        know_one.publish(
            status.revision_id,
            arguments.namespace,
            VALID_FROM,
            None,
            0,
            scope,
            f"rav4-smoke-publish-{name}-v1",
        )
        print(f"已发布 {name}：{status.revision_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
