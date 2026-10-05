"""LLM rerank 模块的降级与重排行为测试。

rerank 是检索结果的重排层：接收 RRF 融合顺序的 Evidence，调用 LLM 做
listwise 相关性排序；LLM 失败（网络/解析）必须降级返回原顺序并标记
degraded，绝不让重排层把检索打死（pipeline.md 的降级契约）。
"""

import json
from datetime import datetime
from urllib.error import URLError

import pytest

from know_one.model import Evidence
from know_one.rerank import RerankConfig, Reranker


def _evidence(text: str, chunk_id: str) -> Evidence:
    """构造仅文本不同的最小 Evidence，排序断言只关心文本身份。"""
    return Evidence(
        text=text,
        document_id="doc",
        revision_id="rev",
        chunk_id=chunk_id,
        source_locator={"page": 1},
        publication_valid_from=datetime(2026, 1, 1),
        publication_valid_until=None,
    )


@pytest.fixture()
def config() -> RerankConfig:
    return RerankConfig(
        base_url="http://llm.local/v1",
        model="test-model",
        timeout_seconds=5,
        max_candidates=4,
        candidate_chars=50,
    )


def _llm_reply(monkeypatch, payload: str) -> None:
    """把 urlopen 替换为返回固定 LLM 文本的假响应。"""

    class _Response:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:  # pragma: no cover - 简单桩
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        return _Response(
            json.dumps({"choices": [{"message": {"content": payload}}]}).encode()
        )

    monkeypatch.setattr("know_one.rerank.urlopen", fake_urlopen)


def test_rerank_reorders_by_llm_ranking(config, monkeypatch) -> None:
    """LLM 返回的编号顺序应直接决定 Evidence 重排顺序。"""
    _llm_reply(monkeypatch, "[3,1,2]")
    reranker = Reranker(config)
    outcome = reranker.rerank(
        "P 挡用途",
        [_evidence(f"正文甲{i}", f"c{i}") for i in (1, 2, 3)],
    )
    assert [e.chunk_id for e in outcome.evidence] == ["c3", "c1", "c2"]
    assert outcome.degraded is False


def test_rerank_extracts_json_inside_markdown_fence(config, monkeypatch) -> None:
    """模型常把 JSON 包在 ```json 围栏里，必须能提取。"""
    _llm_reply(monkeypatch, "排序结果：\n```json\n[2,1]\n```\n完毕")
    outcome = Reranker(config).rerank("q", [_evidence("甲", "c1"), _evidence("乙", "c2")])
    assert [e.chunk_id for e in outcome.evidence] == ["c2", "c1"]


def test_rerank_appends_missing_ids_in_original_order(config, monkeypatch) -> None:
    """模型漏报编号时，未提及的 Evidence 按原顺序补尾，不丢结果。"""
    _llm_reply(monkeypatch, "[2]")
    outcome = Reranker(config).rerank(
        "q", [_evidence("甲", "c1"), _evidence("乙", "c2"), _evidence("丙", "c3")]
    )
    assert [e.chunk_id for e in outcome.evidence] == ["c2", "c1", "c3"]
    assert outcome.degraded is False


def test_rerank_degrades_to_original_order_on_garbage(config, monkeypatch) -> None:
    """无法提取任何合法编号时降级：原顺序 + degraded 标记。"""
    _llm_reply(monkeypatch, "抱歉，我无法完成这个任务。")
    outcome = Reranker(config).rerank("q", [_evidence("甲", "c1"), _evidence("乙", "c2")])
    assert [e.chunk_id for e in outcome.evidence] == ["c1", "c2"]
    assert outcome.degraded is True


def test_rerank_degrades_on_network_error(config, monkeypatch) -> None:
    """LLM 网络失败时降级为 RRF 顺序，不抛异常。"""

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        raise URLError("connection refused")

    monkeypatch.setattr("know_one.rerank.urlopen", fake_urlopen)
    outcome = Reranker(config).rerank("q", [_evidence("甲", "c1")])
    assert [e.chunk_id for e in outcome.evidence] == ["c1"]
    assert outcome.degraded is True


def test_rerank_reranks_head_and_keeps_tail_beyond_max(config, monkeypatch) -> None:
    """候选超过 max_candidates 时只重排前段，尾部按原序保留。"""
    _llm_reply(monkeypatch, "[4,1]")
    evidence = [_evidence(f"正文{i}", f"c{i}") for i in range(1, 6)]
    outcome = Reranker(config).rerank("q", evidence)
    assert [e.chunk_id for e in outcome.evidence] == ["c4", "c1", "c2", "c3", "c5"]


def test_rerank_sends_candidate_texts_truncated(config, monkeypatch) -> None:
    """候选文本按 candidate_chars 截断后才进入 prompt，控制输入长度。"""
    captured = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        captured["body"] = json.loads(request.data.decode())
        body = json.dumps({"choices": [{"message": {"content": "[1]"}}]}).encode()

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        return _Response()

    monkeypatch.setattr("know_one.rerank.urlopen", fake_urlopen)
    Reranker(config).rerank("q", [_evidence("长" * 200, "c1")])
    prompt = captured["body"]["messages"][0]["content"]
    assert "长" * 50 in prompt and "长" * 51 not in prompt
