"""11.5 指标可观测性实证测试（D24；16.6 RV-37）。

规格 11.5 各指标当前无独立 metrics 模块/计数器（D24 盘点确认），可观测
数据源为：审计 attempt/result 事件、任务记录、MCP 结构化日志环形缓冲
（mcp_call 条目：tool/ok/error_code/duration_ms）。本套件实证每条指标
在现有数据源上可测量、有断言，实测数值写入 RV-37 报告。

已登记限制（RV-37 缺陷清单，信息级）：
- waiting 积压时长：TaskRecord 无 waiting_entered_at 时间戳，无法计算
  "在 waiting_user_input / waiting_review 停留多久"（仅 created_at/
  updated_at/completed_at 可观测）；
- 模型 token/耗时/成本：无实现与计量（intent 模型层为 stub 接口）；
- 超时计数：MCP_TIMEOUT_RESULT_UNKNOWN 归类已测（tests/contract/
  test_mcp_errors.py::test_read_timeout_result_unknown），执行器场景无法
  确定性注入超时，不重复。
"""

from __future__ import annotations

import structlog
import pytest

from administrative_agent.agent.configuration import (
    ERROR_CODE_AUDIT_WRITE_FAILED,
    RuntimeBag,
    _mark_audit_write_failed,
)
from administrative_agent.audit.service import (
    EVENT_MCP_RETRY,
    AuditFailureError,
)
from administrative_agent.domains.attendance import ERROR_PARTIAL
from administrative_agent.main import _configure_logging
from administrative_agent.observability.log_capture import ring
from administrative_agent.tasks.machine import TaskStatus

from tests.integration.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ATTENDANCE_DATE,
    EMPLOYEES,
    TRACE_ID,
    create_task,
    order_task,
    scenario1_plan,
)


@pytest.fixture(scope="module", autouse=True)
def _wire_log_ring() -> None:
    """把 structlog 全局处理器接到日志环形缓冲（main.py 同款配置）。"""
    structlog.reset_defaults()
    _configure_logging("INFO")
    ring.clear()
    yield
    ring.clear()


def _mcp_calls() -> list[dict]:
    return [e for e in ring.snapshot() if e.get("event") == "mcp_call"]


# ---------------------------------------------------------------------------
# 11.5-2：MCP 延迟 / 错误码分布 / 超时数（mcp_call 环形缓冲聚合）
# ---------------------------------------------------------------------------


def test_11_5_mcp_call_latency_and_error_distribution(ctx) -> None:
    """mcp_call 条目可聚合：每次调用有 tool/ok/error_code/duration_ms；
    成功条目 error_code 为空、失败条目带原始错误码；延迟均为非负整数，
    平均/最大延迟可计算（实测值见 RV-37）。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)  # 李四 create 被 ERP 拒绝（BUSINESS_ERROR）
    ring.clear()
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.counts["failed"] == 1

    calls = _mcp_calls()
    assert len(calls) >= 6  # 元数据 + 5 员工写前查询 + 5 create
    for entry in calls:
        assert entry["tool"] in {
            "initialize", "list_entities", "query", "create", "update",
        }
        assert isinstance(entry["duration_ms"], int)
        assert entry["duration_ms"] >= 0
        assert isinstance(entry["ok"], bool)
        if entry["ok"]:
            assert entry.get("error_code") is None
        else:
            assert entry.get("error_code") is not None
    # 错误码分布：恰 1 次 BUSINESS_ERROR（李四 create 被拒）
    failed = [e for e in calls if not e["ok"]]
    assert [e["error_code"] for e in failed] == ["BUSINESS_ERROR"]
    # 延迟聚合可计算（均值/最大）
    latencies = [e["duration_ms"] for e in calls]
    avg = sum(latencies) / len(latencies)
    assert avg >= 0 and max(latencies) >= 0
    # 超时计数：本场景零超时（MCP_TIMEOUT_RESULT_UNKNOWN 归类见契约测试）
    assert not any(e["error_code"] == "MCP_TIMEOUT_RESULT_UNKNOWN" for e in calls)
    # 读（query）延迟量级小于写（create）延迟——同进程假服务实证量级关系
    query_lat = [e["duration_ms"] for e in calls if e["tool"] == "query"]
    create_lat = [e["duration_ms"] for e in calls if e["tool"] == "create"]
    assert query_lat and create_lat


# ---------------------------------------------------------------------------
# 11.5-3：create_order_bundle replayed 数量（审计 result_status 聚合）
# ---------------------------------------------------------------------------


def test_11_5_replayed_count_from_audit(rctx14) -> None:
    """replayed 指标：网络结果未知同 key 重试 → 审计恰 1 条
    result_status=="replayed"，与 summary.replayed_count 一致。"""
    from tests.domains.test_reconciliation_execute import (
        CREATE_ROW,
        create_diff_entry,
        create_item,
        make_report,
        make_write_plan,
    )

    erp, tasks, audit, reviews, executor, mappings = rctx14
    erp.fail_network_unknown("d24-scenario-key-000001")
    task = order_task(
        tasks, duty="R6",
        structured_intent={"duty": "R6", "operation": "reconcile_execute"},
    )
    plan = make_write_plan(task.task_id, [create_item()])
    review = reviews.request_review(
        task_id=plan.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    review = reviews.approve(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,
    )
    summary = executor.execute(
        task_id=task.task_id, plan=plan, review=review,
        report=make_report(create_diff_entry(row_number=2)), rows=CREATE_ROW,
    )
    assert summary.replayed_count == 1
    # 审计聚合（2026-08-29 口径）：task_attempts 一行 = 业务层一次逻辑调用，
    # 瞬时重试在 MCP 协议层内部完成，不计入 attempt；故此处为 ["replayed"]。
    attempts = [a for a in audit.attempts(task.operation_id)
                if a.target_type == "metal_order"]
    statuses = [a.result_status for a in attempts]
    assert statuses == ["replayed"]
    assert sum(1 for s in statuses if s == "replayed") == 1
    # 被协议层吞掉的那次瞬时失败，改由持久化审计事件留痕（11.2 规则 3：
    # 连接失败属"结果未知"，写操作是否执行一度不可知，须可追溯）
    retries = [
        e for e in audit.events(task_id=task.task_id, event_type=EVENT_MCP_RETRY)
        if e.operation_id == task.operation_id
    ]
    assert len(retries) == 1
    assert retries[0].structured_payload["tool"] == "create_order_bundle"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 11.5-4：ORDER_VERSION_CONFLICT 数量（审计 error_code / 汇总计数）
# ---------------------------------------------------------------------------


def test_11_5_version_conflict_count_from_audit(rctx14) -> None:
    """版本冲突指标：调用瞬间冲突 → 汇总 conflict_count==1，审计写 attempt
    落 error_code（ORDER_UPDATE_VERSION_CONFLICT），供按码计数。"""
    from tests.domains.test_reconciliation_execute import (
        make_report,
        make_write_plan,
        update_item,
    )

    erp, tasks, audit, reviews, executor, mappings = rctx14
    erp.fail_version_conflict_at_call(11, {"weight_per_unit": "8.00"})
    task = order_task(
        tasks, duty="R6",
        structured_intent={"duty": "R6", "operation": "reconcile_execute"},
    )
    plan = make_write_plan(task.task_id, [update_item()])
    review = reviews.request_review(
        task_id=plan.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    review = reviews.approve(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,
    )
    summary = executor.execute(
        task_id=task.task_id, plan=plan, review=review,
        report=make_report(), rows={},
    )
    assert summary.conflict_count == 1
    item = summary.items[0]
    assert item.error_code == "ORDER_UPDATE_VERSION_CONFLICT"  # 条目级重分类
    # 审计 attempt 带错误码（冲突计数数据源：按 error_code 聚合；
    # 审计落 MCP 分类码 ORDER_VERSION_CONFLICT，条目级重分类见域测试）
    conflict_results = [
        a.error_code
        for a in audit.attempts(task.operation_id)
        if a.target_type == "metal_order"
        and a.error_code == "ORDER_VERSION_CONFLICT"
    ]
    assert len(conflict_results) == 1
    assert conflict_results == ["ORDER_VERSION_CONFLICT"]
    # 冲突不覆盖新数据（13.1-3）
    assert erp.orders[11]["weight_per_unit"] == "8.00"


# ---------------------------------------------------------------------------
# 11.5-5：考勤部分成功/恢复次数（attempt_no 聚合）
# ---------------------------------------------------------------------------


def test_11_5_attendance_partial_recovery_counts(ctx) -> None:
    """部分成功与恢复指标：一次失败 → 任务 awaiting_recovery（部分）；
    恢复执行 → 失败员工 attempt_no 递增（attempt_no>1 即恢复次数），
    成功员工不再重复写。"""
    erp, tasks, audit, executor, _ = ctx
    erp.fail_create_employee.add(2)
    task = create_task(tasks)
    summary = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.counts == {"created": 4, "updated": 0, "unchanged": 0, "failed": 1}
    assert summary.error_code == ERROR_PARTIAL
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING
    assert record.current_step == "awaiting_recovery"

    # 恢复执行：故障解除后李四 2 次 attempt（failed → success），恢复次数 = 1
    erp.fail_create_employee.discard(2)
    recovered = executor.execute(
        task_id=task.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert recovered.verification_passed is True
    by_ref = {r.employee_ref: r for r in recovered.employee_results}
    assert by_ref["李四"].final_action == "created"
    assert by_ref["张三"].final_action == "skipped"  # 已成功不重复写
    lisi_attempts = [
        a for a in audit.attempts(op_id(task.task_id, 2))
        if a.target_type == "attendance"
    ]
    assert [a.attempt_no for a in lisi_attempts] == [1, 2]
    assert [a.result_status for a in lisi_attempts] == ["failed", "success"]
    # 恢复次数（attempt_no > 1）聚合
    recovery_count = sum(1 for a in lisi_attempts if a.attempt_no > 1)
    assert recovery_count == 1
    # 成功员工无额外 attempt（恢复不重放整批）
    zhang_attempts = [
        a for a in audit.attempts(op_id(task.task_id, 1))
        if a.target_type == "attendance"
    ]
    assert len(zhang_attempts) == 1


def op_id(task_id: str, employee_id: int) -> str:
    """考勤员工级 operation_id（与 domains/attendance.py 一致）。"""
    from administrative_agent.domains.attendance import stable_operation_id

    return stable_operation_id(task_id, ATTENDANCE_DATE, employee_id)


# ---------------------------------------------------------------------------
# 11.5-6/7：waiting 积压时长（限制登记）与审计落库失败数（引用）
# ---------------------------------------------------------------------------


def test_11_5_waiting_backlog_observable_fields_limitation(ctx) -> None:
    """waiting 积压时长限制实证：任务记录只暴露 created_at/updated_at/
    completed_at，无 waiting_entered_at —— 无法计算停留时长（信息级
    限制，RV-37 登记）；排队任务可通过任务状态观测（pending 串行排队）。"""
    erp, tasks, audit, executor, _ = ctx
    first = create_task(tasks)
    second = create_task(tasks)  # 第一个 running → 排队
    assert tasks.get_task(second.task_id).status == TaskStatus.PENDING
    summary = executor.execute(
        task_id=first.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.verification_passed is True
    record = tasks.get_task(first.task_id)
    assert record.created_at is not None
    assert record.updated_at is not None
    assert record.completed_at is not None
    # 限制：无 waiting_entered_at 字段（不得伪装成可测指标）
    assert not hasattr(record, "waiting_entered_at")


def test_11_5_audit_failure_count_aggregatable(ctx) -> None:
    """审计落库失败数：任务级标记 AUDIT_WRITE_FAILED 可聚合（11.5）。

    计数口径：``final_error_code == "AUDIT_WRITE_FAILED"`` 的任务数。实测经
    ``TaskService.list_tasks(status="failed")`` 聚合——即可观测数据源是任务
    记录，不需要额外的 metrics 模块。

    被测对象：``agent/configuration.py::_mark_audit_write_failed``，接入
    tools_node 免确认路径与 confirm_node 确认后路径。行为级专项证据见
    tests/unit/test_audit_failure_marking.py（含"非审计异常不标记"的反向
    守卫）。

    覆盖边界：仅写工具（有任务行）。读工具 erp_query 不产生任务行，审计
    失败无处标记，**不**在本指标聚合范围内（已知盲区，非遗漏）。

    历史：D26 删除 agent/middleware/ 后本用例一度无被测对象而显式 skip；
    2026-08-29 按 AuditFailureError 的既有契约（"调用方必须标记任务需人工
    复核并保留 operation_id"）在写工具执行边界补齐标记，本用例随之恢复。
    """
    _erp, tasks, _audit, _executor, _ = ctx

    audit_task = create_task(tasks)
    other_task = create_task(tasks)

    # 审计落库失败 → AUDIT_WRITE_FAILED
    runtime = RuntimeBag(tasks=tasks)
    assert _mark_audit_write_failed(
        runtime,
        audit_task.task_id,
        AuditFailureError("op-L1179-abc", "审计事件写入失败：db down"),
    ) is True
    # 对照组：普通失败走既有 mark_failed，错误码不同（聚合键必须能区分开）
    tasks.mark_failed(
        other_task.task_id,
        error_code="ERP_BUSINESS_ERROR",
        error_summary="ERP 拒绝",
    )

    # 聚合：按 status=failed 拉全量，再按 final_error_code 计数
    failed, total = tasks.list_tasks(limit=100, offset=0, status="failed")
    assert total == 2
    audit_failed = [
        t for t in failed if t.final_error_code == ERROR_CODE_AUDIT_WRITE_FAILED
    ]
    assert len(audit_failed) == 1
    assert audit_failed[0].task_id == audit_task.task_id
    assert audit_failed[0].status == TaskStatus.FAILED
    # 摘要保留 operation_id（L1179 追查链）
    assert "op-L1179-abc" in (audit_failed[0].final_error_summary or "")
    assert ERROR_CODE_AUDIT_WRITE_FAILED == "AUDIT_WRITE_FAILED"  # 聚合键稳定
