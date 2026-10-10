"""R1 考勤计划纯函数测试（RV-16；2.6 tests/unit/test_attendance_rules.py）。

覆盖 4.1 十二条业务规则：首次全员、增量更新、已有异常保留、补建缺失、
同日单条、请假加班共存、小时转整数分钟（含修正路径）、>0 校验、不写 0、
正常出勤不要求工时；14.3 端到端场景 1/2 的纯函数样例表。
"""

from __future__ import annotations

import pytest

from administrative_agent.business.attendance_rules import (
    ACTION_CREATED,
    ACTION_UNCHANGED,
    ACTION_UPDATED,
    AttendanceException,
    AttendancePlan,
    AttendanceRuleError,
    ExistingRecord,
    hours_to_minutes,
    plan_attendance,
)

EMPLOYEES = ["张三", "李四", "王五", "赵六"]


def exc(employee: str, leave: float | None = None,
        overtime: float | None = None) -> AttendanceException:
    return AttendanceException(employee, leave, overtime)


# ---------------------------------------------------------------------------
# 首次全员（规则 1/2/3；14.3 场景 1）
# ---------------------------------------------------------------------------


def test_first_processing_all_employees_created() -> None:
    """首次：全部有效员工各一条 created。"""
    plan = plan_attendance("2026-08-20", EMPLOYEES, [
        exc("张三", leave=2.0),
        exc("李四", overtime=3.0),
    ])
    assert plan.attendance_date == "2026-08-20"
    assert plan.counts() == {"created": 4, "updated": 0, "unchanged": 0}
    assert [p.employee_ref for p in plan.created] == EMPLOYEES
    by_name = {p.employee_ref: p for p in plan.created}
    assert by_name["张三"].leave_minutes == 120   # 2 小时 → 120 分钟
    assert by_name["张三"].overtime_minutes is None
    assert by_name["李四"].overtime_minutes == 180
    assert by_name["李四"].leave_minutes is None


def test_first_unmentioned_employees_normal_attendance() -> None:
    """规则 3：首次未提及员工 → 正常出勤（无异常、无工时）。"""
    plan = plan_attendance("2026-08-20", EMPLOYEES, [exc("张三", leave=2.0)])
    by_name = {p.employee_ref: p for p in plan.created}
    for name in ("王五", "赵六"):
        assert by_name[name].leave_minutes is None
        assert by_name[name].overtime_minutes is None
        assert by_name[name].action == ACTION_CREATED
        assert by_name[name].has_exception() is False


# ---------------------------------------------------------------------------
# 增量更新（规则 4/5/6；14.3 场景 2）
# ---------------------------------------------------------------------------


def test_increment_only_updates_mentioned_employees() -> None:
    """规则 4：后续增量只更新本次明确提到的员工。"""
    existing = {
        "张三": ExistingRecord("张三", 120, None),
        "李四": ExistingRecord("李四", None, 180),
        "王五": ExistingRecord("王五"),
        "赵六": ExistingRecord("赵六"),
    }
    plan = plan_attendance("2026-08-20", EMPLOYEES, [exc("王五", overtime=2.0)],
                           existing)
    assert plan.counts() == {"created": 0, "updated": 1, "unchanged": 3}
    updated = plan.updated[0]
    assert updated.employee_ref == "王五"
    assert updated.overtime_minutes == 120
    assert updated.action == ACTION_UPDATED
    # 其余员工 unchanged，值保持原样
    unchanged = {p.employee_ref: p for p in plan.unchanged}
    assert set(unchanged) == {"张三", "李四", "赵六"}
    assert unchanged["张三"].leave_minutes == 120


def test_existing_exceptions_not_overwritten() -> None:
    """规则 5：本次未提到但已有异常记录的员工保持不变，不能全量覆盖成正常。"""
    existing = {"张三": ExistingRecord("张三", 120, None)}
    plan = plan_attendance("2026-08-20", ["张三"], [], existing)
    assert plan.counts() == {"created": 0, "updated": 0, "unchanged": 1}
    kept = plan.unchanged[0]
    assert kept.leave_minutes == 120
    assert kept.has_exception() is True


def test_empty_exceptions_never_flattens_existing() -> None:
    """空 exceptions 增量：已有记录全部 unchanged（不重写为正常）。"""
    existing = {
        "张三": ExistingRecord("张三", 120, None),
        "李四": ExistingRecord("李四", None, 60),
    }
    plan = plan_attendance("2026-08-20", ["张三", "李四"], [], existing)
    assert plan.counts() == {"created": 0, "updated": 0, "unchanged": 2}
    assert {p.employee_ref for p in plan.unchanged} == {"张三", "李四"}


def test_increment_backfills_missing_normal_records() -> None:
    """规则 6：当天仍缺记录的有效员工补建正常记录。"""
    existing = {"张三": ExistingRecord("张三", 120, None),
                "李四": ExistingRecord("李四")}
    plan = plan_attendance("2026-08-20", ["张三", "李四", "王五"],
                           [exc("李四", overtime=1.5)], existing)
    assert plan.counts() == {"created": 1, "updated": 1, "unchanged": 1}
    created = plan.created[0]
    assert created.employee_ref == "王五"
    assert created.has_exception() is False
    assert plan.updated[0].employee_ref == "李四"  # 已有记录 + 本次加班 → 更新


def test_scenario2_afternoon_increment() -> None:
    """14.3 场景 2：保留上午请假、新增另一名员工加班、补建缺失正常记录。"""
    # 上午（场景 1）：张三请假 2h、李四加班 3h、王五赵六正常
    morning = plan_attendance("2026-08-20", EMPLOYEES,
                              [exc("张三", leave=2.0), exc("李四", overtime=3.0)])
    morning_map = {p.employee_ref: p for p in morning.created}
    # 下午：钱七新加入有效员工（上午无记录），王五加班 2h
    afternoon_employees = ["张三", "李四", "王五", "赵六", "钱七"]
    existing = {
        name: ExistingRecord(name, morning_map[name].leave_minutes,
                             morning_map[name].overtime_minutes)
        for name in ("张三", "李四", "王五", "赵六")
    }
    afternoon = plan_attendance("2026-08-20", afternoon_employees,
                                [exc("王五", overtime=2.0)], existing)
    assert afternoon.counts() == {"created": 1, "updated": 1, "unchanged": 3}
    # 补建：钱七正常
    assert afternoon.created[0].employee_ref == "钱七"
    assert afternoon.created[0].has_exception() is False
    # 更新：王五加班 120 分钟
    updated = afternoon.updated[0]
    assert updated.employee_ref == "王五"
    assert updated.overtime_minutes == 120
    # 保留：张三上午请假 120 分钟不被覆盖
    unchanged = {p.employee_ref: p for p in afternoon.unchanged}
    assert unchanged["张三"].leave_minutes == 120
    assert unchanged["李四"].overtime_minutes == 180


# ---------------------------------------------------------------------------
# 同日单条约束（规则 7）
# ---------------------------------------------------------------------------


def test_duplicate_employee_in_exceptions_rejected() -> None:
    """规则 7：同一员工同一天只允许一条记录。"""
    with pytest.raises(AttendanceRuleError, match="同一天只允许一条"):
        plan_attendance("2026-08-20", EMPLOYEES, [
            exc("张三", leave=2.0),
            exc("张三", overtime=1.0),
        ])


def test_duplicate_existing_records_rejected() -> None:
    """4.3 顺序 4：已有记录同一员工多条 → 数据异常停止。"""
    existing = [
        ExistingRecord("张三", 120, None),
        ExistingRecord("张三", None, 60),
    ]
    with pytest.raises(AttendanceRuleError, match="数据异常"):
        plan_attendance("2026-08-20", ["张三"], [], existing)


# ---------------------------------------------------------------------------
# 同名员工（ERP 重名 → 结构化错误，替代历史裸 assert）
# ---------------------------------------------------------------------------


def test_duplicate_employee_names_rejected() -> None:
    """ERP 同名员工：有效集合出现重复姓名 → AttendanceRuleError（非 AssertionError）。

    复现实测场景（考勤轮 C1）：ERP 里存在同名员工、本人未被提及时，历史实现
    由循环各产出一条记录、落到末尾裸 assert → 抛 AssertionError（图内表现为
    agent_error，用户看不到可操作原因）。改为结构化错误后应抛 AttendanceRuleError
    且消息含重复姓名。
    """
    with pytest.raises(AttendanceRuleError, match="出现多次") as excinfo:
        # 张三 重名（同名不同 id 在 ERP 侧合并为同名候选），且张三本人未被提及
        plan_attendance("2026-08-20", ["张三", "李四", "张三"],
                        [exc("李四", overtime=1.0)])
    message = str(excinfo.value)
    assert "张三" in message                     # 指出是哪个姓名重复
    assert not isinstance(excinfo.value, AssertionError)


def test_duplicate_employee_names_rejected_when_mentioned() -> None:
    """重名员工本人被提及时同样拦截（不因提及而绕过）。"""
    with pytest.raises(AttendanceRuleError, match="出现多次"):
        plan_attendance("2026-08-20", ["张三", "张三"], [exc("张三", leave=2.0)])


def test_unique_names_multiple_employees_not_affected() -> None:
    """反向用例：姓名唯一且员工数 >1 的正常场景不被重名检测误伤。"""
    plan = plan_attendance("2026-08-20", ["张三", "李四", "王五"],
                           [exc("李四", overtime=2.0)])
    assert plan.counts() == {"created": 3, "updated": 0, "unchanged": 0}
    assert {p.employee_ref for p in plan.all_records} == {"张三", "李四", "王五"}


def test_unique_names_with_existing_records_not_affected() -> None:
    """反向用例：姓名唯一 + 增量场景（含 unchanged）不被重名检测误伤。"""
    existing = {
        "张三": ExistingRecord("张三", 120, None),
        "李四": ExistingRecord("李四", None, 180),
    }
    plan = plan_attendance("2026-08-20", ["张三", "李四", "王五"],
                           [exc("王五", leave=1.0)], existing)
    assert plan.counts() == {"created": 1, "updated": 0, "unchanged": 2}


# ---------------------------------------------------------------------------
# 请假加班共存（规则 8）
# ---------------------------------------------------------------------------


def test_leave_and_overtime_coexist_same_record() -> None:
    """规则 8：同日请假加班共存，不同字段互不覆盖。"""
    plan = plan_attendance("2026-08-20", ["张三"], [
        exc("张三", leave=2.0, overtime=1.5),
    ])
    record = plan.created[0]
    assert record.leave_minutes == 120
    assert record.overtime_minutes == 90


def test_incremental_leave_preserved_when_overtime_added() -> None:
    """增量补加班时保留已有请假（不互相覆盖）。"""
    existing = {"张三": ExistingRecord("张三", 120, None)}
    plan = plan_attendance("2026-08-20", ["张三"],
                           [exc("张三", overtime=3.0)], existing)
    updated = plan.updated[0]
    assert updated.leave_minutes == 120       # 保留已有
    assert updated.overtime_minutes == 180    # 新增
    assert updated.action == ACTION_UPDATED


# ---------------------------------------------------------------------------
# 小时转分钟（规则 9/10）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hours,minutes", [
    (1.5, 90),
    (2.0, 120),
    (0.25, 15),
    (1.1, 66),
    (0.1, 6),
    (3, 180),
])
def test_hours_to_integer_minutes(hours: float, minutes: int) -> None:
    """规则 9/10：小时 → 整数分钟。"""
    assert hours_to_minutes(hours) == minutes


@pytest.mark.parametrize("bad_hours", [0.33333333, 1.0001, 0.033333333])
def test_non_integer_minutes_requires_correction(bad_hours: float) -> None:
    """规则 10：×60 不能得到整数分钟 → 要求用户修正（不猜测取整）。"""
    with pytest.raises(AttendanceRuleError, match="整数分钟"):
        hours_to_minutes(bad_hours)
    with pytest.raises(AttendanceRuleError, match="整数分钟"):
        plan_attendance("2026-08-20", ["张三"],
                        [exc("张三", leave=bad_hours)])


# ---------------------------------------------------------------------------
# >0 校验与不写 0（规则 11）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwarg,value", [
    ("leave", 0.0),
    ("leave", -1.0),
    ("overtime", 0),
    ("overtime", -0.5),
])
def test_exception_must_be_positive(kwarg: str, value: float) -> None:
    """规则 11：请假和加班必须大于 0。"""
    with pytest.raises(AttendanceRuleError, match="必须大于 0"):
        plan_attendance("2026-08-20", ["张三"],
                        [exc("张三", **{kwarg: value})])


def test_absent_exception_never_written_as_zero() -> None:
    """规则 11：没有发生的项目不主动写 0（None 而非 0）。"""
    plan = plan_attendance("2026-08-20", ["张三", "李四"],
                           [exc("张三", leave=2.0)])
    by_name = {p.employee_ref: p for p in plan.created}
    assert by_name["张三"].overtime_minutes is None
    assert by_name["李四"].leave_minutes is None
    assert by_name["李四"].overtime_minutes is None
    assert 0 not in (by_name["张三"].leave_minutes, by_name["李四"].overtime_minutes)
    # 计划内任何记录都不含 0 分钟（写库侧 D12 由 None 决定不写该字段）
    for record in plan.all_records:
        assert record.leave_minutes != 0
        assert record.overtime_minutes != 0


# ---------------------------------------------------------------------------
# 正常出勤不要求工时（规则 12）
# ---------------------------------------------------------------------------


def test_normal_attendance_requires_no_hours() -> None:
    """规则 12：正常出勤不要求用户输入工时；全空异常条目走正常路径。"""
    plan = plan_attendance("2026-08-20", ["张三"], [
        exc("张三", None, None),  # 全空条目：无异常
    ])
    record = plan.created[0]
    assert record.has_exception() is False
    assert record.action == ACTION_CREATED


def test_no_hours_needed_for_anyone() -> None:
    """无任何异常时全员正常出勤（不要求任何工时输入）。"""
    plan = plan_attendance("2026-08-20", EMPLOYEES)
    assert plan.counts() == {"created": 4, "updated": 0, "unchanged": 0}
    assert all(not r.has_exception() for r in plan.created)


# ---------------------------------------------------------------------------
# 边界与不变量
# ---------------------------------------------------------------------------


def test_plan_covers_all_employees_exactly_once() -> None:
    """规则 1 不变量：计划覆盖全部有效员工，每人恰好一条。"""
    plan = plan_attendance("2026-08-20", EMPLOYEES, [
        exc("张三", leave=1.5),
        exc("李四", overtime=2.0),
    ], {
        "王五": ExistingRecord("王五"),
    })
    planned = plan.all_records
    assert len(planned) == 4
    assert {p.employee_ref for p in planned} == set(EMPLOYEES)


def test_empty_employee_set_yields_empty_plan() -> None:
    """没有有效员工 → 空计划（不报错）。"""
    plan = plan_attendance("2026-08-20", [])
    assert plan.counts() == {"created": 0, "updated": 0, "unchanged": 0}
    assert plan.all_records == ()


def test_exception_for_unknown_employee() -> None:
    """exceptions 中的员工不在有效集合 → 数据一致性错误（不静默丢弃）。"""
    with pytest.raises(AttendanceRuleError, match="不在有效员工集合"):
        plan_attendance("2026-08-20", ["张三", "李四"],
                        [exc("不存在的员工", leave=2.0)])


def test_plan_counts_match_output_contract() -> None:
    """4.2 输出口径：counts 提供 created/updated/unchanged 数量。"""
    plan = plan_attendance("2026-08-20", ["张三", "李四"], [
        exc("张三", leave=2.0),
    ], {"李四": ExistingRecord("李四", None, 60)})
    assert plan.counts() == {"created": 1, "updated": 0, "unchanged": 1}
    assert isinstance(plan, AttendancePlan)


def test_updated_record_keeps_unmentioned_field() -> None:
    """更新只动本次字段：未提字段保留原值（不写 None 覆盖）。"""
    existing = {"张三": ExistingRecord("张三", 120, 90)}
    plan = plan_attendance("2026-08-20", ["张三"],
                           [exc("张三", leave=1.0)], existing)
    updated = plan.updated[0]
    assert updated.leave_minutes == 60      # 本次新值
    assert updated.overtime_minutes == 90   # 保留原值
