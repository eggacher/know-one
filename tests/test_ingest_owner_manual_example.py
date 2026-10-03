"""整本 RAV4 用户手册导入示例的固定来源映射测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_owner_manual_example_keeps_gasoline_and_hybrid_sources_separate() -> None:
    """车型差异由独立 Namespace 与稳定 source_key 隔离，不能混用同一身份。"""
    path = Path(__file__).parents[1] / "examples" / "rav4" / "scripts" / "ingest_owner_manual.py"
    spec = importlib.util.spec_from_file_location("ingest_owner_manual", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MANUALS["gasoline"][1] != module.MANUALS["hybrid"][1]
    assert "汽油" in module.MANUALS["gasoline"][0].name
    assert "HEV" in module.MANUALS["hybrid"][0].name
