"""通用批量执行壳（domains/batch.py）+ 批量卡基元单测。

[2026-09-12 一张卡多条明细] 阶段 0/1：

- 卡片基元：ConfirmationCard 新增两个纯展示级字段（不进哈希、旧卡 to_dict
  逐字节不变）；考勤卡显式声明行头/量词；build_batch_detail_rows 公共辅助
  的同标签两行不折叠（防 dict() 静默丢行 + 哈希漏检）。
- 批量壳：op_id 稳定、重复条目键唯一、skip、重试上限不循环、部分失败留
  awaiting_recovery 保持 running、全成功 mark_succeeded。

测试用真实 TaskService/AuditService + 内存仓储（与 tests/domains/conftest
同型），runner 模拟执行内核（内部写 attempt/result，与 attendance 分工一致）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from administrative_agent.agent.tools.attendance import (
    build_attendance_confirmation_card,
)
from administrative_agent.audit.service import AuditService
from administrative_agent.business.attendance_rules import (
    ACTION_CREATED,
    AttendancePlan,
    PlannedRecord,
)
from administrative_agent.domains.batch import (
    ERROR_PARTIAL,
    ERROR_RETRY_LIMIT,
    BatchExecutor,
    BatchItem,
    BatchItemResult,
    build_item_ids,
    stable_item_operation_id,
)
from administrative_agent.domains.orders import (
    ACTION_CREATE,
    ACTION_UNCHANGED,
    BATCH_HASH_PREFIX,
    ConfirmationCard,
    build_batch_detail_rows,
    canonical_json,
    card_payload_hash,
)
from administrative_agent.persistence.models import TaskStatus
from administrative_agent.tasks.service import TaskService
from tests.audit.conftest import (
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.domains.conftest import create_task
from tests.tasks.conftest import InMemoryTaskRepository

TRACE_ID = "trace-batch-test-000001"
ACTOR_ACCOUNT = "manager01"
ACTOR_ROLE = "manager"
TOOL_KEY = "batch_test"


def make_services() -> tuple[TaskService, AuditService]:
    tasks = TaskService(InMemoryTaskRepository())
    audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
    return tasks, audit


def make_runner(
    audit: AuditService,
    task_id: str,
    *,
    fail_items: frozenset[str] = frozenset(),
    calls: list[str] | None = None,
):
    """模拟执行内核：记录 attempt → 按 fail_items 成败 → 记 result → 返回结果。"""
    calls = calls if calls is not None else []

    def runner(item: BatchItem, op_id: str) -> BatchItemResult:
        calls.append(item.item_id)
        attempt = audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=TRACE_ID,
            target_type=TOOL_KEY,
            actor_account=ACTOR_ACCOUNT,
            actor_role=ACTOR_ROLE,
        )
        if item.item_id in fail_items:
            audit.record_result(
                attempt_id=attempt.attempt_id,
                result_status="failed",
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                error_code="BOOM",
                result_summary="boom",
            )
            return BatchItemResult(
                index=item.index, item_id=item.item_id, label=item.label,
                final_action="failed", status="failed", error_code="BOOM",
                error_summary="boom", attempt_id=attempt.attempt_id,
            )
        audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=TRACE_ID,
            actor_account=ACTOR_ACCOUNT,
            actor_role=ACTOR_ROLE,
            result_summary="ok",
        )
        return BatchItemResult(
            index=item.index, item_id=item.item_id, label=item.label,
            final_action=item.action, status="success",
            attempt_id=attempt.attempt_id,
        )

    return runner, calls


# ---------------------------------------------------------------------------
# 批量卡基元
# ---------------------------------------------------------------------------


def _base_card() -> ConfirmationCard:
    return ConfirmationCard(
        operation_type="op",
        target="t",
        fields_to_write=(("a", 1),),
        auto_filled=(),
        expected_impact="i",
        limitations=(),
        payload_hash="",
        hash_fields=(("a", 1),),
        hash_target="t",
    )


def test_row_fields_not_in_payload_hash() -> None:
    """两个新增展示字段绝不进 payload_hash（带/不带哈希必须相等）。"""
    plain = _base_card()
    decorated = replace(
        plain, row_header_label="明细", row_quantifier="条"
    )
    assert card_payload_hash(decorated) == card_payload_hash(plain)


def test_legacy_card_to_dict_unchanged() -> None:
    """未设置新字段时 to_dict() 逐字节不变（不出现新键）。"""
    data = _base_card().to_dict()
    assert "row_header_label" not in data
    assert "row_quantifier" not in data
    expected = {
        "operation_type": "op",
        "target": "t",
        "fields_to_write": {"a": 1},
        "auto_filled": [],
        "expected_impact": "i",
        "limitations": [],
        "payload_hash": "",
    }
    assert data == expected
    assert canonical_json(data) == canonical_json(expected)


def test_decorated_card_to_dict_has_row_fields() -> None:
    """设置新字段时追加到末尾，进 to_dict 但不进哈希。"""
    card = replace(_base_card(), row_header_label="明细", row_quantifier="条")
    data = card.to_dict()
    assert data["row_header_label"] == "明细"
    assert data["row_quantifier"] == "条"
    assert card_payload_hash(card) == card_payload_hash(_base_card())


def test_attendance_card_declares_row_fields() -> None:
    """考勤卡零回归是**显式的**：row_header_label=员工 / row_quantifier=人。"""
    plan = AttendancePlan(
        attendance_date="2026-08-22",
        created=(PlannedRecord("张三", 60, None, ACTION_CREATED),),
    )
    card = build_attendance_confirmation_card("2026-08-22", plan)
    data = card.to_dict()
    assert data["row_header_label"] == "员工"
    assert data["row_quantifier"] == "人"


def test_batch_detail_rows_keep_two_same_label_rows() -> None:
    """两条同业务标识明细：展示外层键唯一（不折叠），hash_fields 两条都进哈希。"""
    entry = {
        "action": ACTION_CREATE,
        "fields": {"customer_id": 3, "model": "M1", "quantity": 12},
    }
    items = [("客户 铭泰 / M1", dict(entry)), ("客户 铭泰 / M1", dict(entry))]
    hash_fields, fields_to_write = build_batch_detail_rows(items)

    # 展示：外层键唯一（序号保证），两条都在
    display = dict(fields_to_write)
    assert len(display) == 2
    assert set(display) == {"1. 客户 铭泰 / M1", "2. 客户 铭泰 / M1"}
    assert display["1. 客户 铭泰 / M1"] == {
        "操作": "新建",
        "字段和值": {"型号": "M1", "数量": 12},  # customer_id 展示层隐藏
    }

    # raw：外层键为位置序号，内层保留原始执行语义（含 customer_id）
    raw = dict(hash_fields)
    assert len(raw) == 2
    assert set(raw) == {f"{BATCH_HASH_PREFIX}1", f"{BATCH_HASH_PREFIX}2"}
    assert raw[f"{BATCH_HASH_PREFIX}1"] == entry

    # 两条都进哈希：与单条哈希不同（防静默折叠导致漏检）
    from administrative_agent.domains.orders import payload_hash_for

    two = payload_hash_for("op", "t", hash_fields)
    one = payload_hash_for("op", "t", hash_fields[:1])
    assert two != one


# ---------------------------------------------------------------------------
# 批量壳
# ---------------------------------------------------------------------------


def test_item_operation_id_is_stable_across_runs() -> None:
    items = [
        BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=2, item_id="B", action=ACTION_CREATE, label="l2"),
    ]
    assert build_item_ids(items) == build_item_ids(items) == ["A", "B"]
    op1 = stable_item_operation_id("task", TOOL_KEY, "A")
    op2 = stable_item_operation_id("task", TOOL_KEY, "A")
    assert op1 == op2 == "task:batch_test:A"


def test_duplicate_payload_items_get_distinct_item_ids() -> None:
    items = [
        BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=2, item_id="A", action=ACTION_CREATE, label="l2"),
        BatchItem(index=3, item_id="B", action=ACTION_CREATE, label="l3"),
    ]
    ids = build_item_ids(items)
    assert ids == ["A#1", "A#2", "B"]
    assert len(set(ids)) == 3
    # 同批次稳定
    assert build_item_ids(items) == ids


def test_duplicate_item_ids_fail_closed_before_any_write() -> None:
    """[2026-09-12 fail-closed] 批内 item_id 重复 → 断言抛错、零写调用。

    调用方未经 build_item_ids 去重即传入同 item_id 两条明细时，op_id 会撞车
    （skip/重试上限判定互相串台），必须在 run 入口 fail closed，绝不静默继续。
    """
    tasks, audit = make_services()
    task = create_task(tasks)
    items = [
        BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=2, item_id="A", action=ACTION_CREATE, label="l2"),
    ]
    runner, calls = make_runner(audit, task.task_id)
    executor = BatchExecutor(tasks=tasks, audit=audit, tool_key=TOOL_KEY)
    with pytest.raises(ValueError):
        executor.run(task_id=task.task_id, items=items, runner=runner)
    assert calls == []  # 断言在任何 runner 调用之前
    # 未写执行台账、任务仍 running
    record = tasks.get_task(task.task_id)
    assert record.current_step != "executing_batch"
    assert record.status == TaskStatus.RUNNING


def test_skipped_when_prior_success_exists() -> None:
    tasks, audit = make_services()
    task = create_task(tasks)
    item = BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1")

    # 预置一条 success attempt（模拟上次执行已成功）
    op_id = stable_item_operation_id(task.task_id, TOOL_KEY, "A")
    attempt = audit.record_attempt(
        task_id=task.task_id, operation_id=op_id, trace_id=TRACE_ID,
        target_type=TOOL_KEY, actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE,
    )
    audit.record_result(
        attempt_id=attempt.attempt_id, result_status="success",
        trace_id=TRACE_ID, actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE,
    )

    def boom_runner(item: BatchItem, op_id: str) -> BatchItemResult:
        raise AssertionError("已有成功 result 时不得再调 runner（零写调用）")

    executor = BatchExecutor(tasks=tasks, audit=audit, tool_key=TOOL_KEY)
    summary = executor.run(task_id=task.task_id, items=[item], runner=boom_runner)

    assert summary.item_results[0].final_action == "skipped"
    assert summary.item_results[0].status == "success"
    assert summary.counts["skipped"] == 1
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_retry_limit_marks_failed_without_loop() -> None:
    tasks, audit = make_services()
    task = create_task(tasks)
    item = BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1")
    runner, calls = make_runner(
        audit, task.task_id, fail_items=frozenset({"A"})
    )
    executor = BatchExecutor(
        tasks=tasks, audit=audit, tool_key=TOOL_KEY, retry_limit=1
    )
    summary = executor.run(task_id=task.task_id, items=[item], runner=runner)

    assert len(calls) == 1  # 只尝试一次，不循环
    assert summary.item_results[0].final_action == "failed"
    assert summary.item_results[0].error_code == ERROR_RETRY_LIMIT
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.FAILED
    assert record.final_error_code == ERROR_RETRY_LIMIT


def test_partial_failure_sets_awaiting_recovery_and_keeps_running() -> None:
    tasks, audit = make_services()
    task = create_task(tasks)
    items = [
        BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=2, item_id="B", action=ACTION_CREATE, label="l2"),
    ]
    runner, _ = make_runner(
        audit, task.task_id, fail_items=frozenset({"B"})
    )
    executor = BatchExecutor(tasks=tasks, audit=audit, tool_key=TOOL_KEY)
    summary = executor.run(task_id=task.task_id, items=items, runner=runner)

    assert summary.verification_passed is False
    assert summary.error_code == ERROR_PARTIAL
    assert summary.counts["created"] == 1
    assert summary.counts["failed"] == 1
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING  # 保持 running，可恢复
    assert record.current_step == "awaiting_recovery"


def test_all_success_marks_succeeded_with_verification_passed() -> None:
    tasks, audit = make_services()
    task = create_task(tasks)
    items = [
        BatchItem(index=1, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=2, item_id="B", action=ACTION_CREATE, label="l2"),
    ]
    runner, _ = make_runner(audit, task.task_id)
    executor = BatchExecutor(tasks=tasks, audit=audit, tool_key=TOOL_KEY)
    summary = executor.run(task_id=task.task_id, items=items, runner=runner)

    assert summary.verification_passed is True
    assert summary.error_code is None
    assert summary.counts["created"] == 2
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_counts_and_resume_position_shape() -> None:
    tasks, audit = make_services()
    task = create_task(tasks)
    items = [
        BatchItem(index=1, item_id="U", action=ACTION_UNCHANGED, label="l0"),
        BatchItem(index=2, item_id="A", action=ACTION_CREATE, label="l1"),
        BatchItem(index=3, item_id="B", action=ACTION_CREATE, label="l2"),
    ]
    runner, calls = make_runner(
        audit, task.task_id, fail_items=frozenset({"B"})
    )
    executor = BatchExecutor(tasks=tasks, audit=audit, tool_key=TOOL_KEY)
    summary = executor.run(task_id=task.task_id, items=items, runner=runner)

    # unchanged 零写调用（不进 runner）
    assert calls == ["A", "B"]
    assert summary.counts == {
        "created": 1, "updated": 0, "unchanged": 1, "skipped": 0, "failed": 1,
    }
    assert summary.resume_position is not None
    assert "l2" in summary.resume_position
    data = summary.to_dict()
    assert set(data) == {
        "task_id", "counts", "item_results", "verification_passed",
        "resume_position", "error_code", "error_summary",
    }
    assert data["item_results"][0]["final_action"] == "unchanged"
