"""erp_salary_calc（工资口径专用计算）测试（[2026-09-13] 新增）。

覆盖 R1.salary.calculation 口径下沉到代码后的行为：
- 正常逐人计算（加班、请假、两者都有、两者都无）；
- 全天请假被 +日薪 与 −8h×时薪 抵消为 0；
- 缺参 fail closed（无考勤/缺日薪/缺每日工作时长除零）→ missing 且不算 wage；
- include_inactive 开关（离职员工默认不在名单 → 其考勤行记 missing 不静默丢）；
- totals.wage = 逐行 wage 量化后相加（可照表核对）；
- 权限/接线/元数据/字段目录 fail closed；
- 2026-07 真值核对断言：12 行正确合计 54222.78（而非模型只算考勤天数×日薪
  得到的 53870.00——加班与请假两项被整条丢失的 2026-07 实测 bug 形态）。

假 ERP（SalaryErp）：真分页 query（fields 投影 + 本地 filter 模拟 is_active/
日期范围）+ list_entities 元数据；两次查询（employees/attendance）与本地关联
由真实工具逻辑完成。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.salary import (
    ErpSalaryCalcArgs,
    run_erp_salary_calc,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.metadata import EntityCache

#: 假 ERP 实体目录：employees / attendance（字段 raw 结构与 mcp-api.md §3 一致）
_SALARY_ENTITIES: list[dict] = [
    {
        "name": "employees",
        "label": "员工",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "name": {"type": "string", "label": "姓名"},
            "daily_wage": {"type": "decimal", "label": "日工资"},
            "work_hours_per_day": {"type": "decimal", "label": "每日工作时长"},
            "is_active": {"type": "bool", "label": "在职"},
        },
    },
    {
        "name": "attendance",
        "label": "考勤记录",
        "readable": True,
        "creatable": True,
        "updatable": True,
        "deletable": False,
        "business_operations": [],
        "fields": {
            "id": {"type": "int", "label": "ID"},
            "employee_id": {"type": "int", "label": "员工ID"},
            "att_date": {"type": "date", "label": "考勤日期"},
            "leave_minutes": {"type": "int", "label": "请假分钟"},
            "overtime_minutes": {"type": "int", "label": "加班分钟"},
        },
    },
]


def _matches(row: dict, filt: dict | None) -> bool:
    """本地模拟 ERP filter（等值 + gte/lte + in），覆盖工具实际下推的形态。"""
    if not filt:
        return True
    for key, spec in filt.items():
        value = row.get(key)
        if isinstance(spec, dict):
            for op, bound in spec.items():
                if op == "gte" and not (value is not None and value >= bound):
                    return False
                if op == "lte" and not (value is not None and value <= bound):
                    return False
                if op == "eq" and value != bound:
                    return False
                if op == "in" and value not in bound:
                    return False
        elif value != spec:
            return False
    return True


class SalaryErp:
    """erp_salary_calc 假 ERP：真分页 query（fields 投影 + 本地 filter）+ 元数据。"""

    def __init__(
        self,
        rows_by_entity: dict[str, list[dict]],
        *,
        entities: list[dict] | None = None,
        page_size: int = 200,
        fail_query: bool = False,
        fail_id_in_query: bool = False,
    ):
        self.rows_by_entity = rows_by_entity
        self.entities = list(entities) if entities is not None else _SALARY_ENTITIES
        self.page_size = page_size
        self.fail_query = fail_query
        #: 仅让带 in 过滤的查询失败（模拟 G1 的成因判定补充查询失败，而
        #: employees/attendance 主查询正常）——用于验证「不丢人、工具不失败」
        self.fail_id_in_query = fail_id_in_query
        self.calls: list[dict] = []

    def call_tool(self, tool_name: str, arguments: dict | None = None,
                  operation_id: str | None = None,
                  trace_id: str | None = None) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
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
            if self.fail_id_in_query:
                has_in = any(
                    isinstance(v, dict) and "in" in v
                    for v in (args.get("filter") or {}).values()
                )
                if has_in:
                    return ToolResult(
                        ok=False,
                        error=MCPError(
                            code="MCP_SERVER_ERROR", category=ErrorCategory.SERVER,
                            message="probe query exploded",
                        ),
                    )
            entity = args.get("entity", "")
            offset = args.get("offset") or 0
            window = args.get("limit") or 200
            fields = args.get("fields")
            filt = args.get("filter")
            full = [r for r in self.rows_by_entity.get(entity, []) if _matches(r, filt)]
            page_rows = full[offset : offset + window]
            items = [
                {k: r[k] for k in fields if k in r} if fields else dict(r)
                for r in page_rows
            ]
            return ToolResult(
                ok=True,
                data={"items": items, "total": len(full),
                      "limit": window, "offset": offset},
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    def list_entities(self, operation_id: str | None = None,
                      trace_id: str | None = None) -> ToolResult:
        return self.call_tool("list_entities", {})


def _employee(eid: int, name: str, daily_wage, work_hours=8,
              is_active: bool = True) -> dict:
    return {
        "id": eid, "name": name, "daily_wage": str(daily_wage),
        "work_hours_per_day": str(work_hours), "is_active": is_active,
    }


def _att(aid: int, eid: int, day: str, *, leave: int = 0, overtime: int = 0) -> dict:
    return {
        "id": aid, "employee_id": eid, "att_date": day,
        "leave_minutes": leave, "overtime_minutes": overtime,
    }


def _ctx(erp: SalaryErp | None, *, role: str = "manager",
         scope: frozenset[str] = frozenset({"erp:query"}),
         with_context: bool = True, with_metadata: bool = True) -> ToolContext:
    return ToolContext(
        context=(
            AgentContext(user_id="manager01", user_role=role, scope=scope)
            if with_context
            else None
        ),
        tasks=None, audit=None, sink=None, client=erp,
        metadata=EntityCache(erp) if (erp is not None and with_metadata) else None,
    )


def _run(erp: SalaryErp, **kwargs) -> object:
    return run_erp_salary_calc(
        _ctx(erp),
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31", **kwargs),
    )


# ---------------------------------------------------------------------------
# pydantic 参数校验
# ---------------------------------------------------------------------------


def test_args_schema_validation() -> None:
    """日期格式 / 起止顺序 / include_inactive 缺省值。"""
    with pytest.raises(ValidationError):
        ErpSalaryCalcArgs(start_date="2026/07/01", end_date="2026-07-31")
    with pytest.raises(ValidationError):
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-13-01")
    with pytest.raises(ValidationError):
        ErpSalaryCalcArgs(start_date="2026-07-32", end_date="2026-07-31")
    with pytest.raises(ValidationError):
        ErpSalaryCalcArgs(start_date="2026-07-31", end_date="2026-07-01")  # 起晚于止
    ok = ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31")
    assert ok.include_inactive is False  # 缺省只算在职


# ---------------------------------------------------------------------------
# 正常逐人计算（加班/请假/两者都有/两者都无）
# ---------------------------------------------------------------------------


def _basic_erp() -> SalaryErp:
    employees = [
        _employee(1, "甲", 1000),   # 加班两笔
        _employee(2, "乙", 2000),   # 请假一笔
        _employee(3, "丙", 1200),   # 加班 + 请假
        _employee(4, "丁", 800),    # 无加班无请假
    ]
    attendance = [
        _att(1, 1, "2026-07-01", overtime=120),
        _att(2, 1, "2026-07-02", overtime=60),      # 甲：days=2, ot=3h
        _att(3, 2, "2026-07-01", leave=240),        # 乙：days=1, lv=4h
        _att(4, 3, "2026-07-01", overtime=30),
        _att(5, 3, "2026-07-02", leave=90),
        _att(6, 3, "2026-07-03"),                   # 丙：days=3, ot=0.5h, lv=1.5h
        _att(7, 4, "2026-07-01"),                   # 丁：days=1
    ]
    return SalaryErp({"employees": employees, "attendance": attendance})


def test_basic_per_employee_calculation() -> None:
    """逐人 wage = 天数×日薪 + 时薪×加班 − 时薪×请假；输出结构与量化口径。"""
    result = _run(_basic_erp())
    assert result.ok, result.summary
    s = result.summary
    assert s["entity"] == "erp_salary_calc"
    assert s["count"] == 4
    assert s["total_rows"] == 7
    by_id = {it["employee_id"]: it for it in s["items"]}
    assert set(by_id) == {1, 2, 3, 4}
    # 甲：2×1000 + 125×3 = 2375
    assert by_id[1]["days"] == 2
    assert by_id[1]["daily_wage"] == "1000"
    assert by_id[1]["work_hours_per_day"] == "8"
    assert by_id[1]["overtime_hours"] == "3"
    assert by_id[1]["leave_hours"] == "0"
    assert by_id[1]["wage"] == "2375"
    # 乙：1×2000 − 250×4 = 1000
    assert by_id[2]["leave_hours"] == "4"
    assert by_id[2]["wage"] == "1000"
    # 丙：3×1200 + 150×0.5 − 150×1.5 = 3450
    assert by_id[3]["overtime_hours"] == "0.5"
    assert by_id[3]["leave_hours"] == "1.5"
    assert by_id[3]["wage"] == "3450"
    # 丁：1×800 = 800
    assert by_id[4]["wage"] == "800"
    # totals 为逐行量化后求和
    assert s["totals"]["wage"] == "7625"
    assert s["totals"]["days"] == 7
    assert s["totals"]["overtime_hours"] == "3.5"
    assert s["totals"]["leave_hours"] == "5.5"
    assert s["missing"] == []


def test_totals_equals_sum_of_row_wages() -> None:
    """totals.wage == 逐行 wage 相加（可照表核对；量化后相加）。"""
    result = _run(_basic_erp())
    assert result.ok
    s = result.summary
    row_sum = sum((Decimal(it["wage"]) for it in s["items"]), Decimal(0))
    assert Decimal(s["totals"]["wage"]) == row_sum
    assert Decimal(s["totals"]["days"]) == sum(it["days"] for it in s["items"])


def test_full_day_leave_offsets_to_zero() -> None:
    """全天请假（480 分钟 = 8h）被 +日薪 与 −8h×时薪 抵消为 0，仍在 items。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [_att(1, 1, "2026-07-01", leave=480)],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 1
    item = s["items"][0]
    assert item["leave_hours"] == "8"
    assert item["wage"] == "0"
    assert s["totals"]["wage"] == "0"
    assert s["missing"] == []


# ---------------------------------------------------------------------------
# 舍入口径：逐人 wage 半分位取偶（ROUND_HALF_EVEN），对齐 ERP hr.py:443
# ---------------------------------------------------------------------------
# 为什么是 HALF_EVEN：ERP 权威实现 hengfa-erp/backend/app/api/hr.py:443 写的是
# `round(net_pay, 2)`，net_pay 是 Decimal → Decimal.__round__ 走默认上下文 →
# ROUND_HALF_EVEN（银行家舍入）。本工具的唯一存在理由是复现 ERP 工资口径，逐人
# 工资必须与系统记录逐分一致，故 salary.py 的 _WAGE_QUANTIZE 用 HALF_EVEN。
# 注意：恰在半分位且「末位为偶」是**唯一**能区分 HALF_EVEN 与 HALF_UP 的形态
# （末位为奇时两者同向进位、结果相同），故下面的钉子分别取正/负两种符号方向
# ——负号下 HALF_EVEN 取偶会使结果比 HALF_UP 更大，与正号相反，两个方向都能
# 抓住「有人把 HALF_EVEN 顺手改回 HALF_UP」的回归。


def test_wage_half_even_matches_erp_tie_down() -> None:
    """对齐钉子：daily_wage=100.25 / 8h / 1 天 / 加班 144 分钟。

    全精度 net_pay = 100.25 + (100.25/8)*2.4 = 130.325，恰在半分位且末位为偶
    → HALF_EVEN 取偶得 **130.32**（与 hr.py:443 的 round(Decimal,2) 一致）；
    HALF_UP 会得 130.33（错误）。本断言钉住与 ERP 系统记录逐分一致。
    """
    erp = SalaryErp({
        "employees": [_employee(1, "甲", "100.25")],
        "attendance": [_att(1, 1, "2026-07-01", overtime=144)],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    item = s["items"][0]
    assert item["overtime_hours"] == "2.4"   # 144 分钟 = 2.4 小时
    # 半分位取偶，与 hr.py:443 对齐：130.325 → 130.32（不是 HALF_UP 的 130.33）
    assert item["wage"] == "130.32"
    # 展示列：由已量化的 2 位 wage=130.32 再舍 1 位 HALF_UP → 130.3
    assert item["wage_display"] == "130.3"
    assert s["totals"]["wage"] == "130.32"


def test_wage_half_even_negative_tie_opposite_direction() -> None:
    """反向钉子（负号）：daily_wage=-100.25 → net_pay = -130.325，同样恰在半
    分位且末位为偶 → HALF_EVEN = **-130.32**；HALF_UP（远离零）会得 -130.33。

    正号钉子中 HALF_EVEN 比 HALF_UP 小，负号钉子中 HALF_EVEN 比 HALF_UP 大
    ——两个符号方向都能抓住改回 HALF_UP 的回归。负日薪是合法数据（红冲/调整，
    见 test_negative_daily_wage_still_computed），故本形态真实可达。
    """
    erp = SalaryErp({
        "employees": [_employee(1, "甲", "-100.25")],
        "attendance": [_att(1, 1, "2026-07-01", overtime=144)],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    item = s["items"][0]
    assert item["wage"] == "-130.32"   # HALF_EVEN（取偶）；HALF_UP 会是 -130.33
    assert s["totals"]["wage"] == "-130.32"


def test_wage_non_tie_rounding_unaffected() -> None:
    """非半分位输入（上舍/下舍各一例）：结果与改 HALF_EVEN 之前一致，证明只
    改了恰在半分位的行为，普通舍入不受影响。"""
    erp = SalaryErp({
        # 上舍：1.3×100.02 = 130.026（第三位 6，非半分位）→ 130.03
        # 下舍：1.3×100.08 = 130.104（第三位 4，非半分位）→ 130.10
        "employees": [_employee(1, "上", "100.02"), _employee(2, "下", "100.08")],
        "attendance": [
            _att(1, 1, "2026-07-01", overtime=144),
            _att(2, 2, "2026-07-01", overtime=144),
        ],
    })
    result = _run(erp)
    assert result.ok
    by_id = {it["employee_id"]: it["wage"] for it in result.summary["items"]}
    assert by_id[1] == "130.03"   # 上舍；两种舍入模式一致
    assert by_id[2] == "130.1"    # 下舍（_fmt_decimal 去尾零：130.10 → 130.1）


# ---------------------------------------------------------------------------
# 展示层 wage_display（1 位小数，ROUND_HALF_UP，纯展示）
# ---------------------------------------------------------------------------
# 与计算层 wage（2 位 HALF_EVEN）刻意不同：wage_display 是把**已量化的 wage**
# 再舍一次成 1 位。展示层用 HALF_UP（用户口径「四舍五入到 1 位」），与计算层的
# HALF_EVEN 无关，勿「顺手统一」。下面三条分别钉住：合计用权威值而非展示值、
# 展示层 HALF_UP 进位、展示值派生自量化后的 wage 而非全精度值。


def test_totals_uses_authoritative_wage_not_display() -> None:
    """totals.wage 等于逐人 **wage（2 位）** 之和，且**不等于**逐人
    wage_display（1 位展示值）之和——证明合计用的是权威值，而非把展示列相加。

    甲 net=130.325 → wage=130.32、display=130.3；乙 net=130.364 →
    wage=130.36、display=130.4。Σwage=260.68；Σdisplay=260.70（每组差异让两者
    不同，断言有区分力）。
    """
    erp = SalaryErp({
        "employees": [_employee(1, "甲", "100.25"), _employee(2, "乙", "100.28")],
        "attendance": [
            _att(1, 1, "2026-07-01", overtime=144),  # 甲：net=130.325
            _att(2, 2, "2026-07-01", overtime=144),  # 乙：net=130.364
        ],
    })
    result = _run(erp)
    assert result.ok, result.summary
    s = result.summary
    by_id = {it["employee_id"]: it for it in s["items"]}
    assert by_id[1]["wage"] == "130.32" and by_id[1]["wage_display"] == "130.3"
    assert by_id[2]["wage"] == "130.36" and by_id[2]["wage_display"] == "130.4"
    sum_wage = sum((Decimal(it["wage"]) for it in s["items"]), Decimal(0))
    sum_display = sum((Decimal(it["wage_display"]) for it in s["items"]), Decimal(0))
    assert sum_wage == Decimal("260.68")
    assert sum_display == Decimal("260.70")
    assert sum_wage != sum_display  # 断言有区分力：两种口径确实不同
    # totals 用权威值，不是展示值相加
    assert Decimal(s["totals"]["wage"]) == sum_wage
    assert Decimal(s["totals"]["wage"]) != sum_display


def test_wage_display_uses_half_up_not_half_even() -> None:
    """展示层半分位钉子：2 位 wage 末位恰为 5 且十分位为偶数 → HALF_UP 进位，
    HALF_EVEN 会取偶向下——证明展示层用 HALF_UP（与计算层 HALF_EVEN 不同）。

    daily_wage=130.25 / 8h / 1 天 / 无加班请假 → net=130.25（精确）→
    wage（2 位 HALF_EVEN）=130.25 → wage_display（HALF_UP）应进位为 **130.3**；
    若误用 HALF_EVEN 会得 130.2（十分位 2 取偶）。这正是最容易被后人「顺手
    统一」的地方。
    """
    erp = SalaryErp({
        "employees": [_employee(1, "甲", "130.25")],
        "attendance": [_att(1, 1, "2026-07-01")],
    })
    result = _run(erp)
    assert result.ok
    item = result.summary["items"][0]
    assert item["wage"] == "130.25"
    assert item["wage_display"] == "130.3"  # HALF_UP；HALF_EVEN 会是 130.2


def test_wage_display_derived_from_quantized_wage() -> None:
    """展示值派生自**量化后的 wage**（先舍 2 位再舍 1 位），而非从全精度值直接
    舍 1 位——两者结果不同的形态钉住。

    daily_wage=130.349 / 8h / 1 天 / 无加班请假 → 全精度 net=130.349：
      * 先舍 2 位 HALF_EVEN → wage=130.35，再舍 1 位 HALF_UP → 展示 130.4；
      * 若改从全精度直接舍 1 位（130.349 → 0.349<0.35 舍去）→ 130.3（错）。
    断言展示为 130.4，证明它确实是从已量化 wage 再舍（权威值的忠实缩短）。
    """
    erp = SalaryErp({
        "employees": [_employee(1, "甲", "130.349")],
        "attendance": [_att(1, 1, "2026-07-01")],
    })
    result = _run(erp)
    assert result.ok
    item = result.summary["items"][0]
    assert item["wage"] == "130.35"       # 2 位权威值
    assert item["wage_display"] == "130.4"  # 从 130.35 再舍 1 位；直接舍得 130.3


# ---------------------------------------------------------------------------
# 缺参 fail closed
# ---------------------------------------------------------------------------


def test_zero_work_hours_missing_no_wage() -> None:
    """每日工作时长 0 → 除零，绝不跳过除法：进 missing 且不产出 wage。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000, work_hours=0)],
        "attendance": [_att(1, 1, "2026-07-01")],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 0 and s["items"] == []
    assert s["totals"]["wage"] == "0"
    assert len(s["missing"]) == 1
    assert s["missing"][0]["employee_id"] == 1
    assert "缺每日工作时长" in s["missing"][0]["reason"]


def test_missing_daily_wage() -> None:
    """日薪 0/空 → 进 missing「员工资料缺日薪」，不算 wage。"""
    for wage in (0, ""):
        erp = SalaryErp({
            "employees": [_employee(1, "甲", wage, work_hours=8)],
            "attendance": [_att(1, 1, "2026-07-01")],
        })
        result = _run(erp)
        assert result.ok
        s = result.summary
        assert s["count"] == 0 and s["items"] == []
        assert "缺日薪" in s["missing"][0]["reason"]


def test_no_attendance_missing() -> None:
    """该时段无考勤记录 → 进 missing「该时段无考勤记录」。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [_att(1, 1, "2026-06-30")],  # 不在时段内
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 0
    assert s["total_rows"] == 0
    assert s["missing"][0]["reason"] == "该时段无考勤记录"


# ---------------------------------------------------------------------------
# F1 考勤覆盖度（coverage 常驻 + 首尾缺口告警）
# ---------------------------------------------------------------------------


def test_coverage_partial_first_days_triggers_note() -> None:
    """考勤只覆盖时段前 5 天：coverage 数字正确 + note 提示覆盖不全。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [_att(i, 1, f"2026-07-0{i}") for i in range(1, 6)],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    cov = s["coverage"]
    assert cov["first_att_date"] == "2026-07-01"
    assert cov["last_att_date"] == "2026-07-05"
    assert cov["distinct_days"] == 5  # 去重后的考勤日期数
    assert cov["period_days"] == 31  # 7-01..7-31
    assert "考勤记录仅覆盖 2026-07-01 至 2026-07-05" in s["note"]
    assert "工资可能不完整" in s["note"]


def test_coverage_first_last_present_but_gaps_no_note() -> None:
    """考勤覆盖首尾但中间缺（只录工作日形态）：note **不**触发（正常数据），
    coverage 数字仍常驻供自行判断。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [
            _att(1, 1, "2026-07-01"),
            _att(2, 1, "2026-07-15"),
            _att(3, 1, "2026-07-31"),
        ],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    cov = s["coverage"]
    assert cov["first_att_date"] == "2026-07-01"
    assert cov["last_att_date"] == "2026-07-31"
    assert cov["distinct_days"] == 3  # 天数少于 period_days，但不报警
    assert cov["period_days"] == 31
    assert "考勤记录仅覆盖" not in s["note"]
    assert "工资可能不完整" not in s["note"]


def test_coverage_no_attendance_none_and_note() -> None:
    """完全无考勤行：coverage 的 first/last 为 None、distinct_days=0 + note 触发。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [_att(1, 1, "2026-06-30")],  # 时段外
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    cov = s["coverage"]
    assert cov["first_att_date"] is None
    assert cov["last_att_date"] is None
    assert cov["distinct_days"] == 0
    assert cov["period_days"] == 31
    assert "无任何考勤记录" in s["note"]
    assert "工资可能不完整" in s["note"]


# ---------------------------------------------------------------------------
# F2 分母非正 fail closed（负每日工作时长；负日薪不拦）
# ---------------------------------------------------------------------------


def test_negative_work_hours_missing_no_wage() -> None:
    """每日工作时长为负（分母非正，数据异常）→ 进 missing（独立 reason）
    且不产出 wage；与 0/缺值的 reason 区分开。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 800, work_hours=-8)],
        "attendance": [_att(1, 1, "2026-07-01")],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 0 and s["items"] == []
    assert s["totals"]["wage"] == "0"
    assert len(s["missing"]) == 1
    assert s["missing"][0]["employee_id"] == 1
    assert s["missing"][0]["reason"] == "员工资料每日工作时长非正"
    assert s["missing"][0]["reason"] != "员工资料缺每日工作时长"


def test_negative_daily_wage_still_computed() -> None:
    """日薪为负**不拦**（被乘数，公式仍成立）：仍在 items 正常计算，不进 missing。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", -1000, work_hours=8)],
        "attendance": [_att(1, 1, "2026-07-01"), _att(2, 1, "2026-07-02")],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 1 and s["missing"] == []
    item = s["items"][0]
    assert item["daily_wage"] == "-1000"
    assert item["days"] == 2
    assert item["wage"] == "-2000"  # 2 × (-1000)


# ---------------------------------------------------------------------------
# include_inactive 开关 + 陌生 employee_id 不静默丢弃
# ---------------------------------------------------------------------------


def _with_inactive() -> SalaryErp:
    employees = [
        _employee(1, "在职", 1000, is_active=True),
        _employee(2, "离职", 1000, is_active=False),
    ]
    attendance = [
        _att(1, 1, "2026-07-01"),
        _att(2, 2, "2026-07-01"),  # 离职员工的考勤行
    ]
    return SalaryErp({"employees": employees, "attendance": attendance})


def test_include_inactive_false_excludes_and_reports_missing() -> None:
    """默认只在职：离职员工的考勤行进 missing（不静默丢弃），成因判为「已
    离职」并给出可执行的「勾选含离职」提示；employees 主查询下推 is_active，
    追加的成因判定查询用 id in 且**不带 is_active**（软删会被它挡掉）。"""
    erp = _with_inactive()
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["count"] == 1
    assert [it["employee_id"] for it in s["items"]] == [1]
    assert len(s["missing"]) == 1
    assert s["missing"][0]["employee_id"] == 2
    assert s["missing"][0]["name"] == "离职"  # 判定查询取回了姓名
    assert "已离职" in s["missing"][0]["reason"]
    assert "勾选含离职" in s["missing"][0]["reason"]  # 可执行提示
    assert "档案已删除" not in s["missing"][0]["reason"]
    # employees 主查询确实下推了 is_active=true
    emp_calls = [c for c in erp.calls if c["tool"] == "query"
                 and c["arguments"]["entity"] == "employees"]
    assert emp_calls[0]["arguments"]["filter"] == {"is_active": True}
    # 第三条：成因判定补充查询（id in […]，不带 is_active 过滤）
    probe = emp_calls[1]["arguments"]
    assert probe["filter"] == {"id": {"in": [2]}}
    assert "is_active" not in probe["filter"]


def test_soft_deleted_employee_reports_deleted_not_inactive() -> None:
    """已软删员工：ERP 对 employees 无条件追加 deleted_at IS NULL，勾
    include_inactive=True 也取不回——成因判定查询返回集合里没有该 id →
    reason 判为「档案已删除或不存在」（不误导用户去勾含离职开关）。"""
    erp = SalaryErp({
        # 员工名单里没有 id=2（已软删，被 ERP deleted_at IS NULL 挡掉）
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [_att(1, 1, "2026-07-01"), _att(2, 2, "2026-07-01")],
    })
    result = _run(erp)  # 即便勾含离职也一样
    assert result.ok
    s = result.summary
    assert s["count"] == 1
    assert s["missing"][0]["employee_id"] == 2
    assert "档案已删除" in s["missing"][0]["reason"]
    assert "已离职" not in s["missing"][0]["reason"]
    # 判定查询确实发了且不带 is_active
    probe = [c for c in erp.calls if c["tool"] == "query"
             and c["arguments"]["entity"] == "employees"][1]["arguments"]
    assert probe["filter"] == {"id": {"in": [2]}}


def test_probe_query_failure_keeps_missing_and_tool_ok() -> None:
    """G1 追加的成因判定查询失败：工具不失败、**一个人都不丢**——退回合并
    文案并在 note 说明查询失败（绝不因判定失败而丢人）。"""
    erp = SalaryErp(
        {
            "employees": [_employee(1, "甲", 1000)],
            "attendance": [_att(1, 1, "2026-07-01"), _att(2, 2, "2026-07-01")],
        },
        fail_id_in_query=True,
    )
    result = _run(erp)
    assert result.ok  # 工具不因判定查询失败而失败
    s = result.summary
    assert s["count"] == 1
    assert len(s["missing"]) == 1  # 一个都不丢
    assert s["missing"][0]["employee_id"] == 2
    assert "不在本次统计范围" in s["missing"][0]["reason"]
    assert "补充查询失败" in s["note"]


def test_include_inactive_true_includes_all() -> None:
    """include_inactive=True：离职员工一并纳入，employees 查询不带 is_active 过滤。"""
    erp = _with_inactive()
    result = _run(erp, include_inactive=True)
    assert result.ok
    s = result.summary
    assert s["count"] == 2
    assert {it["employee_id"] for it in s["items"]} == {1, 2}
    assert s["missing"] == []
    emp_call = [c for c in erp.calls if c["tool"] == "query"
                and c["arguments"]["entity"] == "employees"][0]
    assert "filter" not in emp_call["arguments"]


def test_unknown_employee_id_not_silently_dropped() -> None:
    """考勤行里的陌生 employee_id（员工集合里没有且成因判定也查不到）→ 记
    missing、不静默丢，判定为「档案已删除或不存在」。"""
    erp = SalaryErp({
        "employees": [_employee(1, "甲", 1000)],
        "attendance": [
            _att(1, 1, "2026-07-01"),
            _att(2, 99, "2026-07-01"),  # 员工 99 不在名单
            _att(3, 99, "2026-07-02"),  # 同一陌生 id 多行 → 只记一条
        ],
    })
    result = _run(erp)
    assert result.ok
    s = result.summary
    assert s["total_rows"] == 3  # 全部考勤行仍计入 total_rows
    missing_ids = [m["employee_id"] for m in s["missing"]]
    assert missing_ids == [99]  # 只记一条，不重复
    assert "档案已删除" in s["missing"][0]["reason"]


# ---------------------------------------------------------------------------
# 权限与接线 fail closed
# ---------------------------------------------------------------------------


def test_permission_gates_fail_closed_no_erp_call() -> None:
    """非 manager / 缺 erp:query / 无上下文 → 拒绝且不触达 ERP。"""
    for kwargs in (dict(role="developer"), dict(scope=frozenset()),
                   dict(with_context=False)):
        erp = SalaryErp({"employees": [], "attendance": []})
        result = run_erp_salary_calc(
            _ctx(erp, **kwargs),
            ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31"),
        )
        assert result.ok is False, kwargs
        assert "权限不足" in result.summary["error"]
        assert erp.calls == []


def test_wiring_missing_fail_closed() -> None:
    """client/metadata 未接线：明确报错不静默。"""
    no_client = run_erp_salary_calc(
        _ctx(None),
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31"),
    )
    assert no_client.ok is False
    assert "客户端未接线" in no_client.summary["error"]

    erp = SalaryErp({"employees": [], "attendance": []})
    no_metadata = run_erp_salary_calc(
        _ctx(erp, with_metadata=False),
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31"),
    )
    assert no_metadata.ok is False
    assert "元数据未接线" in no_metadata.summary["error"]
    assert erp.calls == []


# ---------------------------------------------------------------------------
# 元数据 / 字段目录 fail closed
# ---------------------------------------------------------------------------


def test_entity_not_in_metadata_fail_closed() -> None:
    """白名单内实体在元数据中缺失 → 拒绝计算。"""
    erp = SalaryErp(
        {"employees": [], "attendance": []},
        entities=[e for e in _SALARY_ENTITIES if e["name"] != "attendance"],
    )
    result = run_erp_salary_calc(
        _ctx(erp),
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31"),
    )
    assert result.ok is False
    assert "attendance" in result.summary["error"]
    assert "不在元数据" in result.summary["error"]


def test_missing_field_in_catalog_fail_closed() -> None:
    """employees 缺 daily_wage 字段 → 字段目录校验拒绝（不猜字段）。"""
    entities = [
        {"name": "employees", "label": "员工", "fields": {
            "id": {"type": "int"}, "name": {"type": "string"},
            "work_hours_per_day": {"type": "decimal"}, "is_active": {"type": "bool"},
        }},
        _SALARY_ENTITIES[1],
    ]
    erp = SalaryErp({"employees": [], "attendance": []}, entities=entities)
    result = run_erp_salary_calc(
        _ctx(erp),
        ErpSalaryCalcArgs(start_date="2026-07-01", end_date="2026-07-31"),
    )
    assert result.ok is False
    assert "daily_wage" in result.summary["error"]
    assert "不在实体字段目录" in result.summary["error"]


def test_query_failure_fail_closed() -> None:
    """ERP query 失败 → ok=False，不返回部分结果。"""
    erp = SalaryErp(
        {"employees": [_employee(1, "甲", 1000)], "attendance": []},
        fail_query=True,
    )
    result = _run(erp)
    assert result.ok is False
    assert "查询失败" in result.summary["error"]


# ---------------------------------------------------------------------------
# 取数形态（两次查询 + 本地关联，不下带关联列）
# ---------------------------------------------------------------------------


def test_two_queries_with_expected_fields_and_filter() -> None:
    """employees + attendance 各查一次；attendance 带 att_date 闭区间 filter；
    取数字段含计算所需全部列（姓名映射本地做，不二次查询）。"""
    erp = _basic_erp()
    result = _run(erp)
    assert result.ok
    query_calls = [c for c in erp.calls if c["tool"] == "query"]
    assert [c["arguments"]["entity"] for c in query_calls] == [
        "employees", "attendance",
    ]
    emp_args = query_calls[0]["arguments"]
    assert set(emp_args["fields"]) == {
        "id", "name", "daily_wage", "work_hours_per_day", "is_active",
    }
    att_args = query_calls[1]["arguments"]
    assert set(att_args["fields"]) == {
        "id", "employee_id", "att_date", "leave_minutes", "overtime_minutes",
    }
    assert att_args["filter"] == {
        "att_date": {"gte": "2026-07-01", "lte": "2026-07-31"},
    }


# ---------------------------------------------------------------------------
# 2026-07 真值核对（12 行 → 54222.78）
# ---------------------------------------------------------------------------


def test_july_truth_total_54222_78() -> None:
    """12 名员工的等价夹具：正确合计 54222.78（加班/请假两项计入）。

    2026-07 财报 bug：模型只能算「考勤天数 × 日薪」，加班与请假整条丢失，
    报 53870.00；按 R1 公式应为 54222.78。本夹具构造 12 行（含加班与请假，
    差额 352.78 由此产生）以钉住口径——若公式再次退化成只算天数×日薪，本
    断言立即失败。夹具为等价构造（非真实逐人数据，真实数据不可得）。
    """
    rows = [
        # (emp_id, daily_wage, days, overtime_minutes, leave_minutes)
        (1, 3000, 4, 0, 0),
        (2, 3000, 3, 0, 0),
        (3, 2000, 2, 120, 0),
        (4, 2000, 2, 0, 60),
        (5, 1500, 1, 30, 30),
        (6, 1000, 1, 0, 0),
        (7, 1000, 1, 0, 0),
        (8, 800, 1, 0, 0),
        (9, 800, 1, 0, 0),
        (10, 600, 1, 0, 120),
        (11, 600, 1, 0, 0),
        (12, Decimal("3137.13"), 6, 0, 0),
    ]
    employees = [_employee(eid, f"E{eid}", dw) for eid, dw, *_ in rows]
    attendance: list[dict] = []
    aid = 0
    for eid, _dw, days, ot, lv in rows:
        for d in range(days):
            aid += 1
            # 加班/请假记在该员工最后一行（不影响逐人合计）
            last = d == days - 1
            attendance.append(_att(
                aid, eid, f"2026-07-{d + 1:02d}",
                overtime=ot if last else 0,
                leave=lv if last else 0,
            ))
    erp = SalaryErp({"employees": employees, "attendance": attendance})
    result = _run(erp)
    assert result.ok, result.summary
    s = result.summary
    assert s["count"] == 12
    assert s["total_rows"] == 24
    assert s["totals"]["wage"] == "54222.78"
    # 与「只算考勤天数×日薪」（丢加班/请假两项）的退化口径对比：本夹具的
    # 退化值 = 54122.78。断言正确值不等于退化值——公式一旦退化成只算
    # 天数×日薪（命中 54122.78），本断言立即失败。（旧断言 != "53870"
    # 恒真、是空转测试：53870 既非正确值也非本夹具退化值。）
    # 注意：本夹具为等价构造（非真实逐人数据，真实数据不可得），
    # 54122.78 是该构造下的退化值，非生产实测值。
    assert s["totals"]["wage"] != "54122.78"
