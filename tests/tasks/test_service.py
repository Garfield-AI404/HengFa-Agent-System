"""8.2/8.3 TaskService 测试：创建、迁移全链路、显式 task_id 绑定、串行队列。"""

from __future__ import annotations

import pytest

from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.tasks.machine import TaskStatus, TaskTransitionError
from administrative_agent.tasks.service import (
    TaskNotRunningError,
    TaskService,
    TaskThreadMismatchError,
    VerificationRequiredError,
)
from tests.tasks.conftest import InMemoryTaskRepository, write_spec


# ---- 创建与初始化（8.2 字段） ----

def test_create_readonly_task_pending(service: TaskService) -> None:
    spec = write_spec(is_write=False, duty_code="R4")
    record = service.create_task(spec)
    assert record.task_id.startswith("task-")
    assert record.operation_id.startswith("op-")
    assert record.status == TaskStatus.PENDING
    assert record.current_step == "created"
    assert record.actor_account == "manager01"
    assert record.actor_role == "manager"
    assert record.duty_code == "R4"
    assert record.original_user_message == "记录今天的考勤"
    assert record.structured_intent == {"duty": "R1", "date": "2026-08-21"}
    assert record.completed_at is None


def test_create_write_task_running_when_lock_free(service: TaskService, write_lock: WriteTaskLock) -> None:
    record = service.create_task(write_spec())
    assert record.status == TaskStatus.RUNNING
    assert write_lock.holder("thread-mgr-1") == record.task_id


def test_create_write_task_queued_when_lock_busy(
    service: TaskService, write_lock: WriteTaskLock
) -> None:
    """8.3 第 3 条：前一个写任务未结束时新写任务排队（保持 pending，不 running）。"""
    first = service.create_task(write_spec())
    assert first.status == TaskStatus.RUNNING
    second = service.create_task(write_spec(original_user_message="第二条写任务"))
    assert second.status == TaskStatus.PENDING  # 排队中，不进入 running
    assert write_lock.holder("thread-mgr-1") == first.task_id


def test_create_write_task_after_terminal_queued_task_promoted(
    service: TaskService, write_lock: WriteTaskLock
) -> None:
    """前一个写任务结束（终态）后队头任务自动晋升 running。"""
    first = service.create_task(write_spec())
    second = service.create_task(write_spec(original_user_message="排队任务"))
    assert second.status == TaskStatus.PENDING
    service.mark_succeeded(first.task_id, verification_passed=True)
    promoted = service.get_task(second.task_id)
    assert promoted is not None
    assert promoted.status == TaskStatus.RUNNING
    assert write_lock.holder("thread-mgr-1") == second.task_id


def test_lock_released_on_transition_failure(
    write_lock: WriteTaskLock,
) -> None:
    """防御：acquire 后状态迁移失败时释放锁，防止串行域永久阻塞。"""

    class ExplodingRepo(InMemoryTaskRepository):
        def update(self, task_id: str, **fields):
            raise RuntimeError("db down")

    service = TaskService(ExplodingRepo(), write_lock)
    with pytest.raises(RuntimeError):
        service.create_task(write_spec())
    assert write_lock.holder("thread-mgr-1") is None


# ---- 迁移语义（8.1 每条迁移条件） ----

def test_full_write_lifecycle(service: TaskService) -> None:
    """写任务完整生命周期：pending→running→waiting_user_input→running→succeeded。"""
    record = service.create_task(write_spec())
    assert record.status == TaskStatus.RUNNING
    record = service.need_input(record.task_id, ["缺少员工字段，请确认目标员工"])
    assert record.status == TaskStatus.WAITING_USER_INPUT
    assert record.pending_questions == ["缺少员工字段，请确认目标员工"]
    record = service.resume(record.task_id, thread_id="thread-mgr-1")
    assert record.status == TaskStatus.RUNNING
    record = service.mark_succeeded(record.task_id, verification_passed=True)
    assert record.status == TaskStatus.SUCCEEDED
    assert record.completed_at is not None


def test_confirmation_and_review_paths(service: TaskService) -> None:
    record = service.create_task(write_spec(duty_code="R3"))
    record = service.request_confirmation(record.task_id, payload_hash="hash-abc")
    assert record.status == TaskStatus.WAITING_CONFIRMATION
    assert record.confirmed_payload_hash == "hash-abc"
    record = service.confirm(record.task_id, approved=True, thread_id="thread-mgr-1")
    assert record.status == TaskStatus.RUNNING
    record = service.request_review(record.task_id, payload_hash="hash-r6")
    assert record.status == TaskStatus.WAITING_REVIEW
    assert record.reviewed_payload_hash == "hash-r6"
    record = service.review(record.task_id, approved=True, thread_id="thread-mgr-1")
    assert record.status == TaskStatus.RUNNING


def test_confirmation_rejected_is_cancelled(service: TaskService) -> None:
    """8.1：复核/确认拒绝后终止 → cancelled。"""
    record = service.create_task(write_spec(duty_code="R3"))
    record = service.request_confirmation(record.task_id, payload_hash="h")
    record = service.confirm(record.task_id, approved=False)
    assert record.status == TaskStatus.CANCELLED
    assert record.completed_at is not None

    record2 = service.create_task(write_spec(duty_code="R6"))
    record2 = service.request_review(record2.task_id, payload_hash="h2")
    record2 = service.review(record2.task_id, approved=False)
    assert record2.status == TaskStatus.CANCELLED


def test_mark_failed_records_error(service: TaskService) -> None:
    record = service.create_task(write_spec())
    record = service.mark_failed(record.task_id, error_code="MCP_TIMEOUT_RESULT_UNKNOWN", error_summary="结果未知")
    assert record.status == TaskStatus.FAILED
    assert record.final_error_code == "MCP_TIMEOUT_RESULT_UNKNOWN"
    assert record.final_error_summary == "结果未知"
    assert record.completed_at is not None


def test_cancel_releases_lock_and_promotes(service: TaskService, write_lock: WriteTaskLock) -> None:
    first = service.create_task(write_spec())
    second = service.create_task(write_spec(original_user_message="排队任务"))
    service.cancel(first.task_id)
    assert service.get_task(second.task_id).status == TaskStatus.RUNNING
    assert write_lock.holder("thread-mgr-1") == second.task_id


def test_illegal_transition_rejected(service: TaskService) -> None:
    """非法迁移被拒（RV-09 通过标准）：pending→succeeded 直接拒绝。"""
    record = service.create_task(write_spec(is_write=False))
    with pytest.raises(TaskTransitionError):
        service.transition(record.task_id, TaskStatus.SUCCEEDED)


def test_terminal_state_absorbing(service: TaskService) -> None:
    record = service.create_task(write_spec())
    service.mark_succeeded(record.task_id, verification_passed=True)
    with pytest.raises(TaskTransitionError):
        service.transition(record.task_id, TaskStatus.FAILED)


# ---- 8.1：succeeded 必须经验证路径 ----

def test_succeeded_requires_verification(service: TaskService) -> None:
    """模型不能直接把任务标记为 succeeded：verification_passed=False 拒绝。"""
    record = service.create_task(write_spec())
    with pytest.raises(VerificationRequiredError):
        service.mark_succeeded(record.task_id, verification_passed=False)
    assert service.get_task(record.task_id).status == TaskStatus.RUNNING


# ---- 8.3 第 2 条：后续消息显式绑定 task_id ----

def test_resume_requires_explicit_task_id(service: TaskService) -> None:
    """resume/rebind 一律要求显式 task_id；未知 task_id 拒绝（无"最近挂起"猜测）。"""
    with pytest.raises(KeyError):
        service.resume("task-no-such")
    with pytest.raises(KeyError):
        service.rebind("task-no-such")


def test_resume_rejected_for_non_waiting_task(service: TaskService) -> None:
    """只有等待用户输入的任务可 resume；running 任务 resume 是非法迁移。"""
    record = service.create_task(write_spec())
    with pytest.raises(TaskTransitionError):
        service.resume(record.task_id, thread_id="thread-mgr-1")


def test_resume_thread_mismatch_rejected(service: TaskService) -> None:
    """跨会话/线程恢复被拒（TaskThreadMismatchError）。"""
    record = service.create_task(write_spec())
    service.need_input(record.task_id, ["问题"])
    with pytest.raises(TaskThreadMismatchError):
        service.resume(record.task_id, thread_id="thread-other")


def test_rebind_waits_and_binds_explicitly(service: TaskService) -> None:
    """rebind：等待任务显式绑定后续消息并恢复执行。"""
    record = service.create_task(write_spec())
    service.need_input(record.task_id, ["请确认员工"])
    recovered = service.rebind(record.task_id, thread_id="thread-mgr-1")
    assert recovered.status == TaskStatus.RUNNING


def test_waiting_keeps_lock_and_new_write_queues(
    service: TaskService, write_lock: WriteTaskLock
) -> None:
    """waiting 期间锁保留（口径见 RV-09 §8）：新写任务排队不抢先。"""
    first = service.create_task(write_spec())
    service.need_input(first.task_id, ["问题"])
    second = service.create_task(write_spec(original_user_message="排队任务"))
    assert second.status == TaskStatus.PENDING
    service.resume(first.task_id, thread_id="thread-mgr-1")
    service.mark_succeeded(first.task_id, verification_passed=True)
    assert service.get_task(second.task_id).status == TaskStatus.RUNNING


def test_list_waiting_only_returns_waiting_tasks(service: TaskService) -> None:
    first = service.create_task(write_spec())  # running
    waiting = service.create_task(write_spec(original_user_message="w1"))  # queued pending
    assert service.list_waiting("thread-mgr-1") == []  # 排队中的 pending 不算 waiting
    service.mark_succeeded(first.task_id, verification_passed=True)  # 晋升 → running
    assert service.get_task(waiting.task_id).status == TaskStatus.RUNNING
    service.need_input(waiting.task_id, ["问题"])
    assert {t.task_id for t in service.list_waiting("thread-mgr-1")} == {waiting.task_id}


def test_readonly_tasks_do_not_touch_lock(service: TaskService, write_lock: WriteTaskLock) -> None:
    """8.3 第 3 条：无关的新查询作为独立只读任务执行，不排队不持锁。"""
    write = service.create_task(write_spec())
    assert write_lock.holder("thread-mgr-1") == write.task_id
    read = service.create_task(write_spec(is_write=False, duty_code="R4"))
    assert read.status == TaskStatus.PENDING  # 只读任务无串行域概念，创建后由调用方调度
    assert write_lock.holder("thread-mgr-1") == write.task_id  # 写锁不受影响


def test_different_threads_do_not_queue(service: TaskService, write_lock: WriteTaskLock) -> None:
    first = service.create_task(write_spec())  # thread-mgr-1
    other = service.create_task(write_spec(thread_id="thread-mgr-2"))
    assert first.status == TaskStatus.RUNNING
    assert other.status == TaskStatus.RUNNING  # 另一会话独立执行


def test_update_progress_writes_step_and_plan(service: TaskService) -> None:
    """4.3 顺序 6：running 任务写入当前步骤和完整执行计划（不改变状态）。"""
    task = service.create_task(write_spec())
    assert task.status == TaskStatus.RUNNING
    updated = service.update_progress(
        task.task_id,
        current_step="executing_attendance_plan",
        execution_plan={"planned_counts": {"created": 5}},
    )
    assert updated.status == TaskStatus.RUNNING  # 状态不变
    assert updated.current_step == "executing_attendance_plan"
    assert updated.execution_plan == {"planned_counts": {"created": 5}}


def test_update_progress_rejects_waiting_and_terminal(service: TaskService) -> None:
    """等待/终态任务不得被执行器写入进度（8.3：等待任务等用户，终态不可再推进）。"""
    task = service.create_task(write_spec())
    service.need_input(task.task_id, ["问题"])
    with pytest.raises(TaskNotRunningError):
        service.update_progress(task.task_id, current_step="executing_attendance_plan")
    # 终态同样拒绝（独立线程避免排队影响）
    done = service.create_task(write_spec(thread_id="thread-mgr-2", original_user_message="终态任务"))
    service.mark_succeeded(done.task_id, verification_passed=True)
    with pytest.raises(TaskNotRunningError):
        service.update_progress(done.task_id, current_step="executing_attendance_plan")
