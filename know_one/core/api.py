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

from know_one.errors import (
    AccessDenied,
    IdempotencyConflict,
    InvalidArgument,
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

    def __init__(self, dsn: str, *, embedding_endpoint: str | None = None) -> None:
        """创建门面实例。

        Args:
            dsn: PostgreSQL 连接串（ADR-0004），如
                ``postgresql://knowone:knowone@localhost:5432/knowone``。
            embedding_endpoint: embedding 模型端点（OpenAI 兼容接口，
                如 LM Studio ``http://192.168.2.6:1234/v1``）；
                None 时从环境变量 KNOWONE_EMBEDDING_ENDPOINT 读取。
                外部模型调用一律不进入数据库事务（pipeline.md「切块、向量化与写入」）。
        """

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
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError

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
