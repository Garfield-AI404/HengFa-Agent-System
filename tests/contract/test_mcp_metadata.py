"""RV-07 契约测试：list_entities 过滤、字段校验与缓存行为（14.2/规格 2.3）。

假服务快照（tests/contract/ 构造）模拟 mcp-api.md §3 的实体描述：按 scope
过滤后的实体集（users 对 Agent 不可见，不出现在响应中）+ attendance 通用
读写能力 + 两类订单只读与领域业务操作。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.contract.conftest import ERP_MCP_URL, call_tool_result
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.metadata import EntityCache

# ---- 假服务快照：mcp-api.md §3 风格 ----

ENTITIES_PAYLOAD = [
    {
        "name": "attendance",
        "label": "考勤",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": True,
        "read_only": False,
        "business_operations": [],
        "default_order": "-id",
        "fields": {
            "employee_id": {
                "type": "int",
                "label": "员工",
                "required": True,
                "nullable": False,
                "writable_on_create": True,
                "writable_on_update": False,
                "system_managed": False,
                "sensitive": False,
            },
            "overtime_minutes": {
                "type": "int",
                "label": "加班分钟",
                "required": False,
                "nullable": True,
                "writable_on_create": True,
                "writable_on_update": True,
                "system_managed": False,
                "sensitive": False,
            },
            "id": {
                "type": "int",
                "label": "ID",
                "required": False,
                "nullable": False,
                "writable_on_create": False,
                "writable_on_update": False,
                "system_managed": True,
                "sensitive": False,
            },
        },
    },
    {
        "name": "metal_orders",
        "label": "钣金订单",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "read_only": True,
        "business_operations": ["create_order_bundle", "update_order"],
        "default_order": "-id",
        "fields": {
            "model": {
                "type": "str",
                "label": "型号",
                "required": True,
                "nullable": False,
                "writable_on_create": False,
                "writable_on_update": False,
                "system_managed": False,
                "sensitive": False,
            },
        },
    },
    {
        "name": "powder_orders",
        "label": "塑粉订单",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "read_only": True,
        "business_operations": ["create_order_bundle", "update_order"],
        "default_order": "-id",
        "fields": {
            "color_code": {
                "type": "str",
                "label": "色号",
                "required": True,
                "nullable": False,
                "writable_on_create": False,
                "writable_on_update": False,
                "system_managed": False,
                "sensitive": False,
            },
        },
    },
]


@respx.mock
def test_list_entities_parse_and_filter(client) -> None:
    """list_entities 返回按 scope 过滤的实体集；客户端解析四能力与字段约束。

    - users 不出现在响应中（mcp-api.md §3 按 scope 过滤，Agent 不可见）；
    - 无 name 的脏条目被丢弃；
    - 四能力字段读取新客户端语义（readable/creatable/updatable/deletable）；
    - 两类订单 business_operations 与字段约束正确。
    """
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(
            200, json=call_tool_result({"entities": ENTITIES_PAYLOAD + [{"label": "脏条目"}]})
        )
    )
    cache = EntityCache(client, ttl_seconds=3600)
    entities = cache.get_entities()

    names = {e.name for e in entities}
    assert names == {"attendance", "metal_orders", "powder_orders"}  # users 不可见

    attendance = cache.get_entity("attendance")
    assert attendance is not None
    assert attendance.creatable and attendance.updatable and attendance.deletable
    assert attendance.fields["employee_id"].required
    assert attendance.fields["employee_id"].writable_on_create
    assert attendance.fields["id"].system_managed
    assert not attendance.fields["id"].writable_on_create

    metal = cache.get_entity("metal_orders")
    assert metal is not None
    assert metal.readable and not metal.creatable
    assert metal.business_operations == ("create_order_bundle", "update_order")

    # 1 次握手（initialize）+ 1 次 tools/call（list_entities）
    assert route.call_count == 2
    assert len(entities) == 3  # 脏条目被丢弃


@respx.mock
def test_cache_hit_no_second_request(client) -> None:
    """缓存命中：TTL 内第二次访问不发请求（2.3 启动时/失效后刷新）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    cache.get_entities()
    assert route.call_count == 2  # 握手 + list_entities
    cache.get_entities()
    cache.get_entity("attendance")
    cache.field_capabilities("attendance", "overtime_minutes")
    assert route.call_count == 2  # 全部命中缓存（含握手只做一次）


@respx.mock
def test_ttl_expiry_triggers_refresh(client) -> None:
    """TTL 到期后下一次访问重新请求（缓存失效策略可配置）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=0)
    cache.get_entities()
    assert route.call_count == 2  # 握手 + list_entities
    cache.get_entities()
    assert route.call_count == 3  # TTL 过期重新调用（握手不重复）


@respx.mock
def test_unloaded_cache_refreshes_when_host_uptime_below_ttl(client, monkeypatch) -> None:
    """回归（[2026-09-13]）：宿主机 uptime < TTL 时，从未加载的缓存也必须刷新。

    防的是量纲不匹配缺陷：新鲜度判据曾以 ``time.monotonic()``（= **宿主机开机
    秒数**，非进程启动时刻）与 ``_loaded_at`` 初值 ``0.0`` 作差，宿主机开机不足
    ttl（默认 3600s）时 ``now - 0.0 < ttl`` 恒真，``_ensure_fresh`` 在前置判断
    处直接 return → 缓存永不加载 → 实体白名单 / 写守卫等依赖元数据的路径全线
    fail closed，且**重启进程无效**（须等开机满 1 小时才自愈）。

    这里把 ``time.monotonic`` 钉在 10.0（模拟「宿主机开机仅 10 秒」），断言缓存
    真的发起 list_entities（握手 + tools/call 共 2 次）并拿到全部实体；旧代码下
    此处 route.call_count 会是 0（连 list_entities 都没发出）、实体为空。
    """
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    monkeypatch.setattr("administrative_agent.mcp.metadata.time.monotonic", lambda: 10.0)

    cache = EntityCache(client, ttl_seconds=3600)
    entities = cache.get_entities()

    assert {e.name for e in entities} == {"attendance", "metal_orders", "powder_orders"}
    assert route.call_count == 2  # 握手 + list_entities；旧代码为 0（缓存被永久跳过）


@respx.mock
def test_refresh_failure_keeps_stale_cache(client) -> None:
    """刷新失败且已有旧缓存：返回旧数据并标记 stale（13.4 降级语义）。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    cache.get_entities()
    assert not cache.is_stale()

    route.mock(return_value=httpx.Response(500, json={"jsonrpc": "2.0", "id": 1, "result": {}}))
    cache.refresh()
    assert cache.is_stale()
    entities = cache.get_entities()  # 读仍可用
    assert {e.name for e in entities} == {"attendance", "metal_orders", "powder_orders"}


@respx.mock
def test_refresh_failure_no_cache_raises(client) -> None:
    """刷新失败且无旧缓存：fail closed，抛 MCPError（13.4 不允许写入）。"""
    respx.post(ERP_MCP_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    cache = EntityCache(client, ttl_seconds=3600)
    with pytest.raises(MCPError) as exc_info:
        cache.get_entities()
    assert exc_info.value.code == "MCP_AUTH_FAILED"
    assert exc_info.value.retryable is False


@respx.mock
def test_field_capabilities_for_upper_layer(client) -> None:
    """字段能力查询供上层校验（任务书交付物 4）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    spec = cache.field_capabilities("attendance", "employee_id")
    assert spec is not None
    assert spec.writable_on_create is True
    assert spec.writable_on_update is False
    assert cache.field_capabilities("attendance", "no_such_field") is None
    assert cache.field_capabilities("no_such_entity", "employee_id") is None


@respx.mock
def test_empty_entity_list_ok(client) -> None:
    """scope 收紧后实体集可为空（2.4 最小 scope），缓存正常但无实体。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": []}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    assert cache.get_entities() == []
    assert cache.get_entity("attendance") is None


# ---- 决策项 a：stale 元数据拒绝写入（RV-07 reviewer 口径复核落地，D6） ----

@respx.mock
def test_stale_metadata_rejects_write(client) -> None:
    """陈旧元数据拒绝写入：刷新失败（stale）后 ensure_write_allowed 抛错。"""
    route = respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    cache.get_entities()
    assert not cache.is_stale()

    route.mock(return_value=httpx.Response(500, json={"jsonrpc": "2.0", "id": 1, "result": {}}))
    cache.refresh()
    assert cache.is_stale()

    with pytest.raises(MCPError) as exc_info:
        cache.ensure_write_allowed("attendance")
    assert exc_info.value.code == "MCP_METADATA_STALE"
    assert exc_info.value.category == ErrorCategory.LOCAL
    assert exc_info.value.retryable is False


@respx.mock
def test_unknown_entity_rejects_write(client) -> None:
    """实体不在元数据内（scope 外/未知）时拒绝写入（fail closed）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    cache.get_entities()
    with pytest.raises(MCPError) as exc_info:
        cache.ensure_write_allowed("no_such_entity")
    assert exc_info.value.code == "MCP_METADATA_UNKNOWN_ENTITY"
    assert exc_info.value.details.get("entity") == "no_such_entity"


@respx.mock
def test_ensure_write_allowed_passes_for_known_entity(client) -> None:
    """元数据正常且实体可见时守卫放行（能力级校验属域适配器 D12+）。"""
    respx.post(ERP_MCP_URL).mock(
        return_value=httpx.Response(200, json=call_tool_result({"entities": ENTITIES_PAYLOAD}))
    )
    cache = EntityCache(client, ttl_seconds=3600)
    entity = cache.ensure_write_allowed("attendance")
    assert entity.name == "attendance"
    assert cache.ensure_write_allowed("metal_orders").name == "metal_orders"
