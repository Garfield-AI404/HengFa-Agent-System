"""[2026-09-16] erp_calc 参数轮「实体字段目录」段守护。

背景：参数轮（tool_args 节点）此前只给 ``erp_query`` / ``finance_*`` 注入
字段目录，``erp_calc`` **无任何分支**——模型写 ``filter``/``group_by``/
``measures`` 时看不到任何字段名，只能凭记忆猜或先花一次工具往返调
``list_entities``。本批新增 ``_erp_calc_catalog_section``，信息源与 erp_query
段**完全同源**（同一 EntityCache、同一 ``_QUERY_CATALOG_ORDER``、同一
``crop.partition_catalog_fields`` 三段划分），只有三段措辞按其参数面重写。

本模块钉住：

1. 端到端：``erp_calc`` 参数轮 prompt 确实包含目录段，且在结构化输出说明之前；
2. 三段各自存在且措辞正确——段 2 必须**同时**含「不可用于 filter」（后果同句）
   与「可用于 group_by/measures」（与 erp_query 段相反的放行）；
3. 段 3 **正向锁定**：段头恰好等于规范串（唯一正向承诺「可用于本工具的 filter
   条件」逐字钉死，锚点与后果同一行）——不靠「整段含某词」的黑名单（裸子串会被
   夹具字段名 ``extra_filter_only`` 救活，中文同义词也拦不住）；禁词断言仅作补充；
4. 注入门：非 ``erp_query`` / ``erp_calc`` 的工具（``finance_create``）参数轮
   不得出现任一目录段（防 ``elif spec.name == "erp_calc"`` 越界到其他工具）；
5. 渲染门：无计算列时不得出现计算列段头（守护 ``if computed_lines:``）；
6. 两段目录覆盖的实体与字段集合**逐一致**（同一 ``partition_catalog_fields``，
   防双真源漂移）；
7. 降级：cache=None / get_entities 抛异常 / 无实体数据 → 返回 None，不抛；
8. ``_erp_query_catalog_section`` 输出**逐字节基线**（sha256 钉子）；
   [2026-09-16 当期应收口径开放] 只读实体 ar_settlements 接入读白名单 /
   ``_QUERY_CATALOG_ORDER`` 后，该段按预期新增两行 ar_settlements（普通字段段
   与仅筛选段各一行）；[2026-09-18 字段中文 label] 字段渲染改为「列名（中文
   label）」后基线**有意重采**；除这两次有意的变更外，该段任何字节漂移仍会变红；
9. 字段中文 label（[2026-09-18]）：每字段渲染成 ``列名（中文 label）``——label
   与列名同 token（防裸子串假锁）、label 缺失/等于列名时只渲染列名（不重复）、
   两段字段数据行逐字节相同（共用 ``_render_catalog_fields``）。

假 ERP：customers（白名单内 / 白名单外仅筛选 / 计算列三态齐全）+
powder_inventory（白名单内计算列 → 计算列段真实渲染）+ 其余 QUERYABLE_ENTITIES
合成三态实体，保证三段都真实渲染。
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.tool_args_node import (
    _erp_calc_catalog_section,
    _erp_query_catalog_section,
    tool_args_node,
)
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES


def _field(name: str, ftype: str = "str", **extra) -> dict:
    return {"type": ftype, "label": name, **extra}


#: 目录段实体字段数据行：行首为小写实体名，可带「（实体中文名）」。
_DATA_LINE = re.compile(r"^[a-z_]+(（[^）]*）)?：")


#: customers：白名单 = id/company_name/unit_price/price_type/price_unit
#: 三态字段：tax_id/bank_account/address（白名单外，仅筛选）；
#: ghost_total（计算列 ∩ 白名单外 = 三不，不入任何段）
_CUSTOMERS_FIELDS = {
    "id": _field("ID", "int"),
    "company_name": _field("公司名称"),
    "unit_price": _field("单价", "decimal"),
    "price_type": _field("价格类型"),
    "price_unit": _field("计价单位"),
    "tax_id": _field("税号"),
    "bank_account": _field("银行账号"),
    "address": _field("地址"),
    "ghost_total": _field("幽灵合计", "decimal", computed=True),
}

#: powder_inventory：白名单内计算列（order_total/consumed_total/stock）→
#: 第二段（计算列段）真实渲染；notes 白名单外非计算列 → 第三段也渲染
_POWDER_INVENTORY_FIELDS = {
    "id": _field("ID", "int"),
    "color_code": _field("色号"),
    "warn_threshold": _field("预警阈值", "decimal"),
    "notes": _field("备注"),
    "order_total": _field("订单总重量", "decimal", computed=True),
    "consumed_total": _field("消耗总量", "decimal", computed=True),
    "stock": _field("净库存", "decimal", computed=True),
}


def _synthetic_entity(name: str) -> dict:
    """其余实体合成最小元数据：id（可回传）/ extra_filter_only（白名单外）/
    extra_ghost（计算列 ∩ 白名单外 = 三不）。"""
    return {
        "name": name,
        "label": name,
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "extra_filter_only": _field("额外可筛选"),
            "extra_ghost": _field("幽灵", "decimal", computed=True),
        },
    }


def _make_entities() -> list[dict]:
    out: list[dict] = [
        {
            "name": "customers",
            "label": "客户",
            "readable": True,
            "creatable": True,
            "updatable": True,
            "deletable": False,
            "business_operations": [],
            "fields": dict(_CUSTOMERS_FIELDS),
        },
        {
            "name": "powder_inventory",
            "label": "塑粉库存色号",
            "readable": True,
            "creatable": False,
            "updatable": False,
            "deletable": False,
            "business_operations": [],
            "fields": dict(_POWDER_INVENTORY_FIELDS),
        },
    ]
    for name in sorted(QUERYABLE_ENTITIES):
        if name not in ("customers", "powder_inventory"):
            out.append(_synthetic_entity(name))
    return out


_ENTITIES: list[dict] = _make_entities()


def _strip_computed(entities: list[dict]) -> list[dict]:
    """深拷一份实体元数据并抹掉所有 ``computed`` 标记（用于 P4：无计算列场景）。

    返回的实体集合与 ``_ENTITIES`` 同规模（仍能渲染普通段与仅筛选段），
    但**没有任何计算列**——此时 ``computed_lines`` 为空，不得渲染计算列段。
    """
    out: list[dict] = []
    for entity in entities:
        clone = dict(entity)
        clone["fields"] = {
            fname: {k: v for k, v in fspec.items() if k != "computed"}
            for fname, fspec in entity["fields"].items()
        }
        out.append(clone)
    return out


class _NoComputedErp:
    """假 ERP：元数据与 ``_FakeErp`` 同源，但抹掉全部 computed 标记。"""

    def list_entities(self) -> ToolResult:
        return ToolResult(
            ok=True, data={"entities": _strip_computed(_ENTITIES)}
        )

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


class _FakeErp:
    """最小假 ERP：list_entities 元数据 + 空查询页。"""

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": [dict(e) for e in _ENTITIES]})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


def _cache() -> EntityCache:
    return EntityCache(_FakeErp())


# ---------------------------------------------------------------------------
# 段解析工具
# ---------------------------------------------------------------------------


def _segments(section: str) -> tuple[str, str | None, str | None]:
    """按空行把目录段切成 (普通字段段, 计算列段|None, 仅筛选字段段|None)。

    每个子段自身不含空行，故 ``split("\\n\\n")`` 即得三段；段头用固定前缀
    识别（与既有 test_restricted_field_catalog 同口径）。
    """
    parts = section.split("\n\n")
    seg1 = parts[0]
    seg2 = next((p for p in parts if p.startswith("计算列（")), None)
    seg3 = next((p for p in parts if p.startswith("仅筛选字段（")), None)
    return seg1, seg2, seg3


def _field_name(token: str) -> str:
    """字段 token → 英文列名。目录里字段渲染成「列名（中文 label）」，
    取全角括号前一段（label 缺失时为裸列名，split 后仍是自身）。"""
    return token.split("（")[0].strip()


def _parse(section: str) -> tuple[dict[str, set], dict[str, set], dict[str, set]]:
    """解析成 (regular, computed, filter_only)：entity → 字段名集合。"""
    reg: dict[str, set] = {}
    comp: dict[str, set] = {}
    filt: dict[str, set] = {}
    cur: dict[str, set] | None = None
    for ln in section.split("\n"):
        if ln.startswith("实体字段目录（"):
            cur = reg
        elif ln.startswith("计算列（"):
            cur = comp
        elif ln.startswith("仅筛选字段（"):
            cur = filt
        elif cur is not None and "：" in ln:
            name, _, rest = ln.partition("：")
            cur[name.split("（")[0].strip()] = {
                _field_name(f) for f in rest.split(",") if f.strip()
            }
    return reg, comp, filt


# ---------------------------------------------------------------------------
# 1. 端到端：erp_calc 参数轮 prompt 确实带目录段（且位置在 schema 之前）
# ---------------------------------------------------------------------------


class _StubArgsClient:
    """最小参数客户端桩：只回 tool_name/tool_arguments，记录收到的 prompt。"""

    def __init__(self, tool_name: str = "erp_calc", arguments: dict | None = None) -> None:
        self._tool_name = tool_name
        self._arguments = (
            arguments
            if arguments is not None
            else {"entity": "powder_inventory", "measures": [{"op": "count"}]}
        )
        self.prompts: list[str] = []

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: object
    ) -> SimpleNamespace:
        self.prompts.append(system_prompt)
        return SimpleNamespace(tool_name=self._tool_name, tool_arguments=self._arguments)


def _run_args_node(tool_name: str) -> tuple[dict, _StubArgsClient]:
    client = _StubArgsClient(tool_name)
    runtime = RuntimeBag(
        client=client,
        tools={tool_name: TOOL_REGISTRY[tool_name]},
        metadata=_cache(),
    )
    state: dict = {
        "messages": [],
        "pending_action": {
            "reply_text": "算",
            "next_action": "call_tool",
            "tool_name": tool_name,
        },
    }
    return tool_args_node(state, runtime), client


def test_calc_parameter_prompt_carries_calc_catalog() -> None:
    """端到端：erp_calc 参数轮 prompt 含 erp_calc 目录段（真实渲染实体/字段名），
    且位置在结构化输出说明（schema）之前。"""
    out, client = _run_args_node("erp_calc")
    assert out["current_step"] == "tool_args_done"
    prompt = client.prompts[0]
    assert "实体字段目录（erp_calc 的" in prompt, "参数轮未注入 erp_calc 目录段"
    # 字段名/实体名由 EntityCache 动态渲染（非硬编码）；每字段带中文字段 label
    assert "customers（客户）：id（ID）, company_name（公司名称）" in prompt
    assert (
        "powder_inventory（塑粉库存色号）：id（ID）, color_code（色号）, "
        "warn_threshold（预警阈值）" in prompt
    )
    # 位置：目录段在 schema 之前
    assert prompt.index("实体字段目录（erp_calc 的") < prompt.index("结构化输出说明：")
    # 不误注入 erp_query 目录段（两段措辞必须区分）
    assert "erp_query 的 fields 字段名" not in prompt


def test_erp_query_parameter_prompt_has_no_calc_catalog() -> None:
    """反向：erp_query 参数轮不得出现 erp_calc 目录段（分支互斥）。"""
    _, client = _run_args_node("erp_query")
    prompt = client.prompts[0]
    assert "实体字段目录（erp_query 的" in prompt
    assert "实体字段目录（erp_calc 的" not in prompt


def test_non_calc_tool_parameter_prompt_has_no_calc_catalog() -> None:
    """反向：非 erp_query / erp_calc 的工具（这里取 finance_create）参数轮
    **不得**出现 erp_calc / erp_query 任一目录段。

    守护点：注入分支是 ``elif spec.name == "erp_calc":``——若被放宽成对
    所有工具注入（如 ``elif spec.name != "":``），仅靠只覆盖 erp_query 的
    现有反向用例测不出（erp_query 走 ``if`` 分支，永远碰不到这个 ``elif``）。
    finance_create 走的是 ``elif spec.name in ("finance_create", "finance_update")``
    分支，任何把非 calc 工具也塞进 calc 目录段的改动都会在此变红。
    """
    _, client = _run_args_node("finance_create")
    prompt = client.prompts[0]
    assert "实体字段目录（erp_calc 的" not in prompt, (
        "非 erp_calc 工具的参数轮被注入了 erp_calc 目录段（注入分支越界）：\n"
        + prompt
    )
    assert "实体字段目录（erp_query 的" not in prompt, (
        "finance_create 参数轮被注入了 erp_query 目录段（分支应互斥）"
    )


# ---------------------------------------------------------------------------
# 2. 三段存在 + 段 2 措辞（锚点与后果同「小句」）
# ---------------------------------------------------------------------------


def test_calc_catalog_has_three_sections() -> None:
    section = _erp_calc_catalog_section(_cache())
    assert section is not None
    seg1, seg2, seg3 = _segments(section)
    # 段 1：普通字段，说明用于 filter/group_by/measures（含 field/multiply_field）
    assert seg1.startswith("实体字段目录（erp_calc 的")
    assert "filter/group_by/measures" in seg1
    assert "multiply_field" in seg1
    assert (
        "powder_inventory（塑粉库存色号）：id（ID）, color_code（色号）, "
        "warn_threshold（预警阈值）" in seg1
    )
    # 段 2：计算列段真实渲染（字段同样带中文 label）
    assert seg2 is not None and (
        "order_total（订单总重量）, consumed_total（消耗总量）, stock（净库存）" in seg2
    )
    # 段 3：仅筛选字段段存在
    assert seg3 is not None and seg3.startswith("仅筛选字段（")


def test_calc_second_section_wording_pairs_anchor_and_consequence() -> None:
    """段 2 必须**同时**含「不可用于 filter」与「可用于 group_by/measures」，
    且各自后果与锚点落在**同一个以「；」切分的小句内**（防裸子串被邻近词救活）。"""
    section = _erp_calc_catalog_section(_cache())
    assert section is not None
    _, seg2, _ = _segments(section)
    assert seg2 is not None
    clauses = seg2.split("；")
    neg = next((c for c in clauses if "不可用于 erp_calc 的 filter" in c), None)
    assert neg is not None, f"段 2 缺「不可用于 filter」：{seg2!r}"
    assert "ERP 会拒绝" in neg, f"「不可用于 filter」的后果未落在同一小句：{neg!r}"
    pos = next((c for c in clauses if "可用于 group_by/measures" in c), None)
    assert pos is not None, f"段 2 缺「可用于 group_by/measures」：{seg2!r}"
    assert "本地" in pos, f"「可用于 group_by/measures」的理据未落在同一小句：{pos!r}"


def test_calc_omits_computed_section_when_no_computed_fields() -> None:
    """守护 ``if computed_lines:``：夹具**无任何计算列**时，不得出现计算列段头。

    复核实测：把 ``tool_args_node.py:201`` 的 ``if computed_lines:`` 改成
    ``if True:`` 会渲染出一个只有段头、无内容的空「计算列（…）」段——模型据
    此以为本批实体会给计算列、却什么都列不出，属提示词谎言。本用例用一份抹掉
    全部 ``computed`` 标记的实体集合驱动，把「有内容才渲染」钉住。
    """
    cache = EntityCache(_NoComputedErp())
    section = _erp_calc_catalog_section(cache)
    assert section is not None
    assert "计算列" not in section, (
        "无计算列时不得渲染计算列段（含空段头）：\n" + section
    )
    seg1, seg2, seg3 = _segments(section)
    assert seg2 is None
    # 对照：这份夹具确实渲染出了目录段与仅筛选段（非因整段降级为 None 而通过）
    assert seg1.startswith("实体字段目录（erp_calc 的")
    assert seg3 is not None
    assert (
        "customers：tax_id（税号）, bank_account（银行账号）, address（地址）" in seg3
    )


# ---------------------------------------------------------------------------
# 3. 段 3：段头**正向锁定**（恰好等于规范串） + 禁词（补充防线）
# ---------------------------------------------------------------------------

#: 段 3 段头的规范串（唯一真源：tool_args_node._erp_calc_catalog_section 第三段）。
#: 本段对敏感字段（个人联系方式/银行账号/税号）**只承诺 filter**，刻意不提
#: group_by/measure（那些路径已 fail closed，指路即诱试被拦通道）——所以这条
#: 段头就是该段**唯一的正向承诺**，必须被逐字钉死。
_THIRD_SECTION_HEADER = "仅筛选字段（ERP 中存在、可用于本工具的 filter 条件）："


def test_calc_third_section_header_equals_normative_line() -> None:
    """段 3 段头**恰好等于**规范串（正向锁定取代黑名单式「不含某几个词」）。

    复核实测的两种绕过在此都会变红：
    - 抹掉该段唯一的正向承诺（「filter 条件」→「筛选条件」）：旧写法
      ``"filter" in seg3`` 被夹具字段名 ``extra_filter_only`` 救活而恒真；
    - 换成中文同义虚假承诺（「分组/聚合条件」）：旧黑名单不含这些词、拦不住。
    锚点与后果绑在段头同一行，判定不跨 ``\\n``，裸子串与同义词都救不活。
    """
    section = _erp_calc_catalog_section(_cache())
    assert section is not None
    # 段头唯一（取段前提）：段 1 的从句用「」收尾，不带全角括号
    assert section.count("仅筛选字段（") == 1, section
    _, _, seg3 = _segments(section)
    assert seg3 is not None
    hdr = seg3.split("\n")[0]
    assert hdr == _THIRD_SECTION_HEADER, (
        "段 3 段头措辞被改动（该段唯一的正向承诺是「可用于本工具的 filter 条件」）——"
        f"实测 {hdr!r} != {_THIRD_SECTION_HEADER!r}"
    )


def test_calc_third_section_has_no_banned_words() -> None:
    """段 3 禁词（**补充**防线，非唯一防线）：不得指路被拦通道（erp_calc /
    group_by / measure），也不得说 erp_query 专属的「不回传」（calc 取数不经
    crop，照抄即说谎）。真正的正向承诺由上面那条段头精确锁钉住。"""
    section = _erp_calc_catalog_section(_cache())
    assert section is not None
    assert section.count("仅筛选字段（") == 1, section
    _, _, seg3 = _segments(section)
    assert seg3 is not None
    for banned in ("erp_calc", "group_by", "measure"):
        assert banned not in seg3, (
            f"「仅筛选字段」段不得出现 {banned!r}（勿给模型指一条被拦通道）：\n{seg3}"
        )
    assert "不回传" not in seg3, (
        "段 3 不得说「不回传」——那是 erp_query 的 crop 事实，erp_calc 取数"
        f"不经 crop，照抄即说谎：\n{seg3}"
    )


# ---------------------------------------------------------------------------
# 4. 两段目录：实体与字段集合逐一致（同一 partition_catalog_fields，防漂移）
# ---------------------------------------------------------------------------


def test_two_sections_cover_identical_entity_field_sets() -> None:
    """两段目录覆盖的实体与字段集合逐一致（同一 ``partition_catalog_fields``）。

    [2026-09-18 说明] 本用例两侧都把字段 token 剥掉 label 后缀（``_parse`` →
    ``_field_name``）后比**列名集合**，因此对「两段 label 渲染漂移」天然失明
    （非本批引入的回归）。label 层的逐字节一致由
    ``test_two_sections_render_identical_field_lines`` 覆盖，勿以为本用例守住了
    那一层。
    """
    query = _erp_query_catalog_section(_cache())
    calc = _erp_calc_catalog_section(_cache())
    assert query is not None and calc is not None
    q_reg, q_comp, q_filt = _parse(query)
    c_reg, c_comp, c_filt = _parse(calc)
    # 实体集合一致（且覆盖全部 QUERYABLE_ENTITIES）
    assert set(q_reg) == set(c_reg) == set(QUERYABLE_ENTITIES)
    # 逐实体、逐段字段集合必须逐一致
    for entity in set(q_reg) | set(c_reg):
        assert q_reg.get(entity) == c_reg.get(entity), (
            f"{entity} 普通字段段漂移：query={q_reg.get(entity)} calc={c_reg.get(entity)}"
        )
    assert set(q_comp) == set(c_comp)
    for entity in set(q_comp) | set(c_comp):
        assert q_comp.get(entity) == c_comp.get(entity), (
            f"{entity} 计算列段漂移：query={q_comp.get(entity)} calc={c_comp.get(entity)}"
        )
    assert set(q_filt) == set(c_filt)
    for entity in set(q_filt) | set(c_filt):
        assert q_filt.get(entity) == c_filt.get(entity), (
            f"{entity} 仅筛选段漂移：query={q_filt.get(entity)} calc={c_filt.get(entity)}"
        )
    # 三不字段（计算列 ∩ 白名单外）不入任何段
    assert "ghost_total" not in calc
    assert "extra_ghost" not in calc


# ---------------------------------------------------------------------------
# 5. 降级：None / get_entities 抛异常 / 无实体数据 → None，不抛
# ---------------------------------------------------------------------------


def test_calc_catalog_degrades_to_none() -> None:
    assert _erp_calc_catalog_section(None) is None

    class _Boom:
        def get_entities(self) -> list:
            raise RuntimeError("boom")

    assert _erp_calc_catalog_section(_Boom()) is None

    class _Empty:
        def get_entities(self) -> list:
            return []

    assert _erp_calc_catalog_section(_Empty()) is None


# ---------------------------------------------------------------------------
# 6. 字段中文 label：与列名同小句渲染 / 缺省不重复 / 两段同形
# ---------------------------------------------------------------------------


def _data_lines(section: str) -> set[str]:
    """目录段里的实体字段数据行（三段皆取）。

    实体行以列 0 的小写实体名开头（形如 ``name：…`` 或 ``name（实体中文）：…``），
    段头/说明行以中文或破折号开头，故用行首正则精确取数据行——用于逐行比对
    两段渲染是否同形（含每字段的中文 label）。
    """
    return {ln for ln in section.split("\n") if _DATA_LINE.match(ln)}


def _field_tokens(section: str) -> set[str]:
    """目录段里全部字段 token（``列名（中文 label）``，三段皆取）。

    从数据行的「：」之后按 ``", "`` 切分——与渲染时
    ``_render_catalog_fields`` 的拼接分隔符一致，故每个 token 恰是一个字段。
    用集合做**成员**判定（而非 ``token in section`` 的裸子串），不会被「以
    该 token 结尾的更长字段名」救活（见 test_field_label_anchor_in_same_token）。
    """
    toks: set[str] = set()
    for ln in _data_lines(section):
        rest = ln.partition("：")[2]
        toks.update(t for t in rest.split(", ") if t)
    return toks


def _fallback_cache() -> EntityCache:
    """customers：覆盖 label 三态——正常中文 label / label == 列名 / label 缺失。

    复用真实 ERP 缺省回退口径（registry._FIELD_LABELS 无命中即回退为列名）。
    """
    fields = {
        "id": {"type": "int", "label": "ID"},
        "company_name": {"type": "str", "label": "公司名称"},
        # label 与列名相同 → 不得渲染成 price_type（price_type）
        "price_type": {"type": "str", "label": "price_type"},
        # label 键缺失 → 不得渲染成 price_unit（price_unit）
        "price_unit": {"type": "str"},
        # 白名单外（仅筛选段）的 label 缺失
        "notes": {"type": "text", "label": "notes"},
    }
    raw = {
        "name": "customers", "label": "客户",
        "readable": True, "creatable": False, "updatable": False,
        "deletable": False, "business_operations": [], "fields": fields,
    }
    return EntityCache(_SingleEntityErp(raw))


class _SingleEntityErp:
    def __init__(self, raw: dict) -> None:
        self._raw = raw

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": [dict(self._raw)]})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


def test_field_label_anchor_in_same_token() -> None:
    """字段中文 label 必须与**其列名**在同一 token（``列名（中文）``）内。

    这是防「裸子串假锁」的核心断言：只断言 ``"单价" in section`` 会被任意
    邻近字段的中文名救活。

    [2026-09-18 reviewer] 判定必须是 **token 级**、不能停在 ``token in section``
    的裸子串——裸子串会被「以该 token 结尾的更长字段名」救活：例如将来某字段
    名以 ``id`` 结尾、其 label 恰为 ``ID``，就会渲染出 ``xxxid（ID）``，使
    ``"id（ID）" in section`` 恒真，而 ``id`` 自身其实漏了 label。故这里把目录
    段按数据行的「：」之后以 ``", "`` 切成字段 token 集合，再做**集合成员**判定。
    """
    section = _erp_query_catalog_section(_cache())
    assert section is not None
    tokens = _field_tokens(section)
    for token in (
        "id（ID）",
        "company_name（公司名称）",
        "unit_price（单价）",
        "price_type（价格类型）",
        "price_unit（计价单位）",
        # 计算列段：label 同样与列名同 token
        "order_total（订单总重量）",
        "consumed_total（消耗总量）",
        "stock（净库存）",
        # 仅筛选字段段：label 同样与列名同 token
        "tax_id（税号）",
        "bank_account（银行账号）",
    ):
        assert token in tokens, (
            f"目录段缺「列名（中文 label）」token：{token}\n"
            f"实际 token 集合：{sorted(tokens)}"
        )


def test_label_equal_to_column_name_not_duplicated() -> None:
    """label 等于列名（或缺省）时**只渲染列名**，不得出现 ``x（x）`` 噪音。"""
    section = _erp_query_catalog_section(_fallback_cache())
    assert section is not None
    # 正常中文 label 照常渲染（对照：非因整段降级而通过）
    assert "id（ID）" in section
    # label == 列名 / 缺失 → 只渲染列名
    assert "price_type（price_type）" not in section
    assert "price_unit（price_unit）" not in section
    assert "notes（notes）" not in section
    assert "price_unit（" not in section, "label 键缺失不得渲染空括号"
    # 且这些字段本身仍出现在目录里（不是被整行丢弃）
    assert "price_type" in section and "price_unit" in section


def test_two_sections_render_identical_field_lines() -> None:
    """两段（erp_query / erp_calc）的字段数据行**逐字节相同**——共用
    ``_render_catalog_fields`` 的唯一渲染口径，三段同形（含中文 label）。"""
    query = _erp_query_catalog_section(_cache())
    calc = _erp_calc_catalog_section(_cache())
    assert query is not None and calc is not None
    q_lines = _data_lines(query)
    c_lines = _data_lines(calc)
    assert q_lines == c_lines, (
        "两段字段数据行漂移（应共用 _render_catalog_fields）：\n"
        f"query-only={q_lines - c_lines}\ncalc-only={c_lines - q_lines}"
    )
    # 非空、且字段 token 确为「列名（中文 label）」形态——既防两侧同为空集，
    # 也防两段**同时退化成裸列名**：[2026-09-18 reviewer] 实体名自带
    # 「（客户）」括号，故 `"（" in ln` 在整行级恒真、抓不到字段 label 整体消失
    # （reviewer 变异：渲染函数恒返回裸列名 → 旧断言仍绿）。必须落到「：」之后
    # 的字段 token 上，按具体 token 判定。
    q_tokens = _field_tokens(query)
    assert {"id（ID）", "company_name（公司名称）"} <= q_tokens, (
        "字段数据行未渲染「列名（中文 label）」token（疑似两段同时退化成裸列名）："
        f"{sorted(q_tokens)}"
    )


# ---------------------------------------------------------------------------
# 7. erp_query 目录段字节基线（sha256 钉子）
#
# 本段 sha256 已**两次有意重采**（详见下方基线常量注释）：ar_settlements 实体
# 扩充、字段中文 label。除此之外任何字节漂移都必须变红。注意本仓库有「重采吞掉
# 意外改动」的前科——**见红先别急着重采**，先逐字节证明这次变动是忠实的有意改动
# （与上一版基线 diff 只应含预期的行），再更新基线常量。
# ---------------------------------------------------------------------------

#: 在上述夹具下 _erp_query_catalog_section 的 sha256 基线。
#: [2026-09-16 当期应收口径开放] 接入只读实体 ar_settlements 后按预期重采
#: （该段较旧基线只多出 ar_settlements 的普通字段行与仅筛选行，其余逐字节不变）。
#: [2026-09-18 字段中文 label] 字段渲染由裸列名改为「列名（中文 label）」（三段
#: 同形、与 list_entities 口径对齐）后**有意重采**——该段每字段追加了中文 label
#: （label 缺失/与列名相同者不加）。除这一处有意的字段渲染变更外，段头/三段措辞
#: 仍逐字节不变；任何其他漂移仍会变红。
_ERP_QUERY_SECTION_SHA256 = (
    "32fc88acbf6b5832770ceaffd665987ac3cbad2fc6387979924260e388874454"
)


def test_erp_query_section_byte_identical() -> None:
    section = _erp_query_catalog_section(_cache())
    assert section is not None
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    assert digest == _ERP_QUERY_SECTION_SHA256, (
        "erp_query 目录段输出发生字节级变化（本段基线已**两次有意重采**："
        "ar_settlements 实体扩充、字段中文 label；除这两次外本段不应改动）。"
        "见红先别急着重采——先逐字节证明本次变动是忠实的有意改动，再更新基线："
        f"{digest} != {_ERP_QUERY_SECTION_SHA256}"
    )
