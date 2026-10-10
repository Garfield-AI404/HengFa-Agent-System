"""RV-25 契约测试：query 工具（14.2，mcp-api.md §4）。

覆盖：query 请求信封（entity/fields/filter/order_by/limit/offset 原样透传）、
响应解析（items/total/limit/offset）、分页 total 透传、空结果、错误码原样
归类（structuredContent.code → MCPError.code）、实体白名单本地拦截（不触网）、
limit 上限本地拒绝、list_entities 元数据驱动的字段校验（L1386，RV-07 已有
解析/缓存覆盖，此处补 query 前置校验链路）。

假服务为文件内快照（D5 规则：测试夹具，不代表 ERP 真实运行态）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from administrative_agent.config import Settings
from administrative_agent.mcp.client import MCPClient
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES
from tests.contract.conftest import (
    ERP_MCP_URL,
    call_tool_result,
    jsonrpc_result,
    structured_error,
)

# ---- mcp-api.md §4 响应快照 ----

QUERY_PAGE_1 = {
    "items": [
        {"id": 1, "model": "HF-001", "status": "完成", "total_amount": "3875.00"},
        {"id": 2, "model": "HF-002", "status": "进行中", "total_amount": "1250.00"},
        {"id": 3, "model": "HF-003", "status": "完成", "total_amount": "900.00"},
    ],
    "total": 3,
    "limit": 50,
    "offset": 0,
}

QUERY_PAGE_2 = {
    "items": [
        {"id": 4, "model": "HF-004", "status": "完成", "total_amount": "2000.00"},
        {"id": 5, "model": "HF-005", "status": "完成", "total_amount": "3100.00"},
    ],
    "total": 5,
    "limit": 3,
    "offset": 3,
}

QUERY_EMPTY = {"items": [], "total": 0, "limit": 50, "offset": 0}


class QueryContractErp:
    """query 契约假服务：按 offset 返回固定快照页；支持错误注入与空结果模式。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next: list[dict] = []
        self.empty_mode = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json=jsonrpc_result(
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "contract-query-erp", "version": "0.0.0"},
                    }
                ),
            )
        params = body["params"]
        args = params.get("arguments") or {}
        self.calls.append(args)
        if self.fail_next:
            return httpx.Response(
                200, json=structured_error(**self.fail_next.pop(0))
            )
        if self.empty_mode:
            return httpx.Response(200, json=call_tool_result(QUERY_EMPTY))
        offset = args.get("offset", 0)
        if offset == 0:
            return httpx.Response(200, json=call_tool_result(QUERY_PAGE_1))
        return httpx.Response(200, json=call_tool_result(QUERY_PAGE_2))


@pytest.fixture
def erp() -> QueryContractErp:
    return QueryContractErp()


@pytest.fixture
def client(erp: QueryContractErp) -> MCPClient:
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        c = MCPClient(Settings())
        yield c
        c.close()


# ---- 请求信封与响应解析（mcp-api.md §4） ----


def test_query_request_envelope_verbatim(client: MCPClient, erp: QueryContractErp):
    """entity/fields/filter/order_by/limit/offset 原样透传（9.2 第 6 条：
    filter/order_by 不解析不 eval，字段名只与元数据比对）。"""
    result = client.call_tool(
        "query",
        {
            "entity": "metal_orders",
            "fields": ["id", "model", "total_amount"],
            "filter": {"status": "完成", "total_amount": {"gte": "100"}},
            "order_by": "-id",
            "limit": 50,
            "offset": 0,
        },
        operation_id="op-contract-query-1",
        trace_id="trace-contract-query-1",
    )
    assert result.ok
    data = result.data
    assert data["items"] == QUERY_PAGE_1["items"]
    assert data["total"] == 3
    assert data["limit"] == 50
    assert data["offset"] == 0
    sent = erp.calls[-1]
    assert sent == {
        "entity": "metal_orders",
        "fields": ["id", "model", "total_amount"],
        "filter": {"status": "完成", "total_amount": {"gte": "100"}},
        "order_by": "-id",
        "limit": 50,
        "offset": 0,
    }


def test_query_pagination_total_preserved_across_pages(client: MCPClient, erp: QueryContractErp):
    """分页：每页 total 为过滤后全量（不随 offset 变化），请求 offset/limit 透传。"""
    page1 = client.call_tool(
        "query", {"entity": "metal_orders", "limit": 3, "offset": 0},
        operation_id="op-contract-query-2", trace_id="trace-contract-query-2",
    )
    page2 = client.call_tool(
        "query", {"entity": "metal_orders", "limit": 3, "offset": 3},
        operation_id="op-contract-query-2", trace_id="trace-contract-query-2",
    )
    assert page1.ok and page2.ok
    assert page1.data["total"] == 3 and page1.data["offset"] == 0
    assert page2.data["total"] == 5 and page2.data["offset"] == 3
    assert erp.calls[-2]["offset"] == 0
    assert erp.calls[-1]["offset"] == 3
    assert erp.calls[-1]["limit"] == 3


def test_query_empty_result_wire(client: MCPClient, erp: QueryContractErp):
    """空结果：items=[] 且 total=0（6.1 第 4 条：空结果明确表达）。"""
    erp.empty_mode = True
    result = client.call_tool(
        "query", {"entity": "customers", "filter": {"region": "西北"}},
        operation_id="op-contract-query-3", trace_id="trace-contract-query-3",
    )
    assert result.ok
    assert result.data["items"] == []
    assert result.data["total"] == 0


def test_query_server_error_code_preserved(client: MCPClient, erp: QueryContractErp):
    """structuredContent.code 原样转换为内部错误类型（9.2 第 5 条）。"""
    erp.fail_next.append(
        {"code": "ORDER_NOT_FOUND", "message": "order 404", "details": {"id": 1}}
    )
    result = client.call_tool(
        "query", {"entity": "metal_orders", "filter": {"id": 1}},
        operation_id="op-contract-query-4", trace_id="trace-contract-query-4",
    )
    assert not result.ok
    assert result.error.code == "ORDER_NOT_FOUND"  # 原样，不新造
    assert isinstance(result.error, MCPError)


def test_query_server_validation_error_classified(client: MCPClient, erp: QueryContractErp):
    erp.fail_next.append({"code": "VALIDATION_ERROR", "message": "bad filter", "details": {}})
    result = client.call_tool(
        "query", {"entity": "metal_orders", "filter": {"nope": 1}},
        operation_id="op-contract-query-5", trace_id="trace-contract-query-5",
    )
    assert not result.ok
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.category == ErrorCategory.VALIDATION


# ---- 本地白名单与参数保护（9.2 第 2/3 条，fail closed 不触网） ----


def test_agent_queryable_entities_whitelist_constant():
    """9.2 第 2 条：Agent 可查询实体白名单（精确集合，fail closed）。

    [2026-08-29] employees 开放（员工只读，mcp-api.md §4.3）。
    [2026-09-02 R7 收支写] 追加 transactions/transaction_categories——
    收支写（先查后写/分类先查后填）的信息前提。
    [2026-09-12 只读扩展 · 用户拍板] 追加 7 个只读实体：customer_contacts/
    attendance/ar_payments/ap_payments/salary_payments/powder_inventory/
    powder_consumption——**本次只开读、不开放写**，写路径白名单
    （_GENERIC_WRITE_ENTITIES / 领域工具）不变。旧注「salary_payments 不在
    开放范围」已作废。仍不可查询的实体（users/webhook_subscriptions/
    file_attachments）一并钉死，防白名单被无差别放大。
    [2026-09-16 当期应收口径开放] 追加 ar_settlements（应收平账/抹零，只读）
    ——SK.receivables 流程第 4 步 Σsettled_amount 的取数前提；ERP 侧同为此
    只读实体。写路径白名单同样不变。
    """
    assert QUERYABLE_ENTITIES == frozenset(
        {"metal_orders", "powder_orders", "customers", "suppliers", "employees",
         "transactions", "transaction_categories",
         "customer_contacts", "attendance", "ar_payments", "ap_payments",
         "salary_payments", "powder_inventory", "powder_consumption",
         "ar_settlements"}
    )
    assert len(QUERYABLE_ENTITIES) == 15
    # 新开的 7 个只读实体确实在册
    for entity in ("customer_contacts", "attendance", "ar_payments",
                   "ap_payments", "salary_payments", "powder_inventory",
                   "powder_consumption"):
        assert entity in QUERYABLE_ENTITIES
    # 反例：仍不在白名单的实体必须拒（不因本次扩展被一并放开）
    assert "users" not in QUERYABLE_ENTITIES
    assert "webhook_subscriptions" not in QUERYABLE_ENTITIES
    assert "file_attachments" not in QUERYABLE_ENTITIES
    assert "metal_orders" in QUERYABLE_ENTITIES
    assert "employees" in QUERYABLE_ENTITIES
    assert "transactions" in QUERYABLE_ENTITIES
    assert "transaction_categories" in QUERYABLE_ENTITIES


def test_query_local_validation_fails_closed_without_network(client: MCPClient, erp: QueryContractErp):
    """白名单外实体/非法 limit：本地拦截，不发出请求（fail closed，9.2 第 3 条）。"""
    erp.calls.clear()
    # limit 超出 200（mcp-api.md §4 最大 200）→ 本地 VALIDATION_ERROR
    from administrative_agent.domains.query import QuerySpec

    with pytest.raises(MCPError) as exc:
        QuerySpec(entity="metal_orders", limit=201)
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value
    assert erp.calls == []  # 未触网


def test_list_entities_drives_field_validation(client: MCPClient, erp: QueryContractErp):
    """L1386：查询字段校验以 list_entities 元数据为准（14.2；RV-07 已覆盖
    list_entities 解析/过滤，此处补 query 前置校验链路）。"""
    import httpx as _httpx  # noqa: F401
    from administrative_agent.mcp.metadata import EntityCache
    from administrative_agent.domains.query import build_query_spec, validate_query_spec

    entities_payload = [
        {
            "name": "metal_orders", "label": "钣金订单", "readable": True,
            "creatable": False, "updatable": False, "deletable": False,
            "business_operations": ["create_order_bundle", "update_order"],
            "fields": {
                "id": {"type": "integer", "label": "ID"},
                "model": {"type": "string", "label": "型号"},
                "total_amount": {"type": "string", "label": "金额"},
            },
        },
        {
            "name": "customers", "label": "客户", "readable": True,
            "creatable": False, "updatable": False, "deletable": False,
            "business_operations": [],
            "fields": {"id": {"type": "integer", "label": "ID"}, "name": {"type": "string", "label": "名称"}},
        },
    ]

    class MetaErp(QueryContractErp):
        def handler(self, request):
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    json=jsonrpc_result({"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "m", "version": "0"}}),
                )
            params = body["params"]
            if params.get("name") == "list_entities":
                return httpx.Response(200, json=call_tool_result({"entities": entities_payload}))
            self.calls.append(params.get("arguments") or {})
            return httpx.Response(200, json=call_tool_result(QUERY_PAGE_1))

    meta_erp = MetaErp()
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=meta_erp.handler)
        c = MCPClient(Settings())
        try:
            cache = EntityCache(c)
            # 元数据内字段通过
            validate_query_spec(
                build_query_spec(entity="metal_orders", fields=("id", "model")),
                cache,
                QUERYABLE_ENTITIES,
            )
            # 元数据外字段拒绝（6.1 第 2 条：查询字段必须来自 list_entities）
            with pytest.raises(MCPError) as exc:
                validate_query_spec(
                    build_query_spec(entity="metal_orders", fields=("password_hash",)),
                    cache,
                    QUERYABLE_ENTITIES,
                )
            assert exc.value.code == InternalCode.VALIDATION_ERROR.value
            assert exc.value.details["fields"] == ["password_hash"]
        finally:
            c.close()
