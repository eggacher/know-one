"""KnowOne 业务门面。

接口契约的权威定义见 docs/contracts.md；数据生命周期见
docs/architecture.md「数据生命周期与变更流程」。

首版（M1）实现 ingest / get_ingestion 及配套的 worker 执行入口 process_job；
publish / withdraw / set_access / delete / retrieve 为已定义契约的占位方法，
调用会抛出 NotImplementedError，方法注释仍完整描述目标语义，作为后续实现依据。

用法示意（README 中的调用形态）::

    kb = KnowOne(dsn="postgresql://knowone:knowone@localhost:5432/knowone")
    ref = kb.ingest(source=FileSource(path), namespace="rav4",
                    source_key="rav4-hybrid-user-manual",
                    access_scope=scope, idempotency_key="ing-20260924-001")
    kb.process_job(ref.job_id)          # worker 执行：解析→清洗→切块→向量化→校验
    st = kb.get_ingestion(ref.job_id, scope)
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import os
import re
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from know_one.config import load_local_env
from know_one.embedding import OpenAIEmbeddingClient
from know_one.errors import (
    AccessDenied,
    ConcurrentModification,
    IdempotencyConflict,
    InvalidArgument,
    NotFoundOrForbidden,
    PublicationConflict,
    UnsupportedSource,
)
from know_one.model import (
    AccessScope,
    IngestionJobRef,
    IngestionStatus,
    Source,
)


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
                如 LM Studio ``http://192.168.2.6:1234/v1``）；
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
        try:
            source_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidArgument("text/plain 来源必须是 UTF-8 编码") from error
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
            "chunker": "m1-paragraph-v1",
            "embedding_model": self._embedding_model,
            "dimensions": self._embedding_dimensions,
            "full_text": "postgres-simple-v1",
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
                SELECT revision.id, source.source_bytes
                FROM document_revision AS revision
                JOIN document ON document.id = revision.document_id
                JOIN LATERAL (
                    SELECT source_bytes
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
            text = bytes(revision["source_bytes"]).decode("utf-8")
            paragraphs = self._paragraphs(text)
            if not paragraphs:
                raise InvalidArgument("Revision 原文中没有可切分的文本段落")
            # 外部网络调用绝不放入数据库事务，避免长时间持锁。
            vectors = OpenAIEmbeddingClient(
                self._embedding_endpoint, self._embedding_model, self._embedding_dimensions
            ).embed([raw_text for raw_text, _, _ in paragraphs])
            self._write_rebuilt_revision(
                generation["namespace_id"], generation_id, revision["id"], paragraphs, vectors
            )

    def _write_rebuilt_revision(
        self,
        namespace_id: object,
        generation_id: str,
        revision_id: object,
        paragraphs: list[tuple[str, int, int]],
        vectors: list[list[float]],
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
            for ordinal, ((raw_text, start, end), vector) in enumerate(
                zip(paragraphs, vectors, strict=True)
            ):
                connection.execute(
                    """
                    INSERT INTO chunk (
                        id, namespace_id, revision_id, index_generation_id, ordinal,
                        raw_text, search_text, source_locator, embedding, tsv
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                              CAST(%s AS vector), to_tsvector('simple', %s))
                    """,
                    (
                        uuid4(), namespace_id, revision_id, generation_id, ordinal,
                        raw_text, raw_text,
                        Jsonb({"char_start": start, "char_end": end}),
                        "[" + ",".join(str(value) for value in vector) + "]", raw_text,
                    ),
                )
            connection.execute(
                """
                UPDATE revision_index_build
                SET status = 'ready', expected_chunk_count = %s,
                    completed_chunk_count = %s, completed_at = now()
                WHERE revision_id = %s AND index_generation_id = %s
                """,
                (len(paragraphs), len(paragraphs), revision_id, generation_id),
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

    def _connect(self) -> psycopg.Connection:
        """创建一个短生命周期连接；M1 避免过早引入连接池配置。"""
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @staticmethod
    def _require_permission(scope: AccessScope, namespace: str, permission: str) -> None:
        """在任何数据库写入前失败关闭地检查调用方授权。"""
        if not scope.allows(namespace, permission):
            raise AccessDenied(f"无权在 Namespace {namespace!r} 执行 {permission}")

    @staticmethod
    def _validate_ingest_arguments(
        source: Source, namespace: str, source_key: str, idempotency_key: str
    ) -> None:
        """M1 的输入边界：只处理调用方已准备好的 UTF-8 纯文本。"""
        if not namespace.strip() or not source_key.strip() or not idempotency_key.strip():
            raise InvalidArgument("namespace、source_key 和 idempotency_key 均不能为空")
        if len(source_key) > 512:
            raise InvalidArgument("source_key 不能超过 512 个字符")
        if getattr(source, "media_type", None) != "text/plain":
            raise UnsupportedSource("M1 仅支持 text/plain 来源")

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
    def _paragraphs(text: str) -> list[tuple[str, int, int]]:
        """按空行切分连续原文段落，保留字符区间供证据定位。

        这是有意保守的 M1 兜底切块；标题树、FAQ、表格及句子依赖关系由
        后续解析器提供后再接入，不在这里假装理解语义。
        """
        chunks: list[tuple[str, int, int]] = []
        for match in re.finditer(r"\S(?:.*?\S)?(?=\s*\n\s*\n|\s*\Z)", text, re.DOTALL):
            raw_text = match.group(0)
            chunks.append((raw_text, match.start(), match.end()))
        return chunks

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

        text = bytes(job["source_bytes"]).decode("utf-8")
        paragraphs = self._paragraphs(text)
        if not paragraphs:
            raise InvalidArgument("来源中没有可切分的文本段落")
        # 外部网络调用绝不放入数据库事务，避免长时间持锁。
        vectors = OpenAIEmbeddingClient(
            self._embedding_endpoint, self._embedding_model, self._embedding_dimensions
        ).embed([raw_text for raw_text, _, _ in paragraphs])

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

            for ordinal, ((raw_text, start, end), vector) in enumerate(zip(paragraphs, vectors, strict=True)):
                connection.execute(
                    """
                    INSERT INTO chunk (
                        id, namespace_id, revision_id, index_generation_id, ordinal,
                        raw_text, search_text, source_locator, embedding, tsv
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                              CAST(%s AS vector), to_tsvector('simple', %s))
                    """,
                    (
                        uuid4(), job["namespace_id"], revision_id, generation_id, ordinal,
                        raw_text, raw_text,
                        Jsonb({"char_start": start, "char_end": end}),
                        "[" + ",".join(str(value) for value in vector) + "]", raw_text,
                    ),
                )
            connection.execute(
                """
                UPDATE revision_index_build
                SET status = 'ready', expected_chunk_count = %s,
                    completed_chunk_count = %s, completed_at = now()
                WHERE revision_id = %s AND index_generation_id = %s
                """,
                (len(paragraphs), len(paragraphs), revision_id, generation_id),
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
        raise NotImplementedError

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
        raise NotImplementedError

    def retrieve(
        self,
        query: str,
        namespace: str,
        access_scope: AccessScope,
        at: datetime | None = None,
        applicability: dict | None = None,
        top_k: int = 8,
        deadline_ms: int = 3000,
    ):
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
        """
        raise NotImplementedError


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
