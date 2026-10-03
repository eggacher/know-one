"""检索 smoke 评估命令的公开行为测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from know_one import Evidence, RetrievalResult
from know_one.eval.smoke import load_cases, main


def test_hybrid_golden_draft_has_unique_parseable_cases() -> None:
    """RAV4 草案应保持为可运行且 ID 不重复的人工复核输入。"""
    path = Path(__file__).parents[1] / "examples" / "rav4" / "eval" / "hybrid_golden_draft.jsonl"

    cases = load_cases(path)

    assert len(cases) == 30
    assert len({case.identifier for case in cases}) == len(cases)


def test_smoke_cli_reports_each_recall_mode(
    tmp_path, capsys, monkeypatch
) -> None:
    """同一标注集应分别汇总全文、向量与混合召回的命中情况。"""
    dataset = tmp_path / "smoke.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "manual-001",
                "query": "如何保养",
                "expected_any": ["定期保养"],
                "expected_pages": [92],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeKnowOne:
        """根据诊断模式返回可观察的候选，用于隔离 CLI 报告逻辑。"""

        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://test"
            self.deadline_values: list[object] = []

        def retrieve(self, query: str, namespace: str, access_scope, **kwargs: object) -> RetrievalResult:
            assert query == "如何保养"
            assert namespace == "rav4"
            self.deadline_values.append(kwargs["deadline_ms"])
            assert kwargs["deadline_ms"] == 15_000
            mode = kwargs["recall_mode"]
            text = "定期\n保养说明" if mode != "vector" else "无关内容"
            evidence = Evidence(
                text=text,
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": len(text), "page": 92},
                publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                publication_valid_until=None,
            )
            return RetrievalResult(
                evidence=(evidence,),
                index_generation="generation-1",
                model_version="test-model",
                trace_id="trace-1",
            )

    monkeypatch.setattr("know_one.eval.smoke.KnowOne", FakeKnowOne)

    assert main(
        [
            "--dataset",
            str(dataset),
            "--namespace",
            "rav4",
            "--principal",
            "evaluator",
            "--dsn",
            "postgresql://test",
            "--deadline-ms",
            "15000",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["total_cases"] == 1
    assert report["modes"]["full_text"] == {"hits": 1, "hit_rate": 1.0, "miss_ids": []}
    assert report["modes"]["vector"] == {"hits": 0, "hit_rate": 0.0, "miss_ids": ["manual-001"]}
    assert report["modes"]["hybrid"] == {"hits": 1, "hit_rate": 1.0, "miss_ids": []}


def test_smoke_cli_can_include_evidence_for_missed_cases(
    tmp_path, capsys, monkeypatch
) -> None:
    """诊断开关只为漏检样本附带候选原文，默认报告不受影响。"""
    dataset = tmp_path / "smoke.jsonl"
    dataset.write_text(
        json.dumps({"id": "manual-001", "query": "如何保养", "expected_any": ["定期保养"]}) + "\n",
        encoding="utf-8",
    )

    class FakeKnowOne:
        """固定返回不命中的候选，验证诊断输出内容。"""

        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            text = "无关内容"
            return RetrievalResult(
                evidence=(
                    Evidence(
                        text=text,
                        document_id="document-1",
                        revision_id="revision-1",
                        chunk_id="chunk-1",
                        source_locator={"char_start": 0, "char_end": len(text)},
                        publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                        publication_valid_until=None,
                    ),
                ),
                index_generation="generation-1",
                model_version="test-model",
                trace_id="trace-1",
            )

    monkeypatch.setattr("know_one.eval.smoke.KnowOne", FakeKnowOne)

    assert main(
        [
            "--dataset", str(dataset), "--namespace", "rav4", "--principal", "evaluator",
            "--dsn", "postgresql://test", "--include-miss-evidence",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    for mode in ("full_text", "vector", "hybrid"):
        assert report["modes"][mode]["miss_evidence"] == {"manual-001": ["无关内容"]}


def test_smoke_cli_treats_a_matching_text_on_the_wrong_pdf_page_as_a_miss(
    tmp_path, capsys, monkeypatch
) -> None:
    """PDF 标注页码时，相同正文但错误的来源定位不能算作命中。"""
    dataset = tmp_path / "smoke.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "manual-001",
                "query": "如何保养",
                "expected_any": ["定期保养"],
                "expected_pages": [92],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeKnowOne:
        """返回正确正文但错误页码，覆盖引用定位回归。"""

        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            text = "定期保养说明"
            return RetrievalResult(
                evidence=(
                    Evidence(
                        text=text,
                        document_id="document-1",
                        revision_id="revision-1",
                        chunk_id="chunk-1",
                        source_locator={"char_start": 0, "char_end": len(text), "page": 93},
                        publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                        publication_valid_until=None,
                    ),
                ),
                index_generation="generation-1",
                model_version="test-model",
                trace_id="trace-1",
            )

    monkeypatch.setattr("know_one.eval.smoke.KnowOne", FakeKnowOne)

    assert main(
        [
            "--dataset", str(dataset), "--namespace", "rav4", "--principal", "evaluator",
            "--dsn", "postgresql://test",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    for mode in ("full_text", "vector", "hybrid"):
        assert report["modes"][mode]["miss_ids"] == ["manual-001"]
