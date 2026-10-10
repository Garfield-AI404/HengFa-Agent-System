"""模型输出失败可观测性单测（[2026-09-10 可观测性]；2026-09-09 定稿口径）。

生产事故背景：2026-09-10 03:33:33Z 决策轮 HTTP 200 但输出未过
``AgentDecision.model_validate``，fail-closed 日志只有
``error_type=ValidationError``，分辨不了「max_tokens 截断」与「模型畸形
输出」（此前 12 连败 JSONDecodeError 同类盲点）。本套件钉住新增的脱敏
诊断特征（不记原始内容，11.4）：

- 决策轮假 SSE 流（monkeypatch ``model_client.httpx.Client``，不接真实
  模型）：语法级（非 JSON）最终失败日志含 finish_reason / output_bytes /
  ends_with_brace / output_head / output_tail；finish_reason=length 可
  区分截断；
- 语义级（缺 reply_text 且 next_action=reply，今日事故同款形态）：
  ``validation_issues=["reply_text:missing"]`` + ``parsed_keys`` 顶层键名，
  **日志任何字段不含输出正文**（head/tail 不落该路径）；
- 参数轮（假 chat + 真实 PydanticOutputParser 产生的包装异常）：语法/
  语义两类根因按 ``__cause__`` 解包记录特征；
- 行为零变化守卫：异常类型与用户文案不变、语义级 fail closed 不重试、
  语法级 6 次尝试（``_JSON_PARSE_MAX_RETRIES``=5）预算不变。

实测备注（影响用例构造）：langchain 参数轮走 ``parse_partial_json``
（宽松解析）——截断 JSON 会被补全后进入 pydantic 校验（ValidationError
路径），不产生 JSONDecodeError；语法级用例须用非 JSON 文本构造。

D5：假流/假 chat 夹具，不代表真实模型行为；不触达真实网络。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pydantic
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from structlog.testing import capture_logs

import administrative_agent.agent.model_client as model_client
from administrative_agent.agent.model_client import (
    AgentDecision,
    ModelCallError,
    ModelDecisionClient,
    ToolArguments,
)
from administrative_agent.config import Settings


# ---------------------------------------------------------------------------
# 夹具：假 httpx 流式客户端 / 假参数轮 chat / 测试 Settings
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    """确定性测试配置（显式入参覆盖 .env；不读真实凭据）。"""
    base: dict[str, Any] = {
        "model_api_key": "test-key-not-real",
        "model_base_url": "https://model.test/v1",
        "model_call_max_retries": 0,
        "model_reasoning_effort": "off",
    }
    base.update(overrides)
    return Settings(**base)


def _sse_lines(content: str, *, finish_reason: str | None) -> list[str]:
    """OpenAI 兼容 SSE 行：正文 delta（可空）+ 尾 chunk（finish_reason）+ [DONE]。"""
    chunks: list[dict[str, Any]] = []
    if content:
        chunks.append(
            {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
        )
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
    return [
        *(f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks),
        "data: [DONE]",
    ]


class _FakeStreamResponse:
    """假 httpx 流式响应（决策轮只用到 status_code/headers/iter_lines）。"""

    def __init__(self, lines: list[str], status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._lines = lines

    def iter_lines(self) -> Iterator[str]:
        return iter(self._lines)


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeHttpxClient:
    """假 httpx.Client：每次尝试共用同一批 SSE 行（重试返回同一响应）。"""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response
        self.stream_calls = 0

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def stream(self, method: str, url: str, *, headers=None, json=None):
        self.stream_calls += 1
        return _FakeStreamContext(self._response)


def _install_fake_stream(
    monkeypatch: pytest.MonkeyPatch, lines: list[str], *, status_code: int = 200
) -> _FakeHttpxClient:
    """把 model_client 模块内的 httpx.Client 换成假流式客户端。"""
    fake = _FakeHttpxClient(_FakeStreamResponse(lines, status_code))
    monkeypatch.setattr(model_client.httpx, "Client", lambda **_kwargs: fake)
    # 重试 sleep 一律置零（只验证日志与预算，不拖慢测试）
    monkeypatch.setattr(model_client.time, "sleep", lambda _seconds: None)
    return fake


class _FakeArgsChat:
    """假参数轮 chat：invoke 每次抛出预设解析异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def invoke(self, _payload: list) -> Any:
        self.calls += 1
        raise self._exc


def _real_wrapped_parse_error(raw: str) -> Exception:
    """用真实 langchain PydanticOutputParser 产生参数轮包装异常。

    与 ``with_structured_output(json_mode)`` 同实现：语法级 JSONDecodeError
    与语义级 ValidationError 均包成 OutputParserException（根因在
    ``__cause__``，输出文本在 ``llm_output``）。
    """
    try:
        PydanticOutputParser(pydantic_object=ToolArguments).parse(raw)
    except OutputParserException as exc:
        return exc
    raise AssertionError(f"raw 应触发解析失败：{raw!r}")


def _events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [record for record in logs if record.get("event") == event]


def _run_decision(client: ModelDecisionClient) -> None:
    """跑一次决策轮（预期 fail closed，异常由调用方 pytest.raises 断言）。"""
    client.generate_decision(
        system_prompt="系统提示",
        messages=[("human", "查询 8 月钣金订单金额")],
        schema_instruction="请只输出 json",
    )


def _run_tool_arguments(client: ModelDecisionClient) -> None:
    """跑一次参数轮（预期 fail closed，异常由调用方 pytest.raises 断言）。"""
    client.generate_tool_arguments(
        system_prompt="系统提示",
        messages=[("human", "创建订单")],
        schema_instruction="请只输出 json",
    )


# ---------------------------------------------------------------------------
# 决策轮：语法级（JSONDecodeError）——定稿口径四件套 + finish_reason
# ---------------------------------------------------------------------------


def test_decision_invalid_json_final_log_has_features(monkeypatch) -> None:
    """非 JSON 输出（截断）最终失败：finish_reason=length + 字节数 +
    ends_with_brace + 首尾 20 字符；重试日志（5 次）也带 finish_reason。"""
    # 截断在字符串中间（不闭合），finish_reason=length 即生产「截断」场景
    raw = '{"reply_text": "订单合计 1200 元", "next_action": "rep'
    fake = _install_fake_stream(monkeypatch, _sse_lines(raw, finish_reason="length"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError):
            _run_decision(client)

    failed = _events(logs, "model_call_failed")
    assert len(failed) == 1
    record = failed[0]
    assert record["tool_path"] == "decision"
    assert record["error_type"] == "JSONDecodeError"  # 归类不变
    assert record["finish_reason"] == "length"  # 截断可判定（核心盲点）
    assert record["output_bytes"] == len(raw.encode("utf-8"))  # 字节数非字符数
    assert record["output_bytes"] > len(raw)  # 中文多字节佐证
    assert record["ends_with_brace"] is False
    assert record["output_head"] == raw[:20]
    assert record["output_tail"] == raw[-20:]
    # 语法级重试预算不变：额外 5 次、总尝试 6 次；重试日志不落 head/tail
    retries = _events(logs, "model_call_retry")
    assert len(retries) == 5
    assert fake.stream_calls == 6
    for retry in retries:
        assert retry["finish_reason"] == "length"
        assert "output_head" not in retry and "output_tail" not in retry


def test_decision_invalid_json_brace_end_finish_reason_stop(monkeypatch) -> None:
    """语法错但以 } 结尾 + finish_reason=stop：ends_with_brace True 且
    finish_reason 非 length——排除「截断」、指向模型侧畸形输出。"""
    raw = '{"reply_text": , "next_action": "reply"}'
    _install_fake_stream(monkeypatch, _sse_lines(raw, finish_reason="stop"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError):
            _run_decision(client)

    record = _events(logs, "model_call_failed")[0]
    assert record["finish_reason"] == "stop"
    assert record["ends_with_brace"] is True
    assert record["error_type"] == "JSONDecodeError"


# ---------------------------------------------------------------------------
# 决策轮：语义级（ValidationError）——loc+type 摘要 + 顶层键名，不记原文
# ---------------------------------------------------------------------------


def test_decision_validation_error_log_structure_without_content(monkeypatch) -> None:
    """今日事故同款形态（next_action=reply 缺 reply_text）：日志含
    validation_issues=["reply_text:missing"] 与顶层键名，且任何字段不含
    输出正文（无 head/tail、无业务值）。"""
    marker = "SENSITIVE-ORDER-30176"
    raw = json.dumps({"next_action": "reply", "tool_name": marker}, ensure_ascii=False)
    fake = _install_fake_stream(monkeypatch, _sse_lines(raw, finish_reason="stop"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError) as excinfo:
            _run_decision(client)

    # 行为零变化：异常类型与用户文案保持原样
    assert "模型结构化输出解析失败" in str(excinfo.value)
    failed = _events(logs, "model_call_failed")
    assert len(failed) == 1
    record = failed[0]
    assert record["tool_path"] == "decision"
    assert record["error_type"] == "ValidationError"
    assert record["validation_issues"] == ["reply_text:missing"]
    assert record["parsed_keys"] == ["next_action", "tool_name"]
    assert record["finish_reason"] == "stop"
    assert record["output_bytes"] == len(raw.encode("utf-8"))
    # 语义级 fail closed 不重试（与今日事故的 fail-closed 语义一致）
    assert _events(logs, "model_call_retry") == []
    assert fake.stream_calls == 1
    # 脱敏断言：结构字段之外的正文/业务值/首尾预览一律不落日志
    serialized = json.dumps(record, ensure_ascii=False)
    assert marker not in serialized
    assert raw not in serialized
    assert "output_head" not in record and "output_tail" not in record


def test_decision_snapshot_keys_precede_coercion(monkeypatch) -> None:
    """parsed_keys 取 _coerce_decision 之前的快照：call_tool 缺 reply_text 时
    中间件会补文案——日志键名仍反映模型原始输出（不含补出的 reply_text）。"""
    raw = json.dumps({"next_action": "call_tool", "questions": "oops"})
    _install_fake_stream(monkeypatch, _sse_lines(raw, finish_reason="stop"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError):
            _run_decision(client)

    record = _events(logs, "model_call_failed")[0]
    assert record["validation_issues"] == ["questions:list_type"]
    assert record["parsed_keys"] == ["next_action", "questions"]  # 无补出的 reply_text


# ---------------------------------------------------------------------------
# 决策轮：[2026-09-10 6b 修复] 合法 JSON 但顶层非对象——优雅 fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shape"),
    [
        pytest.param("[]", "空数组", id="array"),
        pytest.param("null", "null", id="null"),
        pytest.param('"text"', "字符串", id="string"),
        pytest.param("123", "数字", id="number"),
        pytest.param("true", "布尔", id="boolean"),
    ],
)
def test_decision_non_object_json_fails_closed(monkeypatch, raw, shape) -> None:
    """[2026-09-10 6b 修复] 顶层非对象（空数组/null/字符串/数字/布尔）不得抛
    AttributeError 穿透：转 ValidationError → 优雅 ModelCallError（既有文案）
    + 特征日志（validation_issues=["model_type"]、无原文/无 head/tail）；
    语义级 fail closed 不重试。"""
    fake = _install_fake_stream(monkeypatch, _sse_lines(raw, finish_reason="stop"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError) as excinfo:  # 非 AttributeError
            _run_decision(client)

    # 行为变化仅限「穿透 → 优雅 fail closed」：文案与其他语义级畸形一致
    assert "模型结构化输出解析失败" in str(excinfo.value)
    assert "未执行任何操作" in str(excinfo.value)
    failed = _events(logs, "model_call_failed")
    assert len(failed) == 1
    record = failed[0]
    assert record["tool_path"] == "decision"
    assert record["error_type"] == "ValidationError"
    assert record["validation_issues"] == ["model_type"]
    assert record["finish_reason"] == "stop"
    assert record["output_bytes"] == len(raw.encode("utf-8"))
    assert "parsed_keys" not in record  # 顶层非 dict，无键名可记
    assert "output_head" not in record and "output_tail" not in record
    # 脱敏：无日志字符串字段等于原始输出（短形态按值精确断言，避免子串偶合）
    assert raw not in [v for v in record.values() if isinstance(v, str)]
    assert fake.stream_calls == 1  # 语义级 fail closed 不重试


def test_coerce_decision_passthrough_for_non_dict() -> None:
    """[2026-09-10 6b 修复] 守卫直测：非 dict 原样返回（不再抛
    AttributeError）；dict 路径行为不变（原有修复点照旧）。"""
    for value in ([], None, "text", 123, True):
        assert model_client._coerce_decision(value) == value
    # dict 路径回归：call_tool 缺 reply_text 仍补过渡文案
    assert model_client._coerce_decision({"next_action": "call_tool"}) == {
        "reply_text": "正在处理，请稍候。",
        "next_action": "call_tool",
    }


# ---------------------------------------------------------------------------
# 决策轮：空输出——finish_reason 可判定（length=输出预算耗尽/截断）
# ---------------------------------------------------------------------------


def test_decision_empty_output_log_carries_finish_reason(monkeypatch) -> None:
    """空输出（无 content delta）最终失败：日志附 finish_reason，仍 fail
    closed 不重试。"""
    fake = _install_fake_stream(monkeypatch, _sse_lines("", finish_reason="length"))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        with pytest.raises(ModelCallError) as excinfo:
            _run_decision(client)

    assert "流式输出为空" in str(excinfo.value)
    record = _events(logs, "model_call_failed")[0]
    assert record["error_type"] == "ModelCallError"
    assert record["finish_reason"] == "length"
    assert fake.stream_calls == 1  # 空输出不重试（既有语义不变）


# ---------------------------------------------------------------------------
# 参数轮：langchain 包装异常（__cause__ 解包）——两类根因分别记录
# ---------------------------------------------------------------------------


def test_tool_arguments_json_decode_error_log_has_features(monkeypatch) -> None:
    """参数轮语法级失败（OutputParserException 包装 JSONDecodeError）：
    特征按 __cause__ 解包，llm_output 作为解析候选文本；无流式 chunk →
    finish_reason 字段省略。"""
    # 非 JSON 文本才能触发 langchain 的 JSONDecodeError（宽松解析补全不算）
    raw = "订单创建请求：创建 8 月钣金订单，编号 A-178。"
    exc = _real_wrapped_parse_error(raw)
    assert isinstance(exc.__cause__, json.JSONDecodeError)
    client = ModelDecisionClient(settings=_settings())
    fake_chat = _FakeArgsChat(exc)
    monkeypatch.setattr(client, "_ensure_args_chat", lambda: fake_chat)
    monkeypatch.setattr(model_client.time, "sleep", lambda _seconds: None)

    with capture_logs() as logs:
        with pytest.raises(ModelCallError):
            _run_tool_arguments(client)

    failed = _events(logs, "model_call_failed")
    assert len(failed) == 1
    record = failed[0]
    assert record["tool_path"] == "tool_arguments"
    assert record["error_type"] == "OutputParserException"  # 外层类名不变
    assert record["output_bytes"] == len(raw.encode("utf-8"))
    assert record["ends_with_brace"] is False
    assert record["output_head"] == raw[:20]
    assert record["output_tail"] == raw[-20:]
    assert "finish_reason" not in record  # 参数轮无流式 chunk，不可得
    # 语法级重试预算不变：额外 5 次、总尝试 6 次
    assert fake_chat.calls == 6
    assert len(_events(logs, "model_call_retry")) == 5


def test_tool_arguments_validation_error_log_structure_without_content(monkeypatch) -> None:
    """参数轮语义级失败（OutputParserException 包装 ValidationError）：
    记 loc+type 摘要 + 顶层键名，不含输出正文/业务值。"""
    marker = "SENSITIVE-ORDER-30176"
    raw = json.dumps(
        {"tool_name": "order_create", "tool_arguments": marker}, ensure_ascii=False
    )
    exc = _real_wrapped_parse_error(raw)
    assert exc.__cause__ is not None and exc.__cause__.__class__.__name__ == "ValidationError"
    client = ModelDecisionClient(settings=_settings())
    fake_chat = _FakeArgsChat(exc)
    monkeypatch.setattr(client, "_ensure_args_chat", lambda: fake_chat)
    monkeypatch.setattr(model_client.time, "sleep", lambda _seconds: None)

    with capture_logs() as logs:
        with pytest.raises(ModelCallError):
            _run_tool_arguments(client)

    record = _events(logs, "model_call_failed")[0]
    assert record["tool_path"] == "tool_arguments"
    assert record["validation_issues"] == ["tool_arguments:dict_type"]
    assert record["parsed_keys"] == ["tool_arguments", "tool_name"]
    assert "finish_reason" not in record
    # 脱敏断言：业务值不落日志（llm_output 只用于反解键名）
    serialized = json.dumps(record, ensure_ascii=False)
    assert marker not in serialized
    assert raw not in serialized
    assert "output_head" not in record and "output_tail" not in record
    # 现状说明（既有行为，本次改动不变）：langchain 把 ValidationError 包成
    # OutputParserException → _is_json_parse_error 按外层类名判定为语法级，
    # 走解析重试预算（6 次尝试）；若后续修正该归类，本断言随之更新。
    assert fake_chat.calls == 6


# ---------------------------------------------------------------------------
# 单元级：特征提取助手（脱敏边界与防御上限）
# ---------------------------------------------------------------------------


def _many_field_validation_error() -> pydantic.ValidationError:
    """构造 20 个必填字段的 ValidationError（测错误摘要条数上限）。"""
    model = pydantic.create_model(
        "ManyFields", **{f"f{index:02d}": (int, ...) for index in range(20)}
    )
    try:
        model.model_validate({})
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError("空输入应产生 20 条 missing 错误")


def test_parse_failure_features_desensitization_and_caps() -> None:
    """助手直接断言：语法级只出四件套；语义级无正文；键集/错误列表设上限。"""
    # 语法级：head/tail 恰好 20 字符、ends_with_brace 按去尾空白判定
    raw_json = '{"reply_text": "x' + "y" * 50
    features = model_client._parse_failure_features(
        json.JSONDecodeError("Expecting value", raw_json, 0),
        raw_json,
        finish_reason="length",
    )
    assert features["output_head"] == raw_json[:20]
    assert features["output_tail"] == raw_json[-20:]
    assert features["output_bytes"] == len(raw_json.encode("utf-8"))
    assert features["ends_with_brace"] is False
    assert features["finish_reason"] == "length"
    # 尾空白不影响「以 } 结尾」判定
    assert model_client._parse_failure_features(
        json.JSONDecodeError("Expecting value", "{} \n", 0), "{} \n"
    )["ends_with_brace"] is True

    # 语义级：只出结构字段（loc+type / 顶层键名），不出 head/tail
    try:
        ToolArguments.model_validate({"tool_name": 1})
        raise AssertionError("应校验失败")
    except pydantic.ValidationError as validation_exc:
        val_err = validation_exc
    semantic = model_client._parse_failure_features(val_err, None)
    assert semantic["validation_issues"] == [
        "tool_name:string_type",
        "tool_arguments:missing",
    ]
    assert "parsed_keys" not in semantic  # 无已解析 JSON 可记
    assert "output_head" not in semantic and "output_tail" not in semantic
    assert "output_bytes" not in semantic  # output_text=None → 字段省略

    # 语义级 + 可反解文本：顶层键名从 llm_output 反解（参数轮路径）
    from_llm_output = model_client._parse_failure_features(
        val_err, '{"tool_name": 1, "tool_arguments": {}}'
    )
    assert from_llm_output["parsed_keys"] == ["tool_arguments", "tool_name"]

    # 防御上限：超大键集 / 超多错误列表被截断并附计数标记
    many_keys = {f"k{index}": index for index in range(30)}
    keys = model_client._parsed_top_keys(many_keys)
    assert keys is not None and keys[-1] == "...(+18)"
    assert len(keys) == 13  # 12 个键名 + 计数标记
    long_key = "x" * 80
    assert model_client._parsed_top_keys({long_key: 1}) == [long_key[:40]]
    issues = model_client._validation_issue_summary(_many_field_validation_error())
    assert issues[-1] == "...(+8)"  # 20 条 → 12 条 + 计数标记
    assert len(issues) == 13
    assert issues[0] == "f00:missing"


def test_parse_failure_features_unwraps_langchain_cause() -> None:
    """参数轮包装异常按 __cause__ 解包：语义级不误走语法级 head/tail 分支。"""
    raw = json.dumps({"tool_name": 1})
    exc = _real_wrapped_parse_error(raw)
    assert exc.__class__.__name__ == "OutputParserException"
    features = model_client._parse_failure_features(exc, exc.llm_output)
    assert features["validation_issues"] == [
        "tool_name:string_type",
        "tool_arguments:missing",
    ]
    assert "output_head" not in features and "output_tail" not in features


def test_log_model_failed_omits_none_features() -> None:
    """可选字段为 None 时不落（避免 null 噪音）；既有字段面保持不变。"""
    with capture_logs() as logs:
        model_client._log_model_failed(
            "decision", ModelCallError("x"), None, 12,
            finish_reason=None, output_bytes=3,
        )
    record = _events(logs, "model_call_failed")[0]
    assert record["tool_path"] == "decision"
    assert record["error_type"] == "ModelCallError"
    assert record["http_status"] is None  # 既有字段保留（口径不变）
    assert record["duration_ms"] == 12
    assert record["output_bytes"] == 3
    assert "finish_reason" not in record


def test_retry_log_finish_reason_optional() -> None:
    """重试日志：finish_reason 非 None 才落；既有字段面不变。"""
    with capture_logs() as logs:
        model_client._log_model_retry("decision", 2, ModelCallError("x"), 429, 2)
        model_client._log_model_retry(
            "decision", 3, ModelCallError("x"), None, 1, finish_reason="length"
        )
    records = _events(logs, "model_call_retry")
    assert "finish_reason" not in records[0]
    assert records[1]["finish_reason"] == "length"
    assert records[0]["error_type"] == "ModelCallError"
    assert records[0]["http_status"] == 429


def test_agent_decision_field_order_stable() -> None:
    """防御：诊断特征依赖 AgentDecision 既有字段（reply_text 必填、
    next_action 三值枚举）——字段面被改动时本套件应立刻失败。

    [2026-09-13 skill 层接入] 按预期触发后追加 ``skill_name``（**追加在末尾**：
    字段顺序即传给模型的 JSON Schema 顺序，插在中间会让既有字段位置整体漂移）。
    该字段**有默认值**（None）——缺席等同 null（不进入流程），这是刻意的宽松侧，
    因为"必填"会让模型一次漏输出就 ValidationError → 整轮对话失败（爆炸半径
    不对称，论证见 model_client.AgentDecision 字段注释）。
    """
    assert list(AgentDecision.model_fields) == [
        "reply_text",
        "next_action",
        "tool_name",
        "tool_arguments",
        "questions",
        "skill_name",
    ]
