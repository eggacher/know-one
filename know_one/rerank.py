"""LLM listwise rerank：对 RRF 融合后的 Evidence 做语义重排。

定位是调用方组合层（pipeline.md 规划的核心集成待后续）：调用方先
``retrieve(top_k=N)`` 拿融合顺序，再交本模块重排取头部。契约要点：
LLM 失败（网络/超时/解析不出合法编号）必须降级返回原顺序并标记
``degraded``，重排层绝不能把检索打死。

OpenAI 兼容 ``/chat/completions`` 非流式调用（预验证：qwen3.5-9b
listwise 对 signal/fuse 类边缘题排序精准，RRF r9/r10 → r1）。
"""

import json
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from know_one.model import Evidence

# 从模型输出中提取「编号数组」；容忍 markdown 围栏与前后缀文字。
_RANKING_PATTERN = re.compile(r"\[[\d,\s]+\]")


@dataclass(frozen=True)
class RerankConfig:
    """重排的可调参数与 LLM 端点；候选深度与截断长度控制输入成本。"""

    base_url: str
    model: str
    timeout_seconds: float = 180.0
    max_candidates: int = 16
    candidate_chars: int = 180


@dataclass(frozen=True)
class RerankOutcome:
    """重排结果；degraded=True 表示 LLM 失败，evidence 保持 RRF 原序。"""

    evidence: tuple[Evidence, ...]
    degraded: bool


class Reranker:
    """listwise 重排器：一次调用让 LLM 对全部候选做相关性排序。"""

    def __init__(self, config: RerankConfig) -> None:
        self._config = config

    @property
    def max_candidates(self) -> int:
        """重排候选深度；调用方检索时至少取这么多候选才有重排意义。"""
        return self._config.max_candidates

    def rerank(self, query: str, evidence: Sequence[Evidence]) -> RerankOutcome:
        """按与 query 的相关性重排 Evidence；只重排头部，尾部保原序。

        超过 ``max_candidates`` 的尾部不进入 LLM（控制输入长度），直接
        按原顺序接在重排结果之后，保证不丢证据。
        """
        head = evidence[: self._config.max_candidates]
        tail = evidence[self._config.max_candidates :]
        if not head:
            return RerankOutcome(tuple(evidence), degraded=False)
        ranking = self._ask_llm(query, head)
        if ranking is None:
            return RerankOutcome(tuple(evidence), degraded=True)
        return RerankOutcome(
            tuple([head[index - 1] for index in ranking] + list(tail)),
            degraded=False,
        )

    def _ask_llm(self, query: str, head: Sequence[Evidence]) -> list[int] | None:
        """调用 LLM 并解析为「合法、互异、完整」的 1 基编号列表。

        返回 None 表示降级（网络失败或解析不出任何合法编号）。模型漏报
        的编号按原顺序补尾——部分输出仍优于丢弃。
        """
        listing = "\n".join(
            f"[{index}] {evidence.text[: self._config.candidate_chars].replace(chr(10), ' ')}"
            for index, evidence in enumerate(head, 1)
        )
        prompt = (
            "你是汽车手册检索重排器。根据文档与问题的相关性从高到低排序"
            "（最可能直接回答问题的在前）。\n"
            f"问题：{query}\n\n候选文档：\n{listing}\n\n"
            "只输出 JSON 数组，如 [3,1,2,...]，包含全部编号，无其他文字。"
        )
        try:
            request = Request(
                f"{self._config.base_url}/chat/completions",
                json.dumps(
                    {
                        "model": self._config.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                        "max_tokens": 200,
                    }
                ).encode(),
                {"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                payload = json.load(response)
        except (URLError, TimeoutError, OSError, ValueError, KeyError):
            # 网络/超时/非 JSON 响应：降级为 RRF 顺序（pipeline.md 契约）。
            return None
        match = _RANKING_PATTERN.search(payload["choices"][0]["message"]["content"])
        if match is None:
            return None
        # 过滤越界编号并去重，模型漏报的按原序补尾，保证结果完整。
        seen: set[int] = set()
        ranked: list[int] = []
        for value in json.loads(match.group(0)):
            if isinstance(value, int) and 1 <= value <= len(head) and value not in seen:
                seen.add(value)
                ranked.append(value)
        if not ranked:
            return None
        return ranked + [index for index in range(1, len(head) + 1) if index not in seen]
