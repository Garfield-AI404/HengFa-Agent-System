"""R2 钣金订单字段提取/补全/校验纯函数测试（RV-19；规格 5.1 L698-715、
5.3、14.3 场景 3、mcp-api.md §6）。

覆盖：输入提取（total_amount 拒绝/数值解析）、客户主数据补全（唯一命中
来源标注/unit_price 0=未定价滤除/多值冲突数据异常/缺失必须询问）、定价
三字段按客户主数据固定口径解析（[2026-09-08 口径修正]：单价/价格类型取
主数据值、weight_unit 由主数据 price_unit 确定性映射（元/kg→kg、元/平方
→m²）、无历史订单推断、price_unit 缺失/异常时兜底询问不默认 kg、用户
0=合法免费价与主数据 0=未定价的语义差异）、默认值（套/当天/重量 0/状态
未完成）、校验错误、14.3 场景 3（缺重量写 0，价格与单位由主数据补全）。
"""

from __future__ import annotations

import pytest

from administrative_agent.business.defaults import (
    DEFAULT_QUANTITY_UNIT,
    DEFAULT_STATUS,
    MISSING_WEIGHT_PER_UNIT,
)
from administrative_agent.business.order_rules import (
    ERR_CUSTOMER_MATCH_REQUIRED,
    ERR_CUSTOMER_PROFILE_MISSING,
    ERR_DATA_CONFLICT,
    ERR_MODEL_REQUIRED,
    ERR_NUMERIC_INVALID,
    ERR_PRICE_NON_NEGATIVE,
    ERR_PRICE_TYPE_INVALID,
    ERR_QUANTITY_POSITIVE,
    ERR_QUANTITY_REQUIRED,
    ERR_QUANTITY_UNIT_INVALID,
    ERR_STATUS_FIXED,
    # [2026-09-08 ERP 附加款上线] 钣金 surcharge_amount 校验错误码
    ERR_SURCHARGE_NON_NEGATIVE,
    ERR_SURCHARGE_PRECISION,
    ERR_TOTAL_AMOUNT_FORBIDDEN,
    ERR_WEIGHT_UNIT_INVALID,
    # [2026-09-08 口径修正] 计价单位映射：元/kg→kg、元/平方→m²
    PRICE_UNIT_TO_WEIGHT_UNIT,
    SOURCE_CUSTOMER_PROFILE,
    SOURCE_DEFAULT,
    SOURCE_USER,
    CustomerProfile,
    MetalOrderError,
    MetalOrderInput,
    complete_metal_order,
    extract_metal_order_input,
    resolve_customer_profile,
)

TODAY = "2026-08-22"
CUSTOMER_ID = 11
CUSTOMER_NAME = "示例机械有限公司"
#: 客户主数据齐备样本（[2026-09-08 口径修正]：计价单位存 price_unit，
#: weight_unit 由映射得出——PROFILE 无 weight_unit 属性）
PROFILE = CustomerProfile(unit_price=150.0, price_type="含税",
                          price_unit="元/kg")


def input(**overrides) -> MetalOrderInput:
    """完整用户输入（全部字段提供），可覆盖。"""
    base = dict(
        customer_name=CUSTOMER_NAME,
        model="MK-01",
        unit_price=120.0,
        price_type="含税",
        weight_unit="kg",
        quantity=2,
        quantity_unit="套",
        weight_per_unit=35.0,
        in_date="2026-08-21",
        status="未完成",
        out_date=None,
        notes="加急",
    )
    base.update(overrides)
    return MetalOrderInput(**base)


def complete(inp: MetalOrderInput, profile: CustomerProfile = PROFILE,
             customer_id: int = CUSTOMER_ID) -> object:
    """complete_metal_order 便捷包装（默认客户唯一命中 + 主数据齐备）。

    [2026-09-08 口径修正]：无 history_rows 参数——定价按客户主数据固定
    口径（5.1），不查历史订单推断。
    """
    return complete_metal_order(
        inp, customer_id=customer_id, customer_name=CUSTOMER_NAME,
        profile=profile, today=TODAY,
    )


def assert_error(excinfo: pytest.ExceptionInfo, code: str) -> None:
    assert isinstance(excinfo.value, MetalOrderError)
    assert excinfo.value.code == code
    assert excinfo.value.message  # 面向用户消息非空


# ---------------------------------------------------------------------------
# 输入提取（extract_metal_order_input）
# ---------------------------------------------------------------------------

def test_extract_empty_input_all_none() -> None:
    """空载荷 → 全部字段 None（进入补全/询问流程）。"""
    parsed = extract_metal_order_input({})
    assert parsed.customer_name is None
    assert parsed.model is None
    assert parsed.unit_price is None
    assert parsed.price_type is None
    assert parsed.weight_unit is None
    assert parsed.quantity is None
    assert parsed.quantity_unit is None
    assert parsed.weight_per_unit is None
    assert parsed.in_date is None
    assert parsed.status is None


def test_extract_full_mapping_parses_values() -> None:
    """完整载荷 → 字段解析（数值转 float/int）。"""
    parsed = extract_metal_order_input({
        "customer_name": CUSTOMER_NAME,
        "model": "MK-01",
        "unit_price": "120",
        "price_type": "含税",
        "weight_unit": "kg",
        "quantity": 2,
        "quantity_unit": "套",
        "weight_per_unit": 35.0,
        "in_date": "2026-08-21",
        "status": "未完成",
    })
    assert parsed.model == "MK-01"
    assert parsed.unit_price == 120.0
    assert parsed.quantity == 2
    assert parsed.weight_per_unit == 35.0
    assert parsed.in_date == "2026-08-21"


def test_extract_quantity_float_integral_ok() -> None:
    """quantity 传整数值的 float（2.0）→ 2（不做浮点数量）。"""
    assert extract_metal_order_input({"quantity": 2.0}).quantity == 2


def test_extract_total_amount_forbidden() -> None:
    """total_amount 出现即拒绝（5.1：禁止传入、由 ERP 计算；fail closed）。"""
    with pytest.raises(MetalOrderError) as excinfo:
        extract_metal_order_input({"model": "MK-01", "total_amount": 1000.0})
    assert_error(excinfo, ERR_TOTAL_AMOUNT_FORBIDDEN)


def test_extract_total_amount_none_still_forbidden() -> None:
    """total_amount 传 None 同样拒绝（键存在即拒绝，不静默丢弃）。"""
    with pytest.raises(MetalOrderError) as excinfo:
        extract_metal_order_input({"total_amount": None})
    assert_error(excinfo, ERR_TOTAL_AMOUNT_FORBIDDEN)


@pytest.mark.parametrize("field,value", [
    ("unit_price", "abc"),
    ("weight_per_unit", "x"),
    ("quantity", "2.5"),       # 数量必须为整数
    ("quantity", True),        # 布尔不是数量
])
def test_extract_numeric_parse_failure(field: str, value: object) -> None:
    """数值字段解析失败 → ERR_NUMERIC_INVALID。"""
    with pytest.raises(MetalOrderError) as excinfo:
        extract_metal_order_input({field: value})
    assert_error(excinfo, ERR_NUMERIC_INVALID)


def test_extract_unknown_keys_ignored() -> None:
    """未知键忽略（透传字段按需扩展，不猜测）。"""
    parsed = extract_metal_order_input({"model": "MK-01", "remark": "任意"})
    assert parsed.model == "MK-01"


# ---------------------------------------------------------------------------
# 必填与基础校验（complete_metal_order）
# ---------------------------------------------------------------------------

def test_model_required() -> None:
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(model=None))
    assert_error(excinfo, ERR_MODEL_REQUIRED)


def test_model_whitespace_only_required() -> None:
    """model 去首尾空格后为空 → 必填（5.1：model 必填）。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(model="   "))
    assert_error(excinfo, ERR_MODEL_REQUIRED)


def test_model_stripped_of_whitespace() -> None:
    """model 首尾空白去除后入载荷。"""
    draft = complete(input(model="  MK-01  "))
    assert draft.model == "MK-01"


def test_customer_match_required() -> None:
    """customer_id 缺失（客户未唯一匹配）→ 数据异常，不创建。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(), customer_id=None)  # type: ignore[arg-type]
    assert_error(excinfo, ERR_CUSTOMER_MATCH_REQUIRED)


def test_quantity_required() -> None:
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(quantity=None))
    assert_error(excinfo, ERR_QUANTITY_REQUIRED)


@pytest.mark.parametrize("quantity", [0, -1])
def test_quantity_must_be_positive(quantity: int) -> None:
    """5.1：quantity > 0（0 与负数拒绝）。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(quantity=quantity))
    assert_error(excinfo, ERR_QUANTITY_POSITIVE)


def test_unit_price_negative_rejected() -> None:
    """mcp-api.md §6：unit_price 默认 0 且 >= 0。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(unit_price=-1.0))
    assert_error(excinfo, ERR_PRICE_NON_NEGATIVE)


def test_weight_per_unit_negative_rejected() -> None:
    """单件重量不能为负。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(weight_per_unit=-1.0))
    assert_error(excinfo, ERR_PRICE_NON_NEGATIVE)


def test_status_fixed_to_unfinished() -> None:
    """5.1：status 创建时固定"未完成"，其他值拒绝。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(status="已完成"))
    assert_error(excinfo, ERR_STATUS_FIXED)


@pytest.mark.parametrize("field,value,code", [
    ("price_type", "含税价", ERR_PRICE_TYPE_INVALID),
    ("weight_unit", "吨", ERR_WEIGHT_UNIT_INVALID),
    ("quantity_unit", "件", ERR_QUANTITY_UNIT_INVALID),
])
def test_choice_fields_enum_validated(
    field: str, value: str, code: str,
) -> None:
    """价格类型/重量单位/数量单位必须是枚举值之一。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(**{field: value}))
    assert_error(excinfo, code)


# ---------------------------------------------------------------------------
# 客户资料补全与字段来源标注（5.1 L715 / L543）
# ---------------------------------------------------------------------------

def test_user_values_win_no_autofill() -> None:
    """用户提供全部值 → 无自动补全（filled 为空），值原样保留。"""
    draft = complete(input())
    assert draft.filled == ()
    assert draft.auto_filled() == ()
    assert draft.unit_price == 120.0
    assert draft.price_type == "含税"
    assert draft.weight_unit == "kg"
    assert draft.quantity_unit == "套"
    assert draft.weight_per_unit == 35.0
    assert draft.in_date == "2026-08-21"
    assert draft.status == DEFAULT_STATUS


def test_profile_fills_price_type_unit_sources() -> None:
    """用户未提供 → 客户资料唯一值补全（source=customer_profile）。"""
    inp = input(unit_price=None, price_type=None, weight_unit=None)
    draft = complete(inp)
    assert draft.unit_price == 150.0
    assert draft.price_type == "含税"
    assert draft.weight_unit == "kg"
    sources = {f.field: f.source for f in draft.filled}
    assert sources == {
        "unit_price": SOURCE_CUSTOMER_PROFILE,
        "price_type": SOURCE_CUSTOMER_PROFILE,
        "weight_unit": SOURCE_CUSTOMER_PROFILE,
    }
    # L543：旧值（用户未提供 → None）与新值
    price_fill = [f for f in draft.filled if f.field == "unit_price"][0]
    assert price_fill.previous_value is None
    assert price_fill.value == 150.0


def test_defaults_fill_quantity_unit_weight_in_date_status() -> None:
    """未提供 → 默认值补全：套 / 重量 0 / 业务当天 / 未完成。"""
    inp = input(
        unit_price=None, price_type=None, weight_unit=None,
        quantity_unit=None, weight_per_unit=None, in_date=None, status=None,
    )
    draft = complete(inp)
    assert draft.quantity_unit == DEFAULT_QUANTITY_UNIT
    assert draft.weight_per_unit == MISSING_WEIGHT_PER_UNIT
    assert draft.in_date == TODAY
    assert draft.status == DEFAULT_STATUS
    sources = {f.field: f.source for f in draft.filled}
    assert sources == {
        "unit_price": SOURCE_CUSTOMER_PROFILE,
        "price_type": SOURCE_CUSTOMER_PROFILE,
        "weight_unit": SOURCE_CUSTOMER_PROFILE,
        "quantity_unit": SOURCE_DEFAULT,
        "weight_per_unit": SOURCE_DEFAULT,
        "in_date": SOURCE_DEFAULT,
        "status": SOURCE_DEFAULT,
    }


def test_scenario3_missing_weight_writes_zero() -> None:
    """14.3 场景 3：缺重量写 0；客户价格/类型/单位由资料唯一查询补全。"""
    inp = MetalOrderInput(
        customer_name=CUSTOMER_NAME, model="MK-02", quantity=1,
    )
    draft = complete(inp)
    assert draft.weight_per_unit == MISSING_WEIGHT_PER_UNIT == 0
    assert draft.unit_price == 150.0          # 资料补全
    assert draft.price_type == "含税"          # 资料补全
    assert draft.weight_unit == "kg"           # 资料补全
    assert draft.quantity_unit == "套"         # 默认
    assert draft.in_date == TODAY              # 默认业务当天
    assert draft.status == DEFAULT_STATUS      # 固定未完成
    fields = {f.field for f in draft.auto_filled()}
    assert fields == {
        "unit_price", "price_type", "weight_unit",
        "quantity_unit", "weight_per_unit", "in_date", "status",
    }


def test_auto_filled_excludes_user_fields() -> None:
    """auto_filled() 只含自动补全项（用户值不入列）。"""
    draft = complete(input(price_type=None))
    assert {f.field for f in draft.auto_filled()} == {"price_type"}
    assert all(f.source != SOURCE_USER for f in draft.auto_filled())


def test_to_write_dict_excludes_total_amount() -> None:
    """写值载荷不含 total_amount（由 ERP 计算）；out_date/notes 可选。"""
    payload = complete(input()).to_write_dict()
    assert "total_amount" not in payload
    assert payload["notes"] == "加急"
    payload2 = complete(input(out_date=None, notes=None)).to_write_dict()
    assert "out_date" not in payload2
    assert "notes" not in payload2
    assert payload2["customer_id"] == CUSTOMER_ID
    assert payload2["model"] == "MK-01"
    assert payload2["weight_per_unit"] == 35.0


# ---------------------------------------------------------------------------
# [2026-09-08 ERP 附加款上线] surcharge_amount（mcp-api.md §6.1）
# ---------------------------------------------------------------------------

def test_extract_surcharge_amount_parses() -> None:
    """提取：surcharge_amount 数字 → float；缺省 None（可选字段）。"""
    out = extract_metal_order_input({
        "customer_name": CUSTOMER_NAME, "model": "MK-01",
        "surcharge_amount": 12.5,
    })
    assert out.surcharge_amount == 12.5
    assert extract_metal_order_input({
        "customer_name": CUSTOMER_NAME, "model": "MK-01",
    }).surcharge_amount is None


def test_complete_surcharge_negative_rejected() -> None:
    """补全校验：附加款 < 0 → ERR_SURCHARGE_NON_NEGATIVE（不落库不创建）。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(surcharge_amount=-1))
    assert_error(excinfo, ERR_SURCHARGE_NON_NEGATIVE)


def test_complete_surcharge_too_many_decimals_rejected() -> None:
    """补全校验：超过 2 位小数（0.001/88.888）→ ERR_SURCHARGE_PRECISION。"""
    for bad in (0.001, 88.888):
        with pytest.raises(MetalOrderError) as excinfo:
            complete(input(surcharge_amount=bad))
        assert_error(excinfo, ERR_SURCHARGE_PRECISION)


def test_complete_surcharge_over_column_width_rejected() -> None:
    """补全校验：超出 decimal(10,2) 列宽（≥ 1e8）→ ERR_SURCHARGE_PRECISION。"""
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(surcharge_amount=100000000.0))
    assert_error(excinfo, ERR_SURCHARGE_PRECISION)


def test_complete_surcharge_non_numeric_rejected() -> None:
    """补全校验：非数字/非有限值 → ERR_NUMERIC_INVALID。"""
    for bad in ("abc", float("nan")):
        with pytest.raises(MetalOrderError) as excinfo:
            complete(input(surcharge_amount=bad))  # type: ignore[arg-type]
        assert_error(excinfo, ERR_NUMERIC_INVALID)


def test_to_write_dict_surcharge_optional() -> None:
    """载荷透传：提供附加款才写入（None 不入载荷 → ERP 默认 0.00）；
    显式 0 也透传（表示无附加款/清除）。"""
    payload = complete(input(surcharge_amount=12.5)).to_write_dict()
    assert payload["surcharge_amount"] == 12.5
    assert "total_amount" not in payload  # 行总额仍由 ERP 计算
    payload_none = complete(input()).to_write_dict()
    assert "surcharge_amount" not in payload_none
    payload_zero = complete(input(surcharge_amount=0)).to_write_dict()
    assert payload_zero["surcharge_amount"] == 0


# ---------------------------------------------------------------------------
# 客户主数据缺失必须询问（不静默兜底；5.1 主数据固定口径，无历史推断）
# ---------------------------------------------------------------------------

def test_profile_missing_price_must_ask_unpriced() -> None:
    """主数据未定价（单价 0 已被滤除为 None）且用户未提供 → 询问，消息注明
    「该客户主数据未定价（单价为 0）」。"""
    empty = CustomerProfile()
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(unit_price=None, price_type=None, weight_unit=None),
                 profile=empty)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)
    assert "该客户主数据未定价（单价为 0）且用户未提供" in excinfo.value.message
    assert "必须询问客户确认" in excinfo.value.message


def test_profile_missing_price_type_must_ask() -> None:
    """主数据缺价格类型、用户未提供 → 询问。"""
    partial = CustomerProfile(unit_price=150.0, price_unit="元/kg")
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(price_type=None, weight_unit=None), profile=partial)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)
    assert "price_type" in excinfo.value.message
    assert "必须询问客户确认" in excinfo.value.message


def test_profile_missing_price_unit_must_ask_not_default_kg() -> None:
    """主数据 price_unit 缺失、用户未提供单位 → 询问（不默认 kg，避免静默
    选 kg）。"""
    partial = CustomerProfile(unit_price=150.0, price_type="含税")
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(weight_unit=None), profile=partial)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)
    assert "price_unit" in excinfo.value.message
    assert "必须询问客户确认" in excinfo.value.message


def test_profile_abnormal_price_unit_asks_not_default() -> None:
    """主数据 price_unit 异常枚举值（如「元/吨」）→ 映射未命中 → 兜底询问
    （不默认 kg，也不把异常值当单位写）。"""
    weird = CustomerProfile(unit_price=150.0, price_type="含税",
                            price_unit="元/吨")
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(weight_unit=None), profile=weird)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)
    assert "price_unit" in excinfo.value.message
    assert "无法确定重量单位" in excinfo.value.message


def test_partial_profile_fills_what_exists() -> None:
    """主数据部分缺失：有值项补全，缺失项仍按用户值/询问处理。"""
    partial = CustomerProfile(unit_price=150.0, price_type="含税")
    draft = complete(input(unit_price=None, price_type=None,
                           weight_unit="kg"), profile=partial)
    assert draft.unit_price == 150.0
    assert draft.price_type == "含税"
    assert draft.weight_unit == "kg"  # 用户提供 → 用户值


# ---------------------------------------------------------------------------
# 定价三字段按客户主数据固定口径补全（[2026-09-08 口径修正]：单价与计价
# 单位是客户主数据绑定的固定口径——unit_price/price_type 直接采用主数据
# 值；weight_unit 由主数据 price_unit 确定性映射（元/kg→kg、元/平方→m²），
# 不查历史订单推断；weight_unit 无普通询问分支，仅 price_unit 缺失/异常
# 枚举值时兜底询问）
# ---------------------------------------------------------------------------

def _customer_source_fields(draft) -> dict:
    """draft.filled 中来源为客户主数据补全的 {field: FilledField}。"""
    return {f.field: f for f in draft.filled if f.source == SOURCE_CUSTOMER_PROFILE}


def test_weight_unit_maps_from_price_unit_per_kg() -> None:
    """主数据 price_unit=元/kg → weight_unit=kg（SOURCE_CUSTOMER_PROFILE）。"""
    profile = CustomerProfile(unit_price=150.0, price_type="含税",
                              price_unit="元/kg")
    draft = complete(input(unit_price=None, price_type=None, weight_unit=None),
                     profile=profile)
    assert draft.unit_price == 150.0
    assert draft.price_type == "含税"
    assert draft.weight_unit == "kg"
    fills = _customer_source_fields(draft)
    assert set(fills) == {"unit_price", "price_type", "weight_unit"}
    assert fills["weight_unit"].value == "kg"
    assert fills["weight_unit"].previous_value is None


def test_weight_unit_maps_from_price_unit_per_square_meter() -> None:
    """主数据 price_unit=元/平方 → weight_unit=m²。"""
    profile = CustomerProfile(unit_price=180.0, price_type="含税",
                              price_unit="元/平方")
    draft = complete(input(unit_price=None, price_type=None, weight_unit=None),
                     profile=profile)
    assert draft.weight_unit == "m²"
    assert draft.unit_price == 180.0
    assert draft.price_type == "含税"


def test_user_weight_unit_wins_over_price_unit_mapping() -> None:
    """用户明确提供单位 → 以用户为准（不按主数据映射覆盖，也不询问）。"""
    profile = CustomerProfile(unit_price=150.0, price_type="含税",
                              price_unit="元/平方")
    draft = complete(input(unit_price=None, price_type=None, weight_unit="kg"),
                     profile=profile)
    assert draft.weight_unit == "kg"  # 用户值（主数据映射本会得 m²）
    assert "weight_unit" not in {f.field for f in draft.filled}


def test_price_unit_not_in_write_payload() -> None:
    """主数据 price_unit 只用于换算 weight_unit，本身不进写载荷。"""
    profile = CustomerProfile(unit_price=150.0, price_type="含税",
                              price_unit="元/平方")
    draft = complete(input(unit_price=None, price_type=None, weight_unit=None),
                     profile=profile)
    assert draft.weight_unit == "m²"
    assert "price_unit" not in draft.to_write_dict()
    assert "weight_unit" in draft.to_write_dict()


def test_user_unit_price_zero_is_free_price_not_missing() -> None:
    """用户显式 unit_price=0 = 合法免费价 → 透传写 0（不视为缺失、不触发
    询问）；对照塑粉：用户 price_per_kg=0 视为「待补充」须询问（5.2）——
    两处 0 语义不同（[2026-09-08 评审后口径]）。"""
    draft = complete(input(unit_price=0.0, price_type="含税",
                           weight_unit="kg"), profile=CustomerProfile())
    assert draft.unit_price == 0.0
    assert draft.to_write_dict()["unit_price"] == 0.0  # 载荷写 0（免费价）
    assert draft.filled == ()          # 三字段均用户提供 → 无自动补全
    assert draft.weight_unit == "kg"


def test_price_unit_mapping_covers_erp_enum() -> None:
    """PRICE_UNIT_TO_WEIGHT_UNIT 恰覆盖 ERP customers.price_unit 枚举两值
    （[2026-09-08 口径修正]：元/kg→kg、元/平方→m²；ERP SqlEnum 仅此两值
    且 NOT NULL 默认「元/kg」，映射必命中——weight_unit 无普通询问分支）。"""
    assert PRICE_UNIT_TO_WEIGHT_UNIT == {"元/kg": "kg", "元/平方": "m²"}


# ---------------------------------------------------------------------------
# 客户主数据多值冲突（5.1：数据异常，不能任选）
# ---------------------------------------------------------------------------

def test_profile_unique_values() -> None:
    """主数据行唯一值 → 取该值。"""
    rows = [{"unit_price": 150.0, "price_type": "含税", "price_unit": "元/kg"}]
    profile = resolve_customer_profile(rows)
    assert profile.unit_price == 150.0
    assert profile.price_type == "含税"
    assert profile.price_unit == "元/kg"
    # weight_unit 不存在于 profile——由 price_unit 在补全层映射得出（5.1）
    assert not hasattr(profile, "weight_unit")


def test_profile_duplicate_same_values_single() -> None:
    """多行同值 → 去重取唯一值。"""
    rows = [
        {"unit_price": 150.0, "price_type": "含税", "price_unit": "元/kg"},
        {"unit_price": 150.0, "price_type": "含税", "price_unit": "元/kg"},
    ]
    profile = resolve_customer_profile(rows)
    assert profile.unit_price == 150.0
    assert profile.price_unit == "元/kg"


def test_profile_price_unit_empty_string_treated_missing() -> None:
    """price_unit 空串/空白 → 视同缺失（None）；异常枚举值保留原样供补全层
    判定（映射未命中 → 询问兜底，不默认 kg）。"""
    assert resolve_customer_profile([{"price_unit": ""}]).price_unit is None
    assert resolve_customer_profile([{"price_unit": "  "}]).price_unit is None
    assert resolve_customer_profile([{"price_unit": "元/吨"}]).price_unit \
        == "元/吨"


def test_profile_price_conflict_is_data_error() -> None:
    """单价多值冲突 → ERR_DATA_CONFLICT（数据异常，不能任选）。"""
    rows = [{"unit_price": 150.0}, {"unit_price": 160.0}]
    with pytest.raises(MetalOrderError) as excinfo:
        resolve_customer_profile(rows)
    assert_error(excinfo, ERR_DATA_CONFLICT)
    assert "unit_price" in excinfo.value.message


def test_profile_price_type_conflict_is_data_error() -> None:
    rows = [{"price_type": "含税"}, {"price_type": "不含税"}]
    with pytest.raises(MetalOrderError) as excinfo:
        resolve_customer_profile(rows)
    assert_error(excinfo, ERR_DATA_CONFLICT)


def test_profile_price_unit_conflict_is_data_error() -> None:
    """计价单位（price_unit）多值冲突 → ERR_DATA_CONFLICT（不能任选换算）。"""
    rows = [{"price_unit": "元/kg"}, {"price_unit": "元/平方"}]
    with pytest.raises(MetalOrderError) as excinfo:
        resolve_customer_profile(rows)
    assert_error(excinfo, ERR_DATA_CONFLICT)
    assert "price_unit" in excinfo.value.message


def test_profile_all_missing_returns_none_fields() -> None:
    """主数据三项全缺失 → None（进入询问流程，不猜测）。"""
    profile = resolve_customer_profile([{"unit_price": None}, {}])
    assert profile.unit_price is None
    assert profile.price_type is None
    assert profile.price_unit is None


# ---------------------------------------------------------------------------
# 主数据 unit_price=0 = 未定价（[2026-09-08 评审后口径]：ERP customers.
# unit_price NOT NULL 默认 0，查询永不返回 None；0 视同缺失进入询问，
# 与塑粉对 0 的语义一致；对照：用户显式 0 = 合法免费价可写）
# ---------------------------------------------------------------------------

def test_profile_unit_price_zero_is_unpriced_and_filtered() -> None:
    """主数据 unit_price 以 0.0/0.0000 形态出现 → 滤除为 None（0=未定价，
    不是客户真实单价，不得采用）。"""
    assert resolve_customer_profile(
        [{"unit_price": 0.0, "price_type": "含税"}]
    ).unit_price is None
    assert resolve_customer_profile([{"unit_price": "0.0000"}]).unit_price is None
    # 0 与真实价并存 → 滤 0 后取真实唯一价
    profile = resolve_customer_profile(
        [{"unit_price": 0.0}, {"unit_price": "150.0000"}]
    )
    assert profile.unit_price == 150.0
    # price_type 无 0 语义（默认「含税」是合法默认值），原样保留
    assert resolve_customer_profile(
        [{"unit_price": 0.0, "price_type": "含税"}]
    ).price_type == "含税"
    # 0 价行不能被 continue 整行跳过——同行的 price_unit 仍须收集
    assert resolve_customer_profile(
        [{"unit_price": 0.0, "price_type": "含税", "price_unit": "元/kg"}]
    ).price_unit == "元/kg"


def test_unpriced_profile_asks_with_zero_message() -> None:
    """主数据 0=未定价（滤为 None）+ 用户未提供单价 → 询问，消息含
    「未定价（单价为 0）」，请用户确认是否免费或提供单价。"""
    unpriced = resolve_customer_profile(
        [{"unit_price": 0.0, "price_type": "含税", "price_unit": "元/kg"}]
    )
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(unit_price=None), profile=unpriced)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)
    assert "该客户主数据未定价（单价为 0）" in excinfo.value.message
    assert "必须询问客户确认" in excinfo.value.message


def test_unpriced_price_keeps_other_profile_fields() -> None:
    """主数据未定价只影响单价：同行的 price_type/price_unit 正常收集（单价
    补全失败时先抛询问，其余字段照常进入确认流程的候选）。"""
    unpriced = resolve_customer_profile(
        [{"unit_price": 0.0, "price_type": "含税", "price_unit": "元/平方"}]
    )
    assert unpriced.unit_price is None
    assert unpriced.price_type == "含税"
    assert unpriced.price_unit == "元/平方"
    with pytest.raises(MetalOrderError) as excinfo:
        complete(input(unit_price=None), profile=unpriced)
    assert_error(excinfo, ERR_CUSTOMER_PROFILE_MISSING)  # 单价未定价 → 询问


# ---------------------------------------------------------------------------
# 错误语义
# ---------------------------------------------------------------------------

def test_metal_order_error_is_value_error() -> None:
    """异常继承 ValueError（面向用户消息 + 稳定错误码）。"""
    err = MetalOrderError(ERR_MODEL_REQUIRED, "model 必填")
    assert isinstance(err, ValueError)
    assert err.code == ERR_MODEL_REQUIRED
    assert err.message == "model 必填"
    assert str(err) == "model 必填"


# ===========================================================================
# 塑粉订单（RV-21/RV-22；规格 5.2 L717-738 / 14.3 场景 4-6）
# ===========================================================================

from administrative_agent.business.order_rules import (  # noqa: E402
    ERR_POWDER_COLOR_CODE_REQUIRED,
    ERR_POWDER_HISTORY_MISMATCH,
    ERR_POWDER_PRICE_AMBIGUOUS,
    ERR_POWDER_PRICE_MISSING,
    ERR_POWDER_PRICE_NEGATIVE,
    # [2026-09-08 ERP 附加款上线] 塑粉无附加款：键出现即拒
    ERR_POWDER_SURCHARGE_FORBIDDEN,
    ERR_POWDER_SUPPLIER_MATCH_REQUIRED,
    ERR_POWDER_WEIGHT_REQUIRED,
    POWDER_WEIGHT_UNIT,
    PowderOrderError,
    PowderOrderInput,
    complete_powder_order,
    extract_powder_order_input,
    resolve_powder_price,
)

SUPPLIER_ID = 101
SUPPLIER_NAME = "示例塑粉材料有限公司"
COLOR = "R-01"


def powder_input(**overrides) -> PowderOrderInput:
    """完整塑粉用户输入（全部字段提供），可覆盖。"""
    base = dict(
        supplier_name=SUPPLIER_NAME,
        color_code=COLOR,
        total_weight=50.0,
        price_per_kg=28.5,
        in_date="2026-08-21",
        notes=None,
    )
    base.update(overrides)
    return PowderOrderInput(**base)


def complete_powder(inp: PowderOrderInput, history: list | None = None,
                    supplier_id: int = SUPPLIER_ID) -> object:
    """complete_powder_order 便捷包装（默认供货商唯一命中 + 无历史价）。"""
    return complete_powder_order(
        inp, supplier_id=supplier_id, supplier_name=SUPPLIER_NAME,
        history_rows=history or [], today=TODAY,
    )


def assert_powder_error(excinfo: pytest.ExceptionInfo, code: str) -> None:
    assert isinstance(excinfo.value, PowderOrderError)
    assert excinfo.value.code == code


# ---- 输入提取（5.2 字段表） ----

def test_powder_extract_total_amount_forbidden() -> None:
    """total_amount 键出现即拒绝（fail closed，D13 先例）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        extract_powder_order_input({"color_code": COLOR, "total_amount": 100.0})
    assert_powder_error(excinfo, ERR_TOTAL_AMOUNT_FORBIDDEN)


def test_powder_extract_numeric_invalid() -> None:
    """数值字段解析失败 → PowderOrderError（非字符串/非数字）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        extract_powder_order_input({"total_weight": "五十"})
    assert_powder_error(excinfo, ERR_NUMERIC_INVALID)
    with pytest.raises(PowderOrderError):
        extract_powder_order_input({"price_per_kg": True})


def test_powder_extract_optional_fields() -> None:
    """提取：缺失为 None，未知键忽略，notes 透传。"""
    inp = extract_powder_order_input(
        {"supplier_name": SUPPLIER_NAME, "color_code": COLOR,
         "total_weight": 50.0, "unknown_key": 1}
    )
    assert inp.supplier_name == SUPPLIER_NAME
    assert inp.total_weight == 50.0
    assert inp.price_per_kg is None
    assert inp.notes is None


def test_powder_extract_surcharge_amount_forbidden() -> None:
    """[2026-09-08 ERP 附加款] 塑粉订单没有附加款字段：surcharge_amount
    真实值（12.5）传入即拒（fail closed，避免工具/白名单之外的键静默
    丢失）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        extract_powder_order_input({"color_code": COLOR, "surcharge_amount": 12.5})
    assert_powder_error(excinfo, ERR_POWDER_SURCHARGE_FORBIDDEN)


def test_powder_extract_surcharge_zero_still_forbidden() -> None:
    """[2026-09-08 回归修复] 0 是显式传入的非 None 值，仍须拒绝（判定是
    is not None 而非 truthy——0 不能借 truthy 漏洞绕过 fail closed）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        extract_powder_order_input({"color_code": COLOR, "surcharge_amount": 0})
    assert_powder_error(excinfo, ERR_POWDER_SURCHARGE_FORBIDDEN)


def test_powder_extract_none_surcharge_key_allowed() -> None:
    """[2026-09-08 回归修复] 等价 order_create 真实上游链路：OrderCreateArgs
    metal/powder 共用一份 schema，仅填 powder 字段后 args.model_dump()
    必带全部 None 默认键（surcharge_amount 及 metal 侧 customer_name/
    model/unit_price/...）——surcharge_amount 值为 None 属默认键，放行不
    误拒（修复前生产上所有塑粉订单创建被 ERR_POWDER_SURCHARGE_FORBIDDEN
    拦下；真值由 OrderCreateArgs 冲突校验兜底，business 层只兜非 None）。"""
    raw = {
        "order_type": "powder",
        "customer_name": None,
        "model": None,
        "unit_price": None,
        "price_type": None,
        "weight_unit": None,
        "quantity": None,
        "quantity_unit": None,
        "weight_per_unit": None,
        # 本修复核心：None 默认键必须放行（is not None 判定）
        "surcharge_amount": None,
        "supplier_name": SUPPLIER_NAME,
        "color_code": COLOR,
        "total_weight": 50.0,
        "price_per_kg": 28.5,
        "in_date": "2026-08-21",
        "out_date": None,
        "status": None,
        "notes": None,
    }
    inp = extract_powder_order_input(raw)
    assert inp.supplier_name == SUPPLIER_NAME
    assert inp.color_code == COLOR
    assert inp.total_weight == 50.0
    assert inp.price_per_kg == 28.5
    assert inp.in_date == "2026-08-21"
    assert inp.notes is None


# ---- 供货商与色号（5.2） ----

def test_powder_supplier_match_required() -> None:
    """供货商未唯一匹配 → 不创建（5.2：supplier_id 唯一查询）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(), supplier_id=None)
    assert_powder_error(excinfo, ERR_POWDER_SUPPLIER_MATCH_REQUIRED)


def test_powder_color_code_required() -> None:
    """color_code 必填（用户必须提供，去首尾空格非空）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(color_code=None))
    assert_powder_error(excinfo, ERR_POWDER_COLOR_CODE_REQUIRED)
    with pytest.raises(PowderOrderError):
        complete_powder(powder_input(color_code="   "))


def test_powder_color_code_stripped() -> None:
    """色号写入前去首尾空格（mcp-api §6 L224）。"""
    draft = complete_powder(powder_input(color_code="  R-01  "))
    assert draft.color_code == "R-01"


# ---- 重量校验（RV-21 通过标准：重量缺失或 0 不创建）----

def test_powder_weight_missing_not_created() -> None:
    """重量缺失 → 直接询问，不创建（14.3 场景 4，L738 比 ERP 更严格）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(total_weight=None))
    assert_powder_error(excinfo, ERR_POWDER_WEIGHT_REQUIRED)


def test_powder_weight_zero_not_created() -> None:
    """重量 0 → 不创建（0 不能表示待补充；与钣金缺重量写 0 口径不同）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(total_weight=0.0))
    assert_powder_error(excinfo, ERR_POWDER_WEIGHT_REQUIRED)


def test_powder_weight_negative_not_created() -> None:
    """重量为负 → 不创建。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(total_weight=-1.0))
    assert_powder_error(excinfo, ERR_POWDER_WEIGHT_REQUIRED)


# ---- 价格补全三分支（5.2 算法 / 14.3 场景 4-6） ----

def test_powder_price_unique_history_autofill() -> None:
    """唯一非零历史价 → 自动补全（SOURCE_HISTORY_PRICE，14.3 场景 5）。"""
    history = [
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 28.5},
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 28.5},
    ]
    draft = complete_powder(powder_input(price_per_kg=None), history=history)
    assert draft.price_per_kg == 28.5
    by_field = {f.field: f for f in draft.filled}
    assert by_field["price_per_kg"].source == "history_price"
    assert by_field["price_per_kg"].value == 28.5


def test_powder_price_no_history_asks() -> None:
    """无历史价格 → 询问用户，不创建（14.3 场景 4，L738 第 4 条）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=None))
    assert_powder_error(excinfo, ERR_POWDER_PRICE_MISSING)


def test_powder_price_multiple_asks() -> None:
    """多个不同历史价格 → 询问停止，不创建（14.3 场景 6，第 5 条）。"""
    history = [
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 25.0},
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 30.0},
    ]
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=None), history=history)
    assert_powder_error(excinfo, ERR_POWDER_PRICE_AMBIGUOUS)
    assert "25.0" in excinfo.value.message and "30.0" in excinfo.value.message


def test_powder_price_user_value_wins() -> None:
    """用户提供 >0 价格 → 直接采用，不查历史（即使用户值不同）。"""
    history = [{"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 25.0}]
    draft = complete_powder(powder_input(price_per_kg=31.0), history=history)
    assert draft.price_per_kg == 31.0
    assert "price_per_kg" not in {f.field for f in draft.filled}


def test_powder_price_zero_treated_as_missing() -> None:
    """用户传 0 → 视为未提供（0 表示待补充，不能写入）→ 走历史补全。"""
    history = [{"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 26.0}]
    draft = complete_powder(powder_input(price_per_kg=0.0), history=history)
    assert draft.price_per_kg == 26.0  # 历史唯一价补全
    with pytest.raises(PowderOrderError):
        complete_powder(powder_input(price_per_kg=0.0))  # 无历史 → 询问


def test_powder_price_negative_rejected() -> None:
    """价格为负 → 拒绝（mcp-api §6 L226 不得小于 0）。"""
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=-1.0))
    assert_powder_error(excinfo, ERR_POWDER_PRICE_NEGATIVE)


def test_powder_history_zero_prices_ignored() -> None:
    """历史价 0 表示待补充：全零 → 视为无历史价（询问）。"""
    history = [
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 0},
        {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 0},
    ]
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=None), history=history)
    assert_powder_error(excinfo, ERR_POWDER_PRICE_MISSING)


# ---- 不跨供货商/跨色号推断（算法第 6 条） ----

def test_powder_history_mismatch_supplier_is_error() -> None:
    """历史行供货商与请求不符 → 数据异常（不跨供货商推断价格）。"""
    history = [{"supplier_id": 999, "color_code": COLOR, "price_per_kg": 25.0}]
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=None), history=history)
    assert_powder_error(excinfo, ERR_POWDER_HISTORY_MISMATCH)


def test_powder_history_mismatch_color_is_error() -> None:
    """历史行色号与请求不符 → 数据异常（不跨色号推断价格）。"""
    history = [{"supplier_id": SUPPLIER_ID, "color_code": "B-02", "price_per_kg": 25.0}]
    with pytest.raises(PowderOrderError) as excinfo:
        complete_powder(powder_input(price_per_kg=None), history=history)
    assert_powder_error(excinfo, ERR_POWDER_HISTORY_MISMATCH)


def test_powder_resolve_uses_only_scope_rows() -> None:
    """resolve_powder_price 只对传入（该供货商 + 色号）行做去重决策。"""
    price = resolve_powder_price(
        [
            {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 27.0},
            {"supplier_id": SUPPLIER_ID, "color_code": COLOR, "price_per_kg": 27.0},
        ],
        supplier_id=SUPPLIER_ID,
        color_code=COLOR,
    )
    assert price == 27.0


# ---- 默认值与载荷（5.2/mcp-api §6） ----

def test_powder_in_date_defaults_to_today() -> None:
    """in_date 未提供 → 默认业务当天（SOURCE_DEFAULT）。"""
    draft = complete_powder(powder_input(in_date=None, price_per_kg=25.0))
    assert draft.in_date == TODAY
    by_field = {f.field: f for f in draft.filled}
    assert by_field["in_date"].source == SOURCE_DEFAULT


def test_powder_draft_to_write_dict_excludes_forbidden() -> None:
    """写值载荷：塑粉五字段，不含 total_amount/weight_unit/status。"""
    draft = complete_powder(
        powder_input(price_per_kg=25.0), history=None
    )
    payload = draft.to_write_dict()
    assert payload["supplier_id"] == SUPPLIER_ID
    assert payload["color_code"] == COLOR
    assert payload["total_weight"] == 50.0
    assert payload["price_per_kg"] == 25.0
    assert payload["in_date"] == "2026-08-21"
    assert "total_amount" not in payload
    assert "weight_unit" not in payload  # 固定 kg 不传单位字段（5.2）
    assert "status" not in payload  # mcp-api 塑粉字段表无 status
    assert POWDER_WEIGHT_UNIT == "kg"


def test_powder_error_is_value_error() -> None:
    """PowderOrderError 继承 ValueError（与 MetalOrderError 同风格）。"""
    err = PowderOrderError(ERR_POWDER_PRICE_MISSING, "无历史价格")
    assert isinstance(err, ValueError)
    assert err.code == ERR_POWDER_PRICE_MISSING
