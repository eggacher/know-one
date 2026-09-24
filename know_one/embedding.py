"""OpenAI 兼容 embedding 服务的最小客户端。"""

from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from know_one.errors import DependencyUnavailable, InvalidArgument


class OpenAIEmbeddingClient:
    """调用 ``POST /embeddings``，并校验模型返回的向量。"""

    def __init__(self, endpoint: str | None, model: str | None, dimensions: int) -> None:
        if not endpoint or not model:
            raise InvalidArgument("embedding 需要 KNOWONE_EMBEDDING_ENDPOINT 和 MODEL")
        self._url = f"{endpoint.rstrip('/')}/embeddings"
        self._model = model
        self._dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        """为顺序文本批量生成向量；网络与协议错误不产生半成品。"""
        payload = json.dumps({"input": texts, "model": self._model}).encode("utf-8")
        request = Request(self._url, payload, {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 -- endpoint 由部署方配置
                body = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DependencyUnavailable("embedding 服务不可用或返回无效 JSON") from error
        try:
            rows = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in rows]
        except (KeyError, TypeError) as error:
            raise DependencyUnavailable("embedding 响应缺少 data/index/embedding") from error
        if len(vectors) != len(texts):
            raise DependencyUnavailable("embedding 返回数量与输入不一致")
        for vector in vectors:
            if len(vector) != self._dimensions or not all(
                isinstance(value, (int, float)) and math.isfinite(value) for value in vector
            ):
                raise DependencyUnavailable("embedding 返回向量维度或数值非法")
        return [[float(value) for value in vector] for vector in vectors]
