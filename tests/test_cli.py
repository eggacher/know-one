import pytest

from know_one.cli import _schema_sql, main


def test_help_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    """项目入口至少应能在未连接数据库时展示可用命令。"""
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    assert "init-db" in capsys.readouterr().out


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
