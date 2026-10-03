"""检查 KnowOne 调用方部署所需的本地依赖是否可用。"""

from __future__ import annotations

import argparse
import json
import os
from shutil import which
from typing import Callable, Sequence
from urllib.request import Request, urlopen

from know_one.config import load_local_env
from know_one.embedding import OpenAIEmbeddingClient


DEFAULT_LM_STUDIO_BASE_URL = "http://192.168.2.6:1234/api/v1"
DEFAULT_LM_STUDIO_MODEL = "qwen3.5-9b"


def _parser() -> argparse.ArgumentParser:
    """构造只读 preflight 参数，不执行入库、发布或模型生成。"""
    load_local_env()
    parser = argparse.ArgumentParser(description="检查 KnowOne 本地部署依赖")
    parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
    parser.add_argument("--embedding-endpoint", default=os.environ.get("KNOWONE_EMBEDDING_ENDPOINT"))
    parser.add_argument("--embedding-model", default=os.environ.get("KNOWONE_EMBEDDING_MODEL"))
    parser.add_argument("--embedding-dimensions", type=int, default=os.environ.get("KNOWONE_EMBEDDING_DIMENSIONS", "1024"))
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("KNOWONE_ANSWER_BASE_URL", DEFAULT_LM_STUDIO_BASE_URL),
    )
    parser.add_argument("--llm-model", default=os.environ.get("KNOWONE_ANSWER_MODEL", DEFAULT_LM_STUDIO_MODEL))
    parser.add_argument("--timeout-seconds", type=float, default=10)
    return parser


def _lm_studio_model_state(body: object, model: str) -> tuple[bool, bool]:
    """返回目标模型是否存在及是否已有加载实例。"""
    if not isinstance(body, dict) or not isinstance(models := body.get("models"), list):
        return False, False
    for item in models:
        if isinstance(item, dict) and item.get("key") == model:
            instances = item.get("loaded_instances")
            return True, isinstance(instances, list) and bool(instances)
    return False, False


def _postgres(dsn: str) -> dict[str, object]:
    """确认数据库、pgvector 扩展和核心表均已就绪。"""
    if not dsn:
        raise ValueError("未设置 KNOWONE_DSN")
    import psycopg

    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS has_vector,
                   to_regclass('public.namespace') IS NOT NULL AS has_schema
            """
        ).fetchone()
    if row is None or not row[0] or not row[1]:
        raise RuntimeError("pgvector 扩展或 KnowOne schema 缺失")
    return {"pgvector": True, "schema": True}


def _embedding(endpoint: str | None, model: str | None, dimensions: int, timeout_seconds: float) -> dict[str, object]:
    """用一个无业务含义的短文本验证 embedding 路径和向量维度。"""
    vector = OpenAIEmbeddingClient(endpoint, model, dimensions).embed(
        ["KnowOne preflight"], timeout_seconds=timeout_seconds
    )[0]
    return {"model": model, "dimensions": len(vector)}


def _pdftotext() -> dict[str, object]:
    """确认 PDF 文本层解析器位于当前 PATH，供后续 worker 调用。"""
    if not (path := which("pdftotext")):
        raise RuntimeError("未安装 pdftotext")
    return {"path": path}


def _llm(base_url: str, model: str, timeout_seconds: float) -> dict[str, object]:
    """读取模型目录，不触发耗时生成或自动装载。"""
    headers = {}
    if token := os.environ.get("KNOWONE_ANSWER_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{base_url.rstrip('/')}/models", headers=headers)
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 -- endpoint 由部署方配置
        exists, loaded = _lm_studio_model_state(json.loads(response.read()), model)
    if not exists:
        raise RuntimeError("目标 LLM 模型不在 LM Studio 模型目录中")
    return {"model": model, "loaded": loaded}


def _run_check(name: str, check: Callable[[], dict[str, object]]) -> dict[str, object]:
    """将依赖异常归一为安全的机器可读状态，不输出 DSN 或鉴权令牌。"""
    try:
        return {"component": name, "status": "ok", "details": check()}
    except Exception as error:  # 部署前检查必须收集其余依赖，不能在首项失败时中断。
        return {"component": name, "status": "failed", "error_type": type(error).__name__}


def main(argv: Sequence[str] | None = None) -> int:
    """依次检查全部依赖，输出单个 JSON 报告并以状态决定退出码。"""
    arguments = _parser().parse_args(argv)
    if arguments.timeout_seconds <= 0:
        raise ValueError("timeout-seconds 必须大于 0")
    checks = (
        _run_check("postgres", lambda: _postgres(arguments.dsn or "")),
        _run_check("pdftotext", _pdftotext),
        _run_check(
            "embedding",
            lambda: _embedding(
                arguments.embedding_endpoint,
                arguments.embedding_model,
                arguments.embedding_dimensions,
                arguments.timeout_seconds,
            ),
        ),
        _run_check("lm_studio", lambda: _llm(arguments.llm_base_url, arguments.llm_model, arguments.timeout_seconds)),
    )
    report = {"checks": checks, "status": "ok" if all(item["status"] == "ok" for item in checks) else "failed"}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
