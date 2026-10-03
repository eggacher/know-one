"""部署前检查示例的协议判断测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    """按路径加载示例脚本，避免将部署检查引入核心包。"""
    path = Path(__file__).parents[1] / "examples" / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_identifies_the_requested_lm_studio_model_and_load_state() -> None:
    """模型列表存在不等于已加载，报告必须保留这一区别。"""
    module = _module()
    assert module._lm_studio_model_state(
        {"models": [{"key": "qwen3.5-9b", "loaded_instances": [{"id": "instance-1"}]}]},
        "qwen3.5-9b",
    ) == (True, True)
    assert module._lm_studio_model_state(
        {"models": [{"key": "qwen3.5-9b", "loaded_instances": []}]},
        "qwen3.5-9b",
    ) == (True, False)
    assert module._lm_studio_model_state({"models": []}, "qwen3.5-9b") == (False, False)
