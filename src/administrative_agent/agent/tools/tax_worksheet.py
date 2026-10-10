"""render_tax_worksheet 工具（只读，当期增值税估算底稿固定格式渲染器）。

背景：用户（小厂老板）想日常看数——「当期大概要交多少增值税 + 附加税」，
减少频繁问代账。产出的**是估算值、不是申报依据**。本流程的税率与算式都
是**固定口径**（用户已逐轮拍板），故一律下沉进代码，模型只负责按
``SK.tax_estimate`` 取回各块金额、喂进来排版。

**职责边界（严格守住）**：本工具**只做排版 + 估算算式，不重算任何明细
合计、不碰 ERP**——
- 各块（含税销项/未计税收入/单价类型未知/进项 13% 档/进项 9% 档）的
  ``total`` 入参只用于 **fail closed 完整性校验**（``sum(rows.amount) ==
  total``），不再据此反算税额；
- 税额一律**逐行先算、各自取整到分、再在「分」域相加**（用户裁定：
  「每个客户先算出税额、各自取整到分，再相加得合计」）——这样表列行相加
  恒等于合计行，且算式段每一步都是已量化到分的值之间做加减；
- 只做估算所需的几笔算数：销项税额、进项税额（两档）、应纳增值税、附加税、
  合计应缴（公式见模块级税率常量与 ``run_render_tax_worksheet``）；
- **不碰 ERP、不发任何查询**（纯函数式格式化）——故 run 不依赖
  ctx.client/metadata，也不做权限检查（无数据访问面，与 memory_lookup /
  render_financial_report 同为内部只读工具）。

口径（用户拍板，勿改）：金额均为不含税口径；含税客户的收款是含税价，
销项税额 = 收款额 ÷ 113 × 13；未计税收入（不含税客户的收款）与「单价类型
未知」客户的收款**只展示、不计税**；进项 13% 档（塑粉付款 + 电费）按 13%、
进项 9% 档（天然气）按 9%；附加税按综合税率 6%；**留抵保护**：应纳增值税
≤ 0 时附加税恒为 0（不得为负）。

fail closed：逐块校验 ``sum(rows.amount) == total``（容差 0.01，防量化
误报）。不一致即 ``ok=False`` 并在 error 里点明**是哪一块、两边各是多少、
差多少**，让模型自查重试，绝不静默按错误的合计排版。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary
# 与财报渲染器（render_financial_report）共用同一套固定模板的转义（``_cell``）
# 与金额格式（``_money``）——本工具的呈现形态与它同宗，复用同一实现以免两处漂移。
from administrative_agent.agent.tools.financial_report import _cell, _money

#: 明细合计与 total 的允许容差（防 ERP 侧量化到分后的末位误差造成误报）。
_TOLERANCE = Decimal("0.01")

#: 销项税率口径：含税收款是含税价，销项税额 = 收款额 ÷ 113 × 13。
#: 以「分子/分母」两常量表达、计算时**先除后乘**，与「÷ 113 × 13」逐字对应；
#: 不用预算好的比率 Decimal("13")/Decimal("113")——预除会先把无限小数截到
#: 上下文精度，与「先除后乘」在末位（分）上可能不同。
_OUTPUT_NUMERATOR = Decimal("13")
_OUTPUT_DENOMINATOR = Decimal("113")

#: 进项税率（百分比整数）与附加税综合税率（百分比整数）。
#: 进项税额 = 金额 × 税率 ÷ 100；附加税 = max(应纳增值税, 0) × 6 ÷ 100。
_INPUT_13_RATE = Decimal("13")
_INPUT_9_RATE = Decimal("9")
_SURCHARGE_RATE = Decimal("6")
_PERCENT = Decimal("100")

#: 进项表税率列展示用（与上面两个税率常量一一对应）。
_INPUT_13_RATE_LABEL = "13%"
_INPUT_9_RATE_LABEL = "9%"

#: 末尾固定声明（正文即产物，模型必须原样输出）。
_DECLARATION = "本表为按 ERP 收支流水估算的参考值，以代账实际申报为准。"

#: 留抵（应纳增值税 ≤ 0）时附加税行的固定文案。
_NO_VAT_SURCHARGE_LINE = "当期无应纳增值税（留抵），附加税计 0.00"

#: 出「单价类型未知」行时的末尾提示（说明这些行未计税的原因）。
_UNKNOWN_PRICE_HINT = (
    "单价类型未知的客户行金额照列、未计税（客户单价类型未知，无法判定是否含税）。"
)

#: 当期进项税额合计为 0 时的数据完整性提示。
#: ERP 的收支自动联动只覆盖「收款→钣金收款」「付款→塑粉付款」两条通道，
#: 电费/天然气只能手工录收支流水；漏录时进项为 0 → 报出的税偏高，故提示核对。
_INPUT_ZERO_HINT = (
    "当期未取到任何进项税额。若实际有电费、天然气或塑粉付款支出，"
    "请确认是否已录入 ERP 收支流水。"
)


class _CustomerRow(BaseModel):
    """销项/未计税收入/单价类型未知的明细行（按客户）。"""

    name: str = Field(description="客户名称（已用 customers.company_name 映射后的名称）")
    amount: Decimal = Field(
        description="该客户当期钣金收款金额合计（元）；由 erp_calc 按客户分组的"
        "结果逐行照搬，不要自己改数"
    )


class _AmountBlock(BaseModel):
    """按客户的金额块（销项桶 / 未计税桶 / 单价类型未知桶共用）。"""

    rows: list[_CustomerRow] = Field(
        description="按客户分组的金额明细（分组结果为 id 桶时，先用 erp_query 查 "
        "customers 取 company_name 再填 name）；当期无记录传空数组 []"
    )
    total: Decimal = Field(
        description="该块金额合计（元）——必须传 erp_calc 返回的 totals 里的合计，"
        "不要自己把各行相加"
    )


class _CategoryRow(BaseModel):
    """进项明细行（按类目）。"""

    category: str = Field(description="收支类目名称（以 ERP 实际名称为准）")
    amount: Decimal = Field(
        description="该类目当期支出合计（元）；由 erp_calc 按类目分组的结果逐行照搬"
    )


class _CategoryBlock(BaseModel):
    """按类目的进项块（13% 档 / 9% 档共用）。"""

    rows: list[_CategoryRow] = Field(
        description="按类目分组的支出明细；当期无记录传空数组 []"
    )
    total: Decimal = Field(
        description="该块支出合计（元）——必须传 erp_calc 返回的 totals 里的合计，"
        "不要自己把各行相加"
    )


class RenderTaxWorksheetArgs(BaseModel):
    """render_tax_worksheet 参数：各块均已算完、带各自合计的增值税估算入参。

    只用于**本流程最后一步**：各块（含税销项/未计税收入/单价类型未知/进项
    13% 档/进项 9% 档）都算完、拿到各自合计数之后调用它排版。各块合计一律传
    既有工具给的 totals（erp_calc 的 totals），**不要自己重算**。
    """

    period_start: date = Field(description="时段起始日（YYYY-MM-DD，含当日）")
    period_end: date = Field(description="时段截止日（YYYY-MM-DD，含当日；闭区间含首尾）")
    output_taxed: _AmountBlock = Field(
        description="含税客户的钣金收款（按客户）——只有这一块参与算税"
    )
    output_untaxed: _AmountBlock = Field(
        description="不含税客户的钣金收款（按客户）——**只展示、不计税**"
    )
    output_unknown: _AmountBlock = Field(
        description="单价类型未知（客户单价类型查不到）的钣金收款（按客户）"
        "——**只展示、不计税**，单独成一类"
    )
    input_13: _CategoryBlock = Field(
        description="进项 13% 档（塑粉付款、电费两类的当期支出，按类目）"
    )
    input_9: _CategoryBlock = Field(
        description="进项 9% 档（天然气一类的当期支出，按类目）"
    )

    @model_validator(mode="after")
    def _check_period_order(self) -> "RenderTaxWorksheetArgs":
        if self.period_start > self.period_end:
            raise ValueError(
                f"时段起止非法：period_start（{self.period_start.isoformat()}）晚于 "
                f"period_end（{self.period_end.isoformat()}）——闭区间要求起点不晚于终点"
            )
        return self


def _check_complete(label: str, rows_sum: Decimal, total: Decimal) -> str | None:
    """逐块完整性校验：明细逐行相加必须等于传入 total（容差 0.01）。

    不一致返回错误文案（点明是哪一块、两边各是多少、差多少），否则 None。
    """
    if abs(rows_sum - total) <= _TOLERANCE:
        return None
    return (
        f"{label}的明细合计与 total 不一致：明细逐行相加 = {_money(rows_sum)}，"
        f"total = {_money(total)}，差 {_money(rows_sum - total)}。"
        "请核对后重试（total 必须传对应工具给的合计数——erp_calc 的 totals，"
        "不要自己重算）"
    )


def _quantize(value: Decimal) -> Decimal:
    """金额统一量化到分（``ROUND_HALF_UP``）；负零归一为正零。

    量化后为 ``Decimal("-0.00")`` 时改记 ``Decimal("0.00")``——否则展示成
    ``-0.00``（如 ``-0.01`` 的含税项舍入到零），在估算表里没有意义、且会与
    「分」域相加的其它项视觉打架。
    """
    quantized = value.quantize(_TOLERANCE, rounding=ROUND_HALF_UP)
    if quantized == 0:
        return _TOLERANCE * 0  # Decimal("0.00")，正零
    return quantized


def _finite_float(value: Decimal) -> float | None:
    """``float(Decimal)`` 可能得 ``inf``（极大 Decimal）；非有限即记 None。

    与 render_financial_report 同款守卫：``json.dumps`` 默认会把 ``inf`` 写成
    非法 JSON token ``Infinity``，故非有限一律记 None（给不出可用数值）。
    """
    candidate = float(value)
    return candidate if math.isfinite(candidate) else None


def _output_tax(amount: Decimal) -> Decimal:
    """含税收款额 → 销项税额：``amount ÷ 113 × 13``（先除后乘，见常量注释）。"""
    return amount / _OUTPUT_DENOMINATOR * _OUTPUT_NUMERATOR


def _input_tax(amount: Decimal, rate: Decimal) -> Decimal:
    """不含税金额 → 进项税额：``amount × 税率 ÷ 100``（先乘后除）。"""
    return amount * rate / _PERCENT


def _sum_output_taxes(rows: list[_CustomerRow]) -> Decimal:
    """逐客户算销项税额、各自取整到分，再在「分」域相加（用户裁定）。"""
    return sum((_quantize(_output_tax(r.amount)) for r in rows), Decimal(0))


def _sum_input_taxes(rows: list[_CategoryRow], rate: Decimal) -> Decimal:
    """逐类目算进项税额、各自取整到分，再在「分」域相加（用户裁定）。"""
    return sum((_quantize(_input_tax(r.amount, rate)) for r in rows), Decimal(0))


def _section(
    title: str,
    headers: list[str],
    rows: list[list[str]],
    empty_cells: list[str],
    total_cells: list[str] | None,
) -> str:
    """渲染一张固定形态的 Markdown 表（标题 + 表头 + 明细/无记录 + 可选合计行）。

    rows 为空（当期无记录）时明细位置放 ``empty_cells`` 那一行，该表**照样
    出**、不省略、不留空；``total_cells`` 为 None 时不输出合计行（进项表按
    口径仅列类目行，进项合计在算式段给出）。
    """
    lines = [title, "| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    if rows:
        lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    else:
        lines.append("| " + " | ".join(empty_cells) + " |")
    if total_cells is not None:
        lines.append("| " + " | ".join(total_cells) + " |")
    return "\n".join(lines)


def run_render_tax_worksheet(
    ctx: ToolContext, args: RenderTaxWorksheetArgs
) -> ToolResultSummary:
    """把各块已算完的金额渲染成固定格式的当期增值税估算底稿（只读；不碰 ERP）。

    - 完整性 fail closed：任一块明细与 total 不符 → ok=False + 明确错误；
    - 税额一律逐行先算、各自取整到分、再在「分」域相加（表列合计即逐行之和）；
    - 算式段全部用已量化到分的值做运算：应纳增值税 = 销项税额 - 进项税额合计；
      附加税 = max(应纳增值税, 0) × 6 ÷ 100（留抵时为 0，绝不为负）；
      合计应缴 = 应纳增值税 + 附加税。
    """
    checks = [
        (
            "含税销项（按客户）",
            sum((r.amount for r in args.output_taxed.rows), Decimal(0)),
            args.output_taxed.total,
        ),
        (
            "未计税收入（按客户）",
            sum((r.amount for r in args.output_untaxed.rows), Decimal(0)),
            args.output_untaxed.total,
        ),
        (
            "单价类型未知（按客户）",
            sum((r.amount for r in args.output_unknown.rows), Decimal(0)),
            args.output_unknown.total,
        ),
        (
            "进项 13% 档（按类目）",
            sum((r.amount for r in args.input_13.rows), Decimal(0)),
            args.input_13.total,
        ),
        (
            "进项 9% 档（按类目）",
            sum((r.amount for r in args.input_9.rows), Decimal(0)),
            args.input_9.total,
        ),
    ]
    for label, rows_sum, total in checks:
        error = _check_complete(label, rows_sum, total)
        if error is not None:
            return ToolResultSummary(ok=False, summary={"error": error})

    # 估算算数：全部在「分」域（逐行取整相加 → 各步都在已量化值上运算）。
    output_tax_q = _sum_output_taxes(args.output_taxed.rows)
    input_13_tax_q = _sum_input_taxes(args.input_13.rows, _INPUT_13_RATE)
    input_9_tax_q = _sum_input_taxes(args.input_9.rows, _INPUT_9_RATE)
    input_tax_total_q = input_13_tax_q + input_9_tax_q
    vat_payable_q = output_tax_q - input_tax_total_q
    # 留抵保护：应纳增值税 ≤ 0 时附加税恒为 0（不得为负）；乘数用已量化的应纳增值税。
    surcharge_q = (
        Decimal("0.00")
        if vat_payable_q <= 0
        else _quantize(vat_payable_q * _SURCHARGE_RATE / _PERCENT)
    )
    total_payable_q = vat_payable_q + surcharge_q

    output_rows = [
        [_cell(r.name), _money(r.amount), _money(_quantize(_output_tax(r.amount)))]
        for r in args.output_taxed.rows
    ]
    untaxed_rows = [
        [_cell(r.name), _money(r.amount)] for r in args.output_untaxed.rows
    ]
    unknown_rows = [
        [_cell(r.name), _money(r.amount)] for r in args.output_unknown.rows
    ]
    input_rows = [
        [_cell(r.category), _money(r.amount), _INPUT_13_RATE_LABEL,
         _money(_quantize(_input_tax(r.amount, _INPUT_13_RATE)))]
        for r in args.input_13.rows
    ] + [
        [_cell(r.category), _money(r.amount), _INPUT_9_RATE_LABEL,
         _money(_quantize(_input_tax(r.amount, _INPUT_9_RATE)))]
        for r in args.input_9.rows
    ]

    sections = [
        _section(
            "## 当期销项（按客户）",
            ["客户", "收款金额", "销项税额"],
            output_rows,
            ["（当期无记录）", "0.00", "0.00"],
            ["**合计**", f"**{_money(args.output_taxed.total)}**",
             f"**{_money(output_tax_q)}**"],
        ),
        _section(
            "## 未计税收入（不含税客户，仅展示不计税）",
            ["客户", "收款金额"],
            untaxed_rows,
            ["（当期无记录）", "0.00"],
            ["**合计**", f"**{_money(args.output_untaxed.total)}**"],
        ),
        _section(
            "## 单价类型未知（仅展示不计税）",
            ["客户", "收款金额"],
            unknown_rows,
            ["（当期无记录）", "0.00"],
            ["**合计**", f"**{_money(args.output_unknown.total)}**"],
        ),
        _section(
            "## 当期进项（按类目）",
            ["类目", "金额", "税率", "进项税额"],
            input_rows,
            ["（当期无记录）", "0.00", "—", "0.00"],
            None,
        ),
    ]

    # 算式段（不进表格）：全部用已量化到分的值做运算。
    surcharge_line = (
        _NO_VAT_SURCHARGE_LINE
        if vat_payable_q <= 0
        else f"{_money(vat_payable_q)} × 6% = {_money(surcharge_q)}"
    )
    formula = "\n\n".join(
        [
            "销项税额\n"
            + "各含税客户收款额按「÷ 113 × 13」逐笔取整到分后合计 = "
            + _money(output_tax_q),
            "应纳增值税\n"
            + f"{_money(output_tax_q)} - {_money(input_tax_total_q)} = "
            + _money(vat_payable_q),
            "附加税\n" + surcharge_line,
            "合计应缴\n"
            + f"{_money(vat_payable_q)} + {_money(surcharge_q)} = "
            + _money(total_payable_q),
        ]
    )

    tails = [formula]
    if args.output_unknown.rows:
        tails.append(_UNKNOWN_PRICE_HINT)
    if input_tax_total_q == 0:
        tails.append(_INPUT_ZERO_HINT)
    tails.append(_DECLARATION)

    text = "\n\n".join(sections + tails)

    summary: dict[str, Any] = {
        "text": text,
        "period_start": args.period_start.isoformat(),
        "period_end": args.period_end.isoformat(),
        "output_tax": _finite_float(output_tax_q),
        "input_tax": _finite_float(input_tax_total_q),
        "vat_payable": _finite_float(vat_payable_q),
        "surcharge": _finite_float(surcharge_q),
        "total_payable": _finite_float(total_payable_q),
        "untaxed_revenue": _finite_float(_quantize(args.output_untaxed.total)),
        "untaxed_unknown_revenue": _finite_float(_quantize(args.output_unknown.total)),
    }
    return ToolResultSummary(ok=True, summary=summary)
