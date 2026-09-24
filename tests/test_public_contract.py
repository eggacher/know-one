from know_one import AccessScope, InvalidArgument, KnowOne
from know_one.core.api import _validate_timezone


def test_public_entry_is_importable() -> None:
    """调用方只需从根包导入门面和领域类型。"""
    assert KnowOne is not None


def test_scope_checks_namespace_and_operation_together() -> None:
    scope = AccessScope(
        principal_id="editor",
        namespaces=frozenset({"game-a"}),
        permissions=frozenset({"ingest"}),
    )

    assert scope.allows("game-a", "ingest")
    assert not scope.allows("game-a", "publish")
    assert not scope.allows("game-b", "ingest")

def test_naive_time_is_rejected() -> None:
    from datetime import datetime

    import pytest

    with pytest.raises(InvalidArgument, match="时区"):
        _validate_timezone(datetime(2026, 1, 1), field_name="valid_from")
