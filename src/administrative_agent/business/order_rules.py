"""R2 钣金/塑粉订单字段提取、补全与校验纯规则（规格 5.1/5.2/5.3、
14.3 场景 3-6）。

5.1 钣金字段表（L698-715）：
- customer_id 按客户名称唯一查询（matching.match_customer）；
- model 必填（去首尾空格非空）；
- 钣金定价三字段（unit_price/price_type/weight_unit）按客户主数据固定
  口径解析（单价与计价单位是客户主数据绑定的
  固定口径，不查历史订单推断）：
  用户原话提供 > ERP 客户主数据（customers.unit_price / price_type /
  price_unit）> 询问。unit_price/price_type 直接采用主数据值；
  weight_unit 由主数据 price_unit 确定性映射（PRICE_UNIT_TO_WEIGHT_UNIT：
  元/kg→kg、元/平方→m²；ERP 枚举仅此两值且 NOT NULL，映射必命中——
  weight_unit 无普通询问分支，仅 price_unit 缺失/异常枚举值时兜底询问，
  不默认 kg）；用户明确提供单位（kg/m²）时以用户为准（普通优先级）；
- unit_price 的「0」有两处语义差异（须区分）：
  ① 客户主数据 unit_price=0 =「未定价/未记录」——真实 ERP
     customers.unit_price 为 NOT NULL 默认 0（查询永不返回 None，
     以 0.0/0.0000 形态出现），resolve_customer_profile 对 0 视同缺失
     滤除（与塑粉 resolve_powder_price 对 0 的语义一致），落到询问分支
     时消息注明「该客户主数据未定价（单价为 0）且用户未提供，必须询问
     客户确认」；
  ② 用户对话显式给 unit_price=0 = 合法免费价 → 允许写 0（_resolve_price
     用户分支透传）；对照塑粉：用户 price_per_kg 提供 0 =「待补充」须
     询问（5.2 与钣金口径不同）。
- 客户资料同一字段多值冲突 → 数据异常（ERR_DATA_CONFLICT），不能任选；
- quantity 必填且 > 0；
- quantity_unit 未提供默认"套"；
- weight_per_unit 未提供写 0（0 仅表示待补充，14.3 场景 3）；
- in_date 未提供默认业务当天；
- status 创建时固定"未完成"（不接受其他值，ERR_STATUS_FIXED）；
- total_amount 禁止传入（由 ERP 计算，ERR_TOTAL_AMOUNT_FORBIDDEN）；
- surcharge_amount（附加款）可选：用户提供时校验
  非负 + 最多 2 位小数（decimal(10,2) 列契约，surcharge_errors）；未提供
  不写入载荷（ERP 默认 0.00，基础金额 + 附加款 = 行总额由 ERP 计算）。

5.2 塑粉字段表（L717-738）：
- supplier_id 按供货商名称唯一查询（matching.match_supplier）；
- color_code 必填（去首尾空格非空，用户必须提供）；
- total_weight 必填且 > 0——重量缺失或 0 不创建（L738：Agent 规则比 ERP
  更严格，不能用 0 表示待补充；与钣金"缺重量写 0"口径不同，RV-21 标准）；
- 重量单位业务固定 kg，不另传单位字段；
- price_per_kg 未提供（含 0——0 仅表示待补充）时按"供货商 + 色号"查询
  有效历史订单：唯一非零价自动补全、无历史价询问、多价格询问（5.2
  价格补全算法 6 条；不用供货商级固定价格，不跨供货商/跨色号推断）；
- in_date 未提供默认业务当天；
- total_amount 禁止传入（由 ERP 计算，金额 = total_weight × price_per_kg）。

字段来源标注（L543 轻确认展示自动补全来源）：
SOURCE_USER / SOURCE_CUSTOMER_PROFILE / SOURCE_DEFAULT / SOURCE_HISTORY_PRICE
（塑粉历史价格补全，5.2）。钣金定价按客户主数据口径补全，无历史订单来源。

纯规则层（2.6 规则 4）：无 IO——客户资料行由调用方（domains 适配层）经
ERP query 传入；本模块只做确定性解析与决策。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from administrative_agent.business.defaults import (
    DEFAULT_QUANTITY_UNIT,
    DEFAULT_STATUS,
    MISSING_WEIGHT_PER_UNIT,
    PRICE_TYPES,
    QUANTITY_UNITS,
    WEIGHT_UNITS,
)

# ---- 字段来源标注（L543：轻确认展示自动补全来源、旧值、新值及影响）----

SOURCE_USER = "user"
SOURCE_CUSTOMER_PROFILE = "customer_profile"
SOURCE_HISTORY_PRICE = "history_price"
SOURCE_DEFAULT = "default"


class MetalOrderError(ValueError):
    """钣金订单字段规则错误（面向用户消息 + 稳定错误码）。

    与 attendance_rules.AttendanceRuleError 同风格：消息可直呈用户，
    code 供执行器/审计归类。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- 错误码 ----

ERR_MODEL_REQUIRED = "ERR_MODEL_REQUIRED"
ERR_CUSTOMER_MATCH_REQUIRED = "ERR_CUSTOMER_MATCH_REQUIRED"
ERR_TOTAL_AMOUNT_FORBIDDEN = "ERR_TOTAL_AMOUNT_FORBIDDEN"
ERR_NUMERIC_INVALID = "ERR_NUMERIC_INVALID"
ERR_QUANTITY_REQUIRED = "ERR_QUANTITY_REQUIRED"
ERR_QUANTITY_POSITIVE = "ERR_QUANTITY_POSITIVE"
ERR_PRICE_NON_NEGATIVE = "ERR_PRICE_NON_NEGATIVE"
ERR_CUSTOMER_PROFILE_MISSING = "ERR_CUSTOMER_PROFILE_MISSING"
ERR_DATA_CONFLICT = "ERR_DATA_CONFLICT"
ERR_STATUS_FIXED = "ERR_STATUS_FIXED"
ERR_PRICE_TYPE_INVALID = "ERR_PRICE_TYPE_INVALID"
ERR_WEIGHT_UNIT_INVALID = "ERR_WEIGHT_UNIT_INVALID"
ERR_QUANTITY_UNIT_INVALID = "ERR_QUANTITY_UNIT_INVALID"
ERR_SURCHARGE_NON_NEGATIVE = "ERR_SURCHARGE_NON_NEGATIVE"
ERR_SURCHARGE_PRECISION = "ERR_SURCHARGE_PRECISION"

#: 钣金附加款列契约（[2026-09-08 ERP 上线] metal_orders.surcharge_amount：
#: decimal(10,2)，非负，默认 0.00，中文名「附加款」；仅钣金，塑粉无此列）
SURCHARGE_MAX_SCALE = "0.01"  # 最多 2 位小数
SURCHARGE_COLUMN_MAX = Decimal("99999999.99")  # decimal(10,2) 列宽上限


def surcharge_errors(value: object) -> tuple[str, str] | None:
    """钣金附加款数值校验 → (错误码, 面向用户消息)；合法返回 None。

    与 ERP metal_orders.surcharge_amount 列契约一致（mcp-api.md §6.1/§7.1）：
    - 非数字/NaN/Inf → (ERR_NUMERIC_INVALID, ...)；
    - 负数 → (ERR_SURCHARGE_NON_NEGATIVE, ...)；
    - 超过 2 位小数或超出列宽 decimal(10,2) → (ERR_SURCHARGE_PRECISION, ...)。

    数值字符串兼容（"88.80"/"0.00"）。规则层（create 补全）与更新预检
    （domains/orders.py precheck_update_inputs）共用同一套校验口径。
    """
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return (ERR_NUMERIC_INVALID, f"附加款必须是数字，收到：{value!r}")
    if not dec.is_finite():
        return (ERR_NUMERIC_INVALID, f"附加款必须是有限数字，收到：{value!r}")
    if dec < 0:
        return (ERR_SURCHARGE_NON_NEGATIVE, f"附加款不能为负（≥0，元），收到：{value!r}")
    try:
        rounded = dec.quantize(Decimal(SURCHARGE_MAX_SCALE))
    except InvalidOperation:
        return (ERR_SURCHARGE_PRECISION, f"附加款最多 2 位小数，收到：{value!r}")
    if rounded != dec:
        return (ERR_SURCHARGE_PRECISION, f"附加款最多 2 位小数（decimal(10,2)），收到：{value!r}")
    if dec > SURCHARGE_COLUMN_MAX:
        return (
            ERR_SURCHARGE_PRECISION,
            f"附加款超出列宽上限（decimal(10,2)，最多 {SURCHARGE_COLUMN_MAX}），"
            f"收到：{value!r}",
        )
    return None


@dataclass(frozen=True)
class MetalOrderInput:
    """钣金订单原始输入（全部可选；None=未提供，进入补全/询问流程）。

    由 extract_metal_order_input 从用户/上游载荷提取（数值已解析）。
    """

    customer_name: str | None = None
    model: str | None = None
    unit_price: float | None = None
    price_type: str | None = None
    weight_unit: str | None = None
    quantity: int | None = None
    quantity_unit: str | None = None
    weight_per_unit: float | None = None
    #: 附加款（元，[2026-09-08 ERP 上线]；None=未提供 → ERP 默认 0.00，
    #: 不入载荷；仅钣金，塑粉提取层键出现即拒）
    surcharge_amount: float | None = None
    in_date: str | None = None
    status: str | None = None
    out_date: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CustomerProfile:
    """客户主数据中与钣金订单计价相关的字段（5.1 L715：单价/价格类型/
    计价单位；[2026-09-08 口径修正]：单价与计价单位是主数据绑定口径）。

    None=客户主数据缺失该项（需询问用户，不得静默兜底）。
    unit_price 不变量（[2026-09-08 评审后口径]）：本对象只经
    resolve_customer_profile 构造——ERP customers.unit_price=0（NOT NULL
    默认 0）已视同「未定价」滤除为 None；直接构造 0 属违反不变量。
    price_unit 不变量（[2026-09-08 口径修正]）：ERP customers.price_unit
    枚举「元/kg」「元/平方」且 NOT NULL 默认「元/kg」——主数据必有值，
    本字段在查询缺失/空串时取 None；weight_unit 不存放在本对象——由
    price_unit 经 PRICE_UNIT_TO_WEIGHT_UNIT 确定性映射得出（5.1）。
    """

    unit_price: float | None = None
    price_type: str | None = None
    price_unit: str | None = None


@dataclass(frozen=True)
class FilledField:
    """一次自动补全记录（L543：来源/旧值/新值）。

    - field：字段名；
    - value：补全后的新值；
    - source：SOURCE_USER / SOURCE_CUSTOMER_PROFILE /
      SOURCE_HISTORY_PRICE（塑粉）/ SOURCE_DEFAULT；
    - previous_value：用户原提供的值（通常 None；用户未提供才会补全）。
    """

    field: str
    value: object
    source: str
    previous_value: object | None = None


@dataclass(frozen=True)
class MetalOrderDraft:
    """补全校验完成的钣金订单草稿（确认卡片与创建载荷的数据源）。

    filled 记录全部自动补全项（轻确认卡片"自动补全字段及来源"要素）；
    to_write_dict() 为将要写入 ERP 的字段与值（total_amount 不在其中）。
    """

    customer_id: int
    customer_name: str
    model: str
    unit_price: float
    price_type: str
    weight_unit: str
    quantity: int
    quantity_unit: str
    weight_per_unit: float
    in_date: str
    status: str = DEFAULT_STATUS
    #: 附加款（元；None=用户未提供 → 不入载荷，ERP 默认 0.00）
    surcharge_amount: float | None = None
    out_date: str | None = None
    notes: str | None = None
    filled: tuple[FilledField, ...] = ()

    def to_write_dict(self) -> dict:
        """将要写入 ERP 的字段与值（5.3 创建载荷；不含 total_amount）。

        out_date/notes 未提供（None）时不入载荷（ERP 可缺省）；
        surcharge_amount 未提供（None）时不入载荷（ERP 默认 0.00，附加款
        为 0 无需显式传；mcp-api.md §6.1 可省略）——用户提供（含 0）则透传。
        其余字段全部列出便于确认卡片逐项展示。
        """
        payload = {
            "customer_id": self.customer_id,
            "model": self.model,
            "unit_price": self.unit_price,
            "price_type": self.price_type,
            "weight_unit": self.weight_unit,
            "quantity": self.quantity,
            "quantity_unit": self.quantity_unit,
            "weight_per_unit": self.weight_per_unit,
            "in_date": self.in_date,
            "status": self.status,
        }
        if self.surcharge_amount is not None:
            payload["surcharge_amount"] = self.surcharge_amount
        if self.out_date is not None:
            payload["out_date"] = self.out_date
        if self.notes is not None:
            payload["notes"] = self.notes
        return payload

    def auto_filled(self) -> tuple[FilledField, ...]:
        """自动补全字段列表（仅补全项；用户提供的值不在此列）。"""
        return tuple(f for f in self.filled if f.source != SOURCE_USER)


def extract_metal_order_input(raw: Mapping) -> MetalOrderInput:
    """从用户/上游载荷提取钣金订单输入。

    - total_amount 出现即拒绝（5.1：禁止传入、由 ERP 计算；fail closed，
      不静默丢弃）；
    - 数值字段（unit_price/weight_per_unit/quantity/surcharge_amount）
      解析失败 → 错误；
    - 未知键忽略（透传字段按需扩展，不猜测）。
    """
    if "total_amount" in raw:
        raise MetalOrderError(
            ERR_TOTAL_AMOUNT_FORBIDDEN,
            "钣金订单不允许传入 total_amount（总金额由 ERP 计算）。",
        )
    return MetalOrderInput(
        customer_name=_as_optional_str(raw.get("customer_name"), "customer_name"),
        model=_as_optional_str(raw.get("model"), "model"),
        unit_price=_as_optional_float(raw.get("unit_price"), "unit_price"),
        price_type=_as_optional_str(raw.get("price_type"), "price_type"),
        weight_unit=_as_optional_str(raw.get("weight_unit"), "weight_unit"),
        quantity=_as_optional_int(raw.get("quantity"), "quantity"),
        quantity_unit=_as_optional_str(raw.get("quantity_unit"), "quantity_unit"),
        weight_per_unit=_as_optional_float(
            raw.get("weight_per_unit"), "weight_per_unit"
        ),
        surcharge_amount=_as_optional_float(
            raw.get("surcharge_amount"), "surcharge_amount"
        ),
        in_date=_as_optional_str(raw.get("in_date"), "in_date"),
        status=_as_optional_str(raw.get("status"), "status"),
        out_date=_as_optional_str(raw.get("out_date"), "out_date"),
        notes=_as_optional_str(raw.get("notes"), "notes"),
    )


def resolve_customer_profile(rows: Iterable[Mapping]) -> CustomerProfile:
    """客户主数据查询结果 → 唯一值（5.1：多值冲突按数据异常处理，不能任选）。

    对 unit_price/price_type/price_unit 三项分别取非空去重值：
    - unit_price=0 视为「未定价/未记录」滤除（[2026-09-08 评审后口径]：
      真实 ERP customers.unit_price 为 NOT NULL 默认 0，查询永不返回
      None，以 0.0/0.0000 形态出现——0 不是客户的真实单价，必须滤掉，
      与塑粉 resolve_powder_price 对 0 的语义一致），该字段进入询问链路；
    - price_unit（[2026-09-08 口径修正]：ERP customers.price_unit 计价
      单位，枚举「元/kg」「元/平方」且 NOT NULL 默认「元/kg」）取唯一值；
      weight_unit 不在此收集——由 price_unit 经 PRICE_UNIT_TO_WEIGHT_UNIT
      确定性映射得出（5.1），不在本函数处理；price_unit 空串视同缺失，
      异常枚举值保留原样（映射未命中 → 补全层兜底询问）；
    - 同项多个不同值 → ERR_DATA_CONFLICT（数据异常，需人工核对）；
    - 同项多个相同值 → 取该值；
    - 全部缺失/为空/全 0 → None（进入询问流程）。

    说明：本决策放在 order_rules 而非 matching——matching 只做实体名称
    唯一匹配，资料字段值冲突属"资料值解析"规则，与客户匹配解耦。
    """
    unit_prices: list[float] = []
    price_types: list[str] = []
    price_units: list[str] = []
    for row in rows:
        # 注意不能用 continue 跳过 0 价行——同一行还携带 price_type/
        # price_unit，须继续处理（0 只滤 unit_price 一个字段）
        if row.get("unit_price") is not None:
            price = float(row["unit_price"])
            if price != 0:  # 主数据 0 = 未定价（ERP NOT NULL 默认 0），视同缺失
                unit_prices.append(price)
        if row.get("price_type") is not None:
            price_types.append(str(row["price_type"]))
        if row.get("price_unit") is not None:
            price_unit = str(row["price_unit"]).strip()
            if price_unit:
                price_units.append(price_unit)
    return CustomerProfile(
        unit_price=_unique_or_conflict(unit_prices, "unit_price"),
        price_type=_unique_or_conflict(price_types, "price_type"),
        price_unit=_unique_or_conflict(price_units, "price_unit"),
    )


#: ERP customers.price_unit → weight_unit 确定性映射（[2026-09-08 口径
#: 修正]：单价与计价单位是客户主数据绑定的固定口径——重量单位不查历史
#: 订单推断，由主数据 price_unit 映射得出。price_unit 枚举仅「元/kg」
#: 「元/平方」两值且 NOT NULL，映射必命中；映射未命中（price_unit 查询
#: 缺失/异常枚举值，理论状态）→ 补全层兜底询问，不默认 kg）
PRICE_UNIT_TO_WEIGHT_UNIT: dict[str, str] = {
    "元/kg": "kg",
    "元/平方": "m²",
}


def complete_metal_order(
    inp: MetalOrderInput,
    *,
    customer_id: int,
    customer_name: str,
    profile: CustomerProfile,
    today: str,
) -> MetalOrderDraft:
    """输入 + 客户匹配结果 + 客户主数据 → 补全校验完成的草稿（5.1/5.3）。

    定价三字段按客户主数据固定口径解析（[2026-09-08 口径修正]：单价与
    计价单位是客户主数据绑定的固定口径，不查历史订单推断；其余字段按
    默认值补全）：

    - unit_price/price_type：用户提供值 > 客户主数据（resolve_customer_
      profile 收集；主数据 unit_price=0 = 未定价已滤为 None）> 询问
      （ERR_CUSTOMER_PROFILE_MISSING，必须询问用户，不得静默兜底——
      未定价客户的询问消息注明「该客户主数据未定价（单价为 0）且用户
      未提供」）；
    - weight_unit：用户提供值 > 主数据 price_unit 确定性映射
      （PRICE_UNIT_TO_WEIGHT_UNIT：元/kg→kg、元/平方→m²，SOURCE_
      CUSTOMER_PROFILE 记入 filled）> 询问兜底（price_unit 缺失或异常
      枚举值映射未命中时，不默认 kg）。

    customer_id/customer_name 来自调用方（matching.match_customer 的
    matched 结果）；传入前必须唯一命中，否则 ERR_CUSTOMER_MATCH_REQUIRED。
    """
    if customer_id is None:
        raise MetalOrderError(
            ERR_CUSTOMER_MATCH_REQUIRED,
            "客户未唯一匹配（customer_id 缺失），无法创建钣金订单，请先核对客户名称。",
        )
    filled: list[FilledField] = []

    model = (inp.model or "").strip()
    if not model:
        raise MetalOrderError(ERR_MODEL_REQUIRED, "钣金订单 model（型号）必填。")

    unit_price = _resolve_price(
        inp.unit_price, profile.unit_price, "unit_price", filled
    )
    price_type = _resolve_choice(
        inp.price_type, profile.price_type, "price_type", filled
    )
    weight_unit = _resolve_weight_unit(inp.weight_unit, profile.price_unit, filled)

    if inp.quantity is None:
        raise MetalOrderError(ERR_QUANTITY_REQUIRED, "钣金订单 quantity（数量）必填。")
    if inp.quantity <= 0:
        raise MetalOrderError(ERR_QUANTITY_POSITIVE, "数量必须大于 0。")

    quantity_unit = inp.quantity_unit
    if quantity_unit is None:
        quantity_unit = DEFAULT_QUANTITY_UNIT
        filled.append(
            FilledField(
                field="quantity_unit",
                value=quantity_unit,
                source=SOURCE_DEFAULT,
                previous_value=None,
            )
        )
    elif quantity_unit not in QUANTITY_UNITS:
        raise MetalOrderError(
            ERR_QUANTITY_UNIT_INVALID,
            f"数量单位必须是 {'/'.join(QUANTITY_UNITS)} 之一，收到：{quantity_unit}",
        )

    weight_per_unit = inp.weight_per_unit
    if weight_per_unit is None:
        weight_per_unit = MISSING_WEIGHT_PER_UNIT
        filled.append(
            FilledField(
                field="weight_per_unit",
                value=weight_per_unit,
                source=SOURCE_DEFAULT,
                previous_value=None,
            )
        )
    elif weight_per_unit < 0:
        raise MetalOrderError(
            ERR_PRICE_NON_NEGATIVE,
            "weight_per_unit（单件重量）不能为负。",
        )

    surcharge_amount = inp.surcharge_amount
    if surcharge_amount is not None:
        # 附加款（[2026-09-08 ERP 上线]）：非负 + ≤2 位小数（decimal(10,2)）
        surcharge_issue = surcharge_errors(surcharge_amount)
        if surcharge_issue:
            code, message = surcharge_issue
            raise MetalOrderError(code, message)

    in_date = inp.in_date or today
    if inp.in_date is None:
        filled.append(
            FilledField(
                field="in_date",
                value=in_date,
                source=SOURCE_DEFAULT,
                previous_value=None,
            )
        )

    status = inp.status
    if status is None:
        status = DEFAULT_STATUS
        filled.append(
            FilledField(
                field="status",
                value=status,
                source=SOURCE_DEFAULT,
                previous_value=None,
            )
        )
    elif status != DEFAULT_STATUS:
        raise MetalOrderError(
            ERR_STATUS_FIXED,
            f"钣金订单创建时 status 固定为“{DEFAULT_STATUS}”，收到：{status}。",
        )

    return MetalOrderDraft(
        customer_id=customer_id,
        customer_name=customer_name,
        model=model,
        unit_price=unit_price,
        price_type=price_type,
        weight_unit=weight_unit,
        quantity=inp.quantity,
        quantity_unit=quantity_unit,
        weight_per_unit=weight_per_unit,
        in_date=in_date,
        status=status,
        surcharge_amount=surcharge_amount,
        out_date=inp.out_date,
        notes=inp.notes,
        filled=tuple(filled),
    )


# ---- 内部辅助 ----

def _as_optional_str(
    value: object,
    field: str,
    error_cls: type = MetalOrderError,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise error_cls(
            ERR_NUMERIC_INVALID, f"字段 {field} 必须为字符串，收到：{value!r}"
        )
    return value


def _as_optional_float(
    value: object,
    field: str,
    error_cls: type = MetalOrderError,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise error_cls(ERR_NUMERIC_INVALID, f"字段 {field} 必须为数字。")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise error_cls(
            ERR_NUMERIC_INVALID, f"字段 {field} 必须为数字，收到：{value!r}"
        ) from None


def _as_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MetalOrderError(ERR_NUMERIC_INVALID, f"字段 {field} 必须为整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        text = str(value).strip()
        return int(text)
    except (TypeError, ValueError):
        raise MetalOrderError(
            ERR_NUMERIC_INVALID, f"字段 {field} 必须为整数，收到：{value!r}"
        ) from None


def _resolve_price(
    user_value: float | None,
    profile_value: float | None,
    field: str,
    filled: list[FilledField],
) -> float:
    """单价解析：用户值 > 客户主数据 > 询问（5.1 主数据固定口径，不兜底）。

    「0」的两处语义（[2026-09-08 评审后口径]，勿混）：
    - user_value == 0：用户显式给的合法**免费价**，允许透传写 0
      （user_value < 0 才拒绝）——对照塑粉：用户 price_per_kg 提供 0
      视为「待补充」须走历史/询问（5.2），与钣金口径不同；
    - profile_value 恒不为 0：客户主数据 unit_price=0（=未定价，ERP
      NOT NULL 默认 0）已由 resolve_customer_profile 滤除为 None——
      补全层永远不会把 0 当真实价采用；落到询问分支时消息注明主数据
      未定价（单价为 0），请用户确认是否免费（显式 0 可写）或提供单价。
    """
    if user_value is not None:
        if user_value < 0:
            raise MetalOrderError(
                ERR_PRICE_NON_NEGATIVE, f"{field}（单价）不能为负。"
            )
        return user_value
    if profile_value is not None:
        filled.append(
            FilledField(
                field=field,
                value=profile_value,
                source=SOURCE_CUSTOMER_PROFILE,
                previous_value=None,
            )
        )
        return profile_value
    raise MetalOrderError(
        ERR_CUSTOMER_PROFILE_MISSING,
        "该客户主数据未定价（单价为 0）且用户未提供，必须询问客户确认。",
    )


def _resolve_choice(
    user_value: str | None,
    profile_value: str | None,
    field: str,
    filled: list[FilledField],
) -> str:
    """枚举解析（price_type）：用户值 > 客户主数据 > 询问（5.1 不兜底）。

    现仅用于 price_type（主数据 price_type 默认「含税」是合法默认值，
    主数据缺失时才询问；weight_unit 解析见 _resolve_weight_unit，不经
    本函数）。
    """
    if user_value is not None:
        if user_value not in PRICE_TYPES:
            raise MetalOrderError(
                ERR_PRICE_TYPE_INVALID,
                f"价格类型必须是 {'/'.join(PRICE_TYPES)} 之一，收到：{user_value}",
            )
        return user_value
    if profile_value is not None:
        filled.append(
            FilledField(
                field=field,
                value=profile_value,
                source=SOURCE_CUSTOMER_PROFILE,
                previous_value=None,
            )
        )
        return profile_value
    raise MetalOrderError(
        ERR_CUSTOMER_PROFILE_MISSING,
        f"客户主数据缺少 {field}，且用户未提供，必须询问客户确认。",
    )


def _resolve_weight_unit(
    user_value: str | None,
    profile_price_unit: str | None,
    filled: list[FilledField],
) -> str:
    """重量单位解析：用户值 > 主数据 price_unit 映射 > 询问兜底（5.1）。

    [2026-09-08 口径修正]：单价与计价单位是客户主数据绑定的固定口径——
    weight_unit 由主数据 price_unit 确定性映射（PRICE_UNIT_TO_WEIGHT_UNIT：
    元/kg→kg、元/平方→m²；ERP 枚举仅此两值且 NOT NULL，映射必命中，故
    weight_unit 无普通询问分支）。用户明确提供单位（kg/m²）时以用户为
    准（普通优先级）。映射未命中（price_unit 查询缺失或异常枚举值，理论
    状态）→ 兜底询问，不默认 kg。
    """
    if user_value is not None:
        if user_value not in WEIGHT_UNITS:
            raise MetalOrderError(
                ERR_WEIGHT_UNIT_INVALID,
                f"重量单位必须是 {'/'.join(WEIGHT_UNITS)} 之一，收到：{user_value}",
            )
        return user_value
    if profile_price_unit is not None:
        mapped = PRICE_UNIT_TO_WEIGHT_UNIT.get(profile_price_unit)
        if mapped is not None:
            filled.append(
                FilledField(
                    field="weight_unit",
                    value=mapped,
                    source=SOURCE_CUSTOMER_PROFILE,
                    previous_value=None,
                )
            )
            return mapped
    raise MetalOrderError(
        ERR_CUSTOMER_PROFILE_MISSING,
        "该客户主数据缺少计价单位（price_unit 缺失或异常）且用户未提供，"
        "无法确定重量单位（kg/m²），必须询问客户确认。",
    )


def _unique_or_conflict(values: list, field: str):
    """非空去重：多个不同值 → 数据异常（ERR_DATA_CONFLICT），不能任选。"""
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) > 1:
        raise MetalOrderError(
            ERR_DATA_CONFLICT,
            f"客户资料 {field} 存在多个冲突值（{'、'.join(map(str, unique))}），"
            "属数据异常，不能任选，需人工核对。",
        )
    return unique[0] if unique else None


# ===========================================================================
# 塑粉订单（规格 5.2 L717-738 / 14.3 场景 4-6 / mcp-api.md §6 塑粉字段）
# ===========================================================================

#: 塑粉重量单位（业务固定 kg；ERP 以 total_weight 表示，不另传单位字段）
POWDER_WEIGHT_UNIT = "kg"


class PowderOrderError(ValueError):
    """塑粉订单字段规则错误（与 MetalOrderError 同风格：消息可直呈用户、
    code 供执行器/审计归类）。独立类以避免影响既有 MetalOrderError 调用方。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: 塑粉错误码（5.2/14.3 场景 4-6）
ERR_POWDER_SUPPLIER_MATCH_REQUIRED = "ERR_POWDER_SUPPLIER_MATCH_REQUIRED"
ERR_POWDER_COLOR_CODE_REQUIRED = "ERR_POWDER_COLOR_CODE_REQUIRED"
ERR_POWDER_WEIGHT_REQUIRED = "ERR_POWDER_WEIGHT_REQUIRED"
ERR_POWDER_WEIGHT_POSITIVE = "ERR_POWDER_WEIGHT_POSITIVE"
ERR_POWDER_PRICE_MISSING = "ERR_POWDER_PRICE_MISSING"
ERR_POWDER_PRICE_AMBIGUOUS = "ERR_POWDER_PRICE_AMBIGUOUS"
ERR_POWDER_PRICE_NEGATIVE = "ERR_POWDER_PRICE_NEGATIVE"
ERR_POWDER_HISTORY_MISMATCH = "ERR_POWDER_HISTORY_MISMATCH"
#: 附加款仅钣金订单支持（[2026-09-08 ERP 上线]；塑粉字段表无此字段）
ERR_POWDER_SURCHARGE_FORBIDDEN = "ERR_POWDER_SURCHARGE_FORBIDDEN"


@dataclass(frozen=True)
class PowderOrderInput:
    """塑粉订单原始输入（全部可选；None=未提供，进入补全/询问流程）。

    由 extract_powder_order_input 从用户/上游载荷提取（数值已解析）。
    supplier_id 由匹配结果决定（不入输入）；weight_unit 固定 kg 不提取。
    """

    supplier_name: str | None = None
    color_code: str | None = None
    total_weight: float | None = None
    price_per_kg: float | None = None
    in_date: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class PowderOrderDraft:
    """补全校验完成的塑粉订单草稿（5.3 创建载荷数据源）。

    filled 记录全部自动补全项（价格历史补全/日期默认）；to_write_dict()
    为将要写入 ERP 的字段与值（不含 total_amount，不传重量单位——业务
    固定 kg，5.2；mcp-api 塑粉字段表无 status）。
    """

    supplier_id: int
    supplier_name: str
    color_code: str
    total_weight: float
    price_per_kg: float
    in_date: str
    notes: str | None = None
    filled: tuple[FilledField, ...] = ()

    def to_write_dict(self) -> dict:
        """将要写入 ERP 的字段与值（5.3 创建载荷；不含 total_amount）。"""
        payload = {
            "supplier_id": self.supplier_id,
            "color_code": self.color_code,
            "total_weight": self.total_weight,
            "price_per_kg": self.price_per_kg,
            "in_date": self.in_date,
        }
        if self.notes is not None:
            payload["notes"] = self.notes
        return payload

    def auto_filled(self) -> tuple[FilledField, ...]:
        """自动补全字段列表（仅补全项；用户提供的值不在此列）。"""
        return tuple(f for f in self.filled if f.source != SOURCE_USER)


def extract_powder_order_input(raw: Mapping) -> PowderOrderInput:
    """从用户/上游载荷提取塑粉订单输入。

    - total_amount 出现即拒绝（5.2：禁止传入、由 ERP 计算；fail closed，
      不静默丢弃——D13 钣金先例；args schema 无此字段，不存在 None 默认
      键问题，仍按键存在判定）；
    - surcharge_amount 值非空（非 None）才拒绝（[2026-09-08 ERP 上线] 附
      加款仅钣金订单支持，塑粉字段表无此字段——mcp-api.md §6.1；fail
      closed 不静默丢弃）。[2026-09-08 回归修复] 只判「键存在」会误伤上
      游真实链路：order_create 的 OrderCreateArgs metal/powder 共用一份
      schema，prepare 层经 args.model_dump() 传入必带 surcharge_amount=
      None 默认键——故 None/null 键放行；非 None 值（含显式 0，故用
      is not None 而非 truthy，勿退化 truthy 判定）早在
      OrderCreateArgs 冲突校验
      （_POWDER_FORBIDDEN_FIELDS，agent/tools/base.py）被拦，本层仅兜底；
    - 数值字段（total_weight/price_per_kg）解析失败 → 错误；
    - 未知键忽略（透传字段按需扩展，不猜测）。
    """
    if "total_amount" in raw:
        raise PowderOrderError(
            ERR_TOTAL_AMOUNT_FORBIDDEN,
            "塑粉订单不允许传入 total_amount（总金额由 ERP 计算）。",
        )
    if raw.get("surcharge_amount") is not None:
        raise PowderOrderError(
            ERR_POWDER_SURCHARGE_FORBIDDEN,
            "塑粉订单没有 surcharge_amount（附加款）字段：附加款仅钣金订单"
            "支持（mcp-api.md §6.1 塑粉字段表），不允许传入。",
        )
    return PowderOrderInput(
        supplier_name=_as_optional_str(
            raw.get("supplier_name"), "supplier_name", PowderOrderError
        ),
        color_code=_as_optional_str(
            raw.get("color_code"), "color_code", PowderOrderError
        ),
        total_weight=_as_optional_float(
            raw.get("total_weight"), "total_weight", PowderOrderError
        ),
        price_per_kg=_as_optional_float(
            raw.get("price_per_kg"), "price_per_kg", PowderOrderError
        ),
        in_date=_as_optional_str(raw.get("in_date"), "in_date", PowderOrderError),
        notes=_as_optional_str(raw.get("notes"), "notes", PowderOrderError),
    )


def resolve_powder_price(
    history_rows: Iterable[Mapping],
    *,
    supplier_id: int,
    color_code: str,
) -> float:
    """5.2 价格补全算法（L729-736）六条中第 2-5 条：历史价格去重决策。

    调用方（domains 适配层）已按同一 supplier_id + color_code 查询全部
    有效历史塑粉订单（算法第 1 条，query 过滤由调用方执行）；本函数：
    - 防御：历史行带 supplier_id/color_code 字段且与请求不一致 →
      ERR_POWDER_HISTORY_MISMATCH（算法第 6 条：不使用供货商级固定价格、
      不跨供货商或跨色号推断——范围不符即数据异常）；
    - 对非零 price_per_kg 去重：
      - 唯一非零价 → 返回该价（算法第 3 条，自动补全）；
      - 无历史价（无行或全零）→ ERR_POWDER_PRICE_MISSING（第 4 条，询问）；
      - 多个不同价格 → ERR_POWDER_PRICE_AMBIGUOUS（第 5 条，询问停止）。

    返回值为补全价格；是否记录 filled 由调用方（complete_powder_order）
    决定。
    """
    prices: list[float] = []
    for row in history_rows:
        if row.get("supplier_id") is not None and int(row["supplier_id"]) != supplier_id:
            raise PowderOrderError(
                ERR_POWDER_HISTORY_MISMATCH,
                f"历史价格行供货商（supplier_id={row['supplier_id']}）与请求"
                f"（supplier_id={supplier_id}）不一致：不跨供货商推断价格。",
            )
        if row.get("color_code") is not None and str(row["color_code"]) != color_code:
            raise PowderOrderError(
                ERR_POWDER_HISTORY_MISMATCH,
                f"历史价格行色号（{row['color_code']}）与请求（{color_code}）"
                "不一致：不跨色号推断价格。",
            )
        value = row.get("price_per_kg")
        if value is None:
            continue
        price = float(value)
        if price <= 0:
            continue  # 0 表示待补充，不参与补全
        if price not in prices:
            prices.append(price)
    if len(prices) == 1:
        return prices[0]
    if not prices:
        raise PowderOrderError(
            ERR_POWDER_PRICE_MISSING,
            "该供货商 + 色号没有有效历史价格（price_per_kg），"
            "无法自动补全，必须询问用户。",
        )
    raise PowderOrderError(
        ERR_POWDER_PRICE_AMBIGUOUS,
        f"该供货商 + 色号存在多个历史价格（{'、'.join(map(str, prices))}），"
        "无法确定唯一价格，必须询问用户确认。",
    )


def complete_powder_order(
    inp: PowderOrderInput,
    *,
    supplier_id: int,
    supplier_name: str,
    history_rows: Iterable[Mapping],
    today: str,
) -> PowderOrderDraft:
    """输入 + 供货商匹配结果 + 历史价格行 → 补全校验完成的草稿（5.2/5.3）。

    - supplier_id 来自调用方（matching.match_supplier 的 matched 结果）；
      传入前必须唯一命中，否则 ERR_POWDER_SUPPLIER_MATCH_REQUIRED；
    - color_code 用户必须提供（去首尾空格非空）；
    - total_weight 必填且 > 0：缺失或 0 不创建（L738 比 ERP 更严格，
      14.3 场景 4/6——直接询问，RV-21 通过标准）；
    - price_per_kg：用户提供 >0 直接采用；未提供或为 0（0 仅表示待补充，
      不能写入）→ 按"供货商 + 色号"历史补全——唯一非零价自动补全
      （SOURCE_HISTORY_PRICE，14.3 场景 5）、无历史价/多价格 → 询问
      （14.3 场景 4/6 停止不创建）；
    - in_date 未提供默认业务当天（SOURCE_DEFAULT）。
    """
    if supplier_id is None:
        raise PowderOrderError(
            ERR_POWDER_SUPPLIER_MATCH_REQUIRED,
            "供货商未唯一匹配（supplier_id 缺失），无法创建塑粉订单，请先核对供货商名称。",
        )
    filled: list[FilledField] = []

    color_code = (inp.color_code or "").strip()
    if not color_code:
        raise PowderOrderError(
            ERR_POWDER_COLOR_CODE_REQUIRED,
            "塑粉订单 color_code（色号）必填，用户必须提供。",
        )

    if inp.total_weight is None:
        raise PowderOrderError(
            ERR_POWDER_WEIGHT_REQUIRED,
            "塑粉订单 total_weight（重量，kg）必填：重量缺失或 0 不创建，必须询问用户。",
        )
    if inp.total_weight <= 0:
        raise PowderOrderError(
            ERR_POWDER_WEIGHT_REQUIRED,
            "塑粉订单 total_weight（重量，kg）必须大于 0："
            f"收到 {inp.total_weight}，重量 0 不创建（不能用 0 表示待补充），必须询问用户。",
        )

    price_per_kg = inp.price_per_kg
    if price_per_kg is not None and price_per_kg < 0:
        raise PowderOrderError(
            ERR_POWDER_PRICE_NEGATIVE,
            f"price_per_kg 不能为负，收到：{price_per_kg}。",
        )
    if price_per_kg is None or price_per_kg == 0:
        # 用户未提供或提供 0（0 仅表示待补充）：按"供货商 + 色号"查询补全
        price_per_kg = resolve_powder_price(
            history_rows, supplier_id=supplier_id, color_code=color_code
        )
        filled.append(
            FilledField(
                field="price_per_kg",
                value=price_per_kg,
                source=SOURCE_HISTORY_PRICE,
                previous_value=inp.price_per_kg,
            )
        )

    in_date = inp.in_date or today
    if inp.in_date is None:
        filled.append(
            FilledField(
                field="in_date",
                value=in_date,
                source=SOURCE_DEFAULT,
                previous_value=None,
            )
        )

    return PowderOrderDraft(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        color_code=color_code,
        total_weight=inp.total_weight,
        price_per_kg=price_per_kg,
        in_date=in_date,
        notes=inp.notes,
        filled=tuple(filled),
    )
