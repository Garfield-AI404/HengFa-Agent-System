"""RV-07/RV-17 契约测试：考勤通用 CRUD、认证与载荷传递（14.2/规格 2.3）。

通用 create/update 只允许 attendance（2.3）；认证 X-API-Key 优先
（mcp-api.md §1）；凭据未配置时 fail closed，不发请求（10.5）；
D12 补充：字段载荷原样传递、同一业务操作复用 operation_id（§5）、
结构化错误归类（§8）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.contract.conftest import (
    ERP_MCP_URL,
    TEST_API_KEY,
    call_tool_result,
    structured_error,
)
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.scopes import verify_generic_write

ATTENDANCE_CREATE_SUCCESS = {
    "status": "created",
    "record": {"id": 100, "employee_id": 1, "att_date": "2026-08-19"},
    "operation_id": "op-att-1",
    "trace_id": "trace-att-1",
}


# ---- 白名单（2.3：create/update 只由考勤编排服务调用 attendance） ----

def test_create_attendance_allowed_locally() -> None:
    """通用 create 只允许 attendance；合法 payload 本地校验通过。"""
    verify_generic_write("create", "attendance", {"employee_id": 1, "att_date": "2026-08-19"})
    verify_generic_write("update", "attendance", {"overtime_minutes": 60})


def test_generic_create_other_entities_rejected() -> None:
    """其他实体（customers/suppliers）不允许通用 create/update → NOT_WHITELISTED。"""
    with pytest.raises(MCPError) as exc_info:
        verify_generic_write("create", "customers", {"name": "x"})
    assert exc_info.value.code == "NOT_WHITELISTED"
    with pytest.raises(MCPError) as exc_info:
        verify_generic_write("update", "suppliers", {"name": "y"})
    assert exc_info.value.code == "NOT_WHITELISTED"


def test_attendance_system_fields_rejected() -> None:
    """考勤写入同样禁止伪造系统字段（mcp-api.md §5）。"""
    with pytest.raises(MCPError) as exc_info:
        verify_generic_write("create", "attendance", {"employee_id": 1, "created_by": 5})
    assert exc_info.value.code == "SYSTEM_FIELD_FORBIDDEN"
    with pytest.raises(MCPError) as exc_info:
        verify_generic_write("update", "attendance", {"id": 100, "overtime_minutes": 30})
    assert exc_info.value.code == "SYSTEM_FIELD_FORBIDDEN"


# ---- 客户端认证与请求头（mcp-api.md §1） ----

@respx.mock
def test_attendance_create_via_client_success(client) -> None:
    """考勤 create 走客户端成功路径；断言 X-API-Key 认证头且无 Bearer。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(ATTENDANCE_CREATE_SUCCESS))
    )
    result = client.call_tool(
        "create",
        {"entity": "attendance", "data": {"employee_id": 1, "att_date": "2026-08-19"}},
    )
    assert result.ok
    assert result.data["record"]["id"] == 100

    # 握手（initialize）与工具调用都携带 X-API-Key；工具调用取 method=tools/call
    all_requests = [c.request for c in route.calls]
    assert len(all_requests) == 2  # initialize + tools/call
    for request in all_requests:
        assert request.headers["X-API-Key"] == TEST_API_KEY
        assert "Authorization" not in request.headers
    tool_request = next(
        r for r in all_requests if json.loads(r.content).get("method") == "tools/call"
    )
    sent = json.loads(tool_request.content)
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "tools/call"
    assert "operation_id" in sent["params"]["arguments"]  # 写工具注入（§5）


@respx.mock
def test_auth_401_fails_without_retry(client) -> None:
    """HTTP 401：认证失败归类、不重试（13.1 MCP 认证/权限错误停止写入）。"""
    route = respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    result = client.call_tool(
        "create",
        {"entity": "attendance", "data": {"employee_id": 1}},
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "MCP_AUTH_FAILED"
    assert result.error.category == ErrorCategory.AUTH
    assert result.error.retryable is False
    assert result.error.requires_operator_action is False
    assert route.call_count == 1  # 认证失败不重试


@respx.mock
def test_credentials_missing_fail_closed(client, monkeypatch) -> None:
    """X-API-Key 与 JWT 均未配置：fail closed，不发请求（10.5）。"""
    monkeypatch.setattr(client._settings, "erp_mcp_api_key", "")
    monkeypatch.setattr(client._settings, "erp_mcp_jwt", "")
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(ATTENDANCE_CREATE_SUCCESS))
    )
    result = client.call_tool("create", {"entity": "attendance", "data": {"employee_id": 1}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CREDENTIALS_NOT_CONFIGURED"
    assert route.call_count == 0  # 未发出任何请求


def test_bearer_fallback_when_api_key_missing(client, monkeypatch) -> None:
    """X-API-Key 缺失时回退 Authorization: Bearer <JWT>（mcp-api.md §1）。"""
    import administrative_agent.mcp.auth as mcp_auth

    monkeypatch.setattr(client._settings, "erp_mcp_api_key", "")
    monkeypatch.setattr(client._settings, "erp_mcp_jwt", "test-jwt-token-not-real")
    headers = mcp_auth.build_auth_headers(client._settings)
    assert "X-API-Key" not in headers
    assert headers["Authorization"] == "Bearer test-jwt-token-not-real"


def test_credentials_never_leak_in_error_output(client, monkeypatch) -> None:
    """凭据不出现在异常字符串（14.4：认证信息不泄露）。"""
    monkeypatch.setattr(client._settings, "erp_mcp_api_key", "")
    monkeypatch.setattr(client._settings, "erp_mcp_jwt", "")
    result = client.call_tool("create", {"entity": "attendance", "data": {"employee_id": 1}})
    assert result.error is not None
    assert TEST_API_KEY not in str(result.error)
    assert "test-jwt" not in str(result.error)


# ---- D12 补充：载荷传递 / operation_id 复用 / 结构化错误（RV-17 契约） ----


def _tool_call_args(route) -> dict:
    """提取最近一次 tools/call 请求的 arguments。"""
    call = next(
        r for r in route.calls
        if json.loads(r.request.content).get("method") == "tools/call"
    )
    return json.loads(call.request.content)["params"]["arguments"]


@respx.mock
def test_create_payload_preserves_leave_and_overtime(client) -> None:
    """考勤 create 载荷原样传递：leave_minutes/overtime_minutes 进请求
    且系统字段/operation_id 由客户端注入（mcp-api.md §5）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(ATTENDANCE_CREATE_SUCCESS))
    )
    result = client.call_tool(
        "create",
        {
            "entity": "attendance",
            "data": {
                "employee_id": 1,
                "att_date": "2026-08-19",
                "leave_minutes": 120,
                "overtime_minutes": 180,
            },
        },
        operation_id="op-att-leave-1",
        trace_id="trace-att-leave-1",
    )
    assert result.ok
    args = _tool_call_args(route)
    data = args["data"]
    assert data["employee_id"] == 1
    assert data["att_date"] == "2026-08-19"
    assert data["leave_minutes"] == 120
    assert data["overtime_minutes"] == 180
    # 写工具注入 operation_id/trace_id（§5），与传入值一致
    assert args["operation_id"] == "op-att-leave-1"
    assert args["trace_id"] == "trace-att-leave-1"


@respx.mock
def test_same_operation_id_reused_on_retry(client) -> None:
    """4.4/§5：同一业务操作重试复用 operation_id（客户端 setdefault 不覆盖）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(ATTENDANCE_CREATE_SUCCESS))
    )
    for _ in range(2):
        client.call_tool(
            "create",
            {"entity": "attendance", "data": {"employee_id": 2, "att_date": "2026-08-19"}},
            operation_id="op-att-stable-2",
            trace_id="trace-stable-2",
        )
    # initialize 握手不注入；两次 create 均携带同一 operation_id
    tool_ids = [_tool_call_args(route)["operation_id"] for _ in range(2)]
    assert tool_ids == ["op-att-stable-2", "op-att-stable-2"]
    assert len(route.calls) == 3  # initialize + 两次 create


@respx.mock
def test_structured_error_classified_and_not_retried(client) -> None:
    """mcp-api.md §8：结构化错误只读 code 归类（BUSINESS_ERROR → VALIDATION，
    不重试）；message 不参与重试判断。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200, json=structured_error("BUSINESS_ERROR", "erp create rejected")
        )
    )
    result = client.call_tool(
        "create",
        {"entity": "attendance", "data": {"employee_id": 3, "att_date": "2026-08-19"}},
        operation_id="op-att-3",
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "BUSINESS_ERROR"
    assert result.error.category == ErrorCategory.VALIDATION
    assert result.error.retryable is False  # 13.1：不自动重试
    assert route.call_count == 2  # initialize + 一次 create（不重试）


@respx.mock
def test_update_payload_only_changed_fields_with_id(client) -> None:
    """考勤 update：id 定位 + 只传变更字段（增量口径，D12 更新分支契约）。"""
    updated = {
        "status": "updated",
        "record": {"id": 100, "employee_id": 1, "att_date": "2026-08-19",
                   "leave_minutes": 180},
        "operation_id": "op-att-upd-1",
        "trace_id": "trace-upd-1",
    }
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(updated))
    )
    result = client.call_tool(
        "update",
        {"entity": "attendance", "id": 100, "data": {"leave_minutes": 180}},
        operation_id="op-att-upd-1",
        trace_id="trace-upd-1",
    )
    assert result.ok
    assert result.data["record"]["id"] == 100
    args = _tool_call_args(route)
    assert args["id"] == 100
    assert args["data"] == {"leave_minutes": 180}  # 只含变更字段
    assert args["operation_id"] == "op-att-upd-1"
