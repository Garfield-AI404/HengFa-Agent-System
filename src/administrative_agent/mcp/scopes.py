"""Agent scope 与工具白名单（规格 2.3/2.4）。

- REQUIRED_SCOPES：生产服务账号最小 scope（2.4 六项 + R7 收支两项），
  之外的 scope 一律拒绝。
- WHITELISTED_TOOLS：内勤挂载工具（2.3）；delete 不挂载、不调用。
- 通用 create/update 只允许 attendance/transactions（2.3 + transactions）；
  订单写入必须走领域工具 create_order_bundle /
  update_order（mcp-api.md §2 代码层禁止，客户端本地同样拦截，14.2
  契约测试覆盖）。
- 系统字段与派生金额（id/created_at/created_by/updated_at/updated_by/
  deleted_at/amount/total_amount）禁止传入（mcp-api.md §5/§6.1/§7.1）；
  本地拦截返回与服务端一致的 SYSTEM_FIELD_FORBIDDEN，供审计统一归类。
- transactions 的 amount 是业务字段
  （收支金额），放行；transactions 只额外禁止 source_type/source_id
  （ERP 联动维护，mcp-api.md §5）；其余实体（订单/考勤）口径不变——
  amount/total_amount 等派生金额仍拒（回归：orders/attendance 侧 amount
  仍拒）。

本模块是本地写保护层，在 client 请求发出之前拦截（fail closed）。
"""

from __future__ import annotations

from typing import Any

from administrative_agent.mcp.errors import ErrorCategory, InternalCode, MCPError

# 2.4 最小 scope 八项（生产只申请这些；管理员权限不能作为生产 Agent 凭据）。
# [2026-09-02 R7 收支写] 追加 finance:create / finance:update——scope 名为
# **占位符，待与 ERP 权限表核对**（真实 ERP 侧 agent_office 实测
# transactions create/update 已放行；scope 名核对属上线前事项，代码侧
# 按此名登记，三处一致：scopes.py / tools/base.py / web/routes_chat.py）
REQUIRED_SCOPES: tuple[str, ...] = (
    "erp:query",
    "attendance:create",
    "attendance:update",
    "orders:create_bundle",
    "orders:update",
    "reconciliation:query",
    "finance:create",
    "finance:update",
)

# 2.3 内勤挂载工具（delete 不挂载；list_entities 只做元数据刷新）
WHITELISTED_TOOLS: tuple[str, ...] = (
    "list_entities",
    "query",
    "create",
    "update",
    "create_order_bundle",
    "update_order",
)

# 通用 CRUD 只允许这些实体（2.3：create/update 只由考勤编排服务调用
# attendance；[2026-09-02 R7] 追加 transactions——收支写只走
# finance_create/finance_update 工具载荷，该白名单是工具侧/执行器侧
# verify_generic_write 的兜底层）
_GENERIC_WRITE_ENTITIES = frozenset({"attendance", "transactions"})

# 订单实体：写入必须走领域工具（mcp-api.md §2）
_ORDER_ENTITIES = frozenset({"metal_orders", "powder_orders"})

# 9.2 查询实体白名单：R4/R5 查询前强制应用（6.1"查询字段必须来自
# list_entities"+ 9.2 第 2 条"强制应用当前 Agent 可查询实体白名单"）。
# erp:query scope 的只读实体范围——本白名单只管**读**，与写权限无关
# （写路径仍由 _GENERIC_WRITE_ENTITIES / 领域工具把关，见下）。
# [2026-08-28 项目负责人批准] 开放 employees（员工只读，mcp-api.md §4.3）
# ——考勤全员记录自动化：模型先经 erp_query 主动查 employees 取全员名单，
# 再调用 attendance_plan_execute（规则 6）。
# [2026-09-02 R7 收支写] 追加 transactions（收支记录）/transaction_categories
# （收支分类）——finance_create/finance_update 先查后写的前提
# （分类强校验须按 transaction_categories 归属核验，R7 规则 1）。
# [2026-09-12 只读扩展 · 用户拍板] 追加 7 个**只读**实体：customer_contacts
# （客户联系人）/attendance（考勤记录）/ar_payments（应收收款）/ap_payments
# （应付付款）/salary_payments（工资发放）/powder_inventory（塑粉库存色号）/
# powder_consumption（塑粉消耗记录）——用户明确本次只开读、不开放写。
# 口径变更（有意推翻旧记载）：
#   * 旧注「对账类实体属 R6、由 D19 扩展、本单元不超前开放」「salary_payments
#     不在开放范围」一并作废——ar/ap_payments 与 salary_payments 本次按用户
#     要求开放**只读**查询（写路径不变：无对应写工具，ERP 侧也对 Agent 账号
#     关闭其 create/update）；
#   * attendance 是既有的写实体（R1 考勤，写链路走 attendance_plan_execute），
#     本次只让它可被 erp_query/erp_calc/export_data **读**，写链路不动；
#   * powder_inventory 直接暴露派生列 order_total（订单总重量）/consumed_total
#     （消耗总量）/stock（净库存，实时计算）——净库存直接查 stock 字段，禁止
#     再按 Σ未软删 powder_orders.total_weight − Σ未软删 powder_consumption.
#     quantity 自己另算（旧「ERP 不存库、须按公式另算」口径已作废）；三者是
#     **计算列**（list_entities 元数据 computed=true），只能作为 fields 取回，
#     不可用于 filter/order_by（ERP 侧拒绝）。
# [2026-09-16 当期应收口径开放] 追加 ar_settlements（应收平账/抹零，**只读**）
# ——SK.receivables 流程第 4 步要按客户求 Σsettled_amount（累计平账抹零），
# 未经本白名单会被 erp_query/erp_calc 在本地 fail closed 拒绝、取数拿不到。
# ERP 侧 ar_settlements 为只读实体（read_only=True，creatable/updatable/
# deletable 均 False；deleted_by 为隐藏字段、不注册；deleted_at 随软删过滤
# 不进结果），agent 侧同样只开读、无对应写工具。
# 读白名单的消费者：tools/query.py、tools/calc.py、domains/query.py
# （QueryExecutor.allowed_entities 默认值，实调处未覆盖）、
# domains/export_fields.py（导出跟随读，不为导出单列白名单）、
# nodes/tool_args_node.py（_QUERY_CATALOG_ORDER 必须与本常量一致）。
# [2026-09-13 核对] 以上 5 处均实际以 QUERYABLE_ENTITIES 为准，无遗漏/多余。
QUERYABLE_ENTITIES: frozenset[str] = frozenset(
    {
        "metal_orders", "powder_orders", "customers", "suppliers",
        "employees", "transactions", "transaction_categories",
        "customer_contacts", "attendance", "ar_payments", "ap_payments",
        "salary_payments", "powder_inventory", "powder_consumption",
        "ar_settlements",
    }
)

# 系统字段不可伪造（mcp-api.md §5）
SYSTEM_FIELDS = frozenset(
    {"id", "created_at", "created_by", "updated_at", "updated_by", "deleted_at"}
)

# 派生金额禁止传入/修改（mcp-api.md §6.1/§7.1）
DERIVED_FIELDS = frozenset({"amount", "total_amount"})

# [2026-09-02 R7] transactions 额外禁止字段：source_type/source_id 只能由
# ERP 联动维护（mcp-api.md §5），本地一律拦截（与工具层 finance.py
# 载荷构造/域执行器双重保护之一；tools/finance.py 载荷恒不含这些键）。
_TRANSACTIONS_EXTRA_FORBIDDEN = frozenset({"source_type", "source_id"})

_FORBIDDEN_FIELDS = SYSTEM_FIELDS | DERIVED_FIELDS


def _forbidden_fields_for(context: str) -> frozenset[str]:
    """实体感知禁止字段（[2026-09-02 R7 关键改动]）。

    - context == "transactions"：SYSTEM_FIELDS | source_type/source_id——
      transactions.amount 是业务字段（收支金额），**放行**；
    - 其余实体/上下文（订单/考勤/update_order.changes 等）：原口径
      SYSTEM_FIELDS | DERIVED_FIELDS——amount/total_amount 派生金额仍拒
      （orders/attendance 侧 amount 仍拒，回归 14.2 契约测试覆盖）。

    只在 _assert_no_forbidden_fields 内部调用；context 由调用方（工具层
    verify_generic_write / verify_order_*）显式传入。
    """
    if context == "transactions":
        return SYSTEM_FIELDS | _TRANSACTIONS_EXTRA_FORBIDDEN
    return _FORBIDDEN_FIELDS

# mcp-api.md §7.1 白名单：钣金允许修改字段（其余未知字段 → VALIDATION_ERROR）。
# [2026-09-08 ERP 上线] 追加 surcharge_amount（附加款，decimal(10,2)，非负，
# 最多 2 位小数，仅钣金；属业务字段而非派生金额——总额 total_amount 仍拒）
UPDATE_WHITELIST_METAL = frozenset(
    {
        "customer_id", "model", "unit_price", "price_type", "status",
        "weight_per_unit", "weight_unit", "quantity", "quantity_unit",
        "surcharge_amount", "in_date", "out_date", "notes",
    }
)

# mcp-api.md §7.1 白名单：塑粉允许修改字段（仅 total_weight 为当前功能重量字段；
# 已删除的 unit_type/quantity/unit_weight 不可查询或修改）
UPDATE_WHITELIST_POWDER = frozenset(
    {"supplier_id", "color_code", "total_weight", "price_per_kg", "in_date", "notes"}
)

_ORDER_UPDATE_WHITELISTS = {
    "metal": UPDATE_WHITELIST_METAL,
    "powder": UPDATE_WHITELIST_POWDER,
}
_ORDER_TYPES = frozenset(_ORDER_UPDATE_WHITELISTS)


def verify_scope(scope: str) -> None:
    """scope 必须在 2.4 八项内（[2026-09-02 R7] +finance 两项占位）；
    未知 scope fail closed。"""
    if scope not in REQUIRED_SCOPES:
        raise MCPError(
            code=InternalCode.SCOPE_NOT_ALLOWED.value,
            category=ErrorCategory.LOCAL,
            message=f"Scope {scope!r} is not allowed for the agent.",
            details={"scope": scope},
        )


def verify_tool(tool_name: str) -> None:
    """工具必须在内勤挂载白名单（2.3）；delete 等一律拒绝。"""
    if tool_name not in WHITELISTED_TOOLS:
        raise MCPError(
            code=InternalCode.NOT_WHITELISTED.value,
            category=ErrorCategory.LOCAL,
            message=f"Tool {tool_name!r} is not mounted for the agent.",
            details={"tool": tool_name},
        )


def verify_generic_write(tool_name: str, entity: str, data: dict[str, Any]) -> None:
    """通用 create/update 本地拦截（2.3/mcp-api.md §2）。

    - create/update 只允许 attendance/transactions（R7）；订单走领域工具；
    - delete 不挂载（verify_tool 已拦，此处兜底订单 delete 语义）；
    - 系统字段与派生金额禁止传入（transactions 实体感知：amount 业务
      字段放行，source_type/source_id 恒禁，见 _forbidden_fields_for）。
    """
    verify_tool(tool_name)
    if entity in _ORDER_ENTITIES:
        if tool_name == "create":
            _reject_order_create(entity)
        if tool_name == "update":
            raise MCPError(
                code="ORDER_UPDATE_REQUIRES_ORDER_TOOL",
                category=ErrorCategory.VALIDATION,
                message="Order updates must use the update_order tool.",
                details={"entity": entity},
            )
        if tool_name == "delete":
            raise MCPError(
                code="ORDER_DELETE_NOT_ALLOWED",
                category=ErrorCategory.VALIDATION,
                message="Order deletion is not allowed.",
                details={"entity": entity},
            )
    if tool_name in ("create", "update") and entity not in _GENERIC_WRITE_ENTITIES:
        raise MCPError(
            code=InternalCode.NOT_WHITELISTED.value,
            category=ErrorCategory.LOCAL,
            message=f"Generic {tool_name} is not allowed for entity {entity!r}.",
            details={"tool": tool_name, "entity": entity},
        )
    _assert_no_forbidden_fields(data, context=entity)


def verify_order_bundle_arguments(arguments: dict[str, Any]) -> None:
    """create_order_bundle 参数保护：order 内禁止系统字段与 amount/total_amount。"""
    order = arguments.get("order")
    if isinstance(order, dict):
        _assert_no_forbidden_fields(order, context="create_order_bundle.order")


def verify_update_order_arguments(arguments: dict[str, Any]) -> None:
    """update_order 参数保护（mcp-api.md §7.1，fail closed）。

    - changes 内系统字段与金额 → SYSTEM_FIELD_FORBIDDEN（最先检查：
      伪造系统/派生字段是最严重违规，任何请求形态都优先拦截）；
    - order_type 只能 metal/powder；changes 必填且非空对象；
    - changes 内白名单外字段（未知或不适用于该订单类型）→ VALIDATION_ERROR。
    """
    changes = arguments.get("changes")
    if isinstance(changes, dict):
        _assert_no_forbidden_fields(changes, context="update_order.changes")
    order_type = arguments.get("order_type")
    if order_type not in _ORDER_TYPES:
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="order_type must be 'metal' or 'powder'.",
            details={"order_type": order_type},
        )
    if not isinstance(changes, dict) or not changes:
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="changes must be a non-empty object.",
            details={"order_type": order_type},
        )
    whitelist = _ORDER_UPDATE_WHITELISTS[order_type]
    unknown = [f for f in changes if f not in whitelist]
    if unknown:
        raise MCPError(
            code=InternalCode.VALIDATION_ERROR.value,
            category=ErrorCategory.VALIDATION,
            message="Field(s) not allowed for this order type.",
            details={"fields": unknown, "order_type": order_type},
        )


def _assert_no_forbidden_fields(data: dict[str, Any], context: str) -> None:
    forbidden = [f for f in data if f in _forbidden_fields_for(context)]
    if forbidden:
        raise MCPError(
            code=InternalCode.SYSTEM_FIELD_FORBIDDEN.value,
            category=ErrorCategory.VALIDATION,
            message="Forbidden field(s) in payload.",
            details={"fields": forbidden, "context": context},
        )


def _reject_order_create(entity: str) -> None:
    raise MCPError(
        code="ORDER_CREATE_REQUIRES_BUNDLE",
        category=ErrorCategory.VALIDATION,
        message="Orders must be created via create_order_bundle.",
        details={"entity": entity},
    )
