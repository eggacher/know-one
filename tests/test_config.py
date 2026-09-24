"""本地 .env 配置的最小行为测试。"""

from __future__ import annotations

import os

from know_one.config import load_local_env
from know_one.core.api import KnowOne


def test_local_env_fills_missing_values_without_overwriting_process_env(tmp_path, monkeypatch) -> None:
    """本地文件只补默认值，部署平台注入的值始终优先。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KNOWONE_DSN=postgresql://from-file\n"
        "KNOWONE_EMBEDDING_ENDPOINT='http://from-file/v1'\n"
        "KNOWONE_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B\n"
        "KNOWONE_EMBEDDING_DIMENSIONS=1024\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWONE_DSN", "postgresql://from-process")
    monkeypatch.delenv("KNOWONE_EMBEDDING_ENDPOINT", raising=False)

    load_local_env(env_file)

    assert os.environ["KNOWONE_DSN"] == "postgresql://from-process"
    assert os.environ["KNOWONE_EMBEDDING_ENDPOINT"] == "http://from-file/v1"
    assert os.environ["KNOWONE_EMBEDDING_MODEL"] == "Qwen/Qwen3-Embedding-0.6B"
    assert os.environ["KNOWONE_EMBEDDING_DIMENSIONS"] == "1024"


def test_know_one_reads_embedding_configuration_from_environment(tmp_path, monkeypatch) -> None:
    """构造门面时，模型标识与向量维度应来自统一环境配置。"""
    (tmp_path / ".env").write_text(
        "KNOWONE_DSN=postgresql://from-file\n"
        "KNOWONE_EMBEDDING_ENDPOINT=http://embedding/v1\n"
        "KNOWONE_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B\n"
        "KNOWONE_EMBEDDING_DIMENSIONS=1024\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in (
        "KNOWONE_DSN",
        "KNOWONE_EMBEDDING_ENDPOINT",
        "KNOWONE_EMBEDDING_MODEL",
        "KNOWONE_EMBEDDING_DIMENSIONS",
    ):
        monkeypatch.delenv(key, raising=False)

    kb = KnowOne()

    assert kb._embedding_endpoint == "http://embedding/v1"
    assert kb._embedding_model == "Qwen/Qwen3-Embedding-0.6B"
    assert kb._embedding_dimensions == 1024
