"""KnowOne 业务门面。

接口契约的权威定义见 docs/contracts.md；数据生命周期见
docs/architecture.md「数据生命周期与变更流程」。

首版实现 ingest / get_ingestion 及配套的 worker 执行入口 process_job、
IndexGeneration 管理、发布和生命周期管理，以及受约束的混合检索。

用法示意（README 中的调用形态）::

    kb = KnowOne(dsn="postgresql://knowone:knowone@localhost:5432/knowone")
    ref = kb.ingest(source=FileSource(path), namespace="rav4",
                    source_key="rav4-hybrid-user-manual",
                    access_scope=scope, idempotency_key="ing-20260924-001")
    kb.process_job(ref.job_id)          # worker 执行：解析→清洗→切块→向量化→校验
    st = kb.get_ingestion(ref.job_id, scope)
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
import re
import subprocess
import tempfile
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from know_one.config import load_local_env
from know_one.embedding import OpenAIEmbeddingClient
from know_one.errors import (
    AccessDenied,
    ConcurrentModification,
    DeadlineExceeded,
    DependencyUnavailable,
    IdempotencyConflict,
    InvalidArgument,
    NotFoundOrForbidden,
    PublicationConflict,
    UnsupportedSource,
)
from know_one.full_text import FULL_TEXT_CONFIG_VERSION, build_or_tsquery, tokenize_text
from know_one.model import (
    AccessScope,
    ContextPart,
    Evidence,
    IngestionJobRef,
    IngestionStatus,
    RetrievalResult,
    Source,
)


RETRIEVAL_CANDIDATE_LIMIT = 50
# 整本手册中“轮胎”“驻车”等高频词会让全文候选重复命中；全文保留较弱加分。
# 0.25 是 golden 草案与锁定集双验证的平衡点：词面强、语义弱的题（如未注册钥匙）
# 可借全文加分进入前列；0.30 实验会把锁定集 traction-battery-001 挤出 top-k，不可再调高。
FULL_TEXT_RRF_WEIGHT = 0.25
VECTOR_RRF_WEIGHT = 1.0
# 相邻块只在调用方显式请求补充上下文时读取；绝不参与主 Evidence 的排序。
CONTEXT_PART_NEIGHBOR_DISTANCE = 2
CONTEXT_PART_LIMIT = 4
# 本地 embedding 服务对整本手册的单次 432 Chunk 请求超过默认 30 秒预算；
# 64 条在实际探针中稳定完成，同时保留足够吞吐，避免为每段单独发起请求。
EMBEDDING_BATCH_SIZE = 64
# 真实 PDF 的 64 个段落可累积到近 4 万字符并超过本地模型 30 秒预算；
# 字符上限与条数上限共同约束单次请求，单段超限时仍单独提交以保留原文边界。
EMBEDDING_BATCH_CHAR_LIMIT = 12_000
# PDF 阅读顺序经常把整个页面排成一个段落。超过此长度时优先按手册小节切开，
# 避免页面末尾的具体操作被页面开头的泛化说明稀释。
PDF_CHUNK_MAX_CHARS = 400


def _embedding_batches(texts: list[str]) -> list[list[str]]:
    """按条数和字符数拆分文本，避免短文本探针掩盖长文请求超时。"""
    batches: list[list[str]] = []
    batch: list[str] = []
    character_count = 0
    for text in texts:
        if batch and (
            len(batch) == EMBEDDING_BATCH_SIZE
            or character_count + len(text) > EMBEDDING_BATCH_CHAR_LIMIT
        ):
            batches.append(batch)
            batch = []
            character_count = 0
        batch.append(text)
        character_count += len(text)
    if batch:
        batches.append(batch)
    return batches


def _embed_one_batch(
    client: OpenAIEmbeddingClient, batch: list[str], start: int
) -> list[list[float]]:
    """提交一个批次；仅在超时时二分重试，避免慢批次中止整个入库任务。"""
    try:
        return client.embed(batch)
    except DependencyUnavailable as error:
        # 网络不可用等故障不会因缩小请求而恢复，不能放大成大量无效请求。
        # 超时则通常意味着该批正文的 token 密度或服务瞬时负载偏高，二分可保留已完成的前序批次。
        if len(batch) > 1 and "超时" in str(error):
            midpoint = len(batch) // 2
            return _embed_one_batch(client, batch[:midpoint], start) + _embed_one_batch(
                client, batch[midpoint:], start + midpoint
            )
        # 仅附加批次元数据，帮助定位模型上下文／网关限制，绝不记录正文。
        raise DependencyUnavailable(
            f"embedding 批次 start={start} chunks={len(batch)} chars={sum(map(len, batch))} 失败：{error}"
        ) from error


def _embed_texts_in_batches(client: OpenAIEmbeddingClient, texts: list[str]) -> list[list[float]]:
    """按上限分批向 embedding 服务提交，保持输出与输入的顺序一一对应。"""
    vectors: list[list[float]] = []
    start = 0
    for batch in _embedding_batches(texts):
        vectors.extend(_embed_one_batch(client, batch, start))
        start += len(batch)
    return vectors


def _fuse_rrf_candidates(
    full_text_rows: list[dict], vector_rows: list[dict], *, top_k: int
) -> list[tuple[dict, float]]:
    """按 RRF 合并两路候选，并保留向量首位的语义锚点。"""
    fused: dict[object, tuple[dict, float]] = {}
    for candidates, weight in (
        (full_text_rows, FULL_TEXT_RRF_WEIGHT),
        (vector_rows, VECTOR_RRF_WEIGHT),
    ):
        for rank, row in enumerate(candidates, start=1):
            existing = fused.get(row["chunk_id"])
            score = weight / (60 + rank) + (existing[1] if existing else 0.0)
            fused[row["chunk_id"]] = (row, score)
    ranked = sorted(fused.values(), key=lambda item: -item[1])[:top_k]
    if not vector_rows or top_k <= 0:
        return ranked

    # 全文高频词可能使多个泛化段落叠加得分并挤出向量首位。
    # 保留该语义锚点，避免融合结果反而丢失最接近用户问题的候选。
    vector_anchor = fused[vector_rows[0]["chunk_id"]]
    if vector_anchor not in ranked:
        ranked = ranked[:-1] + [vector_anchor]
        ranked.sort(key=lambda item: -item[1])
    return ranked


class KnowOne:
    """统一入库与检索能力的门面。

    一个实例持有数据库连接池与可注入的模型/存储依赖；
    线程安全性：M1 面向 worker 批量场景，方法可在多线程调用，
    但不做连接池外的并发承诺。
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        embedding_endpoint: str | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ) -> None:
        """创建门面实例。

        Args:
            dsn: PostgreSQL 连接串（ADR-0004）。未传入时读取 ``KNOWONE_DSN``。
            embedding_endpoint: embedding 模型端点（OpenAI 兼容接口，
                如 LM Studio ``http://localhost:1234/v1``）；
                None 时从环境变量 KNOWONE_EMBEDDING_ENDPOINT 读取。
                外部模型调用一律不进入数据库事务（pipeline.md「切块、向量化与写入」）。
            embedding_model: embedding 模型标识；None 时读取
                ``KNOWONE_EMBEDDING_MODEL``。当前默认值为
                ``Qwen/Qwen3-Embedding-0.6B``，后续 IndexGeneration 将保存它。
            embedding_dimensions: 向量维度；None 时读取
                ``KNOWONE_EMBEDDING_DIMENSIONS``，缺省使用 Qwen 0.6B 的 1024 维。
        """
        load_local_env()
        self._dsn = dsn or os.environ.get("KNOWONE_DSN", "")
        if not self._dsn.strip():
            raise InvalidArgument("dsn 不能为空；请传入 dsn 或设置 KNOWONE_DSN")
        # M1 尚未调用 embedding；先统一读取配置，M2 接入模型客户端时复用。
        self._embedding_endpoint = embedding_endpoint or os.environ.get(
            "KNOWONE_EMBEDDING_ENDPOINT"
        )
        self._embedding_model = embedding_model or os.environ.get("KNOWONE_EMBEDDING_MODEL")
        configured_dimensions = embedding_dimensions or os.environ.get(
            "KNOWONE_EMBEDDING_DIMENSIONS", "1024"
        )
        try:
            self._embedding_dimensions = int(configured_dimensions)
        except (TypeError, ValueError) as error:
            raise InvalidArgument("KNOWONE_EMBEDDING_DIMENSIONS 必须是正整数") from error
        if self._embedding_dimensions <= 0:
            raise InvalidArgument("KNOWONE_EMBEDDING_DIMENSIONS 必须是正整数")

    # ------------------------------------------------------------------
    # 入库（M1 实现）
    # ------------------------------------------------------------------

    def ingest(
        self,
        source: Source,
        namespace: str,
        source_key: str,
        access_scope: AccessScope,
        idempotency_key: str,
    ) -> IngestionJobRef:
        """提交入库：把 Source 变为可检索 Chunk 的异步任务。

        语义（contracts「ingest 与任务状态」）：
        - 返回持久化后的 IngestionJobRef，不承诺调用返回时可检索；
        - 提交时立即调用 source.snapshot() 保存不可变快照并计算内容摘要，
          写入 operation_receipt（operation="ingest"）与 ingestion_job
          （status=queued，保存 source_key 字符串，此时 document 可能尚不存在，
          故不加外键）；
        - 幂等：(namespace, "ingest", idempotency_key) 内唯一。同键同指纹
          （内容摘要 + source_key + 构建参数）返回首次操作的任务；
          同键不同指纹抛 IdempotencyConflict；
        - 若该 Document 的内容 hash 未变化且当前 IndexGeneration 已有完整构建，
          任务直接进入 ready 并复用已有 DocumentRevision，不新建。

        Args:
            source: 入库来源（PDF 等）。必须能被 worker 持久读取；
                提交时保存不可变快照，不接受临时文件路径。
            namespace: 目标 Namespace name；必须存在于 namespace 表，
                且 access_scope 需含 (namespace, "ingest") 权限。
            source_key: 稳定外部 ID 或宿主分配的标识；
                (namespace, source_key) 构成 Document 身份。
                不使用可变化的标题或内容 hash。变体差异（如汽油版/混动版）
                用不同 source_key + 元数据表达，不做代码分支。
            access_scope: 宿主构造的可信授权范围；缺失或无 ingest 权限时
                抛 AccessDenied，不降级。
            idempotency_key: 调用方幂等键；同一业务操作在重试时必须复用同键。

        Returns:
            IngestionJobRef：job_id + 提交时状态（通常 queued）。

        Raises:
            AccessDenied: scope 缺失或无 (namespace, ingest) 权限。
            InvalidArgument: source_key 为空或超长、source.media_type 不明等。
            UnsupportedSource: 媒体类型没有已注册的解析器。
            IdempotencyConflict: 同键不同请求指纹。
        """
        self._require_permission(access_scope, namespace, "ingest")
        self._validate_ingest_arguments(source, namespace, source_key, idempotency_key)

        source_bytes = source.snapshot()
        if source.media_type != "application/pdf":
            try:
                source_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise InvalidArgument("文本来源必须是 UTF-8 编码") from error
        if not source_bytes.strip():
            raise InvalidArgument("来源文本不能为空")

        source_snapshot = source.describe()
        if not isinstance(source_snapshot, dict):
            raise InvalidArgument("source.describe() 必须返回字典")

        content_hash = sha256(source_bytes).hexdigest()
        request_fingerprint = self._fingerprint(source_key, content_hash, source.media_type)

        with self._connect() as connection, connection.transaction():
            namespace_row = self._namespace_row(connection, namespace)
            receipt = connection.execute(
                """
                SELECT request_fingerprint, result_ref
                FROM operation_receipt
                WHERE namespace_id = %(namespace_id)s
                  AND operation = 'ingest'
                  AND idempotency_key = %(idempotency_key)s
                FOR UPDATE
                """,
                {"namespace_id": namespace_row["id"], "idempotency_key": idempotency_key},
            ).fetchone()
            if receipt:
                if receipt["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict("同一幂等键对应了不同的入库请求")
                result = receipt["result_ref"]
                return IngestionJobRef(
                    job_id=str(result["job_id"]),
                    namespace=namespace,
                    status=result["status"],
                )

            document_id = self._ensure_document(
                connection, namespace_row["id"], source_key, access_scope.principal_id
            )
            job_id = str(uuid4())
            source_ref = str(source_snapshot.get("source_name", "inline-text"))
            connection.execute(
                """
                INSERT INTO ingestion_job (
                    id, namespace_id, source_key, source_ref, source_bytes,
                    source_media_type, source_snapshot, content_hash,
                    idempotency_key, request_fingerprint, status
                ) VALUES (
                    %(id)s, %(namespace_id)s, %(source_key)s, %(source_ref)s,
                    %(source_bytes)s, %(source_media_type)s, %(source_snapshot)s,
                    %(content_hash)s, %(idempotency_key)s, %(request_fingerprint)s, 'queued'
                )
                """,
                {
                    "id": job_id,
                    "namespace_id": namespace_row["id"],
                    "source_key": source_key,
                    "source_ref": source_ref,
                    "source_bytes": source_bytes,
                    "source_media_type": source.media_type,
                    "source_snapshot": Jsonb(source_snapshot),
                    "content_hash": content_hash,
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                },
            )
            result = {"job_id": job_id, "status": "queued", "document_id": str(document_id)}
            connection.execute(
                """
                INSERT INTO operation_receipt (
                    id, namespace_id, operation, idempotency_key,
                    request_fingerprint, status, result_ref, completed_at
                ) VALUES (
                    %(id)s, %(namespace_id)s, 'ingest', %(idempotency_key)s,
                    %(request_fingerprint)s, 'succeeded', %(result_ref)s, now()
                )
                """,
                {
                    "id": str(uuid4()),
                    "namespace_id": namespace_row["id"],
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "result_ref": Jsonb(result),
                },
            )
        return IngestionJobRef(job_id=job_id, namespace=namespace, status="queued")

    def get_ingestion(self, job_id: str, access_scope: AccessScope) -> IngestionStatus:
        """查询入库任务的阶段、进度与结果。

        语义（contracts「ingest 与任务状态」）：
        - 返回 stage / progress / error_code / attempt_count / warnings /
          revision_id / preview；
        - ready 仅表示内容构建并通过完整性校验（revision_index_build.status=ready），
          尚未发布；是否可检索另查 Publication；
        - 批量导入返回逐项任务，不用一个「成功」掩盖部分失败。

        Args:
            job_id: ingest 返回的任务 ID。
            access_scope: 宿主构造的可信授权范围；需要对该任务所属
                Namespace 的管理可见权限（M1 要求 ingest 同级权限）。

        Returns:
            IngestionStatus：字段语义见模型定义。

        Raises:
            AccessDenied: scope 无权访问该任务。
            NotFoundOrForbidden: 任务不存在或无权知晓。
        """
        if not job_id.strip():
            raise InvalidArgument("job_id 不能为空")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT job.id, ns.name AS namespace, job.status, job.stage,
                       job.attempt_count, job.error_code, job.result_revision_id,
                       build.expected_chunk_count, build.completed_chunk_count
                FROM ingestion_job AS job
                JOIN namespace AS ns ON ns.id = job.namespace_id
                LEFT JOIN revision_index_build AS build
                  ON build.revision_id = job.result_revision_id
                 AND build.index_generation_id = ns.current_index_generation_id
                WHERE job.id = %s
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise NotFoundOrForbidden("任务不存在或无权访问")
        self._require_permission(access_scope, row["namespace"], "ingest")
        return IngestionStatus(
            job_id=str(row["id"]),
            namespace=row["namespace"],
            status=row["status"],
            stage=row["stage"],
            progress_done=row["completed_chunk_count"] or 0,
            progress_total=row["expected_chunk_count"],
            error_code=row["error_code"],
            attempt_count=row["attempt_count"],
            revision_id=(str(row["result_revision_id"]) if row["result_revision_id"] else None),
        )

    def create_index_generation(self, namespace: str) -> str:
        """创建使用当前运行时配置的 building IndexGeneration。

        此接口供受控的本地管理入口调用。配置一经写入 generation 即不可
        覆盖；与当前 active generation 配置相同的请求明确失败，避免产生
        无意义的代次并掩盖本应执行的重建流程。
        """
        if not namespace.strip():
            raise InvalidArgument("namespace 不能为空")
        if not self._embedding_model:
            raise InvalidArgument("创建 IndexGeneration 需要 embedding 模型配置")

        config = {
            # v3：段内新增次级标题短行软边界，避免表格被前置段落合并污染
            # （transmission-001 病历）；■ 小节与超长句读兜底规则不变。
            "chunker": "m1-pdf-section-v3-table",
            "embedding_model": self._embedding_model,
            "dimensions": self._embedding_dimensions,
            "full_text": FULL_TEXT_CONFIG_VERSION,
        }
        fingerprint = sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
        generation_id = uuid4()
        with self._connect() as connection, connection.transaction():
            namespace_row = connection.execute(
                """
                SELECT namespace.id, generation.config_fingerprint
                FROM namespace
                LEFT JOIN index_generation AS generation
                  ON generation.id = namespace.current_index_generation_id
                WHERE namespace.name = %s
                FOR UPDATE OF namespace
                """,
                (namespace,),
            ).fetchone()
            if namespace_row is None:
                raise NotFoundOrForbidden("Namespace 不存在或无权访问")
            if namespace_row["config_fingerprint"] == fingerprint:
                raise InvalidArgument("新 IndexGeneration 的配置必须与当前代不同")
            connection.execute(
                """
                INSERT INTO index_generation (
                    id, namespace_id, config_fingerprint, embedding_model,
                    tokenizer_version, dims, distance, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'cosine', 'building')
                """,
                (
                    generation_id,
                    namespace_row["id"],
                    fingerprint,
                    self._embedding_model,
                    self._embedding_model,
                    self._embedding_dimensions,
                ),
            )
        return str(generation_id)

    def rebuild_index_generation(self, namespace: str, generation_id: str) -> None:
        """为 building IndexGeneration 重建该 Namespace 的全部 Revision。

        M1 的 Revision 原文由成功入库任务持久保存。每个 Revision 的 embedding
        在事务外生成，随后在短事务中替换该 generation 的草稿 Chunk 并完成
        ``revision_index_build``，因此失败不会影响任何 active generation。
        """
        if not namespace.strip() or not generation_id.strip():
            raise InvalidArgument("namespace 和 generation_id 均不能为空")
        with self._connect() as connection:
            generation = connection.execute(
                """
                SELECT generation.id, generation.namespace_id, generation.status,
                       generation.embedding_model, generation.dims
                FROM index_generation AS generation
                JOIN namespace ON namespace.id = generation.namespace_id
                WHERE namespace.name = %s AND generation.id = %s
                """,
                (namespace, generation_id),
            ).fetchone()
            if generation is None:
                raise NotFoundOrForbidden("IndexGeneration 不存在或不属于该 Namespace")
            if generation["status"] != "building":
                raise InvalidArgument("只有 building IndexGeneration 可以重建")
            if (
                generation["embedding_model"] != self._embedding_model
                or generation["dims"] != self._embedding_dimensions
            ):
                raise InvalidArgument("目标 IndexGeneration 与本地 embedding 配置不一致")
            revisions = connection.execute(
                """
                SELECT revision.id, source.source_bytes, source.source_media_type,
                       source.source_snapshot
                FROM document_revision AS revision
                JOIN document ON document.id = revision.document_id
                JOIN LATERAL (
                    SELECT source_bytes, source_media_type, source_snapshot
                    FROM ingestion_job
                    WHERE result_revision_id = revision.id
                    ORDER BY created_at
                    LIMIT 1
                ) AS source ON true
                WHERE document.namespace_id = %s
                ORDER BY revision.created_at
                """,
                (generation["namespace_id"],),
            ).fetchall()

        for revision in revisions:
            text = self._source_text(
                bytes(revision["source_bytes"]),
                revision["source_media_type"],
                revision["source_snapshot"],
            )
            chunks = self._chunks_for_media_type(text, revision["source_media_type"])
            if not chunks:
                raise InvalidArgument("Revision 原文中没有可切分的文本段落")
            # 外部网络调用绝不放入数据库事务，避免长时间持锁。
            embedding_client = OpenAIEmbeddingClient(
                self._embedding_endpoint, self._embedding_model, self._embedding_dimensions
            )
            vectors = _embed_texts_in_batches(embedding_client, [chunk[0] for chunk in chunks])
            self._write_rebuilt_revision(
                generation["namespace_id"], generation_id, revision["id"], chunks, vectors,
                revision["source_media_type"], text, revision["source_snapshot"],
            )

    def _write_rebuilt_revision(
        self,
        namespace_id: object,
        generation_id: str,
        revision_id: object,
        chunks: list[tuple[str, int, int, tuple[str, ...]]],
        vectors: list[list[float]],
        source_media_type: str,
        source_text: str,
        source_snapshot: dict[str, object],
    ) -> None:
        """在短事务中覆盖一个 Revision 在 building generation 中的草稿。"""
        with self._connect() as connection, connection.transaction():
            generation = connection.execute(
                """
                SELECT status FROM index_generation
                WHERE id = %s AND namespace_id = %s
                FOR UPDATE
                """,
                (generation_id, namespace_id),
            ).fetchone()
            if generation is None or generation["status"] != "building":
                raise InvalidArgument("目标 IndexGeneration 已不处于 building 状态")
            build = connection.execute(
                """
                SELECT status FROM revision_index_build
                WHERE revision_id = %s AND index_generation_id = %s
                FOR UPDATE
                """,
                (revision_id, generation_id),
            ).fetchone()
            if build and build["status"] == "ready":
                return
            if build:
                # 重试从干净草稿开始，避免旧的半成品混入本次完整性校验。
                connection.execute(
                    "DELETE FROM chunk WHERE revision_id = %s AND index_generation_id = %s",
                    (revision_id, generation_id),
                )
                connection.execute(
                    """
                    UPDATE revision_index_build
                    SET status = 'building', expected_chunk_count = NULL,
                        completed_chunk_count = 0, error_code = NULL, completed_at = NULL
                    WHERE revision_id = %s AND index_generation_id = %s
                    """,
                    (revision_id, generation_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO revision_index_build (revision_id, index_generation_id, status)
                    VALUES (%s, %s, 'building')
                    """,
                    (revision_id, generation_id),
                )
            for ordinal, ((raw_text, start, end, heading_path), vector) in enumerate(
                zip(chunks, vectors, strict=True)
            ):
                search_text = tokenize_text(raw_text)
                connection.execute(
                    """
                    INSERT INTO chunk (
                        id, namespace_id, revision_id, index_generation_id, ordinal,
                        raw_text, search_text, heading_path, source_locator, embedding, tsv
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                              CAST(%s AS vector), to_tsvector('simple', %s))
                    """,
                    (
                        uuid4(), namespace_id, revision_id, generation_id, ordinal,
                        raw_text, search_text, list(heading_path),
                        Jsonb(
                            self._source_locator(
                                source_media_type, source_text, start, end, source_snapshot
                            )
                        ),
                        "[" + ",".join(str(value) for value in vector) + "]", search_text,
                    ),
                )
            connection.execute(
                """
                UPDATE revision_index_build
                SET status = 'ready', expected_chunk_count = %s,
                    completed_chunk_count = %s, completed_at = now()
                WHERE revision_id = %s AND index_generation_id = %s
                """,
                (len(chunks), len(chunks), revision_id, generation_id),
            )

    def activate_index_generation(self, namespace: str, generation_id: str) -> None:
        """原子切换已完成重建的 IndexGeneration。

        激活前以 Namespace 锁固定校验范围：该 Namespace 的每个 Revision
        都必须在目标代有完整 ready 构建。切换时同时更新当前指针和新旧代
        状态，因此后续入库会读取新 active generation，不会继续写入旧代。
        """
        if not namespace.strip() or not generation_id.strip():
            raise InvalidArgument("namespace 和 generation_id 均不能为空")
        with self._connect() as connection, connection.transaction():
            namespace_row = connection.execute(
                """
                SELECT id, current_index_generation_id
                FROM namespace WHERE name = %s
                FOR UPDATE
                """,
                (namespace,),
            ).fetchone()
            if namespace_row is None:
                raise NotFoundOrForbidden("Namespace 不存在或无权访问")
            generation = connection.execute(
                """
                SELECT id, status FROM index_generation
                WHERE id = %s AND namespace_id = %s
                FOR UPDATE
                """,
                (generation_id, namespace_row["id"]),
            ).fetchone()
            if generation is None:
                raise NotFoundOrForbidden("IndexGeneration 不存在或不属于该 Namespace")
            if generation["status"] != "building":
                raise InvalidArgument("只有 building IndexGeneration 可以激活")
            missing = connection.execute(
                """
                SELECT revision.id
                FROM document_revision AS revision
                JOIN document ON document.id = revision.document_id
                WHERE document.namespace_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM revision_index_build AS build
                      WHERE build.revision_id = revision.id
                        AND build.index_generation_id = %s
                        AND build.status = 'ready'
                  )
                LIMIT 1
                """,
                (namespace_row["id"], generation_id),
            ).fetchone()
            if missing is not None:
                raise InvalidArgument("IndexGeneration 尚未完成全部 Revision 的重建")

            # 指针与状态在同一短事务提交，读者不会观察到半完成的切换。
            connection.execute(
                "UPDATE index_generation SET status = 'retired' WHERE id = %s",
                (namespace_row["current_index_generation_id"],),
            )
            connection.execute(
                "UPDATE index_generation SET status = 'active' WHERE id = %s",
                (generation_id,),
            )
            connection.execute(
                "UPDATE namespace SET current_index_generation_id = %s WHERE id = %s",
                (generation_id, namespace_row["id"]),
            )

    def process_job(self, job_id: str, *, lease_seconds: int = 300) -> None:
        """worker 执行入口：领取并处理一个任务（实现层方法，不属于对外契约）。

        流水线（pipeline.md「入库：构建与发布分离」）：
        解析 → 保守清洗 → 结构/句子切块 → embedding + 全文字段 →
        完整性校验 → revision_index_build=ready / failed / needs_review。

        幂等与恢复（contracts「ingest 与任务状态」）：
        - running 期间持有可过期租约（ingestion_job.lease_expires_at），
          长阶段应定期续租；
        - 重试可能重复执行阶段，写入依靠
          (revision_id, index_generation_id, ordinal) 唯一键与完成标记保证幂等；
        - 失败置 ingestion_job.status=failed 并记录 error_code，
          不影响已发布 revision。

        Args:
            job_id: 要执行的任务 ID；任务必须处于 queued 或租约已过期的 running。
            lease_seconds: 本次租约时长（秒）；超时未完成视为 worker 失联，
                任务可被其他 worker 重新领取。
        """
        if lease_seconds <= 0:
            raise InvalidArgument("lease_seconds 必须大于 0")
        try:
            self._process_job(job_id, lease_seconds)
        except (InvalidArgument, NotFoundOrForbidden):
            # 未领取的任务、已被其他 worker 持有等预期状态不能被误标失败。
            raise
        except Exception:
            # 处理事务会回滚；另开短事务仅记录失败状态，供调用方和重试器观察。
            self._mark_job_failed(job_id)
            raise

    def process_next_job(self, *, lease_seconds: int = 300) -> str | None:
        """领取并处理下一条可恢复的入库任务，空队列时返回 ``None``。

        此入口只处理一条任务，供宿主的 cron、进程管理器或常驻 worker 循环调用。
        候选选择跳过正被其他 worker 锁住的行；实际领取仍复用 ``process_job``
        的行锁与 lease 检查，因此两个 worker 即使短暂看到同一候选，也只有一个
        能在有效租约内开始处理。
        """
        if lease_seconds <= 0:
            raise InvalidArgument("lease_seconds 必须大于 0")
        with self._connect() as connection, connection.transaction():
            job = connection.execute(
                """
                SELECT id
                FROM ingestion_job
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at <= now())
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
        if job is None:
            return None
        job_id = str(job["id"])
        try:
            self.process_job(job_id, lease_seconds=lease_seconds)
        except InvalidArgument as error:
            # 选择事务提交后，另一 worker 可能已先领取该任务；这不是本 worker 的
            # 处理失败，下一次调度会继续寻找剩余任务。
            if str(error) in {"任务正在被其他 worker 处理", "任务状态 failed 不能被处理"}:
                return None
            raise
        return job_id

    def _connect(self) -> psycopg.Connection:
        """创建一个短生命周期连接；M1 避免过早引入连接池配置。"""
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @staticmethod
    def _set_retrieval_statement_timeout(connection: psycopg.Connection, deadline: float) -> None:
        """把剩余检索预算映射为当前数据库事务的 statement_timeout。"""
        remaining_ms = int((deadline - monotonic()) * 1000)
        if remaining_ms <= 0:
            raise DeadlineExceeded("检索 deadline 已耗尽")
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{remaining_ms}ms",),
        )

    @staticmethod
    def _require_permission(scope: AccessScope, namespace: str, permission: str) -> None:
        """在任何数据库写入前失败关闭地检查调用方授权。"""
        if not scope.allows(namespace, permission):
            raise AccessDenied(f"无权在 Namespace {namespace!r} 执行 {permission}")

    @staticmethod
    def _validate_ingest_arguments(
        source: Source, namespace: str, source_key: str, idempotency_key: str
    ) -> None:
        """校验当前已注册的 UTF-8 文本来源类型与公共入库参数。"""
        if not namespace.strip() or not source_key.strip() or not idempotency_key.strip():
            raise InvalidArgument("namespace、source_key 和 idempotency_key 均不能为空")
        if len(source_key) > 512:
            raise InvalidArgument("source_key 不能超过 512 个字符")
        if getattr(source, "media_type", None) not in {"text/plain", "text/markdown", "application/pdf"}:
            raise UnsupportedSource("仅支持 text/plain、text/markdown 和 application/pdf 来源")

    @staticmethod
    def _fingerprint(source_key: str, content_hash: str, media_type: str) -> str:
        """用稳定字段生成幂等比较指纹，不把可变来源名称混进去。"""
        value = f"{source_key}\0{content_hash}\0{media_type}".encode()
        return sha256(value).hexdigest()

    @staticmethod
    def _namespace_row(connection: psycopg.Connection, namespace: str) -> dict:
        """读取 Namespace 与当前索引代；M1 不替部署方自动创建它们。"""
        row = connection.execute(
            "SELECT id, current_index_generation_id FROM namespace WHERE name = %s",
            (namespace,),
        ).fetchone()
        if row is None:
            raise NotFoundOrForbidden("Namespace 不存在或无权访问")
        if row["current_index_generation_id"] is None:
            raise InvalidArgument("Namespace 尚未设置当前 IndexGeneration")
        return row

    @staticmethod
    def _ensure_document(
        connection: psycopg.Connection,
        namespace_id: object,
        source_key: str,
        principal_id: str,
    ) -> object:
        """在提交时预留 Document 身份，并只在首次创建时写入默认 ACL。"""
        row = connection.execute(
            """
            SELECT id FROM document
            WHERE namespace_id = %s AND source_key = %s
            FOR UPDATE
            """,
            (namespace_id, source_key),
        ).fetchone()
        if row:
            return row["id"]
        document_id = uuid4()
        connection.execute(
            """
            INSERT INTO document (id, namespace_id, source_key, acl)
            VALUES (%s, %s, %s, %s)
            """,
            (document_id, namespace_id, source_key, Jsonb({"principals": [principal_id]})),
        )
        return document_id

    @staticmethod
    def _paragraphs(text: str) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """按空行切分连续原文段落，保留字符区间供证据定位。

        这是有意保守的 M1 兜底切块；标题树、FAQ、表格及句子依赖关系由
        后续解析器提供后再接入，不在这里假装理解语义。
        """
        chunks: list[tuple[str, int, int, tuple[str, ...]]] = []
        for match in re.finditer(r"\S(?:.*?\S)?(?=\s*\n\s*\n|\s*\Z)", text, re.DOTALL):
            raw_text = match.group(0)
            chunks.append((raw_text, match.start(), match.end(), ()))
        return chunks

    @staticmethod
    def _markdown_paragraphs(text: str) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """按 Markdown ATX 标题切分段落，并保留段落所在标题路径与原文区间。

        围栏代码块采用 CommonMark 的简化规则：同字符（``` 或 ~~~）、闭栏不短于
        开栏、闭栏不带信息串；未闭合时延伸到文档结束。围栏内的 # 行是代码
        注释，不得识别为标题，否则会污染其后所有正文的标题路径。

        其余有意保守的边界：不识别 Setext（下划线式）标题与孤立空标题行，
        两者均按正文保留；tests/test_markdown_parsing.py 的钉子测试固化了
        这些行为。
        """
        chunks: list[tuple[str, int, int, tuple[str, ...]]] = []
        headings: list[str] = []
        paragraph_start: int | None = None
        paragraph_end: int | None = None
        paragraph_path: tuple[str, ...] = ()
        fence_char = ""  # 当前所在围栏的标记字符；空串表示不在围栏内
        fence_length = 0  # 开栏标记长度，闭栏必须不短于它
        offset = 0

        def finish_paragraph() -> None:
            """提交当前非空段落；标题行本身不是可引用正文。"""
            nonlocal paragraph_start, paragraph_end
            if paragraph_start is not None and paragraph_end is not None:
                chunks.append(
                    (text[paragraph_start:paragraph_end], paragraph_start, paragraph_end, paragraph_path)
                )
            paragraph_start = None
            paragraph_end = None

        for line in text.splitlines(keepends=True):
            line_end = offset + len(line)
            content = line.rstrip("\r\n")
            # 一行内的围栏标记：至多 3 个前导空格 + 3 个以上反引号或波浪线，
            # 后面可跟信息串（开栏）或仅空白（闭栏）。
            fence = re.fullmatch(r" {0,3}(`{3,}|~{3,})[ \t]*(.*)", content)
            if fence_char:
                # 围栏内不识别标题；只有同字符、不短于开栏且无信息串的裸标记行
                # 才能闭合围栏，围栏内容和闭栏行都作为正文保留。
                if (
                    fence
                    and fence.group(2).strip() == ""
                    and fence.group(1)[0] == fence_char
                    and len(fence.group(1)) >= fence_length
                ):
                    fence_char = ""
                    fence_length = 0
                body = True
            else:
                heading = re.fullmatch(r"(#{1,6})[ \t]+(.+?)[ \t]*", content)
                if fence:
                    # 开栏行本身进入正文片段，保证证据能回溯到原文。
                    fence_char = fence.group(1)[0]
                    fence_length = len(fence.group(1))
                    body = True
                elif heading:
                    finish_paragraph()
                    level = len(heading.group(1))
                    # 闭合 # 序列前置空格才剥离（CommonMark 规则）；否则 C#、
                    # F# 这类以 # 结尾的标题会被静默截断。
                    title = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2)).rstrip()
                    headings[level - 1 :] = [title]
                    body = False
                elif not content.strip():
                    finish_paragraph()
                    body = False
                else:
                    body = True
            if body:
                if paragraph_start is None:
                    paragraph_start = offset
                    paragraph_path = tuple(headings)
                paragraph_end = offset + len(content)
            offset = line_end
        finish_paragraph()
        return chunks

    @classmethod
    def _chunks_for_media_type(
        cls, text: str, media_type: str
    ) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """按提交时冻结的媒体类型解析，保证索引重建复现原始切块规则。

        标题路径仅存入 heading_path 列，暂不拼入 search_text/embedding，
        即不参与全文与向量召回；这是首版有意取舍，待检索评测后再调整。
        """
        if media_type == "text/plain":
            return cls._paragraphs(text)
        if media_type == "text/markdown":
            return cls._markdown_paragraphs(text)
        if media_type == "application/pdf":
            chunks = []
            offset = 0
            # form feed 是 pdftotext 的页边界；不得让段落跨页，否则 Evidence 无法
            # 指向唯一页码。保留全局字符偏移以便回查冻结的提取文本。
            for page in text.split("\f"):
                for raw_text, start, end, heading_path in cls._paragraphs(page):
                    if cls._is_low_information_pdf_chunk(raw_text):
                        continue
                    chunks.extend(
                        cls._split_pdf_paragraph(raw_text, offset + start, heading_path)
                    )
                offset += len(page) + 1
            return chunks
        raise UnsupportedSource(f"未注册的来源媒体类型：{media_type}")

    @staticmethod
    def _is_low_information_pdf_chunk(raw_text: str) -> bool:
        """识别 pdftotext 产生的孤立页码或短页眉，避免它们污染召回。"""
        normalized = " ".join(raw_text.split())
        if re.fullmatch(r"\d+", normalized):
            return True
        return bool(
            len(normalized) <= 40
            and re.fullmatch(r"\d+\s+\d+-\d+\.\s+\S.*", normalized)
        )

    @classmethod
    def _split_pdf_paragraph(
        cls, raw_text: str, start: int, heading_path: tuple[str, ...]
    ) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """把 PDF 段落切为连续原文片段，优先保留 ``■`` 小节与次级标题边界。

        pdftotext 对部分页面不输出空行，次级标题（如「混合动力变速器」）
        会连同前置段落与表格合并成一块，嵌入语义被前置段稀释；纯词短行
        作为软边界让标题带表格独立成块（transmission-001 病历）。
        """
        boundaries = sorted(
            {match.start() for match in re.finditer(r"(?m)^■", raw_text) if match.start() > 0}
            | set(cls._pdf_subheading_offsets(raw_text))
        )
        if not boundaries:
            return cls._split_pdf_piece(raw_text, start, heading_path)
        boundaries.append(len(raw_text))
        chunks: list[tuple[str, int, int, tuple[str, ...]]] = []
        previous = 0
        for boundary in boundaries:
            chunks.extend(
                cls._split_pdf_piece(
                    raw_text[previous:boundary], start + previous, heading_path
                )
            )
            previous = boundary
        return chunks

    @classmethod
    def _pdf_subheading_offsets(cls, raw_text: str) -> list[int]:
        """识别段内次级标题短行，返回行首在段内的字符偏移。

        收窄条件以防误切：纯词行（无任何空白，排除「R 倒车」「档位 目的
        或功能」等表格数据行）、无句读标点、非纯数字页码、非 ■● 列表
        标记；前行以句末标点收尾（排除「…请在 / 驾驶车辆前」跨行断词），
        且后 2 行内紧随表格特征行（「短标签 空白 值」型）——只有带表格
        的次级标题才开新块，其余短行一律保持原块，避免碎块与词频稀释。
        """
        lines = raw_text.split("\n")
        offsets: list[int] = []
        offset = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if (
                0 < index < len(lines) - 1
                and 2 <= len(stripped) <= 12
                and not stripped.startswith(("■", "●", "○", "（"))
                and not stripped.isdigit()
                and not re.search(r"\s", stripped)
                and not re.search(r"[。！？；，、：：]", stripped)
                and re.search(r"[。！？]$", lines[index - 1].rstrip())
                and any(
                    cls._is_pdf_table_row(lines[j])
                    for j in (index + 1, index + 2)
                    if j < len(lines)
                )
            ):
                offsets.append(offset)
            offset += len(line) + 1
        return offsets

    @staticmethod
    def _is_pdf_table_row(line: str) -> bool:
        """识别「短标签 空白 值」型表格行，如「档位 目的或功能」「P 驻车 …」。"""
        stripped = line.strip()
        return bool(
            0 < len(stripped) <= 42
            and re.fullmatch(r"\S{1,12}[ \t　]+\S{1,30}", stripped)
            and not re.search(r"[。！？；，]", stripped)
        )

    @staticmethod
    def _split_pdf_piece(
        raw_text: str, start: int, heading_path: tuple[str, ...]
    ) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """仅在小节仍超长时，以中文句末标点作为不破坏语义的兜底边界。"""
        if len(raw_text) <= PDF_CHUNK_MAX_CHARS:
            return [(raw_text, start, start + len(raw_text), heading_path)]

        chunks: list[tuple[str, int, int, tuple[str, ...]]] = []
        piece_start = 0
        last_sentence_end: int | None = None
        for match in re.finditer(r"[。！？]", raw_text):
            sentence_end = match.end()
            if sentence_end - piece_start > PDF_CHUNK_MAX_CHARS:
                if last_sentence_end is None:
                    break
                chunks.append(
                    (
                        raw_text[piece_start:last_sentence_end],
                        start + piece_start,
                        start + last_sentence_end,
                        heading_path,
                    )
                )
                piece_start = last_sentence_end
            last_sentence_end = sentence_end
        if piece_start:
            chunks.append(
                (raw_text[piece_start:], start + piece_start, start + len(raw_text), heading_path)
            )
            return chunks
        return [(raw_text, start, start + len(raw_text), heading_path)]

    @staticmethod
    def _source_text(
        source_bytes: bytes, media_type: str, source_snapshot: dict[str, object] | None = None
    ) -> str:
        """把冻结来源转换为可切块文本；PDF 固定使用 raw 阅读顺序。"""
        if media_type != "application/pdf":
            return source_bytes.decode("utf-8")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source_file:
            source_file.write(source_bytes)
            source_file.flush()
            try:
                command = ["pdftotext", "-raw"]
                # 页码范围随来源快照持久化，重建必须重放完全相同的 PDF 子集。
                if source_snapshot and source_snapshot.get("first_page") is not None:
                    command.extend(["-f", str(source_snapshot["first_page"])])
                if source_snapshot and source_snapshot.get("last_page") is not None:
                    command.extend(["-l", str(source_snapshot["last_page"])])
                command.extend(["-enc", "UTF-8", source_file.name, "-"])
                result = subprocess.run(
                    command,
                    check=True, capture_output=True, text=True,
                )
            except FileNotFoundError as error:
                raise DependencyUnavailable("PDF 解析需要安装 pdftotext") from error
            except subprocess.CalledProcessError as error:
                raise InvalidArgument("PDF 文本层无法解析") from error
        return result.stdout

    @staticmethod
    def _source_locator(
        media_type: str,
        text: str,
        start: int,
        end: int,
        source_snapshot: dict[str, object] | None = None,
    ) -> dict[str, int]:
        """构造原文区间；PDF 额外记录 1 起始页码供人工回查。"""
        locator = {"char_start": start, "char_end": end}
        if media_type == "application/pdf":
            first_page = 1
            if source_snapshot and source_snapshot.get("first_page") is not None:
                first_page = int(source_snapshot["first_page"])
            locator["page"] = first_page + text.count("\f", 0, start)
        return locator

    def _process_job(self, job_id: str, lease_seconds: int) -> None:
        """先领取任务并在事务外生成向量，再用短事务写入完整构建。"""
        with self._connect() as connection, connection.transaction():
            job = connection.execute(
                """
                SELECT job.*, ns.current_index_generation_id,
                       generation.embedding_model, generation.dims
                FROM ingestion_job AS job
                JOIN namespace AS ns ON ns.id = job.namespace_id
                LEFT JOIN index_generation AS generation
                  ON generation.id = ns.current_index_generation_id
                WHERE job.id = %s
                FOR UPDATE OF job
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise NotFoundOrForbidden("任务不存在或无权访问")
            if job["status"] == "ready":
                return
            if job["status"] not in {"queued", "running"}:
                raise InvalidArgument(f"任务状态 {job['status']} 不能被处理")
            if job["status"] == "running" and job["lease_expires_at"] is not None:
                lease = job["lease_expires_at"]
                if lease > datetime.now(lease.tzinfo):
                    raise InvalidArgument("任务正在被其他 worker 处理")
            if job["current_index_generation_id"] is None:
                raise InvalidArgument("Namespace 尚未设置当前 IndexGeneration")
            if job["embedding_model"] != self._embedding_model or job["dims"] != self._embedding_dimensions:
                raise InvalidArgument("当前 IndexGeneration 与本地 embedding 模型或维度不一致")

            connection.execute(
                """
                UPDATE ingestion_job
                SET status = 'running', stage = 'chunking', attempt_count = attempt_count + 1,
                    lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE id = %s
                """,
                (lease_seconds, job_id),
            )

        text = self._source_text(
            bytes(job["source_bytes"]), job["source_media_type"], job["source_snapshot"]
        )
        chunks = self._chunks_for_media_type(text, job["source_media_type"])
        if not chunks:
            raise InvalidArgument("来源中没有可切分的文本段落")
        # 外部网络调用绝不放入数据库事务，避免长时间持锁。
        embedding_client = OpenAIEmbeddingClient(
            self._embedding_endpoint, self._embedding_model, self._embedding_dimensions
        )
        vectors = _embed_texts_in_batches(embedding_client, [chunk[0] for chunk in chunks])

        with self._connect() as connection, connection.transaction():
            current_generation = connection.execute(
                """
                SELECT current_index_generation_id FROM namespace
                WHERE id = %s
                FOR UPDATE
                """,
                (job["namespace_id"],),
            ).fetchone()
            if current_generation is None:
                raise InvalidArgument("任务所属 Namespace 不存在")
            if current_generation["current_index_generation_id"] != job["current_index_generation_id"]:
                # 切换已先提交时，丢弃旧配置生成的向量并交由 worker 按新代重试。
                # 这条复核保证旧代在激活后不会再接收迟到的 Chunk 写入。
                connection.execute(
                    """
                    UPDATE ingestion_job
                    SET status = 'queued', stage = NULL, lease_expires_at = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (job_id,),
                )
                return
            document = connection.execute(
                """
                SELECT id FROM document
                WHERE namespace_id = %s AND source_key = %s
                FOR UPDATE
                """,
                (job["namespace_id"], job["source_key"]),
            ).fetchone()
            if document is None:
                raise InvalidArgument("任务对应的 Document 不存在")
            revision = connection.execute(
                """
                SELECT id FROM document_revision
                WHERE document_id = %s AND content_hash = %s
                """,
                (document["id"], job["content_hash"]),
            ).fetchone()
            if revision is None:
                revision_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO document_revision (
                        id, document_id, content_hash, content_ref, source_snapshot
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        revision_id,
                        document["id"],
                        job["content_hash"],
                        f"ingestion-job://{job_id}",
                        Jsonb(job["source_snapshot"]),
                    ),
                )
            else:
                revision_id = revision["id"]

            generation_id = job["current_index_generation_id"]
            build = connection.execute(
                """
                SELECT status FROM revision_index_build
                WHERE revision_id = %s AND index_generation_id = %s
                FOR UPDATE
                """,
                (revision_id, generation_id),
            ).fetchone()
            if build and build["status"] == "ready":
                self._finish_job(connection, job_id, revision_id)
                return
            if build:
                # 失败重试从同一 revision 的干净构建开始，避免残留半成品。
                connection.execute(
                    "DELETE FROM chunk WHERE revision_id = %s AND index_generation_id = %s",
                    (revision_id, generation_id),
                )
                connection.execute(
                    """
                    UPDATE revision_index_build
                    SET status = 'building', expected_chunk_count = NULL,
                        completed_chunk_count = 0, error_code = NULL, completed_at = NULL
                    WHERE revision_id = %s AND index_generation_id = %s
                    """,
                    (revision_id, generation_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO revision_index_build (revision_id, index_generation_id, status)
                    VALUES (%s, %s, 'building')
                    """,
                    (revision_id, generation_id),
                )

            for ordinal, ((raw_text, start, end, heading_path), vector) in enumerate(
                zip(chunks, vectors, strict=True)
            ):
                search_text = tokenize_text(raw_text)
                connection.execute(
                    """
                    INSERT INTO chunk (
                        id, namespace_id, revision_id, index_generation_id, ordinal,
                        raw_text, search_text, heading_path, source_locator, embedding, tsv
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                              CAST(%s AS vector), to_tsvector('simple', %s))
                    """,
                    (
                        uuid4(), job["namespace_id"], revision_id, generation_id, ordinal,
                        raw_text, search_text, list(heading_path),
                        Jsonb(
                            self._source_locator(
                                job["source_media_type"], text, start, end, job["source_snapshot"]
                            )
                        ),
                        "[" + ",".join(str(value) for value in vector) + "]", search_text,
                    ),
                )
            connection.execute(
                """
                UPDATE revision_index_build
                SET status = 'ready', expected_chunk_count = %s,
                    completed_chunk_count = %s, completed_at = now()
                WHERE revision_id = %s AND index_generation_id = %s
                """,
                (len(chunks), len(chunks), revision_id, generation_id),
            )
            self._finish_job(connection, job_id, revision_id)

    @staticmethod
    def _finish_job(connection: psycopg.Connection, job_id: str, revision_id: object) -> None:
        """只在对应构建已完整 ready 后把任务标为成功。"""
        connection.execute(
            """
            UPDATE ingestion_job
            SET status = 'ready', stage = 'ready', result_revision_id = %s,
                lease_expires_at = NULL, error_code = NULL, updated_at = now()
            WHERE id = %s
            """,
            (revision_id, job_id),
        )

    def _mark_job_failed(self, job_id: str) -> None:
        """将可识别的处理异常暴露为任务失败；失败细节仍由 worker 日志保存。"""
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE ingestion_job
                SET status = 'failed', stage = 'failed', error_code = 'PROCESSING_FAILED',
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status <> 'ready'
                """,
                (job_id,),
            )

    # ------------------------------------------------------------------
    # 发布与生命周期管理（M2 实现；契约已定，先占位）
    # ------------------------------------------------------------------

    def publish(
        self,
        revision_id: str,
        namespace: str,
        valid_from: datetime,
        valid_until: datetime | None,
        expected_generation: int,
        access_scope: AccessScope,
        idempotency_key: str,
    ) -> None:
        """发布一个 ready 的修订，使其在有效时间窗内可检索。

        语义（contracts「publish、withdraw、ACL 与删除」）：
        - 仅接受 revision 状态 ready 且在当前 IndexGeneration 构建完整；
        - append/replace-tail：存在无上界 Publication 时，把旧窗口截止改到
          新窗口起点并插入新记录；冲突窗口/乱序回填/未来待发布记录抛
          PublicationConflict；
        - 短事务内检查 expected_generation（Document.state_generation，不是
          IndexGeneration）、写窗口、写审计、递增 state_generation；
          外部模型调用不进该事务；
        - 同幂等键重放返回首次结果，不因 generation 已变而误报冲突。

        Args:
            revision_id: 目标修订 ID（document_revision.id）。
            namespace: 目标 Namespace；revision 必须属于它。
            valid_from: 生效开始时间（带时区；内部统一 UTC）。
                首次发布可设过去起点；后续只能追加非重叠窗口。
            valid_until: 生效结束时间（不含）；None 表示长期有效。
            expected_generation: Document 当前 state_generation，
                用于乐观并发控制；不匹配抛 ConcurrentModification。
            access_scope: 需含 (namespace, "publish") 权限。
            idempotency_key: 幂等键，(namespace, "publish", key) 内唯一。
        """
        self._require_permission(access_scope, namespace, "publish")
        if not revision_id.strip() or not namespace.strip() or not idempotency_key.strip():
            raise InvalidArgument("revision_id、namespace 和 idempotency_key 均不能为空")
        if expected_generation < 0:
            raise InvalidArgument("expected_generation 不能为负数")
        _validate_timezone(valid_from, field_name="valid_from")
        if valid_until is not None:
            _validate_timezone(valid_until, field_name="valid_until")
            if valid_from >= valid_until:
                raise InvalidArgument("valid_from 必须早于 valid_until")

        fingerprint = sha256(
            json.dumps(
                {
                    "revision_id": revision_id,
                    "valid_from": valid_from.isoformat(),
                    "valid_until": valid_until.isoformat() if valid_until else None,
                    "expected_generation": expected_generation,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection, connection.transaction():
            namespace_row = connection.execute(
                """
                SELECT id, current_index_generation_id
                FROM namespace WHERE name = %s
                FOR UPDATE
                """,
                (namespace,),
            ).fetchone()
            if namespace_row is None:
                raise NotFoundOrForbidden("Namespace 不存在或无权访问")
            receipt = connection.execute(
                """
                SELECT request_fingerprint FROM operation_receipt
                WHERE namespace_id = %s AND operation = 'publish' AND idempotency_key = %s
                FOR UPDATE
                """,
                (namespace_row["id"], idempotency_key),
            ).fetchone()
            if receipt is not None:
                if receipt["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("同一幂等键对应了不同的发布请求")
                return

            revision = connection.execute(
                """
                SELECT revision.id, document.id AS document_id, document.state_generation
                FROM document_revision AS revision
                JOIN document ON document.id = revision.document_id
                WHERE revision.id = %s AND document.namespace_id = %s
                FOR UPDATE OF document
                """,
                (revision_id, namespace_row["id"]),
            ).fetchone()
            if revision is None:
                raise NotFoundOrForbidden("Revision 不存在或不属于该 Namespace")
            if revision["state_generation"] != expected_generation:
                raise ConcurrentModification("Document 的 state_generation 已变化")
            build = connection.execute(
                """
                SELECT status FROM revision_index_build
                WHERE revision_id = %s AND index_generation_id = %s
                """,
                (revision_id, namespace_row["current_index_generation_id"]),
            ).fetchone()
            if build is None or build["status"] != "ready":
                raise InvalidArgument("Revision 尚未在当前 IndexGeneration 完成构建")

            existing = connection.execute(
                """
                SELECT id, valid_from FROM publication
                WHERE document_id = %s AND valid_until IS NULL
                FOR UPDATE
                """,
                (revision["document_id"],),
            ).fetchone()
            if existing is not None:
                if valid_from <= existing["valid_from"]:
                    raise PublicationConflict("新发布窗口必须晚于当前无上界窗口")
                connection.execute(
                    "UPDATE publication SET valid_until = %s WHERE id = %s",
                    (valid_from, existing["id"]),
                )
            else:
                prior = connection.execute(
                    "SELECT 1 FROM publication WHERE document_id = %s LIMIT 1",
                    (revision["document_id"],),
                ).fetchone()
                if prior is not None:
                    raise PublicationConflict("已有封闭发布窗口，不能乱序回填")

            publication_id = uuid4()
            connection.execute(
                """
                INSERT INTO publication (
                    id, document_id, revision_id, valid_from, valid_until, published_by
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    publication_id,
                    revision["document_id"],
                    revision_id,
                    valid_from,
                    valid_until,
                    access_scope.principal_id,
                ),
            )
            connection.execute(
                """
                UPDATE document SET state_generation = state_generation + 1, updated_at = now()
                WHERE id = %s
                """,
                (revision["document_id"],),
            )
            connection.execute(
                """
                INSERT INTO operation_receipt (
                    id, namespace_id, operation, idempotency_key,
                    request_fingerprint, status, result_ref, completed_at
                ) VALUES (%s, %s, 'publish', %s, %s, 'succeeded', %s, now())
                """,
                (
                    uuid4(),
                    namespace_row["id"],
                    idempotency_key,
                    fingerprint,
                    Jsonb({"publication_id": str(publication_id)}),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event (
                    id, actor, object_type, object_id, before_state, after_state, trace_id
                ) VALUES (%s, %s, 'publication', %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    access_scope.principal_id,
                    str(publication_id),
                    Jsonb({"state_generation": expected_generation}),
                    Jsonb({"state_generation": expected_generation + 1}),
                    str(uuid4()),
                ),
            )

    def withdraw(
        self,
        document_id: str,
        expected_generation: int,
        access_scope: AccessScope,
        idempotency_key: str,
    ) -> None:
        """撤回文档：立即从所有业务时刻的检索中移除。

        语义（contracts「publish、withdraw、ACL 与删除」）：
        - 更新 document.withdrawn 并递增 state_generation，记录审计；
        - 不删除历史 revision / chunk；历史查询仍执行当前 ACL，
          撤回立即作用于所有 at；
        - 恢复必须是明确管理操作（再次发布或显式恢复）。
        """
        if not document_id.strip() or not idempotency_key.strip():
            raise InvalidArgument("document_id 和 idempotency_key 均不能为空")
        if expected_generation < 0:
            raise InvalidArgument("expected_generation 不能为负数")
        fingerprint = sha256(
            json.dumps(
                {"document_id": document_id, "expected_generation": expected_generation},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection, connection.transaction():
            document = connection.execute(
                """
                SELECT document.id, document.state_generation, namespace.id AS namespace_id,
                       namespace.name AS namespace
                FROM document JOIN namespace ON namespace.id = document.namespace_id
                WHERE document.id = %s
                FOR UPDATE OF document
                """,
                (document_id,),
            ).fetchone()
            if document is None:
                raise NotFoundOrForbidden("Document 不存在或无权访问")
            self._require_permission(access_scope, document["namespace"], "withdraw")
            receipt = connection.execute(
                """
                SELECT request_fingerprint FROM operation_receipt
                WHERE namespace_id = %s AND operation = 'withdraw' AND idempotency_key = %s
                FOR UPDATE
                """,
                (document["namespace_id"], idempotency_key),
            ).fetchone()
            if receipt is not None:
                if receipt["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("同一幂等键对应了不同的撤回请求")
                return
            if document["state_generation"] != expected_generation:
                raise ConcurrentModification("Document 的 state_generation 已变化")
            connection.execute(
                """
                UPDATE document
                SET withdrawn = true, state_generation = state_generation + 1, updated_at = now()
                WHERE id = %s
                """,
                (document_id,),
            )
            connection.execute(
                """
                INSERT INTO operation_receipt (
                    id, namespace_id, operation, idempotency_key,
                    request_fingerprint, status, result_ref, completed_at
                ) VALUES (%s, %s, 'withdraw', %s, %s, 'succeeded', %s, now())
                """,
                (
                    uuid4(), document["namespace_id"], idempotency_key, fingerprint,
                    Jsonb({"document_id": document_id}),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event (
                    id, actor, object_type, object_id, before_state, after_state, trace_id
                ) VALUES (%s, %s, 'document', %s, %s, %s, %s)
                """,
                (
                    uuid4(), access_scope.principal_id, document_id,
                    Jsonb({"withdrawn": False, "state_generation": expected_generation}),
                    Jsonb({"withdrawn": True, "state_generation": expected_generation + 1}),
                    str(uuid4()),
                ),
            )

    def set_access(
        self,
        document_id: str,
        acl: dict,
        expected_generation: int,
        access_scope: AccessScope,
        idempotency_key: str,
    ) -> None:
        """原子替换 Document 当前 ACL，并使下一次检索立即使用新权限。"""
        principals = acl.get("principals") if isinstance(acl, dict) else None
        if (
            not document_id.strip()
            or not idempotency_key.strip()
            or not isinstance(principals, list)
            or not principals
            or not all(isinstance(principal, str) and principal for principal in principals)
        ):
            raise InvalidArgument("acl 必须包含非空 principals 字符串列表")
        if expected_generation < 0:
            raise InvalidArgument("expected_generation 不能为负数")
        fingerprint = sha256(
            json.dumps(
                {"document_id": document_id, "acl": acl, "expected_generation": expected_generation},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection, connection.transaction():
            document = connection.execute(
                """
                SELECT document.id, document.acl, document.state_generation,
                       namespace.id AS namespace_id, namespace.name AS namespace
                FROM document JOIN namespace ON namespace.id = document.namespace_id
                WHERE document.id = %s FOR UPDATE OF document
                """,
                (document_id,),
            ).fetchone()
            if document is None:
                raise NotFoundOrForbidden("Document 不存在或无权访问")
            self._require_permission(access_scope, document["namespace"], "manage_acl")
            receipt = connection.execute(
                """
                SELECT request_fingerprint FROM operation_receipt
                WHERE namespace_id = %s AND operation = 'set_access' AND idempotency_key = %s
                FOR UPDATE
                """,
                (document["namespace_id"], idempotency_key),
            ).fetchone()
            if receipt is not None:
                if receipt["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("同一幂等键对应了不同的 ACL 请求")
                return
            if document["state_generation"] != expected_generation:
                raise ConcurrentModification("Document 的 state_generation 已变化")
            connection.execute(
                """
                UPDATE document SET acl = %s, state_generation = state_generation + 1, updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(acl), document_id),
            )
            connection.execute(
                """
                INSERT INTO operation_receipt (
                    id, namespace_id, operation, idempotency_key,
                    request_fingerprint, status, result_ref, completed_at
                ) VALUES (%s, %s, 'set_access', %s, %s, 'succeeded', %s, now())
                """,
                (uuid4(), document["namespace_id"], idempotency_key, fingerprint, Jsonb({"document_id": document_id})),
            )
            connection.execute(
                """
                INSERT INTO audit_event (
                    id, actor, object_type, object_id, before_state, after_state, trace_id
                ) VALUES (%s, %s, 'document', %s, %s, %s, %s)
                """,
                (
                    uuid4(), access_scope.principal_id, document_id,
                    Jsonb({"acl": document["acl"], "state_generation": expected_generation}),
                    Jsonb({"acl": acl, "state_generation": expected_generation + 1}), str(uuid4()),
                ),
            )

    def delete(
        self,
        document_id: str,
        access_scope: AccessScope,
        idempotency_key: str,
    ) -> None:
        """删除文档：先使内容不可检索，再后台清理。

        语义（contracts「publish、withdraw、ACL 与删除」）：
        - 先置不可检索（等同撤回语义），再后台清理 Chunk、向量、原文与缓存；
        - 共享原文引用需引用计数检查；
        - 保留不含正文的最小删除审计；物理清理期限由部署策略约定。
        """
        if not document_id.strip() or not idempotency_key.strip():
            raise InvalidArgument("document_id 和 idempotency_key 均不能为空")
        fingerprint = sha256(json.dumps({"document_id": document_id}, sort_keys=True).encode()).hexdigest()
        with self._connect() as connection, connection.transaction():
            document = connection.execute(
                """
                SELECT document.id, document.state_generation, namespace.id AS namespace_id,
                       namespace.name AS namespace
                FROM document JOIN namespace ON namespace.id = document.namespace_id
                WHERE document.id = %s FOR UPDATE OF document
                """,
                (document_id,),
            ).fetchone()
            if document is None:
                raise NotFoundOrForbidden("Document 不存在或无权访问")
            self._require_permission(access_scope, document["namespace"], "delete")
            receipt = connection.execute(
                """
                SELECT request_fingerprint FROM operation_receipt
                WHERE namespace_id = %s AND operation = 'delete' AND idempotency_key = %s
                FOR UPDATE
                """,
                (document["namespace_id"], idempotency_key),
            ).fetchone()
            if receipt is not None:
                if receipt["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("同一幂等键对应了不同的删除请求")
                return
            connection.execute(
                """
                UPDATE document
                SET withdrawn = true, deleted_at = now(), state_generation = state_generation + 1,
                    updated_at = now()
                WHERE id = %s
                """,
                (document_id,),
            )
            # 立即清理正文和索引；Document、回执与审计保留删除状态。
            connection.execute(
                """
                UPDATE ingestion_job SET source_bytes = ''::bytea, source_snapshot = '{}'::jsonb,
                    source_ref = 'deleted', result_revision_id = NULL
                WHERE namespace_id = %s AND source_key = (
                    SELECT source_key FROM document WHERE id = %s
                )
                """,
                (document["namespace_id"], document_id),
            )
            connection.execute("DELETE FROM publication WHERE document_id = %s", (document_id,))
            connection.execute("DELETE FROM chunk WHERE revision_id IN (SELECT id FROM document_revision WHERE document_id = %s)", (document_id,))
            connection.execute("DELETE FROM revision_index_build WHERE revision_id IN (SELECT id FROM document_revision WHERE document_id = %s)", (document_id,))
            connection.execute("DELETE FROM document_revision WHERE document_id = %s", (document_id,))
            connection.execute(
                """
                INSERT INTO operation_receipt (
                    id, namespace_id, operation, idempotency_key,
                    request_fingerprint, status, result_ref, completed_at
                ) VALUES (%s, %s, 'delete', %s, %s, 'succeeded', %s, now())
                """,
                (uuid4(), document["namespace_id"], idempotency_key, fingerprint, Jsonb({"document_id": document_id})),
            )
            connection.execute(
                """
                INSERT INTO audit_event (
                    id, actor, object_type, object_id, before_state, after_state, trace_id
                ) VALUES (%s, %s, 'document', %s, %s, %s, %s)
                """,
                (
                    uuid4(), access_scope.principal_id, document_id,
                    Jsonb({"deleted_at": None, "state_generation": document["state_generation"]}),
                    Jsonb({"deleted": True, "state_generation": document["state_generation"] + 1}), str(uuid4()),
                ),
            )

    def retrieve(
        self,
        query: str,
        namespace: str,
        access_scope: AccessScope,
        at: datetime | None = None,
        applicability: dict | None = None,
        top_k: int = 8,
        deadline_ms: int = 3000,
        recall_mode: str = "hybrid",
        include_context: bool = False,
    ) -> RetrievalResult:
        """受约束的混合检索，返回可引用的 Evidence 列表。

        语义（contracts「retrieve 与 Evidence」；M2 实现）：
        - 校验 AccessScope、业务条件、at、deadline，捕获本次使用的
          IndexGeneration（query 向量与文档向量必须同一模型版本）；
        - 并行双路召回（向量 + 全文），均携带 Namespace/权限/发布/时效/
          适用条件约束；RRF 融合后 rerank；
        - 每条 Evidence 携带逐字原文、身份定位三件套（document_id /
          revision_id / chunk_id）、Publication 窗口、标题路径、
          source_locator 与独立定位的 context_parts[]；
        - 返回前再次校验 ACL / 撤回 / 发布状态，失败关闭；
        - 空 Evidence 只表示本次约束及预算下未找到依据，
          非空不保证足以回答；不做意图路由与答案生成。

        Args:
            query: 自然语言查询原文；口语补全由调用方完成，
                改写仅作库内部受控扩展。
            namespace: 限定命名空间。
            access_scope: 权限过滤；返回前再次检查。
            at: 按发布记录查询的业务时刻；默认当前；不允许晚于当前时刻。
            applicability: 产品、地区、渠道等适用条件；
                文档声明而请求缺失时抛 InvalidArgument。
            top_k: 最多返回条数（建议 1–20，上限为部署配置）；不保证凑满。
            deadline_ms: 本次检索总预算（毫秒）；耗尽抛 DeadlineExceeded。
            recall_mode: 召回候选来源；默认 hybrid 融合全文与向量，
                full_text 和 vector 仅供受控诊断与评估使用。
            include_context: 是否为每条主 Evidence 附加同修订的相邻原文；
                补充原文不参与排序，且每段具有独立定位。
        """
        self._require_permission(access_scope, namespace, "read")
        if not query.strip():
            raise InvalidArgument("query 不能为空")
        if not 1 <= top_k <= 20:
            raise InvalidArgument("top_k 必须在 1 到 20 之间")
        if deadline_ms <= 0:
            raise InvalidArgument("deadline_ms 必须大于 0")
        if not isinstance(recall_mode, str) or recall_mode not in {"hybrid", "full_text", "vector"}:
            raise InvalidArgument("recall_mode 必须是 hybrid、full_text 或 vector")
        if not isinstance(include_context, bool):
            raise InvalidArgument("include_context 必须是布尔值")
        deadline = monotonic() + deadline_ms / 1000
        at = at or datetime.now(UTC)
        _validate_timezone(at, field_name="at")
        if at > datetime.now(at.tzinfo):
            raise InvalidArgument("at 不能晚于当前时刻")
        applicability = applicability or {}
        if not isinstance(applicability, dict):
            raise InvalidArgument("applicability 必须是字典")

        with self._connect() as connection:
            self._set_retrieval_statement_timeout(connection, deadline)
            active = connection.execute(
                """
                SELECT namespace.id, namespace.current_index_generation_id,
                       generation.embedding_model, generation.dims
                FROM namespace
                JOIN index_generation AS generation
                  ON generation.id = namespace.current_index_generation_id
                WHERE namespace.name = %s
                """,
                (namespace,),
            ).fetchone()
        if active is None:
            raise NotFoundOrForbidden("Namespace 不存在或无权访问")
        if not applicability:
            with self._connect() as connection:
                self._set_retrieval_statement_timeout(connection, deadline)
                requires_applicability = connection.execute(
                    """
                    SELECT 1 FROM document
                    WHERE namespace_id = %s AND applicability <> '{}'::jsonb
                    LIMIT 1
                    """,
                    (active["id"],),
                ).fetchone()
            if requires_applicability is not None:
                raise InvalidArgument("存在声明 applicability 的 Document，请求必须提供条件")
        vector_rows = []
        full_text_query = build_or_tsquery(query) if recall_mode != "vector" else None
        query_vector = None
        if recall_mode != "full_text":
            try:
                # 模型调用位于数据库连接的事务之外，避免网络延迟持有数据库锁。
                remaining_seconds = deadline - monotonic()
                if remaining_seconds <= 0:
                    raise DeadlineExceeded("检索 deadline 已耗尽")
                query_vector = OpenAIEmbeddingClient(
                    self._embedding_endpoint, active["embedding_model"], active["dims"]
                ).embed([query], timeout_seconds=remaining_seconds)[0]
            except (DependencyUnavailable, InvalidArgument):
                query_vector = None
        if monotonic() >= deadline:
            raise DeadlineExceeded("检索 deadline 已耗尽")

        with self._connect() as connection:
            self._set_retrieval_statement_timeout(connection, deadline)
            namespace_row = connection.execute(
                """SELECT id, current_index_generation_id FROM namespace WHERE name = %s""",
                (namespace,),
            ).fetchone()
            if namespace_row is None or namespace_row["current_index_generation_id"] is None:
                raise NotFoundOrForbidden("Namespace 不存在或无权访问")
            generation = connection.execute(
                """SELECT embedding_model FROM index_generation WHERE id = %s""",
                (namespace_row["current_index_generation_id"],),
            ).fetchone()
            rows = []
            if recall_mode != "vector" and full_text_query is not None:
                rows = connection.execute(
                    """
                SELECT chunk.id AS chunk_id, chunk.ordinal AS chunk_ordinal, chunk.raw_text, chunk.source_locator,
                       chunk.heading_path, revision.id AS revision_id, document.id AS document_id,
                       publication.valid_from, publication.valid_until
                FROM chunk
                JOIN revision_index_build AS build
                  ON build.revision_id = chunk.revision_id
                 AND build.index_generation_id = chunk.index_generation_id
                JOIN document_revision AS revision ON revision.id = chunk.revision_id
                JOIN document ON document.id = revision.document_id
                JOIN publication ON publication.revision_id = revision.id
                                 AND publication.document_id = document.id
                WHERE chunk.namespace_id = %s
                  AND chunk.index_generation_id = %s
                  AND build.status = 'ready'
                  AND document.withdrawn = false AND document.deleted_at IS NULL
                  AND document.acl @> jsonb_build_object('principals', jsonb_build_array(%s::text))
                  AND document.applicability <@ %s::jsonb
                  AND publication.valid_from <= %s
                  AND (publication.valid_until IS NULL OR %s < publication.valid_until)
                  AND chunk.tsv @@ to_tsquery('simple', %s)
                ORDER BY ts_rank_cd(chunk.tsv, to_tsquery('simple', %s)) DESC, chunk.ordinal
                LIMIT %s
                    """,
                    (
                        namespace_row["id"], namespace_row["current_index_generation_id"],
                        access_scope.principal_id, Jsonb(applicability), at, at,
                        full_text_query, full_text_query, RETRIEVAL_CANDIDATE_LIMIT,
                    ),
                ).fetchall()
            if recall_mode != "full_text" and query_vector is not None:
                vector_literal = "[" + ",".join(str(value) for value in query_vector) + "]"
                vector_rows = connection.execute(
                    """
                    SELECT chunk.id AS chunk_id, chunk.ordinal AS chunk_ordinal, chunk.raw_text, chunk.source_locator,
                           chunk.heading_path, revision.id AS revision_id, document.id AS document_id,
                           publication.valid_from, publication.valid_until
                    FROM chunk
                    JOIN revision_index_build AS build
                      ON build.revision_id = chunk.revision_id
                     AND build.index_generation_id = chunk.index_generation_id
                    JOIN document_revision AS revision ON revision.id = chunk.revision_id
                    JOIN document ON document.id = revision.document_id
                    JOIN publication ON publication.revision_id = revision.id
                                     AND publication.document_id = document.id
                    WHERE chunk.namespace_id = %s
                      AND chunk.index_generation_id = %s
                      AND build.status = 'ready'
                      AND document.withdrawn = false AND document.deleted_at IS NULL
                      AND document.acl @> jsonb_build_object('principals', jsonb_build_array(%s::text))
                      AND document.applicability <@ %s::jsonb
                      AND publication.valid_from <= %s
                      AND (publication.valid_until IS NULL OR %s < publication.valid_until)
                    ORDER BY chunk.embedding <=> CAST(%s AS vector), chunk.ordinal
                    LIMIT %s
                    """,
                    (
                        namespace_row["id"], namespace_row["current_index_generation_id"],
                        access_scope.principal_id, Jsonb(applicability), at, at,
                        vector_literal, RETRIEVAL_CANDIDATE_LIMIT,
                    ),
                ).fetchall()
        if monotonic() >= deadline:
            raise DeadlineExceeded("检索 deadline 已耗尽")
        ranked_rows = _fuse_rrf_candidates(rows, vector_rows, top_k=top_k)
        if ranked_rows:
            with self._connect() as connection:
                self._set_retrieval_statement_timeout(connection, deadline)
                valid_rows = connection.execute(
                    """
                    SELECT chunk.id
                    FROM chunk
                    JOIN namespace ON namespace.id = chunk.namespace_id
                    JOIN revision_index_build AS build
                      ON build.revision_id = chunk.revision_id
                     AND build.index_generation_id = chunk.index_generation_id
                    JOIN document_revision AS revision ON revision.id = chunk.revision_id
                    JOIN document ON document.id = revision.document_id
                    JOIN publication ON publication.revision_id = revision.id
                                     AND publication.document_id = document.id
                    WHERE namespace.name = %s
                      AND chunk.index_generation_id = namespace.current_index_generation_id
                      AND build.status = 'ready'
                      AND document.withdrawn = false AND document.deleted_at IS NULL
                      AND document.acl @> jsonb_build_object('principals', jsonb_build_array(%s::text))
                      AND document.applicability <@ %s::jsonb
                      AND publication.valid_from <= %s
                      AND (publication.valid_until IS NULL OR %s < publication.valid_until)
                      AND chunk.id = ANY(%s)
                    """,
                    (
                        namespace, access_scope.principal_id, Jsonb(applicability), at, at,
                        [row["chunk_id"] for row, _ in ranked_rows],
                    ),
                ).fetchall()
            valid_chunk_ids = {row["id"] for row in valid_rows}
            ranked_rows = [
                item for item in ranked_rows if item[0]["chunk_id"] in valid_chunk_ids
            ]
        context_parts_by_seed: dict[object, tuple[ContextPart, ...]] = {}
        if include_context and ranked_rows:
            with self._connect() as connection:
                self._set_retrieval_statement_timeout(connection, deadline)
                context_rows = connection.execute(
                    """
                    SELECT seed.id AS seed_chunk_id, neighbor.id AS chunk_id,
                           neighbor.ordinal AS chunk_ordinal, neighbor.raw_text,
                           neighbor.source_locator, neighbor.heading_path
                    FROM chunk AS seed
                    JOIN chunk AS neighbor
                      ON neighbor.revision_id = seed.revision_id
                     AND neighbor.index_generation_id = seed.index_generation_id
                     AND neighbor.ordinal BETWEEN seed.ordinal - %s AND seed.ordinal + %s
                     AND neighbor.id <> seed.id
                    JOIN revision_index_build AS build
                      ON build.revision_id = neighbor.revision_id
                     AND build.index_generation_id = neighbor.index_generation_id
                    JOIN document_revision AS revision ON revision.id = neighbor.revision_id
                    JOIN document ON document.id = revision.document_id
                    JOIN publication ON publication.revision_id = revision.id
                                     AND publication.document_id = document.id
                    WHERE seed.id = ANY(%s)
                      AND neighbor.namespace_id = %s
                      AND neighbor.index_generation_id = %s
                      AND build.status = 'ready'
                      AND document.withdrawn = false AND document.deleted_at IS NULL
                      AND document.acl @> jsonb_build_object('principals', jsonb_build_array(%s::text))
                      AND document.applicability <@ %s::jsonb
                      AND publication.valid_from <= %s
                      AND (publication.valid_until IS NULL OR %s < publication.valid_until)
                    ORDER BY seed.id, abs(neighbor.ordinal - seed.ordinal), neighbor.ordinal
                    """,
                    (
                        CONTEXT_PART_NEIGHBOR_DISTANCE,
                        CONTEXT_PART_NEIGHBOR_DISTANCE,
                        [row["chunk_id"] for row, _ in ranked_rows],
                        namespace_row["id"], namespace_row["current_index_generation_id"],
                        access_scope.principal_id, Jsonb(applicability), at, at,
                    ),
                ).fetchall()
            primary_chunk_ids = {row["chunk_id"] for row, _ in ranked_rows}
            mutable_parts: dict[object, list[ContextPart]] = {}
            for context_row in context_rows:
                if context_row["chunk_id"] in primary_chunk_ids:
                    continue
                parts = mutable_parts.setdefault(context_row["seed_chunk_id"], [])
                if len(parts) < CONTEXT_PART_LIMIT:
                    parts.append(
                        ContextPart(
                            text=context_row["raw_text"],
                            chunk_id=str(context_row["chunk_id"]),
                            source_locator=context_row["source_locator"],
                            heading_path=tuple(context_row["heading_path"]),
                        )
                    )
            context_parts_by_seed = {
                chunk_id: tuple(parts) for chunk_id, parts in mutable_parts.items()
            }
        if monotonic() >= deadline:
            raise DeadlineExceeded("检索 deadline 已耗尽")
        score_type = "rrf" if recall_mode == "hybrid" else recall_mode
        vector_unavailable = recall_mode != "full_text" and query_vector is None
        return RetrievalResult(
            evidence=tuple(
                Evidence(
                    text=row["raw_text"], document_id=str(row["document_id"]),
                    revision_id=str(row["revision_id"]), chunk_id=str(row["chunk_id"]),
                    source_locator=row["source_locator"], heading_path=tuple(row["heading_path"]),
                    publication_valid_from=row["valid_from"], publication_valid_until=row["valid_until"],
                    rank_score=score, score_type=score_type,
                    context_parts=context_parts_by_seed.get(row["chunk_id"], ()),
                ) for row, score in ranked_rows
            ),
            index_generation=str(namespace_row["current_index_generation_id"]),
            model_version=generation["embedding_model"], trace_id=str(uuid4()),
            degraded=vector_unavailable,
            warnings=("vector_retrieval_unavailable",) if vector_unavailable else (),
        )


def _validate_timezone(dt: datetime, *, field_name: str) -> None:
    """校验时间参数带时区（contracts「共同约束」：拒绝 naive datetime）。

    Args:
        dt: 待校验时间。
        field_name: 参数名，用于错误消息定位。

    Raises:
        InvalidArgument: dt 为 naive（tzinfo 为空）。
    """
    if dt.tzinfo is None:
        raise InvalidArgument(
            f"{field_name} 必须携带时区信息，拒绝 naive datetime",
            details={"field": field_name},
        )
