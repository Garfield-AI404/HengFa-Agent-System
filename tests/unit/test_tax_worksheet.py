"""render_tax_worksheet（当期增值税估算底稿渲染器）测试。

覆盖渲染工具的存在理由——**格式每次完全一致 + 全部算数由代码完成 + 印出的
每个数都能对上**：
- 标准场景：逐字钉住整段 Markdown（四张表 / 四行算式 / 末尾声明）；金额特意
  取**除不尽**的（1000.00 / 0.73 / 0.01+0.04 / 0.10），这样「逐客户取整到分
  再相加」与「先把合计去除再取整」会给出**不同的分位**，逐字钉不住就露馅；
- **机械验算**：从产出的 text 里把等式与表列解析回 Decimal，逐条核对
  （应纳增值税减法、附加税乘法、合计应缴加法、表列「逐行之和 == 合计行」），
  不靠人眼、不靠字符串对比；
- **口径钉死**：销项表列合计是**逐客户逐一取整后相加**（不是
  ``quantize(合计/113*13)``）——用一组能区分两者的金额正面验证；
- **留抵保护**（进项 > 销项）：应纳增值税为负、附加税恒 0；
- **单价类型未知桶**：单独成表、只列不计税、末尾出提示；进项税额合计为 0 时
  另出数据完整性提示；
- 完整性 fail closed：任一块明细与 total 不一致 → ok=False（五块各一条）；
- 五块全空：各表照样出、记 0.00、不报错；
- 负零归一：``-0.01`` 的含税项取整到零后记 ``0.00`` 而非 ``-0.00``；
- ``_finite_float`` 非有限值守卫；
- 名称单元格转义 ``|`` / 换行不破坏固定模板；
- 时段倒置 → 参数校验失败；
- 「正文即产物」：大载荷经真实 ``tools_node`` 主路径派发不被字符预算腰斩。

ToolSpec 登记：只读、非写、无规则包、无 prepare、manager 准入、无日期版本
标记的模型可见 description。
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal

import pytest
from pydantic import ValidationError

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.tools_node import (
    TOOL_RESULT_HISTORY_CHARS,
    tools_node,
)
from administrative_agent.agent.prompts.skills import SKILL_REGISTRY
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.tax_worksheet import (
    RenderTaxWorksheetArgs,
    _DECLARATION,
    _INPUT_ZERO_HINT,
    _finite_float,
    _input_tax,
    _output_tax,
    _quantize,
    run_render_tax_worksheet,
)

TOOL_NAME = "render_tax_worksheet"
SKILL_KEY = "SK.tax_estimate"


def _block(rows, key, total):
    """构造一个明细块 dict（rows 元素为 (name, amount)）。"""
    return {
        "rows": [{key: name, "amount": amount} for name, amount in rows],
        "total": total,
    }


def _make_args(**overrides) -> RenderTaxWorksheetArgs:
    """默认一份正常五块参数（各块均可覆盖）。金额特意取除不尽的组合。"""
    data = {
        "period_start": "2026-08-01",
        "period_end": "2026-08-31",
        "output_taxed": _block(
            [("甲公司", "1000.00"), ("乙公司", "0.73"), ("丙公司", "0.01"),
             ("丁公司", "0.04")],
            "name",
            "1000.78",
        ),
        "output_untaxed": _block([("戊公司", "500.00")], "name", "500.00"),
        "output_unknown": _block([("己公司", "200.00")], "name", "200.00"),
        "input_13": _block(
            [("塑粉付款", "800.00"), ("电费", "0.73")], "category", "800.73"
        ),
        "input_9": _block([("天然气", "0.10")], "category", "0.10"),
    }
    data.update(overrides)
    return RenderTaxWorksheetArgs(**data)


def _run(**overrides):
    return run_render_tax_worksheet(ToolContext(), _make_args(**overrides))


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dec(cell: str) -> Decimal:
    """把表格单元格里的金额（可能带 ``**`` 与千分位）解析回 Decimal。"""
    return Decimal(cell.replace("*", "").replace(",", "").strip())


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(text: str, heading: str) -> list[list[str]]:
    """取某表标题下的数据行（含合计行；不含表头与分隔行）。"""
    section = text.split(heading + "\n", 1)[1].split("\n\n", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return [_cells(r) for r in rows[2:]]


def _section_line(text: str, label: str) -> str:
    """取算式段某标签下一行的正文（如「应纳增值税」下的等式）。"""
    match = re.search(re.escape(label) + r"\n([^\n]+)", text)
    assert match is not None, f"text 中找不到算式段标签：{label}"
    return match.group(1)


def _nums(line: str) -> list[Decimal]:
    """行内所有 ``-?千分位.两位`` 数字，按出现顺序解析回 Decimal。"""
    return [_dec(m) for m in re.findall(r"-?[\d,]+\.\d{2}", line)]


# ------------------------------------------------------------------ 正常渲染

_EXPECTED_TEXT = (
    "## 当期销项（按客户）\n"
    "| 客户 | 收款金额 | 销项税额 |\n"
    "|---|---|---|\n"
    "| 甲公司 | 1,000.00 | 115.04 |\n"
    "| 乙公司 | 0.73 | 0.08 |\n"
    "| 丙公司 | 0.01 | 0.00 |\n"
    "| 丁公司 | 0.04 | 0.00 |\n"
    "| **合计** | **1,000.78** | **115.12** |\n"
    "\n"
    "## 未计税收入（不含税客户，仅展示不计税）\n"
    "| 客户 | 收款金额 |\n"
    "|---|---|\n"
    "| 戊公司 | 500.00 |\n"
    "| **合计** | **500.00** |\n"
    "\n"
    "## 单价类型未知（仅展示不计税）\n"
    "| 客户 | 收款金额 |\n"
    "|---|---|\n"
    "| 己公司 | 200.00 |\n"
    "| **合计** | **200.00** |\n"
    "\n"
    "## 当期进项（按类目）\n"
    "| 类目 | 金额 | 税率 | 进项税额 |\n"
    "|---|---|---|---|\n"
    "| 塑粉付款 | 800.00 | 13% | 104.00 |\n"
    "| 电费 | 0.73 | 13% | 0.09 |\n"
    "| 天然气 | 0.10 | 9% | 0.01 |\n"
    "\n"
    "销项税额\n"
    "各含税客户收款额按「÷ 113 × 13」逐笔取整到分后合计 = 115.12\n"
    "\n"
    "应纳增值税\n"
    "115.12 - 104.10 = 11.02\n"
    "\n"
    "附加税\n"
    "11.02 × 6% = 0.66\n"
    "\n"
    "合计应缴\n"
    "11.02 + 0.66 = 11.68\n"
    "\n"
    "单价类型未知的客户行金额照列、未计税（客户单价类型未知，无法判定是否含税）。\n"
    "\n"
    "本表为按 ERP 收支流水估算的参考值，以代账实际申报为准。"
)


class TestNormalRender:
    def test_text_matches_fixed_template_verbatim(self) -> None:
        """逐字钉住整段输出——「格式每次完全一致」的核心断言。

        金额取除不尽的组合：若把表列合计改回 ``quantize(合计/113*13)``（=115.13）
        或把逐行取整挪回整体取整，逐字比对即失败。
        """
        result = _run()
        assert result.ok is True
        assert result.summary["text"] == _EXPECTED_TEXT

    def test_four_headings_and_declaration(self) -> None:
        text = _run().summary["text"]
        assert _headings(text) == [
            "## 当期销项（按客户）",
            "## 未计税收入（不含税客户，仅展示不计税）",
            "## 单价类型未知（仅展示不计税）",
            "## 当期进项（按类目）",
        ]
        assert text.endswith(
            "本表为按 ERP 收支流水估算的参考值，以代账实际申报为准。"
        )

    def test_summary_numbers_match_hand_computed(self) -> None:
        summary = _run().summary
        # 手算（逐笔取整到分再相加）：
        # 销项 = q(1000/113*13)+q(0.73/113*13)+q(0.01/113*13)+q(0.04/113*13)
        #      = 115.04 + 0.08 + 0.00 + 0.00 = 115.12
        # 进项 = q(800*13/100)+q(0.73*13/100) + q(0.10*9/100)
        #      = 104.00 + 0.09 + 0.01 = 104.10
        # 应纳 = 115.12-104.10 = 11.02；附加 = q(11.02*6/100)=0.66；合计 11.68
        assert summary["output_tax"] == 115.12
        assert summary["input_tax"] == 104.10
        assert summary["vat_payable"] == 11.02
        assert summary["surcharge"] == 0.66
        assert summary["total_payable"] == 11.68
        assert summary["untaxed_revenue"] == 500.00
        assert summary["untaxed_unknown_revenue"] == 200.00
        assert summary["period_start"] == "2026-08-01"
        assert summary["period_end"] == "2026-08-31"

    def test_untaxed_blocks_do_not_affect_tax(self) -> None:
        """未计税收入与单价类型未知两块改金额，不影响任何税额字段。"""
        base = _run().summary
        changed = _run(
            output_untaxed=_block([("戊公司", "999999.99")], "name", "999999.99"),
            output_unknown=_block([("己公司", "888888.88")], "name", "888888.88"),
        ).summary
        for key in (
            "output_tax", "input_tax", "vat_payable", "surcharge", "total_payable",
        ):
            assert base[key] == changed[key], key
        assert changed["untaxed_revenue"] == 999999.99
        assert changed["untaxed_unknown_revenue"] == 888888.88


# ------------------------------------------------------ 机械验算：等式与表列

class TestMechanicalReconciliation:
    """把 text 解析回 Decimal 逐条核对——不靠字符串对比，靠算。

    这一组是 Fix「印出的每个数都要自洽」的硬判据：把任何一步的量化顺序改错
    （如先加再取整、或改用未量化的应纳增值税算附加税），等式核不上即红。
    """

    def test_output_table_column_totals_equal_sum_of_rows(self) -> None:
        text = _run().summary["text"]
        rows = _table(text, "## 当期销项（按客户）")
        detail = [r for r in rows if "合计" not in r[0]]
        total = [r for r in rows if "合计" in r[0]][0]
        assert sum(_dec(r[1]) for r in detail) == _dec(total[1])
        assert sum(_dec(r[2]) for r in detail) == _dec(total[2])

    def test_input_table_tax_column_sums_to_summary_input_tax(self) -> None:
        summary = _run().summary
        rows = _table(summary["text"], "## 当期进项（按类目）")
        assert sum(_dec(r[3]) for r in rows) == Decimal(str(summary["input_tax"]))
        assert sum(_dec(r[3]) for r in rows) == Decimal("104.10")

    def test_input_table_has_no_total_row(self) -> None:
        """按口径：进项表只列类目行，不出合计行（合计在算式段给出）。"""
        rows = _table(_run().summary["text"], "## 当期进项（按类目）")
        assert all("合计" not in r[0] for r in rows)

    @pytest.mark.parametrize(
        ("field", "rows", "total", "rate"),
        [
            # 0.50×13÷100 = 0.065 恰在 HALF_UP(0.07) 与 _money 环境上下文
            # HALF_EVEN(0.06) 的分歧点上：行内税额若漏掉 _quantize，印出的数
            # 就变 0.06，与 summary 的 0.07 也打架 → 本参数化即真锁。
            ("input_13", [("塑粉付款", "0.50")], "0.50", Decimal(13)),
            # 0.50×9÷100 = 0.045 同理（HALF_UP 0.05 / HALF_EVEN 0.04）。
            ("input_9", [("天然气", "0.50")], "0.50", Decimal(9)),
        ],
    )
    def test_input_row_tax_is_per_row_quantized(
        self, field, rows, total, rate
    ) -> None:
        """进项表「进项税额」列逐行 `_quantize`（ROUND_HALF_UP），且逐行之和
        必须等于 summary 的 input_tax。"""
        summary = _run(**{field: _block(rows, "category", total)}).summary
        table = _table(summary["text"], "## 当期进项（按类目）")
        assert sum(_dec(r[3]) for r in table) == _dec(str(summary["input_tax"]))
        row = next(r for r in table if r[0] == rows[0][0])
        assert _dec(row[3]) == _q(_dec(rows[0][1]) * rate / Decimal(100))

    def test_vat_payable_line_is_output_minus_input(self) -> None:
        summary = _run().summary
        line = _section_line(summary["text"], "应纳增值税")
        a, b, c = _nums(line)
        assert a == Decimal(str(summary["output_tax"]))
        assert b == Decimal(str(summary["input_tax"]))
        assert a - b == c
        assert c == Decimal(str(summary["vat_payable"]))

    def test_surcharge_line_is_quantized_vat_times_6_percent(self) -> None:
        summary = _run().summary
        line = _section_line(summary["text"], "附加税")
        a, b = _nums(line)
        assert a == Decimal(str(summary["vat_payable"]))
        assert _q(a * Decimal(6) / Decimal(100)) == b
        assert b == Decimal(str(summary["surcharge"]))

    @pytest.mark.parametrize(
        ("out", "in13", "in9"),
        [
            # 默认那组金额恰好在「附加税乘数用未量化 vat」这一变异下与正确值
            # 同分（不可区分）；下面这几组是复审实测**能区分**的：正确走已量化
            # 的 vat_q，变异走未量化的 vat（乘数差一点，末位差 1 分）。
            ("49256.93", "41089.44", "14.98"),
            ("45629.28", "21833.6", "20945.52"),
            ("48486.35", "29600.96", "6394.91"),
            ("49525.79", "13818.08", "36517.36"),
        ],
    )
    def test_surcharge_uses_quantized_vat_on_distinguishing_inputs(
        self, out, in13, in9
    ) -> None:
        """非默认金额上钉死「附加税 = q(已量化应纳增值税 × 6 ÷ 100)」。

        同时断言该组输入确实能区分「量化 vat」与「未量化 vat」两种乘数——否则
        这组输入对变异 ② 无区分力，就等于没加。
        """
        summary = _run(
            output_taxed=_block([("甲", out)], "name", out),
            input_13=_block([("塑粉付款", in13)], "category", in13),
            input_9=_block([("天然气", in9)], "category", in9),
        ).summary
        out_q = _q(_output_tax(Decimal(out)))
        in13_q = _q(_input_tax(Decimal(in13), Decimal(13)))
        in9_q = _q(_input_tax(Decimal(in9), Decimal(9)))
        vat_q = out_q - in13_q - in9_q
        assert Decimal(str(summary["vat_payable"])) == vat_q
        assert Decimal(str(summary["surcharge"])) == _q(
            vat_q * Decimal(6) / Decimal(100)
        )
        # 区分力：未量化输出额（= 变异若把 sum 的逐行取整拿掉所得）会给出不同的分位
        raw_vat = _output_tax(Decimal(out)) - in13_q - in9_q
        assert _q(raw_vat * Decimal(6) / Decimal(100)) != _q(
            vat_q * Decimal(6) / Decimal(100)
        )

    def test_total_payable_line_is_vat_plus_surcharge(self) -> None:
        summary = _run().summary
        line = _section_line(summary["text"], "合计应缴")
        a, b, c = _nums(line)
        assert a == Decimal(str(summary["vat_payable"]))
        assert b == Decimal(str(summary["surcharge"]))
        assert a + b == c
        assert c == Decimal(str(summary["total_payable"]))

    def test_output_tax_summary_matches_row_summation_not_aggregate(self) -> None:
        """销项表列合计 = 逐客户取整后相加（115.12），**不是**整体取整（115.13）。

        这组金额是特意选的：两种口径差 1 分，正面钉住 Fix 的正确算法。
        """
        rows = _table(_run().summary["text"], "## 当期销项（按客户）")
        per_row = sum(_dec(r[2]) for r in rows if "合计" not in r[0])
        aggregate = _q(Decimal("1000.78") / Decimal(113) * Decimal(13))
        assert per_row == Decimal("115.12")
        assert aggregate == Decimal("115.13")
        assert per_row != aggregate
        assert _run().summary["output_tax"] == float(per_row)


# ------------------------------------------------------------------ 留抵保护


class TestCarryoverProtection:
    def test_input_exceeds_output_surcharge_is_zero(self) -> None:
        """留抵场景（进项 > 销项）：应纳增值税为负、附加税恒为 0、合计应缴 ==
        应纳增值税。"""
        result = _run(
            output_taxed=_block([("甲公司", "1130.00")], "name", "1130.00"),
            input_13=_block([("塑粉付款", "2000.00")], "category", "2000.00"),
            input_9=_block([("天然气", "100.00")], "category", "100.00"),
        )
        # 销项 = 1130/113*13 = 130；进项 = 2000*13/100 + 100*9/100 = 269 - 130
        assert result.summary["output_tax"] == 130.00
        assert result.summary["input_tax"] == 269.00
        assert result.summary["vat_payable"] == -139.00
        assert result.summary["surcharge"] == 0.0
        assert result.summary["total_payable"] == result.summary["vat_payable"]
        text = result.summary["text"]
        assert "当期无应纳增值税（留抵），附加税计 0.00" in text
        assert "-139.00 + 0.00 = -139.00" in text
        # 留抵时附加税行不是乘法算式（不出现 "× 6%" 的负值错算）
        assert "-139.00 × 6%" not in text

    def test_exactly_zero_vat_surcharge_is_zero(self) -> None:
        """应纳增值税恰为 0：附加税为 0（≤ 0 口径），合计应缴 0。"""
        result = _run(
            output_taxed=_block([("甲公司", "1130.00")], "name", "1130.00"),
            input_13=_block([("塑粉付款", "1000.00")], "category", "1000.00"),
            input_9=_block([], "category", "0"),
        )
        # 销项 130；进项 1000*13/100 = 130；应纳 0
        assert result.summary["vat_payable"] == 0.0
        assert result.summary["surcharge"] == 0.0
        assert result.summary["total_payable"] == 0.0


# ---------------------------------------------------- 单价类型未知桶 / 提示行


class TestUnknownBucketAndHints:
    def test_unknown_rows_shown_and_not_taxed(self) -> None:
        result = _run(
            output_unknown=_block([("某客户", "777.77")], "name", "777.77")
        )
        text = result.summary["text"]
        assert "## 单价类型未知（仅展示不计税）" in text
        assert "| 某客户 | 777.77 |" in text
        assert result.summary["untaxed_unknown_revenue"] == 777.77
        assert "单价类型未知的客户行金额照列、未计税" in text

    def test_unknown_hint_absent_when_no_unknown_rows(self) -> None:
        text = _run(output_unknown=_block([], "name", "0")).summary["text"]
        assert "单价类型未知的客户行金额照列、未计税" not in text
        # 空桶时该表照样出、记 0.00
        assert "## 单价类型未知（仅展示不计税）" in text
        assert "| （当期无记录） | 0.00 |" in text

    def test_input_zero_hint_present_when_no_input_tax(self) -> None:
        result = _run(
            input_13=_block([], "category", "0"),
            input_9=_block([], "category", "0"),
        )
        text = result.summary["text"]
        assert result.summary["input_tax"] == 0.0
        assert _INPUT_ZERO_HINT in text
        # 位置：提示必须在算式段（「合计应缴」行）**之后**、声明行**仍最末**——
        # 若被挪到表格之前就会被淹没，此断言即红（用户裁定「末尾加一行」）。
        assert text.index(_INPUT_ZERO_HINT) > text.index("合计应缴")
        assert text.rstrip().endswith(_DECLARATION)
        assert text.index(_INPUT_ZERO_HINT) < text.index(_DECLARATION)

    def test_input_zero_hint_absent_when_input_tax_present(self) -> None:
        text = _run().summary["text"]
        assert _INPUT_ZERO_HINT not in text


# -------------------------------------------------------- 完整性校验 fail closed


class TestCompletenessFailClosed:
    @pytest.mark.parametrize(
        ("override", "label"),
        [
            ({"output_taxed": _block([("甲", "100")], "name", "200")}, "含税销项（按客户）"),
            (
                {"output_untaxed": _block([("乙", "100")], "name", "200")},
                "未计税收入（按客户）",
            ),
            (
                {"output_unknown": _block([("丙", "100")], "name", "200")},
                "单价类型未知（按客户）",
            ),
            (
                {"input_13": _block([("塑粉付款", "100")], "category", "200")},
                "进项 13% 档（按类目）",
            ),
            (
                {"input_9": _block([("天然气", "100")], "category", "200")},
                "进项 9% 档（按类目）",
            ),
        ],
    )
    def test_mismatch_fails_closed_with_both_values(self, override, label) -> None:
        result = _run(**override)
        assert result.ok is False
        error = result.summary["error"]
        assert label in error
        assert "100.00" in error  # 明细逐行相加
        assert "200.00" in error  # 传入 total
        assert "差" in error
        assert "text" not in result.summary

    def test_tolerance_within_one_cent_passes(self) -> None:
        assert _run(output_taxed=_block([("甲", "100.00")], "name", "100.01")).ok is True

    def test_beyond_tolerance_fails(self) -> None:
        assert _run(output_taxed=_block([("甲", "100.00")], "name", "100.02")).ok is False


# ---------------------------------------------------------------- 空块照样出表


class TestEmptyBlocks:
    def test_all_empty_still_renders_tables_and_zeros(self) -> None:
        result = _run(
            output_taxed=_block([], "name", "0"),
            output_untaxed=_block([], "name", "0"),
            output_unknown=_block([], "name", "0"),
            input_13=_block([], "category", "0"),
            input_9=_block([], "category", "0"),
        )
        text = result.summary["text"]
        assert result.ok is True
        assert len(_headings(text)) == 4  # 四张表一张不少
        assert "| （当期无记录） | 0.00 | 0.00 |" in text  # 销项表
        assert text.count("| （当期无记录） | 0.00 |") >= 2  # 未计税表 / 未知表
        assert "| （当期无记录） | 0.00 | — | 0.00 |" in text  # 进项表
        assert text.endswith(
            "本表为按 ERP 收支流水估算的参考值，以代账实际申报为准。"
        )
        for key in ("output_tax", "input_tax", "vat_payable", "total_payable"):
            assert result.summary[key] == 0.0


# ------------------------------------------------------------------ 参数校验


class TestArgsValidation:
    def test_period_order_enforced(self) -> None:
        with pytest.raises(ValidationError, match="时段起止非法"):
            _make_args(period_start="2026-08-31", period_end="2026-08-01")


# ------------------------------------------------------------- 税率与算式口径


class TestTaxFormulaCaliber:
    def test_output_tax_is_divide_113_times_13_not_ratio(self) -> None:
        """含税收款是含税价、销项税额 = 收款额 ÷ 113 × 13，**不是** ×0.115044。

        金额 179.54 上两种算法在「分」上不同：÷113×13 → 20.66，
        ×0.115044 → 20.65。断言取前者并显式否掉后者，钉死口径。
        """
        total = Decimal("179.54")
        expected = _q(total / Decimal(113) * Decimal(13))
        wrong = _q(total * Decimal("0.115044"))
        assert expected == Decimal("20.66")
        assert wrong == Decimal("20.65")  # 证明该金额确实能区分两种算法
        result = _run(output_taxed=_block([("甲", "179.54")], "name", "179.54"))
        assert result.summary["output_tax"] == 20.66
        assert "| 甲 | 179.54 | 20.66 |" in result.summary["text"]
        assert "÷ 113 × 13" in result.summary["text"]

    @pytest.mark.parametrize(
        ("amount", "rate", "expected"),
        [
            ("1200.00", Decimal("13"), "156.00"),
            ("100.00", Decimal("9"), "9.00"),
            ("113.00", Decimal("13"), "14.69"),  # 14.69 恰为精确值
        ],
    )
    def test_input_tax_is_amount_times_rate_over_100(
        self, amount: str, rate: Decimal, expected: str
    ) -> None:
        assert _input_tax(Decimal(amount), rate) == Decimal(expected)

    def test_helpers_are_module_level_read_only(self) -> None:
        # 纯函数，无副作用：同输入同输出
        assert _output_tax(Decimal("11300")) == Decimal("1300")
        assert _output_tax(Decimal("11300")) == _output_tax(Decimal("11300"))


# ---------------------------------------------------------- 负零 / 有限性守卫


class TestNumericGuards:
    def test_negative_zero_normalized_to_positive_zero(self) -> None:
        """``-0.01`` 的含税项取整到零后记 ``0.00`` 而非 ``-0.00``。

        可达路径：output_taxed 只有一个 amount=-0.01 的行、total 亦为 -0.01
        （完整性校验通过）；其销项税额 = q(-0.01/113*13) = -0.00 → 归一 0.00。
        """
        result = _run(output_taxed=_block([("甲", "-0.01")], "name", "-0.01"))
        assert result.ok is True
        text = result.summary["text"]
        assert "| 甲 | -0.01 | 0.00 |" in text
        assert "-0.00" not in text
        assert result.summary["output_tax"] == 0.0

    def test_quantize_negative_zero_is_positive(self) -> None:
        assert str(_quantize(Decimal("-0.001"))) == "0.00"
        assert _quantize(Decimal("-0.001")).is_signed() is False

    def test_finite_float_guards_non_finite(self) -> None:
        assert _finite_float(Decimal("1.5")) == 1.5
        assert _finite_float(Decimal("1E+400")) is None  # float → inf


# ---------------------------------------------------------------- 单元格转义


class TestCellEscaping:
    def test_pipe_in_name_does_not_add_columns(self) -> None:
        text = _run(
            output_taxed=_block([("甲|伪造", "1130.00")], "name", "1130.00")
        ).summary["text"]
        assert "| 甲\\|伪造 | 1,130.00 | 130.00 |" in text

    def test_newline_in_name_does_not_add_rows(self) -> None:
        def render(name: str) -> str:
            return _run(
                output_taxed=_block([(name, "1130.00")], "name", "1130.00")
            ).summary["text"]

        control = render("甲")
        injected = render("甲\n| 伪造 | 9,999.00 |")
        assert len(injected.splitlines()) == len(control.splitlines())
        assert "| 伪造 |" not in injected


# ----------------------------------------------------- ToolSpec / skill 登记


class TestToolSpec:
    def test_registered_as_readonly_tool(self) -> None:
        spec = TOOL_REGISTRY[TOOL_NAME]
        assert spec.is_write is False
        assert spec.requires_manager is True
        assert spec.prepare is None
        assert spec.policy_family == ()
        assert "erp:query" in spec.required_scopes
        assert spec.duty_code == "QRY"

    def test_description_pins_verbatim_output_and_no_self_compute(self) -> None:
        spec = TOOL_REGISTRY[TOOL_NAME]
        assert "原样输出" in spec.description
        assert "不要自己算任何税额" in spec.description
        assert "totals" in spec.description
        # 模型可见文本不得含日期/版本标记
        assert "[2026" not in spec.description


class TestSkillRegistration:
    def test_skill_declares_the_renderer_tool(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        assert TOOL_NAME in pkg.tools
        assert pkg.tools == (
            "erp_calc", "erp_query", "list_entities", "render_tax_worksheet",
        )


# ------------------------------------------------ 「正文即产物」大载荷端到端


def _name20(prefix: str, i: int) -> str:
    return (f"{prefix}{i:02d}" + "公司名称占位占位占位占位占位占位")[:20]


def _big_args() -> RenderTaxWorksheetArgs:
    """一个会踩 TOOL_RESULT_HISTORY_CHARS(4096) 的大载荷。

    200 个客户明细行（100 含税 + 100 不含税）+ 50 未知、名称 20 字：
    ``json.dumps(summary)`` 约 > 4096（实测），足以触发摘要器的缺省分支。
    """
    return RenderTaxWorksheetArgs(
        period_start="2026-07-01",
        period_end="2026-07-31",
        output_taxed=_block(
            [(_name20("客户", i), "100.00") for i in range(100)], "name", "10000.00"
        ),
        output_untaxed=_block(
            [(_name20("客户", i + 100), "200.00") for i in range(100)],
            "name",
            "20000.00",
        ),
        output_unknown=_block(
            [(_name20("客户", i + 200), "300.00") for i in range(50)],
            "name",
            "15000.00",
        ),
        input_13=_block(
            [("塑粉付款", "1000.00"), ("电费", "200.00")], "category", "1200.00"
        ),
        input_9=_block([("天然气", "100.00")], "category", "100.00"),
    )


class TestVerbatimTextExemption:
    def test_tools_node_main_path_wires_tool_name_end_to_end(self) -> None:
        """端到端：真实 ``tools_node`` 主路径派发 render_tax_worksheet（大载荷）。

        锁定 VERBATIM_TEXT_TOOL_NAMES 的接线：漏注册则回落到 4096 腰斩（非法
        JSON），本用例必失败。**不 monkeypatch 摘要器**，走真实回路。
        """
        result = run_render_tax_worksheet(ToolContext(), _big_args())
        assert result.ok is True
        payload = json.dumps({"ok": result.ok, **result.summary}, ensure_ascii=False)
        assert len(payload) > TOOL_RESULT_HISTORY_CHARS  # 先证明确会踩阈值

        runtime = RuntimeBag(
            tools=TOOL_REGISTRY,
            context=AgentContext(
                user_id="manager01",
                user_role="manager",
                scope=frozenset({"erp:query"}),
                thread_id="thread-1",
            ),
        )
        state = {
            "messages": [],
            "pending_action": {
                "tool_name": TOOL_NAME,
                "tool_arguments": _big_args().model_dump(mode="json"),
            },
        }
        out = tools_node(state, runtime)
        content = out["messages"][0].content
        parsed = json.loads(content)  # ① 合法 JSON（未被腰斩）
        assert parsed["text"] == result.summary["text"]  # ② 正文逐字完整
        assert len(content) > TOOL_RESULT_HISTORY_CHARS  # ③ 确实未截断

    def test_verbatim_whitelist_contains_tax_worksheet(self) -> None:
        from administrative_agent.agent.nodes.tools_node import (
            VERBATIM_TEXT_TOOL_NAMES,
        )

        assert TOOL_NAME in VERBATIM_TEXT_TOOL_NAMES
