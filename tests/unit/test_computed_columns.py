"""[2026-09-13 computed 契约] 计算列消费端行为测试。

背景：ERP `list_entities` 现在把派生/计算列也放进 `entity.fields`，并以布尔键
`computed`（普通列 false / 计算列 true）区分（mcp-api.md §3）。ERP `query` 对
计算列的 `filter`/`order_by` 一律拒绝（`_assert_column_field`）——agent 侧若不
本地剔除，`filter={"stock": 100}` 会透传 ERP 被硬拒 → 整轮对话重跑。

本模块钉住消费端（erp_query / erp_calc / tool_args_node 目录段 / FieldSpec）：

- `FieldSpec.from_raw` 正确解析 `computed`（缺省 false）；
- erp_query 的 `filter` 传计算列 → 本地剔除、**不进下发参数**、note 含「计算列」；
- erp_query 的 `order_by` 传 `-stock`/`stock` → 同上被剔除 + note（判前缀前剥 `-`）；
- tool_args_node 渲染：计算列**不在**「必须取自下列列表」行，出现在「不可用于
  erp_query 的 filter/order_by」说明里（并保留 erp_calc 可用路径）；
- 任务 A（净库存文案）：erp_query 描述不含「查不到净库存」，含 stock 直查口径。

假 ERP：最小 list_entities 元数据 + 空查询页（只验剔除/渲染门，不验聚合数值）。
"""

from __future__ import annotations

from administrative_agent.agent.nodes.tool_args_node import _erp_query_catalog_section
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.calc import ErpCalcArgs, run_erp_calc
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache, FieldSpec


def _field(name: str, ftype: str = "str", **extra) -> dict:
    return {"type": ftype, "label": name, **extra}


#: powder_inventory：普通列 + 三个计算列（computed=true，贴近 ERP 派生列）
_POWDER_INVENTORY_FIELDS = {
    "id": _field("ID", "int"),
    "color_code": _field("色号"),
    "warn_threshold": _field("预警阈值", "decimal"),
    # 派生/计算列：只能作为 fields 取回，不可 filter/order_by
    "order_total": _field("订单总重量", "decimal", computed=True),
    "consumed_total": _field("消耗总量", "decimal", computed=True),
    "stock": _field("净库存", "decimal", computed=True),
}

_ENTITIES: list[dict] = [
    {
        "name": "powder_inventory",
        "label": "塑粉库存色号",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": _POWDER_INVENTORY_FIELDS,
    },
]


class _FakeErp:
    """最小假 ERP：list_entities 元数据 + query 空页 + 记录下发参数。"""

    def __init__(self) -> None:
        self.query_arguments: list[dict] = []

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": list(_ENTITIES)})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            self.query_arguments.append(dict(arguments or {}))
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


def _ctx(erp: _FakeErp) -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}),
        ),
        tasks=None,
        audit=None,
        sink=None,
        client=erp,
        metadata=EntityCache(erp),
    )


# ---------------------------------------------------------------------------
# FieldSpec.from_raw：computed 解析
# ---------------------------------------------------------------------------


def test_field_spec_parses_computed_flag() -> None:
    spec = FieldSpec.from_raw("stock", {"type": "decimal", "computed": True})
    assert spec.computed is True
    # raw 兜底保留新键（HTTP 层不丢字段）
    assert spec.raw["computed"] is True


def test_field_spec_computed_defaults_false() -> None:
    spec = FieldSpec.from_raw("color_code", {"type": "str"})
    assert spec.computed is False
    spec2 = FieldSpec.from_raw("color_code", {"type": "str", "computed": False})
    assert spec2.computed is False


# ---------------------------------------------------------------------------
# erp_query：filter 计算列剔除
# ---------------------------------------------------------------------------


def test_erp_query_filter_computed_column_evicted() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(entity="powder_inventory", filter={"stock": 100}),
    )
    assert result.ok is True, result.summary
    # 计算列被剔除 → filter 不下发（剔除后为空）
    assert erp.query_arguments, "未发出查询"
    assert "filter" not in erp.query_arguments[0], "计算列 filter 不应透传 ERP"
    note = result.summary.get("note", "")
    assert "计算列" in note, f"note 未点明计算列：{note!r}"


def test_erp_query_filter_keeps_regular_drops_computed() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(
            entity="powder_inventory",
            filter={"color_code": "P01", "stock": {"gte": 0}},
        ),
    )
    assert result.ok is True, result.summary
    sent = erp.query_arguments[0].get("filter")
    assert sent == {"color_code": "P01"}, f"下发 filter 不符：{sent!r}"
    assert "计算列" in result.summary.get("note", "")


# ---------------------------------------------------------------------------
# erp_query：order_by 计算列剔除（-stock / stock）
# ---------------------------------------------------------------------------


def test_erp_query_order_by_computed_column_evicted() -> None:
    for order_by in ("stock", "-stock"):
        erp = _FakeErp()
        result = run_erp_query(
            _ctx(erp),
            ErpQueryArgs(entity="powder_inventory", order_by=order_by),
        )
        assert result.ok is True, result.summary
        assert "order_by" not in erp.query_arguments[0], (
            f"计算列 order_by（{order_by}）不应透传 ERP"
        )
        assert "计算列" in result.summary.get("note", "")


def test_erp_query_order_by_keeps_regular_drops_computed_prefix() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(
            entity="powder_inventory", order_by=["color_code", "-stock"]
        ),
    )
    assert result.ok is True, result.summary
    assert erp.query_arguments[0].get("order_by") == ["color_code"]
    note = result.summary.get("note", "")
    assert "计算列" in note and "-stock" in note


# ---------------------------------------------------------------------------
# erp_calc：filter 计算列剔除
# ---------------------------------------------------------------------------


def test_erp_calc_filter_computed_column_evicted() -> None:
    erp = _FakeErp()
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="powder_inventory",
            filter={"stock": {"gte": 0}},
            measures=[{"op": "count"}],
        ),
    )
    assert result.ok is True, result.summary
    assert erp.query_arguments, "未发出查询"
    assert "filter" not in erp.query_arguments[0], "计算列 filter 不应透传 ERP"
    assert "计算列" in result.summary.get("note", "")


# ---------------------------------------------------------------------------
# tool_args_node：目录段把计算列与普通字段分开渲染
# ---------------------------------------------------------------------------


def test_catalog_section_separates_computed_from_regular() -> None:
    section = _erp_query_catalog_section(EntityCache(_FakeErp()))
    assert section is not None
    # 普通字段行含色号，不含计算列
    regular_line = next(
        ln for ln in section.splitlines() if ln.startswith("powder_inventory（")
    )
    assert "color_code" in regular_line
    assert "stock" not in regular_line, "计算列不得混进「必须取自下列列表」行"
    assert "order_total" not in regular_line
    # 计算列单独说明，点明仅可 fields 取回、不可用于 erp_query 的
    # filter/order_by（[2026-09-12 措辞收口] 不再泛化——erp_calc 的
    # group_by/measure 本地计算、可用计算列，措辞须保留该路径）
    assert "stock" in section
    assert "不可用于 erp_query 的 filter/order_by" in section
    assert "erp_calc" in section
    assert "fields" in section


# ---------------------------------------------------------------------------
# 任务 A：净库存文案（直接查 stock，不再教自算）
# ---------------------------------------------------------------------------


def test_erp_query_description_net_inventory_direct_stock() -> None:
    text = TOOL_REGISTRY["erp_query"].description
    assert "查不到净库存" not in text
    assert "无净库存字段" not in text
    assert "stock" in text
    assert "禁止" in text  # 明令禁止按旧公式自算


# ---------------------------------------------------------------------------
# [2026-09-12 任务 2] export_data：computed filter/order_by 本地剔除（同 erp_query）
# ---------------------------------------------------------------------------


def _export_ctx(erp: _FakeErp):
    """覆盖 ExcelExporter.export（wire 级捕获 filter/order_by，不落盘）。"""
    from dataclasses import replace

    from administrative_agent.agent.tools.export import (
        ExportDataArgs,
        run_export_data,
    )

    return ExportDataArgs, run_export_data, replace(_ctx(erp), audit=object())


def _capture_export(monkeypatch) -> list[dict]:
    from administrative_agent.domains import export as export_mod

    captured: list[dict] = []

    def fake_export(self, *, entity, fields, filter, order_by, filename,
                    sensitive_fields=None):
        captured.append({"filter": filter, "order_by": order_by})
        raise export_mod.ExportError("stop-after-capture")

    monkeypatch.setattr(export_mod.ExcelExporter, "export", fake_export)
    return captured


def test_export_data_filter_computed_column_evicted(monkeypatch) -> None:
    ExportDataArgs, run_export_data, ctx = _export_ctx(_FakeErp())
    captured = _capture_export(monkeypatch)
    result = run_export_data(
        ctx, ExportDataArgs(entity="powder_inventory", filter={"stock": 100})
    )
    assert result.ok is False  # fake export 主动抛错，仅捕获参数
    assert captured[-1]["filter"] is None, "计算列 filter 不应透传 ERP"
    assert "计算列" in result.summary.get("note", "")


def test_export_data_filter_keeps_regular_drops_computed(monkeypatch) -> None:
    ExportDataArgs, run_export_data, ctx = _export_ctx(_FakeErp())
    captured = _capture_export(monkeypatch)
    result = run_export_data(
        ctx,
        ExportDataArgs(
            entity="powder_inventory",
            filter={"color_code": "P01", "stock": {"gte": 0}},
        ),
    )
    assert captured[-1]["filter"] == {"color_code": "P01"}  # 值原样透传
    assert "计算列" in result.summary.get("note", "")


def test_export_data_order_by_computed_column_evicted(monkeypatch) -> None:
    ExportDataArgs, run_export_data, ctx = _export_ctx(_FakeErp())
    captured = _capture_export(monkeypatch)
    for order_by in ("stock", "-stock"):
        result = run_export_data(
            ctx, ExportDataArgs(entity="powder_inventory", order_by=order_by)
        )
        assert captured[-1]["order_by"] is None, (
            f"计算列 order_by（{order_by}）不应透传 ERP"
        )
        assert "计算列" in result.summary.get("note", "")


def test_export_data_order_by_keeps_regular_drops_computed(monkeypatch) -> None:
    ExportDataArgs, run_export_data, ctx = _export_ctx(_FakeErp())
    captured = _capture_export(monkeypatch)
    result = run_export_data(
        ctx,
        ExportDataArgs(entity="powder_inventory", order_by=["color_code", "-stock"]),
    )
    assert captured[-1]["order_by"] == ["color_code"]
    note = result.summary.get("note", "")
    assert "计算列" in note and "-stock" in note
