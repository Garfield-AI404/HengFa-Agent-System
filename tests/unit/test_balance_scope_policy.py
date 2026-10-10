"""[2026-09-15 应收应付余额口径] R4.balance.scope 守护测试。

背景：[2026-09-15] ERP 新增「平账」（抹零结清），当时 agent 无任何应收应付
工具，模型只能跨两次调用自己心算、很可能把「已收款累计」当成「欠款」报给
用户，本轮先只加**约束与引导**。[2026-09-16 口径开放] `ar_settlements` 已
接入只读可查白名单、新 skill `SK.receivables` 已登记——「当期应收」现在**有
正规给出路径**（经 SK.receivables 按客户逐户相减），未结余额仍以 ERP 财务页
为准；本文件据新口径同步守护。

本文件守护两条落点，**刻意避开裸子串断言**（本仓「prompt 守护测试假锁」
教训：``assert "余额" in text`` 会被邻近同字词救活）：

- 落点一：规则包正文（参数轮注入）——锚点、后果、动作须**同处一个小句**；
- 落点二：两个只读工具 ``description``（决策轮注入）——**最关键的守护**：
  决策轮只渲染 description，若约束只落在 policy_family（正文只到参数轮），
  就是「把锁装在答题时关着的门上」，约束退化为假锁。
"""

from __future__ import annotations

import re

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.model_node import _build_system_prompt
from administrative_agent.agent.prompts.policies import POLICY_REGISTRY, policy_for
from administrative_agent.agent.tools import TOOL_REGISTRY

POLICY_KEY = "R4.balance.scope"

#: 锚点词：指代「欠款/未结余额」这类问题的措辞
_ANCHORS = ("未结余额", "还欠多少", "欠多少", "还欠")
#: 后果词：禁止性措辞
_CONSEQUENCES = ("不得", "禁止")
#: 动作词：被禁止的自行推算动作
_ACTIONS = ("推算", "相加", "相减", "相乘", "加减")

#: 「账期 = 当期月往前推 2 个月」的同句判据：数字 2 与「月」绑定的写法。
#: 容忍两种正文措辞——R4 包写「当期月 − 2」（U+2212/ASCII 连字符/减字），
#: skill 第 1 步写「往前推 2 个月」；数字被改成别的值即失配（本判据的靶子）。
_MONTH_OFFSET_RE = re.compile(
    r"当期月\s*(?:减|[−\-–—])\s*2|往前\s*推?\s*2\s*个?\s*月"
)


def _clauses(text: str) -> list[str]:
    """按句末标点/换行切小句（与「锚点与后果同处一个小句」的判据同构）。"""
    return [c for c in re.split(r"[。；\n]", text) if c.strip()]


# ---------------------------------------------------------------------------
# 落点一：规则包正文（按小句断言，非裸子串）
# ---------------------------------------------------------------------------


def test_balance_scope_anchor_and_consequence_share_one_clause() -> None:
    """锚点（未结余额/欠多少）+ 后果（不得/禁止）+ 动作（推算/相加/相减）
    必须**同处一个小句**——防止把「不得推算」拆到另一句后约束仍被同字词
    救活的假锁。"""
    clauses = _clauses(policy_for(POLICY_KEY).text)
    hits = [
        c
        for c in clauses
        if any(a in c for a in _ANCHORS)
        and any(k in c for k in _CONSEQUENCES)
        and any(act in c for act in _ACTIONS)
    ]
    assert hits, (
        "R4.balance.scope 缺少「锚点+后果+动作」同处一小句的约束："
        f"clauses={clauses}"
    )


def test_balance_scope_has_redirect_clause() -> None:
    """必须给出可执行的替代路径：「资产管理」+「查看」同处一小句。"""
    clauses = _clauses(policy_for(POLICY_KEY).text)
    assert any("资产管理" in c and "查看" in c for c in clauses), clauses


def test_actual_payment_still_answerable() -> None:
    """反向用例：约束**不得把「实际收款/实际付款」一起禁掉**。

    用户口径是要看**实际收付款**（不是应当），第 3 条明文放行
    ar_payments/ap_payments；这条防的是「矫枉过正把有用能力也砍掉」。
    """
    clauses = _clauses(policy_for(POLICY_KEY).text)
    allowed = [
        c for c in clauses if "实际收" in c and ("可查" in c or "照常" in c)
    ]
    assert allowed, f"约束把实际收付款也禁掉了（缺少放行小句）：{clauses}"


def test_balance_scope_only_bans_arrears_figures_not_all_numbers() -> None:
    """[2026-09-16] 第 2 条不得写成「不要给数字」式一刀切。

    应收应付（ar_payments/ap_payments）已对模型开放：模型**能**给发生额与
    收付款数字，真正给不出的只有未结余额（缺 ``settled``/平账抹零这一项）。
    故禁令必须**只绑定欠款数额**，且明文要求能查的部分照常给数字。
    """
    text = policy_for(POLICY_KEY).text
    # 反向：无条件禁数字的旧措辞不得回潮（这句就是本次修复的靶子）
    assert "不要给数字" not in text, "第 2 条退回「一刀切不给数字」"
    clauses = _clauses(text)
    # 正向：必须存在「欠款 + 不得/禁止 + 数字/数额」同处一小句的约束
    assert any(
        "欠款" in c
        and any(k in c for k in _CONSEQUENCES)
        and ("数字" in c or "数额" in c or "金额" in c)
        for c in clauses
    ), f"缺「只禁欠款数额」的约束小句：{clauses}"
    # 正向：必须有放行小句——同一问题里查得到的部分照常给数字
    assert any(
        "数字" in c and ("照常" in c or "照给" in c) for c in clauses
    ), f"缺「能查的部分照常给数字」的放行小句：{clauses}"


def test_balance_scope_pins_two_month_offset_in_one_clause() -> None:
    """R4.balance.scope 正文：「到期月」与「当期月 − 2（个月）」的账期表述必须
    **同处一个小句**。

    本仓此前对 R4.balance.scope 只钉「未结余额/不得/资产管理/实际收/发生额」
    五个词元（test_prompts.py），「账期 = 当期月往前推 2 个月」这一**取数口径
    零断言**——「2」被改成别的数字、或「到期月」语义被删、或两者被拆到不同句，
    全套测试仍绿。本条补上内容守护。

    按本仓纪律：不逐字锚整句（正文会继续演进），只锚「语义词（到期月）+ 数字
    2」同句（``_MONTH_OFFSET_RE``）。
    """
    clauses = _clauses(policy_for(POLICY_KEY).text)
    assert any(
        "到期月" in c and _MONTH_OFFSET_RE.search(c) for c in clauses
    ), (
        "R4.balance.scope 缺「到期月」+「当期月 − 2 个月」同句的账期口径："
        f"clauses={clauses}"
    )


def test_balance_scope_pins_negative_customer_exclusion() -> None:
    """[2026-09-17 当期应收口径收口] R4.balance.scope 第 1 条的客户级汇总口径：
    负值户不计入当期应收合计（按 0 计），合计只把当期应收为正的客户相加。

    [2026-09-17 复审收口] 只断「不计入」与「负值」同处一个小句**不够**：第 1 条
    里「负值」会在同一小句内以「不含负值户」的形式复现，故把「负值户。不计入」
    拆到另一小句后，后半句仍同时含「不计入」与「负值（不含负值户）」，旧判据
    照样绿（复审实测复现——本仓「prompt 守护测试假锁」的老毛病）。
    改为把锚点（负值）与后果（不计入）绑进**同一段无「、。；」句读的跨度**内
    （正则窗口，容忍两种语序），拆句即失配。
    """
    clauses = _clauses(policy_for(POLICY_KEY).text)
    assert any(
        re.search(r"负值[^、。；\n]{0,20}不计入", c)
        or re.search(r"不计入[^、。；\n]{0,20}负值", c)
        for c in clauses
    ), (
        "R4.balance.scope 第 1 条缺「负值户不计入合计」同句（不含句读跨度）的"
        f"汇总口径：clauses={clauses}"
    )


# ---------------------------------------------------------------------------
# 落点二：决策轮可见的 description（本次最重要的守护）
# ---------------------------------------------------------------------------


def test_decision_round_tool_descriptions_carry_balance_scope() -> None:
    """两个只读工具的 description 必须含**已开放**的当期应收口径，且该口径
    确实渲染进决策轮 system prompt。

    决策轮 system prompt **只渲染** ``- {name}：{description}``（policy_family
    正文只到参数轮）。本用例红了 = 口径/约束退化成假锁（模型答题时看不到，
    照样拒答或自行推算）。故同时断言「描述里有新口径关键词」且「决策轮 prompt
    里确有」。

    判据为什么不会退化成假锁：新口径的**独有**标记是「当期应收」与
    ``SK.receivables`` 两个词——旧文案没有它们，若描述回退到旧口径
    （"口径未开放"）两词必然缺席、本用例即红。注意「资产管理」**不是**新口径
    的独有标记：旧句（"引导用户到 ERP 财务-资产管理页查看"）里就有它，故它只
    作**存在性**断言（未结余额的另一口径仍须保留该 redirect 指引），不参与
    "是否已开放"的判定。

    按本文件纪律（锚点与后果须**同处一个小句**），本用例对 description 也按
    小句切分，断言「当期应收」与 ``SK.receivables`` 同处一个小句——只做段落级
    合取会被散落三段的同字词救活（假锁）。另外本用例传给
    ``_build_system_prompt`` 的 schema 是字面量 ``"SCHEMA"``（不含 skill
    索引），故 prompt 里出现 ``SK.receivables`` 只可能来自工具 description
    本身——排除了"其实是 skill 索引表注进来的"这条假绿路径。
    """
    for name in ("erp_query", "erp_calc"):
        desc = TOOL_REGISTRY[name].description
        # 旧口径不回潮：口径已开放，描述不得再声称「未开放」
        assert "口径未开放" not in desc, name
        # 新口径独有标记必须**同处一个小句**（段落级合取会被散落同字词救活）
        assert any(
            "当期应收" in c and "SK.receivables" in c for c in _clauses(desc)
        ), f"{name} 描述缺「当期应收 … SK.receivables」同句的新口径"
        # 未结余额的另一口径仍须保留 redirect 指引（旧口径已有 → 仅存在性）
        assert "资产管理" in desc, name
        # 决策轮 prompt 逐字渲染 description 全文：同句共现须一并成立
        runtime = RuntimeBag(tools={name: TOOL_REGISTRY[name]})
        prompt = _build_system_prompt(runtime, {}, "SCHEMA")
        assert f"- {name}：" in prompt, name
        assert any(
            "当期应收" in c and "SK.receivables" in c for c in _clauses(prompt)
        ), f"{name} 决策轮 prompt 未渲染「当期应收 … SK.receivables」同句"
        assert "资产管理" in prompt, name


def test_erp_query_and_calc_bind_balance_scope() -> None:
    """policy_family 绑定：erp_query 保留 R4.query 并追加余额包；
    erp_calc 由空改绑余额包（不绑 R4.query，原注释论证仍成立）。"""
    assert POLICY_KEY in POLICY_REGISTRY
    assert TOOL_REGISTRY["erp_query"].policy_family == ("R4.query", POLICY_KEY)
    assert TOOL_REGISTRY["erp_calc"].policy_family == (POLICY_KEY,)
