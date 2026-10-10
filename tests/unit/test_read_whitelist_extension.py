"""[2026-09-12 只读扩展] 新开 7 个只读实体的白名单贯通测试。

背景：用户在 `mcp/scopes.py:QUERYABLE_ENTITIES` 之外要求把
customer_contacts/attendance/ar_payments/ap_payments/salary_payments/
powder_inventory/powder_consumption 开放为**只读**查询实体。本模块钉死：

- 这 7 个实体确实通过 erp_query / erp_calc / export_data 三条读路径的
  白名单校验（各至少一条）；
- 反例：不在白名单的实体（users/webhook_subscriptions/file_attachments）
  三条读路径仍一律拒绝（防白名单被无差别放大）；
- QUERYABLE_ENTITIES 含 7 个新实体（目录顺序一致性由
  test_tool_descriptions.py::test_query_catalog_order_matches_queryable_entities
  显式钉住元组顺序，本模块不重复）。

假 ERP：返回最小 list_entities 元数据 + 空查询页（本模块只验白名单
放行/拒绝的门，不验聚合/导出数值，那些由 test_tools_calc.py /
test_export.py 覆盖）。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.calc import ErpCalcArgs, run_erp_calc
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.domains.export_fields import (
    EXPORT_DEFAULT_EXCLUDED,
    validate_export_fields,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import InternalCode, MCPError
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

#: 2026-09-12 新开的 7 个只读实体
NEW_READ_ENTITIES: tuple[str, ...] = (
    "customer_contacts",
    "attendance",
    "ar_payments",
    "ap_payments",
    "salary_payments",
    "powder_inventory",
    "powder_consumption",
)

#: 仍不在白名单的实体（三条读路径都必须拒）
NON_QUERYABLE_ENTITIES: tuple[str, ...] = (
    "users",
    "webhook_subscriptions",
    "file_attachments",
)


def _field(name: str, ftype: str = "str", **extra) -> dict:
    return {"type": ftype, "label": name, **extra}


#: 新实体的最小字段目录（贴近 ERP 注册表字段；只为白名单/字段校验提供落点）
_EXTENSION_ENTITIES: list[dict] = [
    {
        "name": "customer_contacts",
        "label": "客户联系人",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "customer_id": _field("客户ID", "int"),
            "contact_name": _field("联系人姓名"),
            "contact_phone": _field("联系电话"),
            "is_primary": _field("是否主联系人", "bool"),
            "created_at": _field("创建时间", "datetime"),
            "updated_at": _field("更新时间", "datetime"),
        },
    },
    {
        "name": "attendance",
        "label": "考勤记录",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "employee_id": _field("员工ID", "int"),
            "att_date": _field("考勤日期", "date"),
            "leave_minutes": _field("请假分钟数", "int"),
            "overtime_minutes": _field("加班分钟数", "int"),
            "created_at": _field("创建时间", "datetime"),
            "updated_at": _field("更新时间", "datetime"),
        },
    },
    {
        "name": "ar_payments",
        "label": "应收收款",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "customer_id": _field("客户ID", "int"),
            "amount": _field("金额", "decimal"),
            "pay_date": _field("收款/付款日期", "date"),
            "notes": _field("备注"),
            "created_at": _field("创建时间", "datetime"),
        },
    },
    {
        "name": "ap_payments",
        "label": "应付付款",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "supplier_id": _field("供货商ID", "int"),
            "amount": _field("金额", "decimal"),
            "pay_date": _field("收款/付款日期", "date"),
            "notes": _field("备注"),
            "created_at": _field("创建时间", "datetime"),
        },
    },
    {
        "name": "salary_payments",
        "label": "工资发放",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "employee_id": _field("员工ID", "int"),
            "year": _field("年份", "int"),
            "month": _field("月份", "int"),
            "amount": _field("金额", "decimal"),
            "paid_at": _field("发放时间", "datetime"),
            "notes": _field("备注"),
            "created_at": _field("创建时间", "datetime"),
        },
    },
    {
        "name": "powder_inventory",
        "label": "塑粉库存色号",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "color_code": _field("色号"),
            "warn_threshold": _field("预警阈值", "decimal"),
            "notes": _field("备注"),
            "created_by": _field("创建人ID", "int"),
            "updated_by": _field("更新人ID", "int"),
            "created_at": _field("创建时间", "datetime"),
            "updated_at": _field("更新时间", "datetime"),
        },
    },
    {
        "name": "powder_consumption",
        "label": "塑粉消耗记录",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "inventory_id": _field("色号ID", "int"),
            "color_code": _field("色号"),
            "consume_date": _field("消耗日期", "date"),
            "quantity": _field("数量", "decimal"),
            "notes": _field("备注"),
            "created_by": _field("创建人ID", "int"),
            "updated_by": _field("更新人ID", "int"),
            "created_at": _field("创建时间", "datetime"),
            "updated_at": _field("更新时间", "datetime"),
        },
    },
    # 反例实体：目录可见、但不在白名单
    {
        "name": "users",
        "label": "用户账号",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {"id": _field("ID", "int"), "username": _field("用户名")},
    },
    {
        "name": "webhook_subscriptions",
        "label": "Webhook订阅",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {"id": _field("ID", "int"), "url": _field("回调地址")},
    },
    {
        "name": "file_attachments",
        "label": "上传文件",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {"id": _field("ID", "int"), "filename": _field("原始文件名")},
    },
]


class _FakeErp:
    """最小假 ERP：list_entities 元数据 + query 返回空页（只验白名单门）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": list(_EXTENSION_ENTITIES)})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        self.calls.append(tool_name)
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
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
# 白名单常量与目录顺序一致性
# ---------------------------------------------------------------------------


def test_queryable_entities_includes_all_new_read_entities() -> None:
    for entity in NEW_READ_ENTITIES:
        assert entity in QUERYABLE_ENTITIES, f"{entity} 未纳入读白名单"
    # [2026-09-16 当期应收口径开放] 追加 ar_settlements（只读）→ 14 变 15
    assert len(QUERYABLE_ENTITIES) == 15


def test_non_queryable_entities_still_excluded_from_constant() -> None:
    for entity in NON_QUERYABLE_ENTITIES:
        assert entity not in QUERYABLE_ENTITIES


# 目录顺序一致性（_QUERY_CATALOG_ORDER 与 QUERYABLE_ENTITIES 含顺序精确一致）
# 的权威断言收在 tests/unit/test_tool_descriptions.py::
# test_query_catalog_order_matches_queryable_entities（显式钉住元组顺序）——
# 此处不再保留弱副本（旧断言只比 frozenset + 长度，顺序漂移不被发现）。


# ---------------------------------------------------------------------------
# erp_query：新实体放行 / 反例拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", NEW_READ_ENTITIES)
def test_erp_query_accepts_new_read_entities(entity: str) -> None:
    result = run_erp_query(_ctx(_FakeErp()), ErpQueryArgs(entity=entity))
    assert result.ok is True, result.summary


@pytest.mark.parametrize("entity", NON_QUERYABLE_ENTITIES)
def test_erp_query_rejects_non_whitelisted_entities(entity: str) -> None:
    result = run_erp_query(_ctx(_FakeErp()), ErpQueryArgs(entity=entity))
    assert result.ok is False
    assert "不可查询" in result.summary["error"]


# ---------------------------------------------------------------------------
# erp_calc：新实体放行 / 反例拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", NEW_READ_ENTITIES)
def test_erp_calc_accepts_new_read_entities(entity: str) -> None:
    result = run_erp_calc(
        _ctx(_FakeErp()),
        ErpCalcArgs(entity=entity, measures=[{"op": "count"}]),
    )
    assert result.ok is True, result.summary


@pytest.mark.parametrize("entity", NON_QUERYABLE_ENTITIES)
def test_erp_calc_rejects_non_whitelisted_entities(entity: str) -> None:
    result = run_erp_calc(
        _ctx(_FakeErp()),
        ErpCalcArgs(entity=entity, measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "白名单" in result.summary["error"]


# ---------------------------------------------------------------------------
# export_data：新实体放行（fields 缺省走元数据全字段兜底）/ 反例拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", NEW_READ_ENTITIES)
def test_export_fields_accepts_new_read_entities_default_columns(entity: str) -> None:
    cache = EntityCache(_FakeErp())
    columns = validate_export_fields(entity, None, cache)
    # 无 EXPORT_DEFAULT_COLUMNS → 元数据全字段减系统字段（有意接受的行为）
    assert columns, f"{entity} 默认导出列为空"
    for excluded in EXPORT_DEFAULT_EXCLUDED:
        assert excluded not in columns, f"{entity} 默认导出列不应含系统字段 {excluded}"


@pytest.mark.parametrize("entity", NON_QUERYABLE_ENTITIES)
def test_export_fields_rejects_non_whitelisted_entities(entity: str) -> None:
    cache = EntityCache(_FakeErp())
    with pytest.raises(MCPError) as exc:
        validate_export_fields(entity, None, cache)
    assert exc.value.code == InternalCode.NOT_WHITELISTED.value
