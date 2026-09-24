"""需要真实 PostgreSQL 的首条入库链路验收测试。"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from know_one import AccessScope, KnowOne
from know_one.ingestion import TextSource


@pytest.fixture
def dsn() -> str:
    """显式提供测试库才运行，避免普通单元测试依赖 Docker。"""
    value = os.getenv("KNOWONE_TEST_DSN")
    if not value:
        pytest.skip("未设置 KNOWONE_TEST_DSN，跳过 PostgreSQL 集成测试")
    return value


def test_plain_text_ingestion_creates_ready_revision_and_chunks(dsn: str) -> None:
    """纯文本提交后，任务、Revision、构建记录和段落 Chunk 应完整关联。"""
    namespace_id, generation_id = uuid4(), uuid4()
    namespace = f"test-{uuid4()}"
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO namespace (id, name) VALUES (%s, %s)", (namespace_id, namespace))
        connection.execute(
            """
            INSERT INTO index_generation (
                id, namespace_id, config_fingerprint, embedding_model,
                tokenizer_version, dims, distance, status
            ) VALUES (%s, %s, 'test', 'not-used-in-m1', 'test', 3, 'cosine', 'active')
            """,
            (generation_id, namespace_id),
        )
        connection.execute(
            "UPDATE namespace SET current_index_generation_id = %s WHERE id = %s",
            (generation_id, namespace_id),
        )

    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    kb = KnowOne(dsn)
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
            "SELECT raw_text, source_locator FROM chunk WHERE revision_id = %s ORDER BY ordinal",
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
    assert build == ("ready", 2, 2)
