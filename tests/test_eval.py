"""检索 smoke 评估命令的公开行为测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from know_one import ContextPart, Evidence, RetrievalResult
from know_one.eval.smoke import load_cases, main
from know_one.query_expansion import load_expansions


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


def test_smoke_cli_accepts_cross_chunk_answer_via_context_parts(
    tmp_path, capsys, monkeypatch
) -> None:
    """跨块答案：主证据锚定后，答案原文允许由 context_parts 补全。

    expected_context_any 是附加条件：主证据必须命中锚文本与页码，
    且同一条主证据的补充上下文必须带出答案原文，二者缺一即判漏检。
    """

    dataset = tmp_path / "cross.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "cross-001",
                "query": "电子钥匙换什么型号电池？",
                "expected_any": ["解锁并取出机械钥匙"],
                "expected_pages": [315],
                "expected_context_any": ["锂电池 CR2032"],
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "id": "strict-002",
                "query": "电子钥匙换什么型号电池？",
                "expected_any": ["解锁并取出机械钥匙"],
                "expected_pages": [315],
                # 上下文必须真的带出答案，否则不允许借页码邻近蒙混过关。
                "expected_context_any": ["原文不存在的句子"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeKnowOne:
        """主证据锚定更换步骤，答案型号位于相邻补充原文。"""

        instances: list["FakeKnowOne"] = []

        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://test"
            self.include_context_values: list[object] = []
            type(self).instances.append(self)

        def retrieve(self, query: str, namespace: str, access_scope, **kwargs: object) -> RetrievalResult:
            self.include_context_values.append(kwargs.get("include_context"))
            main_evidence = Evidence(
                text="更换电池\n1 解锁并取出机械钥匙。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": 20, "page": 315},
                publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                publication_valid_until=None,
                context_parts=(
                    ContextPart(
                        text="准备下列物品：\n锂电池 CR2032",
                        chunk_id="chunk-0",
                        source_locator={"char_start": 0, "char_end": 10, "page": 314},
                    ),
                ),
            )
            return RetrievalResult(
                evidence=(main_evidence,),
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

    fake_report = json.loads(capsys.readouterr().out)
    for mode in ("full_text", "vector", "hybrid"):
        assert fake_report["modes"][mode]["miss_ids"] == ["strict-002"]
        assert fake_report["modes"][mode]["hits"] == 1
    # 只要数据集出现跨块答案要求，检索就必须附带补充上下文。
    assert set(FakeKnowOne.instances[0].include_context_values) == {True}


def test_load_cases_rejects_malformed_expected_context_any(tmp_path) -> None:
    """expected_context_any 必须是非空字符串列表，防止静默放宽判定。"""
    dataset = tmp_path / "bad.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "bad-001",
                "query": "如何保养",
                "expected_any": ["定期保养"],
                "expected_context_any": ["   "],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="第 1 行"):
        load_cases(dataset)


def test_smoke_cli_applies_query_expansions_when_provided(
    tmp_path, capsys, monkeypatch
) -> None:
    """--expansions 词典把手册侧术语追加进查询，模拟调用方口语补全。

    契约规定 query 口语补全由调用方完成（contracts.md），评测器作为
    调用方在检索前追加同义术语；报告需记录扩展生效的样本数以便复现。
    """
    dataset = tmp_path / "signal.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "signal-001",
                "query": "轻拨一下转向灯会闪几次？",
                "expected_any": ["右侧信号灯将闪烁 3 次"],
                "expected_pages": [163],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    expansions = tmp_path / "expansions.json"
    expansions.write_text(
        json.dumps({"转向灯": ["转向信号灯"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeKnowOne:
        """记录实际收到的查询，验证扩展发生在评测器调用方一侧。"""

        instances: list["FakeKnowOne"] = []

        def __init__(self, dsn: str) -> None:
            self.received_queries: list[str] = []
            type(self).instances.append(self)

        def retrieve(self, query: str, namespace: str, access_scope, **kwargs: object) -> RetrievalResult:
            self.received_queries.append(query)
            text = "右侧信号灯将闪烁 3 次" if "转向信号灯" in query else "无关内容"
            evidence = Evidence(
                text=text,
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": len(text), "page": 163},
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
            "--dataset", str(dataset), "--namespace", "rav4", "--principal", "evaluator",
            "--dsn", "postgresql://test", "--expansions", str(expansions),
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["expansions_applied"] == 1
    assert report["modes"]["hybrid"]["hits"] == 1
    # 三种召回模式共用同一扩展查询，原查询不因词典而丢弃。
    assert FakeKnowOne.instances[0].received_queries == [
        "轻拨一下转向灯会闪几次？ 转向信号灯",
    ] * 3


def test_load_expansions_rejects_malformed_dictionary(tmp_path) -> None:
    """词典必须是 {非空 str: [非空 str, ...]}，防止静默失效。"""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"转向灯": []}, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="词典"):
        load_expansions(bad)


def test_smoke_cli_applies_rerank_layer_when_rerank_llm_provided(
    tmp_path, capsys, monkeypatch
) -> None:
    """--rerank-base-url 为每路追加 {mode}+rerank 模式：取更深候选重排后截回 top_k。"""
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
        """返回 16 个候选，期望证据位于第 10 位（top-8 外、重排深度内）。"""

        def __init__(self, dsn: str) -> None:
            self.top_k_values: list[object] = []

        def retrieve(self, query: str, namespace: str, access_scope, **kwargs: object) -> RetrievalResult:
            self.top_k_values.append(kwargs["top_k"])
            evidence = tuple(
                Evidence(
                    text=f"第{i}条无关内容" if i != 10 else "定期保养的原文",
                    document_id="document-1",
                    revision_id="revision-1",
                    chunk_id=f"chunk-{i}",
                    source_locator={"page": 92},
                    publication_valid_from=datetime(2026, 9, 1, tzinfo=UTC),
                    publication_valid_until=None,
                )
                for i in range(1, 17)
            )
            return RetrievalResult(
                evidence=evidence,
                index_generation="generation-1",
                model_version="test-model",
                trace_id="trace-1",
            )

    class FakeReranker:
        """把第 10 位（期望证据）提到首位，模拟 LLM 语义重排。"""

        observed_depths: list[int] = []
        max_candidates = 16  # 与默认 RerankConfig 对齐，控制重排深度

        def __init__(self, config: object) -> None:
            pass

        def rerank(self, query: str, evidence: Sequence[Evidence]) -> object:
            FakeReranker.observed_depths.append(len(evidence))
            head = list(evidence)
            rescued = head.pop(9)
            class _Outcome:
                evidence = tuple([rescued] + head)
                degraded = False
            return _Outcome()

    import know_one.eval.smoke as smoke_module

    monkeypatch.setattr(smoke_module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(smoke_module, "Reranker", FakeReranker)

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
            "--rerank-base-url",
            "http://llm.local/v1",
            "--rerank-model",
            "test-model",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    # 原三模式保持（RRF 顺序第 10 位 → 全部 miss）；rerank 层命中。
    assert report["modes"]["hybrid"] == {"hits": 0, "hit_rate": 0.0, "miss_ids": ["manual-001"]}
    assert report["modes"]["hybrid+rerank"]["hits"] == 1
    assert report["modes"]["hybrid+rerank"]["miss_ids"] == []
    assert report["modes"]["vector+rerank"]["hits"] == 1
    # rerank 深度必须大于最终 top_k：retrieve 取候选深度 16，判定只看前 8。
    assert FakeReranker.observed_depths and set(FakeReranker.observed_depths) == {16}
