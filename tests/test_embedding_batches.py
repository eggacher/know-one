"""大文档 embedding 分批提交的回归测试。"""

from __future__ import annotations

import pytest

from know_one.core.api import _embed_texts_in_batches
from know_one.errors import DependencyUnavailable


def test_embedding_batches_preserve_input_order_and_limit_each_request() -> None:
    """整本手册不能作为单一请求超时，但批次结果必须仍与 Chunk 顺序对应。"""
    calls: list[list[str]] = []

    class FakeEmbeddingClient:
        def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[float(int(text))] for text in texts]

    vectors = _embed_texts_in_batches(FakeEmbeddingClient(), [str(index) for index in range(130)])

    assert [len(call) for call in calls] == [64, 64, 2]
    assert vectors == [[float(index)] for index in range(130)]


def test_embedding_batches_report_only_the_failing_batch_metadata() -> None:
    """远端拒绝长批次时，操作者应能定位批次但不能在日志中得到正文。"""
    class FailingEmbeddingClient:
        def embed(self, _texts: list[str]) -> list[list[float]]:
            raise DependencyUnavailable("embedding 服务返回 HTTP 413")

    with pytest.raises(
        DependencyUnavailable,
        match=r"start=0 chunks=2 chars=5.*HTTP 413",
    ):
        _embed_texts_in_batches(FailingEmbeddingClient(), ["甲乙", "丙丁丁"])


def test_embedding_batches_split_long_texts_before_reaching_the_chunk_limit() -> None:
    """真实 PDF 的 Chunk 虽未到 64 条，也不能累积成一次超时的大请求。"""
    calls: list[list[str]] = []

    class FakeEmbeddingClient:
        def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[0.0] for _ in texts]

    _embed_texts_in_batches(FakeEmbeddingClient(), ["x" * 5000 for _ in range(5)])

    assert [len(call) for call in calls] == [2, 2, 1]


def test_embedding_batches_retry_a_timed_out_batch_by_halving_it() -> None:
    """服务端慢批次不应让整本手册失败；重试后仍需保持原始向量顺序。"""
    calls: list[list[str]] = []

    class SlowBatchEmbeddingClient:
        def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            if len(texts) > 1:
                raise DependencyUnavailable("embedding 服务在 30 秒内超时")
            return [[float(int(texts[0]))]]

    assert _embed_texts_in_batches(SlowBatchEmbeddingClient(), ["0", "1", "2"]) == [
        [0.0], [1.0], [2.0]
    ]
    assert [len(call) for call in calls] == [3, 1, 2, 1, 1]
