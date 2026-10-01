"""直接 Python PDF 提交示例的行为测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from know_one import IngestionJobRef


def _example_module():
    """按文件路径加载示例，避免把 examples 伪装成核心运行包。"""
    path = Path(__file__).parents[1] / "examples" / "submit_pdf.py"
    spec = importlib.util.spec_from_file_location("submit_pdf_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_submit_pdf_example_snapshots_the_selected_page_range(tmp_path, monkeypatch, capsys) -> None:
    """示例只提交任务，并把页码范围和 ingest 权限传给核心库。"""
    module = _example_module()
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-test")

    class FakeKnowOne:
        """隔离数据库，只验证直接 Python 调用的边界。"""

        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://test"

        def ingest(self, source, namespace: str, source_key: str, scope, idempotency_key: str):
            assert source.snapshot() == b"%PDF-test"
            assert source.describe() == {
                "source_name": "manual.pdf",
                "first_page": 92,
                "last_page": 102,
            }
            assert namespace == "manuals"
            assert source_key == "manual/rav4"
            assert scope.permissions == frozenset({"ingest"})
            assert idempotency_key == "import-1"
            return IngestionJobRef("job-1", namespace, "queued")

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "editor", "--source-key", "manual/rav4",
            "--pdf", str(pdf), "--first-page", "92", "--last-page", "102",
            "--idempotency-key", "import-1", "--dsn", "postgresql://test",
        ]
    ) == 0

    assert json.loads(capsys.readouterr().out) == {
        "job_id": "job-1", "namespace": "manuals", "status": "queued"
    }
