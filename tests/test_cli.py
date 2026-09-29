import pytest

from know_one.cli import _schema_sql, main
from know_one.model import PERMISSIONS


def test_help_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    """项目入口至少应能在未连接数据库时展示可用命令。"""
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    assert "init-db" in capsys.readouterr().out


def test_help_lists_namespace_setup_command(capsys: pytest.CaptureFixture[str]) -> None:
    """无需手工插表即可准备入库所需的 Namespace 与索引代。"""
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "create-namespace" in capsys.readouterr().out


def test_help_lists_index_generation_management_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """本地运维可发现创建、重建和激活索引代的受控命令。"""
    with pytest.raises(SystemExit):
        main(["--help"])

    output = capsys.readouterr().out
    assert "create-index-generation" in output
    assert "rebuild-index-generation" in output
    assert "activate-index-generation" in output


def test_help_lists_local_administrator_lifecycle_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """受控本地入口可发现发布、撤回、ACL 与删除命令。"""
    with pytest.raises(SystemExit):
        main(["--help"])

    output = capsys.readouterr().out
    assert "publish" in output
    assert "withdraw" in output
    assert "set-access" in output
    assert "delete" in output


@pytest.mark.parametrize(
    ("arguments", "operation", "scope_position"),
    [
        (
            [
                "publish",
                "game-a-cs",
                "revision-1",
                "2026-09-29T09:00:00+08:00",
                "--expected-generation",
                "0",
                "--idempotency-key",
                "publish-1",
                "--dsn",
                "postgresql://test",
            ],
            "publish",
            5,
        ),
        (
            [
                "withdraw",
                "game-a-cs",
                "document-1",
                "--expected-generation",
                "1",
                "--idempotency-key",
                "withdraw-1",
                "--dsn",
                "postgresql://test",
            ],
            "withdraw",
            2,
        ),
        (
            [
                "set-access",
                "game-a-cs",
                "document-1",
                '{"principals":["operator"]}',
                "--expected-generation",
                "2",
                "--idempotency-key",
                "acl-1",
                "--dsn",
                "postgresql://test",
            ],
            "set_access",
            3,
        ),
        (
            [
                "delete",
                "game-a-cs",
                "document-1",
                "--idempotency-key",
                "delete-1",
                "--dsn",
                "postgresql://test",
            ],
            "delete",
            1,
        ),
    ],
)
def test_lifecycle_commands_use_fixed_local_administrator_scope(
    arguments: list[str],
    operation: str,
    scope_position: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生命周期命令不接收调用者自声明的权限，只构造固定本地管理员 scope。"""
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeKnowOne:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://test"

        def __getattr__(self, name: str):
            def record(*call_arguments: object) -> None:
                calls.append((name, call_arguments))

            return record

    monkeypatch.setattr("know_one.cli.KnowOne", FakeKnowOne)

    assert main(arguments) == 0
    assert len(calls) == 1
    assert calls[0][0] == operation
    scope = calls[0][1][scope_position]
    assert scope.principal_id == "local-admin"
    assert scope.namespaces == frozenset({"game-a-cs"})
    assert scope.permissions == PERMISSIONS


def test_set_access_rejects_non_object_acl_before_connecting_to_storage(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACL 命令拒绝 JSON 数组等非对象输入，避免把歧义数据传入领域接口。"""
    monkeypatch.setattr("know_one.cli.KnowOne", lambda _: pytest.fail("不应调用 KnowOne"))
    with pytest.raises(SystemExit) as error:
        main(
            [
                "set-access",
                "game-a-cs",
                "document-1",
                "[]",
                "--expected-generation",
                "2",
                "--idempotency-key",
                "acl-1",
                "--dsn",
                "postgresql://test",
            ]
        )

    assert error.value.code == 2
    assert "ACL 必须是 JSON 对象" in capsys.readouterr().err


def test_init_db_requires_a_connection_string(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """未提供连接串时在连接数据库之前失败，避免误连默认库。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KNOWONE_DSN", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["init-db"])

    assert error.value.code == 2
    assert "需要 --dsn" in capsys.readouterr().err


def test_schema_is_packaged_with_the_cli() -> None:
    """CLI 从安装包读取 schema，避免依赖调用命令时所在目录。"""
    assert "CREATE TABLE namespace" in _schema_sql()
