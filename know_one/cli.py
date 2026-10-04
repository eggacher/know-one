"""KnowOne 的最小命令行入口。"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from importlib import resources
from typing import Sequence
from uuid import uuid4

from know_one.config import load_local_env
from know_one.core.api import KnowOne
from know_one.errors import KnowOneError
from know_one.full_text import FULL_TEXT_CONFIG_VERSION
from know_one.model import AccessScope, PERMISSIONS


def _local_admin_scope(namespace: str, actor: str) -> AccessScope:
    """构造仅供受控本地管理命令使用的固定管理员授权范围。"""
    if not actor.strip():
        raise ValueError("actor 不能为空")
    return AccessScope(actor, frozenset({namespace}), PERMISSIONS)


def _parse_timestamp(value: str) -> datetime:
    """解析带时区的 ISO 8601 时间，拒绝 CLI 无法明确解释的本地时间。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("时间必须是 ISO 8601 格式") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须携带时区")
    return parsed

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
        # v3-table：表格前次级标题开新块，表格与前置说明段分离；与
        # api.create_index_generation 保持一致。
        "chunker": "m1-pdf-section-v3-table",
        "embedding_model": model,
        "dimensions": dimensions,
        "full_text": FULL_TEXT_CONFIG_VERSION,
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
    create_generation_parser = subcommands.add_parser(
        "create-index-generation", help="按当前配置创建 building IndexGeneration"
    )
    create_generation_parser.add_argument("name", help="目标 Namespace 名称")
    create_generation_parser.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    rebuild_generation_parser = subcommands.add_parser(
        "rebuild-index-generation", help="为 building IndexGeneration 重建全部 Revision"
    )
    rebuild_generation_parser.add_argument("name", help="目标 Namespace 名称")
    rebuild_generation_parser.add_argument("generation_id", help="building IndexGeneration ID")
    rebuild_generation_parser.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    process_next_parser = subcommands.add_parser(
        "process-next-job", help="领取并处理一条 queued 或租约已过期的入库任务"
    )
    process_next_parser.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    process_next_parser.add_argument(
        "--lease-seconds",
        type=int,
        default=300,
        help="本次任务的 worker 租约秒数，默认 300",
    )
    activate_generation_parser = subcommands.add_parser(
        "activate-index-generation", help="原子切换已完整重建的 IndexGeneration"
    )
    activate_generation_parser.add_argument("name", help="目标 Namespace 名称")
    activate_generation_parser.add_argument("generation_id", help="building IndexGeneration ID")
    activate_generation_parser.add_argument(
        "--dsn",
        default=os.environ.get("KNOWONE_DSN"),
        help="PostgreSQL 连接串；未提供时读取 KNOWONE_DSN",
    )
    publish_parser = subcommands.add_parser("publish", help="以本地管理员身份发布 ready Revision")
    publish_parser.add_argument("name", help="目标 Namespace 名称")
    publish_parser.add_argument("revision_id")
    publish_parser.add_argument("valid_from", type=_parse_timestamp)
    publish_parser.add_argument("--valid-until", type=_parse_timestamp)
    publish_parser.add_argument("--expected-generation", type=int, required=True)
    publish_parser.add_argument("--idempotency-key", required=True)
    withdraw_parser = subcommands.add_parser("withdraw", help="以本地管理员身份撤回 Document")
    withdraw_parser.add_argument("name", help="目标 Namespace 名称")
    withdraw_parser.add_argument("document_id")
    withdraw_parser.add_argument("--expected-generation", type=int, required=True)
    withdraw_parser.add_argument("--idempotency-key", required=True)
    access_parser = subcommands.add_parser("set-access", help="以本地管理员身份替换 Document ACL")
    access_parser.add_argument("name", help="目标 Namespace 名称")
    access_parser.add_argument("document_id")
    access_parser.add_argument("acl", help="ACL JSON，例如 {\"principals\":[\"operator\"]}")
    access_parser.add_argument("--expected-generation", type=int, required=True)
    access_parser.add_argument("--idempotency-key", required=True)
    delete_parser = subcommands.add_parser("delete", help="以本地管理员身份立即删除 Document 内容")
    delete_parser.add_argument("name", help="目标 Namespace 名称")
    delete_parser.add_argument("document_id")
    delete_parser.add_argument("--idempotency-key", required=True)
    for command_parser in (publish_parser, withdraw_parser, access_parser, delete_parser):
        command_parser.add_argument("--actor", default="local-admin", help="写入审计的本地操作者")
        command_parser.add_argument("--dsn", default=os.environ.get("KNOWONE_DSN"))
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

    if arguments.command == "create-index-generation":
        if not arguments.dsn:
            parser.error("create-index-generation 需要 --dsn 或 KNOWONE_DSN")
        try:
            generation_id = KnowOne(arguments.dsn).create_index_generation(arguments.name)
        except (RuntimeError, ValueError, KnowOneError) as error:
            parser.error(str(error))
        print(f"IndexGeneration 已创建：{generation_id}")
        return 0

    if arguments.command == "rebuild-index-generation":
        if not arguments.dsn:
            parser.error("rebuild-index-generation 需要 --dsn 或 KNOWONE_DSN")
        try:
            KnowOne(arguments.dsn).rebuild_index_generation(arguments.name, arguments.generation_id)
        except (RuntimeError, ValueError, KnowOneError) as error:
            parser.error(str(error))
        print(f"IndexGeneration 已重建：{arguments.generation_id}")
        return 0

    if arguments.command == "process-next-job":
        if not arguments.dsn:
            parser.error("process-next-job 需要 --dsn 或 KNOWONE_DSN")
        if arguments.lease_seconds <= 0:
            parser.error("lease-seconds 必须大于 0")
        try:
            job_id = KnowOne(arguments.dsn).process_next_job(
                lease_seconds=arguments.lease_seconds
            )
        except (RuntimeError, ValueError, KnowOneError) as error:
            parser.error(str(error))
        if job_id is None:
            print("没有可处理的入库任务")
        else:
            print(f"入库任务已处理：{job_id}")
        return 0

    if arguments.command == "activate-index-generation":
        if not arguments.dsn:
            parser.error("activate-index-generation 需要 --dsn 或 KNOWONE_DSN")
        try:
            KnowOne(arguments.dsn).activate_index_generation(arguments.name, arguments.generation_id)
        except (RuntimeError, ValueError, KnowOneError) as error:
            parser.error(str(error))
        print(f"IndexGeneration 已激活：{arguments.generation_id}")
        return 0

    if arguments.command in {"publish", "withdraw", "set-access", "delete"}:
        if not arguments.dsn:
            parser.error(f"{arguments.command} 需要 --dsn 或 KNOWONE_DSN")
        try:
            scope = _local_admin_scope(arguments.name, arguments.actor)
            acl = None
            if arguments.command == "set-access":
                acl = json.loads(arguments.acl)
                if not isinstance(acl, dict):
                    raise ValueError("ACL 必须是 JSON 对象")
            know_one = KnowOne(arguments.dsn)
            if arguments.command == "publish":
                know_one.publish(
                    arguments.revision_id,
                    arguments.name,
                    arguments.valid_from,
                    arguments.valid_until,
                    arguments.expected_generation,
                    scope,
                    arguments.idempotency_key,
                )
                print(f"Revision 已发布：{arguments.revision_id}")
            elif arguments.command == "withdraw":
                know_one.withdraw(
                    arguments.document_id,
                    arguments.expected_generation,
                    scope,
                    arguments.idempotency_key,
                )
                print(f"Document 已撤回：{arguments.document_id}")
            elif arguments.command == "set-access":
                know_one.set_access(
                    arguments.document_id,
                    acl,
                    arguments.expected_generation,
                    scope,
                    arguments.idempotency_key,
                )
                print(f"Document ACL 已更新：{arguments.document_id}")
            else:
                know_one.delete(arguments.document_id, scope, arguments.idempotency_key)
                print(f"Document 内容已删除：{arguments.document_id}")
        except (RuntimeError, ValueError, KnowOneError) as error:
            parser.error(str(error))
        return 0

    parser.error(f"未知命令：{arguments.command}")
    return 2
