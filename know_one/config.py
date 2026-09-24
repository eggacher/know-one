"""本地开发环境变量读取。"""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(path: Path | None = None) -> None:
    """读取当前项目的 .env，且绝不覆盖进程已设置的环境变量。

    本项目只需要 ``KEY=VALUE`` 这一种简单格式；带引号的值会去掉外层
    同类引号。生产部署应直接由平台注入环境变量，不依赖此本地文件。
    """
    env_file = path or Path.cwd() / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
