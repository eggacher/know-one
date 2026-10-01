"""直接 Python Evidence 查询示例的行为测试。"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

from know_one import Evidence, RetrievalResult


def _example_module():
    """按文件路径加载示例，避免把 examples 伪装成核心运行包。"""
    path = Path(__file__).parents[1] / "examples" / "query_evidence.py"
    spec = importlib.util.spec_from_file_location("query_evidence_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_evidence_example_serializes_evidence_without_generating_an_answer(
    monkeypatch, capsys
) -> None:
    """示例应传入可信读权限，并完整输出 Evidence 的引用字段。"""
    module = _example_module()

    class FakeKnowOne:
        """隔离数据库与模型端点，只验证调用参数和输出契约。"""

        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://test"

        def retrieve(self, query: str, namespace: str, scope, **kwargs: object) -> RetrievalResult:
            assert query == "怎么保养"
            assert namespace == "manual"
            assert scope.principal_id == "operator"
            assert scope.permissions == frozenset({"read"})
            assert kwargs["applicability"] == {"model": "rav4"}
            evidence = Evidence(
                text="请定期保养。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": 6, "page": 92},
                heading_path=("保养",),
                publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                publication_valid_until=None,
                rank_score=0.1,
            )
            return RetrievalResult(
                evidence=(evidence,),
                index_generation="generation-1",
                model_version="test-model",
                trace_id="trace-1",
            )

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)

    assert module.main(
        [
            "--namespace", "manual", "--principal", "operator", "--query", "怎么保养",
            "--dsn", "postgresql://test", "--applicability", '{"model":"rav4"}',
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["evidence"] == [
        {
            "chunk_id": "chunk-1",
            "document_id": "document-1",
            "heading_path": ["保养"],
            "publication_valid_from": "2026-09-01T00:00:00+00:00",
            "publication_valid_until": None,
            "rank_score": 0.1,
            "revision_id": "revision-1",
            "score_type": "rrf",
            "source_locator": {"char_end": 6, "char_start": 0, "page": 92},
            "text": "请定期保养。",
        }
    ]
