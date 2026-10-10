"""[2026-09-08 口径修正] prepare_order_create(metal) 工具层测试；
[2026-09-08 回归修复] 追加 prepare_order_create(powder) 同链路回归
（surcharge_amount=None 默认键不误拒）。

背景（生产事故复现路径）：老客户（如昌盛）创建钣金订单曾红卡
「客户资料缺少 weight_unit，且用户未提供，必须询问客户确认」。
定稿口径：单价与计价单位是客户主数据**绑定的固定口径**（5.1）——
ERP customers 以 price_unit 承载计价单位（枚举「元/kg」「元/平方」，
NOT NULL 默认「元/kg」），weight_unit 由 price_unit 确定性映射得出
（元/kg→kg、元/平方→m²）；**不查历史金属订单推断**。上一版「按该
客户历史金属订单推断补定价字段」机制已整体撤除（resolve_history_
profile / ERR_HISTORY_AMBIGUOUS / history_rows 参数 / metal_orders
历史查询均已移除）。

资料行按真实 ERP 形态建模：
- customers.unit_price NOT NULL 默认 0——0=未定价（resolve_customer_
  profile 滤除 → 询问，消息注明「该客户主数据未定价（单价为 0）」）；
- customers.price_type 默认「含税」是合法默认值（资料覆盖价格类型）；
- customers.price_unit 必有枚举值（重量单位权威来源——weight_unit 由
  其映射，不查询 customers 不存在的 weight_unit 字段）。

覆盖：
- 用户未提供三定价字段 → draft 值全部来自客户主数据：unit_price/
  price_type 取资料值，weight_unit 由 price_unit 映射（kg 与 m² 两条
  路径），来源标注 customer_profile，确认卡一致；
- 用户明确提供单位 → 以用户为准，不被主数据映射覆盖；
- 查询序列恰为 customers×2（唯一匹配 + 资料查询）、零 metal_orders
  查询（断言）；资料查询 fields 必须含 price_unit；
- price_unit 只用于换算，不进写载荷；
- 主数据未定价（unit_price=0）+ 用户未提供单价 → ToolPreparationError
  消息含「未定价（单价为 0）」，必须询问（不落库）；
- prepare 全程零写调用（只读）；
- [2026-09-08 回归修复] powder 全链路回归：只填 powder 字段的
  OrderCreateArgs 走 prepare 真实链路（args.model_dump() → extract →
  补全 → 确认卡），surcharge_amount=None 等全部 None 默认键不得触发
  ERR_POWDER_SURCHARGE_FORBIDDEN 误拒（生产回归：附加款上线后所有塑粉
  订单创建被拦；business 层单测只覆盖 12.5 拒绝分支、prepare 历史测试
  全为 metal 用例，均漏此链路）。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.tools.base import (
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
)
from administrative_agent.agent.tools.orders import (
    OrderCreateArgs,
    prepare_order_create,
)

CUSTOMER_ID = 11
CUSTOMER_NAME = "昌盛喷涂有限公司"

#: powder 回归用例供货商（5.2：supplier_id 按供货商名称唯一精确匹配）
SUPPLIER_ID = 101
SUPPLIER_NAME = "示例塑粉材料有限公司"

#: 客户主数据齐备（计价 kg）：unit_price=150 / price_type 含税 /
#: price_unit 元/kg → weight_unit 映射为 kg
CUSTOMER_PRICED_KG = {
    "id": CUSTOMER_ID,
    "company_name": CUSTOMER_NAME,
    "unit_price": "150.0000",
    "price_type": "含税",
    "price_unit": "元/kg",
}

#: 客户主数据齐备（计价按平方）：price_unit 元/平方 → weight_unit 映射 m²
CUSTOMER_PRICED_M2 = {
    "id": CUSTOMER_ID,
    "company_name": CUSTOMER_NAME,
    "unit_price": 180.0,
    "price_type": "含税",
    "price_unit": "元/平方",
}

#: 该客户主数据未定价（真实 ERP 形态）：customers.unit_price NOT NULL
#: 默认 0 = 未定价（滤除为 None → 单价必须询问）；price_type 默认「含税」
#: 合法采用；price_unit 正常（元/kg）
CUSTOMER_UNPRICED = {
    "id": CUSTOMER_ID,
    "company_name": CUSTOMER_NAME,
    "unit_price": 0.0,
    "price_type": "含税",
    "price_unit": "元/kg",
}


class _FakeErp:
    """prepare 只读 fake：按 entity 返回预置行；等值 filter + fields 裁剪。

    记录每次 call_tool 参数（供查询序列/字段断言——「零 metal_orders
    查询」「资料查询含 price_unit」依赖此记录）。
    """

    def __init__(
        self,
        customers: list[dict] | None = None,
        suppliers: list[dict] | None = None,
    ) -> None:
        rows = {"customers": customers or []}
        if suppliers is not None:
            rows["suppliers"] = suppliers
        self._rows = rows
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, args: dict):
        self.calls.append((name, dict(args)))
        assert name == "query"
        rows = self._rows[args["entity"]]
        filt = args.get("filter") or {}
        items = [
            dict(r) for r in rows
            if all(r.get(k) == v for k, v in filt.items())
        ]
        fields = args.get("fields")
        if fields:
            items = [{k: it[k] for k in fields if k in it} for it in items]

        class _Result:
            ok = True
            data = {"items": items}

        return _Result()


def _ctx(erp: _FakeErp) -> ToolContext:
    """prepare 只经 _require_wiring 校验 client/tasks/audit 非 None。"""
    return ToolContext(client=erp, tasks=object(), audit=object())


def _query_entities(erp: _FakeErp) -> list[str]:
    return [args["entity"] for name, args in erp.calls if name == "query"]


def _profile_query_args(erp: _FakeErp) -> dict:
    """客户资料（第二次 customers）查询的参数（无则断言失败）。"""
    customer_queries = [
        args for _, args in erp.calls if args.get("entity") == "customers"
    ]
    assert len(customer_queries) == 2, "查询序列应为 唯一匹配 + 资料查询"
    return customer_queries[1]


def _assert_only_reads(erp: _FakeErp) -> None:
    assert all(name == "query" for name, _ in erp.calls)  # prepare 只读


def test_metal_create_prices_from_customer_master_data() -> None:
    """三定价字段均缺 → 全部按客户主数据固定口径补全（不查历史订单）：
    unit_price/price_type 取资料值，weight_unit 由 price_unit 映射 kg。"""
    erp = _FakeErp(customers=[CUSTOMER_PRICED_KG])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="855", quantity=3),
    )

    assert isinstance(result, PreparedToolWrite)
    draft = result.execution["draft"]
    assert draft["customer_id"] == CUSTOMER_ID
    assert draft["unit_price"] == 150.0          # "150.0000" 归一 float
    assert draft["price_type"] == "含税"          # 主数据默认值
    assert draft["weight_unit"] == "kg"           # price_unit 元/kg → kg
    assert draft["quantity"] == 3
    fills = {f["field"]: f["source"] for f in draft["filled"]}
    assert fills["unit_price"] == "customer_profile"
    assert fills["price_type"] == "customer_profile"
    assert fills["weight_unit"] == "customer_profile"
    assert "history_order" not in fills.values()   # 无历史推断来源
    # 确认卡自动补全项含来源 token（展示层本地化字段名，source 原样入卡）
    card = result.card
    auto = {f["field"]: f["source"] for f in card["auto_filled"]}
    assert auto["单价"] == "customer_profile"
    assert auto["重量单位"] == "customer_profile"
    assert auto["价格类型"] == "customer_profile"
    assert result.payload_hash and card["payload_hash"] == result.payload_hash
    # 调用序：客户唯一匹配 → 客户资料；零 metal_orders 查询；零写调用
    assert _query_entities(erp) == ["customers", "customers"]
    _assert_only_reads(erp)


def test_metal_create_weight_unit_maps_square_meter() -> None:
    """主数据 price_unit=元/平方 → draft.weight_unit=m²（映射另一分支）。"""
    erp = _FakeErp(customers=[CUSTOMER_PRICED_M2])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="855", quantity=3),
    )
    draft = result.execution["draft"]
    assert draft["weight_unit"] == "m²"
    assert draft["unit_price"] == 180.0
    # 资料查询必须携带 price_unit（映射的输入字段）
    q = _profile_query_args(erp)
    assert q["fields"] == ["id", "unit_price", "price_type", "price_unit"]
    assert q["filter"] == {"id": CUSTOMER_ID}
    assert _query_entities(erp) == ["customers", "customers"]
    _assert_only_reads(erp)


def test_metal_create_user_weight_unit_wins_over_mapping() -> None:
    """用户明确提供单位 → 以用户为准（不被主数据 price_unit 映射覆盖）。"""
    erp = _FakeErp(customers=[CUSTOMER_PRICED_KG])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="855", weight_unit="m²", quantity=3),
    )
    draft = result.execution["draft"]
    assert draft["weight_unit"] == "m²"          # 用户值（映射本会得 kg）
    assert draft["unit_price"] == 150.0          # 单价仍主数据补全
    fills = {f["field"] for f in draft["filled"]}
    assert "weight_unit" not in fills            # 用户值不入自动补全
    assert _query_entities(erp) == ["customers", "customers"]
    _assert_only_reads(erp)


def test_metal_create_price_unit_not_in_write_payload() -> None:
    """price_unit 只用于换算 weight_unit，不进入写载荷与卡片字段。"""
    erp = _FakeErp(customers=[CUSTOMER_PRICED_M2])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="855", quantity=3),
    )
    draft = result.execution["draft"]
    assert draft["weight_unit"] == "m²"
    assert "price_unit" not in draft             # draft 无该键（非写字段）
    fields = dict(result.card["fields_to_write"])
    assert "price_unit" not in fields
    _assert_only_reads(erp)


def test_metal_create_unpriced_customer_asks_no_history_query() -> None:
    """主数据未定价（unit_price=0=未定价，滤除）+ 用户未提供单价 → 必须
    询问（ToolPreparationError 消息注明「该客户主数据未定价（单价为 0）」）；
    不查历史订单、零写调用。"""
    erp = _FakeErp(customers=[CUSTOMER_UNPRICED])
    with pytest.raises(ToolPreparationError) as excinfo:
        prepare_order_create(
            _ctx(erp),
            OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                            model="855", quantity=3),
        )
    message = str(excinfo.value)
    assert "该客户主数据未定价（单价为 0）且用户未提供" in message
    assert "必须询问客户确认" in message
    assert "订单创建准备失败（客户" in message
    # 未定价只影响单价：price_type（含税）与 price_unit（元/kg）照常收集，
    # 但单价必须询问 → prepare 整体失败，无 metal_orders 查询、不落库
    assert _query_entities(erp) == ["customers", "customers"]
    _assert_only_reads(erp)


def test_metal_create_fully_user_covered_no_profile_gap() -> None:
    """用户提供三定价字段 → 即使主数据未定价也不询问；仍只查 匹配 + 资料
    两次 customers，自动补全仅剩四个默认项（数量单位/单重/日期/状态）。"""
    erp = _FakeErp(customers=[CUSTOMER_UNPRICED])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="metal", customer_name=CUSTOMER_NAME,
                        model="855", unit_price=120.0, price_type="不含税",
                        weight_unit="kg", quantity=3),
    )
    draft = result.execution["draft"]
    assert draft["unit_price"] == 120.0          # 用户值
    assert draft["price_type"] == "不含税"        # 用户值
    assert draft["weight_unit"] == "kg"          # 用户值
    # 三定价字段均用户提供 → 无资料补全；只剩四个默认补全项
    assert [f["source"] for f in draft["filled"]] \
        == ["default", "default", "default", "default"]
    assert _query_entities(erp) == ["customers", "customers"]
    _assert_only_reads(erp)


# ---- [2026-09-08 回归修复] powder 全链路（surcharge None 键不误拒）----

def test_powder_create_prepare_full_args_model_dump_not_rejected() -> None:
    """[2026-09-08 回归修复] 只填 powder 字段的 OrderCreateArgs 走真实
    prepare 链路不得被 ERR_POWDER_SURCHARGE_FORBIDDEN 误拦：prepare 内部
    把 args.model_dump() 原样喂 extract_powder_order_input——共用 schema
    必带 surcharge_amount=None 及 metal 侧全部 None 默认键；修复前按
    「键存在」判拒 → 生产上所有塑粉订单创建报「执行准备未通过…附加款仅
    钣金订单支持，不允许传入」。用户已提供 price_per_kg → 不查历史价，
    查询序列恰为 suppliers 一次；全程零写调用。"""
    erp = _FakeErp(suppliers=[{"id": SUPPLIER_ID, "name": SUPPLIER_NAME}])
    result = prepare_order_create(
        _ctx(erp),
        OrderCreateArgs(order_type="powder", supplier_name=SUPPLIER_NAME,
                        color_code="R-01", total_weight=50.0,
                        price_per_kg=28.5),
    )
    assert isinstance(result, PreparedToolWrite)
    draft = result.execution["draft"]
    assert draft["supplier_id"] == SUPPLIER_ID
    assert draft["supplier_name"] == SUPPLIER_NAME
    assert draft["color_code"] == "R-01"
    assert draft["total_weight"] == 50.0
    assert draft["price_per_kg"] == 28.5
    assert draft["notes"] is None
    # 确认卡与执行载荷 hash 一致（批准卡 = 执行载荷）
    assert result.card["payload_hash"] == result.payload_hash
    assert _query_entities(erp) == ["suppliers"]
    _assert_only_reads(erp)
