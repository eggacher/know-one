"""公开错误类别；调用方据此区分参数错误、并发冲突和依赖故障。"""


class KnowOneError(Exception):
    """所有公开接口错误的基类。"""


class InvalidArgument(KnowOneError):
    pass


class UnsupportedSource(KnowOneError):
    pass


class NotFoundOrForbidden(KnowOneError):
    pass


class AccessDenied(KnowOneError):
    pass


class IdempotencyConflict(KnowOneError):
    pass


class PublicationConflict(KnowOneError):
    pass


class ConcurrentModification(KnowOneError):
    pass


class DependencyUnavailable(KnowOneError):
    pass


class RateLimited(KnowOneError):
    pass


class DeadlineExceeded(KnowOneError):
    pass


class IngestionFailed(KnowOneError):
    pass


class NeedsReview(KnowOneError):
    pass
