"""D21 R6 复核后写入执行契约：写只能走订单领域工具（本地写保护层）。

## 2026-08-29 重写说明

D26 删除了 `agent/intent.py` 与 `agent/policy_resolver.py`，本文件原有的
7 个用例中 4 个随之永久失效——`OPERATION_SCOPES[reconcile_execute]` 的
操作级 scope 映射、以及 `validate_intent` 的角色/scope 校验，已改由
LLM 决策 + 确定性防护层承担。

**仍然活着、且此前零覆盖**的是 `mcp/scopes.py`：它用代码层拦截保证
「对账差异写入走订单领域工具，不直接写 ERP」这条规则包第 4 句落地。
本文件重写为对它的契约断言。

## 覆盖

- `reconciliation:write` 不申请、`verify_scope` 拒绝；
- R6.reconciliation.execute 规则包仍注册且含四句；
- 通用 create/update 落在订单实体上被拦（必须走 `create_order_bundle`
  / `update_order`）；
- 通用 create/update 只允许 attendance；
- 系统字段与派生金额禁止传入（`SYSTEM_FIELD_FORBIDDEN`）；
- `update_order` / `create_order_bundle` 参数保护（order_type、changes
  非空、字段白名单）。

## 两条实测得到、与直觉不同的行为（勿"修正"成想象的样子）

1. `verify_generic_write("delete", ...)` 抛的是 `NOT_WHITELISTED` 而非
   `ORDER_DELETE_NOT_ALLOWED`：函数首行先调 `verify_tool`，`delete` 不在
   `WHITELISTED_TOOLS`，`ORDER_DELETE_NOT_ALLOWED` 分支实际不可达
   （防御性死代码）。断言按实际行为写。
2. `verify_order_bundle_arguments({})` 放行：无 `order` 键时不校验。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError
from administrative_agent.mcp.scopes import (
    REQUIRED_SCOPES,
    WHITELISTED_TOOLS,
    verify_generic_write,
    verify_order_bundle_arguments,
    verify_scope,
    verify_tool,
    verify_update_order_arguments,
)

EXECUTE_POLICY_KEY = "R6.reconciliation.execute"


# ---------------------------------------------------------------------------
# scope 边界：一期不申请 reconciliation:write
# ---------------------------------------------------------------------------


def test_reconciliation_write_scope_never_requested():
    """一期不申请 reconciliation:write（差异写入走既有订单 scope）。"""
    assert "reconciliation:write" not in REQUIRED_SCOPES
    # 写入所需的两项订单领域 scope 已申请
    assert "orders:create_bundle" in REQUIRED_SCOPES
    assert "orders:update" in REQUIRED_SCOPES


def test_reconciliation_write_scope_rejected_by_verify_scope():
    """模型不能以任何途径获得写 ERP 的通用 scope。"""
    with pytest.raises(MCPError) as exc:
        verify_scope("reconciliation:write")
    assert exc.value.code == InternalCode.SCOPE_NOT_ALLOWED.value
    assert exc.value.category == ErrorCategory.LOCAL


# ---------------------------------------------------------------------------
# 规则包：R6.reconciliation.execute 仍注册且含四句
# ---------------------------------------------------------------------------


def test_execute_rule_package_registered_with_four_rules():
    """规则包注册于 10.2 表，正文含 report_id 校验/重新查询/版本检查/
    订单领域工具四句（最后一句即本文件其余用例的代码层落地）。

    D26 只删除了 `POLICY_REGISTRY` / `ALL_POLICY_KEYS` 两个索引常量，
    `policy_for` 本身与规则包正文未受影响。
    [2026-09-10 P0-5] 规则 1 由「review_id」改「report_id」（对齐工具参数
    ReconcileConfirmArgs.report_id 与 base.py 参数轮口径）——断言同步。
    """
    package = policy_for(EXECUTE_POLICY_KEY)
    assert package.policy_key == EXECUTE_POLICY_KEY
    text = package.text
    assert "report_id" in text
    assert "重新查询" in text
    assert "版本检查" in text
    assert "订单领域工具" in text


# ---------------------------------------------------------------------------
# 写只能走订单领域工具（规则包第 4 句的代码层落地）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", ["metal_orders", "powder_orders"])
def test_generic_create_on_order_entity_requires_bundle(entity):
    """通用 create 落在订单实体 → 必须走 create_order_bundle
    （mcp-api.md §2 代码层禁止）。"""
    with pytest.raises(MCPError) as exc:
        verify_generic_write("create", entity, {"model": "X"})
    assert exc.value.code == "ORDER_CREATE_REQUIRES_BUNDLE"
    assert exc.value.category == ErrorCategory.VALIDATION
    assert exc.value.details == {"entity": entity}


@pytest.mark.parametrize("entity", ["metal_orders", "powder_orders"])
def test_generic_update_on_order_entity_requires_order_tool(entity):
    """通用 update 落在订单实体 → 必须走 update_order。"""
    with pytest.raises(MCPError) as exc:
        verify_generic_write("update", entity, {"model": "X"})
    assert exc.value.code == "ORDER_UPDATE_REQUIRES_ORDER_TOOL"
    assert exc.value.category == ErrorCategory.VALIDATION


def test_delete_tool_never_mounted():
    """delete 不挂载（2.3）：函数白名单层即拒绝。

    注意落在 `verify_tool` 而非 `verify_generic_write` 内的
    `ORDER_DELETE_NOT_ALLOWED` 分支——后者因首行 `verify_tool` 先抛而
    不可达（见文件头说明 1）。
    """
    assert "delete" not in WHITELISTED_TOOLS
    with pytest.raises(MCPError) as exc:
        verify_tool("delete")
    assert exc.value.code == InternalCode.NOT_WHITELISTED.value
    assert exc.value.category == ErrorCategory.LOCAL
    # 经 verify_generic_write 进入时同样先被工具白名单拦下
    with pytest.raises(MCPError) as exc2:
        verify_generic_write("delete", "metal_orders", {})
    assert exc2.value.code == InternalCode.NOT_WHITELISTED.value


@pytest.mark.parametrize(
    "tool,entity",
    [
        ("create", "customers"),
        ("create", "suppliers"),
        ("update", "employees"),
        ("update", "customers"),
    ],
)
def test_generic_write_restricted_to_attendance(tool, entity):
    """通用 create/update 只允许 attendance（2.3：订单走领域工具，
    其余实体一律拒绝）。"""
    with pytest.raises(MCPError) as exc:
        verify_generic_write(tool, entity, {"x": 1})
    assert exc.value.code == InternalCode.NOT_WHITELISTED.value
    assert exc.value.details == {"tool": tool, "entity": entity}


def test_generic_write_on_attendance_passes():
    """attendance 是唯一允许通用写的实体（正向对照，防上条误伤）。"""
    verify_generic_write("create", "attendance", {"employee_id": "e1"})  # 不抛即通过
    verify_generic_write("update", "attendance", {"employee_id": "e1"})


# ---------------------------------------------------------------------------
# 系统字段与派生金额禁止伪造
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["id", "created_at", "created_by", "updated_at", "updated_by", "deleted_at", "amount", "total_amount"],
)
def test_forbidden_fields_rejected_in_generic_write(forbidden):
    """系统字段与派生金额禁止传入（mcp-api.md §5/§6.1/§7.1）。"""
    with pytest.raises(MCPError) as exc:
        verify_generic_write("create", "attendance", {forbidden: "x"})
    assert exc.value.code == InternalCode.SYSTEM_FIELD_FORBIDDEN.value
    assert exc.value.details["fields"] == [forbidden]


def test_order_bundle_arguments_reject_forbidden_fields():
    """create_order_bundle 的 order 内禁止系统字段与金额。"""
    with pytest.raises(MCPError) as exc:
        verify_order_bundle_arguments({"order": {"model": "X", "total_amount": 1}})
    assert exc.value.code == InternalCode.SYSTEM_FIELD_FORBIDDEN.value
    assert exc.value.details["context"] == "create_order_bundle.order"
    # 干净参数放行
    verify_order_bundle_arguments({"order": {"model": "X"}})


# ---------------------------------------------------------------------------
# update_order 参数保护（mcp-api.md §7.1，fail closed）
# ---------------------------------------------------------------------------


def test_update_order_rejects_invalid_order_type():
    with pytest.raises(MCPError) as exc:
        verify_update_order_arguments({"order_type": "plastic", "changes": {"model": "X"}})
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value
    assert exc.value.details == {"order_type": "plastic"}


@pytest.mark.parametrize("changes", [{}, None, "model"])
def test_update_order_rejects_empty_or_non_object_changes(changes):
    """changes 必须是非空对象。"""
    with pytest.raises(MCPError) as exc:
        verify_update_order_arguments({"order_type": "metal", "changes": changes})
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value


def test_update_order_rejects_fields_outside_whitelist():
    """白名单外字段 → VALIDATION_ERROR（钣金/塑粉白名单不通用）。"""
    with pytest.raises(MCPError) as exc:
        verify_update_order_arguments({"order_type": "metal", "changes": {"color_code": "c"}})
    assert exc.value.code == InternalCode.VALIDATION_ERROR.value
    assert exc.value.details == {"fields": ["color_code"], "order_type": "metal"}


def test_update_order_forbidden_fields_checked_before_order_type():
    """伪造系统/派生字段是最严重违规：任何请求形态都优先拦截。

    此处 order_type 也是非法的，但抛出的必须是 SYSTEM_FIELD_FORBIDDEN
    而非 VALIDATION_ERROR —— 校验顺序本身是契约的一部分。
    """
    with pytest.raises(MCPError) as exc:
        verify_update_order_arguments({"order_type": "plastic", "changes": {"amount": 1}})
    assert exc.value.code == InternalCode.SYSTEM_FIELD_FORBIDDEN.value


@pytest.mark.parametrize(
    "order_type,changes",
    [
        ("metal", {"model": "X"}),
        ("metal", {"unit_price": 10, "quantity": 2}),
        ("powder", {"color_code": "c"}),
        ("powder", {"total_weight": 5.0}),
    ],
)
def test_update_order_accepts_whitelisted_fields(order_type, changes):
    """正向对照：各自类型白名单内的字段放行。"""
    verify_update_order_arguments({"order_type": order_type, "changes": changes})
