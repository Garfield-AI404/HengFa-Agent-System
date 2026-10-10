"""render_financial_report（财报固定格式渲染器）测试（[2026-09-13] 新增）。

覆盖渲染工具的存在理由——**格式每次完全一致**：
- 正常五表：逐字钉住整段 Markdown（标题/表头/分隔行/合计行/净利润算式行），
  ``## `` 标题恰好 5 个、净利润行有且仅有一行且不含 ``|``（不在表格里）；
- 某项 rows 为空：该表照样出，明细位置 ``| （当期无记录） | 0.00 |``；
- 净利润为负：负号 ASCII ``-``；
- 完整性 fail closed：任一明细与 total 不一致 → ok=False，错误点明项名与两边数值；
- 金额千分位 + 2 位小数；
- 房租只算 单价 × 天数、净利润 = 五项合计加减、塑粉占比 = 塑粉合计 ÷ 钣金合计
  （工具仅有的三笔算数；除法钉 ``ROUND_HALF_UP``、>100% 不截断、除数为零记
  「无法计算」仍出报告）；
- ToolSpec 登记：只读、非写、无规则包、无 prepare、manager 准入。

[2026-09-14 补]：
- 「正文即产物」豁免：大月份（正文 > 4096）的固定 Markdown 不被腰斩（正向），
  且豁免**仅认工具名**——memory_lookup / tool_name=None 仍受 4096 约束（反向护栏）；
- ``rent.days`` 必须等于时段含首尾自然日数（少 1 / 多 1 / 0 / 负数各一条）；
- 名称类单元格转义：含 ``|`` 不加列、含换行不多行，正常名称输出逐字不变。
"""

from __future__ import annotations

import decimal
import json
import math
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.tools_node import (
    TOOL_RESULT_HISTORY_CHARS,
    fallback_truncate_result,
    summarize_tool_result,
    tools_node,
)
from administrative_agent.agent.prompts.skills import SKILL_REGISTRY
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.financial_report import (
    RenderFinancialReportArgs,
    run_render_financial_report,
)

TOOL_NAME = "render_financial_report"


def _block(rows, key, total, **kw):
    """构造一个明细块 dict（rows 元素为 (name, value)）。"""
    return {
        "rows": [{key: name, (kw.get("value_key", "amount")): value} for name, value in rows],
        "total": total,
    }


def _make_args(**overrides) -> RenderFinancialReportArgs:
    """默认一份正常五表参数（metal/powder/dynamic/salary/rent 均可覆盖）。"""
    data = {
        "period_start": "2026-08-01",
        "period_end": "2026-08-31",
        "metal": _block([("甲公司", "1000.00"), ("乙公司", "2345.67")], "name", "3345.67"),
        "powder": _block([("供货商A", "500")], "name", "500"),
        "dynamic": _block([("运费", "100.5"), ("电费", "200")], "category", "300.5"),
        "salary": _block(
            [("张三", "5000"), ("李四", "6000")], "employee", "11000", value_key="wage"
        ),
        "rent": {"unit_price": "1000", "days": 31},
    }
    data.update(overrides)
    return RenderFinancialReportArgs(**data)


def _run(**overrides):
    return run_render_financial_report(ToolContext(), _make_args(**overrides))


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def _net_line(text: str) -> str:
    """净利润算式行——不含 ``|``、也不以 ``## `` 开头、含 `` = `` 的行。

    注意：自 [2026-09-14] 起全文还有第二处 `` = ``（塑粉占比行），故本函数
    按输入顺序返回**第一处**（净利润行在比值行之前）。比值行请用 ``_ratio_line``。
    """
    return next(
        line
        for line in text.splitlines()
        if " = " in line and "|" not in line
    )


def _ratio_line(text: str) -> str:
    """塑粉金额占钣金订单金额比行——全文唯一含 ``÷`` 且不含 ``|`` 的行。

    进任何表格的行都以 ``|`` 开头，故该行必不在表格内；除数为零时该行含
    ``→`` 而不含 `` = ``。
    """
    return next(line for line in text.splitlines() if "÷" in line and "|" not in line)


# ------------------------------------------------------------------ 正常五表


class TestNormalRender:
    def test_text_matches_fixed_template_verbatim(self) -> None:
        """逐字钉住整段输出——这是「格式每次完全一致」的核心断言。"""
        result = _run()
        assert result.ok is True
        assert result.summary["text"] == (
            "## 当期钣金订单金额\n"
            "| 客户 | 金额 |\n"
            "|---|---|\n"
            "| 甲公司 | 1,000.00 |\n"
            "| 乙公司 | 2,345.67 |\n"
            "| **合计** | **3,345.67** |\n"
            "\n"
            "## 当期塑粉订单金额\n"
            "| 供货商 | 金额 |\n"
            "|---|---|\n"
            "| 供货商A | 500.00 |\n"
            "| **合计** | **500.00** |\n"
            "\n"
            "## 当期动态账款\n"
            "| 类目 | 金额 |\n"
            "|---|---|\n"
            "| 运费 | 100.50 |\n"
            "| 电费 | 200.00 |\n"
            "| **合计** | **300.50** |\n"
            "\n"
            "## 工资账款\n"
            "| 员工 | 工资 |\n"
            "|---|---|\n"
            "| 张三 | 5,000.00 |\n"
            "| 李四 | 6,000.00 |\n"
            "| **合计** | **11,000.00** |\n"
            "\n"
            "## 房租账款\n"
            "| 项目 | 金额 |\n"
            "|---|---|\n"
            "| 1,000.00元 × 31天 | 31,000.00 |\n"
            "| **合计** | **31,000.00** |\n"
            "\n"
            "净利润（不用表格）\n"
            "3,345.67 - 500.00 - 300.50 - 11,000.00 - 31,000.00 = -39,454.83"
            "\n"
            "\n"
            "塑粉金额占钣金订单金额比\n"
            "500.00 ÷ 3,345.67 = 14.94%"
        )

    def test_exactly_five_headings_and_two_formula_lines(self) -> None:
        text = _run().summary["text"]
        assert len(_headings(text)) == 5
        assert _headings(text) == [
            "## 当期钣金订单金额",
            "## 当期塑粉订单金额",
            "## 当期动态账款",
            "## 工资账款",
            "## 房租账款",
        ]
        # 全文恰好两处 " = "：① 净利润算式行（五项加减）；② 塑粉占比算式行
        # （塑粉 ÷ 钣金 = 百分比）。本工具共三笔算数（房租乘法、净利润加减、
        # 塑粉占比除法），但只有这两笔以 " = " 出算式行（房租乘法在表格单元格内）。
        assert text.count(" = ") == 2

    def test_net_profit_is_not_in_a_table(self) -> None:
        """净利润只出算式行：该行不含 ``|``，且不出现在任何表格行里。"""
        text = _run().summary["text"]
        net_line = _net_line(text)
        assert "|" not in net_line
        assert net_line.endswith("= -39,454.83")
        for line in text.splitlines():
            if line.startswith("|"):
                assert "-39,454.83" not in line

    def test_net_profit_numeric_and_totals_in_summary(self) -> None:
        summary = _run().summary
        assert summary["net_profit"] == -39454.83
        assert summary["totals"] == {
            "metal": "3,345.67",
            "powder": "500.00",
            "dynamic": "300.50",
            "salary": "11,000.00",
            "rent": "31,000.00",
        }

    def test_rent_is_unit_price_times_days(self) -> None:
        # 默认时段 2026-08-01..08-31 = 31 天，故 days 必须为 31（[2026-09-14] 新增校验）
        text = _run(rent={"unit_price": "1200.50", "days": 31}).summary["text"]
        assert "| 1,200.50元 × 31天 | 37,215.50 |" in text

    def test_positive_net_profit_uses_ascii_hyphen_operator(self) -> None:
        result = _run(
            metal=_block([("甲", "50000")], "name", "50000"),
            powder=_block([("供", "1000")], "name", "1000"),
            dynamic=_block([("运费", "500")], "category", "500"),
            salary=_block([("张三", "10000")], "employee", "10000", value_key="wage"),
            rent={"unit_price": "1000", "days": 31},
        )
        assert result.summary["net_profit"] == 7500.0
        assert _net_line(result.summary["text"]).endswith(
            "50,000.00 - 1,000.00 - 500.00 - 10,000.00 - 31,000.00 = 7,500.00"
        )

    def test_rows_keep_input_order_not_sorted_by_amount(self) -> None:
        text = _run(
            metal=_block([("小客户", "10"), ("大客户", "99999")], "name", "100009")
        ).summary["text"]
        assert text.index("| 小客户 |") < text.index("| 大客户 |")


# --------------------------------------------------------------- 空明细的项


class TestEmptyBlock:
    def test_empty_rows_still_render_table_with_no_record_row(self) -> None:
        result = _run(
            powder=_block([], "name", "0"),
            dynamic=_block([], "category", "0"),
            salary=_block([], "employee", "0", value_key="wage"),
        )
        text = result.summary["text"]
        assert result.ok is True
        assert len(_headings(text)) == 5  # 一张不少
        assert text.count("| （当期无记录） | 0.00 |") == 3
        assert "| **合计** | **0.00** |" in text

    def test_all_blocks_empty_and_zero_rent_still_five_tables(self) -> None:
        result = _run(
            metal=_block([], "name", "0"),
            powder=_block([], "name", "0"),
            dynamic=_block([], "category", "0"),
            salary=_block([], "employee", "0", value_key="wage"),
            rent={"unit_price": "0", "days": 31},
        )
        text = result.summary["text"]
        assert len(_headings(text)) == 5
        assert text.count("| （当期无记录） | 0.00 |") == 4
        assert result.summary["net_profit"] == 0.0


# ------------------------------------------------------- 完整性校验 fail closed


class TestCompletenessFailClosed:
    @pytest.mark.parametrize(
        ("override", "label"),
        [
            ({"metal": _block([("甲", "100")], "name", "200")}, "钣金订单金额"),
            ({"powder": _block([("甲", "100")], "name", "200")}, "塑粉订单金额"),
            ({"dynamic": _block([("运费", "100")], "category", "200")}, "动态账款"),
            (
                {"salary": _block([("张三", "100")], "employee", "200", value_key="wage")},
                "工资账款",
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
        result = _run(metal=_block([("甲", "100.00")], "name", "100.01"))
        assert result.ok is True

    def test_beyond_tolerance_fails(self) -> None:
        result = _run(metal=_block([("甲", "100.00")], "name", "100.02"))
        assert result.ok is False


# ------------------------------------------------------------------ 参数校验


class TestArgsValidation:
    def test_period_order_enforced(self) -> None:
        with pytest.raises(ValidationError, match="时段起止非法"):
            _make_args(period_start="2026-08-31", period_end="2026-08-01")


# ------------------------------------------------------------------ ToolSpec


class TestToolSpec:
    def test_registered_as_readonly_tool(self) -> None:
        spec = TOOL_REGISTRY[TOOL_NAME]
        assert spec.is_write is False
        assert spec.requires_manager is True
        assert spec.prepare is None
        assert spec.policy_family == ()
        assert "erp:query" in spec.required_scopes
        assert spec.duty_code == "QRY"

    def test_description_pins_verbatim_output_and_no_recompute(self) -> None:
        spec = TOOL_REGISTRY[TOOL_NAME]
        assert "原样输出" in spec.description
        assert "不要自己重算" in spec.description
        assert "totals.wage" in spec.description

    def test_description_tracks_powder_ratio_behaviour(self) -> None:
        """防漂移：工具 description 必须与「三笔算数」的新行为同步。

        [2026-09-14] 本工具由「两笔」扩到「三笔」（新增塑粉占比除法，text 末尾
        多出比值算式段）。该 description 在**两处对模型可见**：决策轮工具枚举表
        （``model_node._build_system_prompt`` 的「可用工具」段，2026-09-16 起为决策轮
        唯一一份工具表）与参数轮工具卡
        （``tool_args_node``，与 skill 第 8/9 步同处一条 prompt）。描述若仍说
        「只做两笔 / 只产出五张表 + 净利润」，会与 skill「比值由渲染工具产出、
        呈现形态全由工具保证」打架，诱导模型删掉比值段或自己算比值。

        [2026-09-14 P1] 旧版断言锚的是描述末尾「另出『塑粉金额占钣金订单金额比』
        算式段」那句——只与**呈现形态**有关、与**公式**无关：把 description 里的
        公式整体反转（如改成 ``③ 钣金占比 = 钣金合计 ÷ 塑粉合计``）仍照样全绿
        （假锁）。现改锚**第三笔算数的公式本身**（运行时整句，description 是跨行
        拼接故按运行时值写）加「不再说『两笔』」。仍不逐字钉死整段描述（避免措辞
        微调误报）——目的就是防「行为改了、描述没改」这类**模型可见契约漂移**复发。
        """
        spec = TOOL_REGISTRY[TOOL_NAME]
        assert "③ 塑粉占比 = 塑粉合计 ÷ 钣金合计" in spec.description
        assert "两笔" not in spec.description


# --------------------------- skill 正文「经营分析」步骤防漂移

SKILL_KEY = "SK.financial_report"


class TestSkillAnalysisStepDriftGuards:
    """防漂移：财报 skill 正文必须含「经营分析」步骤与两条关键禁令。

    [2026-09-14] 新增第 10 步「经营分析」——正文之后每期另附一段经营分析，
    六项指标、每数字写成算式行，且**不许给业务建议**、**禁止谈**
    现金流/毛利之类本口径算不出的东西。该步骤是纯文本契约（不改工具行为、
    不新增取数），因此放在**渲染工具的测试文件**里做轻量守护：``test_skills.py``
    的职责是 loader/注入机制，不该承载具体 skill 的业务口径。

    与 ``TestToolSpec::test_description_tracks_powder_ratio_behaviour`` 同纪律
    ——**只钉语义关键词**，不逐字钉死整段正文（否则任何措辞微调都误报）。
    """

    def test_skill_text_carries_analysis_step(self) -> None:
        pkg = SKILL_REGISTRY[SKILL_KEY]
        text = pkg.text
        assert "经营分析" in text
        # 结构断言（非纯字符串存在）：第 9 步（下标 8）必须是「出报告」那一步，
        # 且前向指针「见第 10 步」必须落在**这一步之内**；第 10 步（下标 9）必须
        # 是「经营分析」。若把第 9/10 步对调、或在前面插一步而不改指针，位置
        # 断言即红——纯 `"见第 10 步" in text` 抓不到这类错位（指针仍在正文里）。
        assert pkg.steps[8].startswith("出报告"), pkg.steps[8]
        assert "见第 10 步" in pkg.steps[8]
        assert pkg.steps[9].startswith("经营分析"), pkg.steps[9]
        # 六项指标各自出现（名称级关键词）
        for marker in (
            "成本结构占比",
            "净利润率",
            "客户集中度",
            "供货商集中度",
            "能耗占产值比",
            "人工成本占比",
        ):
            assert marker in text, marker

    def test_skill_text_forbids_advice_and_offcatalogue_metrics(self) -> None:
        text = SKILL_REGISTRY[SKILL_KEY].text
        assert "不许给业务建议" in text  # 禁令一：只陈述、不评价/不建议
        assert "现金流" in text  # 禁令二：本口径算不出的项目，明列禁谈
        assert "毛利" in text

    def test_skill_text_makes_analysis_mandatory_every_period(self) -> None:
        """防漂移：经营分析**每期都出**，不得反问用户要不要分析。

        [2026-09-14 补] 本条锁用户最强调的一条要求——分析是**默认附上**的，
        不依赖用户开口。变异实测：删掉第 10 步里「**每期都出**，不必等用户
        开口、不得反问用户要不要分析。」整句，其余断言全绿（100 passed，
        一条未抓），故单列一条锁住。只钉两个语义关键词（``每期都出`` /
        ``不得反问``），不逐字钉死整句。**断言落在第 10 步这一行内**（结构
        位置），故第 9/10 步对调这类错位也会红。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert step.startswith("经营分析"), step
        assert "每期都出" in step  # 默认每期附分析，不等用户开口
        assert "不得反问" in step  # 不得反问用户要不要分析

    def test_skill_text_pins_analysis_position_and_formula_form(self) -> None:
        """防漂移：经营分析的位置（正文之后）与呈现形态（写成算式行）。

        [2026-09-14 补] 第 10 步两个要件此前未被锁：①分析必须落在**正文之后**
        （错把分析混进正文会违背「正文原样输出」）；②每个数字**必须写成算式行**
        以便核对（删掉后模型会只给裸数字、失去可核对性）。断言落在第 10 步
        这一行内（结构位置）。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "正文之后" in step  # 位置：正文之后另起一段，不混进正文
        assert "必须写成算式行" in step  # 形态：每个数字写成算式行以便核对

    def test_skill_text_tells_model_to_say_uncomputable(self) -> None:
        """防漂移：某项数字不具备时必须直说「当期无法计算」，不许估算/编造。

        [2026-09-14 补] 删掉该句会让模型在缺数时臆造数字（财报口径事故的典型
        复发形态），故单列一条锁住（关键词 ``当期无法计算``）。断言落在第 10
        步这一行内（结构位置）。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "当期无法计算" in step  # 缺数时直说，不许估算、不许编造


# --------------------------------------------------- 大月份载荷（正文 > 4096）


def _name20(prefix: str, i: int) -> str:
    """恰好 20 字的名称（真实客户名量级），用于逼近历史预算阈值。"""
    return (f"{prefix}{i:02d}" + "公司名称占位占位占位占位占位占位")[:20]


def _big_args() -> RenderFinancialReportArgs:
    """一个会踩 TOOL_RESULT_HISTORY_CHARS(4096) 的大月份参数。

    107 明细行（70 客户 + 15 供货商 + 9 类目 + 13 员工）、名称 20 字：
    ``json.dumps(summary)`` 约 4400 字符 > 4096（实测），足以触发
    summarize_tool_result 的 items 缺省分支。
    """
    return RenderFinancialReportArgs(
        period_start="2026-07-01",
        period_end="2026-07-31",
        metal=_block([(_name20("客户", i), "1000.00") for i in range(70)], "name", "70000.00"),
        powder=_block([(_name20("供货商", i), "2000.00") for i in range(15)], "name", "30000.00"),
        dynamic=_block(
            [
                (c, "300.00")
                for c in ("运费", "杂物", "天然气", "电费", "水费", "手续费", "税款", "人情", "售后服务")
            ],
            "category",
            "2700.00",
        ),
        salary=_block(
            [(_name20("员工", i), "8000.00") for i in range(13)],
            "employee",
            "104000.00",
            value_key="wage",
        ),
        rent={"unit_price": "1000", "days": 31},
    )


# ------------------------------------------- 改动 1：「正文即产物」豁免字符预算


class TestVerbatimTextExemption:
    """render_financial_report 的固定 Markdown 正文不得被 4096 腰斩。

    缺陷（改动前实测）：summary 无 ``items``，走 summarize_tool_result 的
    items 缺省分支，超 4096 即 ``return content[:max_chars]``（原始 JSON 从
    中间截断）→ 模型拿到**非法 JSON + 断行 Markdown**，且无 ``ok=False`` /
    无日志 / 无 ``EVENT_AGENT_ERROR``。修法：按工具名白名单豁免字符预算。
    """

    def test_exempted_tool_preserves_full_text_and_valid_json(self) -> None:
        result = run_render_financial_report(ToolContext(), _big_args())
        assert result.ok is True
        payload = json.dumps(result.summary, ensure_ascii=False)
        # 先证明本载荷确实会踩阈值——否则本用例是假绿、起不到证明作用
        assert len(payload) > TOOL_RESULT_HISTORY_CHARS
        out = summarize_tool_result(payload, tool_name=TOOL_NAME)
        parsed = json.loads(out)  # ① 仍是合法 JSON
        assert parsed["text"] == result.summary["text"]  # ② 正文逐字完整
        assert len(out) > TOOL_RESULT_HISTORY_CHARS  # ③ 确实未做字符预算

    @pytest.mark.parametrize("other_name", [None, "memory_lookup"])
    def test_exemption_is_tool_name_scoped_not_global(self, other_name: str | None) -> None:
        """护栏：豁免只认工具名——其他工具（含 memory_lookup 的 "text"）仍受预算约束。

        防止日后有人把豁免放宽成「按 key（text）」或「全局」，那会把
        memory_lookup 的分页正文一起放过。
        """
        result = run_render_financial_report(ToolContext(), _big_args())
        payload = json.dumps(result.summary, ensure_ascii=False)
        assert len(payload) > TOOL_RESULT_HISTORY_CHARS
        out = summarize_tool_result(payload, tool_name=other_name)
        assert len(out) == TOOL_RESULT_HISTORY_CHARS  # 仍被腰斩到 4096
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_tools_node_main_path_wires_tool_name_end_to_end(self) -> None:
        """端到端：真实 ``tools_node`` 主路径派发 render_financial_report（大载荷）。

        锁定**只读主路径调用点**（``tools_node`` → ``summarize_tool_result``）的
        ``tool_name`` 实参接线：删掉它就回落到 4096 腰斩（非法 JSON），本用例
        必失败（对比：兜底路径已有 ``test_fallback_preserves_verbatim_tool_result``
        端到端锁住 ``fallback_truncate_result`` 的接线）。**不 monkeypatch 摘要器**，
        走真实回路。
        """
        result = run_render_financial_report(ToolContext(), _big_args())
        assert result.ok is True
        payload = json.dumps({"ok": result.ok, **result.summary}, ensure_ascii=False)
        assert len(payload) > TOOL_RESULT_HISTORY_CHARS  # 先证明确会踩阈值，否则假绿

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


# ------------------------- 改动 3：摘要器异常兜底也遵守「正文即产物」白名单


class TestSummarizeFailureFallback:
    """[2026-09-14 补] ``summarize_tool_result`` 抛异常时 tools_node 的
    except 兜底分支，也必须遵守 ``VERBATIM_TEXT_TOOL_NAMES``。

    缺陷（改动前实测）：该兜底无条件 ``json.dumps(summary)[:4096]``，会把
    render_financial_report 的固定 Markdown 正文从中间腰斩成非法 JSON——
    与 ``summarize_tool_result`` items 缺省分支修的是同一类静默损坏，只是
    触发路径更罕见（摘要器抛异常）。修法：兜底抽成极小纯函数
    ``fallback_truncate_result`` 并同样按工具名白名单豁免。
    """

    @staticmethod
    def _raising_summarize(*args, **kwargs):
        raise RuntimeError("boom: 模拟摘要器异常（兜底分支）")

    def test_fallback_preserves_verbatim_tool_result(self, monkeypatch) -> None:
        """端到端：摘要器抛异常 → 走 tools_node 兜底 → 正文不截断且合法 JSON。"""
        import importlib

        # 注意：不能写 ``import ...nodes.tools_node as mod``——nodes 包把函数
        # tools_node 绑定到了同名属性上，按属性取会拿到函数而非模块。
        tools_node_mod = importlib.import_module(
            "administrative_agent.agent.nodes.tools_node"
        )

        result = run_render_financial_report(ToolContext(), _big_args())
        assert result.ok is True
        payload = json.dumps({"ok": result.ok, **result.summary}, ensure_ascii=False)
        assert len(payload) > TOOL_RESULT_HISTORY_CHARS  # 先证明确会踩阈值

        monkeypatch.setattr(
            tools_node_mod, "summarize_tool_result", self._raising_summarize
        )
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
        parsed = json.loads(content)  # ① 仍是合法 JSON（未被腰斩）
        assert parsed["text"] == result.summary["text"]  # ② 正文逐字完整
        assert len(content) > TOOL_RESULT_HISTORY_CHARS  # ③ 确实未截断

    @pytest.mark.parametrize("other_name", [None, "memory_lookup"])
    def test_fallback_truncation_guard_for_other_tools(self, other_name: str | None) -> None:
        """护栏：兜底白名单也只认工具名——其余工具仍截到 4096（防豁免放宽）。"""
        payload = json.dumps(
            {"text": "x" * (TOOL_RESULT_HISTORY_CHARS + 100)}, ensure_ascii=False
        )
        out = fallback_truncate_result(payload, other_name)
        assert len(out) == TOOL_RESULT_HISTORY_CHARS
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_fallback_exempts_only_verbatim_tool(self) -> None:
        payload = json.dumps(
            {"text": "y" * (TOOL_RESULT_HISTORY_CHARS + 100)}, ensure_ascii=False
        )
        assert fallback_truncate_result(payload, TOOL_NAME) == payload  # 逐字不截


# ------------------------------------ 改动 2a：rent.days 与时段交叉校验


class TestRentDaysConsistency:
    """房租 days 必须等于 ``(period_end - period_start).days + 1``。

    缺陷（改动前实测）：默认时段 2026-08-01..08-31（31 天）下，days =
    30/0/-5/1000000000 **全部 ok=True 静默接受**（房租无 rows 可自查、又是
    五项里单笔最大），净利被静默抬高或压低。
    """

    @pytest.mark.parametrize("bad_days", [30, 32, 0, -5, 1000000000])
    def test_days_mismatch_rejected_with_expected_and_actual(self, bad_days: int) -> None:
        # 参数校验层拒绝（tools_node 随后把它转成 ok=False 的错误 ToolMessage）
        with pytest.raises(ValidationError) as exc:
            _make_args(rent={"unit_price": "1000", "days": bad_days})
        msg = str(exc.value)
        assert "rent.days" in msg
        assert "31" in msg  # 期望天数（2026-08-01..08-31 含首尾）
        assert str(bad_days) in msg  # 实传天数

    def test_days_matching_period_passes(self) -> None:
        assert _run(rent={"unit_price": "1000", "days": 31}).ok is True

    def test_days_matching_non_31_day_period_passes(self) -> None:
        # 2026-02-01..2026-02-28 = 28 天：校验按实际时段算，不是硬编码 31
        result = _run(
            period_start="2026-02-01",
            period_end="2026-02-28",
            rent={"unit_price": "1000", "days": 28},
        )
        assert result.ok is True

    def test_tools_node_returns_ok_false_for_days_mismatch(self) -> None:
        """消费点边界：参数校验失败 → ``ok=False`` 错误 ToolMessage（不落库、不静默）。

        与 test_period_order_enforced 同源：校验在 args_schema 构造时抛
        ValidationError，tools_node 把它包成 ok=False（current_step=
        tool_invalid_args，模型下轮自纠）。这里直接走 tools_node 证明确实是
        ok=False 而非静默接受。
        """
        runtime = RuntimeBag(
            tools=TOOL_REGISTRY,
            context=AgentContext(
                user_id="manager01",
                user_role="manager",
                scope=frozenset({"erp:query"}),
                thread_id="thread-1",
            ),
        )
        args_raw = _make_args().model_dump(mode="json")
        args_raw["rent"]["days"] = 30  # 人为制造与时段不符（模型构造会被拦，故先 dump 再改）
        state = {
            "messages": [],
            "pending_action": {"tool_name": TOOL_NAME, "tool_arguments": args_raw},
        }
        out = tools_node(state, runtime)
        assert out["current_step"] == "tool_invalid_args"
        payload = json.loads(out["messages"][0].content)
        assert payload["ok"] is False
        assert "rent.days" in payload["error"]


# ------------------------------------ 改动 2b：名称单元格转义


class TestCellEscaping:
    """名称类单元格转义：``|`` / 换行不得漂移固定模板。"""

    def test_pipe_in_name_does_not_add_columns(self) -> None:
        text = _run(metal=_block([("甲|伪造", "1000")], "name", "1000")).summary["text"]
        line = next(ln for ln in text.splitlines() if ln.startswith("| 甲"))
        # 名称里的 ``|`` 被转义为 ``\|``；未转义分隔符仍恰好 3 个（2 列）
        assert line == "| 甲\\|伪造 | 1,000.00 |"
        assert len(re.findall(r"(?<!\\)\|", line)) == 3

    def test_newline_in_name_does_not_add_rows(self) -> None:
        baseline = _run(metal=_block([("甲", "1000")], "name", "1000")).summary["text"]
        injected = _run(
            metal=_block([("甲\n| 伪造 | 9,999.00 |", "1000")], "name", "1000")
        ).summary["text"]
        # 换行被替换成空格 → 行数不变；伪行 "| 伪造 |" 没有出现
        assert len(injected.splitlines()) == len(baseline.splitlines())
        assert "| 伪造 |" not in injected

    def test_normal_name_output_unchanged(self) -> None:
        text = _run(metal=_block([("甲公司", "1000")], "name", "1000")).summary["text"]
        assert "| 甲公司 | 1,000.00 |" in text


# ------------------------------------ 改动 4：塑粉金额占钣金订单金额比（除法）


class TestPowderRatio:
    """报告末尾新增「塑粉金额占钣金订单金额比」算式行 = 塑粉合计 ÷ 钣金合计。

    该除法**必须由工具算**（历史实测模型做除法会错，见 memory
    ``erp-calc-capability-gaps``），呈百分比两位小数、``ROUND_HALF_UP``、
    不做上限截断；除数为零时照常出报告、比值行记「无法计算」。
    """

    def test_ratio_line_verbatim(self) -> None:
        """比值行逐字钉住（含千分位与 ``%``）。"""
        text = _run().summary["text"]
        assert _ratio_line(text) == "500.00 ÷ 3,345.67 = 14.94%"

    @pytest.mark.parametrize(
        ("powder", "metal", "expected_pct"),
        [
            ("1", "3", "33.33"),   # 33.333…% → 33.33%（截去多余小数）
            ("1", "32", "3.13"),   # 3.125% → HALF_UP 进位 3.13（HALF_EVEN 会得 3.12）
        ],
    )
    def test_ratio_rounds_half_up(
        self, powder: str, metal: str, expected_pct: str
    ) -> None:
        result = _run(
            metal=_block([("甲", metal)], "name", metal),
            powder=_block([("供", powder)], "name", powder),
        )
        assert _ratio_line(result.summary["text"]) == (
            f"{Decimal(powder):,.2f} ÷ {Decimal(metal):,.2f} = {expected_pct}%"
        )

    def test_ratio_above_hundred_percent_not_capped(self) -> None:
        """塑粉大于钣金（真信号）照实显示，不截断到 100%。"""
        text = _run(
            metal=_block([("甲", "100")], "name", "100"),
            powder=_block([("供", "118.30")], "name", "118.30"),
        ).summary["text"]
        assert _ratio_line(text) == "118.30 ÷ 100.00 = 118.30%"

    def test_zero_metal_total_still_reports_with_uncomputable_line(self) -> None:
        """除数为零：照常出五张表 + 净利润，比值行记「无法计算」、不含等号。"""
        result = _run(metal=_block([], "name", "0"))
        text = result.summary["text"]
        assert result.ok is True  # ① 不抛异常、不 ok=False
        ratio = _ratio_line(text)
        assert "无法计算" in ratio  # ②
        assert ratio == "500.00 ÷ 0.00 → 无法计算（当期无钣金订单）"  # 分子照实显示；本行无等号（用 → 而非 =）
        assert len(_headings(text)) == 5  # ③ 五张表仍是 5 条
        assert _net_line(text).endswith("= -42,800.50")  # ④ 净利润行仍在（为负）
        assert result.summary["powder_ratio"] is None  # ⑤

    def test_zero_metal_total_passes_completeness_check(self) -> None:
        """分母为零路径不被 ``_check_complete`` 误判：rows 空、sum(rows)==total==0。"""
        result = _run(metal=_block([], "name", "0"))
        assert result.ok is True
        assert "error" not in result.summary

    def test_net_line_and_ratio_line_do_not_pollute_each_other(self) -> None:
        """净利润行与比值行互不污染：各自不含 ``|``（都不进任何表格）。

        断言必须**可失败**：不能用 ``_net_line`` / ``_ratio_line`` 来证「不含
        ``|``」——两个 helper 的筛选式本身都含 ``"|" not in line``，那样断言恒真、
        信息量为零。这里改为按各自小节标题字面量直接取**下一行**（不过滤），
        故若实现把某行拼成表格行（以 ``|`` 开头）本用例必红。
        """
        text = _run().summary["text"]
        lines = text.splitlines()
        net = lines[lines.index("净利润（不用表格）") + 1]
        ratio = lines[lines.index("塑粉金额占钣金订单金额比") + 1]
        assert "|" not in net
        assert net.count(" = ") == 1
        assert "|" not in ratio

    def test_powder_ratio_numeric_in_summary(self) -> None:
        assert _run().summary["powder_ratio"] == 14.94

    def test_extreme_ratio_reaches_high_precision_fallback(self, monkeypatch) -> None:
        """[2026-09-14 reviewer P2b] 真正进入 ``except InvalidOperation`` 兜底分支。

        此前所有用例都是普通量级，**没有任何一条**走 ``except InvalidOperation``
        （该分支零覆盖）。本用例用 monkeypatch 给模块级 ``localcontext`` 计数——
        该名只在兜底分支内被调用，故「计数非空」即**直接证明**分支被进入（不是
        靠结果反推）。先证明同量级输入在默认 28 位上下文下**必抛**
        ``InvalidOperation``（否则根本进不到兜底；实测阈值在 1e23/1e24 之间），
        再断言函数算出数值、未抛。
        """
        import administrative_agent.agent.tools.financial_report as fr

        calls: list[str] = []
        real_localcontext = fr.localcontext

        def _spy(*args, **kwargs):
            calls.append("entered")
            return real_localcontext(*args, **kwargs)

        monkeypatch.setattr(fr, "localcontext", _spy)

        with decimal.localcontext() as ctx:
            ctx.prec = 28
            with pytest.raises(decimal.InvalidOperation):
                (Decimal("1E+30") / Decimal(1) * 100).quantize(
                    Decimal("0.01"), rounding=decimal.ROUND_HALF_UP
                )

        pct = fr._powder_ratio_pct(Decimal("1E+30"), Decimal(1))
        assert calls, "未进入高精度兜底分支（localcontext 未被调用）"
        assert pct == Decimal("1E+32")  # 算出结果、未抛

    def test_non_finite_ratio_recorded_as_none_not_infinity(self) -> None:
        """[2026-09-14 reviewer P8b] 兜底放宽后 pct 可极大：``float(Decimal)``
        会得 ``inf``，``json.dumps`` 默认写出**非法 JSON token** ``Infinity``。

        用极小分母（``metal=1E-400``）使比值极大（``1E+402``）而 net_profit 仍
        在正常量级（避开 net_profit 量化）。改后非有限即记 ``None``——与除数为
        零同语义（给不出可用数值）。
        """
        # 先证明该比值确实会溢出成 inf（否则本用例是假绿、证明不了守卫）
        assert math.isinf(float(Decimal("1E+402")))
        result = _run(
            metal=_block([("甲", "1E-400")], "name", "1E-400"),
            powder=_block([("供", "1")], "name", "1"),
        )
        assert result.ok is True
        assert result.summary["powder_ratio"] is None
        encoded = json.dumps(result.summary)  # 默认 allow_nan=True 会写出 Infinity
        assert "Infinity" not in encoded
        assert "NaN" not in encoded

    def test_non_finite_input_raises_fail_fast(self) -> None:
        """P5：非有限输入（Infinity/NaN）必须 fail-fast 抛 ``ValueError``。

        [2026-09-14 P5] ``_powder_ratio_pct`` 的 ``while True`` 只按「抛
        ``InvalidOperation`` 就翻倍精度」推进、无次数上限。非有限输入本不该走到
        那条路径：``Infinity`` 在**任意精度**下恒抛 ``InvalidOperation``，经兜底
        会一路翻倍到 > ``decimal.MAX_PREC``，最终由 ``ctx.prec = prec`` 抛
        ``ValueError: valid range for prec is [1, MAX_PREC]``（**很快终止，不是死
        循环**；但错误文案与真实原因「输入非有限」无关、误导排查）；``NaN`` 则
        **根本不抛**、静默返回 ``Decimal('NaN')``。故加入口守卫，fail-fast 成切题
        的 ``ValueError``，把「非有限值到不了这里」的隐式依赖变显式。当前本不可达
        （pydantic 2.x 拒绝非有限 ``Decimal``）；本用例**直接调私有函数**（不经
        pydantic 那道闸）以覆盖该守卫。
        """
        import administrative_agent.agent.tools.financial_report as fr

        with pytest.raises(ValueError, match="非有限值"):
            fr._powder_ratio_pct(Decimal("Infinity"), Decimal(1))
        with pytest.raises(ValueError, match="非有限值"):
            fr._powder_ratio_pct(Decimal("NaN"), Decimal(1))
        with pytest.raises(ValueError, match="非有限值"):
            fr._powder_ratio_pct(Decimal(1), Decimal("-Infinity"))


# ---------------------- skill 正文：口径钉死与机械一致性（P1/P3/P5/P6/P7）

class TestSkillStepPins:
    """[2026-09-14 reviewer] 把第 8/10 步里几处「口径/禁令」机械钉死。

    这些是纯文本契约（不改工具行为），放在渲染工具测试文件里做轻量守护。
    """

    def test_energy_metric_names_match_step4_catalogue(self) -> None:
        """P7b：第 10 步 ⑤ 的能耗三个类目名必须 ∈ 第 4 步九类目权威清单。

        防「ERP 改名后两处不一致」——第 4 步是权威清单（其自身声明「类目名以
        ERP 实际名称为准」），第 10 步若另写一份就会漂移。任一处改名而另一处
        未同步，本用例即红。
        """
        pkg = SKILL_REGISTRY[SKILL_KEY]
        step4 = pkg.steps[3]
        assert step4.startswith("动态账款："), step4
        m = re.search(r"「([^」]*、[^」]*)」", step4)  # 取含顿号的那组（九类目清单）
        assert m, "第 4 步未找到九类目封闭清单「…」"
        catalogue = m.group(1).split("、")
        step10 = pkg.steps[9]
        names = ("电费", "天然气", "水费")
        for name in names:
            assert name in catalogue, f"{name} 不在第 4 步权威类目清单里"
        # [2026-09-14 P4] 旧断言用裸子串 ``name in step10``——是子串匹配：把第 10
        # 步 ⑤ 的「水费」改成「自来水费」仍命中（「水费」是「自来水费」的子串），
        # 与本用例「任一处改名而另一处未同步即红」的声称不符。改为**双重绑定**：
        # 三个名字既要 ∈ 第 4 步权威清单，又要由它们**原样**拼成第 10 步 ⑤ 的权威
        # 整段（改名即整段失配）。整段是词边界级的——不会有子串误命中。
        energy_segment = "动态账款中" + "、".join(names) + "三项之和"
        assert energy_segment in step10, (
            "第 10 步 ⑤ 未原样引用第 4 步的三项能耗类目名"
            f"（期望整段：{energy_segment!r}；与第 4 步不一致）"
        )

    def test_headcount_denominator_pinned_to_salary_calc_count(self) -> None:
        """P1：「员工数」分母必须钉死在 erp_salary_calc 的 count（与分子同源）。

        [2026-09-14 收紧] 原断言 ``"count" in step`` 是**假锁**：``count`` 在第 10
        步出现两次——①定义处「…erp_salary_calc 同一次返回里的 count」、②紧随其后
        的括号说明「…也不是 count 加 missing」。把①的定义词改成「数量」后②仍命中
        ``count``，测试照绿（实测 110 passed），模型于是重新失去「员工数」口径。
        故改用 ``同一次返回``（**全步唯一**，只在定义处出现）作锚点：要求它与
        ``count`` 在**同一括号段内相邻出现**。``[^（]*`` 不跨全角括号，括号说明里的
        ``count`` 不会被误当成定义处——改①必红，改②（括号措辞）不受影响。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "人均工资" in step
        assert "员工数" in step
        assert "erp_salary_calc" in step  # 全步唯一，无同名假锁
        # 定义处锚点：``同一次返回`` 之后、同一括号段内紧跟 ``count``。
        assert re.search(r"同一次返回[^（]*count", step), step
        assert "÷ 13 人" in step  # 要求算式行写明分母来源

    def test_top3_customers_listed_item_by_item(self) -> None:
        """P3：③ 前三大客户必须逐个列名 + 逐项列金额（模型自己排序求和时的可核对性）。"""
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "前三大客户" in step
        assert "逐项列出" in step

    def test_step8_forbids_self_output_of_ratio_line(self) -> None:
        """P2：第 8 步是「无需动作」的编号步骤，须把禁令**限定到正文**。

        [2026-09-14 P2] 第 8 步原为无条件「不得自行计算」，与第 10 步①要求
        「塑粉占钣金比重写成算式行」（同一公式）自相矛盾。现措辞限定为「不得
        自行计算后写进正文」，并显式允许第 10 步分析**直接引用**第 9 步工具给出
        的数字。锚点随之改为这两处新语义词——不再锚旧的无条件短语「不得自行
        计算」（它已是新短语的子串，锚不住矛盾是否复发）。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[7]  # 第 8 步（下标 7）
        assert step.startswith("塑粉占比"), step
        assert "不得自行计算后写进正文" in step  # 禁令限定到正文（非无条件）
        assert "不得自行把比值行写进正文" in step
        assert "直接引用第 9 步工具给出的数字" in step  # 分析段可引用、不必另算

    def test_step10_has_zero_denominator_fallback(self) -> None:
        """P3：第 10 步六项指标分母都可能为零，须有明确降级出口。

        [2026-09-14 P3] 第 10 步原唯一出口是「某项数字不具备时」——而「数字
        不具备」≠「分母为零」，模型很可能直接除零或临时另编分母。本条锁住新增
        的分母为零出口：钣金为 0 → ①②③⑤⑥、塑粉为 0 → ④、count 为 0 → 人均
        工资，均写「当期无法计算」（与第 8/9 步渲染工具同口径）。

        [2026-09-14 收紧] 旧断言（``"count 为 0" in step`` 等）全是**裸子串**、
        是假锁：把「count 为 0 时，人均工资写『当期无法计算』」整句改成「count
        为 0 时照常算」——``"count 为 0"`` 仍在、``"当期无法计算"`` 又在本步
        别处（钣金／塑粉两条分支）命中，实测 112 passed 全绿，三条分支各自的
        **后果**均未被钉住。现改用**语义绑定**：每条分支的「触发锚点」与其
        「后果（无法计算）」必须落在**同一小句**内——``[^。；（]*`` 不跨句号、
        不跨分号、也不跨左括号。这两道边界都是实测逼出来的，缺一即假锁：
        只挡句号 → 塑粉分支的后果会顺延到同句的 count 分支（实测仍绿）；只挡
        句号/分号 → 钣金分支去掉后果后，正则会被紧随其后「（与第 8/9 步渲染
        工具的「无法计算（当期无钣金订单）」同口径）」这句**括号说明**里的
        「无法计算」救活（实测仍绿）。锚点与后果之间的连接词仍可自由改动。
        三个锚点（``钣金订单金额为 0 时`` / ``塑粉订单金额为 0 时`` /
        ``count 为 0 时``）在整步中**各仅出现一次**，不跨分支误命中（与
        ``test_headcount_denominator_pinned_to_salary_calc_count`` 同一手法）。
        """
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "分母为零的降级出口" in step
        # 分支一：钣金为 0 → ①②③⑤⑥ 一并写「当期无法计算」
        assert re.search(r"钣金订单金额为 0 时[^。；（]*①②③⑤⑥[^。；（]*无法计算", step), step
        # 分支二：塑粉为 0 → ④ 写「当期无法计算」
        assert re.search(r"塑粉订单金额为 0 时[^。；（]*④[^。；（]*无法计算", step), step
        # 分支三：count 为 0 → 人均工资写「当期无法计算」
        assert re.search(r"count 为 0 时[^。；（]*人均工资[^。；（]*无法计算", step), step
        # 两条总禁令（本步末句显式给出，防「除零／另编分母」复发）
        assert "不许除零" in step
        assert "不许临时另编分母" in step

    def test_step10_brevity_constraint(self) -> None:
        """P6：第 10 步须有压住分析长度的简短约束（大月份 token 余量）。"""
        step = SKILL_REGISTRY[SKILL_KEY].steps[9]
        assert "务必简短" in step


# ---------------------- 动态账款封闭白名单（九类）防漂移
# 真源两处、互不引用：SK.financial_report.md 第 4 步（正文）+ tools/financial_report.py
# 的 _DynamicBlock.rows 描述（参数轮模型可见卡）。两处需同改，故各钉一条。


#: 动态账款封闭白名单（第 4 步权威清单，九类；顺序与正文一致）
_DYNAMIC_WHITELIST: tuple[str, ...] = (
    "运费", "杂物", "天然气", "电费", "水费", "手续费", "税款", "人情", "售后服务",
)

#: 中文数词 → 阿拉伯数字（钉「只认这N个」的 N 与清单实际长度一致）
_CN_NUMBERS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _dynamic_catalogue(step4: str) -> list[str]:
    """从第 4 步抽取权威封闭清单（第一个含顿号的「…」组）。

    与 ``test_energy_metric_names_match_step4_catalogue`` 同一抽取手法：第 4 步
    唯一的「…」顿号组就是白名单本身（反例「「工资」」不含顿号，不会被误抽）。
    """
    m = re.search(r"「([^」]*、[^」]*)」", step4)
    assert m, "第 4 步未找到含顿号的权威类目清单「…」"
    return m.group(1).split("、")


class TestDynamicWhitelistDriftGuards:
    """防漂移：动态账款封闭白名单必须恰为九类（含「手续费」「税款」、无「尾差调整」）。

    [2026-09-15 ERP 类目变更] ①「尾差调整」由 ERP 迁移 0006 改名为「手续费」，
    历史记录一并改名、库中不再有「尾差调整」；②新增支出类目「税款」。二者均
    计入动态账款 → 白名单**七类扩到九类**。本类锁三件事：
    - 白名单恰为九类且**逐位相等**（删/增/换任一类即红——含把「税款」删掉的突变）；
    - 旧名「尾差调整」在**整篇 skill 正文**中已清除（防「改了清单没改完」）；
    - 「封闭清单」语义未被降级（锚点与后果同处一小句，防本仓复发过的假锁）。

    白名单两处真源互不引用，故各钉一条：正文由 ``_dynamic_catalogue`` 结构抽取，
    参数轮模型可见的入参卡描述单独断言（args_schema 的 JSON schema）。
    """

    def test_step4_whitelist_is_exactly_the_nine_categories(self) -> None:
        step4 = SKILL_REGISTRY[SKILL_KEY].steps[3]
        assert step4.startswith("动态账款："), step4
        assert _dynamic_catalogue(step4) == list(_DYNAMIC_WHITELIST)

    def test_whitelist_has_new_categories(self) -> None:
        catalogue = _dynamic_catalogue(SKILL_REGISTRY[SKILL_KEY].steps[3])
        assert "手续费" in catalogue  # 由「尾差调整」改名而来
        assert "税款" in catalogue  # 本次新增

    def test_old_renamed_category_purged_from_whole_skill_text(self) -> None:
        """旧名「尾差调整」必须从整篇正文消失——它已不再是真实类目，留着当反例即失效。"""
        assert "尾差调整" not in SKILL_REGISTRY[SKILL_KEY].text

    def test_closed_whitelist_semantics_bound_within_one_clause(self) -> None:
        """「封闭清单」的语义约束不得退化成假锁。

        按 ``[。；\\n（]`` 切小句，要求：
        - 「封闭清单」锚点与计数后果「只认这N个」**同处一小句**，且 N == 清单长度
          （数字漂移即红——如清单已扩到九类、正文仍写「只认这七个」）；
        - 「不在清单内」锚点与「不计入」「坚决不取」**同处一小句**（弱化成「可酌情
          纳入」即红）。

        ``[^，。；\\s]`` 式边界与本仓既有守护同款：不跨句号/分号/左括号，避免邻近
        同字词把断言救活（本仓 prompt 守护「假锁」已复发 4 次）。
        """
        step4 = SKILL_REGISTRY[SKILL_KEY].steps[3]
        catalogue = _dynamic_catalogue(step4)
        clauses = re.split(r"[。；\n（]", step4)

        closed = [c for c in clauses if "封闭清单" in c]
        assert closed, "第 4 步未声明白名单为封闭清单"
        counts = [
            m.group(1)
            for c in closed
            for m in [re.search(r"只认这([^，。；\s]+)个", c)]
            if m
        ]
        assert counts, "「封闭清单」同一小句内未见计数后果「只认这N个」"
        for count in counts:
            assert count in _CN_NUMBERS, f"无法解析计数数词：{count!r}"
            assert _CN_NUMBERS[count] == len(catalogue), (
                f"「只认这{count}个」与白名单实际长度 {len(catalogue)} 不符"
            )

        outside = [c for c in clauses if "不在清单内" in c]
        assert any(
            "不计入" in c and "坚决不取" in c for c in outside
        ), "「不在清单内」同一小句内未见「不计入」「坚决不取」"

    def test_arg_schema_description_matches_nine_categories(self) -> None:
        """参数轮模型可见的入参卡描述必须与正文同口径（九类、无旧名）。

        该描述在 ``_DynamicBlock.rows`` 上，经 ``model_json_schema`` 进入参数轮
        工具卡——模型据此决定 dynamic.rows 填哪些类目，漏改即与正文打架。
        """
        schema_text = json.dumps(
            RenderFinancialReportArgs.model_json_schema(), ensure_ascii=False
        )
        assert "九个封闭类目" in schema_text
        assert "尾差调整" not in schema_text
        for name in _DYNAMIC_WHITELIST:
            assert name in schema_text, f"入参卡描述缺类目：{name}"
        assert "七个封闭类目" not in schema_text
