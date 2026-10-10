"""list_entities（ERP 能力目录直挂）测试（[2026-09-09 架构改造第一步]）。

覆盖：两级裁剪（紧凑目录 vs 单实体完整细节原样透传）、实时调用不读缓存、
权限 fail closed、ERP 失败/返回不可解析/实体拼写错 fail closed、items
信封标记（entity="catalog"，位于 crop 白名单外，条目原样进上下文）等。

假 ERP 形态对齐 tests/api/conftest.FakeMCPClient 与 test_scenarios_14_3
的 PagedQueryErp（D5：测试夹具，不代表 ERP 真实运行态）。
"""

from __future__ import annotations

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.catalog import (
    CATALOG_ENTITY_MARKER,
    ListEntitiesArgs,
    _compact_entity,
    _compact_field,
    run_list_entities,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError


#: 假 ERP 目录：三个实体——两个可查 + 一个目录可见但不在查询白名单
#: （queryable 外的能力直挂差异由 note 说明）。字段 raw 结构与 mcp-api.md
#: §3 一致（sensitive/description/default 等键在裁剪前后应存亡各异）。
_CATALOG_ENTITIES: list[dict] = [
    {
        "name": "metal_orders",
        "label": "钣金订单",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "description": "钣金类订单主档",
        "fields": {
            "id": {"type": "int", "label": "ID", "system_managed": True},
            "model": {
                "type": "string", "label": "型号", "required": True,
                "description": "订单型号", "sensitive": False,
            },
            "weight_unit": {
                "type": "str", "label": "重量单位", "required": True,
                "enum_values": ["kg", "m²"], "default": "kg",
                "description": "计重单位", "sensitive": False,
            },
            "weight_per_unit": {
                "type": "decimal", "label": "单重", "required": False,
                "min_value": 0, "description": "单件重量（0 表示待补充）",
            },
        },
    },
    {
        "name": "customers",
        "label": "客户",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "company_name": {"type": "string", "label": "客户名称", "required": True},
        },
    },
    {
        "name": "attendance",
        "label": "考勤",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "fields": {"id": {"type": "int", "label": "ID"}},
    },
]


class CatalogErp:
    """list_entities 假 ERP：记录调用 + 可注入失败/空信封。"""

    def __init__(self, *, entities: list[dict] | None = None,
                 fail_list: bool = False, empty_envelope: bool = False):
        self.entities = list(entities) if entities is not None else _CATALOG_ENTITIES
        self.fail_list = fail_list
        self.empty_envelope = empty_envelope
        self.calls: list[dict] = []

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            if self.fail_list:
                return ToolResult(
                    ok=False,
                    error=MCPError(
                        code="MCP_SERVER_ERROR", category=ErrorCategory.SERVER,
                        message="erp catalog exploded",
                    ),
                )
            if self.empty_envelope:
                return ToolResult(ok=True, data={})  # 无 entities 键且非数组
            return ToolResult(ok=True, data={"entities": self.entities})
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(self, operation_id: str | None = None,
                      trace_id: str | None = None) -> ToolResult:
        """EntityCache/上层刷新入口（同 FakeMCPClient 形态：委托 call_tool）。"""
        return self.call_tool("list_entities", {})


def _ctx(erp: CatalogErp | None, *, role: str = "manager",
         scope: frozenset[str] = frozenset({"erp:query"}),
         with_context: bool = True) -> ToolContext:
    """list_entities 的工具上下文（同场景 12 _query_ctx 形态）。"""
    return ToolContext(
        context=(
            AgentContext(user_id="manager01", user_role=role, scope=scope)
            if with_context
            else None
        ),
        tasks=None, audit=None, metadata=None, sink=None, client=erp,
    )


# ---------------------------------------------------------------------------
# 紧凑目录（无 entity）：两级裁剪的形状与白名单外 item 标记
# ---------------------------------------------------------------------------


def test_compact_catalog_shape_and_crop_levels() -> None:
    """无 entity：全量紧凑目录——实体条目仅 {name,label,fields}，字段条目
    仅 {name,label?,type?,enum?}；description/default/min_value/sensitive/
    required/system_managed 等决策无关键一律不出现。"""
    erp = CatalogErp()
    result = run_list_entities(_ctx(erp), ListEntitiesArgs())

    assert result.ok
    s = result.summary
    assert s["entity"] == CATALOG_ENTITY_MARKER == "catalog"
    assert s["scope"] == "compact"
    assert s["count"] == len(_CATALOG_ENTITIES)
    assert "requested" not in s
    assert len(s["items"]) == 3

    meta = next(e for e in s["items"] if e["name"] == "metal_orders")
    assert set(meta) == {"name", "label", "fields"}
    assert meta["label"] == "钣金订单"
    field_map = {f["name"]: f for f in meta["fields"]}
    assert set(field_map) == {"id", "model", "weight_unit", "weight_per_unit"}
    for f in meta["fields"]:
        assert set(f) <= {"name", "label", "type", "enum"}
    # weight_unit 的枚举是模型选字段/写单位条件的关键信息，必须保留
    assert field_map["weight_unit"]["enum"] == ["kg", "m²"]
    # 裁剪掉的内容逐键核验（紧凑层只保留枚举，不保留其他决策无关键）
    assert "description" not in field_map["model"]
    assert "default" not in field_map["weight_unit"]
    assert "min_value" not in field_map["weight_per_unit"]
    assert "required" not in field_map["model"]
    assert "sensitive" not in field_map["model"]
    assert "system_managed" not in field_map["id"]
    # 目录含查询白名单外的实体（考勤）：能力直挂，差异由 note 提示
    assert any(e["name"] == "attendance" for e in s["items"])
    assert "白名单外" in s["note"]


def test_compact_field_omits_empty_label_and_keeps_nonempty() -> None:
    """紧凑字段条目的 key 裁剪：label/type 为空时不出现，enum 只在
    enum_values 非空列表时出现（纯函数直测）。"""
    assert _compact_field("a", {"type": "string", "label": "名称", "enum_values": []}) == {
        "name": "a", "label": "名称", "type": "string",
    }
    assert _compact_field("b", {"type": "", "label": None, "enum_values": None}) == {
        "name": "b",
    }
    assert _compact_field("c", {"type": "str", "label": "单位",
                                "enum_values": ["kg", "m²"]})["enum"] == ["kg", "m²"]
    # enum_values 非列表（畸形）不产生 enum 键
    assert "enum" not in _compact_field("d", {"enum_values": "kg"})


def test_compact_entity_drops_entity_level_long_text() -> None:
    """实体级 description/readable 等不进紧凑目录（纯函数直测）。"""
    raw = dict(_CATALOG_ENTITIES[0])
    compact = _compact_entity(raw)
    assert set(compact) == {"name", "label", "fields"}
    assert compact["name"] == "metal_orders"
    assert len(compact["fields"]) == len(raw["fields"])


# ---------------------------------------------------------------------------
# 单实体模式（带 entity）：server 原样细节透传
# ---------------------------------------------------------------------------


def test_single_entity_returns_server_raw_detail() -> None:
    """entity 指定：命中实体的完整公开字段细节原样返回（server 已按 scope
    过滤敏感内容，本层只过滤实体外内容，不自行叠白名单）。"""
    erp = CatalogErp()
    result = run_list_entities(_ctx(erp), ListEntitiesArgs(entity="metal_orders"))

    assert result.ok
    s = result.summary
    assert s["entity"] == "catalog"
    assert s["scope"] == "entity"
    assert s["requested"] == "metal_orders"
    assert s["count"] == 1
    item = s["items"][0]
    # 原样保留的实体属性与字段细节（含 description/enum_values/default/
    # min_value 等紧凑层已裁掉的键）
    assert item["name"] == "metal_orders"
    assert item["description"] == "钣金类订单主档"
    wu = item["fields"]["weight_unit"]
    assert wu["enum_values"] == ["kg", "m²"]
    assert wu["default"] == "kg"
    assert wu["sensitive"] is False
    assert item["fields"]["weight_per_unit"]["min_value"] == 0
    assert set(item) == set(_CATALOG_ENTITIES[0])  # 顶层结构未增删


def test_unknown_entity_fail_closed_lists_visible() -> None:
    """指定实体拼写错/不可见：fail closed，错误里带可见实体清单供自纠。"""
    erp = CatalogErp()
    result = run_list_entities(_ctx(erp), ListEntitiesArgs(entity="metal_oredrs"))

    assert result.ok is False
    err = result.summary["error"]
    assert "不在可查目录中" in err
    assert "metal_orders" in err and "customers" in err and "attendance" in err
    assert "items" not in result.summary


# ---------------------------------------------------------------------------
# fail closed：ERP 失败 / 信封不可解析 / 权限 / 接线
# ---------------------------------------------------------------------------


def test_list_entities_failure_fail_closed() -> None:
    """ERP 调用失败：ok=False 带错误说明（_err_text 文本），无部分数据。"""
    erp = CatalogErp(fail_list=True)
    result = run_list_entities(_ctx(erp), ListEntitiesArgs())

    assert result.ok is False
    assert "fail closed" in result.summary["error"]
    assert "erp catalog exploded" in result.summary["error"]
    assert "items" not in result.summary


def test_unparseable_envelope_fail_closed() -> None:
    """返回信封不含 entities 且非裸数组：fail closed，不静默。"""
    erp = CatalogErp(empty_envelope=True)
    result = run_list_entities(_ctx(erp), ListEntitiesArgs())

    assert result.ok is False
    assert "无法解析" in result.summary["error"]


def test_permission_gates_fail_closed_no_erp_call() -> None:
    """权限门：非 manager / 缺 erp:query / 无上下文一律拒绝且不触达 ERP。"""
    for kwargs in (
        dict(role="developer"),  # 角色不符（10.5）
        dict(role="manager", scope=frozenset()),  # scope 不足（2.4）
        dict(with_context=False),  # 上下文缺失
    ):
        erp = CatalogErp()
        result = run_list_entities(_ctx(erp, **kwargs), ListEntitiesArgs())
        assert result.ok is False, kwargs
        assert "权限不足" in result.summary["error"]
        assert erp.calls == []  # 关键：不触达 ERP


def test_missing_client_fail_closed() -> None:
    """客户端未接线：明确报错不静默（harness 运行时缺 client）。"""
    result = run_list_entities(_ctx(None), ListEntitiesArgs())

    assert result.ok is False
    assert "客户端未接线" in result.summary["error"]


def test_live_call_not_cached() -> None:
    """直挂语义：每次调用都实时打 server（不读 EntityCache）——模型要的是
    「现在有什么」。"""
    erp = CatalogErp()
    run_list_entities(_ctx(erp), ListEntitiesArgs())
    run_list_entities(_ctx(erp), ListEntitiesArgs(entity="customers"))

    assert [c["tool"] for c in erp.calls] == ["list_entities", "list_entities"]
