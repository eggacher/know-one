"""KnowOne 错误类型。

错误语义的权威定义见 docs/contracts.md「错误与资源生命周期」一节。
所有异常继承 KnowOneError，携带机器可读的 ``code`` 与可选的 ``trace_id`` /
``details``；调用方应按 ``code`` 分支处理，不要对异常消息做字符串匹配。

错误分为三类语义：

- 参数/输入问题（InvalidArgument、UnsupportedSource）：修正输入，不自动重试；
- 状态冲突（IdempotencyConflict、PublicationConflict、ConcurrentModification）：
  先读取当前状态，由业务明确决策，不能盲目覆盖；
- 依赖/预算问题（DependencyUnavailable、RateLimited、DeadlineExceeded）：
  在总预算内有限重试，尊重 retry-after。
"""

from __future__ import annotations


class KnowOneError(Exception):
    """所有 KnowOne 异常的基类。

    Attributes:
        code: 机器可读错误码，子类各自固定；调用方据此分支。
        trace_id: 本次操作的链路追踪 ID；未提供时为 None。
        details: 结构化补充信息（如冲突双方的指纹、当前 generation 值），
            只放机器可读数据，不放正文。
    """

    code = "KNOW_ONE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        trace_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.trace_id = trace_id
        self.details = details or {}


class InvalidArgument(KnowOneError):
    """参数非法。

    触发场景（contracts「共同约束」）：
    - 缺少必填字段、字段超长/超量；
    - naive datetime（所有时间必须带时区，内部统一 UTC）；
    - Validity Window 下界不小于上界；
    - retrieve 的 top_k 超出部署配置上限；
    - 文档声明了 applicability 条件而请求未提供必要条件。
    """

    code = "INVALID_ARGUMENT"


class UnsupportedSource(KnowOneError):
    """来源格式不受支持。

    触发场景（pipeline.md「解析与清洗」）：解析器不支持的输入
    （如首版不支持纯扫描件 PDF、无文本层的 OCR 输入）。
    库必须明确报错，不静默发布残缺内容。
    调用方处理：换用受支持的来源格式，不自动重试。
    """

    code = "UNSUPPORTED_SOURCE"


class NotFoundOrForbidden(KnowOneError):
    """文档不存在，或调用方无权知晓其存在。

    触发场景（contracts「共同约束」）：未知或无权访问的文档统一报此错误，
    不区分 404/403，避免向无权调用方暴露文档存在性。
    调用方处理：核验身份与授权配置，不能降级为匿名访问重试。
    """

    code = "NOT_FOUND_OR_FORBIDDEN"


class AccessDenied(KnowOneError):
    """AccessScope 不含本操作所需权限。

    触发场景：read、ingest、publish、withdraw、manage_acl、delete
    分别授权（contracts「共同约束」）；scope 缺失、过期或无法核验时
    失败关闭（fail-closed），绝不降级放行。
    """

    code = "ACCESS_DENIED"


class IdempotencyConflict(KnowOneError):
    """同一幂等键提交了不同内容的请求。

    触发场景（contracts「ingest 与任务状态」）：``operation_receipt`` 中
    已存在 (namespace, operation, idempotency_key) 记录，但
    ``request_fingerprint``（来源版本/内容摘要 + source_key + 构建参数）
    与本次不一致。
    调用方处理：确认业务意图后换用新幂等键，不得盲目重试。
    """

    code = "IDEMPOTENCY_CONFLICT"


class PublicationConflict(KnowOneError):
    """发布窗口与既有 Publication 冲突。

    触发场景（contracts「publish、withdraw、ACL 与删除」）：
    - 新窗口与同一 Document 的既有有效区间重叠；
    - 乱序回填已封闭的历史窗口；
    - 已存在未来待发布记录时再追加新窗口。
    首版发布采用 append/replace-tail 语义：只允许追加非重叠窗口，
    或截短最后一个无上界窗口；其余情况一律报此错误，由运营明确处理。
    """

    code = "PUBLICATION_CONFLICT"


class ConcurrentModification(KnowOneError):
    """expected_generation 与 Document 当前 state_generation 不一致。

    触发场景：publish / withdraw / set_access / delete 携带的
    ``expected_generation``（Document 可检索状态的乐观并发控制版本，
    不是 IndexGeneration）已过期，说明并发修改了发布、撤回或 ACL。
    调用方处理：读取当前状态后重新决策；同幂等键重放不会误报此错误
    （首次成功结果已由 operation_receipt 保存）。
    """

    code = "CONCURRENT_MODIFICATION"


class DependencyUnavailable(KnowOneError):
    """数据库或模型端点不可用。

    触发场景：两路检索都因底层数据库不可用而失败；入库时数据库连接失败；
    模型端点持续不可达。此类故障不允许被包装成「空检索结果」。
    调用方处理：在总预算内有限重试；入库任务由 worker 按租约机制重试。
    """

    code = "DEPENDENCY_UNAVAILABLE"


class RateLimited(KnowOneError):
    """请求被限流。

    触发场景：模型端点或数据库连接池达到上限。
    调用方处理：读取 details 中的 retry_after 秒数，退避后重试；
    在线请求不应因此拖垮事件循环（异步宿主用非阻塞适配）。
    """

    code = "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after  # 建议等待秒数；None 表示未给出


class DeadlineExceeded(KnowOneError):
    """本次操作的时限预算耗尽。

    触发场景：retrieve 的 deadline_ms 用尽且无合法部分结果。
    注意语义（contracts「错误与资源生命周期」）：DeadlineExceeded 只表示
    本次未完成，不能解释为「库中没有相关知识」。
    """

    code = "DEADLINE_EXCEEDED"


class IngestionFailed(KnowOneError):
    """入库任务失败。

    触发场景：ingestion_job.status = failed；error_code 记录具体原因
    （如解析失败、embedding 维度不符、完整性校验未通过）。
    已发布旧版本继续可用——失败只影响正在构建的 revision。
    调用方处理：按 get_ingestion 返回的 error_code 逐项修复后重新提交。
    """

    code = "INGESTION_FAILED"


class NeedsReview(KnowOneError):
    """内容需要人工确认，不能自动通过。

    触发场景（chunking.md「默认算法」第 6 条）：解析质量不达标、
    切块无法保持规则与例外的关系、表格行无法与表头关联等——
    库标记 ingestion_job.status = needs_review，不静默截掉事实。
    调用方处理：人工预览 revision 草稿，修复来源或明确确认后重试。
    """

    code = "NEEDS_REVIEW"
