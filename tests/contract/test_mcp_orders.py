"""RV-07/RV-20 契约测试：订单工具（14.2）。

覆盖 create_order_bundle 成功/replayed/key conflict/in progress、
update_order 金额重算与 expected_updated_at 版本冲突、通用 CRUD 的订单
本地拒绝、系统字段与 total_amount 禁止传入、结构化错误码归类；
D14（RV-20）追加：idempotency_key 写工具透传（§6.1/§5）、同 key 同
payload 重放返回原订单（§6.3）。

本地拒绝路径（scopes.py）不发请求；服务端拒绝路径（假服务返回
structuredContent）走 errors.py 归类。
注：并发同 key 的 InnoDB 锁等待（§6.3 L269）无法用 SQLite/内存假服务
模拟，并发幂等测试仅限非 SQLite 真实库（见 RV-20 材料 §说明）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.contract.conftest import ERP_MCP_URL, call_tool_result, structured_error
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.scopes import verify_generic_write, verify_tool


def pytest_raises_order(code: str):
    return pytest.raises(MCPError, match=code)

# ---- mcp-api.md §6.2 成功响应（假服务快照） ----

CREATE_SUCCESS = {
    "status": "created",
    "replayed": False,
    "operation_id": "agent-operation-0001",
    "trace_id": "agent-trace-0001",
    "order_type": "metal",
    "order": {
        "id": 10,
        "customer_id": 1,
        "model": "HF-001",
        "surcharge_amount": "0.00",
        "total_amount": "3875.00",
    },
    "receivables": [],
    "payables": [],
    "transactions": [],
    "other_effects": [
        {
            "type": "receivable_current_impact",
            "mode": "derived",
            "customer_id": 1,
            "amount": "3875.00",
        }
    ],
}

# ---- mcp-api.md §7.4 成功响应（假服务快照） ----

UPDATE_SUCCESS = {
    "status": "updated",
    "order_type": "metal",
    "order": {
        "id": 10,
        "customer_id": 1,
        "model": "HF-001",
        "weight_per_unit": "2.7500",
        "unit_price": "15.5000",
        "quantity": "100.00",
        "surcharge_amount": "0.00",
        "total_amount": "4262.50",
        "status": "完成",
        "out_date": "2026-08-20",
        "updated_by": 8,
        "updated_at": "2026-08-20T10:20:00.123456",
    },
    "changed_fields": ["out_date", "status", "total_amount", "unit_price", "weight_per_unit"],
    "other_effects": [
        {"type": "receivable_current_impact", "mode": "derived", "customer_id": 1, "amount": "4262.50"}
    ],
    "operation_id": "order-update-10",
    "trace_id": "agent-trace-0001",
}


# ---- create_order_bundle ----

@respx.mock
def test_create_order_bundle_success(client) -> None:
    """create_order_bundle 成功：解析 created 与订单摘要。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(CREATE_SUCCESS))
    )
    result = client.call_tool("create_order_bundle", {"order": {"model": "HF-001"}})
    assert result.ok
    assert result.data["status"] == "created"
    assert result.data["replayed"] is False
    assert result.data["order"]["id"] == 10
    assert result.data["other_effects"][0]["amount"] == "3875.00"


@respx.mock
def test_create_order_bundle_replayed(client) -> None:
    """幂等重放：相同 key 相同 payload 返回 replayed=true（mcp-api.md §6.3）。"""
    payload = dict(CREATE_SUCCESS)
    payload["status"] = "created"
    payload["replayed"] = True
    respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(200, json=call_tool_result(payload)))
    result = client.call_tool("create_order_bundle", {"order": {"model": "HF-001"}})
    assert result.ok
    assert result.data["replayed"] is True


@respx.mock
def test_idempotency_key_conflict_mapping(client) -> None:
    """相同 key 不同 payload → IDEMPOTENCY_KEY_CONFLICT（13.1 保留原 key 状态）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json=structured_error(
                "IDEMPOTENCY_KEY_CONFLICT",
                "幂等键已用于不同请求",
                details={"idempotency_key": "agent-job-20260819-0001"},
            ),
        )
    )
    result = client.call_tool("create_order_bundle", {"order": {"model": "HF-001"}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert result.error.category == ErrorCategory.IDEMPOTENCY
    assert result.error.retryable is False
    assert result.error.details["idempotency_key"] == "agent-job-20260819-0001"


@respx.mock
def test_idempotency_in_progress_requires_operator(client) -> None:
    """异常遗留 PENDING → IDEMPOTENCY_IN_PROGRESS：retryable=false 且
    requires_operator_action=true（13.1：不得更换 key 绕过）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json=structured_error(
                "IDEMPOTENCY_IN_PROGRESS",
                "相同幂等键的请求正在处理中",
                requires_operator_action=True,
            ),
        )
    )
    result = client.call_tool("create_order_bundle", {"order": {"model": "HF-001"}})
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "IDEMPOTENCY_IN_PROGRESS"
    assert result.error.retryable is False
    assert result.error.requires_operator_action is True


# ---- update_order ----

@respx.mock
def test_update_order_amount_recalc_response(client) -> None:
    """update_order 成功：金额重算后的订单与 changed_fields（mcp-api.md §7.4）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(UPDATE_SUCCESS))
    )
    result = client.call_tool(
        "update_order",
        {
            "order_type": "metal",
            "order_id": 10,
            "changes": {"weight_per_unit": "2.7500", "unit_price": "15.5000"},
            "expected_updated_at": "2026-08-20T10:15:30.123456",
        },
    )
    assert result.ok
    assert result.data["order"]["total_amount"] == "4262.50"
    assert "total_amount" in result.data["changed_fields"]
    assert result.data["order"]["status"] == "完成"


@respx.mock
def test_update_order_version_conflict(client) -> None:
    """expected_updated_at 过期 → ORDER_VERSION_CONFLICT（13.1 重新 query 后重算）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json=structured_error("ORDER_VERSION_CONFLICT", "订单版本已更新"),
        )
    )
    result = client.call_tool(
        "update_order",
        {
            "order_type": "metal",
            "order_id": 10,
            "changes": {"unit_price": "16.0000"},
            "expected_updated_at": "2026-08-20T10:15:30.123456",
        },
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ORDER_VERSION_CONFLICT"
    assert result.error.category == ErrorCategory.VERSION_CONFLICT
    assert result.error.retryable is False


# ---- D16：update_order 白名单与参数保护（mcp-api.md §7.1，fail closed） ----

def test_update_order_whitelist_unknown_fields_rejected() -> None:
    """白名单外字段（未知或不适用于该订单类型）→ VALIDATION_ERROR。"""
    from administrative_agent.mcp.scopes import verify_update_order_arguments

    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "metal", "order_id": 10,
            "changes": {"color_code": "R-01"},  # 塑粉字段不适用于钣金
        })
    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "powder", "order_id": 20,
            "changes": {"weight_per_unit": "2.7500"},  # 钣金字段不适用于塑粉
        })
    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "metal", "order_id": 10,
            "changes": {"magic_field": 1},  # 未知字段
        })


def test_update_order_requires_valid_order_type_and_changes() -> None:
    """order_type 只接受 metal/powder；changes 必填且非空（§7.1）。"""
    from administrative_agent.mcp.scopes import verify_update_order_arguments

    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "foo", "order_id": 10, "changes": {"notes": "x"},
        })
    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "metal", "order_id": 10, "changes": {},
        })
    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "metal", "order_id": 10,
        })


def test_update_order_all_whitelist_fields_accepted() -> None:
    """两个订单类型的完整白名单字段全部通过（§7.1 表逐字段）。"""
    from administrative_agent.mcp.scopes import verify_update_order_arguments

    verify_update_order_arguments({
        "order_type": "metal", "order_id": 10,
        "changes": {
            "customer_id": 1, "model": "HF-001", "unit_price": "15.5000",
            "price_type": "含税", "status": "未完成",
            "weight_per_unit": "2.7500", "weight_unit": "kg",
            "quantity": "100.00", "quantity_unit": "套",
            "surcharge_amount": "0.00",
            "in_date": "2026-08-20", "out_date": None, "notes": None,
        },
    })
    verify_update_order_arguments({
        "order_type": "powder", "order_id": 20,
        "changes": {
            "supplier_id": 101, "color_code": "R-01",
            "total_weight": "50.00", "price_per_kg": "28.5000",
            "in_date": "2026-08-20", "notes": None,
        },
    })


@respx.mock
def test_update_order_expected_updated_at_passthrough(client) -> None:
    """wire 级：expected_updated_at 原样回传（规则 4）；operation_id/trace_id
    由客户端注入（mcp-api.md §5/§7.1）。"""
    calls: list[dict] = []

    def route_handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=call_tool_result(UPDATE_SUCCESS))

    respx.post(ERP_MCP_URL).mock(side_effect=route_handler)
    result = client.call_tool(
        "update_order",
        {
            "order_type": "metal",
            "order_id": 10,
            "changes": {"unit_price": "16.0000"},
            "expected_updated_at": "2026-08-20T10:15:30.123456",
        },
        operation_id="order-update-10",
        trace_id="agent-trace-0001",
    )
    assert result.ok
    tool_call = [c for c in calls if c.get("method") == "tools/call"][-1]
    sent = tool_call["params"]["arguments"]
    assert sent["expected_updated_at"] == "2026-08-20T10:15:30.123456"
    assert sent["operation_id"] == "order-update-10"
    assert sent["trace_id"] == "agent-trace-0001"
    # changes 不含 id/amount/total_amount 等系统字段
    assert set(sent["changes"]) == {"unit_price"}


@respx.mock
def test_update_order_powder_payable_impact_response(client) -> None:
    """wire 级塑粉更新：payable_current_impact 与金额重算响应可解析（§7.4）。"""
    powder_success = {
        "status": "updated",
        "order_type": "powder",
        "order": {
            "id": 20,
            "supplier_id": 101,
            "color_code": "R-01",
            "total_weight": "50.00",
            "price_per_kg": "30.0000",
            "total_amount": "1500.00",
            "updated_at": "2026-08-20T10:20:00.123456",
        },
        "changed_fields": ["total_amount", "price_per_kg"],
        "other_effects": [
            {"type": "payable_current_impact", "mode": "derived",
             "supplier_id": 101, "amount": "1500.00"}
        ],
    }
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(powder_success))
    )
    result = client.call_tool(
        "update_order",
        {
            "order_type": "powder",
            "order_id": 20,
            "changes": {"price_per_kg": "30.0000"},
            "expected_updated_at": "2026-08-20T10:15:30.123456",
        },
    )
    assert result.ok
    assert result.data["order"]["total_amount"] == "1500.00"
    assert result.data["other_effects"][0]["type"] == "payable_current_impact"


# ---- 通用 CRUD 的订单本地拒绝（mcp-api.md §2 代码层禁止） ----

def test_generic_create_order_rejected_locally() -> None:
    """通用 create 写订单 → ORDER_CREATE_REQUIRES_BUNDLE，且不发出请求。"""
    with pytest_raises_order("ORDER_CREATE_REQUIRES_BUNDLE"):
        verify_generic_write("create", "metal_orders", {"model": "HF-001"})
    with pytest_raises_order("ORDER_CREATE_REQUIRES_BUNDLE"):
        verify_generic_write("create", "powder_orders", {"color_code": "R-01"})


def test_generic_update_order_rejected_locally() -> None:
    """通用 update 写订单 → ORDER_UPDATE_REQUIRES_ORDER_TOOL。"""
    with pytest_raises_order("ORDER_UPDATE_REQUIRES_ORDER_TOOL"):
        verify_generic_write("update", "metal_orders", {"model": "HF-002"})


def test_generic_delete_order_rejected_locally() -> None:
    """delete 不挂载 → NOT_WHITELISTED（2.3）；订单删除另有 ORDER_DELETE_NOT_ALLOWED 语义。"""
    with pytest.raises(MCPError) as exc_info:
        verify_tool("delete")
    assert exc_info.value.code == "NOT_WHITELISTED"


@respx.mock
def test_server_side_order_crud_rejection_mapped(client) -> None:
    """服务端拒绝（假服务返回）→ errors.py 归类：三条订单 CRUD 硬限制。"""
    cases = [
        ("create", "ORDER_CREATE_REQUIRES_BUNDLE"),
        ("update", "ORDER_UPDATE_REQUIRES_ORDER_TOOL"),
        ("delete", "ORDER_DELETE_NOT_ALLOWED"),
    ]
    for tool, code in cases:
        respx.post(ERP_MCP_URL).mock(
            return_value=httpx.Response(200, json=structured_error(code, details={"entity": "metal_orders"}))
        )
        result = client.call_tool(tool, {"entity": "metal_orders", "data": {}})
        assert not result.ok
        assert result.error is not None
        assert result.error.code == code
        assert result.error.category == ErrorCategory.VALIDATION
        assert result.error.retryable is False


# ---- 禁止传入系统字段和 total_amount（mcp-api.md §6.1/§7.1） ----

def test_system_fields_and_total_amount_rejected() -> None:
    """create_order_bundle.order / update_order.changes 禁止系统字段与派生金额。"""
    from administrative_agent.mcp.scopes import (
        verify_order_bundle_arguments,
        verify_update_order_arguments,
    )

    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_order_bundle_arguments({"order": {"model": "HF-001", "total_amount": "100.00"}})
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_order_bundle_arguments({"order": {"model": "HF-001", "amount": "100.00"}})
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_order_bundle_arguments({"order": {"model": "HF-001", "id": 1}})
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_order_bundle_arguments({"order": {"model": "HF-001", "created_by": 3}})
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_update_order_arguments({"changes": {"unit_price": "16.0000", "total_amount": "9999"}})
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_update_order_arguments({"changes": {"updated_at": "2026-08-21T00:00:00"}})


# ---- [2026-09-08 ERP 附加款上线] surcharge_amount 契约（mcp-api.md §6.1/§7.1） ----


def test_surcharge_allowed_for_metal_forbidden_for_powder() -> None:
    """附加款（surcharge_amount）是钣金可写业务字段、塑粉无此字段。

    - create_order_bundle.order：surcharge_amount 非系统字段/非派生金额
      （派生的是行总额 total_amount，仍拒）→ 放行；
    - update_order.changes：metal 白名单已含 surcharge_amount → 放行；
      powder 白名单无此字段 → VALIDATION_ERROR（不适用于该订单类型）。
    """
    from administrative_agent.mcp.scopes import (
        verify_order_bundle_arguments,
        verify_update_order_arguments,
    )

    verify_order_bundle_arguments({
        "order": {"customer_id": 1, "model": "HF-001",
                  "surcharge_amount": "12.50", "unit_price": "15.5000"},
    })
    verify_update_order_arguments({
        "order_type": "metal", "order_id": 10,
        "changes": {"surcharge_amount": "12.50"},
    })
    verify_update_order_arguments({
        "order_type": "metal", "order_id": 10,
        "changes": {"surcharge_amount": "0.00"},  # 0 = 清除附加款
    })
    with pytest_raises_order("VALIDATION_ERROR"):
        verify_update_order_arguments({
            "order_type": "powder", "order_id": 20,
            "changes": {"surcharge_amount": "1.00"},
        })
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        # 行总额仍是派生金额，即使带附加款也不可伪造
        verify_order_bundle_arguments({
            "order": {"customer_id": 1, "surcharge_amount": "12.50",
                      "total_amount": "3887.50"},
        })


@respx.mock
def test_create_order_surcharge_passthrough_wire(client) -> None:
    """wire 级：带附加款的钣金创建载荷原样透传（surcharge_amount 不被
    本地拦截剥离或改写）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(CREATE_SUCCESS))
    )
    result = client.call_tool(
        "create_order_bundle",
        {"order": {"customer_id": 1, "model": "HF-001", "surcharge_amount": "88.80"},
         "idempotency_key": "agent-job-surcharge-0001"},
    )
    assert result.ok
    tool_calls = [
        json.loads(c.request.content)
        for c in route.calls
        if json.loads(c.request.content).get("method") == "tools/call"
    ]
    sent_order = tool_calls[-1]["params"]["arguments"]["order"]
    assert sent_order["surcharge_amount"] == "88.80"
    assert "total_amount" not in sent_order  # 派生金额仍恒禁


@respx.mock
def test_update_order_surcharge_passthrough_wire(client) -> None:
    """wire 级：附加款进入 update_order.changes 原样透传（§7.4 响应金额
    由 ERP 重算：基础金额 + 附加款）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(UPDATE_SUCCESS))
    )
    result = client.call_tool(
        "update_order",
        {
            "order_type": "metal",
            "order_id": 10,
            "changes": {"surcharge_amount": "88.80"},
            "expected_updated_at": "2026-08-20T10:15:30.123456",
        },
    )
    assert result.ok
    tool_calls = [
        json.loads(c.request.content)
        for c in route.calls
        if json.loads(c.request.content).get("method") == "tools/call"
    ]
    sent = tool_calls[-1]["params"]["arguments"]
    assert sent["changes"] == {"surcharge_amount": "88.80"}
    assert result.data["order"]["surcharge_amount"] == "0.00"  # 响应快照含附加款


def test_writable_on_update_false_fields_rejected_in_update_chain() -> None:
    """字段约束执行链路（writable_on_update=False → rejected，14.2 第 2 项）：
    update_order.changes 与通用 update（attendance）中系统管理字段一律
    SYSTEM_FIELD_FORBIDDEN（id/created_at/created_by/updated_at/
    updated_by/deleted_at），派生金额同拒；白名单未知字段 →
    VALIDATION_ERROR。"""
    from administrative_agent.mcp.scopes import (
        verify_generic_write,
        verify_update_order_arguments,
    )

    # update_order.changes：系统管理字段（writable_on_update=False）全部拒绝
    for field in ("id", "created_at", "created_by", "updated_at",
                  "updated_by", "deleted_at"):
        with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
            verify_update_order_arguments({"changes": {field: "2026-08-21T00:00:00"}})
    # 通用 update（attendance）：系统管理字段同拒（实体元数据 writable 语义）
    with pytest_raises_order("SYSTEM_FIELD_FORBIDDEN"):
        verify_generic_write("update", "attendance", {"id": 1})
    # 合法字段放行（不误伤）：白名单内字段 + 合法 order_type / 合法通用字段通过
    verify_update_order_arguments(
        {"order_type": "metal", "changes": {"weight_per_unit": "2.7500"}}
    )
    verify_generic_write(
        "update", "attendance", {"employee_id": 2, "att_date": "2026-08-21"}
    )


# ---- 写工具 operation_id/trace_id 注入（mcp-api.md §5） ----

@respx.mock
def test_write_tool_injects_operation_and_trace(client) -> None:
    """写工具自动注入 operation_id/trace_id；只读工具不注入（§5 契约）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(CREATE_SUCCESS))
    )
    client.call_tool("create_order_bundle", {"order": {"model": "HF-001"}})
    # calls[0] 为 initialize 握手；tools/call 请求需按 method 筛选
    tool_calls = [
        json.loads(c.request.content)
        for c in route.calls
        if json.loads(c.request.content).get("method") == "tools/call"
    ]
    assert len(tool_calls) == 1
    sent = tool_calls[0]
    assert sent["params"]["name"] == "create_order_bundle"
    args = sent["params"]["arguments"]
    assert args["operation_id"].startswith("op-")
    assert args["trace_id"].startswith("trace-")

    route.mock(return_value=httpx.Response(200, json=call_tool_result({"items": []})))
    client.call_tool("query", {"entity": "metal_orders"})
    query_calls = [
        json.loads(c.request.content)
        for c in route.calls
        if json.loads(c.request.content).get("method") == "tools/call"
    ]
    query_args = query_calls[-1]["params"]["arguments"]
    assert "operation_id" not in query_args
    assert "trace_id" not in query_args


# ---- D14（RV-20）：幂等键 wire 级契约（mcp-api.md §6.1/§6.3） ----

@respx.mock
def test_write_tool_passes_idempotency_key_through(client) -> None:
    """调用方显式传 idempotency_key → 原样透传，不被注入逻辑覆盖（§6.1）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result(CREATE_SUCCESS))
    )
    client.call_tool(
        "create_order_bundle",
        {"order": {"model": "HF-001"}, "idempotency_key": "agent-job-0001abcd"},
    )
    tool_calls = [
        json.loads(c.request.content)
        for c in route.calls
        if json.loads(c.request.content).get("method") == "tools/call"
    ]
    args = tool_calls[0]["params"]["arguments"]
    assert args["idempotency_key"] == "agent-job-0001abcd"
    assert args["operation_id"].startswith("op-")   # 注入项与幂等键互不干扰
    assert args["trace_id"].startswith("trace-")


@respx.mock
def test_create_order_bundle_same_key_same_payload_replays_original(client) -> None:
    """同 key 同 payload 重试 → replayed=true 且返回原订单（§6.3，不新建）。

    两段式假服务：首调 created（id=10）；同 key 同载荷二调 → 原订单 + replayed。
    """
    calls = []
    seen: set[str] = set()

    def route_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") != "tools/call":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "result": {"capabilities": {"tools": {}},
                                      "serverInfo": {"name": "x", "version": "0"}}}
            )
        calls.append(json.loads(request.content))
        resp = dict(CREATE_SUCCESS)
        if body["params"]["arguments"].get("idempotency_key") in seen:
            resp["replayed"] = True  # 同 key → 原订单重放（§6.3）
        else:
            seen.add(body["params"]["arguments"]["idempotency_key"])
        return httpx.Response(200, json=call_tool_result(resp))

    respx.post(ERP_MCP_URL).mock(side_effect=route_handler)
    args = {"order": {"model": "HF-001"}, "idempotency_key": "agent-job-0002"}
    first = client.call_tool("create_order_bundle", args)
    second = client.call_tool("create_order_bundle", args)

    assert first.ok and first.data["replayed"] is False
    assert first.data["order"]["id"] == 10
    assert second.ok and second.data["replayed"] is True
    assert second.data["order"]["id"] == 10  # 原订单，未新建
    # 两次请求的幂等键一致（重试复用，非换 key）
    assert calls[0]["params"]["arguments"]["idempotency_key"] == "agent-job-0002"
    assert calls[1]["params"]["arguments"]["idempotency_key"] == "agent-job-0002"
