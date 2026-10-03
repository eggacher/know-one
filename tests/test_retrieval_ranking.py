"""混合检索排序策略的纯单元测试。"""

from __future__ import annotations

from know_one.core.api import _fuse_rrf_candidates


def test_hybrid_promotes_a_candidate_confirmed_by_both_paths() -> None:
    """双路均命中的精确操作证据应能进入最终八条结果。"""
    vector_rows = [{"chunk_id": f"vector-{index}"} for index in range(1, 13)] + [
        {"chunk_id": "both-paths"}
    ]
    full_text_rows = [{"chunk_id": "both-paths"}]

    ranked = _fuse_rrf_candidates(full_text_rows, vector_rows, top_k=8)

    assert "both-paths" in [row["chunk_id"] for row, _score in ranked]


def test_hybrid_keeps_the_first_vector_candidate_when_full_text_promotes_others() -> None:
    """全文加分不能将向量首位的语义锚点挤出最终八条结果。"""
    vector_rows = [{"chunk_id": f"vector-{index}"} for index in range(1, 13)]
    full_text_rows = [{"chunk_id": f"vector-{index}"} for index in range(2, 10)]

    ranked = _fuse_rrf_candidates(full_text_rows, vector_rows, top_k=8)

    assert "vector-1" in [row["chunk_id"] for row, _score in ranked]
