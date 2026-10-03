"""本地 LLM 回答示例的证据边界测试。"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError

from know_one import ContextPart, Evidence, RetrievalResult


def _example_module():
    """按文件路径加载示例，避免把 examples 伪装成核心运行包。"""
    path = Path(__file__).parents[1] / "examples" / "answer_with_evidence.py"
    spec = importlib.util.spec_from_file_location("answer_with_evidence_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_answer_example_sends_evidence_to_lm_studio_and_returns_its_text(monkeypatch, capsys) -> None:
    """模型只读取 Evidence 编号，页码由调用方程序化返回。"""
    module = _example_module()

    class FakeKnowOne:
        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            evidence = Evidence(
                text="自动关闭后停止加油。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": 10, "page": 161},
                publication_valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                publication_valid_until=None,
            )
            return RetrievalResult((evidence,), "generation-1", "test-model", "trace-1")

    class Response:
        def read(self) -> bytes:
            return '{"output":[{"type":"message","content":"应停止加油。[证据 1] 第 162 页"}]}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def urlopen(request, timeout: float):
        assert request.full_url == "http://lm.local:1234/api/v1/chat"
        assert timeout == 12
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "qwen3.5-9b"
        assert payload["store"] is False
        assert payload["max_output_tokens"] == 80
        assert "自动关闭后停止加油。" in payload["input"]
        assert "第 161 页" not in payload["input"]
        assert "最多两句" in payload["input"]
        return Response()

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(module, "urlopen", urlopen)

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "reader", "--query", "自动跳枪后怎么办",
            "--dsn", "postgresql://test", "--llm-base-url", "http://lm.local:1234/api/v1",
            "--answer-timeout-seconds", "12", "--max-output-tokens", "80",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "answer": "应停止加油。[证据 1]",
        "citations": [
            {"evidence_index": 1, "source_locator": {"char_end": 10, "char_start": 0, "page": 161}}
        ],
        "status": "answered",
    }


def test_answer_example_can_cite_an_opt_in_context_part(monkeypatch, capsys) -> None:
    """相邻原文启用后可作为独立引用，不能借用主证据的页码。"""
    module = _example_module()

    class FakeKnowOne:
        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **kwargs: object) -> RetrievalResult:
            assert kwargs["include_context"] is True
            evidence = Evidence(
                text="拆下电量耗尽的电池。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-315",
                source_locator={"char_start": 20, "char_end": 30, "page": 315},
                publication_valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                publication_valid_until=None,
                context_parts=(
                    ContextPart(
                        text="更换电池前，请准备锂电池 CR2032。",
                        chunk_id="chunk-314",
                        source_locator={"char_start": 0, "char_end": 19, "page": 314},
                    ),
                ),
            )
            return RetrievalResult((evidence,), "generation-1", "test-model", "trace-1")

    class Response:
        def read(self) -> bytes:
            return '{"output":[{"type":"message","content":"使用锂电池 CR2032。[证据 2]"}]}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def urlopen(request, timeout: float):
        payload = json.loads(request.data.decode("utf-8"))
        assert "[证据 1]" in payload["input"]
        assert "[证据 2]" in payload["input"]
        assert "锂电池 CR2032" in payload["input"]
        return Response()

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(module, "urlopen", urlopen)

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "reader", "--query", "电子钥匙换什么型号电池",
            "--dsn", "postgresql://test", "--context-neighbors",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "answer": "使用锂电池 CR2032。[证据 2]",
        "citations": [
            {"evidence_index": 2, "source_locator": {"char_end": 19, "char_start": 0, "page": 314}}
        ],
        "status": "answered",
    }


def test_answer_example_rejects_an_answer_without_a_valid_evidence_reference(monkeypatch, capsys) -> None:
    """有检索候选但模型遗漏引用时，不能把未绑定的回答交给调用方。"""
    module = _example_module()

    class FakeKnowOne:
        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            evidence = Evidence(
                text="自动关闭后停止加油。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": 10, "page": 161},
                publication_valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                publication_valid_until=None,
            )
            return RetrievalResult((evidence,), "generation-1", "test-model", "trace-1")

    class Response:
        def read(self) -> bytes:
            return '{"output":[{"type":"message","content":"应停止加油。"}]}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: Response())

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "reader", "--query", "自动跳枪后怎么办",
            "--dsn", "postgresql://test",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "answer": None, "citations": [], "status": "uncited_answer"
    }


def test_answer_example_hides_nearest_candidates_when_model_reports_insufficient_evidence(
    monkeypatch, capsys
) -> None:
    """无答案判断不能把仅用于排除的向量候选误作支持性引用。"""
    module = _example_module()

    class FakeKnowOne:
        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            evidence = Evidence(
                text="无线遥控功能说明。",
                document_id="document-1",
                revision_id="revision-1",
                chunk_id="chunk-1",
                source_locator={"char_start": 0, "char_end": 9, "page": 94},
                publication_valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                publication_valid_until=None,
            )
            return RetrievalResult((evidence,), "generation-1", "test-model", "trace-1")

    class Response:
        def read(self) -> bytes:
            return '{"output":[{"type":"message","content":"现有资料不足以确认。[证据 1]"}]}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: Response())

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "reader", "--query", "股票代码",
            "--dsn", "postgresql://test",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "answer": None, "citations": [], "status": "insufficient_evidence"
    }


def test_answer_example_does_not_call_llm_when_retrieval_has_no_evidence(monkeypatch, capsys) -> None:
    """无依据应直接返回拒答状态，防止模型凭常识补全答案。"""
    module = _example_module()

    class FakeKnowOne:
        def __init__(self, _dsn: str) -> None:
            pass

        def retrieve(self, *_args: object, **_kwargs: object) -> RetrievalResult:
            return RetrievalResult((), "generation-1", "test-model", "trace-1")

    def unexpected_urlopen(*_args: object, **_kwargs: object):
        raise AssertionError("无 Evidence 时不得调用 LLM")

    monkeypatch.setattr(module, "KnowOne", FakeKnowOne)
    monkeypatch.setattr(module, "urlopen", unexpected_urlopen)

    assert module.main(
        [
            "--namespace", "manuals", "--principal", "reader", "--query", "未知问题",
            "--dsn", "postgresql://test",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "answer": None, "citations": [], "status": "no_evidence"
    }


def test_answer_example_reports_lm_studio_http_status_and_short_diagnostic(monkeypatch) -> None:
    """模型名或鉴权配置错误时，调用方应得到可行动的 HTTP 诊断。"""
    module = _example_module()

    def urlopen(*_args: object, **_kwargs: object):
        raise HTTPError(
            "http://lm.local:1234/api/v1/chat",
            404,
            "Not Found",
            None,
            BytesIO(b'{"error":"model is not loaded"}'),
        )

    monkeypatch.setattr(module, "urlopen", urlopen)

    try:
        module._answer("http://lm.local:1234/api/v1", "missing", "问题", "证据", 1, 200)
    except RuntimeError as error:
        assert str(error) == '本地 LLM 回答服务返回 HTTP 404：{"error":"model is not loaded"}'
    else:
        raise AssertionError("预期 HTTPError 被转换为可行动的 RuntimeError")


def test_answer_example_reports_lm_studio_network_reason(monkeypatch) -> None:
    """网络失败不能与服务端 JSON 错误混为一谈。"""
    module = _example_module()
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("timed out")))

    try:
        module._answer("http://lm.local:1234/api/v1", "model", "问题", "证据", 12, 200)
    except RuntimeError as error:
        assert str(error) == "本地 LLM 回答服务网络错误：timed out"
    else:
        raise AssertionError("预期 URLError 被转换为网络诊断")


def test_answer_example_keeps_only_lm_studio_documented_performance_stats() -> None:
    """诊断输出只保留稳定、可比较的服务端指标。"""
    module = _example_module()
    assert module._performance_stats(
        {
            "input_tokens": 321,
            "total_output_tokens": 45,
            "reasoning_output_tokens": 12,
            "tokens_per_second": 18.5,
            "time_to_first_token_seconds": 2.1,
            "model_load_time_seconds": 4.2,
            "unrelated": "不应输出",
        }
    ) == {
        "input_tokens": 321,
        "total_output_tokens": 45,
        "reasoning_output_tokens": 12,
        "tokens_per_second": 18.5,
        "time_to_first_token_seconds": 2.1,
        "model_load_time_seconds": 4.2,
    }


def test_answer_example_stream_releases_only_a_complete_cited_sentence() -> None:
    """流式片段在有效引用到达前不能泄露给调用方。"""
    module = _example_module()
    emitted: list[str] = []
    cursor = module._emit_verified_segments("应停止加油。", 1, 0, emitted.append)
    assert cursor == 0
    assert emitted == []

    cursor = module._emit_verified_segments("应停止加油。[证据 1]", 1, cursor, emitted.append)
    assert cursor == len("应停止加油。[证据 1]")
    assert emitted == ["应停止加油。[证据 1]"]


def test_answer_example_stream_accepts_sentence_punctuation_after_the_citation() -> None:
    """模型常把句号置于引用之后，这种完整句也应在流中释放。"""
    module = _example_module()
    emitted: list[str] = []
    cursor = module._emit_verified_segments("应停止加油[证据 1]。", 1, 0, emitted.append)
    assert cursor == len("应停止加油[证据 1]。")
    assert emitted == ["应停止加油[证据 1]。"]


def test_answer_example_stream_uses_final_chat_end_response_for_the_result() -> None:
    """增量文本只用于展示，最终判断仍以 chat.end 的聚合结果为准。"""
    module = _example_module()
    response = iter(
        line.encode("utf-8")
        for line in (
            "event: message.delta\n",
            'data: {"type":"message.delta","content":"应停止加油。"}\n',
            "\n",
            "event: message.delta\n",
            'data: {"type":"message.delta","content":"[证据 1]"}\n',
            "\n",
            "event: chat.end\n",
            'data: {"type":"chat.end","result":{"output":[{"type":"message","content":"应停止加油。[证据 1]"}]}}\n',
            "\n",
        )
    )
    emitted: list[str] = []
    first_delta: list[str] = []
    body = module._stream_body(response, 1, emitted.append, lambda: first_delta.append("received"))
    assert module._message_text(body) == "应停止加油。[证据 1]"
    assert emitted == ["应停止加油。[证据 1]"]
    assert first_delta == ["received"]


def test_answer_example_stream_releases_content_after_a_valid_leading_citation() -> None:
    """引用前置协议允许在句子结束前安全展示后续正文增量。"""
    module = _example_module()
    response = iter(
        line.encode("utf-8")
        for line in (
            "event: message.delta\n",
            'data: {"type":"message.delta","content":"[证据 1]"}\n',
            "\n",
            "event: message.delta\n",
            'data: {"type":"message.delta","content":"应立即停止加油。"}\n',
            "\n",
            "event: chat.end\n",
            'data: {"type":"chat.end","result":{"output":[{"type":"message","content":"[证据 1]应立即停止加油。"}]}}\n',
            "\n",
        )
    )
    citations: list[int] = []
    emitted: list[str] = []
    module._stream_body(response, 1, emitted.append, on_citation=citations.append)
    assert citations == [1]
    assert emitted == ["应立即停止加油。"]


def test_answer_example_stream_reports_model_load_and_prompt_processing_stages() -> None:
    """首 token 很慢时必须保留服务端阶段边界以定位等待发生的位置。"""
    module = _example_module()
    response = iter(
        line.encode("utf-8")
        for line in (
            "event: model_load.start\n",
            'data: {"type":"model_load.start"}\n',
            "\n",
            "event: prompt_processing.end\n",
            'data: {"type":"prompt_processing.end"}\n',
            "\n",
            "event: chat.end\n",
            'data: {"type":"chat.end","result":{"output":[{"type":"message","content":"[[INSUFFICIENT_EVIDENCE]]"}]}}\n',
            "\n",
        )
    )
    stages: list[str] = []
    module._stream_body(response, 1, lambda _text: None, on_stage=stages.append)
    assert stages == ["model_load.start", "prompt_processing.end"]
