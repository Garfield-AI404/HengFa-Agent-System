"""考勤写工具（R1）参数/确认卡/准备/执行。

考勤域专用：AttendanceExceptionArgs/AttendancePlanExecuteArgs 参数模型、
build_attendance_confirmation_card 确认卡、prepare_attendance/execute_
attendance 两段式接线，以及考勤域私有序列化辅助（_existing_records_for/
_plan_to_dict/_record_to_dict/_plan_from_dict）。

Phase 2 接线目标（domains/attendance.py 内部零改动——本模块只做参数适配
与调用）：prepare 做全部只读前置（ERP employees 查询/姓名唯一匹配/当天
已有记录/计划生成/确认卡构造），执行上下文（plan 序列化）与卡片一并写入
State.pending_confirmation；确认通过后 run 用同一执行上下文调
AttendanceExecutor.execute（保证"批准的卡片"就是"执行的载荷"，payload_hash
变化即失效）。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.tools.base import (
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
    _query_all,
    _require_wiring,
    _summary_for,
)
from administrative_agent.business.attendance_rules import (
    AttendanceException,
    AttendancePlan,
    ExistingRecord,
    PlannedRecord,
    plan_attendance,
)
from administrative_agent.business.matching import EmployeeCandidate, match_employee
from administrative_agent.domains.attendance import AttendanceExecutor
from administrative_agent.domains.orders import (
    ConfirmationCard,
    card_payload_hash,
    localize_confirmation_fields,
)


class AttendanceExceptionArgs(BaseModel):
    """考勤例外（employee_ref 为员工姓名，规则 4 不猜）。

    两个时长参数的单位都是**小时，不是分钟**；规则包里写的分钟数不得直接填进来
    （如 9 小时填 9，填 540 会被按小时换算成 540 小时落库）。
    """

    employee_ref: str
    leave_hours: float | None = Field(
        None,
        description="请假时长，单位是小时不是分钟：9 小时请填 9，不要填分钟数（如 540）；不填表示不请假",
    )
    overtime_hours: float | None = Field(
        None,
        description="加班时长，单位是小时不是分钟：1.5 小时请填 1.5；不填表示不加班",
    )


class AttendancePlanExecuteArgs(BaseModel):
    """attendance_plan_execute 参数（4.2 六要素前提：日期 + 员工名单）。"""

    attendance_date: str = Field(..., description="考勤日期 YYYY-MM-DD")
    employees: list[str] = Field(..., min_length=1, description="员工姓名（至少 1 人）")
    exceptions: list[AttendanceExceptionArgs] | None = None

    @field_validator("attendance_date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        """入参侧 fail closed：形状与日期都要合法。

        两道校验缺一不可——正则拦形状（``2026/08/30``、``2026-8-30``、空串）；
        ``date.fromisoformat`` 拦形状合法但日期非法（``2026-13-40``）；反之
        Python 3.11 的 ``fromisoformat`` 也接受 ``20260830`` 紧凑格式，
        单靠它拦不住非 ISO 写法。
        """
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError(f"attendance_date 必须是 YYYY-MM-DD 格式，收到 {v!r}")
        date.fromisoformat(v)  # 拦 2026-13-99 这类形状合法但日期非法的输入
        return v


def build_attendance_confirmation_card(
    attendance_date: str, plan: AttendancePlan
) -> ConfirmationCard:
    """考勤写入轻确认卡（与 build_metal_confirmation_card 同构，纯函数）。

    写值：计划全量记录（员工 → action/请假分钟/加班分钟）；要素 5 预计
    影响：ERP 写入当天考勤记录（created/updated/unchanged 语义，4.1 规则
    1/3/5/6）；要素 6 限制：小时转分钟与员工名单精确匹配为执行前提。

    hash_fields：[2026-09-10 修复] 取原始计划记录（action 为 created/
    updated/unchanged 英文枚举、分钟为数值）——本地化会把 action 值译为
    中文新，哈希须用真实执行语义。
    """
    raw_fields = tuple(
        (
            r.employee_ref,
            {
                "action": r.action,
                "leave_minutes": r.leave_minutes,
                "overtime_minutes": r.overtime_minutes,
            },
        )
        for r in plan.all_records
    )
    fields_to_write = localize_confirmation_fields(raw_fields)
    card = ConfirmationCard(
        operation_type="记录每日考勤",
        target=(
            f"{attendance_date} 考勤计划"
            f"（{len(plan.created)} 建 / {len(plan.updated)} 改 / "
            f"{len(plan.unchanged)} 保持）"
        ),
        fields_to_write=fields_to_write,
        hash_fields=raw_fields,
        auto_filled=(),
        expected_impact=(
            "在 ERP 写入当天考勤记录：created 新建记录、updated 更新现有"
            "记录、unchanged 保持原样（4.1 规则 1/3/5/6）；"
            "不涉及其他业务表。"
        ),
        limitations=(
            "请假/加班以小时表达，按规则转换为整数分钟记录（规则 9/10）；",
            "员工名单以 ERP employees 实体为准，姓名须精确匹配（规则 2）；",
            "最终校验以 ERP 写入响应为准，单条失败不重放批次（13.2）。",
        ),
        payload_hash="",
        # [2026-09-12 批量卡] 显式声明矩阵卡的行头列名与折叠量词——考勤卡是
        # 「一张卡多条明细」的蓝本，零回归应是**显式的**而非依赖前端回退巧合。
        row_header_label="员工",
        row_quantifier="人",
    )
    return replace(card, payload_hash=card_payload_hash(card))


def prepare_attendance(ctx: ToolContext, args: AttendancePlanExecuteArgs) -> PreparedToolWrite:
    """考勤写：ERP employees 取全 → 姓名唯一匹配 → 当天已有记录 → 计划 →
    确认卡（4.3 顺序 2/3/5 语义迁移）。"""
    _require_wiring(ctx, tool="attendance_plan_execute")

    employees_rows = _query_all(ctx, entity="employees", fields=("id", "name"))
    candidates = [
        EmployeeCandidate(employee_id=int(r["id"]), name=str(r["name"]))
        for r in employees_rows
        if r.get("id") is not None and r.get("name")
    ]
    # 有效员工集合（规则 3：全员覆盖——未提及员工为正常出勤）
    effective_names = [c.name for c in candidates]

    matched: dict[str, EmployeeCandidate] = {}
    for ref in args.employees:
        outcome = match_employee(ref, candidates)
        if not outcome.writable:
            if outcome.status == "ambiguous":
                names = "、".join(name for _, name in outcome.candidates)
                raise ToolPreparationError(
                    f"员工 {ref!r} 存在多个同名候选（{names}），"
                    "需用户确认后重试（不自动选择，fail closed）"
                )
            raise ToolPreparationError(
                f"员工 {ref!r} 未找到精确匹配，需核对姓名后重试（fail closed）"
            )
        matched[ref] = EmployeeCandidate(
            employee_id=outcome.entity_id, name=outcome.matched_name
        )

    # 当天已有记录（4.3 顺序 3；规则 4/5 增量语义：只更新明确提到的员工）
    existing = _existing_records_for(ctx, args.attendance_date, candidates)

    exceptions = [
        AttendanceException(e.employee_ref, e.leave_hours, e.overtime_hours)
        for e in (args.exceptions or [])
    ]
    plan = plan_attendance(
        args.attendance_date,
        effective_employees=effective_names,
        exceptions=exceptions,
        existing_records=existing,
    )

    card = build_attendance_confirmation_card(args.attendance_date, plan)
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=card.payload_hash,
        execution={
            "tool": "attendance_plan_execute",
            "attendance_date": args.attendance_date,
            "plan": _plan_to_dict(plan),
            "employees": [(c.employee_id, c.name) for c in candidates],
            "policy_key": "R1.attendance.daily",
        },
        policy_key="R1.attendance.daily",
    )


def execute_attendance(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """考勤写执行：AttendanceExecutor.execute（内部零改动，参数适配）。"""
    _require_wiring(ctx, tool="attendance_plan_execute")
    plan = _plan_from_dict(execution["plan"])
    employees = [
        EmployeeCandidate(employee_id=int(employee_id), name=name)
        for employee_id, name in execution["employees"]
    ]
    executor = AttendanceExecutor(
        client=ctx.client,
        metadata=ctx.metadata,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=execution.get("trace_id") or "",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        policy=policy_for(execution["policy_key"]) if execution.get("policy_key") else None,
    )
    summary = executor.execute(
        task_id=task_id,
        attendance_date=execution["attendance_date"],
        plan=plan,
        employees=employees,
    )
    return _summary_for(ctx, execution, task_id, summary.to_dict())


# ---- 内部：考勤域私有序列化辅助 ----

def _existing_records_for(
    ctx: ToolContext, attendance_date: str, candidates: list[EmployeeCandidate]
) -> list[ExistingRecord]:
    """当天已有考勤记录（employee_id → 姓名映射；未知 ID 跳过——不在
    有效集合内，规则 4 增量语义不涉及）。"""
    name_by_id = {c.employee_id: c.name for c in candidates}
    rows = _query_all(
        ctx, entity="attendance", filter={"att_date": attendance_date}
    )
    records: list[ExistingRecord] = []
    for row in rows:
        employee_id = row.get("employee_id")
        name = name_by_id.get(employee_id) if employee_id is not None else None
        if name is None:
            continue
        records.append(
            ExistingRecord(
                employee_ref=name,
                leave_minutes=row.get("leave_minutes"),
                overtime_minutes=row.get("overtime_minutes"),
            )
        )
    return records


def _plan_to_dict(plan: AttendancePlan) -> dict[str, Any]:
    return {
        "attendance_date": plan.attendance_date,
        "created": [_record_to_dict(r) for r in plan.created],
        "updated": [_record_to_dict(r) for r in plan.updated],
        "unchanged": [_record_to_dict(r) for r in plan.unchanged],
    }


def _record_to_dict(r: PlannedRecord) -> dict[str, Any]:
    return {
        "employee_ref": r.employee_ref,
        "leave_minutes": r.leave_minutes,
        "overtime_minutes": r.overtime_minutes,
        "action": r.action,
    }


def _plan_from_dict(data: dict[str, Any]) -> AttendancePlan:
    return AttendancePlan(
        attendance_date=data["attendance_date"],
        created=tuple(PlannedRecord(**d) for d in data["created"]),
        updated=tuple(PlannedRecord(**d) for d in data["updated"]),
        unchanged=tuple(PlannedRecord(**d) for d in data["unchanged"]),
    )
