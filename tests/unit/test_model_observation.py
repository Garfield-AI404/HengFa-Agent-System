"""模型每轮决策/参数观测事件单测（[2026-09-16 观测]）。

背景：用户反馈「模型理解不够到位」，但**事后无法知道模型当时看到了什么、
选了什么工具、给了什么参数**——模型的决策（next_action/tool_name/
skill_name）与生成的参数此前不落库不落日志（只在 checkpoint 的
pending_action、被 tools 节点消费后清空）；只读查询（erp_query/erp_calc）
的参数完全无记录。本套件钉住新增的两条成功事件
``model_call_succeeded``（tool_path=decision / tool_arguments）：

- 决策轮成功 → 含 tool_path/next_action/tool_name/skill_name 等；
- 参数轮成功 → 含 arg_keys 与 args（紧凑 JSON）；
- **reply_text 原文不落**（只记 reply_chars，防回话原文泄漏）；
- args 超 800 字符 → 截断且带 ``…(truncated)`` 标记；
- 可选关联标识 thread_id/task_id 透传；
- usage 块（若流中携带）被解析进事件。

D5：假流/假 chat 夹具，不代表真实模型行为；不触达真实网络。只测日志，
断言行为零变化。
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

import administrative_agent.agent.model_client as model_client
from administrative_agent.agent.model_client import (
    ModelDecisionClient,
    ToolArguments,
)
from administrative_agent.config import Settings
from administrative_agent.observability.log_capture import (
    MAX_FIELD_LEN,
    LogRing,
    capture_processor,
    read_logs,
    sanitize_processor,
)


# ---------------------------------------------------------------------------
# 夹具（与 test_model_client_observability.py 同款假流/假 chat，成功路径）
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "model_api_key": "test-key-not-real",
        "model_base_url": "https://model.test/v1",
        "model_call_max_retries": 0,
        "model_reasoning_effort": "off",
    }
    base.update(overrides)
    return Settings(**base)


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
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


def _sse_lines(content: str, usage: dict[str, Any] | None = None) -> list[str]:
    """正文 delta + 尾块（finish_reason=stop）；可选带 usage 的独立块。"""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    if usage is not None:
        # OpenAI 兼容：usage 常在独立的末块（choices 为空）携带——本实现须在
        # choices 判定前读取才拿得到。
        chunks.append({"choices": [], "usage": usage})
    return [
        *(f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks),
        "data: [DONE]",
    ]


def _install_fake_stream(
    monkeypatch: pytest.MonkeyPatch, lines: list[str]
) -> _FakeHttpxClient:
    fake = _FakeHttpxClient(_FakeStreamResponse(lines))
    monkeypatch.setattr(model_client.httpx, "Client", lambda **_kwargs: fake)
    monkeypatch.setattr(model_client.time, "sleep", lambda _seconds: None)
    return fake


class _FakeArgsChat:
    """假参数轮 chat：invoke 返回预设 ToolArguments。"""

    def __init__(self, result: ToolArguments) -> None:
        self._result = result
        self.calls = 0

    def invoke(self, _payload: list) -> Any:
        self.calls += 1
        return self._result


def _events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [record for record in logs if record.get("event") == event]


def _run_decision(client: ModelDecisionClient, **kwargs: Any):
    return client.generate_decision(
        system_prompt="系统提示",
        messages=[("human", "查询 8 月钣金订单金额")],
        schema_instruction="请只输出 json",
        **kwargs,
    )


def _run_tool_arguments(client: ModelDecisionClient, **kwargs: Any):
    return client.generate_tool_arguments(
        system_prompt="系统提示",
        messages=[("human", "创建订单")],
        schema_instruction="请只输出 json",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 决策轮成功事件
# ---------------------------------------------------------------------------


def test_decision_success_event_fields(monkeypatch) -> None:
    """决策轮成功 → 一条 model_call_succeeded：tool_path/next_action/
    tool_name/skill_name/questions_count/reply_chars。"""
    payload = json.dumps(
        {
            "reply_text": "正在查询。",
            "next_action": "call_tool",
            "tool_name": "erp_query",
            "skill_name": "financial_report",
            "questions": None,
        },
        ensure_ascii=False,
    )
    _install_fake_stream(monkeypatch, _sse_lines(payload))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        decision = _run_decision(client)

    assert decision.next_action == "call_tool"  # 行为零变化：结果照常返回
    records = _events(logs, "model_call_succeeded")
    assert len(records) == 1
    record = records[0]
    assert record["tool_path"] == "decision"
    assert record["next_action"] == "call_tool"
    assert record["tool_name"] == "erp_query"
    assert record["skill_name"] == "financial_report"
    assert record["questions_count"] == 0
    assert record["reply_chars"] == len("正在查询。")
    assert record["retried"] is False
    # [2026-09-16] attempt 数值与 retried 并存（区分「第 N 次才成功」）
    assert record["attempt"] == 0
    assert isinstance(record["duration_ms"], int)


def test_decision_success_tool_name_null_when_reply(monkeypatch) -> None:
    """next_action=reply 时 tool_name 如实写 null（决策可见性）。"""
    payload = json.dumps(
        {"reply_text": "好的。", "next_action": "reply"}, ensure_ascii=False
    )
    _install_fake_stream(monkeypatch, _sse_lines(payload))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        _run_decision(client)

    record = _events(logs, "model_call_succeeded")[0]
    assert record["tool_name"] is None
    assert record["next_action"] == "reply"


def test_decision_success_does_not_leak_reply_text(monkeypatch) -> None:
    """脱敏：reply 原文绝不出现在事件里（只记 reply_chars）。"""
    marker = "SENSITIVE-REPLY-30176"
    payload = json.dumps(
        {"reply_text": marker, "next_action": "reply"}, ensure_ascii=False
    )
    _install_fake_stream(monkeypatch, _sse_lines(payload))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        _run_decision(client)

    record = _events(logs, "model_call_succeeded")[0]
    assert marker not in json.dumps(record, ensure_ascii=False)
    assert record["reply_chars"] == len(marker)


def test_decision_success_passthrough_ids_and_usage(monkeypatch) -> None:
    """可选 thread_id/task_id 透传；流内 usage 块被解析进事件。"""
    payload = json.dumps(
        {"reply_text": "查询中。", "next_action": "call_tool", "tool_name": "erp_query"},
        ensure_ascii=False,
    )
    usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 34,
        "total_tokens": 1234,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    _install_fake_stream(monkeypatch, _sse_lines(payload, usage=usage))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        _run_decision(client, thread_id="thread-abc", task_id="task-xyz")

    record = _events(logs, "model_call_succeeded")[0]
    assert record["thread_id"] == "thread-abc"
    assert record["task_id"] == "task-xyz"
    # [2026-09-16] 输出名避开 token 子串（否则被脱敏抹掉；见 model_client
    # _USAGE_TOKEN_KEYS 注释与真实链路的负向断言）
    assert record["ctx_prompt_count"] == 1200
    assert record["ctx_completion_count"] == 34
    assert record["ctx_total_count"] == 1234
    assert record["ctx_cache_hit_count"] == 1000
    assert record["ctx_cache_miss_count"] == 200
    assert record["ctx_cached_count"] == 1000


def test_decision_success_omits_usage_and_ids_when_absent(monkeypatch) -> None:
    """无 usage / 无 thread_id、task_id 时字段不落（避免 null 噪音）。"""
    payload = json.dumps(
        {"reply_text": "好的。", "next_action": "reply"}, ensure_ascii=False
    )
    _install_fake_stream(monkeypatch, _sse_lines(payload))
    client = ModelDecisionClient(settings=_settings())

    with capture_logs() as logs:
        _run_decision(client)

    record = _events(logs, "model_call_succeeded")[0]
    assert "ctx_prompt_count" not in record
    assert "thread_id" not in record
    assert "task_id" not in record


# ---------------------------------------------------------------------------
# 参数轮成功事件
# ---------------------------------------------------------------------------


def test_tool_arguments_success_event_fields(monkeypatch) -> None:
    """参数轮成功 → arg_keys（填了哪些字段）+ args（紧凑 JSON）。"""
    result = ToolArguments(
        tool_name="erp_query",
        tool_arguments={"entity": "metal_orders", "fields": ["id", "total_amount"]},
    )
    client = ModelDecisionClient(settings=_settings())
    monkeypatch.setattr(client, "_ensure_args_chat", lambda: _FakeArgsChat(result))

    with capture_logs() as logs:
        returned = _run_tool_arguments(client)

    assert returned.tool_name == "erp_query"  # 行为零变化
    records = _events(logs, "model_call_succeeded")
    assert len(records) == 1
    record = records[0]
    assert record["tool_path"] == "tool_arguments"
    assert record["tool_name"] == "erp_query"
    assert record["arg_keys"] == ["entity", "fields"]
    assert json.loads(record["args"]) == result.tool_arguments
    assert record["retried"] is False
    assert record["attempt"] == 0


def test_tool_arguments_args_truncated_over_limit(monkeypatch) -> None:
    """args 紧凑 JSON 超 800 字符 → 截断且带 …(truncated) 标记。"""
    big = "x" * 900
    result = ToolArguments(
        tool_name="erp_query", tool_arguments={"entity": "metal_orders", "filter": big}
    )
    client = ModelDecisionClient(settings=_settings())
    monkeypatch.setattr(client, "_ensure_args_chat", lambda: _FakeArgsChat(result))

    with capture_logs() as logs:
        _run_tool_arguments(client)

    record = _events(logs, "model_call_succeeded")[0]
    assert record["args"].endswith(model_client._ARGS_LOG_TRUNCATED)
    # 截断保留 800 字符正文 + 标记（标记长度另计，不挤占 800）
    assert len(record["args"]) == 800 + len(model_client._ARGS_LOG_TRUNCATED)
    # arg_keys 仍是完整参数名列表（不受值截断影响）
    assert record["arg_keys"] == ["entity", "filter"]


def test_tool_arguments_success_passthrough_ids(monkeypatch) -> None:
    result = ToolArguments(
        tool_name="order_create", tool_arguments={"order_no": "A-1"}
    )
    client = ModelDecisionClient(settings=_settings())
    monkeypatch.setattr(client, "_ensure_args_chat", lambda: _FakeArgsChat(result))

    with capture_logs() as logs:
        _run_tool_arguments(client, thread_id="thread-abc", task_id="task-xyz")

    record = _events(logs, "model_call_succeeded")[0]
    assert record["thread_id"] == "thread-abc"
    assert record["task_id"] == "task-xyz"


# ---------------------------------------------------------------------------
# 观测加固：成功事件日志异常绝不影响主链路（[2026-09-16]）
# ---------------------------------------------------------------------------


def test_success_log_failure_does_not_break_main_path(monkeypatch) -> None:
    """成功事件日志抛非 ModelCallError 异常 → 静默降级、不改返回值、不外抛。

    复刻此前 ``thread_id`` TypeError 的现场：调用点在裸函数体内，
    ``except ModelCallError`` 漏过一切其它异常 → 整轮炸穿。加固后日志异常
    必须被吞掉（事件缺失是唯一后果），返回值照常。
    """
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("log exploded")

    # 决策轮
    payload = json.dumps({"reply_text": "好的。", "next_action": "reply"}, ensure_ascii=False)
    _install_fake_stream(monkeypatch, _sse_lines(payload))
    monkeypatch.setattr(model_client, "_log_decision_succeeded", _boom)
    decision = _run_decision(ModelDecisionClient(settings=_settings()))
    assert decision.next_action == "reply"  # 返回值未变

    # 参数轮
    result = ToolArguments(tool_name="erp_query", tool_arguments={"entity": "metal_orders"})
    args_client = ModelDecisionClient(settings=_settings())
    monkeypatch.setattr(args_client, "_ensure_args_chat", lambda: _FakeArgsChat(result))
    monkeypatch.setattr(model_client, "_log_args_succeeded", _boom)
    returned = _run_tool_arguments(args_client, thread_id="t", task_id="k")
    assert returned is result  # 返回值未变（原对象）


# ---------------------------------------------------------------------------
# 真实 processor 链端到端（钉住 sanitize→capture(ring)→JSONRenderer 的交互）
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_log_chain(monkeypatch: pytest.MonkeyPatch):
    """按 main.py:78-91 装配**真实** structlog processor 链 + 独立 LogRing。

    与上面的 ``capture_logs``（绕过全部 processor）不同，本夹具走
    sanitize_processor → capture_processor(ring) → JSONRenderer，钉住真实
    链路上的截断/转义/脱敏交互。隔离措施：
    - ring 用**独立 LogRing 实例**（不碰全局单例），用例结束清空；
    - structlog 全局配置在用例内被改写，用 fixture 还原（顺序无关性）；
    - monkeypatch 换掉模块级 ``model_client.logger`` lazy proxy——此前用例若
      在 cache_logger_on_first_use=True 下用过它，旧配置会被缓存进 proxy，
      令本次 configure 不生效；fresh proxy 保证绑定本次配置，用例结束还原；
    - 渲染输出落到独立 StringIO（PrintLoggerFactory），确定性捕获末级
      JSONRenderer 的字节形态，不依赖 stdout 编码/缓冲。
    """
    saved_config = structlog.get_config()
    local_ring = LogRing()
    sink = io.StringIO()
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            sanitize_processor,            # 写缓冲前脱敏
            capture_processor(local_ring),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            sanitize_processor,            # 输出前再脱敏
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=False,
        logger_factory=structlog.PrintLoggerFactory(file=sink),
    )
    monkeypatch.setattr(model_client, "logger", structlog.get_logger("model_client"))
    try:
        yield local_ring, sink
    finally:
        local_ring.clear()
        structlog.configure(**saved_config)


def test_real_chain_args_unicode_redaction_and_usage_not_redacted(
    real_log_chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 processor 链：sanitize→capture(ring)→JSONRenderer 的交互。

    1. ``args`` 不被**二次截断**：模型层先按 800 截断加标记（实测 812），
       缓冲层 MAX_FIELD_LEN=1000 不应再动它；
    2. 中文**不被转义**（JSONRenderer ensure_ascii=False）；
    3. 命中 ``is_sensitive_key`` 的键（api_key）在链路上**照常脱敏**；
    4. **负向断言**：改名后的 usage 键（``ctx_*_count``）在真实链路上
       **未被脱敏**（值仍是数字，而非 ``***(len=N)``）——这正是改名的目的。
    """
    ring, sink = real_log_chain

    # (a) 决策轮：流内带 usage 块（走真实链，验证 usage 键不被抹）
    payload = json.dumps(
        {"reply_text": "查询中。", "next_action": "call_tool", "tool_name": "erp_query"},
        ensure_ascii=False,
    )
    usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 34,
        "total_tokens": 1234,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    _install_fake_stream(monkeypatch, _sse_lines(payload, usage=usage))
    decision_client = ModelDecisionClient(settings=_settings())
    _run_decision(decision_client, thread_id="thread-abc", task_id="task-xyz")

    # (b) 参数轮：长中文值（900 字符）→ 触发 _compact_args_json 的 800 截断
    result = ToolArguments(
        tool_name="erp_query",
        tool_arguments={"entity": "metal_orders", "备注": "中文" * 450},
    )
    args_client = ModelDecisionClient(settings=_settings())
    monkeypatch.setattr(args_client, "_ensure_args_chat", lambda: _FakeArgsChat(result))
    _run_tool_arguments(args_client, thread_id="thread-abc", task_id="task-xyz")

    # (c) 敏感键探针：走同一条已配置的真实链
    structlog.get_logger("probe").info("redaction_probe", api_key="SUPER-SECRET-VALUE")

    records = read_logs(ring, limit=20)[0]
    args_record = next(r for r in records if r.get("tool_path") == "tool_arguments")
    decision_record = next(r for r in records if r.get("tool_path") == "decision")

    # 1. args 未被二次截断（缓冲层 1000 上限不触发）
    args_val = args_record["args"]
    assert args_val.endswith(model_client._ARGS_LOG_TRUNCATED)
    assert len(args_val) == 800 + len(model_client._ARGS_LOG_TRUNCATED)
    assert len(args_val) < MAX_FIELD_LEN
    # 2. 中文原样保留在 ring 记录里
    assert "中文" in args_val

    # 3. 敏感键经真实链路被脱敏（redact_value → ***(len=N)）
    probe = next(r for r in records if r.get("event") == "redaction_probe")
    probe_value = probe["api_key"]
    assert probe_value.startswith("***")              # 已占位
    assert "SUPER-SECRET-VALUE" not in probe_value    # 原文未泄漏
    # 注：ring 写入与 read_logs 各脱敏一次，字符串值被再包一层
    # （***(len=18) → ***(len=11)）——安全性质（原文不外泄）保持。
    assert probe_value == "***(len=11)"
    assert "SUPER-SECRET-VALUE" not in json.dumps(probe, ensure_ascii=False)

    # 4. 负向断言：改名后的 usage 计数键未被脱敏（值仍是数字）
    for key, expected in (
        ("ctx_prompt_count", 1200),
        ("ctx_completion_count", 34),
        ("ctx_total_count", 1234),
        ("ctx_cache_hit_count", 1000),
        ("ctx_cache_miss_count", 200),
        ("ctx_cached_count", 1000),
    ):
        assert decision_record[key] == expected, (key, decision_record.get(key))
        assert not isinstance(decision_record[key], str)

    # 2b. JSONRenderer 的 ensure_ascii=False：渲染后不含 \uXXXX 转义
    rendered = sink.getvalue()
    assert "中文" in rendered
    assert "\\u4e2d" not in rendered  # 中 = U+4E2D
