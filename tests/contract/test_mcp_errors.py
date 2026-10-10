"""RV-08 契约测试：结构化错误归类、超时与认证失败（13.1/13.2/14.2）。

核心断言：只读 structuredContent.code 归类，不解析中文 message 决定重试；
未知错误码 fail closed；网络超时按"结果未知"处理，写操作不自动重试；
凭据永不进入错误输出。
"""

from __future__ import annotations

import httpx
import respx

from tests.contract.conftest import (
    ERP_MCP_URL,
    TEST_API_KEY,
    call_tool_result,
    structured_error,
)
from administrative_agent.mcp.errors import ErrorCategory, MCPError


# ---- 13.1 结构化错误码归类（mcp-api.md §8） ----

@respx.mock
def test_validation_error_mapping(client) -> None:
    """VALIDATION_ERROR → 修正参数或询问，不自动盲重试。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=structured_error("VALIDATION_ERROR", "参数错误"))
    )
    result = client.call_tool("update_order", {"order_id": 1})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.category == ErrorCategory.VALIDATION
    assert result.error.retryable is False


@respx.mock
def test_order_not_found_mapping(client) -> None:
    """ORDER_NOT_FOUND → 重新确认目标，不更换 ID 猜测。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=structured_error("ORDER_NOT_FOUND", "订单不存在"))
    )
    result = client.call_tool("update_order", {"order_id": 999})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ORDER_NOT_FOUND"
    assert result.error.category == ErrorCategory.NOT_FOUND


@respx.mock
def test_permission_denied_mapping(client) -> None:
    """PERMISSION_DENIED → 认证/权限类别（13.1 停止写入并报告配置或权限问题）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=structured_error("PERMISSION_DENIED", "scope 不足"))
    )
    result = client.call_tool("create_order_bundle", {"order": {}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PERMISSION_DENIED"
    assert result.error.category == ErrorCategory.AUTH
    assert result.error.retryable is False


@respx.mock
def test_business_error_mapping(client) -> None:
    """BUSINESS_ERROR（安全包装后的数据库/业务失败）→ 保守归类。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=structured_error("BUSINESS_ERROR", "服务内部错误"))
    )
    result = client.call_tool("query", {"entity": "attendance"})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "BUSINESS_ERROR"
    assert result.error.retryable is False


@respx.mock
def test_unknown_entity_and_field_mapping(client) -> None:
    """UNKNOWN_ENTITY / UNKNOWN_FIELD → VALIDATION 类别（不重试）。"""
    for code in ("UNKNOWN_ENTITY", "UNKNOWN_FIELD"):
        respx.post(ERP_MCP_URL).mock(
            return_value=httpx.Response(200, json=structured_error(code))
        )
        result = client.call_tool("query", {"entity": "nope"})
        assert result.error is not None
        assert result.error.code == code
        assert result.error.category == ErrorCategory.VALIDATION


@respx.mock
def test_unknown_error_code_fail_closed(client) -> None:
    """未知错误码（不在 mcp-api.md §8 清单）→ fail closed：retryable=false。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=structured_error("SOME_NEW_FUTURE_CODE"))
    )
    result = client.call_tool("update_order", {"order_id": 1})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "SOME_NEW_FUTURE_CODE"
    assert result.error.category == ErrorCategory.UNKNOWN
    assert result.error.retryable is False


@respx.mock
def test_unstructured_error_without_code(client) -> None:
    """isError=true 且无 structuredContent / 非 JSON 文本 → 保守未知错误。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "something went wrong"}],
                    "isError": True,
                },
            },
        )
    )
    result = client.call_tool("update_order", {"order_id": 1})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_TOOL_ERROR_UNSTRUCTURED"
    assert result.error.retryable is False


@respx.mock
def test_text_error_with_json_code_extracted(client) -> None:
    """content[0].text 含 JSON 错误码（mcp-api.md §8 兼容形态）→ 归类。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"code": "ORDER_CREATE_REQUIRES_BUNDLE", "message": "..."}',
                        }
                    ],
                    "isError": True,
                },
            },
        )
    )
    result = client.call_tool("create", {"entity": "metal_orders"})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ORDER_CREATE_REQUIRES_BUNDLE"


# ---- 13.2/13.3：超时与网络错误（结果未知，不自动重放写操作） ----

@respx.mock
def test_read_timeout_result_unknown(client) -> None:
    """读取超时：结果未知（13.1 网络超时且结果未知 → 先 query 确认，不直接重放）。"""
    route = respx.post(ERP_MCP_URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
    result = client.call_tool("create_order_bundle", {"order": {}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_TIMEOUT_RESULT_UNKNOWN"
    assert result.error.category == ErrorCategory.NETWORK
    assert result.error.retryable is False
    assert result.error.details.get("transport_error") == "ReadTimeout"
    assert route.call_count == 1  # 本层不自动重试


@respx.mock
def test_connect_failure_mapped(client) -> None:
    """连接失败：ERP 不可达（13.4 降级：只允许解释性回复，不执行写入）。"""
    respx.post(ERP_MCP_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    result = client.call_tool("create", {"entity": "attendance"})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_CONNECT_FAILED"
    assert result.error.category == ErrorCategory.NETWORK


@respx.mock
def test_http_500_classified_server_error(client) -> None:
    """HTTP 5xx：单列 MCP_SERVER_ERROR/SERVER（决策项 c，项目负责人批准；
    结果未知语义保留，写操作不重试）。"""
    respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(500, text="internal error"))
    result = client.call_tool("update_order", {"order_id": 1})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_SERVER_ERROR"
    assert result.error.category == ErrorCategory.SERVER
    assert result.error.retryable is False
    assert "outcome unknown" in result.error.message


@respx.mock
def test_http_503_classified_server_error(client) -> None:
    """503 同属 5xx：MCP_SERVER_ERROR（供上层区分告警等级）。"""
    respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    result = client.call_tool("update_order", {"order_id": 1})
    assert result.error is not None
    assert result.error.code == "MCP_SERVER_ERROR"
    assert result.error.category == ErrorCategory.SERVER


@respx.mock
def test_http_400_mapped_rejected(client) -> None:
    """HTTP 400：请求被拒绝，不可重试。"""
    respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(400, text="bad request"))
    result = client.call_tool("query", {"entity": "attendance"})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_HTTP_ERROR"
    assert result.error.retryable is False


# ---- 协议健壮性 ----

@respx.mock
def test_sse_envelope_parsed(client) -> None:
    """Streamable HTTP 的 text/event-stream 响应可解析（MCP 协议形态）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"status\\": \\"created\\"}"}],"isError":false}}\n\n',
        )
    )
    result = client.call_tool("create_order_bundle", {"order": {}})
    assert result.ok
    assert result.data["status"] == "created"


@respx.mock
def test_rpc_error_envelope_mapped(client) -> None:
    """JSON-RPC 层错误（error 信封）→ RPC 类别。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}},
        )
    )
    result = client.call_tool("create_order_bundle", {"order": {}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_RPC_ERROR"
    assert result.error.category == ErrorCategory.RPC
    assert result.error.details["jsonrpc_code"] == -32601


@respx.mock
def test_success_text_content_is_json(client) -> None:
    """成功响应 content[0].text 为 JSON 时反序列化为 data（mcp-api.md §6.2/§7.4）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"status": "updated"}))
    )
    result = client.call_tool("update_order", {"order_id": 1})
    assert result.ok
    assert result.data["status"] == "updated"


def test_mcp_error_str_never_contains_credentials() -> None:
    """构造错误时 details 不携带凭据；str() 输出安全（14.4）。"""
    error = MCPError.from_http_status(401)
    assert TEST_API_KEY not in str(error)
    assert "api_key" not in str(error).lower() or "api_key" not in error.details
    assert error.requires_operator_action is False
