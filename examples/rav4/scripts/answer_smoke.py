"""评估本地 LLM 回答层的引用与拒答行为。"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import importlib.util
from io import StringIO
import json
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AnswerCase:
    """一条回答层回归样本；正例与负例使用不同可验证约束。"""

    identifier: str
    query: str
    expected_status: str
    expected_pages: tuple[int, ...] = ()


def load_cases(path: Path) -> tuple[AnswerCase, ...]:
    """读取 JSONL，并拒绝状态与页码约束矛盾的人工标注。"""
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            case = AnswerCase(
                value["id"],
                value["query"],
                value["expected_status"],
                tuple(value.get("expected_pages", [])),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError(f"第 {line_number} 行不是有效回答评测样本") from error
        if (
            not case.identifier.strip()
            or not case.query.strip()
            or case.expected_status not in {"answered", "insufficient_evidence", "no_evidence"}
            or any(not isinstance(page, int) or page <= 0 for page in case.expected_pages)
            or (case.expected_status == "answered" and not case.expected_pages)
            or (case.expected_status != "answered" and case.expected_pages)
        ):
            raise ValueError(f"第 {line_number} 行的状态或页码约束无效")
        cases.append(case)
    if not cases:
        raise ValueError("标注集至少需要一条样本")
    return tuple(cases)


def evaluate(
    cases: Sequence[AnswerCase], answer: Callable[[str], dict], clock: Callable[[], float] = monotonic
) -> dict:
    """只验证状态和程序化页码；自然语言回答内容由人工抽样审核。"""
    failures: dict[str, str] = {}
    latencies_ms: dict[str, int] = {}
    stage_latencies_ms: dict[str, dict[str, int]] = {}
    llm_stats: dict[str, dict] = {}
    for case in cases:
        started_at = clock()
        report = answer(case.query)
        latencies_ms[case.identifier] = round((clock() - started_at) * 1000)
        timings = report.get("timings_ms", {})
        stage_latencies_ms[case.identifier] = {
            "retrieval": int(timings.get("retrieval", 0)),
            "llm": int(timings.get("llm", 0)),
        }
        if isinstance(report.get("llm_stats"), dict) and report["llm_stats"]:
            # 服务端 stats 只在本地模型支持时出现；缺失不影响正确性评测。
            llm_stats[case.identifier] = report["llm_stats"]
        if report.get("status") != case.expected_status:
            failures[case.identifier] = f"状态为 {report.get('status')!r}"
            continue
        citations = report.get("citations")
        if case.expected_status != "answered":
            if citations:
                failures[case.identifier] = "拒答结果仍包含引用"
            continue
        pages = {
            citation.get("source_locator", {}).get("page")
            for citation in citations
            if isinstance(citation, dict)
        }
        if not pages.intersection(case.expected_pages):
            failures[case.identifier] = f"引用页码 {sorted(page for page in pages if page is not None)!r} 不匹配"
    ordered_latencies = sorted(latencies_ms.values())
    def percentile(ratio: float) -> int:
        """使用 nearest-rank 统计少量 smoke 样本，P95 不向下误报为中位数。"""
        return ordered_latencies[min(len(ordered_latencies) - 1, ceil(len(ordered_latencies) * ratio) - 1)]

    def stage_percentiles(stage: str) -> dict[str, int]:
        """按与端到端一致的 nearest-rank 口径汇总两个关键阶段。"""
        values = sorted(item[stage] for item in stage_latencies_ms.values())
        return {
            "p50": values[min(len(values) - 1, ceil(len(values) * 0.5) - 1)],
            "p95": values[min(len(values) - 1, ceil(len(values) * 0.95) - 1)],
        }
    summary = {
        "total_cases": len(cases),
        "passed": len(cases) - len(failures),
        "pass_rate": (len(cases) - len(failures)) / len(cases),
        "failures": failures,
        "latencies_ms": latencies_ms,
        "latency_summary_ms": {"p50": percentile(0.5), "p95": percentile(0.95)},
        "stage_latencies_ms": stage_latencies_ms,
        "stage_latency_summary_ms": {
            "retrieval": stage_percentiles("retrieval"),
            "llm": stage_percentiles("llm"),
        },
    }
    if llm_stats:
        summary["llm_stats"] = llm_stats
    return summary


def summarize_runs(reports: Sequence[dict]) -> dict:
    """汇总连续 smoke 的通过率与服务端首 token 尾延迟。"""
    total_cases = sum(int(report["total_cases"]) for report in reports)
    passed = sum(int(report["passed"]) for report in reports)
    first_token_seconds = sorted(
        value
        for report in reports
        for stats in report.get("llm_stats", {}).values()
        if isinstance(stats, dict)
        and isinstance(value := stats.get("time_to_first_token_seconds"), (int, float))
        and not isinstance(value, bool)
    )
    summary = {
        "total_runs": len(reports),
        "total_cases": total_cases,
        "passed": passed,
        "pass_rate": passed / total_cases,
    }
    if first_token_seconds:
        summary["time_to_first_token_seconds"] = {
            "p50": first_token_seconds[ceil(len(first_token_seconds) * 0.5) - 1],
            "p95": first_token_seconds[ceil(len(first_token_seconds) * 0.95) - 1],
        }
    return summary


def _answer_example_module():
    """加载调用方回答示例，避免评测脚本复制其检索与 LLM 请求逻辑。"""
    path = REPO / "examples" / "answer_with_evidence.py"
    spec = importlib.util.spec_from_file_location("know_one_answer_example", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 answer_with_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parser() -> argparse.ArgumentParser:
    """构造真实回答层 smoke 评测参数。"""
    parser = argparse.ArgumentParser(description="评估 RAV4 本地 LLM 回答的引用与拒答")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--dsn")
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="每个问题交给回答模型的最大证据条数；用于在同一回归集上比较上下文与耗时",
    )
    parser.add_argument("--max-output-tokens", type=int, default=200)
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="连续执行整套数据集的次数；大于 1 时输出首 token 的跨轮 P50/P95",
    )
    parser.add_argument("--deadline-ms", type=int, default=15000)
    parser.add_argument(
        "--context-neighbors",
        action="store_true",
        help="请求相邻原文作为可独立引用的答案补充上下文",
    )
    parser.add_argument(
        "--answer-timeout-seconds",
        type=float,
        default=120,
        help="单条本地 LLM 回答超时；9B 模型连续 smoke 时建议保留加载与排队余量",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行真实回答调用，并将稳定的状态／页码汇总输出为 JSON。"""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.runs <= 0:
        parser.error("runs 必须大于 0")
    module = _answer_example_module()
    cases = load_cases(arguments.dataset)

    def answer(query: str) -> dict:
        output = StringIO()
        command = [
            "--namespace", arguments.namespace, "--principal", arguments.principal,
            "--query", query, "--deadline-ms", str(arguments.deadline_ms),
            "--top-k", str(arguments.top_k),
            "--max-output-tokens", str(arguments.max_output_tokens),
            "--answer-timeout-seconds", str(arguments.answer_timeout_seconds),
            "--include-timings",
        ]
        if arguments.dsn:
            command.extend(["--dsn", arguments.dsn])
        if arguments.context_neighbors:
            command.append("--context-neighbors")
        with redirect_stdout(output), redirect_stderr(output):
            try:
                exit_code = module.main(command)
            except SystemExit as error:
                lines = output.getvalue().strip().splitlines()
                detail = lines[-1] if lines else f"退出码 {error.code}"
                raise RuntimeError(f"回答样本 {query!r} 失败：{detail}") from error
        if exit_code != 0:
            raise RuntimeError(f"回答示例退出码为 {exit_code}")
        return json.loads(output.getvalue())

    reports = tuple(evaluate(cases, answer) for _ in range(arguments.runs))
    result = reports[0] if arguments.runs == 1 else {"runs": reports, "summary": summarize_runs(reports)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
