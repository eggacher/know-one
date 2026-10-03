"""OpenAI 兼容 embedding 协议适配测试。"""

from __future__ import annotations

from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from know_one.embedding import OpenAIEmbeddingClient
from know_one.errors import DependencyUnavailable


class _Response:
    """只实现 urllib 客户端在测试中需要的响应接口。"""

    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_openai_embedding_client_orders_vectors_by_response_index(monkeypatch) -> None:
    """服务返回乱序 data 时，客户端仍须按输入文本顺序输出向量。"""
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout: int):
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data)
        assert timeout == 30
        return _Response(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
        )

    monkeypatch.setattr("know_one.embedding.urlopen", fake_urlopen)

    vectors = OpenAIEmbeddingClient("http://embedding/v1", "test-model", 2).embed(["甲", "乙"])

    assert seen == {
        "url": "http://embedding/v1/embeddings",
        "payload": {"input": ["甲", "乙"], "model": "test-model"},
    }
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


def test_openai_embedding_client_uses_the_callers_timeout_budget(monkeypatch) -> None:
    """在线检索可把剩余 deadline 传给 embedding 适配器。"""
    seen: dict[str, object] = {}

    def fake_urlopen(_request, timeout: float):
        seen["timeout"] = timeout
        return _Response({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    monkeypatch.setattr("know_one.embedding.urlopen", fake_urlopen)

    OpenAIEmbeddingClient("http://embedding/v1", "test-model", 2).embed(
        ["甲"], timeout_seconds=1.5
    )

    assert seen["timeout"] == 1.5


def test_openai_embedding_client_reports_http_status_without_request_content(monkeypatch) -> None:
    """长文入库失败时应区分服务端拒绝，且不把原文回显到错误中。"""
    error = HTTPError(
        "http://embedding/v1/embeddings",
        413,
        "Payload Too Large",
        None,
        BytesIO(b'{"error":"input too long"}'),
    )
    monkeypatch.setattr("know_one.embedding.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(DependencyUnavailable, match=r"HTTP 413.*input too long"):
        OpenAIEmbeddingClient("http://embedding/v1", "test-model", 2).embed(["不应出现在错误中"])
