"""KnowOne 的最小命令行入口。"""

from __future__ import annotations

import argparse
import os
from importlib import resources
from typing import Sequence

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

    parser.error(f"未知命令：{arguments.command}")
    return 2
