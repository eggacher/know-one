"""支持通过 ``python -m know_one`` 调用本地管理命令。"""

from know_one.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
