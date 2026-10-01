"""以直接 Python 调用方式提交文本层 PDF 入库任务。

从仓库根目录运行：
PYTHONPATH=. .venv/bin/python examples/submit_pdf.py \\
  --namespace rav4-smoke-pdf --principal editor --source-key manual/rav4 \\
  --pdf data/Rav4用户手册（汽油版）.pdf --first-page 92 --last-page 102 \\
  --idempotency-key rav4-pages-92-102-v1
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from know_one import AccessScope, KnowOne
from know_one.config import load_local_env
from know_one.errors import KnowOneError
from know_one.ingestion import PdfSource


def _parser() -> argparse.ArgumentParser:
    """构造 PDF 提交参数；发布另走受控生命周期命令。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="直接调用 KnowOne.ingest 提交文本层 PDF")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--principal", required=True, help="由业务系统认证后的可信编辑身份")
    parser.add_argument("--source-key", required=True, help="稳定来源标识，例如 manual/rav4-2026")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--first-page", type=int, help="可选起始页，页码从 1 开始")
    parser.add_argument("--last-page", type=int, help="可选结束页，页码从 1 开始")
    parser.add_argument("--idempotency-key", required=True, help="同一次业务提交重试时复用")
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """持久化 PDF 快照并输出任务引用；不等待构建或自动发布。"""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not arguments.dsn:
        parser.error("需要 --dsn 或 KNOWONE_DSN")
    if not arguments.principal.strip() or not arguments.source_key.strip():
        parser.error("principal 和 source-key 不能为空")
    if not arguments.pdf.is_file():
        parser.error(f"找不到 PDF：{arguments.pdf}")
    try:
        source = PdfSource(arguments.pdf, arguments.first_page, arguments.last_page)
        scope = AccessScope(
            arguments.principal,
            frozenset({arguments.namespace}),
            frozenset({"ingest"}),
        )
        job = KnowOne(arguments.dsn).ingest(
            source,
            arguments.namespace,
            arguments.source_key,
            scope,
            arguments.idempotency_key,
        )
    except (OSError, RuntimeError, ValueError, KnowOneError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {"job_id": job.job_id, "namespace": job.namespace, "status": job.status},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
