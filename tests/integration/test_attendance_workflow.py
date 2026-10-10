"""R1 考勤执行编排集成测试（4.3 顺序 6-12；14.3 场景 1/2；14.1 L1378）。

覆盖（规格 14.1"attempt/result 顺序和部分成功恢复"）：
- RV-17 正常批量：每位有效员工当天恰好一条 → succeeded(verification_passed=True)；
- 4.3 顺序 7/9：attempt 调用前、result 调用后，每次实际调用独立 attempt（L1227）；
- 4.3 顺序 8：写前再次 query 分流 create/update（防重）；
- 4.4 稳定 operation_id {task_id}:attendance:{date}:{employee_id}；
- RV-18 部分成功恢复：单条失败不重放批次、已成功员工不重复写、
  失败员工可继续、写前查询防重复创建、达上限 failed 不循环；
- 8.1 succeeded 门槛（verification_passed=True）；
- 9.5 契约错误停止不写；13.4 元数据 stale 拒绝写；4.3 顺序 2 引用不一致拒绝。

假服务为 tests/integration/conftest.py 的 FakeAttendanceErp（respx 模拟
ERP，非真实运行态——D5 规则标注来源）。

**文件尾部另有一组"考勤确认卡端到端"用例**（2026-08-30 撤回考勤免确认后
补的 P1 通车验证）：走 tools_node → 出确认卡 → confirm_node 批准 → 执行
全链路，ERP 由 FakeMCPClient 内存假客户端承担（零 HTTP、零真实 ERP）。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from administrative_agent import lifecycle
from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.attendance import (
    _plan_from_dict,
    build_attendance_confirmation_card,
)
from administrative_agent.audit.service import AuditService
from administrative_agent.business.attendance_rules import (
    AttendanceException,
    ExistingRecord,
    plan_attendance,
)
from administrative_agent.business.matching import EmployeeCandidate
from administrative_agent.domains.attendance import (
    ERROR_CONTRACT,
    ERROR_PARTIAL,
    ERROR_RETRY_LIMIT,
    ERROR_VERIFY_FAILED,
    AttendanceContractError,
    AttendanceExecutor,
    stable_operation_id,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskNotRunningError, TaskService
from tests.api.conftest import FakeMCPClient
from tests.audit.conftest import (
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)

from tests.integration.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ATTENDANCE_DATE,
    ATTENDANCE_ENTITY_META,
    EMPLOYEES,
    EMPLOYEE_NAMES,
    FakeAttendanceErp,
    TRACE_ID,
    create_task,
    scenario1_plan,
)
from tests.tasks.conftest import InMemoryTaskRepository

RETRY_LIMIT_ONE = 1  # 达上限演示用


def op_id(task_id: str, employee_id: int) -> str:
    return stable_operation_id(task_id, ATTENDANCE_DATE, employee_id)


def attempts_by_employee(audit: AuditService, task_id: str) -> dict[str, list]:
    """task 下按稳定 operation_id 分组的 attempt 记录。"""
    grouped: dict[str, list] = {}
    for employee in EMPLOYEES:
        grouped[employee.name] = audit.attempts(op_id(task_id, employee.employee_id))
    return grouped


# ---------------------------------------------------------------------------
# RV-17：正常批量执行（4.3 顺序 6-12 全链路）
# ---------------------------------------------------------------------------


def test_normal_batch_all_employees_created(ctx) -> None:
    """14.3 场景 1 首次录入：5 名员工全部 created，每人恰好一条 → succeeded。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )

    assert summary.effective_employee_count == 5
    assert summary.counts == {"created": 5, "updated": 0, "unchanged": 0, "failed": 0}
    assert summary.verification_passed is True
    assert summary.resume_position is None
    # 每位员工最终结果：created + 记录 ID 回填
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert {r.final_action for r in summary.employee_results} == {"created"}
    assert {r.status for r in summary.employee_results} == {"success"}
    assert all(r.record_id is not None for r in summary.employee_results)
    assert by_ref["张三"].record_id is not None  # 请假 120
    assert by_ref["李四"].record_id is not None  # 加班 180

    # ERP 侧：5 次 create、唯一约束无重复、当天 5 条记录
    assert len(erp.created_calls) == 5
    assert len(erp.records) == 5
    assert len(erp.updated_calls) == 0

    # 任务 succeeded（verification_passed=True 唯一成功路径）
    record = tasks.get_task(task.task_id)
    assert record is not None
    assert record.status == TaskStatus.SUCCEEDED
    assert record.execution_plan is not None  # 4.3 顺序 6
    assert record.execution_plan["planned_counts"] == {
        "created": 5, "updated": 0, "unchanged": 0,
    }


def test_per_employee_stable_operation_id(ctx) -> None:
    """4.4：每位员工子操作使用稳定 operation_id {task_id}:attendance:{date}:{employee_id}。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    grouped = attempts_by_employee(audit, task.task_id)
    assert len(grouped["张三"]) == 1
    assert grouped["张三"][0].operation_id == f"{task.task_id}:attendance:{ATTENDANCE_DATE}:1"
    assert grouped["钱七"][0].operation_id == f"{task.task_id}:attendance:{ATTENDANCE_DATE}:5"
    # 全部 5 个员工各 1 个独立 operation_id（互不相同）
    assert len({a.operation_id for g in grouped.values() for a in g}) == 5


def test_attempt_before_result_ordering(ctx) -> None:
    """10.4 顺序 7/11：attempt 在调用前、result 在调用后；每次调用独立 attempt。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    grouped = attempts_by_employee(audit, task.task_id)
    assert len(grouped) == 5
    for name, attempts in grouped.items():
        assert len(attempts) == 1, name
        a = attempts[0]
        assert a.result_status == "success"
        assert a.attempt_no == 1
        assert a.created_at < a.result_at  # 调用前 < 调用后
        assert a.target_type == "attendance"
    # 审计事件：每员工恰 2 条（attempt/result），时序 attempt < result
    events = audit.events(task_id=task.task_id)
    assert len(events) == 10
    for name, attempts in grouped.items():
        ev = audit.events(operation_id=attempts[0].operation_id)
        types = [e.event_type for e in ev]
        assert types == ["attempt", "result"], name
        assert all(e.duty_code == "R1" for e in ev), name  # 职责编号进审计事件


def test_leave_and_overtime_written_to_fields(ctx) -> None:
    """4.1 规则 8/11：请假与加班写入各自字段；无异常员工不写 0（None 不出现）。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    created = {c["data"]["employee_id"]: c["data"] for c in erp.created_calls}
    assert created[1]["leave_minutes"] == 120
    assert "overtime_minutes" not in created[1]  # 请假员工无加班，不写 0
    assert created[2]["overtime_minutes"] == 180
    assert "leave_minutes" not in created[2]
    assert "leave_minutes" not in created[3]  # 正常员工两个字段都不写 0
    assert "overtime_minutes" not in created[3]
    assert created[3]["employee_id"] == 3


# ---------------------------------------------------------------------------
# 4.3 顺序 8：写前查询分流 create/update（增量场景 2）
# ---------------------------------------------------------------------------


def test_incremental_update_uses_update_call(ctx) -> None:
    """14.3 场景 2 增量：已有记录员工写前查询存在 → update（只传变更字段）。"""
    erp, tasks, audit, executor, _ = ctx
    # 预置：张三已有请假 120 分钟（上午录入）
    erp.seed({
        "employee_id": 1, "att_date": ATTENDANCE_DATE,
        "leave_minutes": 120, "overtime_minutes": None,
    })
    # 场景 2：下午再次录入——新增李四加班 3h、钱七补建正常，张三未提及
    plan = plan_attendance(
        ATTENDANCE_DATE,
        EMPLOYEE_NAMES,
        [AttendanceException("李四", overtime_hours=3)],
        existing_records=[ExistingRecord("张三", 120, None)],
    )
    assert {p.employee_ref: p.action for p in plan.all_records} == {
        "张三": "unchanged", "李四": "created", "王五": "created",
        "赵六": "created", "钱七": "created",
    }
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=plan,
        employees=EMPLOYEES,
    )
    assert summary.counts == {"created": 4, "updated": 0, "unchanged": 1, "failed": 0}
    assert summary.verification_passed is True
    assert erp.created_calls and all(
        c["data"]["employee_id"] != 1 for c in erp.created_calls
    )  # 张三未被重复 create
    assert len(erp.updated_calls) == 0  # 场景 2 无 update（李四无已有记录）
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["张三"].final_action == "unchanged"
    assert by_ref["张三"].status == "success"


def test_write_before_query_exists_forces_update(ctx) -> None:
    """顺序 8：写前查询已存在 → update（不重复 create；只传本次变更字段）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.seed({
        "employee_id": 1, "att_date": ATTENDANCE_DATE,
        "leave_minutes": 120, "overtime_minutes": None,
    })
    plan = plan_attendance(
        ATTENDANCE_DATE,
        EMPLOYEE_NAMES,
        [AttendanceException("张三", leave_hours=3)],  # 120 → 180 变更
        existing_records=[ExistingRecord("张三", 120, None)],
    )
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=plan,
        employees=EMPLOYEES,
    )
    assert len(erp.updated_calls) == 1
    update = erp.updated_calls[0]
    assert update["id"] == erp.unique_index[(ATTENDANCE_DATE, 1)]
    assert update["data"] == {"leave_minutes": 180}  # 只传变更字段（规则 8）
    assert summary.counts["updated"] == 1
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["张三"].final_action == "updated"
    # 张三唯一约束未变：ERP 侧仍只有 1 条
    assert len([r for r in erp.records.values() if r["employee_id"] == 1]) == 1


def test_unchanged_employee_no_attempt_no_write(ctx) -> None:
    """规则 5：未提及员工保持原样——无 attempt、无写调用。"""
    erp, tasks, audit, executor, _ = ctx
    erp.seed({
        "employee_id": 1, "att_date": ATTENDANCE_DATE,
        "leave_minutes": 120, "overtime_minutes": None,
    })
    plan = plan_attendance(
        ATTENDANCE_DATE,
        EMPLOYEE_NAMES,
        [AttendanceException("李四", overtime_hours=3)],
        existing_records=[ExistingRecord("张三", 120, None)],
    )
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=plan,
        employees=EMPLOYEES,
    )
    grouped = attempts_by_employee(audit, task.task_id)
    assert grouped["张三"] == []  # unchanged 不产生写调用与 attempt
    assert grouped["李四"] != []
    assert len(erp.updated_calls) == 0
    assert len(erp.created_calls) == 4


# ---------------------------------------------------------------------------
# RV-18：中途失败、恢复与重复防护（4.3 顺序 10、13.2）
# ---------------------------------------------------------------------------


def test_partial_failure_continues_batch_and_task_awaits_recovery(ctx) -> None:
    """单条 CRUD 失败不重放批次：失败员工记录 failed，其余继续执行 →
    部分成功；任务保持 running 等待恢复执行（4.3 顺序 10 / RV-18）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)  # 李四 create 被 ERP 拒绝
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.counts == {"created": 4, "updated": 0, "unchanged": 0, "failed": 1}
    assert summary.verification_passed is False
    assert summary.error_code == ERROR_PARTIAL
    assert "李四" in summary.resume_position
    # 其余员工照常执行（批次不中断）
    assert len(erp.created_calls) == 5  # 李四也发起了调用（被拒绝）
    assert len(erp.records) == 4
    # 李四 attempt result failed + 错误码归类
    attempts = audit.attempts(op_id(task.task_id, 2))
    assert len(attempts) == 1
    assert attempts[0].result_status == "failed"
    assert attempts[0].error_code == "BUSINESS_ERROR"
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["李四"].status == "failed"
    assert by_ref["李四"].error_code == "BUSINESS_ERROR"
    # 任务保持 running（awaiting_recovery）：已成功记录保留，失败员工可继续
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING
    assert record.current_step == "awaiting_recovery"
    assert record.final_error_code is None  # 未终止，部分成功信息在汇总 error_code


def test_recovery_skips_successful_employees_only(ctx) -> None:
    """RV-18 通过标准：恢复执行已成功员工不重复写（跳过），失败员工可继续。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)
    task = create_task(tasks)
    first = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert first.counts["failed"] == 1

    # 故障恢复（如 ERP 侧修复后重启）：同一 task 再次执行
    erp.fail_create_employee.clear()
    second = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert second.verification_passed is True
    # 本次运行实际计数：4 人 skipped（已成功不重复写）+ 李四 created
    assert second.counts == {"created": 1, "updated": 0, "unchanged": 0, "failed": 0}
    by_ref = {r.employee_ref: r for r in second.employee_results}
    # 已成功 4 人：skipped（final_action），不重复写
    for name in ("张三", "王五", "赵六", "钱七"):
        assert by_ref[name].final_action == "skipped", name
    assert by_ref["李四"].final_action == "created"  # 失败员工继续并成功
    # attempt 计数：成功员工仍只有 1 次 attempt；李四 2 次（失败 + 恢复）
    grouped = attempts_by_employee(audit, task.task_id)
    assert len(grouped["张三"]) == 1
    assert len(grouped["李四"]) == 2
    assert grouped["李四"][0].result_status == "failed"
    assert grouped["李四"][1].result_status == "success"
    # ERP 侧无重复创建：总记录数仍为 5（每人恰好一条）
    assert len(erp.records) == 5
    assert len(erp.created_calls) == 6  # 5 首次 + 李四恢复 1 次
    # 任务最终 succeeded
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.SUCCEEDED


def test_recovery_network_unknown_uses_update_not_duplicate(ctx) -> None:
    """4.4 防重：网络结果未知（实际已写入）→ 恢复写前查询分流为 update，
    不重复 create；唯一约束 + 写后验证兜底。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_network_create_employee.add(2)  # 李四 create 已写入但响应丢失
    task = create_task(tasks)
    first = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert first.counts["failed"] == 1
    assert first.verification_passed is False
    assert len(erp.records) == 5  # 李四记录实际已写入（ERP 侧存在）

    second = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert second.verification_passed is True
    by_ref = {r.employee_ref: r for r in second.employee_results}
    assert by_ref["李四"].final_action == "updated"  # 写前查询已存在 → update
    # 李四在 ERP 侧只有 1 条记录（没有重复 create）
    lisi_records = [r for r in erp.records.values() if r["employee_id"] == 2]
    assert len(lisi_records) == 1
    assert len(erp.updated_calls) == 1
    assert erp.updated_calls[0]["id"] == lisi_records[0]["id"]


def test_interrupted_attempt_restarts_employee(ctx) -> None:
    """顺序 10：attempt 已写但 result 缺失（进程中断）→ 恢复重新 attempt
    （attempt_no 递增）并完成；成功员工仍跳过。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    # 模拟中断：手动写一条无 result 的 attempt（进程崩溃于调用前）
    interrupted = audit.record_attempt(
        task_id=task.task_id,
        operation_id=op_id(task.task_id, 1),
        trace_id=TRACE_ID,
        target_type="attendance",
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        duty_code="R1",
    )
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.verification_passed is True
    attempts = audit.attempts(op_id(task.task_id, 1))
    assert len(attempts) == 2  # 中断的那条 + 恢复重新尝试
    assert attempts[0].attempt_id == interrupted.attempt_id
    assert attempts[0].result_status is None  # 原中断 attempt 无 result
    assert attempts[1].attempt_no == 2
    assert attempts[1].result_status == "success"


@pytest.mark.parametrize("ctx", [1], indirect=True)
def test_retry_limit_reached_fails_without_loop(ctx) -> None:
    """13.2：达上限 → failed 不循环（不再发起新写调用、任务终止）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.counts["failed"] == 1
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["李四"].status == "failed"
    assert by_ref["李四"].error_code == ERROR_RETRY_LIMIT  # 达上限归类
    assert summary.resume_position is None  # 达上限不可继续，无恢复位置
    assert sum(
        1 for c in erp.created_calls if c["data"]["employee_id"] == 2
    ) == 1  # 李四仅 1 次写调用，不循环
    assert len(erp.created_calls) == 5
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.FAILED
    assert record.final_error_code == ERROR_RETRY_LIMIT


# ---------------------------------------------------------------------------
# 8.1 succeeded 门槛 / 最终一致性验证（顺序 11-12）
# ---------------------------------------------------------------------------


def test_verification_failed_when_erp_loses_write(ctx) -> None:
    """写后验证兜底：ERP 返回成功但不落库 → 最终验证缺员工记录 → failed
    （verification_passed=False，不假报成功）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.lose_write_employee.add(5)  # 钱七 create 返回成功但记录丢失
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.verification_passed is False
    assert summary.error_code == ERROR_VERIFY_FAILED
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.FAILED
    assert record.final_error_code == ERROR_VERIFY_FAILED


def test_succeeded_never_marked_without_verification(ctx) -> None:
    """8.1：任何路径都不允许未经验证置 succeeded（执行器不调用 mark_succeeded(False)）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)
    task = create_task(tasks)
    executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING
    assert tasks.get_task(task.task_id).current_step == "awaiting_recovery"


# ---------------------------------------------------------------------------
# 防写边界：契约错误 / 元数据 stale / 状态与引用校验
# ---------------------------------------------------------------------------


def test_contract_error_stops_before_any_write(ctx) -> None:
    """9.5：字段映射失败（ERP 元数据缺字段）→ 停止并报告契约错误，零写调用。"""
    erp, tasks, audit, executor, _ = ctx
    # 替换为缺 leave_minutes 字段的元数据
    erp.entity_meta = FakeAttendanceErp(omit_fields=("leave_minutes",)).entity_meta
    task = create_task(tasks)
    with pytest.raises(AttendanceContractError):
        executor.execute(
            task_id=task.task_id,
            attendance_date=ATTENDANCE_DATE,
            plan=scenario1_plan(),
            employees=EMPLOYEES,
        )
    assert erp.created_calls == []
    assert erp.updated_calls == []
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.FAILED
    assert record.final_error_code == ERROR_CONTRACT


def test_metadata_stale_blocks_writes(ctx) -> None:
    """13.4/D5 决策 a：元数据 stale → 拒绝写入（fail closed，零写调用）。"""
    erp, tasks, audit, executor, metadata = ctx
    task = create_task(tasks)
    metadata._stale = True  # 模拟刷新失败进入 stale
    with pytest.raises(MCPError) as exc_info:
        executor.execute(
            task_id=task.task_id,
            attendance_date=ATTENDANCE_DATE,
            plan=scenario1_plan(),
            employees=EMPLOYEES,
        )
    assert exc_info.value.code == "MCP_METADATA_STALE"
    assert erp.created_calls == []
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.FAILED


def test_non_running_task_rejected(ctx) -> None:
    """8.3：非 running 任务拒绝执行（等待/终态不写入）。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    tasks.need_input(task.task_id, ["等待用户回答"])
    with pytest.raises(TaskNotRunningError):
        executor.execute(
            task_id=task.task_id,
            attendance_date=ATTENDANCE_DATE,
            plan=scenario1_plan(),
            employees=EMPLOYEES,
        )
    assert erp.created_calls == []


def test_plan_ref_mismatch_rejected_before_write(ctx) -> None:
    """4.3 顺序 2：plan 引用未唯一匹配 → 拒绝执行，不写（歧义先询问）。"""
    erp, tasks, audit, executor, _ = ctx
    plan = plan_attendance(
        ATTENDANCE_DATE,
        ["张三", "李四", "王五", "赵六", "钱七", "孙八"],  # 孙八不在候选集合
        [],
    )
    task = create_task(tasks)
    with pytest.raises(ValueError) as exc_info:
        executor.execute(
            task_id=task.task_id,
            attendance_date=ATTENDANCE_DATE,
            plan=plan,
            employees=EMPLOYEES,
        )
    assert "孙八" in str(exc_info.value)
    assert erp.created_calls == []


def test_summary_six_elements_of_42_contract(ctx) -> None:
    """4.2 输出六要素：日期/有效员工总数/四类数量/每位员工结果/最终验证/恢复位置。"""
    erp, tasks, audit, executor, _ = ctx
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    data = summary.to_dict()
    assert set(data.keys()) == {
        "attendance_date",
        "effective_employee_count",
        "counts",
        "employee_results",
        "verification_passed",
        "resume_position",
    }
    assert data["attendance_date"] == ATTENDANCE_DATE
    assert data["effective_employee_count"] == 5
    assert data["counts"] == {"created": 5, "updated": 0, "unchanged": 0, "failed": 0}
    assert len(data["employee_results"]) == 5
    assert data["verification_passed"] is True
    assert data["resume_position"] is None


def test_query_failure_marks_employee_failed_no_write(ctx) -> None:
    """写前查询失败（结果未知）→ 该员工 failed 且不发写调用，批次继续。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_query_employee.add(2)  # 李四写前查询失败
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    by_ref = {r.employee_ref: r for r in summary.employee_results}
    assert by_ref["李四"].status == "failed"
    assert by_ref["李四"].error_code == "MCP_SERVER_ERROR"
    assert summary.counts["failed"] == 1
    # 李四未发出写调用（写前查询失败不猜测），其余员工照常
    assert len(erp.created_calls) == 4
    assert all(c["data"]["employee_id"] != 2 for c in erp.created_calls)
    assert summary.verification_passed is False


# ---------------------------------------------------------------------------
# 考勤确认卡端到端（2026-08-30 撤回考勤免确认后的 P1 通车验证）
#
# 上方用例直测 AttendanceExecutor（respx 假服务 + 真 MCPClient），断言的是
# "执行器本身"。此前**没有任何用例**验证过考勤走 tools_node → 出确认卡 →
# confirm_node 批准 → 执行的完整链路——tests/unit/test_attendance_tools.py
# 只断言了 require_confirmation 这个开关值（P1 缺口，QA 审计 2026-08-30）。
#
# 本组用例补的就是这条链路，三条断言线：
#   1. tools_node 出卡：卡片六要素 + payload_hash + fields_to_write 覆盖全员
#      （"全员覆盖"必须如实反映在卡面上）+ **出卡前零写入**；
#   2. confirm_node 批准：执行的载荷 == 卡片承诺的 plan、写调用条数与参数、
#      任务 succeeded + 审计按分层落 attempt/result；
#   3. payload_hash 防篡改：hash 与卡片不一致 → 重挂等待重新确认，零写入。
#
# 假客户端在 tests/api/conftest.py 的 FakeMCPClient 之上补 attendance 写
# 语义（内存表 + filter），复用不重造；员工一律用假姓名，考勤写调用只落在
# 内存里——考勤记录不可删除，本组用例绝不触碰真实 ERP（零 HTTP）。
# ---------------------------------------------------------------------------

#: 假员工（不使用任何真实员工姓名）
CONFIRM_EMPLOYEES: list[EmployeeCandidate] = [
    EmployeeCandidate(101, "测试员工甲"),
    EmployeeCandidate(102, "测试员工乙"),
    EmployeeCandidate(103, "测试员工丙"),
]
CONFIRM_THREAD = "thread-attendance-confirm"
#: 写工具（读为 query/list_entities）——"出卡前零写入"的判定依据
WRITE_TOOLS: tuple[str, ...] = ("create", "update", "delete")

# 坑：agent/nodes/__init__.py re-export 了与子模块**同名**的节点函数，
# `from ...nodes import tools_node` 拿到的是函数不是模块——必须走 importlib
# 才能 patch 到 confirm_node 的 interrupt（同 test_audit_failure_marking.py）。
tools_node_mod = importlib.import_module("administrative_agent.agent.nodes.tools_node")
confirm_node_mod = importlib.import_module(
    "administrative_agent.agent.nodes.confirm_node"
)


class FakeAttendanceMCPClient(FakeMCPClient):
    """考勤版假客户端：在基类之上给 attendance 接一个内存表。

    基类（tests/api/conftest.py）的 query 忽略 filter、写工具只记录不落库。
    考勤链路两处依赖真实结果，故必须补写语义：①prepare 查当天已有记录；
    ②执行后最终验证"每位员工恰好一条"（4.2）。

    - create/update → 写内存表并返回 record（含 id，供 record_id 回填）；
    - query(attendance) → 按 filter 回读内存表；
    - 其余实体/操作（employees 查询、list_entities）委派基类。

    读/写分界由 WRITE_TOOLS 判定，测试据此断言写调用次数与参数。
    """

    #: 只暴露 attendance 元数据（9.5 契约校验用，mcp-api.md §3 信封）
    ENTITIES: list[dict] = [ATTENDANCE_ENTITY_META]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.records: dict[int, dict] = {}
        self._next_id = 1000

    # ---- 断言辅助 ----

    @property
    def write_calls(self) -> list[dict]:
        """全部写调用（create/update/delete）。"""
        return [c for c in self.calls if c["tool"] in WRITE_TOOLS]

    @property
    def attendance_rows(self) -> list[dict]:
        """内存考勤记录（id 升序）。"""
        return [self.records[k] for k in sorted(self.records)]

    # ---- 调用分派 ----

    def call_tool(
        self,
        tool_name: str,
        arguments: dict | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        args = dict(arguments or {})
        if tool_name in WRITE_TOOLS or (
            tool_name == "query" and args.get("entity") == "attendance"
        ):
            self.calls.append(
                {"tool": tool_name, "arguments": args, "operation_id": operation_id}
            )
            return self._attendance_call(tool_name, args)
        return super().call_tool(tool_name, args, operation_id, trace_id)

    def _attendance_call(self, tool_name: str, args: dict) -> ToolResult:
        if tool_name == "query":
            items = [
                r
                for r in self.attendance_rows
                if all(
                    r.get(k) == v for k, v in (args.get("filter") or {}).items()
                )
            ]
            return ToolResult(ok=True, data={"items": items, "total": len(items)})
        if tool_name == "create":
            rid = self._next_id
            self._next_id += 1
            self.records[rid] = {"id": rid, **args["data"]}
            return ToolResult(ok=True, data={"status": "created", "record": self.records[rid]})
        if tool_name == "update":
            rid = args["id"]
            if rid not in self.records:
                return ToolResult(
                    ok=False,
                    data={},
                    error=MCPError(
                        code="RECORD_NOT_FOUND",
                        category=ErrorCategory.VALIDATION,
                        message=f"attendance record {rid} not found",
                    ),
                )
            self.records[rid].update(args["data"])
            return ToolResult(ok=True, data={"status": "updated", "record": self.records[rid]})
        raise AssertionError(f"假客户端不支持的写工具：{tool_name}")


@dataclass
class AttendanceConfirmHarness:
    """考勤确认链路装配（假客户端 + 内存任务/审计 + RuntimeBag）。"""

    runtime: RuntimeBag
    client: FakeAttendanceMCPClient
    tasks: TaskService
    audit: AuditService
    employees: list[EmployeeCandidate]

    def tool_state(
        self,
        mentioned: list[str] | None = None,
        exceptions: list[dict] | None = None,
        task_id: str | None = None,
    ) -> dict:
        """首轮 tools_node 的入参 State（模型决策后的 pending_action）。"""
        state: dict[str, Any] = {
            "messages": [HumanMessage(content=f"记录 {ATTENDANCE_DATE} 的考勤")],
            "pending_action": {
                "tool_name": "attendance_plan_execute",
                "tool_arguments": {
                    "attendance_date": ATTENDANCE_DATE,
                    "employees": mentioned or [self.employees[0].name],
                    "exceptions": exceptions
                    if exceptions is not None
                    else [{"employee_ref": self.employees[0].name, "leave_hours": 2}],
                },
            },
        }
        if task_id is not None:  # 13.2：已 running 的写任务复用（不新建）
            state["task_id"] = task_id
        return state

    def prepare(self, **kwargs: Any) -> dict:
        """走 tools_node 出卡，返回 State.pending_confirmation。"""
        out = tools_node_mod.tools_node(self.tool_state(**kwargs), self.runtime)
        assert out["current_step"] == "tool_prepared", out
        return out["pending_confirmation"]

    def attempts_of(self, task_id: str) -> dict[str, list]:
        """task 下按稳定 operation_id 分组的 attempt 记录。"""
        return {
            e.name: self.audit.attempts(
                stable_operation_id(task_id, ATTENDANCE_DATE, e.employee_id)
            )
            for e in self.employees
        }


def _build_harness(
    employee_rows: list[dict], employees: list[EmployeeCandidate]
) -> AttendanceConfirmHarness:
    """装配一条考勤确认链路（假客户端 + 内存任务/审计 + RuntimeBag）。"""
    client = FakeAttendanceMCPClient(rows_by_entity={"employees": employee_rows})
    tasks = TaskService(InMemoryTaskRepository())
    audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
    return AttendanceConfirmHarness(
        runtime=RuntimeBag(
            tasks=tasks,
            audit=audit,
            context=AgentContext(
                user_id=ACTOR_ACCOUNT,
                user_role=ACTOR_ROLE,
                scope=frozenset({"attendance:create", "attendance:update", "erp:query"}),
                thread_id=CONFIRM_THREAD,
            ),
            metadata=EntityCache(client, ttl_seconds=3600),
            mcp_client=client,
            tools=dict(TOOL_REGISTRY),
        ),
        client=client,
        tasks=tasks,
        audit=audit,
        employees=employees,
    )


@pytest.fixture
def cctx() -> AttendanceConfirmHarness:
    """考勤确认链路夹具：3 名假员工（零 HTTP，ERP 全由内存假客户端承担）。

    与上方 ctx 分层：ctx 是 respx 假服务 + 真 MCPClient（测执行器）；本夹具
    不发任何 HTTP 请求，考勤记录不可删除——本组用例绝不触碰真实 ERP。
    """
    lifecycle.reset()  # 停机标志隔离（同 tests/api/conftest.py 的 autouse）
    try:
        yield _build_harness(
            [{"id": e.employee_id, "name": e.name} for e in CONFIRM_EMPLOYEES],
            CONFIRM_EMPLOYEES,
        )
    finally:
        lifecycle.reset()


def _spy_on_attendance_run(harness: AttendanceConfirmHarness) -> list[dict]:
    """给考勤 spec 包一层 run：记录 confirm_node 实际下发的执行载荷。

    用于验证"批准的卡片 = 执行的载荷"——不替换实现，只旁路记录后委派真实
    ``execute_attendance``（ToolSpec 是 frozen dataclass，走 replace）。
    """
    spec = TOOL_REGISTRY["attendance_plan_execute"]
    seen: list[dict] = []

    def run(
        ctx: Any, args: Any = None, task_id: str | None = None,
        execution: dict | None = None,
    ) -> Any:
        seen.append({"task_id": task_id, "execution": execution})
        return spec.run(ctx, args=args, task_id=task_id, execution=execution)

    harness.runtime.tools = {
        **TOOL_REGISTRY,
        "attendance_plan_execute": replace(spec, run=run),
    }
    return seen


# ---- 1. tools_node：出卡且零写入 ----


def test_tools_node_issues_confirmation_card_without_writing(cctx) -> None:
    """P1 通车 1：考勤经 tools_node 出确认卡，出卡前不落任何写调用。

    撤回免确认（2026-08-30）后考勤与 R2/R3/R6 同路径：prepare 只做只读
    前置，卡片进 State 等确认；ERP 侧必须**零写调用**（这是"不再直写"的
    通车证据，此前只有开关值断言）。
    """
    pc = cctx.prepare()

    # 卡片六要素 + payload_hash（7.3）；[2026-09-12 批量卡] 另含两个纯展示级
    # 行头/量词字段（考勤卡显式声明 员工/人，不进哈希）；[2026-10-05 第 3 批]
    # 写分支集中注入整卡级 source_quote（用户原话对照，纯展示级）。
    card = pc["card"]
    assert set(card) == {
        "operation_type", "target", "fields_to_write", "auto_filled",
        "expected_impact", "limitations", "payload_hash",
        "row_header_label", "row_quantifier", "source_quote",
    }
    # 考勤卡不带收支专有的 source_note
    assert "source_note" not in card
    assert card["row_header_label"] == "员工"
    assert card["row_quantifier"] == "人"
    assert card["operation_type"] == "记录每日考勤"
    assert ATTENDANCE_DATE in card["target"]
    assert pc["payload_hash"] == card["payload_hash"]
    assert pc["tool_name"] == "attendance_plan_execute"
    assert pc["policy_key"] == "R1.attendance.daily"

    # 全员覆盖如实上卡：fields_to_write 条目数 == ERP employees 全量人数
    # ——正是必须让用户确认的东西（plan_attendance 会给未提及员工也写记录）
    assert len(card["fields_to_write"]) == len(CONFIRM_EMPLOYEES) == 3
    assert set(card["fields_to_write"]) == {e.name for e in CONFIRM_EMPLOYEES}
    # 请假 2h → 120 分钟；未提及员工不写请假/加班（规则 11：不写 0）
    assert card["fields_to_write"][CONFIRM_EMPLOYEES[0].name]["请假（分钟）"] == 120
    assert card["fields_to_write"][CONFIRM_EMPLOYEES[1].name]["请假（分钟）"] is None

    # 任务 waiting_confirmation + confirmed_payload_hash 落库（7.3 失效依据）
    record = cctx.tasks.get_task(pc["task_id"])
    assert record.status == TaskStatus.WAITING_CONFIRMATION
    assert record.confirmed_payload_hash == pc["payload_hash"]

    # 核心守卫：出卡阶段零写调用（只读前置只发 query）
    assert cctx.client.write_calls == []
    assert {c["tool"] for c in cctx.client.calls} == {"query"}
    assert cctx.client.attendance_rows == []
    assert cctx.audit.events(task_id=pc["task_id"]) == []


# ---- 2. confirm_node：批准 → 执行（载荷 == 卡片承诺）----


def test_confirm_node_executes_the_approved_card_payload(cctx) -> None:
    """P1 通车 2：批准后 spec.run 用的就是确认卡 payload_hash 对应的 plan。

    断言三件事：①执行载荷与卡片逐字节一致（不是被替换/篡改的 plan）；
    ②写出条数与参数符合卡片；③任务 succeeded + 审计按分层落 attempt/result。
    """
    pc = cctx.prepare()
    seen = _spy_on_attendance_run(cctx)

    with patch.object(
        confirm_node_mod,
        "interrupt",
        return_value={"approved": True, "payload_hash": pc["payload_hash"]},
    ):
        out = confirm_node_mod.confirm_node({"pending_confirmation": pc}, cctx.runtime)

    # ① 执行载荷 == 卡片承诺的 plan（双向验证）
    assert len(seen) == 1
    assert seen[0]["task_id"] == pc["task_id"]
    assert seen[0]["execution"] == pc["execution"]
    rebuilt = build_attendance_confirmation_card(
        ATTENDANCE_DATE, _plan_from_dict(seen[0]["execution"]["plan"])
    )
    assert rebuilt.payload_hash == pc["payload_hash"]  # 载荷重建卡 → 同一 hash

    # ② 写调用：全员 3 条 create，参数与卡片一致
    writes = cctx.client.write_calls
    assert [c["tool"] for c in writes] == ["create"] * 3
    by_employee = {c["arguments"]["data"]["employee_id"]: c for c in writes}
    assert set(by_employee) == {e.employee_id for e in CONFIRM_EMPLOYEES}
    assert all(
        c["arguments"]["data"]["att_date"] == ATTENDANCE_DATE for c in writes
    )
    assert by_employee[101]["arguments"]["data"]["leave_minutes"] == 120
    assert "leave_minutes" not in by_employee[102]["arguments"]["data"]
    assert "overtime_minutes" not in by_employee[102]["arguments"]["data"]
    # 内存 ERP（假客户端）里当天恰好 3 条——最终验证通过的前提
    assert len(cctx.client.attendance_rows) == 3

    # ③ 任务终态与审计分层：每人一行 attempt（attempt/result 各一事件）
    assert out["current_step"] == "tool_done"
    record = cctx.tasks.get_task(pc["task_id"])
    assert record.status == TaskStatus.SUCCEEDED
    assert record.execution_plan["planned_counts"] == {
        "created": 3, "updated": 0, "unchanged": 0,
    }
    grouped = cctx.attempts_of(pc["task_id"])
    assert all(len(attempts) == 1 for attempts in grouped.values())
    assert all(a.result_status == "success" for g in grouped.values() for a in g)
    assert len(cctx.audit.events(task_id=pc["task_id"])) == 6  # 3 人 × (attempt+result)


# ---- 3. payload_hash 防篡改 ----


def test_confirm_node_rejects_mismatched_payload_hash_without_writing(cctx) -> None:
    """7.3 防篡改：批准的 hash 与卡片不一致 → 重挂等重新确认，零写入。

    确认卡内容变化（此处模拟批准值被替换为旧/伪造 hash）后，confirm_node
    不得执行——循环回 interrupt 重新挂起等待确认。
    """
    pc = cctx.prepare()

    class _Reinterrupted(Exception):
        """替代第二次 interrupt 的 GraphInterrupt（证明已进入重挂分支）。"""

    state: dict[str, Any] = {"pending_confirmation": pc}
    calls: list[int] = []

    def fake_interrupt(_card: dict) -> dict:
        calls.append(1)
        if len(calls) == 1:
            return {"approved": True, "payload_hash": "tampered-hash-not-the-card"}
        raise _Reinterrupted  # 生产里这里是 interrupt 抛 GraphInterrupt 重挂

    with patch.object(confirm_node_mod, "interrupt", side_effect=fake_interrupt):
        with pytest.raises(_Reinterrupted):
            confirm_node_mod.confirm_node(state, cctx.runtime)

    assert len(calls) == 2  # 首轮 hash 失效 → 再挂起一次（未落入执行分支）
    assert cctx.client.write_calls == []  # 零写入（未执行）
    assert cctx.client.attendance_rows == []
    assert cctx.audit.events(task_id=pc["task_id"]) == []
    record = cctx.tasks.get_task(pc["task_id"])
    assert record.status == TaskStatus.WAITING_CONFIRMATION  # 未被 confirm 推进


def test_tampered_execution_plan_is_rejected_before_any_write(cctx) -> None:
    """篡改 execution 里的 plan（塞入名单外员工）→ 执行器拒绝，零写入。

    4.3 顺序 2 兜底：plan 引用未在有效员工集合中唯一匹配 → ValueError →
    确认卡的 payload_hash 已失效（内容变了），执行器也不放行。

    **已知限制**（本用例之外，报告已记录）：payload_hash 只绑定"卡片 ↔
    批准值"，confirm_node 不拿 execution 重算 hash——若篡改的是**值**（如把
    请假分钟数改大）而非结构，hash 校验抓不到，只能靠 checkpoint 完整性
    与审计留痕兜底。
    """
    pc = cctx.prepare()
    tampered = json.loads(json.dumps(pc["execution"]))  # 深拷贝后篡改
    tampered["plan"]["created"].append(
        {
            "employee_ref": "测试员工丁",  # 不在 ERP employees 名单内
            "leave_minutes": None,
            "overtime_minutes": None,
            "action": "created",
        }
    )
    state: dict[str, Any] = {
        "pending_confirmation": {**pc, "execution": tampered},
    }

    with patch.object(
        confirm_node_mod,
        "interrupt",
        return_value={"approved": True, "payload_hash": pc["payload_hash"]},
    ):
        out = confirm_node_mod.confirm_node(state, cctx.runtime)

    assert out["current_step"] == "tool_failed"
    assert "测试员工丁" in out["messages"][0].content
    assert cctx.client.write_calls == []  # 引用校验在写调用之前，零写入
    assert cctx.client.attendance_rows == []
    # 任务已被 confirm 推进到 running，但执行失败 → 不置 succeeded（8.1）
    assert cctx.tasks.get_task(pc["task_id"]).status == TaskStatus.RUNNING


# ---- 4. 同名员工：卡面折叠风险的守卫（任务三调查结论，见交付报告）----


def test_duplicate_employee_names_block_prepare_before_any_card() -> None:
    """ERP 员工重名 → 计划层拦截：不出卡、零写入、任务 failed（fail closed）。

    这是任务三（``domains/orders.py:106/291 dict(fields_to_write)`` 折叠风险）的结论文据：
    ``plan_attendance`` 的**前置重名检测**（business/attendance_rules.py:126-134，
    对 ``effective_employees`` 判重、抛 ``AttendanceRuleError``）在**卡面构造
    之前**就拦下重名——``build_attendance_confirmation_card`` 根本拿不到含重名
    的 plan，故考勤确认卡**在任何解释器下都不可能**发生条目折叠。

    [2026-09-28 §3.4] 该防护原为 ``plan_attendance`` 末尾的裸 ``assert``：裸
    assert 会被 ``python -O`` 剥离，剥离后重名会穿透到卡面、由
    ``dict(fields_to_write)`` 折叠成一行（payload_hash 亦基于折叠后的 dict 计算，
    卡面与 hash 自洽——不会"批准 A 执行 B"，但卡面少显示一行、误导用户）。本批
    已把防护改为**显式的 ``raise AttendanceRuleError``**（``-O`` 不剥离），并前置到
    循环之前，该"已知限制"随之消除。当前真实 ERP employees 13 人无重名。
    """
    lifecycle.reset()
    try:
        dup_employees = [
            EmployeeCandidate(201, "测试员工甲"),
            EmployeeCandidate(202, "测试员工甲"),  # 同名不同 id
        ]
        harness = _build_harness(
            [{"id": e.employee_id, "name": e.name} for e in dup_employees],
            dup_employees,
        )
        task = create_task(harness.tasks, thread_id=CONFIRM_THREAD)

        out = tools_node_mod.tools_node(
            harness.tool_state(
                mentioned=[dup_employees[0].name], exceptions=[],
                task_id=task.task_id,
            ),
            harness.runtime,
        )

        assert out["current_step"] == "tool_prepare_failed"
        assert "pending_confirmation" not in out  # 不出卡（无卡可确认）
        assert "工具准备失败" in out["messages"][0].content
        assert harness.client.write_calls == []  # 零写入
        # 任务标记 failed 释放串行锁（不卡在 waiting_confirmation）
        record = harness.tasks.get_task(task.task_id)
        assert record.status == TaskStatus.FAILED
        assert record.final_error_code == "preparation_failed"
    finally:
        lifecycle.reset()
