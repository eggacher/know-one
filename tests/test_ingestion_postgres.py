"""需要真实 PostgreSQL 的首条入库链路验收测试。"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from know_one import AccessScope, KnowOne
from know_one.cli import create_namespace
from know_one.ingestion import TextSource


@pytest.fixture
def dsn() -> str:
    """显式提供测试库才运行，避免普通单元测试依赖 Docker。"""
    value = os.getenv("KNOWONE_TEST_DSN")
    if not value:
        pytest.skip("未设置 KNOWONE_TEST_DSN，跳过 PostgreSQL 集成测试")
    return value


def test_plain_text_ingestion_creates_ready_revision_and_chunks(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯文本提交后，任务、Revision、构建记录和段落 Chunk 应完整关联。"""
    namespace = f"test-{uuid4()}"
    generation_id = create_namespace(dsn, namespace)

    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    kb = KnowOne(dsn)
    # 验证数据库写入链路，不依赖开发机是否启动实际模型服务。
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    source = TextSource("保养周期为 5000 公里。\n\n如遇极端工况，请缩短保养周期。")

    ref = kb.ingest(source, namespace, "maintenance", scope, "request-1")
    assert ref.status == "queued"
    assert kb.ingest(source, namespace, "maintenance", scope, "request-1") == ref

    kb.process_job(ref.job_id)
    status = kb.get_ingestion(ref.job_id, scope)
    assert status.status == "ready"
    assert status.stage == "ready"
    assert status.progress_done == status.progress_total == 2
    assert status.revision_id is not None

    with psycopg.connect(dsn) as connection:
        chunks = connection.execute(
            "SELECT raw_text, source_locator, vector_dims(embedding) FROM chunk WHERE revision_id = %s ORDER BY ordinal",
            (status.revision_id,),
        ).fetchall()
        build = connection.execute(
            """
            SELECT status, expected_chunk_count, completed_chunk_count
            FROM revision_index_build WHERE revision_id = %s AND index_generation_id = %s
            """,
            (status.revision_id, generation_id),
        ).fetchone()
    assert [chunk[0] for chunk in chunks] == ["保养周期为 5000 公里。", "如遇极端工况，请缩短保养周期。"]
    assert chunks[0][1]["char_start"] == 0
    assert [chunk[2] for chunk in chunks] == [1024, 1024]
    assert build == ("ready", 2, 2)
