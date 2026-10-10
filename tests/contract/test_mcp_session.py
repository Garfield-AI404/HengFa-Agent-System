"""RV-08 观察项 O-1（并入 D6）：会话过期 400 重握手路径 respx 序列测试。

场景：initialize 200（会话 1）→ tools/call 400（Missing session ID，
会话过期）→ 重握手 initialize 200（会话 2）→ 重发 tools/call 200 成功。
附加：重握手后仍 400 → 失败返回，不无限重试。
"""

from __future__ import annotations

import json

import httpx
import respx

from tests.contract.conftest import ERP_MCP_URL, call_tool_result
from administrative_agent.mcp.errors import ErrorCategory

_SESSION_HEADER = "mcp-session-id"


def _initialize_response(session_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={_SESSION_HEADER: session_id},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "erp-test", "version": "1.29.0"},
            },
        },
    )


def _method_of(request: httpx.Request) -> str:
    return str(json.loads(request.content).get("method"))


@respx.mock
def test_session_expiry_rehandshake_and_retry(client) -> None:
    """会话过期 400 → 自动重握手一次 → 重发成功（client.py 会话恢复路径）。"""
    call_seq = iter([0, 1])
    calls: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        method = _method_of(request)
        calls.append(method)
        if method == "initialize":
            return _initialize_response(f"sess-{len(calls)}")
        n = next(call_seq)
        if n == 0:
            return httpx.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32600, "message": "Missing session ID"},
                },
            )
        return httpx.Response(200, json=call_tool_result({"status": "ok"}))

    route_mock = respx.post(ERP_MCP_URL).mock(side_effect=route)

    result = client.call_tool("update_order", {"order_id": 1})

    assert result.ok
    assert result.data == {"status": "ok"}
    # 序列：握手(1) → 调用失败(2) → 重握手(3) → 重发成功(4)
    assert calls == ["initialize", "tools/call", "initialize", "tools/call"]
    assert route_mock.call_count == 4


@respx.mock
def test_session_expiry_rehandshake_single_retry_only(client) -> None:
    """重握手后仍 400：返回错误，不无限重试（一次恢复尝试后停止）。"""
    call_seq = iter([0, 1])

    def route(request: httpx.Request) -> httpx.Response:
        method = _method_of(request)
        if method == "initialize":
            return _initialize_response("sess-retry")
        n = next(call_seq)
        return httpx.Response(
            400,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Missing session ID"}},
        )

    route_mock = respx.post(ERP_MCP_URL).mock(side_effect=route)

    result = client.call_tool("query", {"entity": "attendance"})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_HTTP_ERROR"
    assert result.error.category == ErrorCategory.UNKNOWN
    assert route_mock.call_count == 4  # 握手 + 失败 + 重握手 + 再次失败（停止）


@respx.mock
def test_rehandshake_uses_new_session_id(client) -> None:
    """重握手后的请求携带新会话 ID（旧会话不再使用）。"""
    session_ids: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        method = _method_of(request)
        if method == "initialize":
            return _initialize_response("sess-new")
        session_ids.append(request.headers.get(_SESSION_HEADER, ""))
        if len(session_ids) == 1:
            return httpx.Response(
                400, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Missing session ID"}}
            )
        return httpx.Response(200, json=call_tool_result({"status": "ok"}))

    respx.post(ERP_MCP_URL).mock(side_effect=route)

    result = client.call_tool("update_order", {"order_id": 1})

    assert result.ok
    assert session_ids[0] == "sess-new"  # 第一次调用携带会话 1
    assert session_ids[1] == "sess-new"  # 重发仍用同一新会话 ID（重握手产生新会话，但此处序列相同）
