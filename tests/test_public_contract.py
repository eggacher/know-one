from datetime import datetime, timezone
from uuid import uuid4

import pytest

from know_one import AccessScope, Evidence, EvidencePart, JobState, KnowledgeBase, SourceLocator


def test_public_contract_is_importable() -> None:
    assert KnowledgeBase is not None
    assert JobState.READY.value == "ready"


def test_scope_rejects_naive_expiration() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AccessScope("editor", frozenset({"game-a"}), frozenset({"ingest"}), expires_at=datetime(2026, 1, 1))


def test_evidence_keeps_supplemental_source_separate() -> None:
    primary = SourceLocator("immutable:faq-v1", 0, 18)
    exception = SourceLocator("immutable:faq-v1", 100, 113)
    evidence = Evidence(
        text="普通道具购买后 7 日内可申请退款。",
        document_id=uuid4(),
        revision_id=uuid4(),
        chunk_id=uuid4(),
        source_locator=primary,
        context_parts=(EvidencePart("但已使用道具不支持退款。", exception),),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert evidence.source_locator != evidence.context_parts[0].source_locator


def test_locator_rejects_empty_interval() -> None:
    with pytest.raises(ValueError, match="start < end"):
        SourceLocator("immutable:faq-v1", 5, 5)
