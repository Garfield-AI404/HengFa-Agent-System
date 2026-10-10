"""[2026-09-16 决策轮提示词两处改动] 守护测试。

改动一（工具描述去重）：``build_select_schema`` 不再内联 ``name（description）``
枚举表，改为指针句「tool_name 只能取上方『可用工具』表中的名称」；工具表唯一一份
落在 ``model_node._build_system_prompt`` 的「可用工具」段，且必须排在该指针句所指
的「结构化输出说明：」段**之前**（否则「上方」指空）。

改动二（P2-1 形态 A）：决策轮规则包索引段由裸 ``- {key}`` 升级为
``- {key}：{title}｜{admission}｜触发工具 {names}``：title 取自规则包本身，准入句
取自 ``POLICY_ADMISSION``，触发工具由 ``TOOL_REGISTRY`` 的 **两关系并集** 求逆
（``POLICY_TOOLS``，代码生成）：(i) ``spec.policy_family`` 的逆（参数轮注入哪个包
的正文）、(ii) ``spec.policy_key_of`` 的静态输出键（工具执行后登记哪个键）。渲染的
键来自 ``state["policy_keys"]``（即 (ii) 的产物），只看 (i) 会把
``R6.reconciliation.plan`` 说成「无任何工具引用」。

本文件按本仓「prompt 守护测试假锁」纪律设计：锚点与后果同处一个小句 / 同一渲染行，
断言取自**真实注入面**（``_build_system_prompt`` 的产物、``build_select_schema``
的产物），不手抄字符串。注：索引行的原子单位是**行**（``\\n`` 切分）而非句——准入句
本身可能含「；」（如 R4.balance.scope），按句切会把一条准入句劈开造成误报。
"""

from __future__ import annotations

import hashlib
import importlib

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.model_client import build_select_schema
from administrative_agent.agent.nodes.model_node import _build_system_prompt

#: 节点模块本身——``from ...nodes import model_node`` 取到的是**节点函数**
#: （nodes/__init__.py 同名 re-export），monkeypatch 需要模块对象，故显式 import。
model_node = importlib.import_module("administrative_agent.agent.nodes.model_node")
from administrative_agent.agent.prompts.policies import (
    ALL_POLICY_KEYS,
    POLICY_ADMISSION,
    POLICY_HASHES,
    POLICY_REGISTRY,
)
from administrative_agent.agent.tools import POLICY_TOOLS, TOOL_REGISTRY

#: 用户拍板的准入句**逐字文案**（基线）。锚点直接钉在字面量上——若 ``POLICY_ADMISSION``
#: 的措辞被删词/改词，本基线即失配变红（准入句是模型可见文本，属用户资产，
#: 不得由实现方擅自改写）。**注意**：不能拿 ``POLICY_ADMISSION`` 自身做锚点
#: 去断言渲染行——那样两侧同源（改一处两处一起变），会退化成假锁（本仓
#: 「prompt 守护测试假锁」教训）。
_EXPECTED_ADMISSION: dict[str, str] = {
    "R1.attendance.daily": "用户要记录当天考勤（谁没来、请假、加班）时适用",
    "R1.salary.calculation": "用户要算工资或问工资怎么算时适用",
    "R2.order.create.metal": "用户要新建钣金订单（客户、型号、数量）时适用",
    "R2.order.create.powder": "供货商送货到货、要新建塑粉订单（色号、kg）时适用",
    "R3.order.update.metal": "用户要改已有钣金订单的字段（状态、交期、数量等）时适用",
    "R3.order.update.powder": "用户要改已有塑粉订单的字段（色号、重量、价格）时适用",
    "R4.balance.scope": "问客户/供货商还欠多少、未结余额、当期应收时适用；不得自行推算未结余额",
    "R4.export.fields": "用户要导出 ERP 数据到 Excel（列、合计、文件名）时适用",
    "R4.query": "凡涉及 ERP 数据的查询都适用：先查后答、字段、分页、空结果",
    "R5.analysis": "对 ERP 数据做分析、下结论、给建议时适用",
    "R6.mapping": "管理对账映射模板（发布版本、列校验、版本递增）时适用",
    "R6.reconciliation.execute": "用户确认差异报告后要写回 ERP 订单时适用",
    "R6.reconciliation.plan": "生成对账差异清单时适用（只读，禁止写入）",
    "R7.finance.create": "用户要记录收入/支出/投资款流水时适用",
    "R7.finance.update": "用户要改已有收支记录的字段时适用",
}


def _index_lines(prompt: str) -> list[str]:
    """取索引段的行（以 ``- `` 开头且含 ``：`` 的行）。"""
    return [ln for ln in prompt.split("\n") if ln.startswith("- ") and "：" in ln]


# ---------------------------------------------------------------------------
# 断言 a：每个注册包的索引行同「行」含 key / title / admission
# 变异：删 admission 里某个词 → 对应断言红
# ---------------------------------------------------------------------------


def test_index_line_carries_key_title_and_admission() -> None:
    runtime = RuntimeBag(tools=TOOL_REGISTRY)
    for key, package in POLICY_REGISTRY.items():
        prompt = _build_system_prompt(runtime, {"policy_keys": [key]}, "")
        lines = [ln for ln in _index_lines(prompt) if ln.startswith(f"- {key}：")]
        assert lines, f"{key}：决策轮未渲染出该包的索引行"
        line = lines[0]
        assert package.title in line, f"{key}：索引行缺 title {package.title!r}"
        # 锚点用**独立字面量**（_EXPECTED_ADMISSION）而非 POLICY_ADMISSION——
        # 两侧同源会让「删准入句里的词」在渲染与断言上一起变、退化成假锁。
        assert _EXPECTED_ADMISSION[key] in line, (
            f"{key}：索引行缺准入句 {_EXPECTED_ADMISSION[key]!r}"
        )
        # 渲染实际引用的仍是 registry 里的那一条（防「渲染读别处常量」）
        assert POLICY_ADMISSION[key] in line
        assert package.title and POLICY_ADMISSION[key]


# ---------------------------------------------------------------------------
# 断言 b：隶属工具由代码求逆（不是手写常量）
# 变异：把渲染行的工具名改成硬编码 → 本用例红
# ---------------------------------------------------------------------------


def _static_policy_keys(spec: object) -> tuple[str, ...]:
    """复算 ``policy_key_of`` 的静态输出键（字节码常量 ∩ ALL_POLICY_KEYS）。

    不真的调用 ``policy_key_of``（可能依赖 args 形态），只提取其字节码常量。
    """
    code = getattr(getattr(spec, "policy_key_of", None), "__code__", None)
    if code is None:
        return ()
    return tuple(
        c for c in code.co_consts if isinstance(c, str) and c in ALL_POLICY_KEYS
    )


def _code_inverse() -> dict[str, tuple[str, ...]]:
    """独立复算：两关系并集（工具名按注册顺序去重）。

    (i) ``spec.policy_family`` 的逆；(ii) ``spec.policy_key_of`` 的静态输出键。
    与生产代码同序（每个 spec 先 family 后 static）。
    """
    acc: dict[str, list[str]] = {}
    for spec in TOOL_REGISTRY.values():
        for key in (*spec.policy_family, *_static_policy_keys(spec)):
            names = acc.setdefault(key, [])
            if spec.name not in names:
                names.append(spec.name)
    return {k: tuple(v) for k, v in acc.items()}


def test_policy_tools_equals_code_inverse() -> None:
    """``POLICY_TOOLS`` 必须等于对两关系代码求逆（并集）的结果——证明它由
    ``policy_family`` ∪ ``policy_key_of`` 派生，而非手写常量表。"""
    assert POLICY_TOOLS == _code_inverse()
    # 键集是 ALL_POLICY_KEYS 的子集（未引用的包不在表里）
    assert set(POLICY_TOOLS) <= set(ALL_POLICY_KEYS)


def test_policy_tools_is_union_of_two_code_relations() -> None:
    """独立复算并集口径（实现路径与生产代码不同）：两关系分别求逆后再并。

    钉住「只在关系 (ii) 出现」的键集——它是 P1 的全部价值所在：
    ``policy_family`` 逆覆盖不到的包（如 R6.reconciliation.plan）必须由
    ``policy_key_of`` 补上，否则渲染成「无任何工具引用」与事实矛盾。
    """
    family_inverse: dict[str, list[str]] = {}
    key_of_inverse: dict[str, list[str]] = {}
    for spec in TOOL_REGISTRY.values():
        for key in spec.policy_family:
            names = family_inverse.setdefault(key, [])
            if spec.name not in names:
                names.append(spec.name)
        for key in set(_static_policy_keys(spec)):
            names = key_of_inverse.setdefault(key, [])
            if spec.name not in names:
                names.append(spec.name)

    # 键集 = 两关系键集的并；每个键的工具集 = 两关系工具集的并（顺序无关）
    assert set(POLICY_TOOLS) == set(family_inverse) | set(key_of_inverse)
    for key, names in POLICY_TOOLS.items():
        expected = set(family_inverse.get(key, ())) | set(key_of_inverse.get(key, ()))
        assert set(names) == expected, f"{key}：隶属工具集与两关系并不一致"

    # 只在关系 (ii) 出现、关系 (i) 覆盖不到的可渲染键——恰为 R6.reconciliation.plan
    only_key_of = set(key_of_inverse) - set(family_inverse)
    assert only_key_of == {"R6.reconciliation.plan"}, (
        "只在 policy_key_of 出现的键集变了："
        f"{sorted(only_key_of)}——请同步 P1 断言与注释"
    )


# ---------------------------------------------------------------------------
# 断言 g：**真调用** policy_key_of → 每个「可渲染键」都有真实工具（钉住静默掉队）
# 变异：把 attachment_preview 的 policy_key_of 改成静态取不到的形式 →
#       test_every_renderable_key_has_a_real_tool 红（断言 B）
# ---------------------------------------------------------------------------

#: 代表性调用 args——与生产调用点同形态：``tools_node`` 里
#: ``spec.policy_key_of(args_raw)``，``args_raw`` 是**模型原始参数 dict**
#: （可能缺键，``pending.get("tool_arguments") or {}``，见 tools_node.py:210/362）。
#: 覆盖 order_type 的有意义取值（metal/powder/unknown）与两种缺省形态
#: （无键/显式 None），逼出 ``f"…{args.get('order_type')}"`` 一类分支。
_POLICY_KEY_OF_ARGS: tuple[dict[str, object], ...] = (
    {},
    {"order_type": "metal"},
    {"order_type": "powder"},
    {"order_type": "unknown"},
    {"order_type": None},
)

#: 无法用上述 args 调用的 spec 白名单（**显式列出并写清理由，让缺口可见**）。
#: 当前为空：全部 spec 的 ``policy_key_of`` 都只读 order_type 或不读 args，
#: 上述 args 集合均可安全调用。若未来新增 spec 因语义原因需要其它必填字段，
#: 请**在此登记并写明理由**——不得改用 try/except 静默跳过（静默跳过 = 假锁，
#: 正是本用例要消灭的失败模式）。
_UNCONSTRUCTIBLE_SPECS: frozenset[str] = frozenset()


def _normalize_policy_key(value: object) -> tuple[str, ...]:
    """把 ``policy_key_of`` 返回值归一化成键元组：str → ``(str,)``；None/空 → ``()``。"""
    if isinstance(value, str) and value:
        return (value,)
    return ()


def test_every_renderable_key_has_a_real_tool() -> None:
    """**真调用** ``policy_key_of``，钉住「可渲染键 → 触发工具」不静默掉队。

    为什么要有这条：``POLICY_TOOLS`` 的关系 (ii) 用**静态**提取
    （``policy_key_of`` 的字节码常量）。静态提取对「常量字面量」能取到键、
    对 ``f"…{args.get(...)}"`` 取不到（自然滤掉），但**发现不了**
    「本来返回常量键、被人改写成静态取不到的形式」这种改写：例如把
    ``attachment_preview`` 的 ``policy_key_of`` 由 ``"R6.reconciliation.plan"``
    改成 ``"R6.reconciliation" + ".plan"``——真调用仍返回同一个键，静态却
    取不到，于是 ``POLICY_TOOLS["R6.reconciliation.plan"]`` 少了
    ``attachment_preview``：决策轮索引行里该工具**静默消失**，而既有全部测试
    照绿（``only_key_of == {"R6.reconciliation.plan"}`` 仍成立）。本用例以
    **真调用结果**为期望值（与静态提取**不同源**），补上这道缺口。

    断言：
    A（假阳性）静态提取集 ⊆ 真调用集——「静态不得比真实多」；
    B（静默掉队·要害）真调用产出的每个键，其触发工具必须含本工具；
    C（无空元组）真调用产出的每个键在 ``POLICY_TOOLS`` 里工具元组非空。

    期望值取自**真调用**与**表**两侧（不同源），不由 ``POLICY_TOOLS`` 自证。
    变异：把某工具的 policy_key_of 改成静态取不到的形式 → 断言 B 红。
    """
    for spec in TOOL_REGISTRY.values():
        if spec.name in _UNCONSTRUCTIBLE_SPECS:
            continue
        # 真调用（**不 try/except**——若某 spec 在此 args 上抛异常，让测试失败
        # 并把该 spec 与异常一起报出来，这是要发现的问题而非要绕过的噪音）
        actual: set[str] = set()
        for args in _POLICY_KEY_OF_ARGS:
            value = spec.policy_key_of(args)
            for key in _normalize_policy_key(value):
                if key in ALL_POLICY_KEYS:
                    actual.add(key)
        static = set(_static_policy_keys(spec))
        # 断言 A：静态不得比真实多（假阳性）
        assert static <= actual, (
            f"{spec.name}：静态提取键 {sorted(static - actual)} 未在真调用中出现"
            "——静态比真实多，POLICY_TOOLS 会登记假工具"
        )
        # 断言 B（要害）：真能产出该键的工具，必须出现在该键的触发工具里
        for key in actual:
            names = POLICY_TOOLS.get(key, ())
            assert spec.name in names, (
                f"{spec.name}：真调用产出可渲染键 {key!r}，却未出现在"
                f" POLICY_TOOLS[{key!r}]（当前 {names}）——该工具会在决策轮"
                "索引行里静默消失（policy_key_of 疑被改写成静态取不到的形式）"
            )
        # 断言 C：每个可渲染键的工具元组非空
        for key in actual:
            assert POLICY_TOOLS.get(key), (
                f"{key}：POLICY_TOOLS 里工具元组为空（应为非空）"
            )


def test_reconciliation_plan_row_names_the_registering_tools() -> None:
    """P1：``R6.reconciliation.plan`` 的索引行必须点出**登记它的工具**。

    该键由 ``reconcile_analyze`` / ``attachment_preview`` 的
    ``policy_key_of`` 在工具执行后登记（写入 ``state["policy_keys"]``），但两者
    ``policy_family`` 为空——只对 ``policy_family`` 求逆会把该行渲染成
    「（当前无工具引用）」，与模型刚执行完的动作直接矛盾。此处用**独立字面量**
    断言（不拿 ``POLICY_TOOLS`` 自证顺序以外的内容）。
    变异：把 ``POLICY_TOOLS`` 退回只取 ``policy_family`` → 本用例红。
    """
    assert POLICY_TOOLS["R6.reconciliation.plan"] == (
        "reconcile_analyze",
        "attachment_preview",
    ), "R6.reconciliation.plan 的触发工具（按注册序）不是这两个"
    prompt = _build_system_prompt(
        RuntimeBag(tools=TOOL_REGISTRY),
        {"policy_keys": ["R6.reconciliation.plan"]},
        "",
    )
    lines = [
        ln
        for ln in _index_lines(prompt)
        if ln.startswith("- R6.reconciliation.plan：")
    ]
    assert lines, "R6.reconciliation.plan：未渲染索引行"
    line = lines[0]
    assert "reconcile_analyze" in line, "该行未点出 reconcile_analyze"
    assert "attachment_preview" in line, "该行未点出 attachment_preview"
    assert "无工具引用" not in line, "该行出现「无工具引用」否定断言（与事实矛盾）"
    # 行仍必须携带 key / title / admission
    assert "R6.reconciliation.plan" in line
    assert POLICY_REGISTRY["R6.reconciliation.plan"].title in line
    assert _EXPECTED_ADMISSION["R6.reconciliation.plan"] in line


def test_render_tool_names_consumed_from_policy_tools(monkeypatch) -> None:
    """渲染行里的触发工具名必须**读自** ``POLICY_TOOLS``：monkeypatch 该表后
    渲染随之变化（若渲染内联硬编码工具名，本断言红）。"""
    runtime = RuntimeBag(tools=TOOL_REGISTRY)
    monkeypatch.setattr(
        model_node, "POLICY_TOOLS", {**POLICY_TOOLS, "R4.query": ("__fake_tool__",)}
    )
    prompt = _build_system_prompt(runtime, {"policy_keys": ["R4.query"]}, "")
    lines = [ln for ln in _index_lines(prompt) if ln.startswith("- R4.query：")]
    assert lines, "R4.query：未渲染索引行"
    assert "触发工具 __fake_tool__" in lines[0], (
        "渲染行未消费 POLICY_TOOLS —— 触发工具名疑似硬编码"
    )


# ---------------------------------------------------------------------------
# 断言 c：POLICY_ADMISSION 键集 == ALL_POLICY_KEYS 且值非空
# ---------------------------------------------------------------------------


def test_policy_admission_keyset_and_nonempty() -> None:
    assert set(POLICY_ADMISSION) == set(ALL_POLICY_KEYS), (
        "POLICY_ADMISSION 键集与 ALL_POLICY_KEYS 不一致"
    )
    for key, text in POLICY_ADMISSION.items():
        assert text and text.strip(), f"{key}：准入句为空"


def test_policy_admission_verbatim_baseline() -> None:
    """准入句是用户拍板的**逐字文案**（模型可见文本，属用户资产）——与基线
    逐字相等，删词/改词/增项即红（变异①）。"""
    assert POLICY_ADMISSION == _EXPECTED_ADMISSION


# ---------------------------------------------------------------------------
# 断言 d：工具表段在「结构化输出说明：」之前（指针句「上方」的落点）
# 变异：调换工具表与 schema 段顺序 → 本用例红
# ---------------------------------------------------------------------------


def test_tool_table_precedes_schema_section() -> None:
    prompt = _build_system_prompt(RuntimeBag(tools=TOOL_REGISTRY), {}, "")
    assert "可用工具（" in prompt
    assert "结构化输出说明：" in prompt
    # 工具表段**唯一**：段头只出现一次。这是位置断言的必要补强——若在 schema
    # 段之后再渲染一份同样的工具表（正是本批消灭的重复形态），仅凭「首次出现
    # 下标」判定位置不会红（本仓「prompt 守护测试假锁」教训：锚点与后果要同处
    # 一个小句）。count == 1 能咬住「再渲染一份」的变异。
    assert prompt.count("可用工具（") == 1, (
        f"工具表段头出现 {prompt.count('可用工具（')} 次——去重后全 prompt"
        "只应有一份工具枚举表"
    )
    assert prompt.index("可用工具（") < prompt.index("结构化输出说明："), (
        "工具表段未排在「结构化输出说明：」之前——指针句「上方『可用工具』表」会指空"
    )


# ---------------------------------------------------------------------------
# 断言 f：新增 admission 不得影响 policy_hash（回归钉）
# ---------------------------------------------------------------------------


def test_policy_hashes_unaffected_by_admission() -> None:
    """POLICY_HASHES[key] 恒等于 sha256("key|version|text")——准入句不参与 hash。"""
    for key, package in POLICY_REGISTRY.items():
        raw = f"{key}|{package.version}|{package.text}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert POLICY_HASHES[key] == expected, f"{key}：policy_hash 算法/取值被改动"


# ---------------------------------------------------------------------------
# 断言 e 的对照：指针句本身（build_select_schema 负向断言见 test_args_schema_dedup）
# ---------------------------------------------------------------------------


def test_pointer_sentence_present() -> None:
    text = build_select_schema(TOOL_REGISTRY)
    assert "tool_name 只能取上方『可用工具』表中的名称，不得臆造其他工具名" in text
