"""本地 .env 配置的最小行为测试。"""

from __future__ import annotations

import os

from know_one.config import load_local_env


def test_local_env_fills_missing_values_without_overwriting_process_env(tmp_path, monkeypatch) -> None:
    """本地文件只补默认值，部署平台注入的值始终优先。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KNOWONE_DSN=postgresql://from-file\n"
        "KNOWONE_EMBEDDING_ENDPOINT='http://from-file/v1'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWONE_DSN", "postgresql://from-process")
    monkeypatch.delenv("KNOWONE_EMBEDDING_ENDPOINT", raising=False)

    load_local_env(env_file)

    assert os.environ["KNOWONE_DSN"] == "postgresql://from-process"
    assert os.environ["KNOWONE_EMBEDDING_ENDPOINT"] == "http://from-file/v1"
