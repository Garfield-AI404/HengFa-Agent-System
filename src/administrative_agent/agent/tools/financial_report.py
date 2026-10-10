"""render_financial_report 工具（只读，财报固定格式渲染器）。

背景：财报（SK.financial_report）要求**每次输出格式完全一致**。此前格式由模型
在正文里自行排版——五张表是否齐全、合计行在不在、净利润会不会被塞进表格，
每轮都可能漂移。改走「工具渲染」路线：把呈现形态收敛成代码里的确定性模板，
模型只负责把五项算完、把各自合计数喂进来。

**职责边界（严格守住）**：本工具**只做排版，不重算任何明细合计**——
- 各项合计**一律用入参传进来的 ``total``**（来自 erp_calc 的 ``totals`` /
  erp_salary_calc 的 ``totals.wage``），不重新相加、不覆盖；
- 只做三笔算数：① ``rent_amount = unit_price × days``（房租乘法）；②
  ``net_profit = metal.total − powder.total − dynamic.total − salary.total −
  rent_amount``（净利润加减）；③ ``powder_ratio = powder.total ÷ metal.total``
  （塑粉金额占钣金订单金额比，除法；呈百分比两位小数，``metal.total == 0``
  时无法计算）；
- **不碰 ERP、不发任何查询**（纯函数式格式化）——故 run 不依赖
  ctx.client/metadata，也不做权限检查（无数据访问面，与 memory_lookup 同为
  内部只读工具）。

fail closed：逐项校验 ``sum(rows.amount) == total``（工资校验 ``sum(rows.wage)
== total``，房租无 rows），容差 0.01（防量化误报）。不一致即 ``ok=False`` 并在
error 里点明**是哪一项、两边各是多少、差多少**，让模型自查重试，绝不静默按
错误的合计排版。房租无 rows 可自查，故在参数校验层交叉校验
``rent.days == (period_end - period_start).days + 1``（此前
days 少算/负数/超大均被静默接受，房租又是五项里单笔最大，会静默抬高净利）。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

from pydantic import BaseModel, Field, model_validator

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary

#: 明细合计与 total 的允许容差（防 ERP 侧量化到分后的末位误差造成误报）。
_TOLERANCE = Decimal("0.01")


class _MetalRow(BaseModel):
    """钣金订单明细行（按客户）。"""

    name: str = Field(description="客户名称（已用 customers.company_name 映射后的名称）")
    amount: Decimal = Field(
        description="该客户当期钣金订单金额合计（元）；由 erp_calc 按客户分组的"
        "结果逐行照搬，不要自己改数"
    )


class _MetalBlock(BaseModel):
    """当期钣金订单金额（按客户）。"""

    rows: list[_MetalRow] = Field(
        description="按客户分组的钣金订单金额明细（erp_calc 的分组结果为 id 桶时，"
        "先用 erp_query 查 customers 取 company_name 再填 name）；当期无记录传空数组 []"
    )
    total: Decimal = Field(
        description="当期钣金订单金额合计（元）——必须传 erp_calc 返回的 totals 里的"
        "钣金合计，不要自己把各行相加"
    )


class _PowderRow(BaseModel):
    """塑粉订单明细行（按供货商）。"""

    name: str = Field(description="供货商名称（已用 suppliers.name 映射后的名称）")
    amount: Decimal = Field(
        description="该供货商当期塑粉订单金额合计（元）；由 erp_calc 按供货商分组的"
        "结果逐行照搬，不要自己改数"
    )


class _PowderBlock(BaseModel):
    """当期塑粉订单金额（按供货商）。"""

    rows: list[_PowderRow] = Field(
        description="按供货商分组的塑粉订单金额明细（id 桶先用 erp_query 查 suppliers "
        "取 name 再填）；当期无记录传空数组 []"
    )
    total: Decimal = Field(
        description="当期塑粉订单金额合计（元）——必须传 erp_calc 返回的 totals "
        "里的塑粉合计，不要自己把各行相加"
    )


class _DynamicRow(BaseModel):
    """动态账款明细行（按类目）。"""

    category: str = Field(description="收支类目名称（以 ERP 实际名称为准）")
    amount: Decimal = Field(
        description="该类目当期支出合计（元）；由 erp_calc 按类目分组的结果逐行照搬"
    )


class _DynamicBlock(BaseModel):
    """当期动态账款（按类目）。"""

    rows: list[_DynamicRow] = Field(
        description="动态账款九个封闭类目（运费/杂物/天然气/电费/水费/手续费/税款/人情/售后服务）"
        "的当期支出明细；当期无记录传空数组 []"
    )
    total: Decimal = Field(
        description="动态账款合计（元）——必须传 erp_calc 返回的 totals 里的动态账款"
        "合计，不要自己把各行相加"
    )


class _SalaryRow(BaseModel):
    """工资明细行（按员工）。"""

    employee: str = Field(description="员工姓名")
    wage: Decimal = Field(
        description="该员工当期工资（元）；取 erp_salary_calc 返回的 items 里的 wage，"
        "逐行照搬，不要自己改数"
    )


class _SalaryBlock(BaseModel):
    """工资账款（按员工）。"""

    rows: list[_SalaryRow] = Field(
        description="erp_salary_calc 返回的 items 逐人工资明细；当期无记录传空数组 []"
        "（注意：只能来自 erp_salary_calc，不得取自 transactions 的「工资」类目或 "
        "salary_payments）"
    )
    total: Decimal = Field(
        description="工资合计（元）——必须传 erp_salary_calc 返回的 totals.wage，"
        "不要自己把各行相加"
    )


class _RentBlock(BaseModel):
    """房租账款（单价 × 天数）。"""

    unit_price: Decimal = Field(
        description="房租单价（元/天）——房租 = 单价 × days；days 必须等于时段"
        "含首尾的自然日数（见下），不得按用户口头口径（如「按 30 天/月」）自行折算"
    )
    days: int = Field(
        description="该时段自然日数（闭区间含首尾；如 8-01 至 8-31 为 31）。"
        "**必须等于 period_start 至 period_end 含首尾的自然日数**"
        "（即 (period_end − period_start).days + 1），否则参数校验失败"
    )


class RenderFinancialReportArgs(BaseModel):
    """render_financial_report 参数：五项均已算完、带各自合计的财报入参。

    只用于**财报最后一步**：钣金/塑粉/动态账款/工资/房租五项都算完、拿到各自
    合计数之后调用它排版。各项合计一律传既有工具给的 totals
    （erp_calc 的 totals / erp_salary_calc 的 totals.wage），**不要自己重算**。
    """

    period_start: date = Field(description="时段起始日（YYYY-MM-DD，含当日）")
    period_end: date = Field(description="时段截止日（YYYY-MM-DD，含当日；闭区间含首尾）")
    metal: _MetalBlock = Field(description="当期钣金订单金额（按客户）")
    powder: _PowderBlock = Field(description="当期塑粉订单金额（按供货商）")
    dynamic: _DynamicBlock = Field(description="当期动态账款（按类目）")
    salary: _SalaryBlock = Field(description="工资账款（按员工）")
    rent: _RentBlock = Field(description="房租账款（单价 × 天数）")

    @model_validator(mode="after")
    def _check_period_order(self) -> "RenderFinancialReportArgs":
        if self.period_start > self.period_end:
            raise ValueError(
                f"时段起止非法：period_start（{self.period_start.isoformat()}）晚于 "
                f"period_end（{self.period_end.isoformat()}）——闭区间要求起点不晚于终点"
            )
        # 房租无明细行可自查、且是五项里单笔最大，故 days 必须与权威时段
        # 交叉校验：房租 = 单价 × days，days 少算/多算/负数都会静默把净利
        # 抬高或压低（其余四项有 sum(rows)==total 兜底，唯独它没有）。
        expected_days = (self.period_end - self.period_start).days + 1
        if self.rent.days != expected_days:
            raise ValueError(
                f"rent.days 与时段不符：时段 {self.period_start.isoformat()} 至 "
                f"{self.period_end.isoformat()}（闭区间含首尾）应为 {expected_days} 个自然日，"
                f"但传入 days = {self.rent.days}。请令 days 等于 "
                f"(period_end − period_start).days + 1 后重试"
            )
        return self


def _money(value: Decimal) -> str:
    """金额统一展示：千分位 + 2 位小数；负号用 ASCII '-'（便于复制进 Excel）。"""
    return f"{value:,.2f}"


def _cell(name: str) -> str:
    """单元格里的名称类值（客户/供货商/类目/员工）转义，防止破坏固定模板。

    模板的全部价值就是「格式不许漂移」，而名称直接插值进 ``| … |`` 行——
    名称含 ``|`` 会把该行撑成多列，含换行/回车会凭空多出一行伪数据。故：
    ``|`` → ``\\|``（Markdown 单元格转义）、换行/回车 → 空格。金额列不必
    （``_money`` 产出字符集本就安全），仅对名称类值调用。
    """
    return name.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _section(
    title: str,
    col_name: str,
    col_value: str,
    rows: list[tuple[str, str]],
    total: Decimal,
) -> str:
    """渲染一张固定形态的 Markdown 表（标题 + 表头 + 明细/无记录 + 合计行）。

    rows 为空（当期无记录）时明细位置放一行 ``| （当期无记录） | 0.00 |``，
    该表**照样出**、不省略、不留空。
    """
    lines = [title, f"| {col_name} | {col_value} |", "|---|---|"]
    if rows:
        lines.extend(f"| {_cell(name)} | {value} |" for name, value in rows)
    else:
        lines.append("| （当期无记录） | 0.00 |")
    lines.append(f"| **合计** | **{_money(total)}** |")
    return "\n".join(lines)


def _powder_ratio_pct(powder: Decimal, metal: Decimal) -> Decimal:
    """``powder / metal × 100``，量化到 0.01（``ROUND_HALF_UP``）。

    默认走**环境默认的 28 位上下文**——普通量级（``|powder / metal|`` 不太大）
    下与朴素 ``(powder / metal * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)``
    逐字节相同。仅当结果系数超过上下文精度、quantize 抛 ``InvalidOperation``
    （默认 prec=28 下 ``|powder/metal| ≥ 1e24`` 即触发；1e23 仍可算，实测阈值
    在 1e23/1e24 之间）时才退到高精度上下文重算，精度按需翻倍至能容纳
    （quantize 到 exponent=-2 需要「整数位数 + 2」位系数，量级有限故必然收敛）。

    **真实边界（勿过度声明）**：本函数只兜住 ``InvalidOperation``（系数超出
    上下文精度）。以下情形仍会**抛异常**、由调用方（tools_node）的通用兜底
    接住并转成 ``ok=False``，本函数**不保证**永不抛：
    - 指数越界：``powder``/``metal`` 运算结果或中间值指数超出 ``Emax=999999``
      （如 ``powder=1E+999998``）→ ``decimal.Overflow``；
    - 分母为零：``metal == 0`` → ``decimal.DivisionByZero``
      （调用点已先判 ``metal.total == 0`` 直接给「无法计算」，不经本函数）；
    - 非有限输入：``powder``/``metal`` 为 ``Infinity``/``NaN`` 时在**入口即**
      ``ValueError``（切题文案见实现）。不加此守卫时：``Infinity`` 会经兜底
      ``while`` 一路翻倍到 > ``decimal.MAX_PREC``，由 ``ctx.prec = prec`` 抛出
      ``ValueError: valid range for prec is [1, MAX_PREC]``——**很快终止、并非
      死循环**，但其文案与真实原因（输入非有限）无关、误导排查；``NaN`` 则
      **根本不抛**，静默返回 ``Decimal("NaN")``。

    那条非有限值入口守卫是把「隐式依赖」变「显式」：本函数当前不可达非有限输入
    （pydantic 2.x 对 ``Decimal`` 字段默认拒绝非有限值，``model_validate`` 抛
    ``Input should be a finite number``），但对该隐式依赖不做担保。

    注意：**不能**无条件把精度提到 60——那并非等价变换，量级到 ~1e24 以上时
    28 位中间舍入会影响「分」的进位（实测 60 万组输入里 147 组分位不同）。
    """
    # [2026-09-14 fail-fast] 非有限输入（Infinity/NaN）在**任意精度**下都无法
    # 正常 quantize（Infinity 恒抛 InvalidOperation；NaN 静默返回 NaN）。若放任
    # 进入下面的「翻倍精度」兜底：Infinity 会一路翻倍到超过 ``decimal.MAX_PREC``，
    # 由 ``ctx.prec = prec`` 抛 ``ValueError: valid range for prec is [1, MAX_PREC]``
    # （很快终止，**不是**死循环；且 ValueError 会被 tools_node 的 except 接住转
    # ok=False）——但该文案与真实原因（输入非有限）无关、误导排查；NaN 则根本不抛，
    # 静默把 NaN 带出去。故在入口显式拒绝，给出切题错误信息，并把「非有限值到不了
    # 这里」的隐式依赖（调用点 metal==0 已先判、pydantic 拒非有限 Decimal）变显式契约。
    if not (powder.is_finite() and metal.is_finite()):
        raise ValueError(
            f"塑粉占比计算失败：比值输入非有限值（powder={powder!r}，"
            f"metal={metal!r}）——本函数只接受有限 Decimal，"
            "非有限值（Infinity/NaN）在任意精度下都无法得到有限结果，故 fail-fast"
        )
    try:
        return (powder / metal * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except InvalidOperation:
        prec = 60
        while True:
            with localcontext() as ctx:
                ctx.prec = prec
                try:
                    return (powder / metal * 100).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                except InvalidOperation:
                    prec *= 2


def _check_complete(label: str, rows_sum: Decimal, total: Decimal) -> str | None:
    """逐项完整性校验：明细逐行相加必须等于传入 total（容差 0.01）。

    不一致返回错误文案（点明是哪一项、两边各是多少、差多少），否则 None。
    """
    if abs(rows_sum - total) <= _TOLERANCE:
        return None
    return (
        f"{label}的明细合计与 total 不一致：明细逐行相加 = {_money(rows_sum)}，"
        f"total = {_money(total)}，差 {_money(rows_sum - total)}。"
        "请核对后重试（total 必须传对应工具给的合计数——erp_calc 的 totals / "
        "erp_salary_calc 的 totals.wage，不要自己重算）"
    )


def run_render_financial_report(
    ctx: ToolContext, args: RenderFinancialReportArgs
) -> ToolResultSummary:
    """把五项已算完的财报结果渲染成固定格式 Markdown（只读；不碰 ERP）。

    - 完整性 fail closed：任一项明细与 total 不符 → ok=False + 明确错误；
    - 只算三笔：①房租 = 单价 × 天数；②净利润 = 五项合计的加减；
      ③塑粉金额占钣金订单金额比 = 塑粉合计 ÷ 钣金合计（百分比两位小数，
      ``ROUND_HALF_UP``；钣金合计为 0 时比值行记「无法计算」、仍出报告）。
    """
    checks = [
        ("钣金订单金额", sum((r.amount for r in args.metal.rows), Decimal(0)), args.metal.total),
        ("塑粉订单金额", sum((r.amount for r in args.powder.rows), Decimal(0)), args.powder.total),
        ("动态账款", sum((r.amount for r in args.dynamic.rows), Decimal(0)), args.dynamic.total),
        ("工资账款", sum((r.wage for r in args.salary.rows), Decimal(0)), args.salary.total),
    ]
    for label, rows_sum, total in checks:
        error = _check_complete(label, rows_sum, total)
        if error is not None:
            return ToolResultSummary(ok=False, summary={"error": error})

    # 三笔算数（唯一由本工具负责的计算）
    # ① 房租乘法
    rent_amount = args.rent.unit_price * args.rent.days
    # ② 净利润加减
    net_profit = (
        args.metal.total
        - args.powder.total
        - args.dynamic.total
        - args.salary.total
        - rent_amount
    )
    # ③ 塑粉金额占钣金订单金额比（除法：塑粉 ÷ 钣金，百分比两位小数）
    # 除数为零时照常出报告，比值行记「无法计算」（用 → 而非 =，使「本行无等号」
    # 可被机械检验）；分子照实显示塑粉金额，不写成 0.00。
    powder_ratio: float | None
    if args.metal.total == 0:
        ratio_line = f"{_money(args.powder.total)} ÷ 0.00 → 无法计算（当期无钣金订单）"
        powder_ratio = None
    else:
        # [2026-09-14] 除法走 _powder_ratio_pct：默认上下文能算则逐字节不变，
        # 仅当抛 InvalidOperation（|powder/metal| 极大）时退到高精度重算，
        # 使该路径不再以异常中断工具（指数越界 Overflow 仍会抛出，见其 docstring）。
        pct = _powder_ratio_pct(args.powder.total, args.metal.total)
        ratio_line = f"{_money(args.powder.total)} ÷ {_money(args.metal.total)} = {_money(pct)}%"
        # [2026-09-14 reviewer] 兜底放宽后 pct 可极大：float(Decimal('1E+402'))
        # 得 inf，json.dumps 会产出非法 JSON token ``Infinity`` 进 summary。
        # 故非有限即记 None（与除数为零同语义：给不出可用数值）。
        powder_ratio_candidate = float(pct)
        powder_ratio = powder_ratio_candidate if math.isfinite(powder_ratio_candidate) else None

    sections = [
        _section(
            "## 当期钣金订单金额",
            "客户",
            "金额",
            [(r.name, _money(r.amount)) for r in args.metal.rows],
            args.metal.total,
        ),
        _section(
            "## 当期塑粉订单金额",
            "供货商",
            "金额",
            [(r.name, _money(r.amount)) for r in args.powder.rows],
            args.powder.total,
        ),
        _section(
            "## 当期动态账款",
            "类目",
            "金额",
            [(r.category, _money(r.amount)) for r in args.dynamic.rows],
            args.dynamic.total,
        ),
        _section(
            "## 工资账款",
            "员工",
            "工资",
            [(r.employee, _money(r.wage)) for r in args.salary.rows],
            args.salary.total,
        ),
        _section(
            "## 房租账款",
            "项目",
            "金额",
            [(f"{_money(args.rent.unit_price)}元 × {args.rent.days}天", _money(rent_amount))],
            rent_amount,
        ),
    ]
    # 净利润不进任何表格，只出这一行算式（被减数 - 各减数 = 结果）
    net_line = (
        f"{_money(args.metal.total)} - {_money(args.powder.total)} - "
        f"{_money(args.dynamic.total)} - {_money(args.salary.total)} - "
        f"{_money(rent_amount)} = {_money(net_profit)}"
    )
    text = (
        "\n\n".join(sections)
        + "\n\n净利润（不用表格）\n"
        + net_line
        + "\n\n塑粉金额占钣金订单金额比\n"
        + ratio_line
    )

    net_profit_q = net_profit.quantize(_TOLERANCE)
    summary: dict[str, Any] = {
        "text": text,
        "period_start": args.period_start.isoformat(),
        "period_end": args.period_end.isoformat(),
        "totals": {
            "metal": _money(args.metal.total),
            "powder": _money(args.powder.total),
            "dynamic": _money(args.dynamic.total),
            "salary": _money(args.salary.total),
            "rent": _money(rent_amount),
        },
        "net_profit": float(net_profit_q),
        "powder_ratio": powder_ratio,
    }
    return ToolResultSummary(ok=True, summary=summary)
