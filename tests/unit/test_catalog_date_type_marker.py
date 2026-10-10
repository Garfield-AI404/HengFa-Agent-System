"""[2026-09-22] 参数轮字段目录：日期型字段标记守护。

背景（历史根因族「参数轮无业务背景」又一例）：参数轮字段目录此前只给
「列名（中文字段 label）」，**不含字段类型**。``erp_calc`` 的
``time_granularity``（按月/季汇总）要求 ``group_by`` 是**日期型**，否则
fail closed 拒绝（calc.py 4.0 段，``_DATE_FIELD_TYPES``）——模型在参数轮
看不出哪个字段是日期型，只能挑一个试、被拒后读报错（报错会回列实际类型）
重试，**白烧一轮**。本批在**两段共用的唯一渲染器** ``_render_catalog_field``
上给日期型字段加标记（括号内、用元数据原样类型 token，与 calc.py 报错词汇
一致）：有 label → ``in_date（入库日期·date）``，无 label → ``in_date（date）``。

本模块钉住（每条给出变异读数，见各类 docstring）：

1. 日期字段带标记（``date`` 与 ``datetime`` 两种类型都标）；
2. **反向**：非日期字段不得带任何类型标记（防标记变噪音、把非日期字段教成
   日期）；
3. **同源**：erp_query 段与 erp_calc 段对同一实体同一字段给出**相同**标记；
4. **无 label 路径**：label 缺失/等于列名的日期字段仍渲染成 ``列名（type）``；
   且非日期无 label 字段渲染**逐字节不变**（= 只有列名）；
5. **不变式**：无日期字段的夹具下两段渲染**逐字节等于改动前基线**（硬编码
   对照），且三段结构/段头不受影响。

假 ERP：单实体 metal_orders（白名单内含 in_date/out_date 两个日期字段 →
普通段真实渲染；created_at/created_by/notes 白名单外 → 仅筛选段渲染）。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from administrative_agent.agent.nodes.tool_args_node import (
    _erp_calc_catalog_section,
    _erp_query_catalog_section,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache


def _f(ftype: str = "str", label: str | None = None, **extra) -> dict:
    """字段元数据；``label=None`` → 键缺失（触发「无 label」路径）。"""
    spec: dict = {"type": ftype, **extra}
    if label is not None:
        spec["label"] = label
    return spec


#: 行首为小写实体名（可带「（实体中文）」）的字段数据行。
_DATA_LINE = re.compile(r"^[a-z_]+(（[^）]*）)?：")


def _entity(name: str, label: str, fields: dict) -> dict:
    return {
        "name": name, "label": label, "readable": True,
        "creatable": False, "updatable": False, "deletable": False,
        "business_operations": [], "fields": fields,
    }


#: metal_orders 白名单含 id/in_date/out_date（普通段）；created_at/created_by/
#: notes 不在白名单（仅筛选段）；ghost_total = 计算列 ∩ 白名单外（三不，不入段）。
_DATE_FIELDS = {
    "id": _f("int", "ID"),
    "in_date": _f("date", "入库日期"),
    "out_date": _f("datetime"),          # 无 label 的日期字段
    "created_at": _f("datetime", "创建时间"),  # 白名单外 datetime → 仅筛选段
    "created_by": _f("str", "创建人"),    # 白名单外非日期 → 仅筛选段
    "notes": _f("str"),                  # 白名单外无 label 非日期
    "ghost_total": _f("decimal", "幽灵合计", computed=True),
}

#: 无日期字段的对照夹具（普通段三字段均非日期，label 齐全）→ 用于基线。
_NON_DATE_FIELDS = {
    "id": _f("int", "ID"),
    "in_date": _f("str", "入库日期"),
    "out_date": _f("str", "出库日期"),
}


class _FakeErp:
    def __init__(self, fields: dict) -> None:
        self._raw = _entity("metal_orders", "钣金订单", fields)

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": [dict(self._raw)]})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


def _cache(fields: dict) -> EntityCache:
    return EntityCache(_FakeErp(fields))


def _data_lines(section: str) -> set[str]:
    return {ln for ln in section.split("\n") if _DATA_LINE.match(ln)}


def _field_tokens(section: str) -> set[str]:
    """目录段全部字段 token：数据行「：」之后按 ``", "`` 切分（与渲染拼接符一致）。"""
    toks: set[str] = set()
    for ln in _data_lines(section):
        toks.update(t for t in ln.partition("：")[2].split(", ") if t)
    return toks


def _expected_token(name: str, spec: dict) -> str:
    """按**契约**从夹具声明的字段元数据推出期望 token。

    判据源与渲染器**同层**：字段 ``type`` 与 ``label``（不是 token 后缀形状）。
    - 有 label：``列名（label）``；日期型 → ``列名（label·type）``；
    - label 缺失/等于列名：``列名``；日期型 → ``列名（type）``。
    """
    label = spec.get("label", "") or ""
    ftype = spec.get("type", "") or ""
    date = ftype in ("date", "datetime")
    if label and label != name:
        return f"{name}（{label}{'·' + ftype if date else ''}）"
    if date:
        return f"{name}（{ftype}）"
    return name



# ---------------------------------------------------------------------------
# 1. 日期字段带标记（date 与 datetime）
# ---------------------------------------------------------------------------


def test_date_fields_carry_type_marker_in_both_sections() -> None:
    """``date`` 与 ``datetime`` 两种日期字段都带原样类型 token，两段都在。

    变异（读数红）：把 ``date_suffix`` 恒置空（删掉标记逻辑）→ 本用例
    ``in_date（入库日期·date）`` / ``out_date（datetime）`` / 仅筛选段
    ``created_at（创建时间·datetime）`` 全部消失 → 红。
    """
    query = _erp_query_catalog_section(_cache(_DATE_FIELDS))
    calc = _erp_calc_catalog_section(_cache(_DATE_FIELDS))
    assert query is not None and calc is not None
    for section in (query, calc):
        toks = _field_tokens(section)
        # 普通段：有 label 的 date、无 label 的 datetime
        assert "in_date（入库日期·date）" in toks, sorted(toks)
        assert "out_date（datetime）" in toks, sorted(toks)
        # 仅筛选段：datetime 同样带标记
        assert "created_at（创建时间·datetime）" in toks, sorted(toks)


# ---------------------------------------------------------------------------
# 2. 反向：非日期字段不得带任何类型标记
# ---------------------------------------------------------------------------


def test_non_date_fields_carry_no_type_marker() -> None:
    """非日期字段渲染**逐字节为原形态**（``列名（label）`` / ``列名``），不带任何
    类型后缀。防「标记变成噪音、把非日期字段也教成日期」。

    判据与渲染器**同层**：[2026-09-22 复审加固] 从夹具声明的字段 ``type``/``label``
    推出期望 token 集合（``_expected_token``），与实渲染的 token 集合**逐一相等**
    比对——**不再**用「含 ``·`` 的 token 后缀形状」这种间接判据（旧写法把非日期
    字段误标成**合法日期 token**（如 ``id（ID·datetime）``）时会漏网，靠别的断言
    兜住；且夹具新增任何有 label 的非日期字段而忘了加显式断言就抓不到）。

    变异（读数红）：
    - ``date_suffix`` 判据从 ``ftype in _DATE_FIELD_TYPES`` 改成 ``if ftype``
      （对所有字段加标记）→ ``id（ID·int）`` / ``created_by（创建人·str）`` 等
      出现 → 集合不等 → 红；
    - 把非日期字段误标成**合法日期 token**（如令所有字段恒得 ``·datetime`` →
      ``id（ID·datetime）``）→ 集合不等 → 红（旧后缀判据会 GREEN）。
    """
    section = _erp_query_catalog_section(_cache(_DATE_FIELDS))
    assert section is not None
    toks = _field_tokens(section)
    expected = {
        _expected_token(name, spec)
        for name, spec in _DATE_FIELDS.items()
        if name != "ghost_total"  # 三不字段（计算列 ∩ 白名单外）不入任何段
    }
    assert toks == expected, (
        "目录 token 集合与按字段 type/label 推出的期望不符（非日期误标 / 日期漏标 / "
        "label 渲染漂移）：\n"
        f"渲染多出={sorted(toks - expected)}\n渲染缺失={sorted(expected - toks)}"
    )


# ---------------------------------------------------------------------------
# 3. 同源：两段对同一实体同一字段给出相同标记
# ---------------------------------------------------------------------------


def test_two_sections_mark_date_fields_identically() -> None:
    """erp_query 段与 erp_calc 段的字段数据行**逐字节相同**（共用唯一渲染器）。

    变异（读数红）：把其中一段（这里取 calc 段）的字段渲染换成不含日期标记的
    内联渲染 → 该段 ``in_date（入库日期·date）`` 缺失、两段数据行不再逐字节
    相同 → 红。
    """
    query = _erp_query_catalog_section(_cache(_DATE_FIELDS))
    calc = _erp_calc_catalog_section(_cache(_DATE_FIELDS))
    assert query is not None and calc is not None
    q_lines, c_lines = _data_lines(query), _data_lines(calc)
    assert q_lines == c_lines, (
        "两段日期标记漂移（应共用 _render_catalog_field）：\n"
        f"query-only={q_lines - c_lines}\ncalc-only={c_lines - q_lines}"
    )
    # 且两段都确实带日期标记（防两段同时退化成无标记而「一致」）
    assert "in_date（入库日期·date）" in _field_tokens(query)
    assert "in_date（入库日期·date）" in _field_tokens(calc)


# ---------------------------------------------------------------------------
# 4. 无 label 路径
# ---------------------------------------------------------------------------


def test_unlabeled_date_field_marked_and_unlabeled_non_date_bare() -> None:
    """无 label 的日期字段渲染成 ``列名（type）``；无 label 的非日期字段渲染
    **逐字节不变**（只有列名，无括号）。

    变异（读数红）：删掉无 label 分支的 ``if date_suffix`` 回退 → 无 label 日期
    字段退化成裸 ``out_date`` → 红。

    [2026-09-22 复审加固] 「无 label 非日期字段不得渲染括号」的判据**落在 token 级**
    —— ``notes`` 必须作为一个完整 token 存在（若渲染成 ``notes（str）`` 等任何带
    括号形态，``_field_tokens`` 切出的 token 就不再等于 ``notes``、该断言即变红）。
    删除了原 ``"notes（" not in section`` 的**整段子串**式否定断言：它无独立判别力
    （三种带括号形态都已被 token 级断言先抓住），且会被夹具/邻近文本里任何含
    ``notes（`` 的子串弄成假红。
    """
    section = _erp_query_catalog_section(_cache(_DATE_FIELDS))
    assert section is not None
    toks = _field_tokens(section)
    # 无 label 的 datetime → 带类型
    assert "out_date（datetime）" in toks, sorted(toks)
    # 无 label 的非日期（notes）→ 恰为裸列名 token（token 级精确判定）
    assert "notes" in toks, sorted(toks)


# ---------------------------------------------------------------------------
# 5. 不变式：无日期夹具下渲染逐字节等于改动前基线（硬编码对照）
# ---------------------------------------------------------------------------

#: 无日期字段夹具下 erp_query 段的字段数据行（改动前形态的硬编码基线）。
_BASELINE_LINE = "metal_orders（钣金订单）：id（ID）, in_date（入库日期）, out_date（出库日期）"


def test_no_date_fixture_renders_byte_identical_to_baseline() -> None:
    """无日期字段时，渲染与改动前**逐字节一致**（硬编码基线对照）；且全段不含
    类型分隔符 ``·``。

    变异（读数红）：任何给无日期字段也加/改渲染的改动（如 label==列名时仍强行
    渲染括号、或加类型后缀）→ 与硬编码基线不符 / 出现 ``·`` → 红。
    """
    query = _erp_query_catalog_section(_cache(_NON_DATE_FIELDS))
    calc = _erp_calc_catalog_section(_cache(_NON_DATE_FIELDS))
    assert query is not None and calc is not None
    lines = _data_lines(query)
    assert _BASELINE_LINE in lines, lines
    assert "·" not in query and "·" not in calc, (
        "无日期字段不得出现类型标记：\n" + query
    )
    # 两段逐字节同形（既有同源不变式的补充）
    assert _data_lines(calc) == lines


def test_three_section_structure_intact_under_date_fixture() -> None:
    """加标记不得改变三段结构：合法日期夹具下，普通段 + 仅筛选段各有实体行，
    且无计算列时不出现「计算列（」段头（守护 ``if computed_lines:``）。"""
    section = _erp_calc_catalog_section(_cache(_DATE_FIELDS))
    assert section is not None
    assert section.startswith("实体字段目录（erp_calc 的")
    assert "计算列（" not in section
    assert section.count("仅筛选字段（") == 1, section
