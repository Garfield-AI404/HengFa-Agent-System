"""R1 考勤计划纯函数（4.1 十二条规则的确定性实现；4.3 固定执行算法顺序 5）。

输入（业务日期、有效员工集合、exceptions 列表、已有记录集合）→ 确定性
输出 created/updated/unchanged 三组计划（4.3 顺序 5 语义）。

纯函数边界（本单元不做）：
- 不调用 MCP、不写库（D12 执行链负责 CRUD/attempt/result/恢复）；
- 不做员工唯一匹配与歧义询问（4.3 顺序 2 归 D12）；
- 内部标准名 leave_minutes/overtime_minutes（4.1 L638），适配层字段
  映射归 D12。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

#: 计划动作（4.3 顺序 5：created/updated/unchanged；failed 归 D12 执行结果）
ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_UNCHANGED = "unchanged"


class AttendanceRuleError(ValueError):
    """违反 4.1 考勤规则；消息面向用户，要求修正输入。"""


@dataclass(frozen=True)
class AttendanceException:
    """4.2 契约 exceptions 条目（小时表达，规则 9）。"""

    employee_ref: str
    leave_hours: float | None = None
    overtime_hours: float | None = None


@dataclass(frozen=True)
class ExistingRecord:
    """当天已有考勤记录（4.3 顺序 3 query 结果；内部标准分钟）。"""

    employee_ref: str
    leave_minutes: int | None = None
    overtime_minutes: int | None = None


@dataclass(frozen=True)
class PlannedRecord:
    """计划中的一条考勤（内部标准分钟；None=未发生，不写 0，规则 11）。"""

    employee_ref: str
    leave_minutes: int | None = None
    overtime_minutes: int | None = None
    action: str = ACTION_CREATED

    def has_exception(self) -> bool:
        return self.leave_minutes is not None or self.overtime_minutes is not None


@dataclass(frozen=True)
class AttendancePlan:
    """4.3 顺序 5 的输出：created/updated/unchanged 三组（4.2 输出口径）。"""

    attendance_date: str
    created: tuple[PlannedRecord, ...] = ()
    updated: tuple[PlannedRecord, ...] = ()
    unchanged: tuple[PlannedRecord, ...] = ()

    @property
    def all_records(self) -> tuple[PlannedRecord, ...]:
        return self.created + self.updated + self.unchanged

    def counts(self) -> dict[str, int]:
        """4.2 输出：created/updated/unchanged 数量（failed 归 D12 执行）。"""
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "unchanged": len(self.unchanged),
        }


def hours_to_minutes(hours: float) -> int:
    """规则 9/10：小时 → 整数分钟；×60 非整数时要求用户修正。

    1.5 小时 = 90 分钟；1.1 小时 = 66 分钟；无法得到整数分钟（如
    0.33333333 小时）抛 AttendanceRuleError，不猜测取整。
    """
    minutes = hours * 60
    if not minutes.is_integer():
        raise AttendanceRuleError(
            f"{hours} 小时无法转换为整数分钟，请用更精确的小时数重新说明"
        )
    return int(minutes)


def plan_attendance(
    attendance_date: str,
    effective_employees: Iterable[str],
    exceptions: Iterable[AttendanceException] = (),
    existing_records: Mapping[str, ExistingRecord] | Iterable[ExistingRecord] = (),
) -> AttendancePlan:
    """生成当天考勤计划（4.1 规则 1-8 的确定性实现；4.3 顺序 5）。

    - 首次（existing 为空）：全部有效员工 created，未提及员工为正常出勤
      （规则 1/3），提及员工带请假/加班（规则 2）；
    - 增量（existing 非空）：只更新本次明确提到的员工（规则 4）；未提及
      但已有记录的员工 unchanged 保持原样（规则 5，异常不被覆盖）；仍缺
      记录的有效员工补建正常（规则 6）；
    - 同一员工同日只允许一条记录（规则 7）：exceptions 内重复 → 错误；
    - 有效员工集合出现重复姓名（ERP 同名员工）→ 错误：本函数不做消歧查询
      （归 D12），只抛可操作的 AttendanceRuleError 说明是哪个姓名重复、为何
      无法确定考勤对象、如何修正后重试；
    - 请假与加班保存在同一条记录不同字段，不互相覆盖（规则 8）；
    - 请假/加班必须大于 0，没有发生的项目不写 0（规则 11）；
    - 正常出勤不要求工时（规则 12）：exceptions 条目两个字段都为空时
      视为未提及异常，走正常路径。
    """
    employees: tuple[str, ...] = tuple(effective_employees)
    exceptions_list: tuple[AttendanceException, ...] = tuple(exceptions)

    # 有效员工集合自身出现重复姓名：ERP 存在同名员工，无法唯一确定考勤对象。
    # 本模块不做消歧查询（边界见模块 docstring，归 D12），只抛结构化错误、
    # 给出可操作的修正方向。必须**在循环产出计划之前**拦截：重名员工会被
    # 循环各产出一条记录，落到下方「每人恰好一条」的纵深校验——历史实现是
    # 裸 assert，图内表现为难以理解的 agent_error（用户看不到原因）。
    repeated_names = sorted(
        name for name, count in Counter(employees).items() if count > 1
    )
    if repeated_names:
        raise AttendanceRuleError(
            f"员工姓名 {repeated_names!r} 在有效员工集合中出现多次："
            "ERP 中存在同名员工，无法唯一确定考勤对象；"
            "请先在 ERP 中区分同名员工，或改用可唯一标识员工的方式指定后重试"
        )

    # 规则 7：同日单条约束——exceptions 内同一员工重复出现即错误
    mentioned = [e.employee_ref for e in exceptions_list]
    if len(set(mentioned)) != len(mentioned):
        dup = next(r for r in mentioned if mentioned.count(r) > 1)
        raise AttendanceRuleError(
            f"员工 {dup!r} 在本次录入中出现多次；同一员工同一天只允许一条考勤记录"
        )

    # 已有记录统一为列表；重复（4.3 顺序 4 数据异常）在计划层同样拦截
    if isinstance(existing_records, Mapping):
        existing_list: list[ExistingRecord] = list(existing_records.values())
    else:
        existing_list = list(existing_records)
    existing_map: dict[str, ExistingRecord] = {r.employee_ref: r for r in existing_list}
    if len(existing_map) != len(existing_list):
        raise AttendanceRuleError("已有考勤记录出现同一员工多条，属数据异常")

    # 用户明确提到的员工必须属于有效集合：未知员工不静默丢弃（规则 2 数据
    # 一致性校验；同名歧义匹配归 D12 执行链）
    unknown = set(mentioned) - set(employees)
    if unknown:
        raise AttendanceRuleError(
            f"员工 {sorted(unknown)!r} 不在有效员工集合中，请核对姓名"
        )

    # 按员工汇总本次异常（结构上已保证不重复）
    exception_by_employee: dict[str, AttendanceException] = {
        e.employee_ref: e for e in exceptions_list
    }

    created: list[PlannedRecord] = []
    updated: list[PlannedRecord] = []
    unchanged: list[PlannedRecord] = []

    for employee in employees:
        existing = existing_map.get(employee)
        exc = exception_by_employee.get(employee)
        leave_minutes, overtime_minutes = _exception_minutes(exc, employee)
        has_new_exception = leave_minutes is not None or overtime_minutes is not None

        if existing is None:
            if has_new_exception:
                created.append(PlannedRecord(employee, leave_minutes,
                                             overtime_minutes, ACTION_CREATED))
            else:
                # 规则 3/6：首次未提及 → 正常出勤；增量仍缺 → 补建正常
                created.append(PlannedRecord(employee, None, None, ACTION_CREATED))
        elif has_new_exception:
            # 规则 8：同一条记录不同字段共存——本次未提字段保留已有值
            updated.append(PlannedRecord(
                employee,
                leave_minutes if leave_minutes is not None
                else existing.leave_minutes,
                overtime_minutes if overtime_minutes is not None
                else existing.overtime_minutes,
                ACTION_UPDATED,
            ))
        else:
            # 规则 5：未提及员工保持原样（不覆盖成正常）
            unchanged.append(PlannedRecord(
                employee, existing.leave_minutes, existing.overtime_minutes,
                ACTION_UNCHANGED,
            ))

    # 规则 1：计划必须覆盖全部有效员工，每人恰好一条（4.2 最终验证前置）。
    # **纵深防御、当前不可达**：下方两处检查**不承担检出职责**——前置重名
    # 检测（本函数开头，对 effective_employees 判重）与"循环按 employees 逐条
    # 产出"已保证此处恒真（planned 的 ref 集合恰为 set(employees) 且无重复）。
    # 保留它们只为未来若有人改动上方循环产出逻辑时的兜底；不要在此处新增
    # 检出测试夹具（不可达代码）。
    # 之所以仍从裸 assert 改为结构化错误：一是裸 assert 会被 ``python -O``
    # 剥离（万一将来变为可达，防护会静默消失），二是措辞与前置检测同族、
    # 对用户可操作。
    planned = created + updated + unchanged
    planned_refs = [p.employee_ref for p in planned]
    if set(planned_refs) != set(employees):
        raise AttendanceRuleError(
            "计划未覆盖全部有效员工，无法生成完整考勤计划；请核对员工名单后重试"
        )
    repeated_in_plan = sorted(
        ref for ref, count in Counter(planned_refs).items() if count > 1
    )
    if repeated_in_plan:
        raise AttendanceRuleError(
            f"计划中同一员工 {repeated_in_plan!r} 出现多条：ERP 中存在同名员工，"
            "无法唯一确定考勤对象；请先在 ERP 中区分同名员工后重试"
        )

    return AttendancePlan(
        attendance_date=attendance_date,
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
    )


def _exception_minutes(
    exc: AttendanceException | None,
    employee: str,
) -> tuple[int | None, int | None]:
    """exceptions 条目 → 内部标准分钟；校验规则 10/11/12。"""
    if exc is None:
        return None, None
    leave = _single_minutes(exc.leave_hours, "请假", employee)
    overtime = _single_minutes(exc.overtime_hours, "加班", employee)
    if leave is None and overtime is None:
        # 规则 12/11：正常出勤不要求工时；无异常条目视为未提及（走正常路径）
        return None, None
    return leave, overtime


def _single_minutes(hours: float | None, kind: str, employee: str) -> int | None:
    """单个字段：None→未发生；否则校验 >0 并转整数分钟（规则 10/11）。"""
    if hours is None:
        return None
    if hours <= 0:
        raise AttendanceRuleError(
            f"员工 {employee!r} 的{kind}必须大于 0（当前 {hours}）"
        )
    return hours_to_minutes(hours)
