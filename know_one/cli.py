"""KnowOne 的最小命令行入口。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from importlib import resources
from typing import Sequence
from uuid import uuid4

from know_one.config import load_local_env

def _schema_sql() -> str:
    """读取随 Python 包发布的初始 schema，避免依赖当前工作目录。"""
    return resources.files("know_one.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def init_database(dsn: str) -> None:
    """为一个空 PostgreSQL 数据库创建 KnowOne 初始表结构。"""
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 psycopg；请先安装项目依赖") from error

    # schema.sql 含 PL/pgSQL 函数体，函数体内也可能有分号；整体交给 PostgreSQL 解析。
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_schema_sql())


def create_namespace(dsn: str, name: str) -> str:
    """创建 Namespace 及其首个 active IndexGeneration。

    generation 的模型、维度和配置指纹在这里冻结；后续更换模型必须创建
    新 generation，不允许修改这一行来覆盖已经写入的 Chunk。
    """
    load_local_env()
    if not name.strip():
        raise ValueError("Namespace 名称不能为空")
    model = os.environ.get("KNOWONE_EMBEDDING_MODEL", "")
    try:
        dimensions = int(os.environ.get("KNOWONE_EMBEDDING_DIMENSIONS", "1024"))
    except ValueError as error:
        raise ValueError("KNOWONE_EMBEDDING_DIMENSIONS 必须是正整数") from error
    if not model or dimensions <= 0:
        raise ValueError("需要有效的 KNOWONE_EMBEDDING_MODEL 和 DIMENSIONS")
    config = {
        "chunker": "m1-paragraph-v1",
        "embedding_model": model,
        "dimensions": dimensions,
        "full_text": "postgres-simple-v1",
    }
    fingerprint = sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 psycopg；请先安装项目依赖") from error

    with psycopg.connect(dsn, row_factory=dict_row) as connection, connection.transaction():
        existing = connection.execute(
            """
            SELECT generation.id, generation.embedding_model, generation.dims
            FROM namespace
            LEFT JOIN index_generation AS generation
              ON generation.id = namespace.current_index_generation_id
            WHERE namespace.name = %s
            FOR UPDATE OF namespace
            """,
            (name,),
        ).fetchone()
        if existing:
            if existing["embedding_model"] == model and existing["dims"] == dimensions:
                return str(existing["id"])
            raise ValueError("Namespace 已存在，但当前 IndexGeneration 与本地配置不一致")

        namespace_id, generation_id = uuid4(), uuid4()
        connection.execute("INSERT INTO namespace (id, name) VALUES (%s, %s)", (namespace_id, name))
        connection.execute(
            """
            INSERT INTO index_generation (
                id, namespace_id, config_fingerprint, embedding_model,
                tokenizer_version, dims, distance, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'cosine', 'active')
            """,
            (generation_id, namespace_id, fingerprint, model, model, dimensions),
        )
        connection.execute(
            "UPDATE namespace SET current_index_generation_id = %s WHERE id = %s",
            (generation_id, namespace_id),
        )
    return str(generation_id)


def _parser() -> argparse.ArgumentParser:
    load_local_env()
    parser = argparse.ArgumentParser(description="KnowOne 本地管理命令")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_db = subcommands.add_parser("init-db", help="初始化空 PostgreSQL 数据库")
    init_db.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    create_namespace_parser = subcommands.add_parser(
        "create-namespace", help="创建 Namespace 与首个 active IndexGeneration"
    )
    create_namespace_parser.add_argument("name", help="Namespace 名称，例如 game-a-cs")
    create_namespace_parser.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 CLI 命令，并返回适合 shell 使用的退出码。"""
    parser = _parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "init-db":
        if not arguments.dsn:
            parser.error("init-db 需要 --dsn 或 KNOWONE_DSN")
        try:
            init_database(arguments.dsn)
        except RuntimeError as error:
            parser.error(str(error))
        print("数据库初始化完成")
        return 0

    if arguments.command == "create-namespace":
        if not arguments.dsn:
            parser.error("create-namespace 需要 --dsn 或 KNOWONE_DSN")
        try:
            generation_id = create_namespace(arguments.dsn, arguments.name)
        except (RuntimeError, ValueError) as error:
            parser.error(str(error))
        print(f"Namespace {arguments.name} 已就绪，IndexGeneration={generation_id}")
        return 0

    parser.error(f"未知命令：{arguments.command}")
    return 2
