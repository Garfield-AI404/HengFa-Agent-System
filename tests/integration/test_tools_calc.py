"""erp_calc（一体式「查+算」）测试（[2026-09-09 架构改造第一步]）。

覆盖：pydantic 参数校验（op 枚举/field 约束/group 与聚合分离）、权限与
接线 fail closed、实体白名单与元数据未知拒绝、字段目录未知拒绝、filter
未知键宽容剔除、Decimal 精确聚合（sum/avg 量化/min/max/count）、
multiply_field 行级乘积、0/空/非数值行跳过语义（含 measure 独立性）、
单位枚举字段按单位分桶（绝不静默混加）、无单位字段实体单桶、分组排序与
结构（不含明细行）、分页去重、取数上限 fail closed、空结果语义、
divide_by 常量除法（先聚合后除一次、含除法结果量化 4 位、参数拒绝面）、顶层 totals
合计（单位分桶不跨桶、仅 sum/count、over 存在时不重复给）、时间粒度
time_granularity（C：按月/季桶键截断、跨年边界、非日期字段 fail closed）、
排序取前 N order_by + limit（D：升降序/tie/空值置后/截断 note/totals 不受影响/
计算列 Top-N 的 id76 回归钉子）。

假 ERP（CalcErp）：真分页（offset/limit 窗口 + 可注入跨页漂移重复 +
字段投影），对齐 test_scenarios_14_3.PagedQueryErp 形态；list_entities
元数据含 weight_unit 枚举（kg/m²）。D5：测试夹具，不代表 ERP 真实运行态。
与真实 ERP 的数值验收（8 月钣金 136012.9 kg/129 行/按日分组）见仓库外
验收脚本 _verify_erp_calc.py（主代理报告的「真实 ERP 集成形态」）。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.calc import (
    ErpCalcArgs,
    MeasureSpec,
    run_erp_calc,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.metadata import EntityCache

#: 假 ERP 实体目录：metal_orders（weight_unit 枚举 kg/m²）与 transactions
#: （无单位枚举字段）。字段 raw 结构与 mcp-api.md §3 一致。
_CALC_ENTITIES: list[dict] = [
    {
        "name": "metal_orders",
        "label": "钣金订单",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "model": {"type": "string", "label": "型号"},
            "in_date": {"type": "date", "label": "进厂日期"},
            "weight_unit": {
                "type": "str", "label": "重量单位",
                "required": True, "enum_values": ["kg", "m²"],
            },
            "weight_per_unit": {"type": "decimal", "label": "单重"},
            "quantity": {"type": "int", "label": "数量"},
            "total_amount": {"type": "decimal", "label": "金额"},
            "customer": {"type": "string", "label": "客户"},
        },
    },
    {
        "name": "transactions",
        "label": "收支记录",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "amount": {"type": "decimal", "label": "金额"},
            "note": {"type": "string", "label": "备注"},
        },
    },
]


class CalcErp:
    """erp_calc 假 ERP：真分页 query（fields 投影、可注入跨页重复/失败）+

    list_entities 元数据。filter 不做本地模拟——工具是否把筛选转发给
    ERP 由 calls 记录断言（真实过滤语义属于 ERP，见 mcp-api.md §4）。
    """

    def __init__(
        self,
        rows_by_entity: dict[str, list[dict]],
        *,
        entities: list[dict] | None = None,
        page_size: int = 200,
        drift_repeat: int = 0,
        fail_query: bool = False,
        fail_list: bool = False,
        total_override: int | None = None,
    ):
        self.rows_by_entity = rows_by_entity
        self.entities = list(entities) if entities is not None else _CALC_ENTITIES
        self.page_size = page_size
        self.drift_repeat = drift_repeat  # 每页开头重复上一窗口尾部行数
        self.fail_query = fail_query
        self.fail_list = fail_list
        #: total 契约覆盖（模拟并发增行：ERP 声明 total 大于实际可取行）
        self.total_override = total_override
        self.calls: list[dict] = []

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            if self.fail_list:
                return ToolResult(
                    ok=False,
                    error=MCPError(
                        code="MCP_METADATA_UNAVAILABLE",
                        category=ErrorCategory.UNKNOWN,
                        message="catalog unavailable",
                    ),
                )
            return ToolResult(ok=True, data={"entities": self.entities})
        if tool_name == "query":
            if self.fail_query:
                return ToolResult(
                    ok=False,
                    error=MCPError(
                        code="MCP_SERVER_ERROR", category=ErrorCategory.SERVER,
                        message="query exploded",
                    ),
                )
            args = arguments or {}
            entity = args.get("entity", "")
            offset = args.get("offset") or 0
            window = args.get("limit") or 200
            fields = args.get("fields")
            full = list(self.rows_by_entity.get(entity, []))
            if offset > 0 and self.drift_repeat > 0:
                start = max(0, offset - self.drift_repeat)
                page_rows = full[start : offset + window]
            else:
                page_rows = full[offset : offset + window]
            items = [
                {k: r[k] for k in fields if k in r} if fields else dict(r)
                for r in page_rows
            ]
            total = self.total_override if self.total_override is not None else len(full)
            return ToolResult(
                ok=True,
                data={"items": items, "total": total,
                      "limit": window, "offset": offset},
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(self, operation_id: str | None = None,
                      trace_id: str | None = None) -> ToolResult:
        """EntityCache 刷新入口（同 FakeMCPClient 形态：委托 call_tool）。"""
        return self.call_tool("list_entities", {})


def _metal(**kw) -> dict:
    """一行钣金订单（id 必填；缺省 kg 单位）。"""
    base = {"id": 0, "model": "M", "in_date": "2026-08-01",
            "weight_unit": "kg", "weight_per_unit": "1.0000", "quantity": 1}
    base.update(kw)
    return base


def _ctx(erp: CalcErp | None, *, role: str = "manager",
         scope: frozenset[str] = frozenset({"erp:query"}),
         with_context: bool = True,
         with_metadata: bool = True) -> ToolContext:
    """erp_calc 的工具上下文（同场景 12 _query_ctx 形态；权限门之后还有
    「元数据接线」与「实体白名单」两道，正向对照必须接上 EntityCache）。"""
    return ToolContext(
        context=(
            AgentContext(user_id="manager01", user_role=role, scope=scope)
            if with_context
            else None
        ),
        tasks=None, audit=None, sink=None, client=erp,
        metadata=EntityCache(erp) if (erp is not None and with_metadata) else None,
    )


# ---------------------------------------------------------------------------
# pydantic 参数校验（确定性兜底）
# ---------------------------------------------------------------------------


def test_args_schema_validation() -> None:
    """op 枚举 / count 约束 / field 约束 / 组维与聚合分离 / 必填项。"""
    with pytest.raises(ValidationError):
        MeasureSpec(field="weight_per_unit", op="total")  # op 不在枚举
    with pytest.raises(ValidationError):
        MeasureSpec(field=None, op="sum")  # 非 count 必须带 field
    with pytest.raises(ValidationError):
        MeasureSpec(op="count", multiply_field="quantity")  # count 无乘数
    with pytest.raises(ValidationError):
        MeasureSpec(field="a", op="sum", multiply_field="a")  # 字段与乘数字段相同
    with pytest.raises(ValidationError):
        MeasureSpec(op="count", multiply_field="x", field=None)  # 仍拒绝乘数
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders")  # measures 至少 1 条
    with pytest.raises(ValidationError):
        ErpCalcArgs(measures=[{"op": "count"}])  # entity 必填
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", measures=[])  # 空 measures 列表拒绝
    # count 允许 field 为空；sum 必须有 field
    assert MeasureSpec(op="count").field is None
    assert MeasureSpec(field="w", op="count").op.value == "count"
    # 组维与聚合分离：group_by 字段不得同时作为非 count 聚合 field
    with pytest.raises(ValidationError):
        ErpCalcArgs(
            entity="metal_orders",
            group_by="in_date",
            measures=[{"field": "in_date", "op": "sum"}],
        )
    # count 以 group_by 字段为对象则允许（计数整行不构成维/度混淆）
    ok = ErpCalcArgs(
        entity="metal_orders",
        group_by="in_date",
        measures=[{"field": "in_date", "op": "count"}],
    )
    assert ok.measures[0].op.value == "count"


# ---------------------------------------------------------------------------
# 权限与接线 fail closed（对齐场景 12/13 护栏语义）
# ---------------------------------------------------------------------------


def test_permission_gates_fail_closed_no_erp_call() -> None:
    """非 manager / 缺 erp:query / 无上下文一律拒绝且不触达 ERP。"""
    for kwargs in (dict(role="developer"), dict(scope=frozenset()),
                   dict(with_context=False)):
        erp = CalcErp({"metal_orders": []})
        result = run_erp_calc(
            _ctx(erp, **kwargs),
            ErpCalcArgs(entity="metal_orders",
                        measures=[{"op": "count"}]),
        )
        assert result.ok is False, kwargs
        assert "权限不足" in result.summary["error"]
        assert erp.calls == []  # 关键：不触达 ERP


def test_wiring_missing_fail_closed() -> None:
    """client/metadata 未接线：明确报错不静默。"""
    no_client = run_erp_calc(
        _ctx(None),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert no_client.ok is False
    assert "客户端未接线" in no_client.summary["error"]

    erp = CalcErp({"metal_orders": []})
    no_metadata = run_erp_calc(
        _ctx(erp, with_metadata=False),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert no_metadata.ok is False
    assert "元数据未接线" in no_metadata.summary["error"]
    assert erp.calls == []  # 元数据接线门在取数之前


def test_entity_whitelist_and_metadata_unknown() -> None:
    """实体白名单外拒绝（与 erp_query 同一开放面）；白名单内但目录缺失
    （scope 不可见/元数据未含）同样拒绝——字段目录未知不计算。"""
    # 目录可见但不在查询白名单（能力直挂不等于可查）
    # [2026-09-12 只读扩展] 原取 attendance——已开只读，改取 users。
    erp = CalcErp({}, entities=[{"name": "users", "fields": {}}])
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="users", measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "白名单" in result.summary["error"]
    assert erp.calls == []

    # 白名单内但假 ERP 目录未提供（模拟不可见/不存在）
    erp2 = CalcErp({"suppliers": []}, entities=[e for e in _CALC_ENTITIES])
    result2 = run_erp_calc(
        _ctx(erp2),
        ErpCalcArgs(entity="suppliers", measures=[{"op": "count"}]),
    )
    assert result2.ok is False
    assert "不在元数据" in result2.summary["error"]


def test_metadata_fetch_failure_fail_closed() -> None:
    """list_entities 失败（无旧缓存）→ 元数据不可用，拒绝计算。"""
    erp = CalcErp({"metal_orders": []}, fail_list=True)
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "元数据不可用" in result.summary["error"]
    # 取数调用没发生（卡在元数据门）
    assert [c["tool"] for c in erp.calls] == ["list_entities"]


# ---------------------------------------------------------------------------
# 字段目录确定性校验（聚合语义硬性要求：未知字段拒绝，不宽容剔除）
# ---------------------------------------------------------------------------


def test_unknown_measure_and_group_field_fail_closed() -> None:
    """measure field/multiply_field/group_by 不在实体字段目录 → 拒绝并附
    可用字段清单（宽松剔除会让模型以为算了它没算的 measure）。"""
    erp = CalcErp({"metal_orders": [_metal(id=1)]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[{"field": "weightt_per_unit", "op": "sum"}],
        ),
    )
    assert result.ok is False
    assert "不在实体字段目录" in result.summary["error"]
    assert "weight_per_unit" in result.summary["error"]  # 附可用字段
    assert [c["tool"] for c in erp.calls] == ["list_entities"]  # 卡在字段门，未取数

    erp2 = CalcErp({"metal_orders": [_metal(id=1)]})
    result2 = run_erp_calc(
        _ctx(erp2),
        ErpCalcArgs(
            entity="metal_orders",
            group_by="in_dat",
            measures=[{"op": "count"}],
        ),
    )
    assert result2.ok is False
    assert "group_by" in result2.summary["error"]
    assert [c["tool"] for c in erp2.calls] == ["list_entities"]


def test_filter_unknown_key_tolerated_with_note() -> None:
    """filter 未知键宽容剔除（对齐 erp_query）+ note 反馈；已知键原样转发。"""
    erp = CalcErp({"metal_orders": [_metal(id=1), _metal(id=2)]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg", "no_such_key": 1},
            measures=[{"op": "count"}],
        ),
    )
    assert result.ok
    assert "已忽略未知筛选键" in result.summary["note"]
    assert "no_such_key" in result.summary["note"]
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert len(query_calls) == 1
    # 转发给 ERP 的 filter 只剩已知键（原样值）
    assert query_calls[0]["arguments"]["filter"] == {"weight_unit": "kg"}


# ---------------------------------------------------------------------------
# 数值聚合正确性（Decimal 精确；结果结构不含明细行）
# ---------------------------------------------------------------------------


def test_sum_decimal_exactness_no_float() -> None:
    """Decimal 字符串聚合精确性：0.1+0.2=0.3（float 会得
    0.30000000000000004）；value 为字符串，可精确 Decimal 回读。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="0.1"),
        _metal(id=2, weight_per_unit="0.2"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"field": "weight_per_unit", "op": "sum"}]),
    )
    assert result.ok
    item = result.summary["items"][0]
    m = item["measures"][0]
    assert m["value"] == "0.3"
    assert Decimal(m["value"]) == Decimal("0.3")
    assert m["n"] == 2
    assert m["alias"] == "sum(weight_per_unit)"  # 缺省别名 op(field)
    assert m["field"] == "weight_per_unit"


def test_multiply_field_row_product() -> None:
    """multiply_field：每行 field×multiply_field 后再聚合（整单重量口径：
    weight_per_unit×quantity；8 月 SQL 真值 136012.9 即此形态）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="10.0000", quantity=2),   # 20
        _metal(id=2, weight_per_unit="1.5000", quantity=100),  # 150
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "整单重量"}],
        ),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "170"  # 20 + 150
    assert m["n"] == 2
    assert m["multiply_field"] == "quantity"
    # 取数字段由代码确定且恒含 id（跨页去重 key；防去重塌缩 bug 形态）
    query_args = [c for c in erp.calls if c["tool"] == "query"][0]["arguments"]
    assert "id" in query_args["fields"]
    assert "quantity" in query_args["fields"]


def test_avg_quantize_and_min_max() -> None:
    """avg 高精度除法后量化 4 位小数（ROUND_HALF_UP 展示口径）；min/max
    取参与行极值。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="0.0001"),
        _metal(id=2, weight_per_unit="0.0002"),
        _metal(id=3, weight_per_unit="10.0000"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[
                {"field": "weight_per_unit", "op": "avg", "alias": "平均"},
                {"field": "weight_per_unit", "op": "min", "alias": "最小"},
                {"field": "weight_per_unit", "op": "max", "alias": "最大"},
            ],
        ),
    )
    assert result.ok
    by_alias = {m["alias"]: m for m in result.summary["items"][0]["measures"]}
    # (0.0001+0.0002+10)/3 = 3.333433…→ 量化 4 位 = 3.3334（HALF_UP）
    assert by_alias["平均"]["value"] == "3.3334"
    assert by_alias["平均"]["n"] == 3
    assert by_alias["最小"]["value"] == "0.0001"
    assert by_alias["最大"]["value"] == "10"


def test_count_with_and_without_field() -> None:
    """count：field 可空（整行计数，count(*)）；计数包含 0/空数值行。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1), _metal(id=2, weight_per_unit="0"),
        _metal(id=3, weight_per_unit=""),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["alias"] == "count(*)"
    assert m["value"] == 3 and m["n"] == 3
    # 纯 count 无数值 measure → 无跳行、无跳行 note
    assert result.summary["total_rows"] == 3
    assert result.summary["used_rows"] == 3 and result.summary["skipped_rows"] == 0
    assert "note" not in result.summary


def test_skip_zero_empty_non_numeric_independent_measures() -> None:
    """0/空/非数值行跳过（数值 measure 的操作数为 0 = 业务「待补充」，
    mcp-api.md §6.1）；每 measure 独立判定可用行——同一行对 A 不可用但对
    B 可用时仍参与 B（行级 skip 只做展示统计，绝不从桶行集剔除行）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="10", quantity=2),   # 全部可用
        _metal(id=2, weight_per_unit="0", quantity=5),    # 单重待补充：算件数不算重量
        _metal(id=3, weight_per_unit="", quantity=3),     # 单重为空
        _metal(id=4, weight_per_unit="abc", quantity=1),  # 单重非数值
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[
                {"field": "weight_per_unit", "op": "sum",
                 "multiply_field": "quantity", "alias": "整单重量"},
                {"field": "quantity", "op": "sum", "alias": "件数"},
            ],
        ),
    )
    assert result.ok
    s = result.summary
    assert s["total_rows"] == 4
    assert s["used_rows"] == 1  # 仅 id1 对两个数值 measure 都可用（union 口径）
    assert s["skipped_rows"] == 3
    assert "已跳过 3 行" in s["note"] and "待补充" in s["note"]
    by_alias = {m["alias"]: m for m in s["items"][0]["measures"]}
    # 重量：仅 id1 参与（id2 的 0 单重被跳过）
    assert by_alias["整单重量"]["value"] == "20" and by_alias["整单重量"]["n"] == 1
    # 件数：四行全部参与（id2 虽被重量 measure 跳过，件数照算——独立性）
    assert by_alias["件数"]["value"] == "11" and by_alias["件数"]["n"] == 4


def test_bucket_all_skipped_returns_empty_value_not_fabricated_zero() -> None:
    """某组/桶全部行被跳过（0/空）：value 返回 ""（无可用样本，不编造 0
    ——0 是 SQL 对 0 值行的求和贡献，语义上该日重量未知）、n=0。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-16", weight_per_unit="0", quantity=5),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "重量"}],
        ),
    )
    assert result.ok
    item = result.summary["items"][0]
    m = item["measures"][0]
    assert m["value"] == "" and m["n"] == 0
    assert result.summary["used_rows"] == 0
    assert result.summary["skipped_rows"] == 1


# ---------------------------------------------------------------------------
# 单位混行保护（kg/m² 绝不静默混加）
# ---------------------------------------------------------------------------


def test_unit_buckets_never_mix_units() -> None:
    """实体含单位枚举字段（weight_unit kg/m²）且混行：数值 measure 按单位
    分桶（桶内标 unit），kg 与 m² 的数值绝不出现在同一桶；count 计数不
    受 0 值行影响。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="10", quantity=2),          # kg 20
        _metal(id=2, weight_per_unit="0", quantity=3),           # kg 待补充
        _metal(id=10, weight_unit="m²", weight_per_unit="1.5",
               quantity=10),                                     # m² 15
        _metal(id=11, weight_unit="m²", weight_per_unit="2",
               quantity=100),                                    # m² 200
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[
                {"field": "weight_per_unit", "op": "sum",
                 "multiply_field": "quantity", "alias": "重量"},
                {"op": "count", "alias": "行数"},
            ],
        ),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert s["unit_values"] == ["kg", "m²"]
    assert "混行" in s["note"] and "分桶" in s["note"]
    assert s["used_rows"] == 3 and s["skipped_rows"] == 1

    assert [it["unit"] for it in s["items"]] == ["kg", "m²"]  # 确定性顺序
    kg_meas = {m["alias"]: m for m in s["items"][0]["measures"]}
    m2_meas = {m["alias"]: m for m in s["items"][1]["measures"]}
    # kg 桶只含 kg 行：20；m² 桶只含 m² 行：15+200=215——绝无混加
    assert kg_meas["重量"]["value"] == "20" and kg_meas["重量"]["n"] == 1
    assert m2_meas["重量"]["value"] == "215" and m2_meas["重量"]["n"] == 2
    # count 计数全行（含 0 值行），不随数值 measure 的跳过口径
    assert kg_meas["行数"]["value"] == 2
    assert m2_meas["行数"]["value"] == 2
    # 条目结构不含明细行字段
    for it in s["items"]:
        assert set(it) == {"unit", "measures"}
        for m in it["measures"]:
            assert set(m) <= {"alias", "value", "n", "field", "multiply_field"}


def test_unit_filter_forwarded_and_single_unit_bucket() -> None:
    """filter 里的单位条件原样转发 ERP（filter 值不解析，mcp-api.md §4）；
    取数全 kg（真实 ERP 过滤后）时单桶、条目恒带 unit 标记。"""
    rows = [_metal(id=1, weight_per_unit="10", quantity=2)]
    erp = CalcErp({"metal_orders": rows})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "重量"}],
        ),
    )
    assert result.ok
    s = result.summary
    assert s["unit_values"] == ["kg"]
    assert len(s["items"]) == 1 and s["items"][0]["unit"] == "kg"
    assert "note" not in s  # 无跳行无混行 → 无附注（无混行提示）
    query_args = [c for c in erp.calls if c["tool"] == "query"][0]["arguments"]
    assert query_args["filter"] == {"weight_unit": "kg"}


def test_entity_without_unit_enum_single_bucket_no_unit_key() -> None:
    """无单位枚举字段的实体（transactions）：单桶、顶层无 unit_field、
    条目无 unit 键（无混行风险，不引入桶结构）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "100.50", "note": "a"},
        {"id": 2, "amount": "200.25", "note": "b"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions",
                    measures=[{"field": "amount", "op": "sum", "alias": "总额"}]),
    )
    assert result.ok
    s = result.summary
    assert "unit_field" not in s and "unit_values" not in s
    assert len(s["items"]) == 1 and "unit" not in s["items"][0]
    m = s["items"][0]["measures"][0]
    assert m["value"] == "300.75" and m["n"] == 2


# ---------------------------------------------------------------------------
# 分组、分页去重、取数上限、空结果、信封标记
# ---------------------------------------------------------------------------


def test_group_by_deterministic_order_and_no_row_leak() -> None:
    """group_by：组按字典序确定性输出（ISO 日期 = 时间序），每组独立聚合；
    返回结构只含组键/单位/measures——明细行（含 id/model）不进模型上下文。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-02", weight_per_unit="1", quantity=10),
        _metal(id=2, in_date="2026-08-01", weight_per_unit="2", quantity=10),
        _metal(id=3, in_date="2026-08-02", weight_per_unit="3", quantity=10),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "重量"}],
        ),
    )
    assert result.ok
    s = result.summary
    assert s["entity"] == "erp_calc"  # items 信封标记（crop 白名单外原样展示）
    assert s["target_entity"] == "metal_orders"
    assert s["group_by"] == "in_date"
    assert [it["in_date"] for it in s["items"]] == [
        "2026-08-01", "2026-08-02",
    ]
    assert s["items"][0]["unit"] == "kg" and s["items"][1]["unit"] == "kg"
    assert s["items"][0]["measures"][0]["value"] == "20"   # 2×10
    assert s["items"][1]["measures"][0]["value"] == "40"   # (1+3)×10
    assert s["total_rows"] == 3 and s["used_rows"] == 3
    for it in s["items"]:
        assert "id" not in it and "model" not in it  # 明细行不进上下文


def test_pagination_fetch_all_and_cross_page_dedupe() -> None:
    """>200 行分页取全（limit 200 窗口续查）；跨页漂移重复（ERP 并发变更
    模拟）按 id 去重，聚合不重复计行。"""
    rows = [
        _metal(id=i, weight_per_unit="1", quantity="2") for i in range(1, 451)
    ]
    erp = CalcErp({"metal_orders": rows}, drift_repeat=5)  # 页间重复 5 行
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[{"op": "count", "alias": "行数"},
                      {"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "重量"}],
        ),
    )
    assert result.ok
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert len(query_calls) == 3  # offset 0 / 200 / 400
    assert [c["arguments"].get("offset", 0) for c in query_calls] == [0, 200, 400]
    s = result.summary
    assert s["total_rows"] == 450  # 去重后唯一行数
    by_alias = {m["alias"]: m for m in s["items"][0]["measures"]}
    assert by_alias["行数"]["value"] == 450  # 漂移重复未计双次
    assert by_alias["重量"]["value"] == "900"  # 450 × 1×2
    assert s["used_rows"] == 450


def test_fetch_cap_fail_closed_no_partial_aggregate() -> None:
    """取数超过 ERP offset 硬限（10000）：fail closed 报错提示加筛选——
    绝不基于不完整数据给聚合结论。"""
    rows = [
        _metal(id=i, weight_per_unit="1", quantity="1") for i in range(1, 10002)
    ]
    erp = CalcErp({"metal_orders": rows})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "取数已达上限" in result.summary["error"]
    assert "加筛选" in result.summary["error"]
    assert "items" not in result.summary


def test_query_failure_fail_closed() -> None:
    """ERP query 失败：ok=False（错误文本），不返回部分聚合。"""
    erp = CalcErp({"metal_orders": [_metal(id=1)]}, fail_query=True)
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "查询失败" in result.summary["error"]
    assert "query exploded" in result.summary["error"]


def test_empty_result_semantics() -> None:
    """空结果：ok=True、total_rows=0、items 为空（无假桶条目——total_rows=0
    已表达「无符合筛选的行」，不给模型产出 count=0 的假桶）。"""
    erp = CalcErp({"metal_orders": []})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            measures=[{"op": "count", "alias": "行数"},
                      {"field": "weight_per_unit", "op": "sum", "alias": "重量"}],
        ),
    )
    assert result.ok
    s = result.summary
    assert s["total_rows"] == 0 and s["used_rows"] == 0
    assert s["skipped_rows"] == 0  # 无可跳过行（键恒在，值为 0）
    assert s["count"] == 0
    assert s["items"] == []
    assert "note" not in s


# ---------------------------------------------------------------------------
# [2026-09-09 reviewer 修复回合] P2-1 / P3-2 / P3-3 / P3-5② 回归
# ---------------------------------------------------------------------------


def test_null_unit_rows_fail_closed_no_silent_drop() -> None:
    """P2-1：单位枚举实体任一行缺 weight_unit（None/键缺失）→ fail closed
    并附行数——该行不进任何桶也不进 skipped，静默丢数模型无从察觉，拒绝
    聚合比给部分结论更安全（全员缺失同样拒绝）。"""
    # 混行部分缺单位：一条缺键行会被静默丢（修复前形态）
    rows = [
        _metal(id=1, weight_per_unit="10", quantity=2),  # kg
        {"id": 2, "model": "M2", "in_date": "2026-08-01",
         "weight_per_unit": "5", "quantity": 3},  # 无 weight_unit 键
    ]
    erp = CalcErp({"metal_orders": rows})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "multiply_field": "quantity", "alias": "重量"}]),
    )
    assert result.ok is False
    assert "1 行缺 weight_unit" in result.summary["error"]
    assert "fail closed" in result.summary["error"]
    assert "items" not in result.summary  # 无部分结果

    # 全员缺单位同样拒绝（修复前退化为 unit=None 单桶「数值照算不标单位」）
    all_missing = [
        {"id": 1, "in_date": "2026-08-01", "weight_per_unit": "5", "quantity": 3},
        {"id": 2, "in_date": "2026-08-01", "weight_per_unit": "7", "quantity": 2},
    ]
    erp2 = CalcErp({"metal_orders": all_missing})
    result2 = run_erp_calc(
        _ctx(erp2),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert result2.ok is False
    assert "2 行缺 weight_unit" in result2.summary["error"]


def test_missing_id_rows_not_collapsed() -> None:
    """P3-2：缺 id 行原样保留不去重（同 query.py 守卫）——旧
    str(row.get("id")) 会把多条缺 id 行塌成 "None" 一个 key 静默丢数。"""
    erp = CalcErp({"metal_orders": [
        {"id": 1, "in_date": "2026-08-01", "weight_unit": "kg",
         "weight_per_unit": "1"},                                   # 有 id
        {"in_date": "2026-08-01", "weight_unit": "kg",
         "weight_per_unit": "5"},                                   # 无 id
        {"in_date": "2026-08-02", "weight_unit": "kg",
         "weight_per_unit": "7"},                                   # 无 id
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "单重合计"}]),
    )
    assert result.ok
    s = result.summary
    assert s["total_rows"] == 3  # 缺 id 行保留：1+5+7 三行（塌缩形态只剩 2 行）
    m = s["items"][0]["measures"][0]
    assert m["value"] == "13" and m["n"] == 3


def test_exact_cap_rows_ok_no_false_positive() -> None:
    """P3-3：恰好 10000 行一次取齐（50 页）不误报「已达上限」——判超限
    先看是否已到 total 尾行。"""
    rows = [_metal(id=i, weight_per_unit="1") for i in range(1, 10001)]
    erp = CalcErp({"metal_orders": rows})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"op": "count", "alias": "行数"}]),
    )
    assert result.ok
    s = result.summary
    assert s["total_rows"] == 10000
    assert s["items"][0]["measures"][0]["value"] == 10000
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert len(query_calls) == 50  # offset 0..9800 各 200 行


def test_pagination_incomplete_fail_closed() -> None:
    """P3-3：完成性终检——契约 total 未取满（并发增行/中途空页）→ fail
    closed，绝不基于部分数据给聚合。"""
    rows = [_metal(id=i) for i in range(1, 206)]  # 仅 205 行可取
    erp = CalcErp({"metal_orders": rows}, total_override=400)  # ERP 声称 400
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count"}]),
    )
    assert result.ok is False
    assert "未取全" in result.summary["error"]
    assert "部分数据" in result.summary["error"]


def test_duplicate_alias_rejected() -> None:
    """P3-5②：同次调用结果列名（显式 alias 或自动名）重复 → 参数校验拒绝
    （重复列名让模型无法区分同名列对应哪个 measure）。"""
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum", "alias": "w"},
            {"field": "quantity", "op": "sum", "alias": "w"},
        ])
    with pytest.raises(ValidationError):
        # 自动名重复：两条 sum 同字段 → 均为 sum(weight_per_unit)
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum"},
            {"field": "weight_per_unit", "op": "sum"},
        ])
    # 显式 alias 区分同字段重复聚合 → 合法
    ok = ErpCalcArgs(entity="metal_orders", measures=[
        {"field": "weight_per_unit", "op": "sum", "alias": "w1"},
        {"field": "weight_per_unit", "op": "avg", "alias": "w2"},
    ])
    assert len(ok.measures) == 2


def test_marker_summary_passthrough_full_detail() -> None:
    """[2026-09-10 明细全量改版] items 信封标记（entity=erp_calc/catalog，
    crop 白名单外）过 tools_node 摘要器——裁剪原样、存档路径全量进上下文
    （明细无字符预算：原「超 16KB 转紧凑索引」路径已删，条目一条不丢）。"""
    from administrative_agent.agent.nodes.tools_node import summarize_tool_result
    from administrative_agent.agent.tools.crop import QUERY_ITEM_WHITELIST, crop_items

    assert "erp_calc" not in QUERY_ITEM_WHITELIST
    assert "catalog" not in QUERY_ITEM_WHITELIST
    items = [
        {"in_date": "2026-08-01", "unit": "kg",
         "measures": [{"alias": "daily_weight", "value": "4270", "n": 4}]},
        {"in_date": "2026-08-02", "unit": "kg",
         "measures": [{"alias": "daily_weight", "value": "6478", "n": 6}]},
    ]
    assert crop_items(items, "erp_calc") == items  # 未登记实体原样返回

    summary = {
        "ok": True, "entity": "erp_calc", "target_entity": "metal_orders",
        "group_by": "in_date", "total_rows": 2, "used_rows": 2,
        "skipped_rows": 0, "count": 2, "unit_field": "weight_unit",
        "items": items,
    }
    content = summarize_tool_result(
        json.dumps(summary, ensure_ascii=False), query_id="tool-call-0001"
    )
    parsed = json.loads(content)
    assert parsed["items"] == items  # 原样透传（不裁字段不丢条目）
    assert parsed["archived"] is True
    assert parsed["query_id"] == "tool-call-0001"
    assert parsed["archived_note"] == (
        "完整原始数据已存档，需要时可查询（memory_lookup，参数 query_id）"
    )
    assert "index" not in parsed  # 全量路径（紧凑索引已删除）

    # catalog 紧凑目录形态（无 query_id → 未存档全量路径，条目原样）
    catalog = {
        "ok": True, "entity": "catalog", "scope": "compact", "count": 1,
        "note": "字段目录",
        "items": [{"name": "metal_orders", "label": "钣金订单",
                   "fields": [{"name": "weight_unit", "label": "重量单位",
                               "type": "str", "enum": ["kg", "m²"]}]}],
    }
    cat_content = summarize_tool_result(json.dumps(catalog, ensure_ascii=False))
    assert json.loads(cat_content)["items"] == catalog["items"]
    assert "…共" not in cat_content  # 无省略提示

    # [2026-09-10] 超 16KB 明细全量透传——条目一条不丢、无紧凑索引、
    # 无省略提示（原明细预算 16384 已删，长度不再封顶）
    big_items = [
        {"in_date": f"2026-08-{i % 28 + 1:02d}", "unit": "kg",
         "measures": [{"alias": "w", "value": "1", "n": 1}], "pad": "x" * 400}
        for i in range(80)
    ]
    big = {"ok": True, "entity": "erp_calc", "target_entity": "metal_orders",
           "count": len(big_items), "items": big_items}
    big_content = summarize_tool_result(
        json.dumps(big, ensure_ascii=False), query_id="tool-call-0002"
    )
    big_parsed = json.loads(big_content)
    assert big_parsed["archived"] is True
    assert len(big_parsed["items"]) == len(big_items)  # 全量，一条不丢
    assert "index" not in big_parsed
    assert "…共" not in big_content
    assert len(big_content) > 16384  # 原明细预算已不再封顶


# ---------------------------------------------------------------------------
# v1.1 归一化（over）：calendar_days / 显式日数 / groups（[2026-09-09 行为
# 验证 F1 修复——「平均每日」分母由代码推断并执行除法，模型不再挑分母口算）
# ---------------------------------------------------------------------------


def test_over_parameter_validation() -> None:
    """over 参数形态校验：数字字符串转 int、0/负数拒绝、非法字符串拒绝
    （Literal）；over=groups 必须配 group_by；avg/min/max 行均值与 over
    互斥（归一化仅对 sum/count 有意义）；count 可与 over 同用。"""
    assert ErpCalcArgs(
        entity="metal_orders", group_by="in_date",
        measures=[{"op": "sum", "field": "weight_per_unit"}],
        over="31",
    ).over == 31  # 数字字符串宽容转 int
    assert ErpCalcArgs(
        entity="metal_orders",
        measures=[{"op": "sum", "field": "weight_per_unit"}],
        over=31,
    ).over == 31
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=0,
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=-5,
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over="week",
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):  # groups 无分组 → 分母无定义
        ErpCalcArgs(entity="metal_orders", over="groups",
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):  # avg 行均值 ÷ 日数无业务含义
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    over="calendar_days",
                    measures=[{"op": "avg", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", group_by="in_date", over="groups",
                    measures=[{"op": "min", "field": "weight_per_unit"}])
    # [reviewer P3-1] 互斥对整数 over 兜底同样生效（旧版漏拦截：over=31 会
    # 静默输出 Σ组均值÷31 的无意义值）——avg/min/max 全形态拒绝
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=31,
                    measures=[{"op": "avg", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", group_by="in_date", over="31",
                    measures=[{"op": "max", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=31,
                    measures=[{"op": "min", "field": "weight_per_unit"}])
    # [reviewer P4-1] bool 显式拒绝（True 经宽松转换会变 1 → divisor=1
    # 的 fail-open 口；bool 不是日数语义）
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=True,
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", over=False,
                    measures=[{"op": "sum", "field": "weight_per_unit"}])
    # count 与 over 同用合法（每日平均行数 = 总行数 ÷ 日数）；整数兜底同样
    assert ErpCalcArgs(
        entity="metal_orders", group_by="in_date", over="calendar_days",
        filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
        measures=[{"op": "count"}],
    ).over == "calendar_days"
    assert ErpCalcArgs(
        entity="metal_orders", group_by="in_date", over=31,
        measures=[{"op": "count"}, {"op": "sum", "field": "weight_per_unit"}],
    ).over == 31


def test_normalize_calendar_days_inferred_from_filter() -> None:
    """over=calendar_days：分母从 filter 日期边界推断（8-01..8-31 → 31），
    per = 桶间合计 ÷ 31（量化 2 位）；items 各日明细原样保留（总量仍可
    核对，归一化不吞原值）；results 带单位与 divisor。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
               quantity=2),  # 200
        _metal(id=2, in_date="2026-08-02", weight_per_unit="150.0000",
               quantity=1),  # 150
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "日重量"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    s = result.summary
    # items 原样：两日明细 200/150（归一化不吞原值，总量可核对）
    assert len(s["items"]) == 2
    assert {m["value"] for it in s["items"]
            for m in it["measures"]} == {"200", "150"}
    n = s["normalized"]
    assert n["over"] == "calendar_days"
    assert n["calendar_start"] == "2026-08-01"
    assert n["calendar_end"] == "2026-08-31"
    assert "31" in n["note"]
    assert len(n["results"]) == 1
    r0 = n["results"][0]
    assert r0["measure"] == "日重量"
    assert r0["unit"] == "kg"  # 桶单位挂载（报告必须能带单位）
    assert r0["total"] == "350" and r0["divisor"] == 31 and r0["n"] == 2
    assert r0["per"] == "11.29"  # 350/31 = 11.2903… → 2 位 HALF_UP


def test_normalize_calendar_days_gt_lt_open_bounds() -> None:
    """开区间边界换算：gt 起点次日起算、lt 终点前一日止算（gt 2026-07-31
    + lt 2026-09-01 → 仍 31 天）；eq 单日 → 分母 1；等值字符串简写同义。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
               quantity=2),  # 200
    ]})
    r1 = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gt": "2026-07-31", "lt": "2026-09-01"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert r1.ok
    assert r1.summary["normalized"]["calendar_start"] == "2026-08-01"
    assert r1.summary["normalized"]["calendar_end"] == "2026-08-31"
    assert r1.summary["normalized"]["results"][0]["divisor"] == 31
    assert r1.summary["normalized"]["results"][0]["per"] == "6.45"  # 200/31
    # eq 单日 → 分母 1（per = 原值）
    r2 = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"eq": "2026-08-01"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert r2.ok
    assert r2.summary["normalized"]["calendar_start"] == "2026-08-01"
    assert r2.summary["normalized"]["results"][0]["divisor"] == 1
    assert r2.summary["normalized"]["results"][0]["per"] == "200"
    # 直接等值字符串（DSL 等值简写）
    r3 = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": "2026-08-01"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert r3.ok and r3.summary["normalized"]["results"][0]["divisor"] == 1


def test_normalize_calendar_days_uninferable_fail_closed() -> None:
    """分母推不出 → fail closed（错误可见可行动，绝不静默猜日数）：filter
    无日期键 / 单边缺终点 / 日期值非法 / 多日期字段歧义 / 起点晚于终点。"""
    base: dict = {"entity": "metal_orders", "group_by": "in_date",
                  "measures": [{"field": "weight_per_unit", "op": "sum",
                                "multiply_field": "quantity"}],
                  "over": "calendar_days"}

    def run_with(filt):
        erp = CalcErp({"metal_orders": [
            _metal(id=1, weight_per_unit="10", quantity=2)]})
        return run_erp_calc(_ctx(erp), ErpCalcArgs(**base, filter=filt))

    r = run_with({"weight_unit": "kg"})  # 无日期键
    assert not r.ok and "无法推断" in r.summary["error"]
    r = run_with({"in_date": {"gte": "2026-08-01"}})  # 只有起点无终点
    assert not r.ok and "无法推断" in r.summary["error"]
    r = run_with({"in_date": {"gte": "2026-08-02", "lte": "2026-08-01"}})
    assert not r.ok and "晚于终点" in r.summary["error"]  # 起点晚于终点
    r = run_with({"in_date": {"gte": "2026-08"}})  # 值非 ISO 日期
    assert not r.ok and "无法推断" in r.summary["error"]

    # 多日期字段歧义（实体元数据含第二个 date 字段 created_at）
    entities = [{**_CALC_ENTITIES[0], "fields": {
        **_CALC_ENTITIES[0]["fields"],
        "created_at": {"type": "date", "label": "建档日期"},
    }}]
    erp = CalcErp(
        {"metal_orders": [_metal(id=1, weight_per_unit="10", quantity=2)]},
        entities=entities,
    )
    r = run_erp_calc(_ctx(erp), ErpCalcArgs(**base, filter={
        "in_date": {"gte": "2026-08-01", "lte": "2026-08-31"},
        "created_at": {"gte": "2026-08-01", "lte": "2026-08-05"},
    }))
    assert not r.ok and "多个日期字段" in r.summary["error"]
    # fail closed 发生在取数前（不白跑一轮 ERP query）
    assert not [c for c in erp.calls if c["tool"] == "query"]


def test_normalize_calendar_days_date_key_prefers_type_match() -> None:
    """reviewer P4-2：日期键两级判定。路径 1（类型命中优先）：filter 内
    有 type=date 命中时只认类型命中——string 字段恰被 ISO 形态值过滤
    （model="2026-08-01"）不再被误当日历字段、不再误报「多个日期字段」；
    路径 2（无类型命中才启用值兜底）：元数据 type 非 date 的日期字段
    （datetime）靠值形如 ISO 命中，日数推断照常。"""
    # 路径 1：in_date(type=date) + model(string) 的 ISO 形态等值过滤
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", model="2026-08-01",
               weight_per_unit="100.0000", quantity=1),
    ]})
    r1 = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"},
                    "model": "2026-08-01"},  # string 字段的 ISO 形态值
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert r1.ok, r1.summary  # 旧版 OR 判定会误报多日期字段 → fail
    assert r1.summary["normalized"]["results"][0]["divisor"] == 31

    # 路径 2：元数据 type 非 date（datetime）→ 无类型命中 → 值兜底命中
    entities = [{**_CALC_ENTITIES[0], "fields": {
        **_CALC_ENTITIES[0]["fields"],
        "in_date": {"type": "datetime", "label": "进厂日期"},
    }}]
    erp2 = CalcErp(
        {"metal_orders": [
            _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
                   quantity=1),
        ]},
        entities=entities,
    )
    r2 = run_erp_calc(
        _ctx(erp2),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert r2.ok, r2.summary  # 值兜底启用（datetime 元数据仍可推断）
    assert r2.summary["normalized"]["results"][0]["divisor"] == 31


def test_normalize_explicit_days_fallback() -> None:
    """显式日数兜底（over=31）：无需日期 filter 也能归一化（分母 31 全桶
    一致），per = 合计 ÷ 31；note 注明分母来源为显式指定。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="100.0000", quantity=2),  # 200
        _metal(id=2, weight_per_unit="0", quantity=9),  # 待补充，跳过
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "总重"}],
            over=31,
        ),
    )
    assert result.ok
    n = result.summary["normalized"]
    assert n["over"] == 31 and "显式" in n["note"]
    r0 = n["results"][0]
    assert r0["total"] == "200" and r0["divisor"] == 31
    assert r0["per"] == "6.45"  # 200/31 = 6.4516…


def test_normalize_groups_counts_rows_present_groups() -> None:
    """over=groups：分母 = 出现订单行的分组数——无有效样本但有行的组也计
    入（08-16 那种当日行全为待补充不排除）；per = 桶间合计 ÷ 组数。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
               quantity=2),  # 200，可用
        _metal(id=2, in_date="2026-08-02", weight_per_unit="0",
               quantity=9),  # 当日行全为待补充——组仍计入分母
        _metal(id=3, in_date="2026-08-03", weight_per_unit="150.0000",
               quantity=2),  # 300
        _metal(id=4, in_date="2026-08-04", weight_per_unit="100.0000",
               quantity=1),  # 100
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "日重量"}],
            over="groups",
        ),
    )
    assert result.ok
    n = result.summary["normalized"]
    assert n["over"] == "groups"
    assert "有行的组也计入" in n["note"]
    r0 = n["results"][0]
    assert r0["divisor"] == 4  # 4 个出现行的组（含 08-02 全待补充组）
    assert r0["total"] == "600"
    assert r0["per"] == "150"  # 600/4
    assert result.summary["skipped_rows"] == 1


def test_normalize_unit_buckets_each_with_unit_and_per() -> None:
    """归一化 × 单位分桶组合：kg 与 m² 各自成桶、各自归一化（results 每
    条挂桶单位），calendar_days 分母全桶一致（31）；绝不跨桶混加。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
               quantity=2),  # kg 200
        _metal(id=2, in_date="2026-08-02", weight_per_unit="150.0000",
               quantity=1),  # kg 150
        _metal(id=10, in_date="2026-08-01", weight_unit="m²",
               weight_per_unit="300.0000", quantity=1),  # m² 300
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "重量"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    n = result.summary["normalized"]
    by_unit = {r["unit"]: r for r in n["results"]}
    assert set(by_unit) == {"kg", "m²"}
    assert by_unit["kg"]["total"] == "350"
    assert by_unit["kg"]["per"] == "11.29"  # 350/31
    assert by_unit["m²"]["total"] == "300"
    assert by_unit["m²"]["per"] == "9.68"  # 300/31 = 9.6774… → HALF_UP
    assert by_unit["kg"]["divisor"] == by_unit["m²"]["divisor"] == 31
    # 混行提示与归一化正交（仍提示按桶分别报告）
    assert "混行" in result.summary["note"]


def test_normalize_count_measure_over_days() -> None:
    """count measure 与 over 同用：total = 总行数（含 0 值行，count 恒可用
    口径），per = 行数 ÷ 日数（每日平均下单行数）。[2026-09-13] total 由
    桶内全量原始行重算，本层 count 与数值 measure 的 total 一律为字符串
    （经 _fmt_decimal，如 "2"；刻意与 totals/items 的 count-int 口径不同）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="10", quantity=2),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="0", quantity=9),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"op": "count", "alias": "行数"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["measure"] == "行数"
    assert r0["total"] == "2" and r0["divisor"] == 31 and r0["n"] == 2
    assert r0["per"] == "0.06"  # 2/31 = 0.0645…


def test_normalize_empty_bucket_and_empty_rows_semantics() -> None:
    """边界语义：某组无有效样本（行在、值空串）→ 该组不贡献合计（不是
    零值混入）；全部行无有效样本 → total/per 空串（不编造 0）；无任何行
    （items 空）→ 不加 normalized 键（total_rows=0 已表达无数据）。"""
    # 场景 A：三天里一天全待补充——total 只含可用组
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100.0000",
               quantity=2),  # 200
        _metal(id=2, in_date="2026-08-02", weight_per_unit="0", quantity=9),
        _metal(id=3, in_date="2026-08-03", weight_per_unit="100.0000",
               quantity=1),  # 100
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["total"] == "300" and r0["divisor"] == 31
    assert r0["per"] == "9.68"  # 300/31（空组贡献 0，不被当作零值混入）
    # 场景 B：全行待补充 → 归一化 total/per 均为空串（不编造 0）
    erp2 = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="0", quantity=9),
    ]})
    result2 = run_erp_calc(
        _ctx(erp2),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert result2.ok
    r0b = result2.summary["normalized"]["results"][0]
    assert r0b["total"] == "" and r0b["per"] == "" and r0b["n"] == 0
    # 场景 C：无任何行 → 无 normalized 键
    erp3 = CalcErp({"metal_orders": []})
    result3 = run_erp_calc(
        _ctx(erp3),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity"}],
            over="calendar_days",
        ),
    )
    assert result3.ok
    assert result3.summary["total_rows"] == 0
    assert "normalized" not in result3.summary


def test_normalize_real_world_shape_august() -> None:
    """真数据验收同构形态（fake 数据、数字取 C2 轮实盘表）：8 月 kg 30 个
    有值日 + 08-16 全待补充组 + m² 7 行 5 个有值日的分桶结构——归一化
    输出与 SQL 基准同口径：kg total=136012.9 divisor=31 per=4387.51；
    m² total=2893 per=93.32（与仓库外 _verify_erp_calc.py 真实 ERP 断言
    同一基准）。"""
    kg_day_totals = [
        4270, 6478, 4526, 2734, 5716, 3744, 4276, 6667.6, 4701, 1945,
        2573, 2105, 7640, 5288, 5124, 12688, 11011, 2420, 4126, 7067,
        2596, 4184, 3385, 4740, 1948, 499.3, 3505, 2680, 3484, 3892,
    ]
    rows: list[dict] = []
    seq = 0
    days_with_kg = [d for d in range(1, 32) if d != 16]
    assert len(days_with_kg) == len(kg_day_totals) == 30
    for day, tot in zip(days_with_kg, kg_day_totals):
        seq += 1
        rows.append(_metal(id=seq, in_date=f"2026-08-{day:02d}",
                           weight_per_unit=str(tot), quantity=1))
    seq += 1
    rows.append(_metal(id=seq, in_date="2026-08-16", weight_per_unit="0",
                       quantity=9))  # 08-16 行存在但全待补充
    m2_rows = [(1, 490), (3, 425), (6, 399), (12, 425), (14, 330),
               (12, 425), (6, 399)]  # 7 行、行级合计 2893（5 个有值日）
    for day, tot in m2_rows:
        seq += 1
        rows.append(_metal(id=seq, in_date=f"2026-08-{day:02d}",
                           weight_unit="m²", weight_per_unit=str(tot),
                           quantity=1))
    erp = CalcErp({"metal_orders": rows})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "multiply_field": "quantity", "alias": "sum(wpu×qty)"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    n = result.summary["normalized"]
    by_unit = {r["unit"]: r for r in n["results"]}
    assert by_unit["kg"]["total"] == "136012.9"
    assert by_unit["kg"]["per"] == "4387.51"  # 136012.9/31
    assert by_unit["kg"]["divisor"] == 31 and by_unit["kg"]["n"] == 30
    assert by_unit["m²"]["total"] == "2893"
    assert by_unit["m²"]["per"] == "93.32"  # 2893/31
    assert by_unit["m²"]["divisor"] == 31 and by_unit["m²"]["n"] == 7
    # 总量可核对：items 桶值合计与 normalized total 一致（normalized 不吞
    # 原值；空串=无有效样本不贡献，与工具 sum 口径一致）
    kg_item_sum = sum(
        (Decimal(m["value"]) for it in result.summary["items"]
         if it.get("unit") == "kg" for m in it["measures"]
         if m["value"] != ""),
        Decimal("0"),
    )
    assert kg_item_sum == Decimal(by_unit["kg"]["total"])


def test_normalize_total_recomputed_from_raw_rows_with_divide_by() -> None:
    """[2026-09-13 复审] sum+divide_by+over：normalized.total 由该桶**全量
    原始行重新聚合**（先聚合、后除一次），不累加各组已量化 value——30 天
    每天 20 分钟 ÷60：旧写法 30×0.3333=9.999，正确值 Σ600/60=10。"""
    rows = [
        _metal(id=d, in_date=f"2026-08-{d:02d}",
               weight_per_unit="20", quantity=1)
        for d in range(1, 31)  # 30 天每天 1 行 20
    ]
    result = run_erp_calc(
        _ctx(CalcErp({"metal_orders": rows})),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "divide_by": 60, "alias": "hours"}],
            over=31,
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["total"] == "10"      # 不是 "9.999"
    assert r0["per"] == "0.32"      # 10/31 = 0.3225… HALF_UP → 0.32
    assert r0["divisor"] == 31
    # 3 天版：Σ60/60 = 1（不是 0.9999）
    rows3 = [
        _metal(id=d, in_date=f"2026-08-{d:02d}",
               weight_per_unit="20", quantity=1)
        for d in range(1, 4)
    ]
    r3 = run_erp_calc(
        _ctx(CalcErp({"metal_orders": rows3})),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "divide_by": 60, "alias": "hours"}],
            over=31,
        ),
    )
    assert r3.ok
    assert r3.summary["normalized"]["results"][0]["total"] == "1"


def test_normalize_groups_divisor_is_group_count_not_row_count() -> None:
    """over=groups 分母 = 桶内出现行的**组数**（≠ 行数）：3 天每天 2 行
    （共 6 行）→ divisor=3；total 仍由全量行重算（不被行数误导）。"""
    rows = []
    for d in (1, 2, 3):
        for j in (0, 1):
            rows.append(_metal(id=d * 10 + j, in_date=f"2026-08-{d:02d}",
                               weight_per_unit="30", quantity=1))
    result = run_erp_calc(
        _ctx(CalcErp({"metal_orders": rows})),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "divide_by": 60, "alias": "hours"}],
            over="groups",
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["divisor"] == 3        # 组数，不是 6 行
    assert r0["total"] == "3"        # 6 行 ×30 = 180 ÷60
    assert r0["per"] == "1"          # 3/3


def test_normalize_sum_without_divide_equals_item_group_sum() -> None:
    """不带除法的 sum + over：total 仍等于各组值之**和**（精确，回归钉子）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="100", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="200", quantity=1),
        _metal(id=3, in_date="2026-08-03", weight_per_unit="300", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum", "alias": "总重"}],
            over=3,
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["total"] == "600"
    assert r0["per"] == "200"


def test_normalize_count_total_is_string_and_equals_rows() -> None:
    """count + over：total 为字符串（经 _fmt_decimal，如 "3"）且等于行数；
    本层 total 一律字符串，刻意与 items/totals 的 count-int 口径不同。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01"),
        _metal(id=2, in_date="2026-08-02"),
        _metal(id=3, in_date="2026-08-03"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"op": "count", "alias": "行数"}],
            over=3,
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert isinstance(r0["total"], str)
    assert r0["total"] == "3"
    assert r0["per"] == "1"


def test_normalize_all_samples_empty_total_and_per_blank() -> None:
    """某 measure 全组无有效样本（0/待补充）→ total/per 均为空串（不是 0）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="0", quantity=9),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="0", quantity=9),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"weight_unit": "kg"},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "divide_by": 60, "alias": "hours"}],
            over=31,
        ),
    )
    assert result.ok
    r0 = result.summary["normalized"]["results"][0]
    assert r0["total"] == "" and r0["per"] == ""


def test_normalize_unit_buckets_independent_with_divide_by() -> None:
    """含 unit_field 的实体 + divide_by + over：两桶各自重算、绝不跨桶混加。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="60", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="120", quantity=1),
        _metal(id=3, in_date="2026-08-01", weight_unit="m²",
               weight_per_unit="300", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders",
            filter={"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}},
            group_by="in_date",
            measures=[{"field": "weight_per_unit", "op": "sum",
                       "divide_by": 60, "alias": "hours"}],
            over="calendar_days",
        ),
    )
    assert result.ok
    by_unit = {r["unit"]: r for r in result.summary["normalized"]["results"]}
    assert set(by_unit) == {"kg", "m²"}
    assert by_unit["kg"]["total"] == "3"    # (60+120)/60
    assert by_unit["kg"]["per"] == "0.1"    # 3/31 = 0.0968 → 0.10 → 尾零归一
    assert by_unit["m²"]["total"] == "5"    # 300/60
    assert by_unit["m²"]["per"] == "0.16"   # 5/31 = 0.1613 → 0.16


def test_totals_note_no_longer_claims_sum_of_groups() -> None:
    """[2026-09-13 复审] totals note 改为「合计由全量原始行重算」，不再写
    「含本桶各组之和」——含 divide_by 时 totals ≠ 各组之和，旧文案会让模型
    核对不上而怀疑 totals。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "b", "amount": "30"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
            {"field": "amount", "op": "avg", "alias": "均值"},
        ]),
    )
    assert result.ok
    assert "含本桶各组之和" not in result.summary["note"]
    assert "全量原始行" in result.summary["note"]
    assert "totals" in result.summary["note"] and "avg" in result.summary["note"]


# ---------------------------------------------------------------------------
# divide_by（常量除法，先聚合后除一次）：参数校验 / multiply 逐行 / 量化 4 位
# ---------------------------------------------------------------------------


def test_divide_by_param_validation_and_coerce() -> None:
    """divide_by 参数面：数字字符串 coerce 为 int；除 0 / 负数拒绝；bool
    拒绝（True 是 int 子类，语义不是除数）；count 拒绝并指引 over。"""
    assert MeasureSpec(field="w", op="sum", divide_by="60").divide_by == 60
    assert MeasureSpec(field="w", op="sum", divide_by=60).divide_by == 60
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by=0)
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by=-5)
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by="0")
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by="-3")
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by=True)
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by=False)
    with pytest.raises(ValidationError):
        MeasureSpec(field="w", op="sum", divide_by="abc")
    # count 无除法语义：拒绝并指引 over 归一化
    with pytest.raises(ValidationError) as exc:
        MeasureSpec(op="count", divide_by=60)
    assert "over" in str(exc.value)
    # 别名追加 /除数（自动名与乘除组合）
    assert MeasureSpec(
        field="w", op="sum", divide_by=60
    ).effective_alias == "sum(w)/60"
    assert MeasureSpec(
        field="a", op="sum", multiply_field="b", divide_by=60
    ).effective_alias == "sum(a×b)/60"


def test_divide_by_after_multiply_row_level() -> None:
    """multiply 保持逐行、divide_by 聚合后除一次：(Σ(field×multiply_field))÷
    除数；divide_by 不进 need_fields；items 条目带 divide_by 展示串。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="120", quantity=2),  # 240/60 = 4
        _metal(id=2, weight_per_unit="60", quantity=1),   # 60/60 = 1
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum",
             "multiply_field": "quantity", "divide_by": 60},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["alias"] == "sum(weight_per_unit×quantity)/60"
    assert m["value"] == "5"  # 4 + 1
    assert m["n"] == 2
    assert m["divide_by"] == "60"
    query_args = [c for c in erp.calls if c["tool"] == "query"][0]["arguments"]
    # 除数是常量：不进取数字段集合（取回行不含 divide_by）
    assert "divide_by" not in query_args["fields"]


def test_divide_by_nonterminating_quantized_4dp() -> None:
    """除不尽：先聚合、后除一次，并把含 divide_by 的结果量化 4 位小数
    ROUND_HALF_UP（展示口径）——不再是旧版的长尾全精度。100/3 → 33.3333。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "100", "note": "a"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", measures=[
            {"field": "amount", "op": "sum", "divide_by": 3, "alias": "h"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "33.3333"
    assert m["divide_by"] == "3"


def test_divide_by_aggregate_then_divide_exact_one() -> None:
    """G2①：三行各 20 分钟 ÷60 → 恰好 "1"（先聚合 60 再除一次），绝不出现
    旧版逐行除法累加的 0.999…（钉住「先聚合、后除」算法）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "20", "note": "a"},
        {"id": 2, "amount": "20", "note": "a"},
        {"id": 3, "amount": "20", "note": "a"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", measures=[
            {"field": "amount", "op": "sum", "divide_by": 60, "alias": "小时"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "1"  # 恰好等于 1
    assert Decimal(m["value"]) == Decimal(1)
    assert not m["value"].startswith("0.999")  # 明确不等于 0.999…


def test_divide_by_250_over_60_quantized() -> None:
    """G2②：250/60 除不尽 → 量化 4 位 "4.1667"（ROUND_HALF_UP）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "250", "note": "a"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", measures=[
            {"field": "amount", "op": "sum", "divide_by": 60, "alias": "小时"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "4.1667"


def test_divide_by_min_max_extreme_after_constant_division() -> None:
    """G2③：min/max 除以常量仍取正确极值（先聚合后除，d>0 序不变）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "120", "note": "a"},
        {"id": 2, "amount": "60", "note": "a"},
        {"id": 3, "amount": "30", "note": "a"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", measures=[
            {"field": "amount", "op": "min", "divide_by": 60, "alias": "最小"},
            {"field": "amount", "op": "max", "divide_by": 60, "alias": "最大"},
        ]),
    )
    assert result.ok
    by_alias = {m["alias"]: m for m in result.summary["items"][0]["measures"]}
    # 含 divide_by 时别名追加 /除数（与 effective_alias 一致）
    assert by_alias["最小/60"]["value"] == "0.5"   # 30/60
    assert by_alias["最大/60"]["value"] == "2"     # 120/60


def test_multiply_field_stays_row_level_not_factored_out() -> None:
    """G2④：multiply_field 必须保持逐行——Σ(a×b) ≠ (Σa)(Σb)，不可像
    divide_by 那样「提出去」。构造 2×3 + 5×7 = 41 ≠ (2+5)(3+7) = 70。
    若后人把乘法也提到聚合之后，本断言立即失败。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="2", quantity=3),
        _metal(id=2, weight_per_unit="5", quantity=7),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum",
             "multiply_field": "quantity", "alias": "整单重量"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "41"  # Σ(a×b)，不是 (Σa)(Σb)=70
    assert m["value"] != "70"


def test_multiply_field_then_divide_by_combined() -> None:
    """G2：乘法逐行 + 除法聚合后只除一次——三行 (2×3)+(5×7)+(1×1)=42，
    /60 恰好 "0.7"（先逐行乘、聚合 42、再除 60）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="2", quantity=3),
        _metal(id=2, weight_per_unit="5", quantity=7),
        _metal(id=3, weight_per_unit="1", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum",
             "multiply_field": "quantity", "divide_by": 60, "alias": "h"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "0.7"


def test_divide_by_zero_value_rows_still_skipped() -> None:
    """divide_by 是常量不影响可用行判定：field/multiply 为 0 的行仍按
    「待补充」跳过（常量恒有效，不因除以非零数而放行 0 行）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "amount": "120", "note": "a"},  # /60 = 2
        {"id": 2, "amount": "0", "note": "b"},    # 待补充，跳过
        {"id": 3, "amount": "", "note": "c"},     # 空，跳过
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", measures=[
            {"field": "amount", "op": "sum", "divide_by": 60, "alias": "h"},
        ]),
    )
    assert result.ok
    m = result.summary["items"][0]["measures"][0]
    assert m["value"] == "2" and m["n"] == 1
    assert result.summary["skipped_rows"] == 2


# ---------------------------------------------------------------------------
# 顶层合计 totals（2026-09-13）：分组跨组合计，按单位分桶绝不混加
# ---------------------------------------------------------------------------


def test_totals_multi_group_sum_exact() -> None:
    """无单位字段实体：分组顶层 totals 给跨组合计（value=各组之和、n=各组
    之和），单条目不带 unit 键；与 items 逐组相加精确一致。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "100.5"},
        {"id": 2, "note": "a", "amount": "200.25"},
        {"id": 3, "note": "b", "amount": "50"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
            {"op": "count", "alias": "笔数"},
        ]),
    )
    assert result.ok
    s = result.summary
    totals = s["totals"]
    by_alias = {t["alias"]: t for t in totals}
    assert set(by_alias) == {"总额", "笔数"}
    for t in totals:
        assert "unit" not in t  # 无单位字段实体：条目不带 unit 键
    assert by_alias["总额"]["value"] == "350.75" and by_alias["总额"]["n"] == 3
    # [F3] count 类 totals 的 value 保持 int（与 items[].measures[].value 同类型）
    assert by_alias["笔数"]["value"] == 3 and by_alias["笔数"]["n"] == 3
    assert isinstance(by_alias["笔数"]["value"], int)
    # count 合计 == items 逐组 value 相加（int 逐行相加）
    count_sum = sum(
        m["value"] for it in s["items"] for m in it["measures"]
        if m["alias"] == "笔数" and m["value"] != ""
    )
    assert by_alias["笔数"]["value"] == count_sum == 3
    # 与 items 逐组相加精确一致（用户可照表核对）
    item_sum = sum(
        (Decimal(m["value"]) for it in s["items"] for m in it["measures"]
         if m["alias"] == "总额" and m["value"] != ""),
        Decimal("0"),
    )
    assert item_sum == Decimal(by_alias["总额"]["value"]) == Decimal("350.75")


def test_totals_unit_buckets_never_mix() -> None:
    """单位分桶：每桶一个合计、绝不跨桶混加（kg 与 m² 的数值绝不出现在
    同一合计）；totals 与逐桶相加精确一致。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="10", quantity=2),   # kg 20
        _metal(id=2, in_date="2026-08-02", weight_per_unit="3", quantity=10),   # kg 30
        _metal(id=10, in_date="2026-08-01", weight_unit="m²",
               weight_per_unit="1.5", quantity=10),                             # m² 15
        _metal(id=11, in_date="2026-08-03", weight_unit="m²",
               weight_per_unit="2", quantity=100),                              # m² 200
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date", measures=[
            {"field": "weight_per_unit", "op": "sum",
             "multiply_field": "quantity", "alias": "重量"},
        ]),
    )
    assert result.ok
    s = result.summary
    by_unit = {t["unit"]: t for t in s["totals"]}
    assert set(by_unit) == {"kg", "m²"}  # 每桶一条，绝无跨桶混加
    assert by_unit["kg"]["value"] == "50" and by_unit["kg"]["n"] == 2
    assert by_unit["m²"]["value"] == "215" and by_unit["m²"]["n"] == 2
    # 与 items 逐桶相加精确一致
    for unit, expected in (("kg", Decimal("50")), ("m²", Decimal("215"))):
        bucket_sum = sum(
            (Decimal(m["value"]) for it in s["items"]
             if it.get("unit") == unit for m in it["measures"]
             if m["value"] != ""),
            Decimal("0"),
        )
        assert bucket_sum == Decimal(by_unit[unit]["value"]) == expected


# ---------------------------------------------------------------------------
# 单位无关 measure（total_amount，金额）：跨桶合并（[2026-09-14] 7 月财报口径）
# ---------------------------------------------------------------------------


def test_unit_independent_total_amount_merged_across_buckets() -> None:
    """[2026-09-14 事故复现] sum(total_amount) 是**单位无关**的单一 measure：
    即使实体存在 weight_unit（kg/m²）混行，金额（元）跨桶相加有意义——items
    每客户只出现一条（**同一客户同时有 kg 单与 m² 单时只出现一条金额**）、
    条目不带 unit 键；totals 单条、无 unit 键、值 = 跨桶之和。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, customer="甲", total_amount="1000"),                  # kg
        _metal(id=2, customer="甲", weight_unit="m²", total_amount="500"),  # m² 同客户
        _metal(id=3, customer="乙", total_amount="300"),                  # kg
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer", measures=[
            {"field": "total_amount", "op": "sum", "alias": "金额"},
        ]),
    )
    assert result.ok
    s = result.summary
    # 视为无单位桶：顶层无 unit_field/unit_values、条目无 unit 键
    assert "unit_field" not in s and "unit_values" not in s
    assert "分桶" not in (s.get("note") or "")
    # 每个客户一条（甲含 kg+m² 两单，仍只一条金额；绝不拆成两条）
    by_cust = {it["customer"]: it["measures"][0]["value"] for it in s["items"]}
    assert by_cust == {"甲": "1500", "乙": "300"}
    assert len(s["items"]) == 2
    for it in s["items"]:
        assert "unit" not in it
    # totals 单条、无 unit 键、值 = 跨桶之和（1000+500+300）
    assert len(s["totals"]) == 1
    assert "unit" not in s["totals"][0]
    assert s["totals"][0]["value"] == "1800" and s["totals"][0]["n"] == 3


def test_unit_dependent_weight_still_bucketed() -> None:
    """回归：物理量 measure（weight_per_unit×quantity，单位随 weight_unit 变）
    仍按单位分桶、条目带 unit 键、桶间绝不混加（不受金额豁免影响）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, weight_per_unit="10", quantity=2),                  # kg 20
        _metal(id=2, weight_unit="m²", weight_per_unit="3", quantity=5),  # m² 15
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "weight_per_unit", "op": "sum",
             "multiply_field": "quantity", "alias": "重量"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert [(it["unit"], it["measures"][0]["value"]) for it in s["items"]] == [
        ("kg", "20"), ("m²", "15"),
    ]


def test_mixed_measures_stay_bucketed_by_design() -> None:
    """【有意行为，非漏改】金额 measure 与物理量 measure **混在同一调用**时，
    整个调用维持分桶（判定为「全部 measure 单位无关」才豁免；全有或全无）。
    理由：items 是「单位桶 × 分组」二维平铺，逐 measure 混合合并会让同一分组
    出现两条且语义含混；财报路径只用单个 sum(total_amount)，skill 层禁止混调。
    此处断言「不合并」即是对该设计的有意锁定。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, total_amount="1000", weight_per_unit="10"),                  # kg
        _metal(id=2, weight_unit="m²", total_amount="500", weight_per_unit="3"),  # m²
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "total_amount", "op": "sum", "alias": "金额"},
            {"field": "weight_per_unit", "op": "sum", "alias": "重量"},
        ]),
    )
    assert result.ok
    s = result.summary
    # 混调 → 维持分桶（unit_field 在、条目带 unit、金额未跨桶合并成 1500）
    assert s["unit_field"] == "weight_unit"
    assert [it["unit"] for it in s["items"]] == ["kg", "m²"]
    amounts = {it["unit"]: it["measures"][0]["value"] for it in s["items"]}
    assert amounts == {"kg": "1000", "m²": "500"}  # 未合并


def test_count_measure_still_bucketed() -> None:
    """回归：count（field 为空）不算单位无关命中 → 仍按单位分桶、条目带 unit。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1), _metal(id=2),
        _metal(id=10, weight_unit="m²"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[{"op": "count", "alias": "行数"}]),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert [(it["unit"], it["measures"][0]["value"]) for it in s["items"]] == [
        ("kg", 2), ("m²", 1),
    ]


def test_count_with_total_amount_field_still_bucketed() -> None:
    """[2026-09-14 reviewer A] 带 field 的 count 也一律不算单位无关命中：豁免
    语义是「该 measure **产出的是金额**」，而 count 任何形态产出的都是**行数**。
    故 count(field="total_amount") 仍按单位分桶（2 行 kg + 1 行 m²）。（变体：
    删掉判据里 `op != COUNT` → 本测试必须 RED。）"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, total_amount="1000"),
        _metal(id=2, total_amount="2000"),
        _metal(id=10, weight_unit="m²", total_amount="500"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "total_amount", "op": "count", "alias": "单数"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert [(it["unit"], it["measures"][0]["value"]) for it in s["items"]] == [
        ("kg", 2), ("m²", 1),
    ]


def test_unit_independent_call_skips_missing_unit_guard() -> None:
    """视为无单位桶时，「行缺单位值」fail-closed 守卫**不得**触发——既然不归
    桶，单位值缺失与本结果无关。这里一行缺 weight_unit 键仍给出金额合计。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, total_amount="1000"),  # 有 weight_unit
        {"id": 2, "model": "M", "in_date": "2026-08-01",
         "total_amount": "500"},            # 无 weight_unit 键
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "total_amount", "op": "sum", "alias": "金额"},
        ]),
    )
    assert result.ok  # 未因缺单位值 fail closed
    s = result.summary
    assert "error" not in s
    assert "unit_field" not in s
    assert len(s["items"]) == 1 and "unit" not in s["items"][0]
    assert s["items"][0]["measures"][0]["value"] == "1500"


def test_total_amount_times_weight_per_unit_still_bucketed() -> None:
    """[2026-09-14 补洞] sum(total_amount × weight_per_unit) 必须分桶：乘数
    weight_per_unit 随单位桶变，乘积 = 元·(kg/件) 或 元·(m²/件)，**跨桶混加
    无意义**。判据是「measure 里没有任何随桶变的因子」，故带单位相关乘数即按
    单位相关处理（这是 M3 的红点：谓词退回只判 field 会让本测试变红）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, total_amount="1000", weight_per_unit="10"),                  # kg
        _metal(id=2, weight_unit="m²", total_amount="500", weight_per_unit="3"),  # m²
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "total_amount", "op": "sum",
             "multiply_field": "weight_per_unit", "alias": "乘积"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert [it["unit"] for it in s["items"]] == ["kg", "m²"]
    values = {it["unit"]: it["measures"][0]["value"] for it in s["items"]}
    assert values == {"kg": "10000", "m²": "1500"}  # 乘积值，未跨桶合并


def test_total_amount_times_quantity_still_bucketed_conservative() -> None:
    """【有意行为，勿"优化"】sum(total_amount × quantity) 必须分桶：quantity
    不在 _UNIT_INDEPENDENT_FIELDS，故整条 measure 判为单位相关。该值其实与单位
    无关（元×件），但两个方向的代价不对称——分桶只让模型多看到两桶（可自行
    相加），误合并则是静默错数；故一律取保守。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, total_amount="1000", quantity=2),                  # kg
        _metal(id=2, weight_unit="m²", total_amount="500", quantity=5),  # m²
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", measures=[
            {"field": "total_amount", "op": "sum",
             "multiply_field": "quantity", "alias": "乘积"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert s["unit_field"] == "weight_unit"
    assert [it["unit"] for it in s["items"]] == ["kg", "m²"]
    values = {it["unit"]: it["measures"][0]["value"] for it in s["items"]}
    assert values == {"kg": "2000", "m²": "2500"}  # 分桶，未合并


def test_totals_skips_avg_min_max_with_note() -> None:
    """totals 仅含 sum/count；avg/min/max 行均值/行极值无桶间合计语义，
    跳过并在 note 说明原因（sum 仍给合计）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "a", "amount": "20"},
        {"id": 3, "note": "b", "amount": "30"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
            {"field": "amount", "op": "avg", "alias": "均值"},
        ]),
    )
    assert result.ok
    s = result.summary
    aliases = {t["alias"] for t in s["totals"]}
    assert aliases == {"总额"}  # avg 不进 totals
    assert s["totals"][0]["value"] == "60"
    assert "totals" in s["note"] and "avg" in s["note"]


def test_totals_absent_when_only_avg_note_has_no_totals_promise() -> None:
    """[F4] 分组且仅 avg/min/max（无 sum/count）→ 不产出 totals，且 note
    **不得**再承诺「报告合计请用 totals」；改给「不提供跨组合计」的说明。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "a", "amount": "20"},
        {"id": 3, "note": "b", "amount": "30"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "avg", "alias": "均值"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert "totals" not in s  # 仅 avg → 无 totals
    assert "报告合计请用 totals" not in s["note"]  # 不再空承诺
    assert "totals" not in s["note"]  # note 完全不提 totals
    assert "不提供" in s["note"] and "avg" in s["note"]


def test_totals_absent_without_group_by_or_with_over() -> None:
    """totals 仅在「分组且无 over」时给出：无 group_by 不给；over 存在时
    由 normalized.total 承载同一数字，不重复给 totals。"""
    base_rows = {"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "b", "amount": "30"},
    ]}
    # 无 group_by → 无 totals
    r1 = run_erp_calc(
        _ctx(CalcErp(dict(base_rows))),
        ErpCalcArgs(entity="transactions",
                    measures=[{"field": "amount", "op": "sum", "alias": "总额"}]),
    )
    assert r1.ok and "totals" not in r1.summary
    # over 存在 → 无 totals（normalized 权威）
    r2 = run_erp_calc(
        _ctx(CalcErp(dict(base_rows))),
        ErpCalcArgs(entity="transactions", group_by="note", over=2,
                    measures=[{"field": "amount", "op": "sum", "alias": "总额"}]),
    )
    assert r2.ok
    assert "totals" not in r2.summary
    assert r2.summary["normalized"]["results"][0]["total"] == "40"


def test_totals_empty_group_contributes_zero_not_fabricated() -> None:
    """组内全行待补充（value 空串）→ 该组对合计贡献 0（不编造零值），totals
    的 n 只计实际参与行数；与 items 空串口径一致。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "100"},
        {"id": 2, "note": "b", "amount": "0"},   # 该组全待补充
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
        ]),
    )
    assert result.ok
    t = {x["alias"]: x for x in result.summary["totals"]}["总额"]
    assert t["value"] == "100" and t["n"] == 1


# ---------------------------------------------------------------------------
# totals 重算口径修正（2026-09-13 复审）：合计对全量原始行重算「先聚合后除
# 一次」，不累加各组已量化 value（后者让量化误差按组数累积，0.3333×3=0.9999）
# ---------------------------------------------------------------------------


def test_totals_recompute_divide_by_no_quantization_drift() -> None:
    """[缺陷钉子] 分组 + divide_by：items 每组仍是被量化的 0.3333（不变），
    但 totals 必须对该桶**全量原始行重算**（先聚合 60、后除一次）→ 恰好
    "1"；旧版累加各组 0.3333 得 "0.9999"（divide_by 伪影在合计层复活）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="20", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="20", quantity=1),
        _metal(id=3, in_date="2026-08-03", weight_per_unit="20", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date", measures=[
            {"field": "weight_per_unit", "op": "sum", "divide_by": 60,
             "alias": "小时"},
        ]),
    )
    assert result.ok
    s = result.summary
    # items 每组被量化到 4 位（不变——组值展示口径不动）
    assert [it["measures"][0]["value"] for it in s["items"]] == ["0.3333"] * 3
    t = {x["alias"]: x for x in s["totals"]}["小时/60"]
    assert t["value"] == "1"  # 60/60，不是 0.9999
    assert Decimal(t["value"]) == Decimal(1)
    assert not t["value"].startswith("0.999")
    assert t["n"] == 3  # 桶内有效样本总数（= 逐组 n 之和）
    assert t["unit"] == "kg" and t["divide_by"] == "60"


def test_totals_sum_without_divide_equals_group_sum() -> None:
    """不带除法的 sum 合计仍等于各组值之和（钉住既有行为未改坏）——用
    0.1/0.2/0.35 这类真正产生小数的数据，Decimal 精确。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "0.1"},
        {"id": 2, "note": "a", "amount": "0.2"},
        {"id": 3, "note": "b", "amount": "0.35"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
        ]),
    )
    assert result.ok
    s = result.summary
    group_sum = sum(
        (Decimal(m["value"]) for it in s["items"] for m in it["measures"]
         if m["value"] != ""),
        Decimal("0"),
    )
    t = {x["alias"]: x for x in s["totals"]}["总额"]
    assert Decimal(t["value"]) == group_sum == Decimal("0.65")  # 0.3 + 0.35


def test_totals_count_int_and_n_identity() -> None:
    """count 类 totals 仍是 int（与 items 同型），且每 measure 的 n 恒等于
    各组 n 之和（分组对桶行集是划分，数值可加）。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "a", "amount": "0"},    # sum 跳过、count 计入
        {"id": 3, "note": "b", "amount": "20"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
            {"op": "count", "alias": "笔数"},
        ]),
    )
    assert result.ok
    s = result.summary
    by_alias = {t["alias"]: t for t in s["totals"]}
    assert isinstance(by_alias["总额"]["value"], str)
    assert by_alias["总额"]["value"] == "30"  # 10 + 20（0 值组贡献 0）
    assert isinstance(by_alias["笔数"]["value"], int)
    assert by_alias["笔数"]["value"] == 3  # 含 0 值行（count 恒可用）
    # n 恒等式：totals.n == 该 measure 各组 n 之和
    for alias, t in by_alias.items():
        group_n = sum(
            m["n"] for it in s["items"] for m in it["measures"]
            if m["alias"] == alias
        )
        assert t["n"] == group_n, alias


def test_totals_only_sum_count_avg_min_max_excluded_mixed() -> None:
    """同一个调用里 sum/count 进了 totals、avg/min/max 全不进（行均值/行
    极值无桶间合计语义）——混合场景逐项断言。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "10"},
        {"id": 2, "note": "a", "amount": "20"},
        {"id": 3, "note": "b", "amount": "30"},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
            {"op": "count", "alias": "笔数"},
            {"field": "amount", "op": "avg", "alias": "均值"},
            {"field": "amount", "op": "min", "alias": "最小"},
            {"field": "amount", "op": "max", "alias": "最大"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert {t["alias"] for t in s["totals"]} == {"总额", "笔数"}  # 仅 sum/count
    assert "avg" in s["note"] and "min" in s["note"] and "max" in s["note"]
    # items 仍含全部五条 measure（totals 过滤不影响明细）
    assert {m["alias"] for it in s["items"] for m in it["measures"]} == {
        "总额", "笔数", "均值", "最小", "最大",
    }


def test_totals_unit_buckets_with_divide_by_independent() -> None:
    """单位桶不混加 × divide_by：两个单位各自成桶、各自对全量原始行重算合计，
    两桶合计都正确且互不混入（kg 与 m² 的数值绝不同桶）。"""
    erp = CalcErp({"metal_orders": [
        # kg：三日各 20 → 桶内聚合 60 ÷60 = 1
        _metal(id=1, in_date="2026-08-01", weight_per_unit="20", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="20", quantity=1),
        _metal(id=3, in_date="2026-08-03", weight_per_unit="20", quantity=1),
        # m²：两日 30/90 → 桶内聚合 120 ÷60 = 2（绝不与 kg 相加）
        _metal(id=10, in_date="2026-08-01", weight_unit="m²",
               weight_per_unit="30", quantity=1),
        _metal(id=11, in_date="2026-08-02", weight_unit="m²",
               weight_per_unit="90", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date", measures=[
            {"field": "weight_per_unit", "op": "sum", "divide_by": 60,
             "alias": "小时"},
        ]),
    )
    assert result.ok
    by_unit = {t["unit"]: t for t in result.summary["totals"]}
    assert set(by_unit) == {"kg", "m²"}  # 每桶一条，绝无跨桶混加
    assert by_unit["kg"]["value"] == "1"     # (20+20+20)/60
    assert by_unit["kg"]["n"] == 3
    assert by_unit["m²"]["value"] == "2"     # (30+90)/60
    assert by_unit["m²"]["n"] == 2


def test_totals_all_samples_empty_returns_empty_not_zero() -> None:
    """桶内该 measure 无任何有效样本（全 0/空）→ 各组 value 空串、totals 也
    是空串（不编造成假数字 "0"），n=0。"""
    erp = CalcErp({"transactions": [
        {"id": 1, "note": "a", "amount": "0"},
        {"id": 2, "note": "b", "amount": ""},
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="transactions", group_by="note", measures=[
            {"field": "amount", "op": "sum", "alias": "总额"},
        ]),
    )
    assert result.ok
    s = result.summary
    assert [it["measures"][0]["value"] for it in s["items"]] == ["", ""]
    t = {x["alias"]: x for x in s["totals"]}["总额"]
    assert t["value"] == "" and t["n"] == 0  # 空串不是 "0"


# ---------------------------------------------------------------------------
# 分组名称解析（erp_calc 服务端把外键 id → 名称；只新增 <外键去_id>_name 键）
# ---------------------------------------------------------------------------


def _name_entities(*, company_name_sensitive: bool = False) -> list[dict]:
    """名称解析用例目录：metal_orders（customer_id）+ customers（company_name）。"""
    return [
        {
            "name": "metal_orders", "label": "钣金订单", "readable": True,
            "creatable": True, "updatable": True, "deletable": False,
            "business_operations": [],
            "fields": {
                "id": {"type": "int", "label": "ID"},
                "customer_id": {"type": "int", "label": "客户ID"},
                "customer": {"type": "string", "label": "客户"},
                "quantity": {"type": "int", "label": "数量"},
                "weight_unit": {
                    "type": "str", "label": "重量单位", "required": True,
                    "enum_values": ["kg", "m²"],
                },
            },
        },
        {
            "name": "customers", "label": "客户", "readable": True,
            "creatable": False, "updatable": False, "deletable": False,
            "business_operations": [],
            "fields": {
                "id": {"type": "int", "label": "客户ID"},
                "company_name": {
                    "type": "str", "label": "公司名称",
                    "sensitive": company_name_sensitive,
                },
            },
        },
    ]


class _NameFailErp(CalcErp):
    """仅让 customers 的名称查询失败（其余 query 正常），验证 fail-soft 降级。"""

    def call_tool(self, tool_name, arguments=None, operation_id=None,
                  trace_id=None):  # type: ignore[override]
        if tool_name == "query" and (arguments or {}).get("entity") == "customers":
            self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
            return ToolResult(
                ok=False,
                error=MCPError(
                    code="MCP_SERVER_ERROR", category=ErrorCategory.SERVER,
                    message="customer lookup exploded",
                ),
            )
        return super().call_tool(tool_name, arguments, operation_id, trace_id)


def test_group_by_fk_adds_name_key_only() -> None:
    """有分组且映射成功：items **同时**含 {group_by: id} 与名称键；原名与
    measures 逐字节不变；名称只发**一次**批量查询（id in [...]）。"""
    erp = CalcErp(
        {
            "metal_orders": [
                {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
                {"id": 2, "customer_id": 10, "quantity": 3, "weight_unit": "kg"},
                {"id": 3, "customer_id": 20, "quantity": 1, "weight_unit": "kg"},
            ],
            "customers": [
                {"id": 10, "company_name": "甲方公司"},
                {"id": 20, "company_name": "乙方公司"},
            ],
        },
        entities=_name_entities(),
    )
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    by_id = {it["customer_id"]: it for it in result.summary["items"]}
    # 原 group_by 键照旧
    assert by_id[10]["measures"][0]["value"] == "5"
    assert by_id[20]["measures"][0]["value"] == "1"
    # 新增名称键（customer_id → customer_name，去掉 _id 后缀，无叠字）
    assert by_id[10]["customer_name"] == "甲方公司"
    assert by_id[20]["customer_name"] == "乙方公司"
    assert "customer_id_name" not in by_id[10]
    # 名称查询只发一次、形状为 id in [...]
    name_calls = [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]
    assert len(name_calls) == 1
    assert name_calls[0]["arguments"]["filter"] == {"id": {"in": [10, 20]}}


def test_group_by_name_lookup_error_is_fail_soft() -> None:
    """名称查询报错：仍 ok=True、measures 不变、无名称键、留可降级提示。"""
    erp = _NameFailErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
        ],
        "customers": [{"id": 10, "company_name": "甲方公司"}],
    }, entities=_name_entities())
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    item = result.summary["items"][0]
    assert item["customer_id"] == 10
    assert "customer_name" not in item
    assert item["measures"][0]["value"] == "2"
    assert "未解析名称" in result.summary["note"]


def test_group_by_name_missing_id_partial_note() -> None:
    """部分组的档案查不到 → 该组不补键，其余照补；summary 有部分降级提示。"""
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
            {"id": 2, "customer_id": 99, "quantity": 1, "weight_unit": "kg"},
        ],
        "customers": [{"id": 10, "company_name": "甲方公司"}],  # 缺 id=99
    }, entities=_name_entities())
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    by_id = {it["customer_id"]: it for it in result.summary["items"]}
    assert by_id[10]["customer_name"] == "甲方公司"
    assert "customer_name" not in by_id[99]
    assert "未解析到名称" in result.summary["note"]


def test_group_by_name_lookup_metadata_missing_is_fail_soft() -> None:
    """目标实体不在元数据（如未开放）→ 不解析、ok=True、有降级提示。"""
    ents = [e for e in _name_entities() if e["name"] != "customers"]
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
        ],
    }, entities=ents)
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    assert "customer_name" not in result.summary["items"][0]
    assert "未解析名称" in result.summary["note"]


def test_group_by_name_sensitive_column_skipped() -> None:
    """名称列被 ERP 标记 sensitive → 跳过解析（宁可不给名称），有提示。"""
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
        ],
        "customers": [{"id": 10, "company_name": "甲方公司"}],
    }, entities=_name_entities(company_name_sensitive=True))
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    assert "customer_name" not in result.summary["items"][0]
    assert "敏感" in result.summary["note"]
    # 敏感列 → 一次名称查询都不该发
    assert not [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]


def test_group_by_name_ids_over_limit_skipped_no_query() -> None:
    """组数超过 _GROUP_NAME_MAX_IDS（501 组）→ **整体跳过**：一次名称查询都
    不发（不逐 id 发查询打爆）、无名称键、measures 不变、留可见降级提示。"""
    from administrative_agent.agent.tools.calc import _GROUP_NAME_MAX_IDS

    n = _GROUP_NAME_MAX_IDS + 1  # 501 组
    orders = [
        {"id": i, "customer_id": i, "quantity": 1, "weight_unit": "kg"}
        for i in range(1, n + 1)
    ]
    erp = CalcErp({"metal_orders": orders}, entities=_name_entities())
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    items = result.summary["items"]
    assert len(items) == n
    # measures 恒不受影响：每组 1 行，各组 sum 合计 = n
    assert sum(int(it["measures"][0]["value"]) for it in items) == n
    # 未补任何名称键
    assert not [k for it in items for k in it if k.endswith("_name")]
    # 整体跳过：一次名称查询都不发
    assert not [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]
    note = result.summary["note"]
    assert "未解析名称" in note
    assert str(n) in note


def test_group_by_name_field_absent_in_metadata_is_fail_soft() -> None:
    """目标实体在元数据里存在但**没有名称字段** → 降级 note、不查、ok=True、
    measures 不变。"""
    ents = _name_entities()
    for e in ents:
        if e["name"] == "customers":
            e["fields"].pop("company_name")
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
        ],
    }, entities=ents)
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer_id",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    assert result.summary["items"][0]["measures"][0]["value"] == "2"
    assert "customer_name" not in result.summary["items"][0]
    assert "未解析名称" in result.summary["note"]
    assert not [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]


def test_group_by_undeclared_fk_not_resolved() -> None:
    """未声明的外键（如 metal_orders.customer 字符串列）不解析、不额外查询。"""
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer": "甲", "quantity": 2, "weight_unit": "kg"},
        ],
    }, entities=_name_entities())
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="customer",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    item = result.summary["items"][0]
    assert item["customer"] == "甲"
    assert not [k for k in item if k.endswith("_name")]
    assert not [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]


def test_no_group_by_output_byte_identical() -> None:
    """无 group_by 的单桶路径**逐字节不变**（基线取自本次改动前的输出形态）。"""
    erp = CalcErp({
        "metal_orders": [
            {"id": 1, "customer_id": 10, "quantity": 2, "weight_unit": "kg"},
            {"id": 2, "customer_id": 10, "quantity": 3, "weight_unit": "kg"},
        ],
        "customers": [{"id": 10, "company_name": "甲方公司"}],
    }, entities=_name_entities())
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders",
                    measures=[{"field": "quantity", "op": "sum"}]),
    )
    assert result.ok is True
    # items 逐字节等于基线（sort_keys 归一化键序）
    assert json.dumps(result.summary["items"], sort_keys=True) == (
        '[{"measures": [{"alias": "sum(quantity)", "field": "quantity", '
        '"n": 2, "value": "5"}], "unit": "kg"}]'
    )
    # summary 其余键也逐字节等于基线（无新键、无 note）
    assert json.dumps(
        {k: v for k, v in result.summary.items() if k != "items"}, sort_keys=True
    ) == (
        '{"count": 1, "entity": "erp_calc", "group_by": null, "skipped_rows": 0, '
        '"target_entity": "metal_orders", "total_rows": 2, '
        '"unit_field": "weight_unit", "unit_values": ["kg"], "used_rows": 2}'
    )
    # 无分组路径不触发名称查询
    assert not [
        c for c in erp.calls
        if c["tool"] == "query" and c["arguments"].get("entity") == "customers"
    ]


# ---------------------------------------------------------------------------
# C 缺口：time_granularity（按月/季汇总在工具层无解）
# ---------------------------------------------------------------------------


def test_time_granularity_month_buckets_cross_year() -> None:
    """C：time_granularity=month 把日期型 group_by 截断成 "YYYY-MM" 桶键——
    跨年边界 12 月归 "2025-12"、1 月归 "2026-01"（不落进同一桶）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2025-12-15", weight_per_unit="1", quantity=1),
        _metal(id=2, in_date="2025-12-31", weight_per_unit="2", quantity=1),
        _metal(id=3, in_date="2026-01-01", weight_per_unit="10", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders", group_by="in_date", time_granularity="month",
            measures=[{"field": "weight_per_unit", "op": "sum", "alias": "重量"}],
        ),
    )
    assert result.ok
    s = result.summary
    assert [it["in_date"] for it in s["items"]] == ["2025-12", "2026-01"]
    by_bucket = {it["in_date"]: it["measures"][0]["value"] for it in s["items"]}
    assert by_bucket["2025-12"] == "3"    # 12 月两行合并
    assert by_bucket["2026-01"] == "10"   # 次年 1 月独立成桶
    # totals 仍是全量权威合计（3 + 10）
    assert s["totals"][0]["value"] == "13"


def test_time_granularity_quarter_buckets_cross_year() -> None:
    """C：time_granularity=quarter 桶键 "YYYY-Qn"——12 月归当年 Q4、1 月归次
    年 Q1，月→季映射 (month-1)//3+1 正确（3/4、9/10 为季界）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2025-12-31", weight_per_unit="1", quantity=1),   # Q4
        _metal(id=2, in_date="2026-01-01", weight_per_unit="2", quantity=1),   # Q1
        _metal(id=3, in_date="2026-03-31", weight_per_unit="3", quantity=1),   # Q1
        _metal(id=4, in_date="2026-04-01", weight_per_unit="4", quantity=1),   # Q2
        _metal(id=5, in_date="2026-09-30", weight_per_unit="5", quantity=1),   # Q3
        _metal(id=6, in_date="2026-10-01", weight_per_unit="6", quantity=1),   # Q4
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(
            entity="metal_orders", group_by="in_date", time_granularity="quarter",
            measures=[{"field": "weight_per_unit", "op": "sum", "alias": "重量"}],
        ),
    )
    assert result.ok
    by_bucket = {it["in_date"]: it["measures"][0]["value"]
                 for it in result.summary["items"]}
    assert by_bucket == {
        "2025-Q4": "1", "2026-Q1": "5", "2026-Q2": "4",
        "2026-Q3": "5", "2026-Q4": "6",
    }


def test_time_granularity_requires_group_by() -> None:
    """C：time_granularity 必须与 group_by 同用（无分组无处施加）→ 参数层拒绝。"""
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", time_granularity="month",
                    measures=[{"op": "count"}])


def test_time_granularity_requires_date_group_by() -> None:
    """C：group_by 字段类型必须为日期型 → 非日期字段（model 字符串）fail closed
    （类型判定需实体元数据，故在 run 内校验）。"""
    erp = CalcErp({"metal_orders": [_metal(id=1, model="M")]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="model",
                    time_granularity="month",
                    measures=[{"op": "count", "alias": "行数"}]),
    )
    assert result.ok is False
    assert "日期型" in result.summary["error"]


def test_time_granularity_unparseable_date_fail_closed() -> None:
    """C：日期值无法解析（非 ISO）→ fail closed，绝不静默错分/丢数。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01"),
        _metal(id=2, in_date="not-a-date"),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    time_granularity="month",
                    measures=[{"op": "count", "alias": "行数"}]),
    )
    assert result.ok is False
    assert "无法解析为日期" in result.summary["error"]


# ---------------------------------------------------------------------------
# D 缺口：order_by + limit（按计算列排序取 Top-N）
# ---------------------------------------------------------------------------


def test_order_by_ascending_descending() -> None:
    """D：order_by 收 measure 别名，前缀 "-" 表降序；升/降序都按**数值**比。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="1", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="2", quantity=1),
        _metal(id=3, in_date="2026-08-03", weight_per_unit="3", quantity=1),
    ]})
    args_kw = dict(entity="metal_orders", group_by="in_date",
                   measures=[{"field": "weight_per_unit", "op": "sum",
                              "alias": "重量"}])
    desc = run_erp_calc(_ctx(erp), ErpCalcArgs(order_by="-重量", **args_kw))
    asc = run_erp_calc(_ctx(erp), ErpCalcArgs(order_by="重量", **args_kw))
    assert desc.ok and asc.ok
    assert [it["in_date"] for it in desc.summary["items"]] == [
        "2026-08-03", "2026-08-02", "2026-08-01"]
    assert [it["in_date"] for it in asc.summary["items"]] == [
        "2026-08-01", "2026-08-02", "2026-08-03"]


def test_order_by_tie_keeps_bucket_key_order() -> None:
    """D：measure 值相同时按**桶键序**做 tiebreaker（升/降序皆保持原键序，
    Python 稳定排序）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="5", quantity=1),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="5", quantity=1),
    ]})
    args_kw = dict(entity="metal_orders", group_by="in_date",
                   measures=[{"field": "weight_per_unit", "op": "sum",
                              "alias": "重量"}])
    for ob in ("重量", "-重量"):
        result = run_erp_calc(_ctx(erp), ErpCalcArgs(order_by=ob, **args_kw))
        assert [it["in_date"] for it in result.summary["items"]] == [
            "2026-08-01", "2026-08-02"], ob


def test_order_by_missing_value_sorts_last_both_directions() -> None:
    """D：无有效样本（value 空串）的桶**恒排最后**——升序/降序都如此。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="0", quantity=1),  # 空串
        _metal(id=2, in_date="2026-08-02", weight_per_unit="5", quantity=1),
    ]})
    args_kw = dict(entity="metal_orders", group_by="in_date",
                   measures=[{"field": "weight_per_unit", "op": "sum",
                              "alias": "重量"}])
    for ob in ("重量", "-重量"):
        result = run_erp_calc(_ctx(erp), ErpCalcArgs(order_by=ob, **args_kw))
        items = result.summary["items"]
        assert items[-1]["in_date"] == "2026-08-01", ob
        assert items[-1]["measures"][0]["value"] == ""


def test_order_by_requires_group_by_and_known_alias() -> None:
    """D：order_by 必须配 group_by；别名解析不到 → 拒绝并回列可用别名。"""
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", order_by="重量",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}])
    with pytest.raises(ValidationError) as exc:
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    order_by="-不存在", measures=[
                        {"field": "weight_per_unit", "op": "sum", "alias": "重量"}])
    assert "重量" in str(exc.value)  # error 回列本次可用别名


def test_limit_truncates_items_with_note_and_totals_unaffected() -> None:
    """D：limit 真的截断时给 note（items 是子集），且 **totals 不受影响**
    （仍是全量原始行合计，不是被截断子集之和）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=i, in_date=f"2026-08-{i:02d}", weight_per_unit=str(i), quantity=1)
        for i in range(1, 6)
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    order_by="-重量", limit=2),
    )
    s = result.summary
    assert [it["in_date"] for it in s["items"]] == ["2026-08-05", "2026-08-04"]
    assert s["count"] == 2
    # totals = 全量 1+2+3+4+5 = 15（不是被截断的 5+4 = 9）
    assert s["totals"][0]["value"] == "15"
    note = s["note"]
    assert re.search(r"前 2 条", note) and re.search(r"子集", note), (
        "note 缺「items 仅返回排序后的前 N 条子集」"
    )
    # S3 极性锁：裸子串被「不不得把本子集之和…」救活，把否定写进正则
    assert re.search(r"(?<!不)不得把本子集之和当成总计报告", note), (
        "note 缺「不得把本子集之和当成总计报告」的告诫"
    )
    # S2：note 里的**条目数口径**此前无任何断言——把「条目）」（R3 的修法）回退成
    # 「组）」必须红（旧口径会把单位桶×组的条目数误报成「组数」）
    assert re.search(r"(?<!不)全量共 \d+ 条目", note), (
        "note 缺「该筛选下全量共 N 条目」的口径（被回退成「共 N 组」必须红）"
    )
    # F6：note 元素内部**不得含「；」**（summary["note"] 是 "；".join(notes)，
    # 元素内再用「；」会被模型切成两条语义不完整的 note——calc.py 成文约定）。
    assert "；" not in note


def test_limit_without_truncation_no_note() -> None:
    """D：limit ≥ 组数（未真截断）时不加 note、items 完整。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=i, in_date=f"2026-08-{i:02d}", weight_per_unit=str(i), quantity=1)
        for i in range(1, 4)
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    limit=10),
    )
    assert result.summary["count"] == 3
    assert "note" not in result.summary


def test_limit_without_order_by_uses_existing_key_order() -> None:
    """D：只传 limit 不传 order_by → 按现有确定性键序（桶键序）取前 N。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=i, in_date=f"2026-08-{i:02d}", weight_per_unit=str(i), quantity=1)
        for i in range(1, 6)
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    limit=2),
    )
    assert [it["in_date"] for it in result.summary["items"]] == [
        "2026-08-01", "2026-08-02"]
    assert "前 2 条" in result.summary["note"]


def test_limit_range_and_requires_group_by() -> None:
    """D：limit 取 1..200（越界拒绝，不静默截断）；必须配 group_by；拒绝 bool。"""
    base = dict(entity="metal_orders", group_by="in_date",
                measures=[{"op": "count", "alias": "行数"}])
    for bad in (0, 201, -1):
        with pytest.raises(ValidationError):
            ErpCalcArgs(limit=bad, **base)
    with pytest.raises(ValidationError):
        ErpCalcArgs(entity="metal_orders", limit=5,
                    measures=[{"op": "count", "alias": "行数"}])  # 无 group_by
    with pytest.raises(ValidationError):
        ErpCalcArgs(limit=True, **base)  # bool 拒绝
    # R4：越界错误提示不得引导模型改用 export_data（与「聚合结果不进模型
    # 上下文、汇总一律本工具」纪律冲突；应引导按 filter 分批调用本工具）
    with pytest.raises(ValidationError) as ei:
        ErpCalcArgs(limit=201, **base)
    assert "export_data" not in str(ei.value)
    # 边界端点放行
    assert ErpCalcArgs(limit=1, **base).limit == 1
    assert ErpCalcArgs(limit=200, **base).limit == 200


def test_order_by_limit_top_n_calculation_column_id76_trap() -> None:
    """D **回归钉子（复刻生产 id76 陷阱）**：造一行「按整单重量进前 3、按单件
    重量排 160+」，断言 order_by="-整单重量" + limit 的 Top-N **包含它**——
    这是本批次唯一能证伪 D 是否真修好的测试。

    形态同 metal_warehouse id76：855 型 803kg×8=6424kg，按单件重量排第 162、
    按整单重量排第 3。若按单件重量排（erp_query 只收存储列、或退回按 wpu 排），
    结构上根本找不到它。
    """
    rows = [
        _metal(id=1, weight_per_unit="5000", quantity=5),   # 整单 25000
        _metal(id=2, weight_per_unit="6000", quantity=5),   # 整单 30000
    ]
    # 161 行：单件重量 > 803，但整单重量（×1）远小于 6424（id 避开 76）
    for i in range(3, 164):
        rows.append(_metal(id=1000 + i, weight_per_unit=str(1000 + i), quantity=1))
    rows.append(_metal(id=76, weight_per_unit="803", quantity=8))  # 整单 6424
    erp = CalcErp({"metal_orders": rows})
    args_kw = dict(entity="metal_orders", group_by="id", limit=10)

    # 正确口径：按整单重量（计算列）降序 → id76 进前 3
    good = run_erp_calc(_ctx(erp), ErpCalcArgs(
        measures=[{"field": "weight_per_unit", "op": "sum",
                   "multiply_field": "quantity", "alias": "整单重量"}],
        order_by="-整单重量", **args_kw))
    assert good.ok
    top_ids = [it["id"] for it in good.summary["items"]]
    assert 76 in top_ids, "按整单重量取 Top-N 竟漏掉 id76（D 未修好）"
    assert top_ids[:3] == [2, 1, 76]  # 30000 / 25000 / 6424

    # 反证：按单件重量排序（错口径）→ id76 结构上不可能进前 10
    bad = run_erp_calc(_ctx(erp), ErpCalcArgs(
        measures=[{"field": "weight_per_unit", "op": "sum", "alias": "单件重量"}],
        order_by="-单件重量", **args_kw))
    assert bad.ok
    assert 76 not in [it["id"] for it in bad.summary["items"]]


def test_time_granularity_accepts_datetime_group_by() -> None:
    """F3：datetime 型 group_by 字段也放行（ERP 元数据确有 datetime——如
    created_at/updated_at）；按月桶键只用到年月，datetime 的时分秒被
    _parse_iso_date 的 split("T") 忽略。守卫不得比桶键函数更窄。"""
    entities = [{
        "name": "metal_orders", "label": "钣金订单", "readable": True,
        "creatable": True, "updatable": True, "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "created_at": {"type": "datetime", "label": "创建时间"},
            "weight_per_unit": {"type": "decimal", "label": "单重"},
        },
    }]
    erp = CalcErp({"metal_orders": [
        {"id": 1, "created_at": "2026-08-01T10:20:30", "weight_per_unit": "1"},
        {"id": 2, "created_at": "2026-08-15T00:00:00", "weight_per_unit": "2"},
        {"id": 3, "created_at": "2026-09-01T23:59:59", "weight_per_unit": "3"},
    ]}, entities=entities)
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="created_at",
                    time_granularity="month",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}]),
    )
    assert result.ok
    by_bucket = {it["created_at"]: it["measures"][0]["value"]
                 for it in result.summary["items"]}
    assert by_bucket == {"2026-08": "3", "2026-09": "3"}


def test_time_granularity_null_date_rows_go_to_none_bucket() -> None:
    """F4：NULL 日期值是**正常业务状态**（如 metal_warehouse.out_date
    nullable，「未出厂就没有值」），照既有「无粒度」成例归入 None 桶，**不**
    fail closed、**不**当作「非 ISO 异常」；该情形附一句 note。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-05", weight_per_unit="1", quantity=1),
        _metal(id=2, in_date=None, weight_per_unit="2", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    time_granularity="month",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}]),
    )
    assert result.ok  # NULL 不 fail closed
    keys = [it["in_date"] for it in result.summary["items"]]
    assert "2026-08" in keys and None in keys  # None 桶存在
    assert "空值组" in result.summary["note"]


def test_over_groups_divisor_uses_full_items_not_limited() -> None:
    """F7：over="groups" + order_by + limit 时，normalized 的分母与 total 取
    **全量** items（未被 limit 截断）——刻意保留的全量口径闸门（换 limited_items
    必须红）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=i, in_date=f"2026-08-{i:02d}", weight_per_unit=str(i), quantity=1)
        for i in range(1, 6)
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    over="groups", order_by="-重量", limit=2),
    )
    assert result.ok
    norm = result.summary["normalized"]
    entry = norm["results"][0]
    assert entry["divisor"] == 5   # 全量组数 5，不是 limit 后的 2
    assert entry["total"] == "15"  # 全量合计 1+2+3+4+5
    assert len(result.summary["items"]) == 2  # items 仍被截断


def test_over_groups_normalized_note_discloses_null_bucket() -> None:
    """R6：over="groups" 时 NULL 日期行自成空值桶、**计入分母**（语义不变），
    只要存在 NULL 行，normalized.note 必须披露这一点（否则模型会把分母当成
    「日期型组数」而漏掉空值桶）。删除该披露必须红。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-05", weight_per_unit="3", quantity=1),
        _metal(id=2, in_date=None, weight_per_unit="2", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    time_granularity="month",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    over="groups"),
    )
    assert result.ok
    note = result.summary["normalized"]["note"]
    assert "NULL" in note
    # S1 极性锁：裸子串「计入分母」被「不计入分母」救活——把否定写进正则
    assert re.search(r"(?<!不)空值桶（[^。；（]*(?<!不)计入分母", note), (
        "normalized.note 缺「NULL 空值桶也计入分母」的披露（或语义被反转）"
    )
    # 语义不变：分母仍是「出现行的组数」= 月份桶 1 + 空值桶 1 = 2
    assert result.summary["normalized"]["results"][0]["divisor"] == 2


def test_over_groups_normalized_note_no_null_disclosure_absent() -> None:
    """R6 对照：无 NULL 行时**不得**出现该披露句（否则是无据多话）。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-05", weight_per_unit="3", quantity=1),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    time_granularity="month",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "alias": "重量"}],
                    over="groups"),
    )
    assert result.ok
    assert "空值桶" not in result.summary["normalized"]["note"]


def test_no_new_params_group_by_output_byte_identical() -> None:
    """无新参数（time_granularity/order_by/limit 全缺省）时，**有 group_by**
    路径的 items / 其余 summary 键逐字节不变（对照）：无新键、无 note。"""
    erp = CalcErp({"metal_orders": [
        _metal(id=1, in_date="2026-08-01", weight_per_unit="1", quantity=10),
        _metal(id=2, in_date="2026-08-02", weight_per_unit="2", quantity=10),
    ]})
    result = run_erp_calc(
        _ctx(erp),
        ErpCalcArgs(entity="metal_orders", group_by="in_date",
                    measures=[{"field": "weight_per_unit", "op": "sum",
                               "multiply_field": "quantity", "alias": "重量"}]),
    )
    assert result.ok
    assert json.dumps(result.summary["items"], sort_keys=True, ensure_ascii=False) == (
        '[{"in_date": "2026-08-01", "measures": [{"alias": "重量", "field": '
        '"weight_per_unit", "multiply_field": "quantity", "n": 1, "value": "10"}], '
        '"unit": "kg"}, {"in_date": "2026-08-02", "measures": [{"alias": "重量", '
        '"field": "weight_per_unit", "multiply_field": "quantity", "n": 1, '
        '"value": "20"}], "unit": "kg"}]'
    )
    assert json.dumps(
        {k: v for k, v in result.summary.items() if k != "items"},
        sort_keys=True, ensure_ascii=False,
    ) == (
        '{"count": 2, "entity": "erp_calc", "group_by": "in_date", '
        '"skipped_rows": 0, "target_entity": "metal_orders", "total_rows": 2, '
        '"totals": [{"alias": "重量", "field": "weight_per_unit", '
        '"multiply_field": "quantity", "n": 2, "unit": "kg", "value": "30"}], '
        '"unit_field": "weight_unit", "unit_values": ["kg"], "used_rows": 2}'
    )


