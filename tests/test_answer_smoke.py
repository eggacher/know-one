"""回答层 smoke 评测的稳定状态与页码检查。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _module():
    """按文件路径加载评测脚本，保持 examples 与核心库解耦。"""
    path = Path(__file__).parents[1] / "examples" / "rav4" / "scripts" / "answer_smoke.py"
    spec = importlib.util.spec_from_file_location("answer_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在解析 postponed annotations 时会回查定义模块。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_answer_smoke_requires_expected_pages_for_answers_and_empty_citations_for_refusals() -> None:
    """正例必须引用标注页，拒答不能泄露相近但无关的候选。"""
    module = _module()
    cases = (
        module.AnswerCase("answer", "问题", "answered", (161,)),
        module.AnswerCase("refusal", "股票代码", "insufficient_evidence"),
    )
    reports = iter(
        (
            {"status": "answered", "citations": [{"source_locator": {"page": 161}}]},
            {"status": "insufficient_evidence", "citations": []},
        )
    )

    clock_values = iter((0.0, 0.1, 0.1, 0.4))
    assert module.evaluate(cases, lambda _query: next(reports), lambda: next(clock_values)) == {
        "total_cases": 2,
        "passed": 2,
        "pass_rate": 1.0,
        "failures": {},
        "latencies_ms": {"answer": 100, "refusal": 300},
        "latency_summary_ms": {"p50": 100, "p95": 300},
        "stage_latencies_ms": {
            "answer": {"retrieval": 0, "llm": 0},
            "refusal": {"retrieval": 0, "llm": 0},
        },
        "stage_latency_summary_ms": {
            "retrieval": {"p50": 0, "p95": 0},
            "llm": {"p50": 0, "p95": 0},
        },
    }


def test_answer_smoke_reports_wrong_pages_and_refusal_citations() -> None:
    """页码偏移或拒答带候选都必须成为可定位的回归失败。"""
    module = _module()
    cases = (
        module.AnswerCase("answer", "问题", "answered", (161,)),
        module.AnswerCase("refusal", "股票代码", "insufficient_evidence"),
    )
    reports = iter(
        (
            {"status": "answered", "citations": [{"source_locator": {"page": 162}}]},
            {"status": "insufficient_evidence", "citations": [{"source_locator": {"page": 94}}]},
        )
    )

    clock_values = iter((0.0, 0.1, 0.1, 0.4))
    report = module.evaluate(cases, lambda _query: next(reports), lambda: next(clock_values))
    assert report["failures"] == {
        "answer": "引用页码 [162] 不匹配",
        "refusal": "拒答结果仍包含引用",
    }
    assert report["latency_summary_ms"] == {"p50": 100, "p95": 300}


def test_answer_smoke_accepts_top_k_for_context_size_comparisons() -> None:
    """评测可固定其他条件，仅改变交给 LLM 的证据数量。"""
    module = _module()
    arguments = module._parser().parse_args(
        [
            "--dataset", "cases.jsonl", "--namespace", "rav4", "--principal", "evaluator",
            "--top-k", "1", "--max-output-tokens", "80", "--context-neighbors",
        ]
    )
    assert arguments.top_k == 1
    assert arguments.max_output_tokens == 80
    assert arguments.context_neighbors is True


def test_answer_smoke_summarizes_first_token_tail_latency_across_runs() -> None:
    """稳定性评测应把多轮的首 token 尾延迟单独汇总。"""
    module = _module()
    summary = module.summarize_runs(
        (
            {
                "total_cases": 1,
                "passed": 1,
                "llm_stats": {"first": {"time_to_first_token_seconds": 0.4}},
            },
            {
                "total_cases": 1,
                "passed": 0,
                "llm_stats": {"second": {"time_to_first_token_seconds": 3.8}},
            },
        )
    )
    assert summary == {
        "total_runs": 2,
        "total_cases": 2,
        "passed": 1,
        "pass_rate": 0.5,
        "time_to_first_token_seconds": {"p50": 0.4, "p95": 3.8},
    }
