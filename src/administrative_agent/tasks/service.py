"""8.2/8.3 task 记录服务（D6）。

职责：
- 创建任务：生成 task_id/operation_id/trace_id（复用 D5 生成器），初始化状态；
  写任务经 WriteTaskLock 串行化（8.3 第 1/3 条），未获执行权则保持 pending 排队。
- 状态迁移：全部经 machine.assert_transition 确定性校验（8.1），非法迁移拒绝；
  succeeded 只能经验证路径（8.1：模型不能直接把任务标记为 succeeded）。
- 显式 task_id 绑定（8.3 第 2 条）：resume/rebind/confirm/review 一律要求显式
  task_id 与线程校验，不提供"最近挂起任务"猜测接口。
- 终态处理：设置 completed_at/final_error_*，释放写锁并晋升队头任务。
- 服务重启后由 task 状态和 checkpoint 恢复（8.3 第 4 条）属 D7/RV-10；
  本单元只保证状态与锁语义，不实现 checkpoint。

本服务不接触 HTTP 层（路由属后续单元）与审计（D8）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

import structlog

from administrative_agent.mcp.client import new_operation_id, new_trace_id
from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.tasks.machine import (
    TERMINAL_STATES,
    WAITING_STATES,
    TaskStatus,
    TaskTransitionError,
    _coerce_status,
    assert_transition,
)
from administrative_agent.tasks.records import NewTaskSpec, TaskRecord

logger: structlog.stdlib.BoundLogger = structlog.get_logger("tasks.service")

#: 任务 ID 前缀（4.2/7.4 契约：task-…、op-…、trace-…）
_TASK_ID_PREFIX = "task-"


class TaskThreadMismatchError(ValueError):
    """后续消息绑定 task 时线程不匹配（8.3 第 2 条：显式绑定 + 防跨会话操作）。"""

    def __init__(self, task_id: str, expected: str, actual: str) -> None:
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"task {task_id!r} belongs to thread {expected!r}, not {actual!r}"
        )


class VerificationRequiredError(ValueError):
    """succeeded 必须经最终验证（8.1：模型不能直接把任务标记为 succeeded）。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id!r} cannot be marked succeeded without final verification")


class TaskNotRunningError(ValueError):
    """update_progress 只允许 running 任务（4.3 顺序 6；waiting/终态拒绝写入）。"""

    def __init__(self, task_id: str, status: TaskStatus) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(
            f"task {task_id!r} progress cannot be updated in status {status.value!r}"
        )


class TaskNotWaitingInputError(ValueError):
    """replies 只接受 waiting_user_input 任务（8.3 第 2 条：显式绑定语义）。"""

    def __init__(self, task_id: str, status: TaskStatus) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(
            f"task {task_id!r} cannot be answered in status {status.value!r}"
            "（只接受 waiting_user_input）"
        )


class TaskAnswersMismatchError(ValueError):
    """回答数量与 pending_questions 不一致（8.2：回答必须逐题对应）。"""

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        self.task_id = task_id
        super().__init__(
            f"task {task_id!r} 回答数量 {actual} 与待回答问题数 {expected} 不一致"
        )


class TaskConfirmHashMismatchError(ValueError):
    """轻确认提交的 payload_hash 与任务确认内容不一致（7.3 L886：
    执行内容/目标记录/关键字段变化 → 原确认失效，必须重新确认）。"""

    def __init__(self, task_id: str, provided: str, expected: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"task {task_id!r} 轻确认 hash 不一致（提交 {provided[:16]}… vs "
            f"任务确认 {expected[:16]}…）：确认内容已变化，须重新确认（7.3）"
        )


class TaskRepository(Protocol):
    """task 持久化契约。SQLAlchemy 实现见 persistence/repositories.py；
    测试用 in-memory 实现（tests/tasks/）。"""

    def create(self, record: TaskRecord) -> TaskRecord: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def update(self, task_id: str, **fields) -> TaskRecord: ...

    def list_by_thread(self, thread_id: str, limit: int | None = None) -> list[TaskRecord]: ...

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> tuple[list[TaskRecord], int]: ...


class TaskService:
    """任务生命周期服务（8.1/8.2/8.3、13.3）。线程安全：锁内部互斥，
    仓储与状态更新在同一调用内完成。"""

    def __init__(self, repository: TaskRepository, lock: WriteTaskLock | None = None) -> None:
        self._repo = repository
        self._lock = lock or WriteTaskLock()

    # ---- 创建 ----

    def create_task(self, spec: NewTaskSpec) -> TaskRecord:
        """创建任务记录并初始化状态。

        写任务（spec.is_write）立即尝试获取串行锁：
        - 获得执行权 → 状态 running（创建即执行，调度口径见 RV-09 §8）；
        - 串行域被占 → 状态 pending 并进入 FIFO 队列（8.3 第 3 条）。
        只读任务不持锁、不排队，可直接执行。
        """
        record = TaskRecord(
            task_id=_new_task_id(),
            thread_id=spec.thread_id,
            operation_id=new_operation_id(),
            actor_account=spec.actor_account,
            actor_role=spec.actor_role,
            duty_code=spec.duty_code,
            original_user_message=spec.original_user_message,
            structured_intent=dict(spec.structured_intent),
            current_step="created",
            status=TaskStatus.PENDING,
        )
        record = self._repo.create(record)
        if spec.is_write:
            result = self._lock.acquire(record.thread_id, record.task_id)
            if result.acquired:
                try:
                    return self.transition(record.task_id, TaskStatus.RUNNING)
                except BaseException:
                    # 状态迁移失败（如 DB 不可用，13.4 禁止写操作）：释放锁，
                    # 防止执行权泄漏使串行域永久阻塞。
                    self._lock.release(record.thread_id, record.task_id)
                    raise
            logger.info(
                "task_write_queued",
                task_id=record.task_id,
                thread_id=record.thread_id,
                reason=result.reason,
                holder_task_id=result.holder_task_id,
            )
        return record

    # ---- 查询 ----

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._repo.get(task_id)

    def list_tasks(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        """任务分页查询（2.7.3 GET /api/v1/tasks；created_at 倒序）。
        status 过滤：允许 status 值与英文别名（_coerce_status，8.1）。"""
        coerce = _coerce_status(status) if status else None
        return self._repo.list(
            limit=limit, offset=offset,
            status=coerce.value if coerce is not None else None,
        )

    def answer_questions(
        self,
        task_id: str,
        answers: list[str],
        *,
        thread_id: str | None = None,
    ) -> TaskRecord:
        """回答 pending_questions（8.3 第 2 条：显式绑定 task_id）。

        - 只接受 waiting_user_input 任务（其余状态 TaskNotWaitingInputError）；
        - answers 数量必须与 pending_questions 一致（TaskAnswersMismatchError）；
        - 回答后清空 pending_questions 并恢复 running（waiting_user_input →
          running；编排链 resume 后继续）。
        """
        record = self._require(task_id)
        self._check_thread(record, thread_id)
        if record.status != TaskStatus.WAITING_USER_INPUT:
            raise TaskNotWaitingInputError(task_id, record.status)
        questions = record.pending_questions or []
        if len(answers) != len(questions):
            raise TaskAnswersMismatchError(task_id, len(questions), len(answers))
        return self.transition(
            task_id, TaskStatus.RUNNING, pending_questions=None
        )

    def confirm_with_hash(
        self,
        task_id: str,
        *,
        approved: bool,
        payload_hash: str | None = None,
        thread_id: str | None = None,
    ) -> TaskRecord:
        """轻确认决策（2.7.3 POST /tasks/:taskId/confirm，对接 7.3）。

        payload_hash 提供时校验与任务 confirmed_payload_hash 一致（7.3
        L886：内容变化 → 原确认失效必须重新确认）；approved=True →
        waiting_confirmation → running；False → cancelled（8.1）。
        """
        record = self._require(task_id)
        self._check_thread(record, thread_id)
        if payload_hash is not None and payload_hash != record.confirmed_payload_hash:
            raise TaskConfirmHashMismatchError(
                task_id, payload_hash, record.confirmed_payload_hash or ""
            )
        return self.confirm(task_id, approved=approved, thread_id=thread_id)

    def list_waiting(self, thread_id: str) -> list[TaskRecord]:
        """展示用：线程内等待中的任务（8.3 第 2 条：恢复必须显式 task_id，
        本方法不提供"最近挂起任务"猜测，仅用于页面/日志展示）。"""
        return [
            t for t in self._repo.list_by_thread(thread_id)
            if t.status in WAITING_STATES
        ]

    # ---- 状态迁移（通用底层） ----

    def transition(self, task_id: str, to_status: TaskStatus | str, **fields) -> TaskRecord:
        """通用迁移：确定性校验（machine.assert_transition）+ 持久化。

        终态（succeeded/failed/cancelled）额外：设置 completed_at、释放写锁、
        晋升排队任务。其余字段（pending_questions/execution_plan/hash 等）
        经 ``**fields`` 透传。
        """
        record = self._require(task_id)
        target = _coerce_status(to_status)
        assert_transition(record.status, target)
        if target in TERMINAL_STATES:
            fields["completed_at"] = _now()
        updated = self._repo.update(task_id, status=target, **fields)
        if target in TERMINAL_STATES:
            self._after_terminal(updated)
        return updated

    # ---- 语义方法（每条对应 8.1 的一个迁移条件，调用方为确定性代码） ----

    def need_input(self, task_id: str, questions: list[str]) -> TaskRecord:
        """running → waiting_user_input：缺少字段、候选歧义或需要用户回答。
        pending_questions 为等待用户的问题（8.2）。"""
        return self.transition(task_id, TaskStatus.WAITING_USER_INPUT, pending_questions=list(questions))

    def request_confirmation(self, task_id: str, payload_hash: str) -> TaskRecord:
        """running → waiting_confirmation：等待轻确认；记录确认目标 payload hash。"""
        return self.transition(
            task_id, TaskStatus.WAITING_CONFIRMATION, confirmed_payload_hash=payload_hash
        )

    def request_review(self, task_id: str, payload_hash: str) -> TaskRecord:
        """running → waiting_review：等待 R6 manager 复核；记录待复核 payload hash。"""
        return self.transition(
            task_id, TaskStatus.WAITING_REVIEW, reviewed_payload_hash=payload_hash
        )

    def resume(self, task_id: str, *, thread_id: str | None = None) -> TaskRecord:
        """waiting_user_input → running：后续消息显式绑定 task_id（8.3 第 2 条）。

        不提供"最近挂起任务"猜测；thread_id 与任务不符（跨会话/线程操作）拒绝。
        用户回答内容的结构化处理属 D9+（意图解析），本方法只做状态迁移与绑定校验。
        """
        record = self._require(task_id)
        self._check_thread(record, thread_id)
        return self.transition(task_id, TaskStatus.RUNNING)

    def rebind(self, task_id: str, *, thread_id: str | None = None) -> TaskRecord:
        """显式把后续消息绑定到指定等待任务并恢复执行（resume 的别名语义，
        名称与 D6 任务书交付物清单一致；同样强制显式 task_id）。"""
        return self.resume(task_id, thread_id=thread_id)

    def confirm(self, task_id: str, *, approved: bool, thread_id: str | None = None) -> TaskRecord:
        """waiting_confirmation 决策：approved=True → running；False → cancelled
        （用户取消确认后终止，8.1）。"""
        record = self._require(task_id)
        self._check_thread(record, thread_id)
        if approved:
            return self.transition(task_id, TaskStatus.RUNNING)
        return self.transition(task_id, TaskStatus.CANCELLED)

    def review(self, task_id: str, *, approved: bool, thread_id: str | None = None) -> TaskRecord:
        """waiting_review 决策：approved=True → running（复核通过后继续执行）；
        False → cancelled（复核拒绝后终止，8.1）。"""
        record = self._require(task_id)
        self._check_thread(record, thread_id)
        if approved:
            return self.transition(task_id, TaskStatus.RUNNING)
        return self.transition(task_id, TaskStatus.CANCELLED)

    def mark_succeeded(self, task_id: str, *, verification_passed: bool) -> TaskRecord:
        """running → succeeded：唯一成功路径。

        8.1"模型不能直接把任务标记为 succeeded"——本方法要求调用方显式声明
        最终验证已通过（verification_passed=True）；False 时拒绝，防止把
        未验证的任务标记为成功。
        """
        if not verification_passed:
            raise VerificationRequiredError(task_id)
        return self.transition(task_id, TaskStatus.SUCCEEDED)

    def update_progress(self, task_id: str, *, current_step: str, execution_plan: dict | None = None) -> TaskRecord:
        """running 任务进度更新（4.3 顺序 6：写入当前步骤和完整执行计划）。

        不改变状态（状态迁移仍走语义方法）；只允许 running 状态更新进度——
        waiting/终态任务不会被执行器写入（8.3：等待中的任务等待用户，终态
        任务不可再推进）。execution_plan 为 8.2 的执行计划字段（4.3 顺序 6
        "写入 task 当前步骤和完整执行计划"）。
        """
        record = self._require(task_id)
        if record.status != TaskStatus.RUNNING:
            raise TaskNotRunningError(task_id, record.status)
        fields: dict = {"current_step": current_step}
        if execution_plan is not None:
            fields["execution_plan"] = execution_plan
        return self._repo.update(task_id, **fields)

    def mark_failed(
        self,
        task_id: str,
        error_code: str,
        error_summary: str,
    ) -> TaskRecord:
        """running/waiting_* → failed：失败需人工处理或重新发起（13.1 错误码摘要）。"""
        return self.transition(
            task_id,
            TaskStatus.FAILED,
            final_error_code=error_code,
            final_error_summary=error_summary,
        )

    def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> TaskRecord:
        """pending/running/waiting_* → cancelled：用户取消或复核拒绝后终止（8.1）。

        reason 仅记录日志（规格 8.2 无取消原因字段，避免污染业务字段）。
        """
        record = self._require(task_id)
        updated = self.transition(record.task_id, TaskStatus.CANCELLED)
        logger.info("task_cancelled", task_id=record.task_id, reason=reason)
        return updated

    # ---- 内部 ----

    def _require(self, task_id: str) -> TaskRecord:
        record = self._repo.get(task_id)
        if record is None:
            raise KeyError(f"task not found: {task_id}")
        return record

    @staticmethod
    def _check_thread(record: TaskRecord, thread_id: str | None) -> None:
        if thread_id is not None and thread_id != record.thread_id:
            raise TaskThreadMismatchError(record.task_id, record.thread_id, thread_id)

    def _after_terminal(self, record: TaskRecord) -> None:
        """终态收尾：释放写锁并晋升队列队头（8.3 第 3 条排队语义）。

        只读任务不持锁（release 无副作用）；终态后队头任务先重新取得执行权
        （acquire）再迁移 pending→running。防御分支：acquire 失败或队头已被
        外部迁移时保持现状并告警，等待下一次 release 再调度。
        """
        next_task_id = self._lock.release(record.thread_id, record.task_id)
        if next_task_id is None:
            return
        queued = self._repo.get(next_task_id)
        if queued is None:
            logger.error("task_queue_promote_missing", task_id=next_task_id)
            return
        result = self._lock.acquire(queued.thread_id, queued.task_id)
        if not result.acquired:
            logger.error(
                "task_queue_promote_lock_lost",
                task_id=queued.task_id,
                reason=result.reason,
            )
            return
        try:
            self.transition(queued.task_id, TaskStatus.RUNNING)
        except TaskTransitionError:
            # 队头任务已被外部迁移（如用户取消）——归还执行权，等待下一次 release
            self._lock.release(queued.thread_id, queued.task_id)
            logger.info("task_queue_promote_skip", task_id=queued.task_id)


def _new_task_id() -> str:
    return f"{_TASK_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
