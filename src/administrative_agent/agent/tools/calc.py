"""erp_calc 工具（2026-09-09 架构改造第一步：一体式「查+算」，只读）。

背景：会话循环长期存在的架构缺陷——ERP 汇总计算依赖模型读明细后心算
（本地 A/B 试验实证：模型把 165 行真实数据读进上下文后自信算错——
总重 86444.6 kg vs SQL 真值 136012.9 kg；另一轮模型断言「没有重量字段」）。
拍板方案：模型收到算术/汇总类任务时调用本工具，参数 = 实体 + 筛选 +
聚合规格（声明式），工具内部**代码查库并计算**，模型全程不接触明细行
（返回结果只含聚合值，数据不进模型上下文——这正是本工具的意义）。

- 只读工具（is_write=False）：无确认卡、不建任务、不进写工具分支——
  权限在 run 内自行校验（fail closed：manager 角色 + erp:query scope，
  与 erp_query/export_data 同款，8.4/10.5）；
- 实体白名单 = QUERYABLE_ENTITIES（与 erp_query 一致：metal_orders/
  powder_orders/customers/suppliers/employees/transactions/
  transaction_categories + customer_contacts/
  attendance/ar_payments/ap_payments/salary_payments/powder_inventory/
  powder_consumption），白名单外实体拒绝——聚合与明细查询同一开放面，
  不提供绕过查询白名单的旁路；
- 取数：代码内确定字段集合（模型声明的聚合字段 + multiply/group/单位
  探测字段 + id），**返回结果不带明细行**；id 强制取回（跨页去重防——
  fields 无 id 时行无法识别实体，本工具
  不存在该形态）；
- 数值计算一律 Python Decimal（ERP decimal 字段 JSON 序列化为字符串，
  mcp-api.md §11——金额/重量可能有小数，float 精度不可靠）；
- 非数值/空/0 值行：按声明式语义跳过并报告 skipped_rows（0 = 业务上的
  「待补充」，mcp-api.md §6.1 weight_per_unit 0 表示待补充；绝不静默）；
- 字段准入（两类校验，均 fail closed、措辞与「不在字段目录」
  明确区分）：① **measure 字段必须数值型**（sum/avg/min/max 及其
  multiply_field 只接受 int/decimal——str 列（电话/税号/银行账号等纯数字串）
  会被 _cell_decimal 当数值加总、静默给错数；count 不受约束）；② **敏感
  字段禁止用于 group_by/measure**（数据源 = ERP 元数据的 sensitive 键，
  FieldSpec.sensitive；**有意放行 filter/order_by**，见 run 内注释）；
- 单位混行保护（metal_orders.weight_unit 枚举 kg/m²）：实体存在「单位
  枚举字段」（enum_values 与 {kg, m²} 精确交集非空）且取数在该字段
  混行时，数值 measure 按单位分桶返回（桶内注明单位）——不静默混加
  不同单位的行（用户 A/B 试验教训：kg 与 m² 行混加是错误总重的根源）；
  任一行缺单位值（None/键缺失）→ fail closed 报错提示加筛选（无法归
  桶的行既不进桶也不进 skipped，静默丢数比报错更有害，[reviewer P2-1]）。
- 取数完成性：分页取全后做终检（契约 total 未取满 → fail closed——中途
  空页/并发增行/超 ERP offset 硬限一律不基于部分数据给结论）；恰好取满
  _FETCH_ROW_CAP（10000，对齐 mcp-api.md §4 offset 上限）不算超限。
- 返回 measures 的 value：空串（""）= 该桶/组无有效样本（全部行 0/空/
  非数值被跳过，见 n=0）——不是数值零，模型不得把空串当作 0 报告。
- 归一化（v1.1）：参数 over 声明「桶间
  平均」口径（平均每日之类）——代码把各组桶的聚合值合计后除以分母
  （divisor），分母推断与除法都进代码，模型不再挑分母口算：
  - over="calendar_days"：分母 = filter 中日期字段边界的自然日数（含首
    尾，8-01..8-31 → 31；gt/lt 开区间自动换算）；filter 无日期边界/歧义/
    值非法 → fail closed 提示显式传整数日数（over=N）；
  - over=正整数（显式日数兜底）：分母 = N；
  - over="groups"：分母 = 出现订单行的分组数（桶内 group_by 值的组数；
    无有效样本但有行的组也计入——08-16 那种当日行全为待补充的组不排除，
    与「当日有订单行」口径一致）；需配 group_by；
  - 归一化仅对 sum/count 类 measures 有意义（合计后均分）；avg/min/max
    行均值语义与 over 互斥（参数校验拒绝，防止除出无意义数）；
  - 输出：summary 顶层 normalized（over/divisor 口径说明/results），
    results 按（单位桶 × measure）给 {measure, total(桶间合计=总量),
    divisor, per(=total÷divisor，量化 2 位展示口径), n}；items 各桶明细
    原样保留（总量随时可核对，归一化不吞原值）；「日均」报告口径以
    normalized 的 divisor 为准（用户拍板：8 月自然日 31 天，kg 日均 =
    136012.9/31 = 4387.51、m² 日均 = 2893/31 = 93.32）。
- 顶层合计 totals（[实测错算：模型自行相加多算]）：
  分组且无 over 时，summary 顶层按（单位桶 × measure）给跨所有组的合计——
  模型报告合计必须用 totals，不得自行把各组数值相加。仅汇总 sum/count
  （avg/min/max 行均值/行极值无桶间合计语义，跳过并在 note 说明）；各桶
  分别给出、绝不跨桶混加单位。
  合计对该桶全量原始行**重新聚合一次**（走 _aggregate
  的「先聚合后除一次」），不累加各组已量化 value——后者让含 divide_by 的组值
  量化误差按组数累积（三行各 20 分钟 ÷60 的组值 0.3333 累加得 0.9999 ≠ 1）。
- 常量除法 divide_by（[实测错算：分钟÷60 模型心算]）：MeasureSpec.divide_by 表达
  「(Σ(field×multiply_field?)) ÷ 常量」——行级乘积保持逐行，聚合结果再
  **只除一次**（旧版逐行除再求和会累加舍入伪影，如三行各 20 分钟 ÷60 得
  0.999…）；含 divide_by 的结果量化 4 位小数（除不尽为近似值）；常量不
  参与取数字段与可用行判定。
- 时间粒度 time_granularity（C 缺口：按月/季汇总在工具层无解）：group_by 只
  收单个原始字段名，模型拿到逐日结果（169/534 行）只能搬进上下文手工归并——
  实测 023「分月全错、合计却分毫不差」（合计用工具给的 totals、分月靠模型
  自归并），006 各月金额一律偏低（逐日输出几百行进上下文被截断/驱逐）。
  time_granularity=month/quarter 把日期型 group_by 字段在**代码内**截断成桶键
  （"2026-08" / "2026-Q3"）后再聚合——多组数据不进上下文、也无从手工归并。
  必须与 group_by 同用、group_by 字段类型必须为 date/datetime（否则 fail closed）；
  桶键仍写进 items[].{group_by}（键名不变）。未传时行为逐字节不变。
- 排序取前 N order_by + limit（D 缺口：按计算列排序取 Top-N 无工具能做）：
  金属**无** total_weight 存储列（整单重量 = weight_per_unit×quantity 计算列），
  erp_query.order_by 只收存储列、erp_calc 只给聚合不给行、export_data 只导
  全量——三条路全堵。实测 metal_warehouse id76（855 型 803kg×8=6424kg）按
  整单重量排第 3、按单件重量排第 162：order_by=["-weight_per_unit"] 的候选集
  里结构上不可能找到它。order_by 收**某条 measure 的别名**（前缀 - 表降序），
  limit 取排序后前 N 条 items（1..200，越界 fail closed）——排序/Top-N 由代码
  完成，模型不再退回「按单件重量排」或「取 200 行本地重排」。
  - 必须与 group_by 同用（照 over=groups 先例）；order_by 别名必须唯一解析到
    一条 measure（显式 alias 或自动名 effective_alias；重复 alias 已在参数层
    拒绝，故唯一），解析不到 fail closed 并回列可用别名；
  - 排序键按**数值**比（测量值可能是 JSON 整数或字符串、可能被 divide_by 量化
    过小数），不按字符串比；同值按单位桶序 → 桶键序做 tiebreaker；无有效样本（value 空串）
    的桶**恒排最后**（升/降序皆然）；
  - **totals/normalized 语义不变**：仍是该 filter 下全量原始行的权威合计，不是
    被 limit 截断后的子集；limit 真的截断时**必须**给一句 note 声明 items 只是
    前 N 条子集、不得把子集之和当总计报告。该情形下顶层 count = 本次返回的条目
    数（= N），全量条目数写进 note（模型不得按 count 报「共 N 组」这种用户可见
    错数）；
  - 混单位实体（metal_orders.weight_unit kg/m²）上 Top-N 是**跨单位桶**排序（kg
    与 m² 的条目进同一排序序列），报告仍按单位桶分别、绝不跨桶混加。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary, _err_text
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

#: 每批查询窗口（mcp-api.md §4：limit 默认 50、最大 200）
_FETCH_WINDOW = 200
#: 取数上限：ERP query offset 最大 10000（mcp-api.md §4）——超过即无法
#: 取全，fail closed 报错（提示加筛选），不截断计算（错算比报错更糟）。
_FETCH_ROW_CAP = 10000
#: 续查页数保险丝（异常契约防死循环；10000/200 = 50 页足够，留余量）
_MAX_PAGES = 60
#: 单位枚举字段判定：enum_values 与下列取值精确交集非空（"元/kg" 等
#: 计价单位枚举不含 "kg"/"m²" 精确元素，不会误判 customers.price_unit）
_WEIGHT_UNIT_VALUES = frozenset({"kg", "m²"})
#: 单位无关字段：其数值**不以单位枚举字段（weight_unit）为单位**，跨桶相加
#: 有意义——本表唯一成员 total_amount：金额单位恒为「元」（依据 mcp-api.md:386
#: 钣金金额 = quantity × weight_per_unit × unit_price + surcharge_amount，kg 单
#: 与 m² 单算出来的都是「元」，可相加）。对比 weight_per_unit 是「kg/件 或
#: m²/件」，随单位桶变化，**未列入本表者一律维持既有硬口径**：按单位桶分别
#: 给出、绝不跨桶混加。这是 **fail-safe 方向**：漏列只会退化成「多给两个桶」
#: （模型多看到一维、可自行相加），**绝不静默跨桶混加**——这正是这条硬口径
#: 存在的意义。**新增 ERP 物理量/金额字段时必须同步本表**，否则新字段的
#: measure 会落进错误的默认（见 _call_is_unit_independent）。surcharge_amount
#: （附加款，同属金额）**不列入**——维持 fail-safe 方向：将来若财报要单列附加
#: 款，会重现「看到两个桶」（可自行相加，不会错数）。
_UNIT_INDEPENDENT_FIELDS: frozenset[str] = frozenset({"total_amount"})
#: 数值 measure（sum/avg/min/max，及其 multiply_field 乘数）允许的字段类型。
#: ERP 字段元数据的 ``type`` 取值域由 ERP 侧 registry.py ``_TYPE_LABELS`` 定：
#: ``bool/datetime/date/int/decimal/str``。只有 int/decimal 是数值列——
#: str/date/datetime/bool 列不得聚合：尤其 **str 列**（电话/税号/银行账号等
#: 「非空值多为纯数字串」）会被 ``_cell_decimal`` 的 ``Decimal(text)`` 当数值
#: 加总、静默返回一个无意义的数（fail open，非 fail closed）——这正是本名单
#: 要拦的。**count 不受本规则约束**（数非空行有意义，与字段类型无关）。
#: 【勿写窄】派生计算列（powder_inventory 的 stock/order_total/consumed_total）
#: 由 ERP 声明为 ``decimal``（见 hengfa-erp/backend/app/mcp/registry.py 的
#: ``_DERIVED_SPECS``），必须继续放行——把名单收窄会让库存查询静默失效。
#: 守护测试：tests/integration/test_tools_calc.py 的派生列放行用例。
_NUMERIC_FIELD_TYPES: frozenset[str] = frozenset({"int", "decimal"})
#: 日期型字段类型：time_granularity 的 group_by 字段必须属于本集合。
#: 放行 **date 与 datetime**——ERP 元数据确有 datetime 型字段（registry.py
#: `DateTime → "datetime"`，created_at/updated_at 正常注册，agent 需要按时间
#: 查询；ERP 自测断言 `created_at["type"] == "datetime"`）。按月/季的桶键只
#: 用到年月，datetime 的时分秒由 `_parse_iso_date` 的 split("T") 忽略——守卫
#: **不得比桶键函数更窄**，否则「按月统计新增记录数」被无谓挡在门外。
_DATE_FIELD_TYPES: frozenset[str] = frozenset({"date", "datetime"})
#: avg 输出量化精度（4 位小数；ERP 业务数值字段最多 decimal(10,4)）
_AVG_QUANTIZE = Decimal("0.0001")
#: 归一化 per 值量化精度（2 位小数——「日均」业务展示口径，
#: 用户拍板验收基准即 round 2：4387.51 / 93.32；total 保留原精度）
_NORMALIZE_QUANTIZE = Decimal("0.01")
#: 分组外键 → 名称来源 (目标实体, 名称列)。**同源既有约定，不新造字段**：
#: 名称列取自 mcp-api.md §4.1（customers.company_name / suppliers.name /
#: employees.name）与 domains/export_fields.py::EXPORT_JOIN_FIELDS（导出关联
#: 列 customer_name 经 customers.company_name、supplier_name 经 suppliers.name）。
#: 键为外键**列名**：同名外键跨实体映射一致（customer_id 恒指 customers、
#: supplier_id 恒指 suppliers、employee_id 恒指 employees），故覆盖
#: metal_orders/transactions/customer_contacts/ar_payments/ar_settlements 的
#: customer_id 与 powder_orders/transactions/ap_payments 的 supplier_id、
#: attendance/salary_payments 的 employee_id。**未列入的外键一律不解析**
#: （如 powder_consumption.inventory_id、ar_settlements.payment_id）——由模型
#: 自行兜底取名。
#: **风险（本表按「列名」映射，非按外键语义）**：同名外键跨实体共用本条目。
#: 新增实体若引入**同名但异义**的外键（该列名本表已登记，却指向别的实体/含义），
#: 会**静默取回错误实体的名字**——比给 id 更坏。此类外键必须先在此**登记或
#: 显式排除**（不得默认复用）；否则名称张冠李戴且无任何告警。
#: 名称列是否可用/是否敏感一律以 ERP 元数据为准（见 _enrich_items_with_names）。
_GROUP_NAME_SOURCES: dict[str, tuple[str, str]] = {
    "customer_id": ("customers", "company_name"),
    "supplier_id": ("suppliers", "name"),
    "employee_id": ("employees", "name"),
}
#: 名称解析的 id 上限：超过即**整体跳过**（**不逐 id 发查询**——名称取数走
#: 一次 `filter id in [...]` 批量取回，超大清单会打爆），只留降级 note。
#: 分组组数正常远小于此值。
#: 取值依据：`mcp-api.md §4` 与 ERP 侧 `hengfa-erp/backend/app/mcp/query.py`
#: 对 `in` 的校验**都没有长度上限**（ERP 不会替 agent 挡），500 是 agent 侧
#: **自定值**；其理由是该值低于 SQLite 预编译语句占位符下限 999 并留余量
#: （开发/测试库为 SQLite，生产为 MySQL 8.x）。**超限是整体跳过 + note，
#: 绝不逐 id 发查询。**
_GROUP_NAME_MAX_IDS = 500


class CalcMeasureOp(str, Enum):
    """聚合操作符（与 SQL 语义一致；数值计算内部 Decimal 完成）。"""

    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    MIN = "min"
    MAX = "max"


#: 顶层 totals 汇总的 measure 操作符（avg/min/max 是行均值/行极值，组间
#: 相加无业务含义，不计入 totals——跳过原因经 summary.note 说明）
_TOTAL_OPS = frozenset({CalcMeasureOp.SUM, CalcMeasureOp.COUNT})


class MeasureSpec(BaseModel):
    """单条聚合规格：对 field 做 op 聚合；数值 measure 可配 multiply_field
    表达「整单重量 = 单件重量 × 数量」这类行级乘积（metal_orders 无整单
    重量列，SQL 真值口径即 weight_per_unit×quantity 求和）。"""

    field: str | None = Field(
        None,
        description="聚合字段名（实体字段目录中的字段，取自 list_entities）；"
        "sum/avg/min/max 要求字段为数值型（int/decimal，类型可用 list_entities "
        "查），字符串/日期/布尔字段会被拒（如需计数用 count）；count 可留空"
        "（计数整行）",
    )
    op: CalcMeasureOp = Field(..., description="聚合操作：sum/avg/count/min/max")
    alias: str | None = Field(
        None,
        description="结果别名（缺省自动生成 op(field)，如 sum(weight_per_unit)；"
        "含 divide_by 时追加 /除数，如 sum(a×b)/60）",
    )
    multiply_field: str | None = Field(
        None,
        description="可选乘数字段：每行先取 field×multiply_field 再聚合（如整单"
        "重量 = weight_per_unit×quantity）；乘数 0 表示待补充、该行按跳过处理",
    )
    divide_by: int | str | None = Field(
        None,
        description="可选常量除数（先聚合、后除一次）：先聚合 field×multiply_"
        "field（若有）的结果（sum/avg/min/max），再整体除以 divide_by——"
        "表达「分钟合计÷60 得小时」「总量÷固定份数」这类常量除法，除法由"
        "代码执行、模型不要心算；结果量化 4 位小数（除不尽为近似值）。"
        "必须为正数（≤0 拒绝，可传数字字符串如 \"60\"）；"
        "count 无此语义（如需「总数÷日数」请用 over 归一化参数）",
    )

    @field_validator("divide_by", mode="before")
    @classmethod
    def _coerce_divide_by(cls, value: Any) -> Any:
        # 对齐 _coerce_over 的宽松口径：数字字符串（"60"）统一转 int；
        # bool 显式拒绝（True 是 int 子类且语义不是除数）；≤0 拒绝（除 0 无
        # 定义、负数会翻转符号）。
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(
                f"divide_by 不接受布尔值（收到 {value!r}）；应为正数除数（如 60）"
            )
        if isinstance(value, str):
            text = value.strip()
            try:
                value = int(text)
            except ValueError:
                raise ValueError(
                    f"divide_by 必须为正数值（收到 {value!r}）；"
                    "如 60 或 \"60\""
                )
        if value <= 0:
            raise ValueError(
                f"divide_by 必须为正数（除 0 无定义、负数会翻转符号），"
                f"收到 {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _check_fields(self) -> "MeasureSpec":
        if self.op == CalcMeasureOp.COUNT:
            # count 允许 field 为空（整行计数）；multiply 对 count 无意义
            if self.multiply_field is not None:
                raise ValueError("count 不需要 multiply_field（计数与乘数无关）")
            if self.divide_by is not None:
                raise ValueError(
                    "count 不需要 divide_by（计数是无量纲行数，行级常量除法无"
                    "语义）；如需「总数÷日数/份数」这类桶间均值，请改用 over "
                    "归一化参数（如 over=calendar_days）"
                )
            return self
        if not self.field:
            raise ValueError(f"{self.op.value} 必须提供 field（聚合对象字段名）")
        if self.field == self.multiply_field:
            raise ValueError("field 与 multiply_field 不能是同一字段")
        return self

    @property
    def effective_alias(self) -> str:
        """measure 输出列名（显式 alias，缺省自动生成 op(field[×multiply_
        field])；count 无 field 时 count(*)；含 divide_by 时追加 /除数，如
        sum(weight_per_unit)/60）——结果列命名与同次调用别名唯一性校验共用
        （[2026-09-09 reviewer P3-5②]）。"""
        if self.alias:
            base = self.alias
        elif self.op == CalcMeasureOp.COUNT and not self.field:
            base = "count(*)"
        elif self.multiply_field:
            base = f"{self.op.value}({self.field}×{self.multiply_field})"
        else:
            base = f"{self.op.value}({self.field})" if self.field else self.op.value
        if self.divide_by is not None:
            base = f"{base}/{self.divide_by}"
        return base


class ErpCalcArgs(BaseModel):
    """erp_calc 参数：实体 + 筛选 + 分组 + 聚合规格（声明式，全量中文说明）。

    返回 measures 的 value 若为空串（""）= 该桶/组无有效样本（0/空/非数值
    行全被跳过，其 n=0）——不是数值 0，勿把空串当零（[reviewer P3-5①]）。
    """

    entity: str = Field(
        description=(
            "实体名：metal_orders（钣金订单）/powder_orders（塑粉订单）/"
            "customers（客户）/suppliers（供货商）/employees（员工）/"
            "transactions（收支记录）/transaction_categories（收支分类）/"
            "customer_contacts（客户联系人）/attendance（考勤记录）/ar_payments"
            "（应收收款）/ap_payments（应付付款）/salary_payments（工资发放）/"
            "powder_inventory（塑粉库存色号：净库存直接查 stock 字段，勿自行"
            "按公式另算）/powder_consumption"
            "（塑粉消耗记录）/ar_settlements"
            "（应收平账）"
        )
    )
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            '字段筛选（同 erp_query DSL）：等值如 {"weight_unit": "kg"}；'
            '范围用操作符对象如 {"in_date": {"gte": "2026-08-01", "lte": '
            '"2026-08-31"}}；日期格式 YYYY-MM-DD。支持操作符 eq/ne/gt/gte/'
            "lt/lte/like（模糊包含，% 通配）/in（数组）/between（区间）；"
            "同一字段多操作符为 AND。涉及重量/面积的汇总必须"
            "在 filter 中给出重量单位条件（如 weight_unit=kg 或 m²），或依赖"
            "工具按单位分桶的结果分别报告——禁止自行混加不同单位的行"
            "（金额类字段 total_amount 除外：即使带单位筛选，其合计本就跨单位"
            "合并、不产出 unit_field）"
        ),
    )
    group_by: str | None = Field(
        None,
        description="分组字段（如 in_date 按日分组；汇总按组给出，勿自行口算）；"
        "**按月/季汇总请用 time_granularity（month/quarter）由代码归并，"
        "不要逐日取数再手工归并各日组**（逐日组数多，搬进上下文既易漏行又易"
        "算错）；"
        "未传 over 时分组结果顶层 totals 给合计——报告合计必须用 totals，"
        "勿自行把各组数值相加（已实测：模型自加 11 个客户值多算的错误）；"
        "**仅当存在 sum/count 类 measure 时才有 totals**，全为 avg/min/max "
        "时不产出 totals（行均值/行极值无桶间合计语义）；"
        "totals 里 **count 类的 value 是 JSON 整数、其余 measure 是字符串**；"
        "**含 divide_by 的结果量化 4 位小数**（除不尽时为近似值）；"
        "传了 over 时不给 totals（权威合计是 normalized.total，勿与之混用）",
    )
    time_granularity: Literal["month", "quarter"] | None = Field(
        None,
        description=(
            "时间粒度（可选，配合日期型 group_by 使用）：按月/季汇总的**正确做法**"
            "——把 group_by 的日期字段在代码内截断成桶键后再聚合，不要按原始日"
            "期逐日取数、再自己搬进上下文手工归并（逐日组数多，既易漏行又易算错）。"
            '取值："month" → 桶键形如 "2026-08"；"quarter" → 桶键形如 "2026-Q3"。'
            "桶键写进 items[].{group_by}（键名不变）。**必须与 group_by 同用**，"
            "且 group_by 字段类型必须为 date/datetime——否则 fail closed 拒绝。"
            "字段值为空（NULL）的行归入空值组（该组 group_by 显示为 None），"
            "不报错。"
        ),
    )
    over: Literal["calendar_days", "groups"] | int | None = Field(
        None,
        description=(
            "可选归一化：「平均每日/日均/每月平均」这类桶间均值口径"
            "的声明——代码先按 group_by 组求和（或无分组时取单桶值），再把"
            "合计除以分母 divisor，除法进代码、不要自己心算。"
            '取值："calendar_days" = 分母取 filter 中日期字段的自然日数'
            "（含首尾，如 8 月 in_date gte/lte → 31）；filter 无日期边界或"
            "工具无法推断时，直接传整数日数兜底（over=31）；\"groups\" = 分母"
            "取有订单行出现的分组数（需配 group_by；某日行全为待补充但当日"
            "有订单行仍计入）。只对 sum/count 类 measures 有意义；avg/min/"
            "max 行均值不与 over 同用。典型：group_by=in_date + measures "
            "sum(整单重量) + over=calendar_days → 各桶仍是当日合计（总量可"
            "核对），normalized 给出合计÷31 的日平均。返回的 divisor/per "
            "见结果结构，报告日均必须以此为准。"
        ),
    )
    order_by: str | None = Field(
        None,
        description=(
            "按某条 measure 的结果排序（可选，**必须与 group_by 同用**）：取值 = "
            "某条 measure 的**别名**（显式 alias 或自动名如 sum(weight_per_unit)），"
            "字段名前加 \"-\" 表降序（同 erp_query 风格，如 \"-整单重量\"）。"
            "**按计算列取 Top-N 的正确做法**：金属无 total_weight 存储列，"
            "「整单重量=weight_per_unit×数量」是计算列，erp_query 的 order_by "
            "只收存储列、拿不到它——请写成 measure "
            "{field:\"weight_per_unit\", op:\"sum\", multiply_field:\"quantity\", "
            "alias:\"整单重量\"} 再配 order_by:\"-整单重量\"，排序由代码完成。"
            "别名解析不到会 fail closed 并回列本次可用别名。排序键按数值比、同值"
            "按确定性键序（**单位桶序 → 桶键序**：同值时 kg 恒在 m² 前）做"
            "tiebreaker、无有效样本的桶恒排最后。"
        ),
    )
    limit: int | None = Field(
        None,
        description=(
            "取排序后结果的前 N 条 items（可选，**必须与 group_by 同用**，取值 "
            "1..200 含端点，越界 fail closed）。**与 order_by 搭配即 Top-N**"
            "（如按整单重量降序取前 10 单）；只传 limit 不传 order_by 时按现有"
            "确定性键序取前 N。**注意：limit 只截断 items，totals/normalized 仍是"
            "该 filter 下全量原始行的权威合计**（limit 不与 totals 同步截断）。"
            "截断时结果附一句 note 提醒不得把子集之和当总计报告；**该情形下顶层 "
            "count 是本次返回的条目数，全量条目数见 note**。混单位实体上 Top-N 是"
            "**跨单位桶**排序（如 kg 桶 300 与 m² 桶 200 进同一序列），报告仍按"
            "单位桶分别；**被截断时某单位桶可能整桶不出现在 items，须以 totals "
            "核对**。需要更多条目时请按 filter 分批依次调用本工具，或加 filter "
            "收窄范围。"
        ),
    )
    measures: list[MeasureSpec] = Field(
        ...,
        min_length=1,
        description="要计算的聚合规格（至少 1 条）；sum/avg/min/max 仅接受数值"
        "字段（int/decimal，类型可用 list_entities 查），字符串/日期/布尔字段会"
        "被拒（如需计数用 count）；每条可配 multiply_field（行级乘积，同样须为"
        "数值型）与 divide_by（常量除法：先聚合、后除一次；含除法的结果量化 4 "
        "位小数），除法一律由本工具执行、模型不要心算",
    )

    @field_validator("over", mode="before")
    @classmethod
    def _coerce_over(cls, value: Any) -> Any:
        # 模型可能把显式日数写成字符串（"31"）——统一转 int（宽松参数
        # 面，避免无谓的 fail：日数本来就是数字语义）。
        # [2026-09-09 reviewer P4-1] bool 显式拒绝：JSON true/false 经
        # pydantic 宽松转换会变成 int 1/0 放行（True→divisor=1 是 fail-open
        # 口）——bool 不是日数语义，一律拒绝并提示合法取值。
        if isinstance(value, bool):
            raise ValueError(
                f"over 不接受布尔值（收到 {value!r}）；合法取值："
                '"calendar_days" / "groups" / 正整数日数（如 31）'
            )
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("limit", mode="before")
    @classmethod
    def _coerce_limit(cls, value: Any) -> Any:
        # 对齐 _coerce_over 的宽松参数面：数字字符串（"10"）统一转 int；
        # bool 显式拒绝（True 是 int 子类，会被 pydantic 宽松转成 1 放行——
        # 那不是条数语义）。范围（1..200）在 _check_group 统一校验。
        if isinstance(value, bool):
            raise ValueError(
                f"limit 不接受布尔值（收到 {value!r}）；应为 1..200 的整数条数"
            )
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @model_validator(mode="after")
    def _check_group(self) -> "ErpCalcArgs":
        if self.group_by and any(
            m.field == self.group_by and m.op != CalcMeasureOp.COUNT for m in self.measures
        ):
            raise ValueError(
                "group_by 字段不要同时作为聚合 field（分组维与聚合对象分离）"
            )
        if self.over is not None:
            if isinstance(self.over, int) and self.over <= 0:
                raise ValueError(
                    f"over 显式日数必须为正整数，收到 {self.over!r}"
                )
            if self.over == "groups" and not self.group_by:
                raise ValueError(
                    "over=groups 需要 group_by（分母=出现订单行的分组数，"
                    "未分组时无法取组数）"
                )
            # [2026-09-09 reviewer P3-1] 互斥对**所有** over 形态生效（含
            # 整数日数兜底）——旧版漏加 isinstance(int) 豁免，over=31+avg
            # 会静默输出 Σ组均值÷31 的无意义值（F1 同类错算）。
            if any(
                m.op in (CalcMeasureOp.AVG, CalcMeasureOp.MIN, CalcMeasureOp.MAX)
                for m in self.measures
            ):
                raise ValueError(
                    "归一化（over）仅对 sum/count 类 measures 有意义——"
                    "avg/min/max 是行均值/行极值，合计后除分母无业务含义；"
                    "请把需要归一化的口径写成 sum（先按组分桶再合计），"
                    "avg/min/max 单独调用本工具（不带 over）"
                )
        # [2026-09-09 reviewer P3-5②] 同次调用的结果列名（显式 alias 或
        # 自动名）不得重复——重复会让模型无法区分同名列对应哪个 measure。
        seen: set[str] = set()
        for m in self.measures:
            alias = m.effective_alias
            if alias in seen:
                raise ValueError(
                    f"measures 输出别名重复：{alias!r}（与前一 measure 冲突）"
                    "——请为其中一条显式指定 alias"
                )
            seen.add(alias)
        # 时间粒度（C）：必须配 group_by（截断的是 group_by 日期字段的桶键；
        # 无分组时无处施加）。group_by 字段**类型**是否日期由 run 内按实体
        # 元数据校验（pydantic 层拿不到元数据）。
        if self.time_granularity is not None and not self.group_by:
            raise ValueError(
                "time_granularity 必须与 group_by 同用（按月/季汇总 = 把日期型 "
                "group_by 字段在代码内截断成桶键后再聚合）；请补上 group_by"
                "（如 group_by=in_date + time_granularity=month）"
            )
        # 排序/取前 N（D）：均要求 group_by（无分组时只有一个桶，排序与取前 N
        # 无意义——照 over=groups 需 group_by 的先例 fail closed）。
        if self.order_by is not None:
            if not self.group_by:
                raise ValueError(
                    "order_by 必须与 group_by 同用（无分组时只有一个桶，排序无"
                    "意义）；如需按计算列取 Top-N，请 group_by 到单条粒度"
                    "（如 group_by=id）再配 order_by 与 limit"
                )
            alias = self.order_by[1:] if self.order_by.startswith("-") else self.order_by
            aliases = [m.effective_alias for m in self.measures]
            # 别名唯一性已由上方校验保证（重复 effective_alias 会抛错），
            # 故命中即唯一解析到一条 measure。
            if not alias or alias not in aliases:
                raise ValueError(
                    f"order_by {self.order_by!r} 无法解析到一条 measure"
                    "（取值必须是某条 measure 的别名，字段名前加 \"-\" 表降序）；"
                    f"本次可用的别名：{aliases}"
                )
        if self.limit is not None:
            if not self.group_by:
                raise ValueError(
                    "limit 必须与 group_by 同用（无分组时只有一个桶，取前 N 无"
                    "意义）"
                )
            if not (1 <= self.limit <= 200):
                raise ValueError(
                    f"limit 必须在 1..200 之间（含端点），收到 {self.limit!r}"
                    "——本工具不静默截断；需要更多条目时请按 filter 分批依次"
                    "调用本工具，或加 filter 收窄范围"
                )
        return self


# ---------------------------------------------------------------------------
# 归一化（over）：日历日数推断 + 桶间合计/除法
# ---------------------------------------------------------------------------


def _parse_iso_date(value: Any) -> date | None:
    """容错解析 ISO 日期（"YYYY-MM-DD"；容忍 "YYYY-MM-DDTHH:MM:SS" 时间戳
    形态——ERP 日期字段可能带时间）。格式不合法 → None（调用层 fail closed，
    不猜日数）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _filter_value_iso(fv: Any) -> bool:
    """filter 值是否含可解析的 ISO 日期（str 等值，或 dict 操作符的边界值）
    ——用于无 type=date 元数据时的日期键回退判定（[v1.1 规格]：type=date
    判定或值形如 ISO 的键，任一命中即视为日期字段）。"""
    if isinstance(fv, str):
        return _parse_iso_date(fv) is not None
    if isinstance(fv, dict):
        return any(
            key in ("gte", "gt", "lte", "lt", "eq") and _parse_iso_date(val) is not None
            for key, val in fv.items()
        )
    return False


def _filter_date_keys(filt: dict[str, Any], entity_fields: dict[str, Any]) -> list[str]:
    """filter 中的日期字段键（保序去重，用于 over=calendar_days 日数推断）。

    [2026-09-09 reviewer P4-2] 两级判定（旧版 type=date 与值形态 OR 混判
    有两个漏口：非日期字段恰被 ISO 形态值过滤时被误当日历字段；真
    type=date 键 + 任一 ISO 值键并存时误报"多日期字段"拒绝）：
    1. 类型命中优先：filter 内有任何元数据 type=date 的键 → **只认类型
       命中**（值兜底不再参与，消除误判/误拒）；
    2. 无类型命中时才启用值形态兜底（兼容 created_at 等元数据 type 非
       date/datetime 命名的日期字段——值形如 ISO 即视为日期键）。
    """
    keys: list[str] = []
    for key, spec in entity_fields.items():
        if key not in filt:
            continue
        raw = getattr(spec, "raw", None) or {}
        if raw.get("type") == "date":
            keys.append(key)
    if keys:
        return keys  # 类型命中优先：值兜底不参与
    return [
        key for key in filt if key in entity_fields and _filter_value_iso(filt[key])
    ]


def _calendar_bounds(fv: Any) -> tuple[date | None, date | None]:
    """filter 值 → (start, end) 自然日边界（含 gt/lt 开区间换算 +1/-1 天）。

    返回 (None, None) 表示无法解析/取值形态不支持——调用层 fail closed。
    - str（等值）→ eq 单日 (d, d)；
    - dict：eq 优先；gte/gt 定 start（gt → 次日）；lte/lt 定 end（lt → 前日）。
    """
    if isinstance(fv, str):
        d = _parse_iso_date(fv)
        return (d, d)
    if isinstance(fv, dict):
        if "eq" in fv:
            d = _parse_iso_date(fv["eq"])
            return (d, d)
        start: date | None = None
        end: date | None = None
        if "gte" in fv or "gt" in fv:
            raw = fv["gte"] if "gte" in fv else fv["gt"]
            d = _parse_iso_date(raw)
            if d is None:
                return (None, None)
            start = d if "gte" in fv else d + timedelta(days=1)
        if "lte" in fv or "lt" in fv:
            raw = fv["lte"] if "lte" in fv else fv["lt"]
            d = _parse_iso_date(raw)
            if d is None:
                return (None, None)
            end = d if "lte" in fv else d - timedelta(days=1)
        return (start, end)
    return (None, None)


def _resolve_calendar_plan(
    filt: dict[str, Any] | None, entity_fields: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """over=calendar_days 的分母推断 → (error, plan)。

    plan: {"field", "start", "end", "days"}（days = (end-start).days+1 自然
    日含首尾）。推断失败一律返回显式错误文本（引导模型重试加日期边界或
    显式 over=N），绝不静默猜日数。
    """
    if not filt:
        return (
            "无法推断日历日数（fail closed）：filter 中没有日期范围。"
            '请在 filter 给出日期边界（如 {"in_date": {"gte": "2026-08-01", '
            '"lte": "2026-08-31"}}）后重试，或直接显式传 over=31 指定日数',
            None,
        )
    keys = _filter_date_keys(filt, entity_fields)
    if not keys:
        return (
            "无法推断日历日数（fail closed）：filter 中未识别出日期字段"
            f"（可用字段：{', '.join(sorted(entity_fields))}）。请给日期边界"
            "或直接显式传 over=N 指定日数",
            None,
        )
    if len(keys) > 1:
        return (
            f"无法推断日历日数（fail closed）：filter 含多个日期字段边界"
            f"（{', '.join(keys)}），归一化按哪个日数口径不明确。请保留一个"
            "日期范围，或直接显式传 over=N 指定日数",
            None,
        )
    start, end = _calendar_bounds(filt[keys[0]])
    if start is None or end is None:
        return (
            f"无法推断日历日数（fail closed）：日期字段 {keys[0]!r} 的边界值"
            "无法解析或取值形态不支持（需要 gte/gt 与 lte/lt 组成的范围，或 "
            "eq 单日，日期格式 YYYY-MM-DD）。请修正后重试，或直接显式传 "
            "over=N 指定日数",
            None,
        )
    if start > end:
        return (
            f"无法推断日历日数（fail closed）：日期范围起点晚于终点"
            f"（{start} > {end}）。请修正 filter 后重试",
            None,
        )
    days = (end - start).days + 1
    return None, {
        "field": keys[0],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
    }


def _build_normalized(
    rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    args: ErpCalcArgs,
    *,
    unit_field: str | None,
    calendar_plan: dict[str, Any] | None,
    null_date_rows: int = 0,
) -> dict[str, Any] | None:
    """归一化结果组装（v1.1）：对每个（单位桶 × measure）给出合计 total、
    divisor、per（= total ÷ divisor，量化 2 位展示口径）。

    [2026-09-13 复审修正] total 改为对该单位桶的**全量原始行重算一次**
    （复用 _unit_buckets/_measure_entries，走同一条 _aggregate「先聚合、
    后除一次」路径），而非累加 items 各组已量化 value——同 _build_totals
    的修正。sum + divide_by + over 是旧写法的缺陷入口：sum 组值含 divide_by
    时已量化到 4 位，累加会按组数累积误差（三行各 20 分钟 ÷60 得各组
    0.3333、旧版 total 0.9999 ≠ 真值 1）。divisor 语义不变（over=groups 的
    分母仍是桶内**出现行的组数** = 该桶 items 条目数，按 items 推导）。

    分母：
    - explicit（args.over 为正整数）→ N（全桶一致）；
    - calendar_days（calendar_plan 已推断）→ 自然日数（全桶一致）；
    - groups → 该桶「出现订单行的组数」（= 该桶 items 条目数——group_by
      每组一条目，无有效样本但有行的组也计入：与「当日有订单行」口径
      一致，08-16 那种行全为待补充的日期不排除，见 docstring 规格）。
    items 为空（无任何行）→ 返回 None（total_rows=0 已表达无数据，不给
    空桶归一化假值）。
    """
    if not items:
        return None  # 调用层判断 None
    over_value = args.over
    # 组桶：unit 键 → 该桶全部条目（items 平铺（单位 × 组），每条一组）
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for it in items:
        u = it.get("unit") if unit_field else None
        buckets.setdefault(u, []).append(it)
    if calendar_plan is not None:
        divisor = calendar_plan["days"]
    else:
        divisor = None  # groups / explicit 桶级确定

    def _divisor_for(entries: list[dict[str, Any]]) -> int:
        if isinstance(over_value, int):
            return over_value
        if calendar_plan is not None:
            return calendar_plan["days"]
        return len(entries)  # groups：出现行的组数（条目即组）

    results: list[dict[str, Any]] = []
    # 原始行桶（键与 items 桶一致——同一 _unit_buckets helper，口径不分叉）：
    # total 由桶内全量原始行重算，故需要行集而不只是 items。
    raw_buckets = dict(_unit_buckets(rows, unit_field))
    for u in sorted(buckets, key=lambda v: "" if v is None else str(v)):
        entries = buckets[u]
        div = _divisor_for(entries)
        # 该桶各 measure 对全量原始行的重算结果（先聚合、后除一次）。
        recomputed = {
            me["alias"]: me for me in _measure_entries(raw_buckets.get(u, []), args)
        }
        for spec in args.measures:
            alias = spec.effective_alias
            # n 语义不变：该桶该 measure 的有效样本数 = 逐组 n 之和（组对桶
            # 行集是划分，数值可加）。divisor 的 groups 口径亦仍取条目数。
            n = 0
            for it in entries:
                me = next((x for x in it["measures"] if x.get("alias") == alias), None)
                if me is None:
                    continue
                n += int(me.get("n") or 0)
            value = recomputed[alias]["value"]
            if value == "":
                # 全组无有效样本：不给 total/per 假值（空串语义与桶 value 一致）
                total = ""
                per = ""
            else:
                # 本层 count 与数值 measure 的 total **一律为字符串**（经
                # _fmt_decimal；整数 Decimal 输出不带小数点的串，如 "2"）。
                # 这是既有契约，刻意与 totals/items 的「count 为 int」口径
                # 不同，勿「顺手统一」。
                total = _fmt_decimal(Decimal(value))
                with localcontext() as lc:
                    lc.prec = 50
                    # total 已含过一次 divide_by（重算时聚合后已除），此处仅
                    # 按 divisor 再除——勿重复应用组内除法。
                    per = _fmt_decimal(
                        (Decimal(value) / Decimal(div)).quantize(
                            _NORMALIZE_QUANTIZE, rounding=ROUND_HALF_UP
                        )
                    )
            entry: dict[str, Any] = {
                "measure": alias,
                "total": total,
                "divisor": div,
                "per": per,
                "n": n,
            }
            if unit_field:
                entry["unit"] = u
            results.append(entry)

    normalized: dict[str, Any] = {"over": over_value, "results": results}
    # [2026-09-09 reviewer P4-4] 空串语义统一附注（各 measure 全组均无
    # 有效样本时 total/per 为空串，不是数值 0——与桶 value 口径一致）
    empty_note = "某 measure 全组无有效样本时 total/per 为空串（不是 0）"
    if calendar_plan is not None:
        normalized["calendar_start"] = calendar_plan["start"]
        normalized["calendar_end"] = calendar_plan["end"]
        normalized["note"] = (
            f"分母 = {calendar_plan['start']} 至 {calendar_plan['end']} "
            f"自然日 {calendar_plan['days']} 天（含首尾）；per = 合计 ÷ "
            f"{calendar_plan['days']}，日均口径以此 divisor 为准；{empty_note}"
        )
    elif isinstance(over_value, int):
        normalized["note"] = (
            f"分母 = 显式指定的 {over_value}（over={over_value}）；per = "
            f"合计 ÷ {over_value}，口径以此 divisor 为准；{empty_note}"
        )
    else:
        null_note = (
            "，日期为 NULL 的行自成空值桶（group_by 显示 None）也计入分母"
            if null_date_rows > 0
            else ""
        )
        normalized["note"] = (
            "分母 = 出现订单行的分组数（按桶分别计；无有效样本但有行的"
            "组也计入——如当日行全为待补充仍占一天）"
            f"{null_note}；per = 合计 ÷ divisor，"
            f"口径以各条 divisor 为准；{empty_note}"
        )
    return normalized


# ---------------------------------------------------------------------------
# 取数（复用 mcp-api.md §4 query；与 tools/query.py 同款续查语义但无 500
# 行合并上限——聚合结果不进模型上下文，取数上限只受 ERP offset 硬限约束）
# ---------------------------------------------------------------------------


def _fetch_all_rows(
    ctx: ToolContext, *, entity: str, fields: list[str], filt: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], str | None]:
    """分页取全实体行（id 强制在 fields 内：跨页去重不依赖模型字段选择）。

    返回 (rows, error)。error 非 None 时 rows 为空：
    - ERP 查询失败 → fail closed（错误文本）；
    - 超过 _FETCH_ROW_CAP 仍未取全 → fail closed（提示加筛选，绝不截断
      计算——基于不完整数据的聚合结论比报错更有害）。
    """
    window = _FETCH_WINDOW
    arguments: dict[str, Any] = {"entity": entity, "fields": fields, "limit": window}
    if filt:
        arguments["filter"] = filt
    merged: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    for _ in range(_MAX_PAGES):
        call_args: dict[str, Any] = dict(arguments)
        if offset:
            call_args["offset"] = offset
        result = ctx.client.call_tool("query", call_args)
        if not result.ok:
            return [], f"ERP 查询失败（fail closed）：{_err_text(result)}"
        data = result.data or {}
        items = data.get("items") or []
        total = data.get("total") if data.get("total") is not None else total
        merged.extend(dict(r) for r in items)
        # 正常终止：空页（mcp-api.md §4 契约：到数据尾才空页）或已取到
        # total 尾行。「恰好取满 _FETCH_ROW_CAP」不是超限——10000 行一次
        # 取齐不得误报上限（[2026-09-09 reviewer P3-3]）。
        if not items or (total is not None and offset + len(items) >= total):
            break
        # 取满窗口仍未见尾（总量超 ERP offset 分页硬限）→ 中断循环，由
        # 下方完成性终检统一判 fail closed（绝不基于部分数据给聚合）。
        if len(merged) >= _FETCH_ROW_CAP:
            break
        offset += window
    # 完成性终检（[2026-09-09 reviewer P3-3]）：契约 total 已知而未取满——
    # 中途空页/并发增行/超 offset 硬限都不得静默给部分聚合结论。
    if total is not None and len(merged) < total:
        if len(merged) >= _FETCH_ROW_CAP:
            return [], (
                f"取数已达上限（{_FETCH_ROW_CAP} 行，ERP offset 分页硬限），"
                "无法取全，拒绝基于部分数据的聚合（fail closed）：请加筛选"
                "条件缩小范围后重试"
            )
        return [], (
            f"分页取数未取全（契约 total={total}，仅取到 {len(merged)} 行——"
            "中途空页或数据并发变更），拒绝基于部分数据的聚合（fail closed）："
            "请重试或加筛选条件"
        )
    if total is None and len(merged) >= _FETCH_ROW_CAP:
        return [], (
            f"取数已达上限（{_FETCH_ROW_CAP} 行）且 ERP 未返回 total，无法"
            "确认取全，拒绝基于部分数据的聚合（fail closed）：请加筛选条件"
            "缩小范围后重试"
        )
    # 跨页去重（续查期间 ERP 并发变更防御；id 恒请求在 fields 内，但极端
    # 数据仍可能缺 id 键）：缺 id 的行无法识别实体，不得去重——原样保留
    # 合并序（同 query.py 去重守卫口径，[2026-09-09 reviewer P3-2]；旧
    # str(row.get("id")) 会把缺 id 行全部塌成 "None" 一个 key）。
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in merged:
        if "id" not in row:
            deduped.append(row)
            continue
        key = str(row["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, None


# ---------------------------------------------------------------------------
# 单位分桶探测与计算
# ---------------------------------------------------------------------------


def _unit_enum_field(entity_fields: dict[str, Any]) -> str | None:
    """探测实体「单位枚举字段」：enum_values 与 {kg, m²} 精确交集非空。

    metal_orders.weight_unit（kg/m²）命中；customers.price_unit（元/kg/
    元/平方）不命中（无 "kg"/"m²" 精确元素——计价单位列不参与行级单位
    拆分）；powder_orders 无枚举字段（重量固定 kg，不存在混行）。
    """
    for name, spec in entity_fields.items():
        raw = getattr(spec, "raw", None) or {}
        enum_values = raw.get("enum_values") or getattr(spec, "enum_values", None)
        if isinstance(enum_values, list) and _WEIGHT_UNIT_VALUES & set(enum_values):
            return name
    return None


def _call_is_unit_independent(measures: list[MeasureSpec]) -> bool:
    """本次调用的 measure 是否**全部**单位无关 → 视为无单位桶（不分桶）。

    判据按调用「全有或全无」：仅当**每一个** measure 都单位无关时才为 True，
    否则整个调用完全维持现状（全部按单位分桶）。单条 measure 单位无关 ⟺
    `op != count` **且** `field ∈ _UNIT_INDEPENDENT_FIELDS` **且**
    （`multiply_field is None` **或** `multiply_field ∈ _UNIT_INDEPENDENT_FIELDS`）
    ——即**该 measure 产出的是金额、且式子里没有任何随单位桶变的因子**。
    `multiply_field` 与 `field` 同权：只要有一个因子不在名单里，整条 measure
    就按单位相关处理。

    count 一律不算命中（无论是否带 field）：豁免的语义是「该 measure **产出的
    是金额**」；`count` 任何形态产出的都是**行数**，不是金额。（另一种自洽口径
    是「该 measure 的**值**不以 unit 为单位」——那样 count 也会命中，但会让
    `count(total_amount)` 从分桶变合并，生产变更面大于本批设计面，故不取。）

    保守方向（有意为之，勿"优化"）：`sum(total_amount × quantity)`（quantity
    不在名单）或 `sum(total_amount × weight_per_unit)`（weight_per_unit 随桶
    变）一律判为**单位相关 → 分桶**。前者数值其实与单位无关，但分桶只是让模型
    多看到两桶（可自行相加），而误合并是静默错数——两个方向代价不对称，一律
    取保守（`Σ(total_amount × weight_per_unit)` = 元·(kg/件)，随单位桶变，若
    合并且混加即击穿本常量「绝不静默跨桶混加」的承诺）。

    理由是「全有或全无」而非 per-measure 混合合并：items 条目形态是「单位桶
    × 分组」二维平铺，若逐 measure 混合合并，同一分组会出现两条且语义含混；
    全有或全无**无歧义、向后兼容、且不会产生「半边合并」的误导形状**。混合
    调用（金额 + 物理量同调用）维持分桶是**可接受**的——财报路径只用单个
    sum(total_amount)，且 skill 层会禁止混调。判据只在 measure 层，与
    group_by/filter/over 无关（divide_by 是严格常量，不进字段名判定）。

    命中时该 measure 的数值（元）跨 kg/m² 桶相加有意义（见
    _UNIT_INDEPENDENT_FIELDS 注释）。
    """
    if not measures:
        return False
    return all(
        m.op != CalcMeasureOp.COUNT
        and bool(m.field)
        and m.field in _UNIT_INDEPENDENT_FIELDS
        and (
            m.multiply_field is None
            or m.multiply_field in _UNIT_INDEPENDENT_FIELDS
        )
        for m in measures
    )


def _cell_decimal(value: Any) -> Decimal | None:
    """行单元格 → Decimal（None = 缺失/空/非数值——按声明式语义跳过）。

    ERP decimal 字段 JSON 序列化为字符串（mcp-api.md §11）；int 原生可
    精确转 Decimal；float/bool/list/dict 一律拒绝（float 精度不可信，
    bool 是 int 子类但语义不是数值）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _fmt_decimal(value: Decimal) -> str:
    """Decimal → 展示字符串（去掉小数尾零，"136012.900000" → "136012.9"）。

    只在含小数点时去尾零（"20" 不能变 "2"、900 行合计 "900" 不能变 "9"——
    rstrip("0") 会把纯整数结果的十进制零一并剥掉，[2026-09-09 单测暴露]）。
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _row_usable(row: dict[str, Any], spec: MeasureSpec) -> bool:
    """行是否参与该 measure 的数值聚合。

    - count：恒可用（行存在即计数）；
    - sum/avg/min/max：field 与 multiply_field 的单元格都必须解析为非 0
      的 Decimal（0 = 业务「待补充」，mcp-api.md §6.1——0 值行不算有效
      样本，这正是 8 月钣金 29 行零重量行被排除、129 行参与求和的口径）。
    """
    if spec.op == CalcMeasureOp.COUNT:
        return True
    fields = [spec.field] + ([spec.multiply_field] if spec.multiply_field else [])
    for name in fields:
        dec = _cell_decimal(row.get(name))
        if dec is None or dec == 0:
            return False
    return True


def _aggregate(rows: list[dict[str, Any]], spec: MeasureSpec) -> tuple[str | int, int]:
    """对行集执行单条 measure → (value, n)。

    n = 实际参与行数（count 为行数本身；数值 measure 为可用行数）。

    算法（[2026-09-13 复审 G2] 先聚合、后除，消除逐行除法的舍入伪影）：
    - multiply_field **保持逐行**（Σ(a×b) ≠ (Σa)(Σb)，拆不开）；
    - divide_by 是**常量**，提到聚合之后**只除一次**——sum: Σa/d；
      avg: Σa/(n×d)（等价 (Σa/n)/d）；min: min(a)/d、max: max(a)/d（d>0
      时序不变，等价成立）。旧版逐行除再求和会让「三行各 20 分钟 ÷60」
      累加成 0.999…（≠1）；
    - 含 divide_by 的 measure 结果量化 4 位小数 ROUND_HALF_UP（与 _AVG_
      QUANTIZE 同口径，「除不尽」如 250/60 仍会有长尾）；不含 divide_by 的
      avg 也量化 4 位（既有口径）；sum/min/max 无 divide_by 时保留全精度
      （字符串输出，尾零归一）。
    取值的行级乘积与聚合统一在高精度 context（prec=50）内完成。
    """
    if spec.op == CalcMeasureOp.COUNT:
        return len(rows), len(rows)
    usable = [r for r in rows if _row_usable(r, spec)]
    n = len(usable)
    if n == 0:
        return "", 0
    with localcontext() as lc:
        lc.prec = 50
        # 行级乘积（保持逐行，不可提取）
        if spec.multiply_field:
            values = [
                _cell_decimal(r.get(spec.field))
                * _cell_decimal(r.get(spec.multiply_field))
                for r in usable
            ]
        else:
            values = [_cell_decimal(r.get(spec.field)) for r in usable]
        # 先聚合
        if spec.op == CalcMeasureOp.SUM:
            result = sum(values, Decimal("0"))
        elif spec.op == CalcMeasureOp.AVG:
            result = sum(values, Decimal("0")) / Decimal(n)
        elif spec.op == CalcMeasureOp.MIN:
            result = min(values)
        else:  # MAX
            result = max(values)
        # 聚合后再除一次（divide_by 为常量；d>0 时对 min/max 序不变）
        if spec.divide_by is not None:
            result = result / Decimal(spec.divide_by)
            # 含 divide_by：除不尽会有长尾，量化 4 位展示口径
            result = result.quantize(_AVG_QUANTIZE, rounding=ROUND_HALF_UP)
        elif spec.op == CalcMeasureOp.AVG:
            result = result.quantize(_AVG_QUANTIZE, rounding=ROUND_HALF_UP)
        return _fmt_decimal(result), n


def _unit_buckets(
    rows: list[dict[str, Any]], unit_field: str | None
) -> list[tuple[Any, list[dict[str, Any]]]]:
    """按单位桶划分行集 → [(unit, bucket_rows)]（桶顺序确定性）。

    单位桶的划分与排序**必须与 items/totals 完全一致**，故抽为共用 helper
    （两处各写一遍迟早分叉）：
    - unit_field 为 None（无单位枚举字段实体）→ 单桶 [(None, rows)]；
    - 否则按 unit_field 取值去重并 sorted(key=str)（与 _build_items 旧口径
      逐字一致；「单位种类数即桶数」），每桶行保持原序。
    NULL/缺键行已在 run_erp_calc 归桶守卫 fail closed，此处取值恒为有效单位
    （无 [None] 回退——守卫后不再可达）。
    """
    if not unit_field:
        return [(None, rows)]
    units = sorted({r.get(unit_field) for r in rows}, key=str)
    return [
        (unit, [r for r in rows if r.get(unit_field) == unit]) for unit in units
    ]


def _measure_entries(
    bucket_rows: list[dict[str, Any]], args: ErpCalcArgs
) -> list[dict[str, Any]]:
    """行集 → 各 measure 的结果条目（items 分组与 totals 桶合计共用同一条
    _aggregate 路径——totals 由本函数对**全量原始行**重算，而非累加各组已
    量化值，见 _build_totals）。

    条目：{"alias", "value", "n"}（value 为字符串数值或整数行数；n 为该
    measure 的实际参与行数）；数值 measure 附 "field"/"multiply_field"，含
    divide_by 附展示串；count 为 int、其余为字符串（同一次调用同型）。
    """
    out: list[dict[str, Any]] = []
    for spec in args.measures:
        value, n = _aggregate(bucket_rows, spec)
        entry: dict[str, Any] = {"alias": spec.effective_alias, "value": value, "n": n}
        if spec.op != CalcMeasureOp.COUNT:
            entry["field"] = spec.field
            if spec.multiply_field:
                entry["multiply_field"] = spec.multiply_field
        if spec.divide_by is not None:
            entry["divide_by"] = _fmt_decimal(Decimal(spec.divide_by))
        out.append(entry)
    return out


def _time_bucket_key(value: Any, granularity: str) -> str | None:
    """日期值 → 时间桶键（C）：month → "2026-08"；quarter → "2026-Q3"。

    值经 `_parse_iso_date` 容错解析（容忍 "YYYY-MM-DDTHH:MM:SS"）；解析失败
    返回 None——调用层（run_erp_calc）在此之前已对全量行做 fail closed 校验
    （日期型字段的值必可解析，异常数据不静默归桶）。
    """
    d = _parse_iso_date(value)
    if d is None:
        return None
    if granularity == "month":
        return f"{d.year:04d}-{d.month:02d}"
    # quarter：1-3→Q1 … 10-12→Q4（跨年由 d.year 决定，12 月归当年 Q4、
    # 次年 1 月归次年 Q1）
    return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"


def _group_key_of(row: dict[str, Any], args: ErpCalcArgs) -> Any:
    """行的分组键：未传 time_granularity 时 = group_by 原始字段值（既有
    行为，逐字节不变）；传了则 = 时间桶键（month/quarter 截断）。"""
    raw = row.get(args.group_by) if args.group_by else None
    if args.time_granularity:
        return _time_bucket_key(raw, args.time_granularity)
    return raw


def _build_items(rows: list[dict[str, Any]], args: ErpCalcArgs, unit_field: str | None) -> list[dict[str, Any]]:
    """行集 → 输出条目（分组/单位桶二维结构平铺，确定性顺序）。

    - 实体存在单位枚举字段（unit_field）→ 一律按单位成桶（桶数 = 取数
      结果中的单位种类数；单值时单桶），条目恒带 "unit" 键——模型能核对
      桶单位，混行时不会静默混加（kg 与 m² 的数值绝不出现在同一桶）；
    - 无单位字段实体（powder_orders 等）→ 单桶无 unit 键；
    - 条目形态：无 group_by → {"unit"?(, "measures")}；有 group_by →
      {"unit"?(, <group_by>: 值, "measures")}；组键按 str 自然序（None
      最后）；count measure 与数值 measure 同桶同组。
    - time_granularity（C）：有分组且传了时间粒度时，分组键 = 日期字段的
      时间桶键（`_group_key_of`，month "2026-08"/quarter "2026-Q3"），仍写进
      条目 {group_by} 键（键名不变）。
    measures 元素：{"alias", "value", "n"}（value 为字符串数值或整数行
    数；n 为该 measure 的实际参与行数——每 measure 独立判定，行级跳过
    只做展示统计，绝不从桶行集里剔除行：同一行对 measure A 不可用但对
    measure B 可用（如 wpu=0 但 qty=5）时必须仍参与 B 的聚合）。
    """
    if not rows:
        # 空结果 → 无桶条目（total_rows=0 已表达「无符合筛选的行」；不给
        # 模型产出 count=0 的假桶。NULL 单位行已在 run_erp_calc 归桶守卫
        # 处 fail closed，空集即真无行，[2026-09-09 reviewer P2-1/P3-3]）
        return []
    items: list[dict[str, Any]] = []
    for unit, bucket_rows in _unit_buckets(rows, unit_field):
        if not args.group_by:
            entry: dict[str, Any] = {"measures": _measure_entries(bucket_rows, args)}
            if unit_field:
                entry["unit"] = unit
            items.append(entry)
            continue
        group_values = sorted(
            {_group_key_of(r, args) for r in bucket_rows},
            key=lambda v: "￿" if v is None else str(v),
        )
        for gv in group_values:
            group_rows = [r for r in bucket_rows if _group_key_of(r, args) == gv]
            entry = {args.group_by: gv, "measures": _measure_entries(group_rows, args)}
            if unit_field:
                entry["unit"] = unit
            items.append(entry)
    return items


def _measure_value_for_sort(
    measures: list[dict[str, Any]], alias: str
) -> Decimal | None:
    """条目 measures 中某 alias 的数值（D 排序键）；无该 measure / 值为空串
    （无有效样本）/ 非数值 → None（调用层置最后）。"""
    for me in measures:
        if me.get("alias") == alias:
            return _cell_decimal(me.get("value"))
    return None


def _order_and_limit_items(
    items: list[dict[str, Any]], args: ErpCalcArgs
) -> tuple[list[dict[str, Any]], str | None]:
    """对已分组/分桶的条目应用 order_by 排序与 limit 截断（D），返回
    (显示用 items, 截断 note)。

    排序（order_by，已由参数层保证解析到唯一 measure、且配 group_by）：
    - 按**数值**比较排序键（measure 值可能是 JSON 整数或字符串、可能被
      divide_by 量化过小数），不按字符串比；
    - 同值按确定性键序（**单位桶序 → 桶键序**）做 tiebreaker——排序前
      `items` 已是确定性键序（先按单位桶、再按桶键），Python 稳定排序保证
      同值项保持原相对次序，故无需另造键；
    - 无有效样本（value 空串）的桶**恒排最后**（升序/降序皆然——`None` 不
      参与数值比较，单列在尾部）。
    截断（limit）：取排序后前 N 条；**仅当真的截断（len(items) > limit）时**
    给一句可独立读懂的 note，声明 items 只是前 N 条子集、totals/normalized
    才是全量权威合计。未传 order_by / limit 时 items 原样返回、note=None
    （行为逐字节不变）。
    """
    note: str | None = None
    ordered = items
    if args.order_by is not None:
        descending = args.order_by.startswith("-")
        alias = args.order_by[1:] if descending else args.order_by
        with_value: list[tuple[int, dict[str, Any], Decimal]] = []
        without_value: list[tuple[int, dict[str, Any]]] = []
        for idx, it in enumerate(items):
            value = _measure_value_for_sort(it.get("measures") or [], alias)
            if value is None:
                without_value.append((idx, it))
            else:
                with_value.append((idx, it, value))
        # 稳定排序：同值保持原（桶键）序
        with_value.sort(key=lambda t: t[2], reverse=descending)
        ordered = [it for _, it, _ in with_value] + [it for _, it in without_value]
    if args.limit is not None and len(ordered) > args.limit:
        total_entries = len(ordered)
        ordered = ordered[: args.limit]
        note = (
            f"items 仅返回排序后的前 {args.limit} 条（该筛选下全量共 {total_entries} "
            "条目）——**这是子集**，不是全部。顶层 totals（或 normalized）才是该"
            "筛选下**全量原始行**的权威合计，**不得把本子集之和当成总计报告**"
        )
    return ordered, note


def _group_name_key(group_by: str) -> str:
    """分组外键列 → 名称输出键：去掉 ``_id`` 后缀再加 ``_name``。

    customer_id → customer_name / supplier_id → supplier_name /
    employee_id → employee_name。**刻意不用 f"{group_by}_name"**——那会得到
    叠字的 ``customer_id_name``；去 ``_id`` 后与导出关联列
    （EXPORT_JOIN_FIELDS 的 customer_name/supplier_name）风格一致。
    非 ``_id`` 结尾的列（当前映射表不含，防御性）退化为 ``<列>_name``。
    """
    base = group_by[:-3] if group_by.endswith("_id") else group_by
    return f"{base}_name"


def _enrich_items_with_names(
    ctx: ToolContext, args: ErpCalcArgs, items: list[dict[str, Any]]
) -> str | None:
    """分组结果**就地**补名称键（只新增 ``_group_name_key`` 键，**绝不改**
    group_by 原值、measures、unit 等既有键）。

    仅在「有 group_by 且该外键在 ``_GROUP_NAME_SOURCES`` 内」时动作；
    无分组（单桶）路径**不触达本函数**。

    **fail-soft**：映射/元数据/取数任一环节失败——目标实体不可查询、名称列
    不在字段目录、名称列为敏感字段、id 数超 ``_GROUP_NAME_MAX_IDS``、ERP
    查询报错、名称缺失/空——一律只**降级**（不补 key + 返回一句可见 note），
    ``ok=True`` 与 ``measures`` 恒不受影响。名称列可用性以 ERP 元数据为准
    （不新造白名单）：名称列必须存在于目标实体的字段目录且 ``sensitive``
    为假，否则跳过解析（宁可不给名称）。**绝不逐 id 发查询**——一次
    ``filter id in [...]`` 批量取回（复用 ``_fetch_all_rows`` 语义）。

    返回给 summary 的 note（无动作/全部解析成功返回 None）。
    """
    group_by = args.group_by
    if not group_by or not items:
        return None
    source = _GROUP_NAME_SOURCES.get(group_by)
    if source is None:
        return None  # 未声明的外键：不解析（也不打扰模型，由描述兜底口径接管）
    entity_name, name_field = source
    key = _group_name_key(group_by)
    # 1. 目标实体必须可查询 + 名称列须在元数据字段目录且非敏感
    if entity_name not in QUERYABLE_ENTITIES:
        return (
            f"分组按 {group_by} 给出，未解析名称（{entity_name} 不在可查询白名单，"
            f"如需名称请自行 erp_query 后映射）"
        )
    try:
        target = ctx.metadata.get_entity(entity_name)
    except Exception as exc:  # noqa: BLE001 —— 元数据异常只降级为 note
        return f"分组按 {group_by} 给出，未解析名称（元数据不可用：{exc}）"
    if target is None:
        return (
            f"分组按 {group_by} 给出，未解析名称（实体 {entity_name} 不在元数据中）"
        )
    spec = target.fields.get(name_field)
    if spec is None:
        return (
            f"分组按 {group_by} 给出，未解析名称（{entity_name} 无 {name_field} 字段）"
        )
    if spec.sensitive:
        return (
            f"分组按 {group_by} 给出，未解析名称（名称字段 {entity_name}.{name_field} "
            "为敏感字段，跳过）"
        )
    # 2. 收集去重 id（仅非空原值；id 列本身不敏感，此处不触发敏感拦截）
    ids: list[Any] = []
    seen: set[str] = set()
    for it in items:
        val = it.get(group_by)
        if val is None:
            continue
        skey = str(val)
        if skey in seen:
            continue
        seen.add(skey)
        ids.append(val)
    if not ids:
        return None
    if len(ids) > _GROUP_NAME_MAX_IDS:
        return (
            f"分组按 {group_by} 给出，本次未解析名称（组数 {len(ids)} 超过解析上限 "
            f"{_GROUP_NAME_MAX_IDS}，如需名称请缩小范围后重试或自行 erp_query 映射）"
        )
    # 3. 一次批量取回（id in [...]）；失败降级，绝不影响计算
    rows, err = _fetch_all_rows(
        ctx,
        entity=entity_name,
        fields=["id", name_field],
        filt={"id": {"in": ids}},
    )
    if err is not None:
        # _fetch_all_rows 的原文以「…（fail closed）：…」结尾，但本处**恰恰没有
        # fail closed**——ok=True、measures/totals 全不受影响，只是降级不补名称
        # 键。原样透传会让模型把 note 里的 "fail closed" 误读成整次 erp_calc 失败
        # 而无谓重试/向用户报错（SK.receivables 3 次分组、SK.tax_estimate 4 次都会
        # 各带一次名称查询，抖动时即触发），故剥掉该措辞并显式声明「不影响数值」。
        detail = err.replace("（fail closed）", "").replace("fail closed", "")
        return (
            f"分组按 {group_by} 给出，未解析名称（名称查询失败，已忽略、"
            f"不影响数值：{detail}）"
        )
    name_by_id: dict[str, Any] = {
        str(r["id"]): r.get(name_field)
        for r in rows
        if r.get("id") is not None
    }
    # 4. 就地补键（只新增；查不到/空串 → 不补该组，计入降级提示）
    missing = 0
    for it in items:
        val = it.get(group_by)
        if val is None:
            continue
        nm = name_by_id.get(str(val))
        if nm is None or nm == "":
            missing += 1
            continue
        it[key] = nm
    if missing:
        # 元素内部不再用「；」（那是 summary.note 的元素分隔符，同形会被模型切成
        # 两条语义不完整的 note）；「另含…列」补主语，避免误读成别的表。
        return (
            f"分组按 {group_by} 给出，分组条目已补 {key} 列，其中 {missing} 组"
            f"未解析到名称（档案缺失或已删除），该组以 {group_by} 原值展示"
        )
    return None


def _build_totals(
    rows: list[dict[str, Any]], args: ErpCalcArgs, unit_field: str | None
) -> list[dict[str, Any]] | None:
    """分组结果的顶层合计（totals）：某（单位桶 × measure）跨所有组的合计。

    仅当 group_by 非空且无 over 时由调用层调用（over 存在时 normalized.total
    已是权威桶间合计，同一数字不两处出现）。仅汇总 op ∈ {sum, count} 的
    measure（avg/min/max 是行均值/行极值，组间相加无业务含义，跳过并在
    summary.note 说明）。

    [2026-09-13 复审修正] 合计**对该单位桶的全量原始行重新聚合一次**（复用
    _measure_entries 走同一条 _aggregate 路径），而非累加各组已量化 value：
    逐组值已被量化（含 divide_by 的组值量化到 4 位），累加会让舍入误差按组数
    累积——三行各 20 分钟 ÷60 得每组 0.3333、旧版合计 0.9999 ≠ 真值 1（正是
    divide_by 要消灭的伪影在合计层复活）。重算后「先聚合、后除一次」在合计层
    同样成立。n = 该桶该 measure 的有效样本数（= 逐组 n 之和，分组对桶行集
    是划分，数值可加）。
    value 与 items[].measures[].value 同型：count 为 int、其余为字符串，
    无有效样本为 ""。**按单位桶分别给出，绝不跨桶混加**（kg 与 m² 的数值绝不
    出现在同一合计，硬口径）；无单位字段实体只有一个条目、不带 unit 键。
    例外：金额类单位无关字段（total_amount 单一 measure）的调用在
    run_erp_calc 第 6.1 步已把 unit_field 归 None，走「无单位桶」单条目路径
    给出跨桶合并的单一合计（不在此处分叉）。
    条目与 items 的 measure entry 同构（alias/value/n/field/multiply_field/
    divide_by，带 unit）。
    """
    if not rows:
        return None  # 无行 → 无合计（total_rows=0 已表达，不给空 totals）
    totals: list[dict[str, Any]] = []
    for unit, bucket_rows in _unit_buckets(rows, unit_field):
        # 桶划分与排序与 _build_items 同一 helper（口径不分叉）。对该桶全量
        # 原始行重算（而非累加各组已量化 value），消除按组数累积的量化误差。
        entries = _measure_entries(bucket_rows, args)
        for spec, me in zip(args.measures, entries):
            if spec.op not in _TOTAL_OPS:
                continue  # avg/min/max 不计入（note 说明）
            entry = dict(me)  # 与 items 的 measure entry 同构（count 保持 int）
            if unit_field:
                entry["unit"] = unit
            totals.append(entry)
    return totals


def run_erp_calc(ctx: ToolContext, args: ErpCalcArgs) -> ToolResultSummary:
    """一体式「查+算」：代码查库计算，返回聚合值（不含明细行）。"""

    # 1. 权限（非写工具分支无 scope 校验，本工具自行 fail closed）
    if (
        ctx.context is None
        or ctx.context.user_role != "manager"
        or "erp:query" not in (ctx.context.scope or frozenset())
    ):
        return ToolResultSummary(
            ok=False,
            summary={"error": "权限不足：计算需要 manager 角色与 erp:query scope（8.4/10.5）"},
        )
    # 2. 客户端/元数据接线（缺依赖不静默，对齐 erp_query）
    if ctx.client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 客户端未接线（harness 运行时缺 client），不能执行"},
        )
    if ctx.metadata is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 实体元数据未接线（harness 运行时缺 metadata），不能执行"},
        )
    # 3. 实体白名单（与 erp_query 同一开放面，防绕过查询白名单）
    if args.entity not in QUERYABLE_ENTITIES:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"实体 {args.entity!r} 不可查询（9.2 白名单），"
                    f"仅允许：{sorted(QUERYABLE_ENTITIES)}"
                )
            },
        )
    # 4. 实体元数据 + 参数确定性校验（聚合语义硬性要求，未知字段拒绝——
    #    宽松剔除会让模型以为算了它没算的 measure，比查询更危险）
    try:
        entity = ctx.metadata.get_entity(args.entity)
    except Exception as exc:
        return ToolResultSummary(
            ok=False, summary={"error": f"ERP 实体元数据不可用：{exc}"}
        )
    if entity is None:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"实体 {args.entity!r} 不在元数据（list_entities）中，"
                    "字段目录未知，拒绝计算（fail closed）"
                )
            },
        )
    known = set(entity.fields)
    bad_measure = [
        f"{m.op.value}({m.field})" + (f"×{m.multiply_field}" if m.multiply_field else "")
        for m in args.measures
        if (m.field and m.field not in known) or (m.multiply_field and m.multiply_field not in known)
    ]
    if bad_measure:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"聚合字段不在实体字段目录中（拒绝计算，fail closed）："
                    f"{'、'.join(bad_measure)}；可用字段：{', '.join(sorted(known))}"
                )
            },
        )
    if args.group_by and args.group_by not in known:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"group_by 字段 {args.group_by!r} 不在实体字段目录中"
                    f"（拒绝计算，fail closed）；可用字段：{', '.join(sorted(known))}"
                )
            },
        )
    # 4.0. 时间粒度（C）：group_by 字段**类型**必须是日期型——按年月的桶键
    #      由日期值截断而来，非日期字段无月/季语义（fail closed；类型判定
    #      需要实体元数据，故不能在 pydantic 层做）。放行 date **与
    #      datetime**（ERP 元数据确有 datetime 型字段：created_at/updated_at
    #      正常注册，agent 需要按时间查询；按月的桶键只用到年月，datetime 的
    #      时分秒被 _parse_iso_date 的 split("T") 忽略——守卫不得比桶键函数
    #      更窄，否则「按月统计新增记录数」被无谓挡在门外）。
    if args.time_granularity is not None:
        gb_type = entity.fields[args.group_by].type  # group_by 已确认在 known
        if gb_type not in _DATE_FIELD_TYPES:
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"time_granularity 要求 group_by 字段为日期型"
                        f"（date/datetime），但 {args.group_by!r} 类型为 "
                        f"{gb_type or '未知'}"
                        "（拒绝计算，fail closed）：请改用日期型字段（如 "
                        "in_date）作为 group_by，或去掉 time_granularity"
                    )
                },
            )
    # 4.1. 敏感字段拦截（fail closed）：sensitive 字段的值不得用于
    #      group_by 或 measure（field 与 multiply_field 均含）。
    #      数据源 = ERP 字段元数据的 sensitive 键（``FieldSpec.sensitive``，
    #      mcp/metadata.py 已解析），**不另立敏感清单**——ERP 标了谁这里就
    #      拦谁。拦截的语义是「值不得被分组输出、不得被聚合」：group_by 会把
    #      每个值（如银行账号）原样列进 summary 回上下文——erp_calc 取数直接
    #      call_tool("query")、结果**不经 crop 裁剪**，这是真正的泄露路径。
    #      count 不豁免：计数虽不直接回传值，但本工具的 group_by/measure 是
    #      同一开放面，统一 fail closed 更不易漏。
    #      ⚠️ **有意放行 filter 与 order_by，勿"顺手补全"**：① 刚上线的目录
    #      第三段明确告诉模型「这些字段可用于 erp_query 的 filter/order_by」，
    #      封掉会让提示词变成谎话；② filter 的值来自调用方自己给出的条件，
    #      不构成新的泄露。若将来要一并封，需先改提示词语义再动这里。
    #      ⚠️ 勿与 ``audit/redaction.py:is_sensitive_key`` 混淆：那是**凭据类
    #      键名**（password/token/secret）的日志脱敏，与字段级敏感是两回事，
    #      不要合并、不要复用。
    sensitive_hits: list[str] = []
    if args.group_by and entity.fields[args.group_by].sensitive:
        sensitive_hits.append(f"group_by({args.group_by})")
    for m in args.measures:
        operands = [m.field] + ([m.multiply_field] if m.multiply_field else [])
        for fname in operands:
            if fname and entity.fields[fname].sensitive:
                sensitive_hits.append(f"{m.op.value}({fname})")
    if sensitive_hits:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    "字段为敏感字段，禁止用于分组/聚合（拒绝计算，fail closed）："
                    f"{'、'.join(sensitive_hits)}；敏感字段（联系方式 / 开票 / "
                    "银行信息）的值不得进入 group_by 或 measure。该字段仍可用于 "
                    "filter/order_by（如需按它筛选或排序，请走这两条路径）"
                )
            },
        )
    # 4.2. measure 字段类型校验（fail closed）：sum/avg/min/max 及其乘数字段
    #      必须是数值列（int/decimal）。**count 不受约束**（数非空行有意义，
    #      与类型无关，含 count 带 field 的情形）——本检查显式跳过 count。
    #      措辞与「不在字段目录」明确区分，让模型一次就懂、不再重试。
    bad_type: list[str] = []
    for m in args.measures:
        if m.op == CalcMeasureOp.COUNT:
            continue
        for fname in (m.field, m.multiply_field):
            if not fname:
                continue
            ftype = entity.fields[fname].type
            if ftype not in _NUMERIC_FIELD_TYPES:
                bad_type.append(
                    f"{m.op.value}({fname})：该字段类型为 {ftype or '未知'}，"
                    "不能参与 sum/avg/min/max（如需计数请用 count）"
                )
    if bad_type:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    "聚合字段类型不是数值（拒绝计算，fail closed）："
                    f"{'；'.join(bad_type)}。sum/avg/min/max 仅接受 int/decimal "
                    "类型的字段，字符串/日期/布尔字段不可聚合"
                )
            },
        )
    # 5. filter 宽容剔除未知键（对齐 erp_query：剔除项经 note 反馈模型；
    #    filter 值原样保留——mcp-api.md §4 不解析 filter 值）。
    #    [computed 契约] 计算列（entity.fields 中 computed=true，如 powder_inventory
    #    的 order_total/consumed_total/stock）不可下推 ERP filter（ERP 侧
    #    query.py `_assert_column_field` 一律拒绝 → 整轮重跑），一并宽容剔除 + note；
    #    group_by/measure 不下推 ERP（本地按取回行计算），计算列可用，故仍以
    #    `known`（全部字段）为准，不受本处影响。
    filt = args.filter
    ignored_keys: list[str] = []
    computed_keys: list[str] = []
    if filt:
        ignored_keys = [k for k in filt if k not in known]
        computed_keys = [
            k for k in filt if k in known and entity.fields[k].computed
        ]
        filt = {
            k: v
            for k, v in filt.items()
            if k in known and not entity.fields[k].computed
        } or None
    # 6. 单位分桶探测（weight_unit 混行不得静默混加）
    unit_field = _unit_enum_field(entity.fields)
    # 6.1. 金额类「单位无关」measure 判定（调用级全有或全无）：本次调用的
    #      每个 measure 都单位无关（如单个 sum(total_amount)）→ 等价无单位桶，
    #      items/totals/normalized 一律不分桶、条目不带 unit 键（金额「元」跨
    #      kg/m² 桶相加有意义，mcp-api.md:386）；只要有一个 measure 单位相关
    #      （count、weight_per_unit、带乘数等）→ 完全维持现状（按桶）。判定
    #      只在 measure 层，与 group_by/filter/over 无关。归 None 后第 6.5 步
    #      的「行缺单位值 fail closed」守卫自然不再触发（既然不归桶，单位值
    #      缺失与本结果无关）。
    if unit_field and _call_is_unit_independent(args.measures):
        unit_field = None
    # 6.4. over=calendar_days 分母推断（v1.1：取数前 fail fast——分母推不出
    #      就不用白取一轮数据；显式整数与 groups 在此不解析，groups 分母
    #      依赖取数结果在 _build_normalized 桶级确定）
    calendar_plan: dict[str, Any] | None = None
    if args.over == "calendar_days":
        resolve_error, calendar_plan = _resolve_calendar_plan(filt, entity.fields)
        if resolve_error is not None:
            return ToolResultSummary(ok=False, summary={"error": resolve_error})
    # 7. 取数（字段集合代码内确定：id + measure/multiply/group/单位字段）
    need_fields = {"id"}
    for m in args.measures:
        if m.field:
            need_fields.add(m.field)
        if m.multiply_field:
            need_fields.add(m.multiply_field)
    if args.group_by:
        need_fields.add(args.group_by)
    if unit_field:
        need_fields.add(unit_field)
    rows, fetch_error = _fetch_all_rows(
        ctx, entity=args.entity, fields=sorted(need_fields), filt=filt
    )
    if fetch_error is not None:
        return ToolResultSummary(ok=False, summary={"error": fetch_error})
    # 6.5. 单位归桶完整性守卫（[2026-09-09 reviewer P2-1，必须修]）：
    #     unit_field 实体任一行缺单位值（None/键缺失）即无法归桶——不进
    #     任何桶、也不计入 skipped，total/used/skipped 与各桶 n 之和对不
    #     上，模型无从察觉静默丢数 → fail closed 要求加筛选后重试（全员
    #     缺失同样拒绝；正常数据 weight_unit 必填，此分支只在异常数据出现）。
    if unit_field and rows:
        missing_unit = [row for row in rows if row.get(unit_field) is None]
        if missing_unit:
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"{len(missing_unit)} 行缺 {unit_field} 值（单位缺失），"
                        "无法按单位归桶，拒绝聚合（fail closed）：请加筛选条件"
                        f"（如 {unit_field}=kg）排除或修正数据后重试"
                    )
                },
            )
    # 6.6. 时间桶键可解析性守卫（C）：传了 time_granularity 时，**非 NULL 且
    #      无法解析**的 group_by 日期值（如脏数据/非 ISO 串）即无法归桶——
    #      静默落进某个桶会错分，丢弃会静默丢数，均不可接受 → fail closed。
    #      但 **NULL 是正常业务状态、不是异常数据**（如 metal_warehouse.out_date
    #      = Column(Date, nullable=True)，「未出厂就没有值」），照既有「无粒度」
    #      成例归入 **None 桶**（`_build_items` 的组值排序键早已把 None 排最后、
    #      条目 {group_by: None} 是被支持的情形），**只有非 NULL 且无法解析才
    #      报错**——把 NULL 当「非 ISO 格式」报错会误导模型去「修正数据」。
    null_date_rows = 0
    if args.time_granularity is not None:
        bad_rows = [
            row for row in rows
            if row.get(args.group_by) is not None
            and _time_bucket_key(row.get(args.group_by), args.time_granularity) is None
        ]
        if bad_rows:
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"{len(bad_rows)} 行的 {args.group_by} 值非空但无法解析为"
                        "日期（非 ISO 格式的异常数据），无法按 "
                        f"{args.time_granularity} 归桶，拒绝聚合（fail closed）："
                        "请加筛选条件排除或修正数据后重试"
                    )
                },
            )
        null_date_rows = sum(1 for row in rows if row.get(args.group_by) is None)
    # 8. 行级跳过统计（仅展示口径，不影响任何 measure 计算——measure 内
    #    部按各自操作数独立判定可用行）：数值 measure 的操作数为 0/空/
    #    非数值的行记入 skipped（union 口径：被任一数值 measure 跳过）。
    #    顶层 total/used/skipped 是「行级可用性摘要」，组条目内 n 才是各
    #    measure 的实际参与行数——两者语义不同，勿混用。
    numerical_specs = [m for m in args.measures if m.op != CalcMeasureOp.COUNT]
    skipped_ids: set[int] = set()
    if numerical_specs:
        for idx, row in enumerate(rows):
            if any(not _row_usable(row, m) for m in numerical_specs):
                skipped_ids.add(idx)
    # 9. 组装条目 + meta（summarize_tool_result 的 items 信封：entity 键用
    #    固定标记 "erp_calc"（不在 crop 白名单内，条目原样进上下文；真实
    #    实体名在 target_entity 键）
    notes: list[str] = []
    if ignored_keys:
        notes.append("已忽略未知筛选键: " + ", ".join(ignored_keys))
    if computed_keys:
        notes.append(
            "已忽略计算列筛选键（实时计算列不可用于 filter，仅可经 fields "
            "取回）: " + ", ".join(computed_keys)
        )
    if skipped_ids:
        notes.append(
            f"已跳过 {len(skipped_ids)} 行（数值操作数为 0/空/非数值，"
            "按业务「待补充」处理，未参与数值聚合）"
        )
    if null_date_rows:
        notes.append(
            f"{null_date_rows} 行 {args.group_by} 值为空（NULL），已归入空值组"
            "（该组 " + args.group_by + " 显示为 None）"
        )
    unit_values = (
        sorted(
            {r.get(unit_field) for r in rows if r.get(unit_field) is not None},
            key=str,
        )
        if unit_field
        else []
    )
    if unit_field and len(unit_values) > 1:
        notes.append(
            f"检测到 {unit_field} 混行（{'/'.join(map(str, unit_values))}），"
            "数值结果已按单位分桶返回——不同单位不可合并，请按桶分别报告"
        )
    # 顶层合计（totals）仅在分组且无 over 时给出。note 只承诺真正产出的
    # 内容：有 sum/count 时才提示「报告合计请用 totals」；若整组 measure
    # 全是 avg/min/max（不产出 totals），改给「不提供合计」的说明——旧版
    # 无条件写「报告合计请用 totals」却拿不到 totals，模型会误判。
    totals_applicable = bool(args.group_by) and args.over is None and bool(rows)
    has_total_measures = any(m.op in _TOTAL_OPS for m in args.measures)
    if totals_applicable:
        skipped_ops = sorted(
            {m.op.value for m in args.measures if m.op not in _TOTAL_OPS}
        )
        if not has_total_measures:
            notes.append(
                "本组 measure 均为 avg/min/max（行均值/行极值），按口径不提供"
                "跨组合计——请勿把各组数值相加当合计，按组分别报告即可"
            )
        elif skipped_ops:
            notes.append(
                "顶层 totals 仅汇总 sum/count 类 measure（合计由该桶**全量"
                "原始行重新聚合**得到：先聚合、后除一次；含 divide_by 时"
                "可能与逐组数值之和有末位（量化）差异，报告合计一律以 "
                "totals 为准）；" + "/".join(skipped_ops) + " 为行均值/"
                "行极值，无桶间合计语义，不计入 totals"
            )
    items = _build_items(rows, args, unit_field)
    # 9.2. 排序与取前 N（D）：order_by/limit 只影响**返回的 items**，
    #      不影响 totals（对全量原始行重算）与 normalized（对全量原始行桶级
    #      重算）——两者的权威口径都是「该 filter 下全部行」，非截断子集。
    #      未传 order_by/limit 时 items 原样、note=None（逐字节不变）。
    full_items = items
    limited_items, limit_note = _order_and_limit_items(items, args)
    if limit_note:
        notes.append(limit_note)
    # 9.3. 分组名称解析（只**新增** <group_by去_id>_name 键，groups 原值/measures
    #      /unit 一律不动——既有测试与契约逐字节依赖它们）。fail-soft：映射
    #      未声明/元数据缺名称列/取数失败/id 超限一律只降级为 note，绝不让计算
    #      失败（ok=True 与 measures 不受影响）。同族 erp_salary_calc 早已自解
    #      员工姓名，本处补齐以消除同族工具的不一致（避免模型把裸 id 当身份列
    #      报给用户）。无 group_by 的单桶路径不触达解析（输出逐字节不变）。
    name_note = _enrich_items_with_names(ctx, args, limited_items)
    if name_note:
        notes.append(name_note)
    summary: dict[str, Any] = {
        "entity": "erp_calc",  # items 信封标记（非 ERP 实体；crop 白名单外原样展示）
        "target_entity": args.entity,
        "group_by": args.group_by,
        "total_rows": len(rows),
        "used_rows": len(rows) - len(skipped_ids),
        "skipped_rows": len(skipped_ids),
        "count": len(limited_items),
    }
    if unit_field:
        summary["unit_field"] = unit_field
    if unit_values:
        summary["unit_values"] = unit_values
    if notes:
        summary["note"] = "；".join(notes)
    # 9.4. 顶层合计（totals）：分组且无 over 时，按（单位桶 × measure）给
    #      跨组合计——模型报告合计必须以此为准，勿自行把各组值相加（已实测
    #      自加多算）。over 存在时 normalized.total 已是权威合计，不重复给。
    if totals_applicable:
        totals = _build_totals(rows, args, unit_field)
        if totals:  # 无非空合计（如仅 avg/min/max）→ 不给空 totals 键
            summary["totals"] = totals
    # 9.5. 归一化（v1.1，F1）：items 桶明细原样保留（总量可核对），顶层
    #      normalized 给 (单位桶 × measure) 的 total/divisor/per（代码除，
    #      模型不再挑分母口算；items 为空时不给假归一化值）
    if args.over is not None:
        normalized = _build_normalized(rows, full_items, args, unit_field=unit_field,
                                       calendar_plan=calendar_plan,
                                       null_date_rows=null_date_rows)
        if normalized is not None:
            summary["normalized"] = normalized
    summary["items"] = limited_items
    return ToolResultSummary(ok=True, summary=summary)
