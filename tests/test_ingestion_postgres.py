"""需要真实 PostgreSQL 的首条入库链路验收测试。"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from uuid import uuid4

import psycopg
import pytest

from know_one import (
    AccessScope,
    ConcurrentModification,
    IdempotencyConflict,
    InvalidArgument,
    KnowOne,
)
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


def test_incomplete_index_generation_cannot_be_activated(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新索引代未重建完全部 Revision 时，旧代必须继续保持 active。"""
    namespace = f"test-{uuid4()}"
    old_generation_id = create_namespace(dsn, namespace)
    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("初始内容。"), namespace, "manual", scope, "initial-request")
    kb.process_job(ref.job_id)

    upgraded_kb = KnowOne(dsn, embedding_model="test-upgraded-model")
    new_generation_id = upgraded_kb.create_index_generation(namespace)

    with pytest.raises(InvalidArgument, match="尚未完成"):
        upgraded_kb.activate_index_generation(namespace, new_generation_id)

    with psycopg.connect(dsn) as connection:
        namespace_row = connection.execute(
            "SELECT current_index_generation_id FROM namespace WHERE name = %s", (namespace,)
        ).fetchone()
        statuses = connection.execute(
            "SELECT id, status FROM index_generation WHERE id IN (%s, %s) ORDER BY id",
            (old_generation_id, new_generation_id),
        ).fetchall()
    assert str(namespace_row[0]) == old_generation_id
    assert dict((str(identifier), status) for identifier, status in statuses) == {
        old_generation_id: "active",
        new_generation_id: "building",
    }


def test_activated_index_generation_receives_subsequent_ingestion(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切换完成后，重建与后续新入库都只能写入新 active generation。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    initial = kb.ingest(TextSource("旧代内容。"), namespace, "old", scope, "old-request")
    kb.process_job(initial.job_id)

    upgraded_kb = KnowOne(dsn, embedding_model="test-upgraded-model")
    new_generation_id = upgraded_kb.create_index_generation(namespace)
    upgraded_kb.rebuild_index_generation(namespace, new_generation_id)
    upgraded_kb.activate_index_generation(namespace, new_generation_id)

    subsequent = upgraded_kb.ingest(
        TextSource("新代内容。"), namespace, "new", scope, "new-request"
    )
    upgraded_kb.process_job(subsequent.job_id)
    subsequent_status = upgraded_kb.get_ingestion(subsequent.job_id, scope)

    with psycopg.connect(dsn) as connection:
        builds = connection.execute(
            """
            SELECT build.index_generation_id, build.status
            FROM revision_index_build AS build
            WHERE build.revision_id = %s
            """,
            (subsequent_status.revision_id,),
        ).fetchall()
    assert [(str(generation_id), status) for generation_id, status in builds] == [
        (new_generation_id, "ready")
    ]


def test_ready_revision_can_be_published_from_a_past_validity_window(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次发布写入窗口，并递增 Document 的可检索状态代数。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "publish"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("可发布内容。"), namespace, "manual", scope, "ingest-request")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None

    valid_from = datetime(2026, 9, 1, tzinfo=UTC)
    kb.publish(revision_id, namespace, valid_from, None, 0, scope, "publish-request")

    with psycopg.connect(dsn) as connection:
        publication = connection.execute(
            """
            SELECT publication.revision_id, publication.valid_from,
                   publication.valid_until, publication.published_by
            FROM publication
            JOIN document ON document.id = publication.document_id
            WHERE document.namespace_id = (SELECT id FROM namespace WHERE name = %s)
            """,
            (namespace,),
        ).fetchone()
        state_generation = connection.execute(
            "SELECT state_generation FROM document WHERE namespace_id = "
            "(SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        ).fetchone()
    assert (str(publication[0]), *publication[1:]) == (revision_id, valid_from, None, "editor")
    assert state_generation == (1,)


def test_publish_appends_a_window_and_replays_the_same_idempotency_key(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """追加发布截短旧窗口，成功回执重放不重复修改状态。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "publish"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    first = kb.ingest(TextSource("第一版。"), namespace, "manual", scope, "ingest-v1")
    kb.process_job(first.job_id)
    first_revision = kb.get_ingestion(first.job_id, scope).revision_id
    assert first_revision is not None
    first_from = datetime(2026, 9, 1, tzinfo=UTC)
    kb.publish(first_revision, namespace, first_from, None, 0, scope, "publish-v1")

    second = kb.ingest(TextSource("第二版。"), namespace, "manual", scope, "ingest-v2")
    kb.process_job(second.job_id)
    second_revision = kb.get_ingestion(second.job_id, scope).revision_id
    assert second_revision is not None
    second_from = datetime(2026, 9, 23, tzinfo=UTC)
    kb.publish(second_revision, namespace, second_from, None, 1, scope, "publish-v2")
    kb.publish(second_revision, namespace, second_from, None, 1, scope, "publish-v2")

    with psycopg.connect(dsn) as connection:
        windows = connection.execute(
            """
            SELECT publication.revision_id, publication.valid_from, publication.valid_until
            FROM publication
            JOIN document ON document.id = publication.document_id
            WHERE document.namespace_id = (SELECT id FROM namespace WHERE name = %s)
            ORDER BY publication.valid_from
            """,
            (namespace,),
        ).fetchall()
        state_generation = connection.execute(
            "SELECT state_generation FROM document WHERE namespace_id = "
            "(SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        ).fetchone()
    assert [(str(revision), start, end) for revision, start, end in windows] == [
        (first_revision, first_from, second_from),
        (second_revision, second_from, None),
    ]
    assert state_generation == (2,)


def test_publish_rejects_incomplete_build_stale_state_and_conflicting_replay(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布必须以当前代完整构建、未过期状态代数和同一请求指纹为前提。"""
    namespace = f"test-{uuid4()}"
    generation_id = create_namespace(dsn, namespace)
    scope = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "publish"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("待发布内容。"), namespace, "manual", scope, "ingest-request")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    valid_from = datetime(2026, 9, 1, tzinfo=UTC)

    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute(
            """
            UPDATE revision_index_build
            SET status = 'building', expected_chunk_count = NULL,
                completed_chunk_count = 0, completed_at = NULL
            WHERE revision_id = %s AND index_generation_id = %s
            """,
            (revision_id, generation_id),
        )
    with pytest.raises(InvalidArgument, match="尚未在当前"):
        kb.publish(revision_id, namespace, valid_from, None, 0, scope, "incomplete-build")

    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute(
            """
            UPDATE revision_index_build
            SET status = 'ready', expected_chunk_count = 1,
                completed_chunk_count = 1, completed_at = now()
            WHERE revision_id = %s AND index_generation_id = %s
            """,
            (revision_id, generation_id),
        )
    kb.publish(revision_id, namespace, valid_from, None, 0, scope, "publish-request")

    with pytest.raises(ConcurrentModification):
        kb.publish(revision_id, namespace, valid_from, None, 0, scope, "stale-state")
    with pytest.raises(IdempotencyConflict):
        kb.publish(
            revision_id,
            namespace,
            datetime(2026, 9, 2, tzinfo=UTC),
            None,
            1,
            scope,
            "publish-request",
        )


def test_retrieve_returns_published_chunk_from_active_generation(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全文首切片只返回当前代中已发布且有权限读取的原文 Chunk。"""
    namespace = f"test-{uuid4()}"
    generation_id = create_namespace(dsn, namespace)
    scope = AccessScope(
        "reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"})
    )
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id,
        namespace,
        datetime(2026, 9, 1, tzinfo=UTC),
        None,
        0,
        scope,
        "publish",
    )

    result = kb.retrieve("保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC))

    assert result.index_generation == generation_id
    assert len(result.evidence) == 1
    assert result.evidence[0].text == "保养周期为 5000 公里。"
    assert result.evidence[0].source_locator == {"char_start": 0, "char_end": 14}
