"""写操作抑制键 ``_retry_key`` 的两形态同键不变式（[2026-09-12 阶段 6]）。

背景：``_retry_key``（``agent/nodes/tools_node.py``）在两条路径被调用——
tools 侧以 **args_raw**（模型参数原样）、confirm 侧以 **execution**（确认
卡执行上下文）。同一调用的两种形态**必须**产出同一个键，否则写操作连续
失败计数在 prepare 失败（tools 侧）与执行失败/确认失效（confirm 侧）之间
断裂，「确认卡重复循环」抑制失效（既有契约，函数 docstring 明载）。

本文件钉住的不变式：
1. **同一调用两形态同键**——对 4 个批量写工具（order_create/order_update/
   finance_create/finance_update）逐一验证「批量（items ≥2）」「items 单元素
   （prepare 委托旧单条执行器）」「扁平单条」三组，args_raw 与 execution
   产出完全相同的键；
2. **批量键的显式取值**——批量形态 = ``f"{tool}:batch"``（粒度粗但方向
   安全：整工具批量写共用一个失败计数维度；同键由构造保证，不依赖 item
   内字段逐项对齐）；
3. **旧形状分支逐字保留**——attendance / mapping_template / reconciliation
   等既有工具的键不受新增 items 分支影响。

突变保护（报告中的定点突变 ①）：删掉 ``_retry_key`` 的批量分支后，
order_create / order_update 的批量两形态会双双降级为裸 ``tool_name``
（相等但非 ``{tool}:batch``）——第 2 条显式取值断言使该突变必红。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.nodes.tools_node import _retry_key
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.finance import (
    FinanceCreateArgs,
    FinanceUpdateArgs,
    prepare_finance_create,
    prepare_finance_update,
)
from administrative_agent.agent.tools.orders import (
    OrderCreateArgs,
    OrderUpdateArgs,
    prepare_order_create,
    prepare_order_update,
)
from administrative_agent.mcp.client import ToolResult


def _same_key(tool: str, args_raw: dict, execution: dict, expected: str) -> None:
    """断言：两形态同键，且等于期望键。"""
    key_args = _retry_key(tool, args_raw)
    key_exec = _retry_key(tool, execution)
    assert key_args == key_exec, (
        f"{tool} 两形态不同键：args_raw={key_args!r} execution={key_exec!r}"
    )
    assert key_args == expected, f"{tool} 键={key_args!r} 期望 {expected!r}"


# ---------------------------------------------------------------------------
# 1. 批量（items ≥ 2）：两形态同键 = f"{tool}:batch"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "args_raw", "execution"),
    [
        (
            "order_create",
            {"order_type": "metal", "items": [
                {"customer_name": "铭泰", "model": "M1", "quantity": 12},
                {"customer_name": "示例", "model": "M2", "quantity": 5},
            ]},
            {"tool": "order_create", "order_type": "metal",
             "policy_key": "R2.order.create.metal", "batch_size": 2, "items": [
                 {"index": 1, "item_id": "metal:3:M1:2026-09-12", "label": "客户 铭泰 / M1",
                  "action": "created", "draft": {"customer_name": "铭泰", "model": "M1"}},
                 {"index": 2, "item_id": "metal:7:M2:2026-09-12", "label": "客户 示例 / M2",
                  "action": "created", "draft": {"customer_name": "示例", "model": "M2"}},
             ]},
        ),
        (
            "order_create",
            {"order_type": "powder", "items": [
                {"supplier_name": "顺达", "color_code": "9010", "total_weight": 30},
                {"supplier_name": "昌盛", "color_code": "RAL7035", "total_weight": 10},
            ]},
            {"tool": "order_create", "order_type": "powder",
             "policy_key": "R2.order.create.powder", "batch_size": 2, "items": [
                 {"index": 1, "item_id": "powder:4:9010:2026-09-12", "label": "供货商 顺达 / 9010",
                  "action": "created", "draft": {"supplier_name": "顺达", "color_code": "9010"}},
                 {"index": 2, "item_id": "powder:9:RAL7035:2026-09-12", "label": "供货商 昌盛 / RAL7035",
                  "action": "created", "draft": {"supplier_name": "昌盛", "color_code": "RAL7035"}},
             ]},
        ),
        (
            "order_update",
            {"order_type": "metal", "items": [
                {"order_id": 5, "status": "完成"},
                {"order_id": 9, "notes": "加急"},
            ]},
            {"tool": "order_update", "order_type": "metal",
             "policy_key": "R3.order.update.metal", "batch_size": 2, "items": [
                 {"index": 1, "item_id": "5", "label": "5", "action": "updated",
                  "order_id": 5, "desired": {"status": "完成"}},
                 {"index": 2, "item_id": "9", "label": "9", "action": "updated",
                  "order_id": 9, "desired": {"notes": "加急"}},
             ]},
        ),
        (
            "order_update",
            {"order_type": "powder", "items": [
                {"order_id": 11, "total_weight": 22.5},
                {"order_id": 12, "color_code": "9010"},
            ]},
            {"tool": "order_update", "order_type": "powder",
             "policy_key": "R3.order.update.powder", "batch_size": 2, "items": [
                 {"index": 1, "item_id": "11", "label": "11", "action": "updated",
                  "order_id": 11, "desired": {"total_weight": 22.5}},
                 {"index": 2, "item_id": "12", "label": "12", "action": "updated",
                  "order_id": 12, "desired": {"color_code": "9010"}},
             ]},
        ),
        (
            "finance_create",
            {"items": [
                {"tx_type": "支出", "category": "运费", "tx_date": "2026-09-10", "amount": "100.00"},
                {"tx_type": "收入", "category": "销售", "tx_date": "2026-09-11", "amount": "50.00"},
            ]},
            {"tool": "finance_create", "tx_type": "支出", "category": "运费",
             "tx_date": "2026-09-10", "policy_key": "R7.finance.create",
             "batch_size": 2, "items": [
                 {"index": 1, "item_id": "支出:运费:2026-09-10:100.00",
                  "label": "支出·运费（2026-09-10）", "action": "created", "payload": {},
                  "tx_type": "支出", "category": "运费", "tx_date": "2026-09-10"},
                 {"index": 2, "item_id": "收入:销售:2026-09-11:50.00",
                  "label": "收入·销售（2026-09-11）", "action": "created", "payload": {},
                  "tx_type": "收入", "category": "销售", "tx_date": "2026-09-11"},
             ]},
        ),
        (
            "finance_update",
            {"items": [
                {"id": 7, "amount": "200.00"},
                {"id": 8, "notes": "报销"},
            ]},
            {"tool": "finance_update", "policy_key": "R7.finance.update",
             "batch_size": 2, "items": [
                 {"index": 1, "item_id": "7", "label": "2026-09-01 运费",
                  "action": "updated", "id": 7, "desired": {"amount": "200.00"}},
                 {"index": 2, "item_id": "8", "label": "2026-09-02 运费",
                  "action": "updated", "id": 8, "desired": {"notes": "报销"}},
             ]},
        ),
    ],
)
def test_batch_shapes_share_batch_key(tool: str, args_raw: dict, execution: dict) -> None:
    """批量（items ≥ 2）两形态同键，且为显式的 ``{tool}:batch``。"""
    _same_key(tool, args_raw, execution, f"{tool}:batch")


# ---------------------------------------------------------------------------
# 2. items 单元素：prepare 委托旧单条执行器（execution 无 items）
#    → 解包 items[0] 复用旧分支，与「扁平单条」等同键
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "args_items", "args_flat", "execution", "expected"),
    [
        (
            "order_create",
            {"order_type": "metal", "items": [{"customer_name": "铭泰", "model": "M1"}]},
            {"order_type": "metal", "customer_name": "铭泰", "model": "M1"},
            {"tool": "order_create", "order_type": "metal",
             "draft": {"customer_name": "铭泰", "model": "M1"},
             "policy_key": "R2.order.create.metal"},
            "order_create:铭泰",
        ),
        (
            "order_create",
            {"order_type": "powder", "items": [{"supplier_name": "顺达", "color_code": "9010"}]},
            {"order_type": "powder", "supplier_name": "顺达", "color_code": "9010"},
            {"tool": "order_create", "order_type": "powder",
             "draft": {"supplier_name": "顺达", "color_code": "9010"},
             "policy_key": "R2.order.create.powder"},
            "order_create:顺达",
        ),
        (
            "order_update",
            {"order_type": "metal", "items": [{"order_id": 5, "status": "完成"}]},
            {"order_type": "metal", "order_id": 5, "status": "完成"},
            {"tool": "order_update", "order_type": "metal", "order_id": 5,
             "desired": {"status": "完成"}, "policy_key": "R3.order.update.metal"},
            "order_update:5",
        ),
        (
            "finance_create",
            {"items": [{"tx_type": "支出", "category": "运费",
                        "tx_date": "2026-09-10", "amount": "100.00"}]},
            {"tx_type": "支出", "category": "运费",
             "tx_date": "2026-09-10", "amount": "100.00"},
            {"tool": "finance_create", "tx_type": "支出", "category": "运费",
             "tx_date": "2026-09-10", "payload": {}, "policy_key": "R7.finance.create"},
            "finance_create:支出:运费:2026-09-10",
        ),
        (
            "finance_update",
            {"items": [{"id": 7, "amount": "200.00"}]},
            {"id": 7, "amount": "200.00"},
            {"tool": "finance_update", "id": 7, "desired": {"amount": "200.00"},
             "policy_key": "R7.finance.update"},
            "finance_update:7",
        ),
    ],
)
def test_items_single_matches_single_execution(
    tool: str, args_items: dict, args_flat: dict, execution: dict, expected: str
) -> None:
    """items 单元素（args_raw）与无 items（execution/扁平单条）同键。"""
    # items 单元素 与 扁平单条 在 args_raw 侧也应同键（等价简写）
    assert _retry_key(tool, args_items) == _retry_key(tool, args_flat)
    _same_key(tool, args_items, execution, expected)
    _same_key(tool, args_flat, execution, expected)


# ---------------------------------------------------------------------------
# 3. 旧形状分支逐字保留（新增 items 分支不得影响既有工具）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "payload", "expected"),
    [
        ("attendance_plan_execute", {"attendance_date": "2026-09-12"},
         "attendance_plan_execute:2026-09-12"),
        ("reconciliation_execute", {"report_id": "r-1"},
         "reconciliation_execute:r-1"),
        ("order_update", {"order_id": 5}, "order_update:5"),
        ("order_create", {"customer_name": "铭泰"}, "order_create:铭泰"),
        ("order_create", {"draft": {"supplier_name": "顺达"}}, "order_create:顺达"),
        ("finance_create", {"tx_type": "支出", "category": "运费", "tx_date": "2026-09-10"},
         "finance_create:支出:运费:2026-09-10"),
        ("finance_create", {}, "finance_create"),
        ("finance_update", {"id": 7}, "finance_update:7"),
        ("mapping_template", {"action": "create_draft", "template_key": "tk-1"},
         "mapping_template:create_draft:tk-1"),
        ("mapping_template", {"action": "publish", "version_id": "v-9"},
         "mapping_template:publish:v-9"),
        ("mapping_template", {"action": "publish"}, "mapping_template"),
        ("some_write_tool", {}, "some_write_tool"),
    ],
)
def test_legacy_branches_unchanged(tool: str, payload: dict, expected: str) -> None:
    """无 items 的旧形状按键规则逐字保留（新增分支不得改动）。"""
    assert _retry_key(tool, payload) == expected


# ---------------------------------------------------------------------------
# 4. 真实形状同键（[2026-09-12 加固]）
#    args_raw 取真实 pydantic 模型 ``model_dump(exclude_none=True)``、execution
#    取真实 ``prepare_*()`` 输出（非手写形状），逐工具断言两形态同键——防将来
#    模型字段/prepare 载荷形状漂移导致两形态错键（上方 1-3 组为手写形状的
#    定点断言，本组为真实产物的整体守护）。
# ---------------------------------------------------------------------------


class _FakeQueryClient:
    """最小只读假客户端：实现 query 的 filter 等值 + fields 裁剪（真实
    prepare 的只读前置只需此能力；写调用不涉及）。"""

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  **_kw: object) -> ToolResult:
        args = dict(arguments or {})
        rows = self._rows.get(args.get("entity", ""), [])
        filt = args.get("filter") or {}
        items = [dict(r) for r in rows if all(r.get(k) == v for k, v in filt.items())]
        fields = args.get("fields")
        if fields:
            items = [{k: it.get(k) for k in fields} for it in items]
        return ToolResult(ok=True, data={"items": items, "total": len(items)})


def _real_ctx() -> ToolContext:
    rows: dict[str, list[dict]] = {
        "customers": [
            {"id": 3, "company_name": "铭泰", "unit_price": 1.0,
             "price_type": "按面积", "price_unit": "元/平方"},
            {"id": 7, "company_name": "示例", "unit_price": 1.0,
             "price_type": "按面积", "price_unit": "元/平方"},
        ],
        "suppliers": [
            {"id": 4, "name": "顺达"}, {"id": 9, "name": "昌盛"},
        ],
        "metal_orders": [
            {"id": 5, "status": "未完成", "notes": None, "model": "M1",
             "customer_id": 3, "updated_at": "T0"},
            {"id": 9, "status": "未完成", "notes": None, "model": "M2",
             "customer_id": 7, "updated_at": "T1"},
        ],
        "transactions": [
            {"id": 7, "tx_type": "支出", "category": "运费", "project": None,
             "amount": "100.00", "tx_date": "2026-09-10", "notes": None,
             "customer_id": None, "supplier_id": None, "updated_at": "T0"},
            {"id": 8, "tx_type": "支出", "category": "运费", "project": None,
             "amount": "200.00", "tx_date": "2026-09-11", "notes": None,
             "customer_id": None, "supplier_id": None, "updated_at": "T1"},
        ],
        "transaction_categories": [
            {"id": 1, "tx_type": "支出", "category_name": "运费"},
            {"id": 3, "tx_type": "收入", "category_name": "回款"},
        ],
    }
    return ToolContext(client=_FakeQueryClient(rows), tasks=object(), audit=object())


_REAL_SHAPES = [
    (
        "order_create",
        OrderCreateArgs(order_type="metal", items=[
            {"customer_name": "铭泰", "model": "M1", "quantity": 12,
             "in_date": "2026-09-12"},
            {"customer_name": "示例", "model": "M2", "quantity": 5,
             "in_date": "2026-09-12"},
        ]),
        prepare_order_create,
    ),
    (
        "order_update",
        OrderUpdateArgs(order_type="metal", items=[
            {"order_id": 5, "notes": "加急"},
            {"order_id": 9, "notes": "补单"},
        ]),
        prepare_order_update,
    ),
    (
        "finance_create",
        FinanceCreateArgs(items=[
            {"tx_type": "支出", "category": "运费", "tx_date": "2026-09-10",
             "amount": "100.00"},
            {"tx_type": "收入", "category": "回款", "tx_date": "2026-09-11",
             "amount": "50.00"},
        ]),
        prepare_finance_create,
    ),
    (
        "finance_update",
        FinanceUpdateArgs(items=[
            {"id": 7, "amount": "150.00"},
            {"id": 8, "notes": "报销"},
        ]),
        prepare_finance_update,
    ),
]


@pytest.mark.parametrize(("tool", "args", "prepare_fn"), _REAL_SHAPES)
def test_real_prepare_shapes_share_batch_key(tool, args, prepare_fn) -> None:
    """真实 args 模型 + 真实 prepare 输出：批量两形态同键 = ``{tool}:batch``。"""
    prepared = prepare_fn(_real_ctx(), args)
    args_raw = args.model_dump(exclude_none=True)
    # args_raw 侧确为批量形状（items ≥ 2），execution 顶层也带 items
    assert len(args_raw["items"]) >= 2
    assert isinstance(prepared.execution.get("items"), list)

    key_args = _retry_key(tool, args_raw)
    key_exec = _retry_key(tool, prepared.execution)
    assert key_args == key_exec, (
        f"{tool} 真实形状两形态不同键：args_raw={key_args!r} "
        f"execution={key_exec!r}"
    )
    assert key_args == f"{tool}:batch"
