"""[2026-09-15 方案 A] 字段目录「仅筛选段」与白名单外字段口径守护。

背景：模型「能查什么字段」（字段目录）与「结果里真能拿到什么」（crop 白名单
``QUERY_ITEM_WHITELIST``）是两个互不校验的真源——ERP 元数据里有、白名单里没有
的字段，模型在目录里看得见 → 写进 ``fields`` → 值被 ``crop_items`` 静默裁掉
（无报错无说明）→ 模型误以为空值反复重试烧决策轮。

方案 A：不隐藏字段，改为**打标 + 明确告知**——把白名单外字段单列
「仅筛选字段」段，明说「取值不回传、勿写进 fields」（但仍可 filter/order_by）。
注：这些字段的取值在 erp_calc 的 group_by/measure 路径上已 **fail closed**
（calc 取数不过 crop；数据源 = ERP 元数据的 ``sensitive`` 键），但目录仍**刻意
不宣传**该路径——见 ``crop.py`` 顶部注释（含个人联系方式/银行账号/税号，
redaction 不覆盖）。
本模块钉住：

1. 目录里出现的字段必满足「可回传」或「被标为仅筛选」二者之一——不存在
   「三不」字段（既不可取回、又不可筛选），含「白名单外 ∩ 计算列」边界；
2. 未登记实体（``returnable_fields`` → None）→ 目录不加标记、``query`` 不
   剔除、``crop_items`` 原样返回，三处口径一致；
3. ``query.py``：白名单外字段进 ``fields`` → 剔除 + note 措辞与「未知字段」区分；
   进 ``filter`` → 原样保留；
4. 两处目录渲染（``tool_args_node`` 与 ``catalog``）对同一实体算出**同一组**
   仅筛选字段（防漂移）；
5. 「仅筛选字段」段（第三段）与 ``catalog`` note 的 filter_only 句**不得**出现
   ``erp_calc``/``group_by``/``measure``（防后人把绕过裁剪的通道加回目录）。

假 ERP：customers（元数据含白名单内 / 白名单外 / 计算列三态）+ 空查询页；
另有一组含「白名单内计算列」的真实形态元数据（powder_inventory），用于让
计算列段**真实渲染**、证明取段正确（见文末）。
"""

from __future__ import annotations

import administrative_agent.agent.tools.crop as crop_mod
import pytest

from administrative_agent.agent.nodes.tool_args_node import _erp_query_catalog_section
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.catalog import ListEntitiesArgs, run_list_entities
from administrative_agent.agent.tools.crop import (
    QUERY_ITEM_WHITELIST,
    is_returnable,
    returnable_fields,
)
from administrative_agent.agent.tools.query import ErpQueryArgs, run_erp_query
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES


def _field(name: str, ftype: str = "str", **extra) -> dict:
    return {"type": ftype, "label": name, **extra}


#: customers：白名单 = id/company_name/unit_price/price_type/price_unit
#: 额外字段三态——tax_id/bank_account/address（白名单外，仅筛选）；
#: ghost_total（计算列 ∩ 白名单外 = 三不，不可 filter/order_by、也不可 fields）
_CUSTOMERS_FIELDS = {
    "id": _field("ID", "int"),
    "company_name": _field("公司名称"),
    "unit_price": _field("单价", "decimal"),
    "price_type": _field("价格类型"),
    "price_unit": _field("计价单位"),
    # 白名单外（非计算列）→ 仅筛选字段
    "tax_id": _field("税号"),
    "bank_account": _field("银行账号"),
    "address": _field("地址"),
    # 计算列 ∩ 白名单外 → 三不字段（不入目录任何一段）
    "ghost_total": _field("幽灵合计", "decimal", computed=True),
}

_CUSTOMERS_FILTER_ONLY = {"tax_id", "bank_account", "address"}

#: 其余实体的合成最小元数据：每实体给三态字段（可回传 id / 白名单外非计算列
#: extra_filter_only / 计算列 ∩ 白名单外 extra_ghost）——供「两处渲染同口径」
#: 参数化到全部 14 个 QUERYABLE_ENTITIES，防单实体漂移漏网。
def _synthetic_entity(name: str) -> dict:
    return {
        "name": name,
        "label": name,
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),  # 各实体白名单均含 id → 可回传
            "extra_filter_only": _field("额外可筛选"),  # 白名单外 → 仅筛选
            "extra_ghost": _field("幽灵", "decimal", computed=True),  # 三不
        },
    }


def _make_entities() -> list[dict]:
    """全部 QUERYABLE_ENTITIES 的假元数据：customers 用富字段，其余合成。"""
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
        }
    ]
    for name in sorted(QUERYABLE_ENTITIES):
        if name != "customers":
            out.append(_synthetic_entity(name))
    return out


_ENTITIES: list[dict] = _make_entities()

#: 各实体期望的「仅筛选字段」集合（参数化防漂移测试的判据）
_FILTER_ONLY_EXPECTED: dict[str, set] = {"customers": _CUSTOMERS_FILTER_ONLY}
for _name in sorted(QUERYABLE_ENTITIES):
    _FILTER_ONLY_EXPECTED.setdefault(_name, {"extra_filter_only"})


#: 真实形态元数据：powder_inventory 的 order_total/consumed_total/stock 是
#: **白名单内计算列** → 目录第二段（计算列段）真实渲染并含 ``erp_calc`` 字样；
#: notes 为白名单外非计算列 → 第三段（仅筛选字段段）也渲染。用于证明「取段」
#: 正确（reviewer 用真实 ERP 元数据渲染同一函数即此形态）。
_REAL_METADATA_ENTITIES: list[dict] = [
    {
        "name": "powder_inventory",
        "label": "塑粉库存色号",
        "readable": True,
        "creatable": False,
        "updatable": False,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": _field("ID", "int"),
            "color_code": _field("色号"),
            "warn_threshold": _field("预警阈值", "decimal"),
            "notes": _field("备注"),  # 白名单外非计算列 → 仅筛选字段
            # 白名单内计算列 → 计算列段（含 erp_calc 字样）
            "order_total": _field("订单总重量", "decimal", computed=True),
            "consumed_total": _field("消耗总量", "decimal", computed=True),
            "stock": _field("净库存", "decimal", computed=True),
        },
    }
]


class _FakeErp:
    """最小假 ERP：list_entities 元数据 + query（记录下发参数）。

    ``entities`` 缺省用 ``_ENTITIES``（含 customers 富字段 + 合成三态实体）；
    可传 ``_REAL_METADATA_ENTITIES`` 让计算列段真实渲染。
    """

    def __init__(self, entities: list[dict] | None = None) -> None:
        self._entities = _ENTITIES if entities is None else entities
        self.query_arguments: list[dict] = []

    def list_entities(self) -> ToolResult:
        return ToolResult(ok=True, data={"entities": [dict(e) for e in self._entities]})

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> ToolResult:
        if tool_name == "list_entities":
            return self.list_entities()
        if tool_name == "query":
            self.query_arguments.append(dict(arguments or {}))
            return ToolResult(ok=True, data={"items": [], "total": 0})
        raise AssertionError(f"unexpected tool: {tool_name}")


def _ctx(erp: _FakeErp) -> ToolContext:
    return ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}),
        ),
        tasks=None,
        audit=None,
        sink=None,
        client=erp,
        metadata=EntityCache(erp),
    )


def _field_name(token: str) -> str:
    """字段 token → 英文列名（目录里字段渲染成「列名（中文 label）」，取全角
    括号前一段；label 缺失时为裸列名，split 后仍是自身）。"""
    return token.split("（")[0].strip()


def _parse_section(section: str) -> tuple[dict[str, set], dict[str, set], dict[str, set]]:
    """把 tool_args_node 目录段解析成 (regular, computed, filter_only)：
    entity → 字段名集合。三段的段头行以固定前缀识别；段内实体行形如
    「name（实体中文）：列名（字段中文）, …」或「name：列名（字段中文）, …」。"""
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
            # 普通段实体行形如「customers（客户）：…」——去掉实体中文 label 后缀
            cur[name.split("（")[0].strip()] = {
                _field_name(f) for f in rest.split(",") if f.strip()
            }
    return reg, comp, filt


def _catalog_item(erp: _FakeErp, entity: str) -> dict:
    """紧凑目录（entity=None）中取该实体条目——fields 为列表形态。"""
    result = run_list_entities(_ctx(erp), ListEntitiesArgs())
    assert result.ok is True, result.summary
    for item in result.summary["items"]:
        if item.get("name") == entity:
            return item
    raise AssertionError(f"目录缺实体 {entity}：{result.summary['items']}")


# ---------------------------------------------------------------------------
# 1. 不存在「三不」字段；白名单外非计算列进仅筛选段
# ---------------------------------------------------------------------------


def test_no_unreachable_field_in_catalog_section() -> None:
    """目录段出现的任何字段必满足「可回传」或「仅筛选」；三不字段不出现。"""
    section = _erp_query_catalog_section(EntityCache(_FakeErp()))
    assert section is not None
    reg, comp, filt = _parse_section(section)
    # 三不字段（计算列 ∩ 白名单外）不得出现在任何一段
    for ghost in ("ghost_total", "extra_ghost"):
        assert ghost not in section, f"三不字段 {ghost} 不得出现在目录任何一段"
    # 逐字段校验：可回传 或 被标为仅筛选（覆盖全部实体）
    for buckets in (reg, comp):
        for name, fields in buckets.items():
            for f in fields:
                assert is_returnable(name, f), (
                    f"目录段出现不可回传字段且未标为仅筛选：{name}.{f}"
                )
    # 白名单外非计算列 → 出现在仅筛选段（逐实体同判据）
    for entity, expected in _FILTER_ONLY_EXPECTED.items():
        assert filt.get(entity) == expected, f"{entity}: {filt.get(entity)}"
    # 白名单内字段仍在普通段
    assert {"id", "company_name", "unit_price", "price_type", "price_unit"} <= reg["customers"]


def test_catalog_tool_marks_filter_only_fields() -> None:
    """catalog 工具：fields 只含可回传字段；filter_only_fields 列白名单外字段；
    三不字段不出现。"""
    item = _catalog_item(_FakeErp(), "customers")
    names = {f["name"] for f in item["fields"]}
    assert "ghost_total" not in names, "三不字段不得出现在 fields"
    assert set(item["filter_only_fields"]) == _FILTER_ONLY_EXPECTED["customers"], item
    assert "tax_id" not in names, "仅筛选字段不应混进 fields（可回传字段列表）"


# ---------------------------------------------------------------------------
# 2. 未登记实体：三处口径一致（目录不标记 / query 不剔除 / crop 原样）
# ---------------------------------------------------------------------------


def test_unregistered_entity_consistent_across_three_sites(monkeypatch) -> None:
    monkeypatch.delitem(QUERY_ITEM_WHITELIST, "customers")
    # 单源 helper：未登记 → None / 恒 True
    assert returnable_fields("customers") is None
    assert is_returnable("customers", "tax_id") is True

    # ① 目录段：该实体不加区分（customers 不出现在「仅筛选字段」段，
    #    其余已登记实体不受影响；故只看 customers 是否被标记）
    section = _erp_query_catalog_section(EntityCache(_FakeErp()))
    assert section is not None
    _, _, filt = _parse_section(section)
    assert "customers" not in filt, "未登记实体不得被标为仅筛选"
    assert "tax_id" in section  # 未登记实体全部字段进普通段（与今天一致）

    # ① catalog 工具：无 filter_only_fields 键
    item = _catalog_item(_FakeErp(), "customers")
    assert "filter_only_fields" not in item
    assert "tax_id" in {f["name"] for f in item["fields"]}

    # ② query 不剔除（fields 原样下发）
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp), ErpQueryArgs(entity="customers", fields=["tax_id", "address"])
    )
    assert result.ok is True, result.summary
    assert erp.query_arguments[0]["fields"] == ["tax_id", "address"]
    assert "note" not in result.summary

    # ③ crop_items 原样返回
    rows = [{"id": 1, "tax_id": "9132", "ghost_total": 5}]
    assert crop_mod.crop_items(rows, "customers") == rows


# ---------------------------------------------------------------------------
# 3. query.py：白名单外字段剔除（措辞区分）/ filter 原样保留
# ---------------------------------------------------------------------------


def test_query_evicts_non_returnable_field_with_distinct_note() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp), ErpQueryArgs(entity="customers", fields=["tax_id"])
    )
    assert result.ok is True, result.summary
    # 剔除后为空 → 不传 fields
    assert "fields" not in erp.query_arguments[0]
    note = result.summary.get("note", "")
    assert "不参与结果回传" in note, f"note 未点明不可回传：{note!r}"
    assert "未知字段" not in note, "存在字段不得报成『未知字段』"


def test_query_keeps_returnable_drops_non_returnable() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(entity="customers", fields=["tax_id", "company_name"]),
    )
    assert result.ok is True, result.summary
    assert erp.query_arguments[0]["fields"] == ["company_name"]


def test_query_distinguishes_unknown_from_non_returnable() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(entity="customers", fields=["totally_made_up", "tax_id"]),
    )
    assert result.ok is True, result.summary
    note = result.summary.get("note", "")
    assert "未知字段" in note and "totally_made_up" in note
    assert "不参与结果回传" in note and "tax_id" in note


def test_query_filter_keeps_non_returnable_field() -> None:
    erp = _FakeErp()
    result = run_erp_query(
        _ctx(erp),
        ErpQueryArgs(
            entity="customers",
            filter={"tax_id": "9132", "company_name": "A"},
        ),
    )
    assert result.ok is True, result.summary
    assert erp.query_arguments[0]["filter"] == {
        "tax_id": "9132",
        "company_name": "A",
    }


# ---------------------------------------------------------------------------
# 4. 两处目录渲染：仅筛选字段集合一致（参数化到全部 14 个可查实体，防单实体漂移）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", sorted(QUERYABLE_ENTITIES))
def test_two_catalog_renders_agree_on_filter_only_fields(entity: str) -> None:
    section = _erp_query_catalog_section(EntityCache(_FakeErp()))
    assert section is not None
    _, _, filt = _parse_section(section)
    from_section = filt.get(entity, set())

    item = _catalog_item(_FakeErp(), entity)
    from_tool = set(item.get("filter_only_fields") or [])

    assert from_section == from_tool == _FILTER_ONLY_EXPECTED[entity], (
        f"{entity} 两处渲染仅筛选字段漂移：section={from_section} tool={from_tool}"
    )


# ---------------------------------------------------------------------------
# 5. 第三段措辞不得给模型指一条绕过裁剪的通道（reviewer 口径裁定）
# ---------------------------------------------------------------------------


def _third_section(section: str) -> str:
    """取「仅筛选字段」第三段原文。

    关键：用段头 ``仅筛选字段（``（全角括号）定位，**不能**用裸 ``仅筛选字段``
    ——第一段段头里含从句「…还可用下方「仅筛选字段」段」（用 ``」`` 收尾），
    ``section.index("仅筛选字段")`` 会命中那里、把第一段 + 第二段（计算列段，
    **必然**含 ``erp_calc``）一起切进来 → 禁词断言在真实元数据下误红。
    ``仅筛选字段（`` 全串唯一（先由调用方 ``assert count == 1`` 钉住）。
    """
    idx = section.index("仅筛选字段（")
    return section[idx:]


#: 两套元数据：默认（无计算列段）+ 真实形态（powder_inventory 白名单内计算列，
#: 计算列段真实渲染）——后者专证取段正确。
_METADATA_CASES: dict[str, list[dict] | None] = {
    "default": None,
    "real_metadata": _REAL_METADATA_ENTITIES,
}


@pytest.mark.parametrize("case", sorted(_METADATA_CASES))
def test_filter_only_section_does_not_advertise_calc_path(case: str) -> None:
    """「仅筛选字段」段（第三段）不得出现 erp_calc/group_by/measure 字样。

    背景（reviewer 裁定）：白名单外字段含个人联系方式/银行账号/税号，redaction
    不覆盖它们——其中被 ERP 标为 ``sensitive`` 的字段在 erp_calc 的 group_by/
    measure 上**已被 fail closed**（calc.py），但目录仍**刻意不宣传**该路径
    （在提示词里宣传等于给模型指一条通道）。故目录刻意只提「取值不回传 / 勿写进
    fields / 可作 filter/order_by」，不得出现下面三个词（防后人「顺手补全」又加
    回去）。（非敏感的仅筛选字段如 ``customers.address`` 在 calc 上**仍可用**——
    本用例只钉住目录渲染措辞，不改变这一事实。）

    参数化含 ``real_metadata``（计算列段真实渲染）——旧取段方式在该输入下会
    把计算列段切进来而误红，本用例证明新取段正确。
    """
    entities = _METADATA_CASES[case]
    section = _erp_query_catalog_section(EntityCache(_FakeErp(entities)))
    assert section is not None
    # 段头唯一性（新取段的前提）
    assert section.count("仅筛选字段（") == 1, section
    third = _third_section(section)
    for banned in ("erp_calc", "group_by", "measure"):
        assert banned not in third, (
            f"「仅筛选字段」段不得出现 {banned!r}（勿给模型指绕过裁剪的通道）：\n{third}"
        )
    # 三条必须给到的信息都在
    assert "filter/order_by" in third
    assert "不回传" in third
    assert "fields" in third


def test_real_metadata_slice_evidence_old_slice_would_misfire() -> None:
    """真实形态元数据下的取段证据：计算列段确实渲染，旧取段方式会误红。

    构造 powder_inventory（白名单内计算列 + 白名单外 notes）使**第二段真实
    渲染**，断言：① 计算列段存在且含 erp_calc；② 旧取段
    ``section[section.index("仅筛选字段"):]`` 会包含该段（即会误报「第三段出现
    erp_calc」）；③ 新取段 ``_third_section`` 不含禁词。把「修好了」变成可复现
    证据，而非依赖夹具里「无计算列段」的巧合。
    """
    section = _erp_query_catalog_section(EntityCache(_FakeErp(_REAL_METADATA_ENTITIES)))
    assert section is not None
    # ① 计算列段真实渲染
    assert "计算列（" in section
    assert "erp_calc" in section
    # ② 旧取段（裸「仅筛选字段」命中第一行从句）会把计算列段切进来 → 误红
    old_third = section[section.index("仅筛选字段"):]
    assert "erp_calc" in old_third, "旧取段方式应包含计算列段（复现误红场景）"
    # ③ 新取段只看第三段 → 无禁词
    third = _third_section(section)
    for banned in ("erp_calc", "group_by", "measure"):
        assert banned not in third, third


def test_catalog_note_filter_only_sentence_does_not_advertise_calc_path() -> None:
    """``catalog`` 工具 note 的 filter_only 句不得出现 erp_calc/group_by/measure。

    note 是**面向模型**的文案；filter_only 那句此前无守护（加回禁词全绿）。
    断言精确落在该句（句边界定位），**不**用全文 ``not in``——note 里既有的
    「写 erp_query/erp_calc 的 fields/filter/measures 参数前…」（工具级总述）与
    「目录可能含 erp_query/erp_calc 白名单外实体」（实体级）不在禁令范围。
    """
    result = run_list_entities(_ctx(_FakeErp()), ListEntitiesArgs())
    assert result.ok is True, result.summary
    note = result.summary["note"]
    start = note.index("条目内 filter_only_fields")
    end = note.index("；", start)
    seg = note[start:end]
    for banned in ("erp_calc", "group_by", "measure"):
        assert banned not in seg, (
            f"catalog note 的 filter_only 句不得出现 {banned!r}：{seg!r}"
        )
    # 三条信息齐备（防止靠删句子蒙混过关）
    assert "回传" in seg and "fields" in seg and "filter/order_by" in seg

