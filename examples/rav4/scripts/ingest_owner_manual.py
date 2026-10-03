"""将一整本有文本层的 RAV4 用户手册入库并发布到车型专属 Namespace。"""

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
VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
# 用独立 Namespace 隔离车型，避免同一问题在汽油／混动条款间错误融合。
MANUALS = {
    "gasoline": (REPO / "data" / "Rav4用户手册（汽油版）.pdf", "rav4-owner-manual-gasoline-2019"),
    "hybrid": (REPO / "data" / "RAV4HEV用户手册（混动版）.pdf", "rav4-owner-manual-hybrid-2019"),
}


def _parser() -> argparse.ArgumentParser:
    """构造整本用户手册导入参数；扫描件和表格手册不走当前文本层管线。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="导入并发布整本 RAV4 汽油／混动用户手册")
    parser.add_argument("--variant", choices=tuple(MANUALS), required=True)
    parser.add_argument("--namespace", required=True, help="车型专属 Namespace，须事先创建")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="仅在前次任务 failed 后递增；同批次重跑仍使用相同幂等键",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """创建快照、同步处理、再将首次 Revision 发布为长期有效版本。"""
    arguments = _parser().parse_args(argv)
    if not arguments.dsn:
        raise SystemExit("需要 --dsn 或 KNOWONE_DSN")
    if not arguments.principal.strip():
        raise SystemExit("principal 不能为空")
    if arguments.attempt <= 0:
        raise SystemExit("attempt 必须是正整数")
    pdf, source_key = MANUALS[arguments.variant]
    if not pdf.is_file():
        raise SystemExit(f"找不到 PDF：{pdf}")

    scope = AccessScope(
        arguments.principal,
        frozenset({arguments.namespace}),
        frozenset({"ingest", "publish", "read"}),
    )
    know_one = KnowOne(arguments.dsn)
    job = know_one.ingest(
        PdfSource(pdf),
        arguments.namespace,
        source_key,
        scope,
        f"{source_key}-ingest-attempt-{arguments.attempt}",
    )
    status = know_one.get_ingestion(job.job_id, scope)
    if status.status == "queued":
        know_one.process_job(job.job_id)
        status = know_one.get_ingestion(job.job_id, scope)
    if status.status != "ready" or status.revision_id is None:
        raise RuntimeError(f"入库未完成：status={status.status} error_code={status.error_code}")
    know_one.publish(
        status.revision_id,
        arguments.namespace,
        VALID_FROM,
        None,
        0,
        scope,
        f"{source_key}-publish-v1",
    )
    print(f"已发布 {arguments.variant} 用户手册：{status.revision_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
