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
from know_one.cli import create_namespace, main
from know_one.ingestion import MarkdownSource, PdfSource, TextSource


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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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


def test_process_next_job_claims_queued_work_and_stops_cleanly_when_empty(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次 worker 调度只处理一条任务，空队列不是异常。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    kb = KnowOne(dsn)
    first = kb.ingest(TextSource("第一条任务。"), namespace, "first", scope, "first")
    second = kb.ingest(TextSource("第二条任务。"), namespace, "second", scope, "second")

    assert kb.process_next_job() == first.job_id
    assert kb.get_ingestion(first.job_id, scope).status == "ready"
    assert kb.get_ingestion(second.job_id, scope).status == "queued"
    assert kb.process_next_job() == second.job_id
    assert kb.process_next_job() is None


def test_pdf_ingestion_records_raw_pdf_page_locator(
    dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """文本层 PDF 应经 worker 解析，并为每页 Chunk 保存可回查页码。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("tester", frozenset({namespace}), frozenset({"ingest"}))
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-test")
    parsed_snapshots: list[dict[str, object]] = []

    def source_text(
        _bytes: bytes, _media_type: str, snapshot: dict[str, object]
    ) -> str:
        parsed_snapshots.append(snapshot)
        return "第一页内容。\f第二页内容。"

    monkeypatch.setattr("know_one.core.api.KnowOne._source_text", staticmethod(source_text))
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )

    kb = KnowOne(dsn)
    job = kb.ingest(
        PdfSource(pdf, first_page=92, last_page=93), namespace, "manual", scope, "pdf-ingest"
    )
    kb.process_job(job.job_id)
    revision_id = kb.get_ingestion(job.job_id, scope).revision_id
    assert revision_id is not None
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT raw_text, source_locator FROM chunk WHERE revision_id = %s ORDER BY ordinal",
            (revision_id,),
        ).fetchall()
    assert rows == [
        ("第一页内容。", {"char_start": 0, "char_end": 6, "page": 92}),
        ("第二页内容。", {"char_start": 7, "char_end": 13, "page": 93}),
    ]
    rebuilt_kb = KnowOne(dsn, embedding_model="pdf-test-v2")
    generation_id = rebuilt_kb.create_index_generation(namespace)
    rebuilt_kb.rebuild_index_generation(namespace, generation_id)
    assert parsed_snapshots == [
        {"source_name": "manual.pdf", "first_page": 92, "last_page": 93},
        {"source_name": "manual.pdf", "first_page": 92, "last_page": 93},
    ]


def test_markdown_ingestion_preserves_heading_paths_and_source_ranges(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Markdown 标题不进入正文 Chunk，但为后续段落提供可追溯的标题路径。

    同时覆盖两个解析边界：代码围栏内的 # 行不改变标题路径；以 # 结尾
    的标题（如 C#）不被闭合序列规则截断。
    """
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope(
        "tester", frozenset({namespace}), frozenset({"ingest", "publish", "read"})
    )
    source = MarkdownSource(
        "# 账户\n账户总览。\n\n## 密码\n忘记密码后请重置。"
        "\n\n## 排障\n```bash\n# 修改配置文件\nexport FOO=1\n```\n\n重启服务。\n\n## 语言\n支持 C#。"
    )
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )

    kb = KnowOne(dsn)
    job = kb.ingest(source, namespace, "guide", scope, "markdown-ingest")
    kb.process_job(job.job_id)
    revision_id = kb.get_ingestion(job.job_id, scope).revision_id
    assert revision_id is not None

    with psycopg.connect(dsn) as connection:
        chunks = connection.execute(
            """
            SELECT raw_text, heading_path, source_locator
            FROM chunk WHERE revision_id = %s ORDER BY ordinal
            """,
            (revision_id,),
        ).fetchall()
    assert chunks == [
        ("账户总览。", ["账户"], {"char_end": 10, "char_start": 5}),
        ("忘记密码后请重置。", ["账户", "密码"], {"char_end": 27, "char_start": 18}),
        # 围栏内的 # 注释不改变标题路径，围栏内容作为连续正文保留。
        ("```bash\n# 修改配置文件\nexport FOO=1\n```", ["账户", "排障"],
         {"char_end": 68, "char_start": 35}),
        ("重启服务。", ["账户", "排障"], {"char_end": 75, "char_start": 70}),
        # 以 # 结尾的标题文字保留，不被当作闭合序列剥离。
        ("支持 C#。", ["账户", "语言"], {"char_end": 89, "char_start": 83}),
    ]

    kb.publish(
        revision_id,
        namespace,
        datetime(2026, 9, 1, tzinfo=UTC),
        None,
        0,
        scope,
        "publish",
    )
    result = kb.retrieve(
        "忘记密码后请重置", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC), top_k=1
    )
    assert [(evidence.text, evidence.heading_path) for evidence in result.evidence] == [
        ("忘记密码后请重置。", ("账户", "密码"))
    ]

    rebuilt_kb = KnowOne(dsn, embedding_model="markdown-test-v2")
    generation_id = rebuilt_kb.create_index_generation(namespace)
    rebuilt_kb.rebuild_index_generation(namespace, generation_id)
    rebuilt_kb.activate_index_generation(namespace, generation_id)
    rebuilt = rebuilt_kb.retrieve(
        "忘记密码后请重置", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC), top_k=1
    )
    assert [(evidence.text, evidence.heading_path) for evidence in rebuilt.evidence] == [
        ("忘记密码后请重置。", ("账户", "密码"))
    ]


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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
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


def test_retrieve_can_diagnose_full_text_and_vector_recall_independently(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评估可分别观察全文和向量候选，且两路仍受同一发布约束。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope(
        "reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"})
    )

    def embed(_client: object, texts: list[str], **_kwargs: object) -> list[list[float]]:
        """为语义候选和关键词候选构造可区分的确定性向量。"""
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            vector[0] = 1.0 if "语义" in text else 0.0
            vectors.append(vector)
        return vectors

    monkeypatch.setattr("know_one.core.api.OpenAIEmbeddingClient.embed", embed)
    kb = KnowOne(dsn)
    revisions = []
    for source_key, text in (("keyword", "关键词"), ("semantic", "语义专用文档。")):
        job = kb.ingest(TextSource(text), namespace, source_key, scope, f"ingest-{source_key}")
        kb.process_job(job.job_id)
        revision_id = kb.get_ingestion(job.job_id, scope).revision_id
        assert revision_id is not None
        revisions.append(revision_id)
    for index, revision_id in enumerate(revisions):
        kb.publish(
            revision_id,
            namespace,
            datetime(2026, 9, 1, tzinfo=UTC),
            None,
            0,
            scope,
            f"publish-{index}",
        )

    full_text = kb.retrieve(
        "关键词",
        namespace,
        scope,
        at=datetime(2026, 9, 2, tzinfo=UTC),
        top_k=1,
        recall_mode="full_text",
    )
    vector = kb.retrieve(
        "语义询问",
        namespace,
        scope,
        at=datetime(2026, 9, 2, tzinfo=UTC),
        top_k=1,
        recall_mode="vector",
    )

    assert [(evidence.text, evidence.score_type) for evidence in full_text.evidence] == [
        ("关键词", "full_text")
    ]
    assert [(evidence.text, evidence.score_type) for evidence in vector.evidence] == [
        ("语义专用文档。", "vector")
    ]
    with pytest.raises(InvalidArgument, match="recall_mode"):
        kb.retrieve("关键词", namespace, scope, recall_mode="unknown")


def test_full_text_retrieval_segments_chinese_natural_language_query(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全文检索应以中文词项召回，不能要求问题中的每个虚词都出现在原文。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope(
        "reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"})
    )
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    kb = KnowOne(dsn)
    job = kb.ingest(
        TextSource("轮胎漏气时不要继续驾驶车辆。"),
        namespace,
        "tire",
        scope,
        "ingest-tire",
    )
    kb.process_job(job.job_id)
    revision_id = kb.get_ingestion(job.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id,
        namespace,
        datetime(2026, 9, 1, tzinfo=UTC),
        None,
        0,
        scope,
        "publish-tire",
    )

    result = kb.retrieve(
        "轮胎漏气还能继续开吗？",
        namespace,
        scope,
        at=datetime(2026, 9, 2, tzinfo=UTC),
        recall_mode="full_text",
    )

    assert [evidence.text for evidence in result.evidence] == ["轮胎漏气时不要继续驾驶车辆。"]


def test_retrieve_excludes_acl_denied_withdrawn_and_outside_window_chunks(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读取约束任一不满足时，检索必须安全地返回空证据。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    editor = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))
    reader = AccessScope("reader", frozenset({namespace}), frozenset({"read"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", editor, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, editor).revision_id
    assert revision_id is not None
    valid_from = datetime(2026, 9, 1, tzinfo=UTC)
    kb.publish(revision_id, namespace, valid_from, None, 0, editor, "publish")

    assert not kb.retrieve("保养周期为", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute(
            "UPDATE document SET acl = '{\"principals\": [\"reader\"]}'::jsonb "
            "WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        )
    assert kb.retrieve("保养周期为", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    assert not kb.retrieve("保养周期为", namespace, reader, at=datetime(2026, 8, 31, tzinfo=UTC)).evidence
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute(
            "UPDATE document SET withdrawn = true WHERE namespace_id = "
            "(SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        )
    assert not kb.retrieve("保养周期为", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence


def test_retrieve_uses_only_the_active_index_generation(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切换索引代后，保留的旧代 Chunk 不能重新进入检索结果。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, "publish"
    )

    upgraded_kb = KnowOne(dsn, embedding_model="test-upgraded-model")
    new_generation_id = upgraded_kb.create_index_generation(namespace)
    upgraded_kb.rebuild_index_generation(namespace, new_generation_id)
    upgraded_kb.activate_index_generation(namespace, new_generation_id)

    result = upgraded_kb.retrieve(
        "保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC)
    )
    assert result.index_generation == new_generation_id
    assert len(result.evidence) == 1


def test_retrieve_returns_a_vector_only_match(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """query 无全文词元命中时，向量召回仍可返回已发布的语义匹配原文。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))
    kb = KnowOne(dsn)
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [vector.copy() for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, "publish"
    )

    result = kb.retrieve("语义相近问题", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC))

    assert [evidence.text for evidence in result.evidence] == ["保养周期为 5000 公里。"]


def test_retrieve_rrf_prioritizes_a_chunk_returned_by_both_paths(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同时命中全文与向量的 Chunk 应获得高于单路命中的 RRF 分数。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))
    kb = KnowOne(dsn)
    vector = [0.0] * 1024
    vector[0] = 1.0
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [vector.copy() for _ in texts],
    )
    revisions: list[str] = []
    for source_key, text in (("both", "关键词命中。"), ("vector", "语义候选。")):
        ref = kb.ingest(TextSource(text), namespace, source_key, scope, f"ingest-{source_key}")
        kb.process_job(ref.job_id)
        revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
        assert revision_id is not None
        revisions.append(revision_id)
    valid_from = datetime(2026, 9, 1, tzinfo=UTC)
    for index, revision_id in enumerate(revisions):
        kb.publish(revision_id, namespace, valid_from, None, 0, scope, f"publish-{index}")

    result = kb.retrieve("关键词命中", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC))

    assert [evidence.text for evidence in result.evidence] == ["关键词命中。", "语义候选。"]
    assert result.evidence[0].score_type == "rrf"
    assert result.evidence[0].rank_score > result.evidence[1].rank_score


def test_hybrid_uses_a_larger_candidate_pool_than_its_final_result(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全文第 2 名且向量第 1 名的证据，不应因 top_k=1 在融合前被截断。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))

    def embed(_client: object, texts: list[str], **_kwargs: object) -> list[list[float]]:
        """构造全文与向量排序不同的候选，覆盖融合前截断回归。"""
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            if text == "关键词":
                vector[:2] = [1.0, 0.0]
            elif "语义目标" in text:
                vector[:2] = [1.0, 0.0]
            elif "中间候选" in text:
                vector[:2] = [0.8, 0.6]
            else:
                vector[:2] = [0.0, 1.0]
            vectors.append(vector)
        return vectors

    monkeypatch.setattr("know_one.core.api.OpenAIEmbeddingClient.embed", embed)
    kb = KnowOne(dsn)
    revisions = []
    for source_key, text in (
        ("keyword", "关键词 关键词 关键词 普通内容。"),
        ("target", "关键词语义目标。"),
        ("middle", "中间候选。"),
    ):
        job = kb.ingest(TextSource(text), namespace, source_key, scope, f"ingest-{source_key}")
        kb.process_job(job.job_id)
        revision_id = kb.get_ingestion(job.job_id, scope).revision_id
        assert revision_id is not None
        revisions.append(revision_id)
    for index, revision_id in enumerate(revisions):
        kb.publish(revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, f"publish-{index}")

    result = kb.retrieve(
        "关键词", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC), top_k=1
    )

    assert [evidence.text for evidence in result.evidence] == ["关键词语义目标。"]


def test_retrieve_filters_by_document_applicability(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """文档声明适用条件时，请求必须提供匹配条件才能读取。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("reader", frozenset({namespace}), frozenset({"ingest", "publish", "read"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("车型保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, "publish"
    )
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute(
            "UPDATE document SET applicability = '{\"model\": \"hybrid\"}'::jsonb "
            "WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        )

    with pytest.raises(InvalidArgument, match="applicability"):
        kb.retrieve("车型保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC))
    assert kb.retrieve(
        "车型保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC),
        applicability={"model": "hybrid"},
    ).evidence
    assert not kb.retrieve(
        "车型保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC),
        applicability={"model": "gas"},
    ).evidence


def test_withdraw_hides_published_document_and_replays_idempotently(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤回立即隐藏已发布内容，并只为首次请求递增状态代数。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope(
        "editor", frozenset({namespace}), frozenset({"ingest", "publish", "read", "withdraw"})
    )
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, "publish"
    )
    with psycopg.connect(dsn) as connection:
        document_id = connection.execute(
            "SELECT id FROM document WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        ).fetchone()[0]

    kb.withdraw(str(document_id), 1, scope, "withdraw")
    kb.withdraw(str(document_id), 1, scope, "withdraw")

    assert not kb.retrieve("保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    with psycopg.connect(dsn) as connection:
        withdrawn, state_generation = connection.execute(
            "SELECT withdrawn, state_generation FROM document WHERE id = %s", (document_id,)
        ).fetchone()
    assert (withdrawn, state_generation) == (True, 2)


def test_set_access_replaces_document_acl_for_subsequent_retrieval(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACL 变更提交后，旧读者失权且新读者立即获得同一已发布内容。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    editor = AccessScope(
        "editor",
        frozenset({namespace}),
        frozenset({"ingest", "publish", "read", "manage_acl"}),
    )
    reader = AccessScope("reader", frozenset({namespace}), frozenset({"read"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", editor, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, editor).revision_id
    assert revision_id is not None
    kb.publish(
        revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, editor, "publish"
    )
    with psycopg.connect(dsn) as connection:
        document_id = connection.execute(
            "SELECT id FROM document WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        ).fetchone()[0]

    kb.set_access(str(document_id), {"principals": ["reader"]}, 1, editor, "set-access")

    assert not kb.retrieve("保养周期为", namespace, editor, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    assert kb.retrieve("保养周期为", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence


def test_delete_marks_document_unretrievable_without_removing_audit_state(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除先切断检索可见性，并保留可审计的逻辑删除标记。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    scope = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "publish", "read", "delete"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr("know_one.core.api.OpenAIEmbeddingClient.embed", lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts])
    ref = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", scope, "ingest")
    kb.process_job(ref.job_id)
    revision_id = kb.get_ingestion(ref.job_id, scope).revision_id
    assert revision_id is not None
    kb.publish(revision_id, namespace, datetime(2026, 9, 1, tzinfo=UTC), None, 0, scope, "publish")
    with psycopg.connect(dsn) as connection:
        document_id = connection.execute("SELECT id FROM document WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)", (namespace,)).fetchone()[0]

    kb.delete(str(document_id), scope, "delete")
    kb.delete(str(document_id), scope, "delete")

    assert not kb.retrieve("保养周期为", namespace, scope, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    with psycopg.connect(dsn) as connection:
        deleted_at, withdrawn = connection.execute("SELECT deleted_at, withdrawn FROM document WHERE id = %s", (document_id,)).fetchone()
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM document_revision WHERE document_id = %s),
                (SELECT count(*) FROM chunk WHERE revision_id IN
                    (SELECT id FROM document_revision WHERE document_id = %s)),
                (SELECT count(*) FROM publication WHERE document_id = %s),
                (SELECT octet_length(source_bytes)
                 FROM ingestion_job WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)),
                (SELECT result_revision_id IS NULL
                 FROM ingestion_job WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s))
            """,
            (document_id, document_id, document_id, namespace, namespace),
        ).fetchone()
    assert deleted_at is not None
    assert withdrawn is True
    assert counts == (0, 0, 0, 0, True)


def test_local_lifecycle_cli_changes_published_document_visibility(
    dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """本地 CLI 的发布、ACL、撤回和删除均作用于真实持久化生命周期。"""
    namespace = f"test-{uuid4()}"
    create_namespace(dsn, namespace)
    editor = AccessScope("editor", frozenset({namespace}), frozenset({"ingest", "read"}))
    reader = AccessScope("reader", frozenset({namespace}), frozenset({"read"}))
    kb = KnowOne(dsn)
    monkeypatch.setattr(
        "know_one.core.api.OpenAIEmbeddingClient.embed",
        lambda _client, texts, **_kwargs: [[0.01] * 1024 for _ in texts],
    )
    job = kb.ingest(TextSource("保养周期为 5000 公里。"), namespace, "manual", editor, "ingest")
    kb.process_job(job.job_id)
    revision_id = kb.get_ingestion(job.job_id, editor).revision_id
    assert revision_id is not None
    with psycopg.connect(dsn) as connection:
        document_id = connection.execute(
            "SELECT id FROM document WHERE namespace_id = (SELECT id FROM namespace WHERE name = %s)",
            (namespace,),
        ).fetchone()[0]

    assert main(
        [
            "publish", namespace, revision_id, "2026-09-01T00:00:00+00:00",
            "--expected-generation", "0", "--idempotency-key", "publish-cli", "--dsn", dsn,
        ]
    ) == 0
    assert "Revision 已发布" in capsys.readouterr().out
    assert kb.retrieve("保养周期", namespace, editor, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence

    assert main(
        [
            "set-access", namespace, str(document_id), '{"principals":["reader"]}',
            "--expected-generation", "1", "--idempotency-key", "acl-cli", "--dsn", dsn,
        ]
    ) == 0
    assert "ACL 已更新" in capsys.readouterr().out
    assert not kb.retrieve("保养周期", namespace, editor, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence
    assert kb.retrieve("保养周期", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence

    assert main(
        [
            "withdraw", namespace, str(document_id), "--expected-generation", "2",
            "--idempotency-key", "withdraw-cli", "--dsn", dsn,
        ]
    ) == 0
    assert "Document 已撤回" in capsys.readouterr().out
    assert not kb.retrieve("保养周期", namespace, reader, at=datetime(2026, 9, 2, tzinfo=UTC)).evidence

    assert main(
        ["delete", namespace, str(document_id), "--idempotency-key", "delete-cli", "--dsn", dsn]
    ) == 0
    assert "Document 内容已删除" in capsys.readouterr().out
