"""erp_salary_calc 工具（只读，工资口径专用计算）。

为什么单做一个工具，而不是给 erp_calc 加通用能力：工资是**业务口径**而非
通用算子。R1.salary.calculation 的公式为

    工资 = 考勤天数 × 日薪 + 时薪 × 加班时长 − 时薪 × 请假时长
    时薪 = 日薪 ÷ 每日工作时长
    加班时长 = 加班分钟 ÷ 60      请假时长 = 请假分钟 ÷ 60

erp_calc 缺三个能力（没有除法、单实体、measure 之间不能相加减），逐人算工资
根本拼不出来——2026-07 财报实测工资一项 12 行错 11 行，模型只能算出
「考勤天数 × 日薪」，加班与请假两项整个丢失（合计报 53870.00，按公式应为
54222.78）。故把整条口径下沉进本工具：模型只需声明时段，除法与逐人计算全部
由代码完成。

本文件的算法与 R1.salary.calculation 正文同步（**不在此钉版本号**：规则包每次
升版都会让写死的版本变陈旧，钉版本只会周期性说谎）；规则正文（
prompts/policy_texts/R1.salary.calculation.md）变更须同步本文件。

取数与口径要点：
- 两次查询：employees（取 id/name/daily_wage/work_hours_per_day/is_active；
  include_inactive=False 时加 filter is_active=true）+ attendance（取
  id/employee_id/att_date/leave_minutes/overtime_minutes，filter att_date
  闭区间）；员工姓名映射本地完成（employees 已全量取回，不再发第二次查询）。
- ERP 查询不支持带出关联列（ERP 侧只 select 本表列），但支持 in 操作符批量
  取——故必须 agent 侧分两步查再本地关联。
- 考勤写入是「按整份员工名单、每人每天一行」（tools/attendance.py
  attendance_plan_execute）——全天请假的行会同时被 +日薪 与 −8h×时薪 抵消为
  0，故 考勤天数 = 该员工该时段的考勤行数 与 R1 自洽，不需要为请假做特殊排除。
- 缺参 fail closed（R1 第 5 条：不得用猜测值）：该时段无考勤 / 缺日薪 /
  缺每日工作时长（除零或为负，绝不许跳过除法继续算）→ 该员工不进 items、
  不计入 totals，进 missing 并带 reason。分母（每日工作时长）非正一律拦
  （0/缺值 / 负值各带 reason）；被乘数日薪为负**不拦**（公式仍成立，
  红冲/调整类数据可能出现负日薪）。
- 考勤覆盖度（coverage）常驻 summary：给出首尾考勤日期 / 去重天数 / 时段
  天数；只在考勤没盖住时段首尾（含完全无考勤行）时 note 追加「工资可能
  不完整」告警；不因 distinct_days < period_days 报警（只录工作日是正常
  数据，按天数不等报警会每月误报）。
- 员工全量取回后按 id 索引；考勤行里出现不在员工集合里的 employee_id →
  记入 missing，**不静默丢弃**。成因区分：仅当确有此类 id 时才追加一次
  employees 查询（filter id in [...]、不带 is_active 过滤）——返回的判为
  「已离职」（仅 include_inactive=False；勾含离职可取回），未返回的判为
  「档案已删除或不存在」（软删被 ERP deleted_at IS NULL 挡掉，勾含离职也
  取不回，故不误导用户去勾）。追加查询失败不让工具失败，退回合并文案并在
  note 说明，**一个都不丢**。
- **本工具存在两种工资精度，用途不同，勿「统一」掉**（展示
  列，用户口径「只入到小数点后一位」）：
  * `wage`（2 位小数，ROUND_HALF_EVEN）——与 ERP 系统记录**逐分一致的权威
    值**，用于核对与合计；见 `_WAGE_QUANTIZE` 注释。
  * `wage_display`（1 位小数，ROUND_HALF_UP）——**纯展示**，给用户报每人工资
    时用它；由**已量化的 `wage` 再舍一次**得到（是权威值的忠实缩短），
    见 `_WAGE_DISPLAY_QUANTIZE` 注释。
  计算与合计**一切不变**：逐人 wage 仍 2 位 HALF_EVEN、totals.wage 仍是逐人
  2 位值相加；「1 位小数」只作用于展示，**不影响任何计算**。
- 计算全用 Python Decimal（ERP decimal 字段经 JSON 序列化为字符串，
  mcp-api.md §11）；时薪/时长等中间量保持全精度、只在输出时量化。另两个展示
  量 overtime_hours/leave_hours 的 4 位量化用 ROUND_HALF_UP，那是我们自己的
  展示约定，无 ERP 口径可对齐（ERP 直接返回未舍入 float）。
- **totals.wage = 各 items 的 wage（2 位权威值）逐行求和**：报告工资合计一律以
  totals.wage 为准。**注意展示列 wage_display 逐行相加 ≠ totals.wage**（展示
  被压成 1 位，每人最多差 0.05 元），不得用展示列相加当合计。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary
from administrative_agent.agent.tools.calc import (
    _AVG_QUANTIZE,
    _cell_decimal,
    _fetch_all_rows,
    _fmt_decimal,
)
from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

#: 参与计算的两个实体（读白名单内；[2026-09-12 只读扩展] attendance 已开只读）
_EMPLOYEES_ENTITY = "employees"
_ATTENDANCE_ENTITY = "attendance"

#: 两实体取数字段集合（代码内确定，不受模型影响）
_EMPLOYEE_FIELDS: tuple[str, ...] = (
    "id", "name", "daily_wage", "work_hours_per_day", "is_active",
)
_ATTENDANCE_FIELDS: tuple[str, ...] = (
    "id", "employee_id", "att_date", "leave_minutes", "overtime_minutes",
)

#: 最终工资量化精度（2 位，舍入 = ROUND_HALF_EVEN）。
#:
#: **刻意对齐 ERP，不是笔误**：ERP 权威实现 hengfa-erp/backend/app/api/hr.py:443
#: 写的是 `round(net_pay, 2)`，而 net_pay 是 Decimal → `Decimal.__round__` 走
#: 默认上下文 → **ROUND_HALF_EVEN（银行家舍入）**。本工具的唯一存在理由是复现
#: ERP 工资口径，逐人工资必须与系统记录**逐分一致**，故这里必须用 HALF_EVEN。
#: 金额惯例通常是 HALF_UP，后来者看到 HALF_EVEN 极易「顺手改回 HALF_UP」——
#: 那会让恰在半分位上的工资每约 219 人次错 1 分（实测分歧率约 0.457%），
#: 例如 daily_wage=100.25 / 8h / 1 天 / 加班 144 分钟：
#:   全精度 net_pay = 130.325 → HALF_EVEN = 130.32（ERP） / HALF_UP = 130.33（错）。
#: 改回 HALF_UP 前请先读 hr.py:443 并想清楚为什么。
#:
#: 注意：以下 `overtime_hours`/`leave_hours` 的 `_AVG_QUANTIZE` 4 位量化仍用
#: HALF_UP——ERP 对这两项**根本不舍入**（hr.py 直接返回未舍入 float），那是
#: 我们自己的展示约定，不存在「ERP 口径」需要对齐，故保持原样。
_WAGE_QUANTIZE = Decimal("0.01")

#: 展示工资量化精度（1 位小数，舍入 = ROUND_HALF_UP）——**纯展示，不参与计算**。
#:
#: 与 `_WAGE_QUANTIZE` 的分工（**两处舍入刻意不同，勿混为一谈、勿顺手统一**）：
#:   * `_WAGE_QUANTIZE`（2 位 / HALF_EVEN）：**计算层**，逐人 wage 与 ERP 逐分
#:     一致，是权威值；容差由 ERP 权威实现决定，与本常量无关。
#:   * 本常量（1 位 / HALF_UP）：**展示层**，把**已经 2 位量化好的 wage**再舍
#:     一次成 1 位，供向用户逐人展示。
#: 为什么展示层用 HALF_UP 而计算层用 HALF_EVEN：用户口径是「四舍五入只到 1 位」
#: = HALF_UP；展示层的输入就是权威值 wage 本身，只是把它缩短，方向以用户常识
#: 为准，与 ERP 口径**无关**（ERP 根本不报 1 位，无「1 位口径」可对齐）。
#: **务必从已量化的 wage 再舍**，不要从全精度值直接舍——否则展示值与权威值可能
#: 在边界/方向上打架（如全精度 net=130.349：先舍 2 位 HALF_EVEN 得 130.35 →
#: 1 位 HALF_UP 得 130.4；而直接从全精度舍 1 位却得 130.3）。展示永远应是权威值
#: 的忠实缩短。
_WAGE_DISPLAY_QUANTIZE = Decimal("0.1")

#: 缺参原因文案（与 R1 第 5 条的「不得用猜测值」显式对应）
_REASON_NO_ATTENDANCE = "该时段无考勤记录"
_REASON_NO_DAILY_WAGE = "员工资料缺日薪"
_REASON_NO_WORK_HOURS = "员工资料缺每日工作时长"
#: 分母（每日工作时长）为负——数据异常，独立 reason 与「0/缺值」区分开
_REASON_NEGATIVE_WORK_HOURS = "员工资料每日工作时长非正"
#: 考勤行出现不在员工名单里的 employee_id 时的 reason——必须区分成因（ERP
#: 员工删除是软删置 deleted_at，而 MCP 查询层对 employees 无条件追加
#: deleted_at IS NULL、DSL 无 include_deleted；见 hengfa-erp/backend/app/mcp/
#: query.py）：
#:   - 离职（is_active=false）→ 勾 include_inactive=True 可取回，提示可执行；
#:   - 已软删 → 勾 include_inactive 也永远取不回，只能提示核实档案。
_REASON_INACTIVE = "员工已离职（如需纳入请勾选含离职后重算）"
_REASON_DELETED = "员工档案已删除或不存在（考勤为残留数据，请核实）"
#: 理论不可达（include_inactive=True 时主查询已不带 is_active 过滤，该 id 本
#: 应已在员工名单中）：仍归「档案有问题」类并说明异常，不误报为离职开关问题。
_REASON_DELETED_ANOMALY = (
    "员工档案存在但未进入本次员工名单（数据异常，请核实；"
    "本次已要求含离职，该 id 仍未进入名单）"
)
#: 追加的成因判定查询失败——无法区分离职/已删除，退回合并文案（绝不丢人）
_REASON_OUT_OF_SCOPE_UNKNOWN = (
    "不在本次统计范围（离职/已删除的成因判定查询失败，未能区分，请核实）"
)


class ErpSalaryCalcArgs(BaseModel):
    """erp_salary_calc 参数：时段闭区间（含首尾）+ 是否含离职员工。

    算工资一律调用本工具，只要这一段起止日即可——日薪/每日工作时长/考勤
    明细的取数与 R1 公式的全部计算都由工具内部完成，不要自己查 employees/
    attendance 明细再心算（模型算术不可靠，已有 2026-07 工资 12 行错 11 行的
    实测教训）。
    """

    start_date: str = Field(
        description="起算日（YYYY-MM-DD，含当日）——工资统计时段的起点"
    )
    end_date: str = Field(
        description="截止日（YYYY-MM-DD，含当日）——工资统计时段的终点"
    )
    include_inactive: bool = Field(
        False,
        description=(
            "是否把已离职（is_active=false）员工也纳入统计；缺省 false 只算"
            "在职员工。一般不填（缺省即在职口径）"
        ),
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        """严格校验 YYYY-MM-DD（不允许时间戳/紧凑写法，防口径漂移）。"""
        text = value.strip() if isinstance(value, str) else ""
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise ValueError(
                f"日期格式必须为 YYYY-MM-DD（如 2026-07-01），收到 {value!r}"
            )
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"日期非法（YYYY-MM-DD）：{value!r}（{exc}）"
            ) from exc
        return text

    @model_validator(mode="after")
    def _check_order(self) -> "ErpSalaryCalcArgs":
        if self.start_date > self.end_date:
            raise ValueError(
                f"时段起止非法：start_date（{self.start_date}）晚于 "
                f"end_date（{self.end_date}）——闭区间要求起点不晚于终点"
            )
        return self


def _sort_key(value: Any) -> tuple[int, int, str]:
    """稳定排序键（员工 id 多为 int；非 int 时退化为字符串序，不抛错）。"""
    if isinstance(value, bool):  # bool 是 int 子类，语义不是 id
        return (1, 0, str(value))
    if isinstance(value, int):
        return (0, value, "")
    return (1, 0, str(value))


def run_erp_salary_calc(ctx: ToolContext, args: ErpSalaryCalcArgs) -> ToolResultSummary:
    """一次性算出一个时段内每位员工的工资（只读；明细不进模型上下文）。"""
    # 1. 权限（非写工具分支无 scope 校验，本工具自行 fail closed，同 run_erp_calc）
    if (
        ctx.context is None
        or ctx.context.user_role != "manager"
        or "erp:query" not in (ctx.context.scope or frozenset())
    ):
        return ToolResultSummary(
            ok=False,
            summary={"error": "权限不足：工资计算需要 manager 角色与 erp:query scope（8.4/10.5）"},
        )
    # 2. 客户端/元数据接线（缺依赖不静默，对齐 erp_query/erp_calc）
    if ctx.client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 客户端未接线（harness 运行时缺 client），不能执行"},
        )
    if ctx.metadata is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 实体元数据未接线（harness 运行时缺 metadata），不能执行"},
        )
    # 3. 实体白名单（与 erp_query/erp_calc 同一开放面，不走查询白名单旁路）
    for entity in (_EMPLOYEES_ENTITY, _ATTENDANCE_ENTITY):
        if entity not in QUERYABLE_ENTITIES:
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"实体 {entity!r} 不可查询（9.2 白名单），"
                        f"仅允许：{sorted(QUERYABLE_ENTITIES)}"
                    )
                },
            )
    # 4. 实体元数据 + 字段目录校验（未知字段拒绝——不猜字段、不凭印象补）
    try:
        emp_entity = ctx.metadata.get_entity(_EMPLOYEES_ENTITY)
        att_entity = ctx.metadata.get_entity(_ATTENDANCE_ENTITY)
    except Exception as exc:  # noqa: BLE001 —— 元数据异常统一 fail closed
        return ToolResultSummary(
            ok=False, summary={"error": f"ERP 实体元数据不可用：{exc}"}
        )
    for entity, meta in ((_EMPLOYEES_ENTITY, emp_entity), (_ATTENDANCE_ENTITY, att_entity)):
        if meta is None:
            return ToolResultSummary(
                ok=False,
                summary={
                    "error": (
                        f"实体 {entity!r} 不在元数据（list_entities）中，"
                        "字段目录未知，拒绝计算（fail closed）"
                    )
                },
            )
    emp_known = set(emp_entity.fields)
    att_known = set(att_entity.fields)
    bad_emp = [f for f in _EMPLOYEE_FIELDS if f not in emp_known]
    bad_att = [f for f in _ATTENDANCE_FIELDS if f not in att_known]
    if bad_emp:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"employees 字段不在实体字段目录中（拒绝计算，fail closed）："
                    f"{'、'.join(bad_emp)}；可用字段：{', '.join(sorted(emp_known))}"
                )
            },
        )
    if bad_att:
        return ToolResultSummary(
            ok=False,
            summary={
                "error": (
                    f"attendance 字段不在实体字段目录中（拒绝计算，fail closed）："
                    f"{'、'.join(bad_att)}；可用字段：{', '.join(sorted(att_known))}"
                )
            },
        )
    # 5. 取数（两次查询 + 本地关联；_fetch_all_rows 与 erp_calc 共享同一分页/
    #    完成性/去重实现——跨模块复用，勿复制一份，见 calc.py 同名函数）
    emp_filter: dict[str, Any] | None = (
        None if args.include_inactive else {"is_active": True}
    )
    emp_rows, err = _fetch_all_rows(
        ctx, entity=_EMPLOYEES_ENTITY, fields=sorted(_EMPLOYEE_FIELDS), filt=emp_filter
    )
    if err is not None:
        return ToolResultSummary(ok=False, summary={"error": err})
    att_rows, err = _fetch_all_rows(
        ctx,
        entity=_ATTENDANCE_ENTITY,
        fields=sorted(_ATTENDANCE_FIELDS),
        filt={"att_date": {"gte": args.start_date, "lte": args.end_date}},
    )
    if err is not None:
        return ToolResultSummary(ok=False, summary={"error": err})

    # 6. 本地索引与分组（姓名映射本地做，不再发第二次查询）
    emp_by_id: dict[str, dict[str, Any]] = {
        str(row["id"]): row for row in emp_rows if row.get("id") is not None
    }
    att_by_emp: dict[str, list[dict[str, Any]]] = {}
    for row in att_rows:
        att_by_emp.setdefault(str(row.get("employee_id")), []).append(row)

    items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for eid in sorted(emp_by_id, key=_sort_key):
        row = emp_by_id[eid]
        name = row.get("name")
        rows = att_by_emp.get(eid) or []
        if not rows:
            missing.append(
                {"employee_id": row.get("id"), "name": name,
                 "reason": _REASON_NO_ATTENDANCE}
            )
            continue
        daily_wage = _cell_decimal(row.get("daily_wage"))
        if daily_wage is None or daily_wage == 0:
            missing.append(
                {"employee_id": row.get("id"), "name": name,
                 "reason": _REASON_NO_DAILY_WAGE}
            )
            continue
        # daily_wage 为负**不拦**：它是被乘数（天数 × 日薪），公式仍成立，
        # 红冲/调整类数据也可能出现负日薪。只拦分母（每日工作时长）非正。
        work_hours = _cell_decimal(row.get("work_hours_per_day"))
        if work_hours is None or work_hours == 0:
            # 除零，绝对不许跳过除法继续算——fail closed 记 missing
            missing.append(
                {"employee_id": row.get("id"), "name": name,
                 "reason": _REASON_NO_WORK_HOURS}
            )
            continue
        if work_hours < 0:
            # 分母为负（数据异常）：同样 fail closed 记 missing，独立 reason
            # 与「0/缺值」区分开——负数会翻转时薪符号，绝不能继续算。
            missing.append(
                {"employee_id": row.get("id"), "name": name,
                 "reason": _REASON_NEGATIVE_WORK_HOURS}
            )
            continue
        days = len(rows)
        ot_minutes = sum(
            (_cell_decimal(r.get("overtime_minutes")) or Decimal(0) for r in rows),
            Decimal(0),
        )
        lv_minutes = sum(
            (_cell_decimal(r.get("leave_minutes")) or Decimal(0) for r in rows),
            Decimal(0),
        )
        with localcontext() as lc:
            lc.prec = 50  # 高精度中间量，输出时再量化
            ot_hours = ot_minutes / Decimal(60)
            lv_hours = lv_minutes / Decimal(60)
            hourly = daily_wage / work_hours
            wage = (
                Decimal(days) * daily_wage + hourly * ot_hours - hourly * lv_hours
            )
        # 半分位取偶（HALF_EVEN）——刻意对齐 ERP hr.py:443 的 round(Decimal, 2)，
        # 逐人工资须与系统记录逐分一致；勿改回 HALF_UP，理由见 _WAGE_QUANTIZE。
        wage_q = wage.quantize(_WAGE_QUANTIZE, rounding=ROUND_HALF_EVEN)
        items.append(
            {
                "employee_id": row.get("id"),
                "name": name,
                "days": days,
                "daily_wage": _fmt_decimal(daily_wage),
                "work_hours_per_day": _fmt_decimal(work_hours),
                "overtime_hours": _fmt_decimal(
                    ot_hours.quantize(_AVG_QUANTIZE, rounding=ROUND_HALF_UP)
                ),
                "leave_hours": _fmt_decimal(
                    lv_hours.quantize(_AVG_QUANTIZE, rounding=ROUND_HALF_UP)
                ),
                "wage": _fmt_decimal(wage_q),
                # 展示列：从**已量化的 wage_q（2 位权威值）再舍一次**成 1 位
                # HALF_UP——不是从全精度 wage 直接舍（见 _WAGE_DISPLAY_QUANTIZE
                # 注释：直接舍会在边界上与权威值打架）。纯展示，不参与 totals。
                "wage_display": _fmt_decimal(
                    wage_q.quantize(_WAGE_DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)
                ),
            }
        )

    # 6.1 考勤行里出现不在员工集合里的 employee_id → 记入 missing，**不静默
    #     丢弃**（静默丢数比报错更有害）。成因必须区分，判据见常量注释：
    #       离职（is_active=false）勾含离职可取回；已软删勾也取不回。
    #     仅当确有 out-of-scope id 时才追加一次 employees 查询（filter id in
    #     [...]，**不带 is_active 过滤**）——软删员工会被 ERP 的 deleted_at
    #     IS NULL 挡掉，返回集合里没有即「已删除」。该追加查询失败绝不让整个
    #     工具失败：退回合并文案，note 说明，**一个都不丢**（这些人本就已在
    #     missing 里）。
    out_of_scope_keys = sorted(
        (k for k in att_by_emp if k not in emp_by_id), key=_sort_key
    )
    out_of_scope_note: str | None = None
    if out_of_scope_keys:
        # att_by_emp 键为 str，查询要传原始 id 值
        orig_id_by_key: dict[str, Any] = {
            k: att_by_emp[k][0].get("employee_id") for k in out_of_scope_keys
        }
        probe_rows, probe_err = _fetch_all_rows(
            ctx,
            entity=_EMPLOYEES_ENTITY,
            fields=sorted(_EMPLOYEE_FIELDS),
            filt={"id": {"in": [orig_id_by_key[k] for k in out_of_scope_keys]}},
        )
        if probe_err is not None:
            out_of_scope_note = (
                "部分考勤行的 employee_id 不在员工名单，判定「离职/已删除」的"
                f"补充查询失败（{probe_err}）——未能区分原因，已按「不在本次"
                "统计范围」记入 missing，未丢人，请核实。"
            )
            for k in out_of_scope_keys:
                missing.append(
                    {"employee_id": orig_id_by_key[k], "name": None,
                     "reason": _REASON_OUT_OF_SCOPE_UNKNOWN}
                )
        else:
            probe_name_by_id: dict[str, Any] = {
                str(r["id"]): r.get("name")
                for r in probe_rows if r.get("id") is not None
            }
            for k in out_of_scope_keys:
                if k in probe_name_by_id:
                    if args.include_inactive:
                        # 理论不可达（include_inactive=True 主查询不带
                        # is_active，该 id 本应已在名单中）→ 数据异常类
                        missing.append(
                            {"employee_id": orig_id_by_key[k],
                             "name": probe_name_by_id[k],
                             "reason": _REASON_DELETED_ANOMALY}
                        )
                    else:
                        missing.append(
                            {"employee_id": orig_id_by_key[k],
                             "name": probe_name_by_id[k],
                             "reason": _REASON_INACTIVE}
                        )
                else:
                    missing.append(
                        {"employee_id": orig_id_by_key[k], "name": None,
                         "reason": _REASON_DELETED}
                    )

    # 6.3 考勤覆盖度（常驻信号，F1）：给出首尾考勤日期、去重天数与时段天数。
    #     任何考勤行都没有 → first/last 为 None、distinct_days=0。
    att_dates = sorted(
        str(r.get("att_date"))[:10] for r in att_rows if r.get("att_date") is not None
    )
    period_days = (
        date.fromisoformat(args.end_date) - date.fromisoformat(args.start_date)
    ).days + 1
    first_att_date = att_dates[0] if att_dates else None
    last_att_date = att_dates[-1] if att_dates else None
    coverage = {
        "first_att_date": first_att_date,
        "last_att_date": last_att_date,
        "distinct_days": len(set(att_dates)),
        "period_days": period_days,
    }

    # 7. totals.wage = 各 items 的 wage（**2 位权威值**）逐行求和——报告合计
    #    一律以此为准。**刻意不用 items 的 wage_display（1 位展示值）相加**：
    #    展示被压成 1 位，逐行相加每人最多差 0.05 元，会把合计带偏（[2026-09-13]
    #    测试 test_totals_uses_authoritative_wage_not_display 钉住此纪律）。
    total_wage = sum((Decimal(it["wage"]) for it in items), Decimal(0))
    total_days = sum(it["days"] for it in items)
    total_ot = sum((Decimal(it["overtime_hours"]) for it in items), Decimal(0))
    total_lv = sum((Decimal(it["leave_hours"]) for it in items), Decimal(0))
    totals = {
        "wage": _fmt_decimal(total_wage),
        "days": total_days,
        "overtime_hours": _fmt_decimal(total_ot),
        "leave_hours": _fmt_decimal(total_lv),
    }

    note = (
        "工资按 R1.salary.calculation 口径计算："
        "工资 = 考勤天数 × 日薪 + 时薪 × 加班时长 − 时薪 × 请假时长"
        "（时薪 = 日薪 ÷ 每日工作时长；加班/请假时长由分钟 ÷ 60）。"
        "items 每位员工有两个工资字段，用途不同："
        "wage 为 **2 位小数**（半分位取偶，与 ERP 系统记录**逐分一致**），"
        "是核对与合计用的**权威值**；"
        "wage_display 为 **1 位小数**，**仅供向用户逐人展示**（由已量化的 "
        "wage 再舍一次得到，是权威值的忠实缩短）——"
        "**给用户报每个人的工资时用 wage_display**。"
        "报告工资**合计一律以 totals.wage 为准**（totals.wage 是各行 **wage"
        "（2 位权威值）** 的逐行求和），不要自己另加。"
        "**警告：展示列 wage_display 逐行相加并不等于 totals.wage**"
        "（展示被压成 1 位，每人最多差 0.05 元）——"
        "**报告合计不得用展示列相加，必须用 totals.wage**。"
    )
    if missing:
        note += (
            f"missing 中 {len(missing)} 人因缺参未计入（缺考勤/日薪/每日工作时长"
            "等），需向用户确认后处理，不得用猜测值补齐。"
        )
    if out_of_scope_note is not None:
        note += out_of_scope_note
    # 覆盖度告警只针对「首尾没盖住请求时段」这一确定缺口（含完全无考勤行）；
    # 不因 distinct_days < period_days 报警——「只录工作日、不录周末」是正常
    # 数据，按天数不等报警会每月误报；完整 coverage 数字常驻供自行判断。
    if (
        first_att_date is None
        or first_att_date > args.start_date
        or last_att_date < args.end_date
    ):
        if first_att_date is None:
            note += (
                f"本时段无任何考勤记录（请求期间 {args.start_date} 至 "
                f"{args.end_date}，共 {period_days} 天），工资可能不完整，"
                "请核实缺录日期。"
            )
        else:
            note += (
                f"本时段考勤记录仅覆盖 {first_att_date} 至 {last_att_date}"
                f"（请求期间 {args.start_date} 至 {args.end_date}，共 "
                f"{period_days} 天），工资可能不完整，请核实缺录日期。"
            )

    summary: dict[str, Any] = {
        "entity": "erp_salary_calc",  # items 信封标记（crop 白名单外原样进上下文）
        "start_date": args.start_date,
        "end_date": args.end_date,
        "count": len(items),
        "total_rows": len(att_rows),
        "coverage": coverage,
        "items": items,
        "totals": totals,
        "missing": missing,
        "note": note,
    }
    return ToolResultSummary(ok=True, summary=summary)
