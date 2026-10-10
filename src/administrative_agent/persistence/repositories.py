"""task/audit/review/mapping 持久化仓储（规格 2.6 persistence/repositories.py）。

D6 实现 TaskRepository（8.2 task 记录）；D8 实现 AuditRepository 与
AttemptRepository（11.1 audit_events 只追加、11.2 task_attempts
attempt/result）；D20 实现 ReviewRepository（7.4 review_records 十二字段，
repositories.py 注释预留补建）；mapping 仓储由 D18 在
files/mapping_service.py（SqlAlchemyMappingStore）。
SQLAlchemy 实现在 PostgreSQL（12.1 六张表由 D3 Alembic 管理）；服务层经
协议（tasks.service.TaskRepository / audit.service 协议 / domains.reviews
ReviewRepository）依赖本实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeAlias

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from administrative_agent.audit.service import AuditEventRecord, TaskAttemptRecord
from administrative_agent.domains.reviews import ReviewRecord
from administrative_agent.persistence.models import (
    AuditEvent,
    ChatThread,
    ReviewRecord as ReviewRow,
    Task,
    TaskAttempt,
)
from administrative_agent.tasks.records import TaskRecord
from administrative_agent.tasks.machine import TaskStatus, _coerce_status

#: 会话工厂（sessionmaker 或函数）；业务写路径统一经 session_scope 边界（D6 起）
SessionFactory: TypeAlias = Callable[[], Session]


def _to_record(task: Task) -> TaskRecord:
    return TaskRecord(
        task_id=task.task_id,
        thread_id=task.thread_id,
        operation_id=task.operation_id,
        actor_account=task.actor_account,
        actor_role=task.actor_role,
        duty_code=task.duty_code,
        original_user_message=task.original_user_message,
        structured_intent=dict(task.structured_intent or {}),
        current_step=task.current_step,
        status=TaskStatus(task.status),
        pending_questions=list(task.pending_questions) if task.pending_questions else None,
        execution_plan=dict(task.execution_plan) if task.execution_plan else None,
        confirmed_payload_hash=task.confirmed_payload_hash,
        reviewed_payload_hash=task.reviewed_payload_hash,
        checkpoint_id=task.checkpoint_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
        final_error_code=task.final_error_code,
        final_error_summary=task.final_error_summary,
    )


class SqlAlchemyTaskRepository:
    """TaskRepository 的 SQLAlchemy/PostgreSQL 实现（12.1 tasks 表）。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def create(self, record: TaskRecord) -> TaskRecord:
        task = Task(
            task_id=record.task_id,
            thread_id=record.thread_id,
            operation_id=record.operation_id,
            actor_account=record.actor_account,
            actor_role=record.actor_role,
            duty_code=record.duty_code,
            original_user_message=record.original_user_message,
            structured_intent=record.structured_intent,
            current_step=record.current_step,
            status=record.status.value,
            pending_questions=record.pending_questions,
            execution_plan=record.execution_plan,
            confirmed_payload_hash=record.confirmed_payload_hash,
            reviewed_payload_hash=record.reviewed_payload_hash,
            checkpoint_id=record.checkpoint_id,
            completed_at=record.completed_at,
            final_error_code=record.final_error_code,
            final_error_summary=record.final_error_summary,
        )
        with self._factory() as session:
            session.add(task)
            session.commit()
            session.refresh(task)
        return _to_record(task)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._factory() as session:
            task = session.get(Task, task_id)
            return _to_record(task) if task is not None else None

    def update(self, task_id: str, **fields) -> TaskRecord:
        """按字段名映射到 ORM 列并更新（状态机已校验，此处只持久化）。"""
        column_map = {
            "status": "status",
            "current_step": "current_step",
            "pending_questions": "pending_questions",
            "execution_plan": "execution_plan",
            "confirmed_payload_hash": "confirmed_payload_hash",
            "reviewed_payload_hash": "reviewed_payload_hash",
            "checkpoint_id": "checkpoint_id",
            "completed_at": "completed_at",
            "final_error_code": "final_error_code",
            "final_error_summary": "final_error_summary",
        }
        values: dict[str, object] = {}
        for key, value in fields.items():
            if key not in column_map:
                raise ValueError(f"unknown task field: {key}")
            if key == "status":
                values[key] = _coerce_status(value).value
            elif key == "pending_questions" and value is not None:
                values[key] = list(value)
            else:
                values[key] = value
        with self._factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise KeyError(f"task not found: {task_id}")
            for col, value in values.items():
                setattr(task, col, value)
            task.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(task)
        return _to_record(task)

    def list_by_thread(self, thread_id: str, limit: int | None = None) -> list[TaskRecord]:
        stmt = (
            select(Task)
            .where(Task.thread_id == thread_id)
            .order_by(Task.created_at.desc())
            .limit(limit)
        )
        with self._factory() as session:
            rows = session.scalars(stmt).all()
        return [_to_record(t) for t in rows]

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        """任务分页查询（2.7.3 GET /api/v1/tasks；created_at 倒序）。"""
        filters = [Task.status == status] if status is not None else []
        count_stmt = select(func.count()).select_from(Task).where(*filters)
        stmt = (
            select(Task)
            .where(*filters)
            .order_by(Task.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        with self._factory() as session:
            total = session.scalar(count_stmt) or 0
            rows = session.scalars(stmt).all()
        return [_to_record(t) for t in rows], int(total)


# ---------------------------------------------------------------------------
# chat_threads（2026-08-24：对话会话索引）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadRecord:
    """会话索引行（chat_threads；API 层直接映射为列表/删除操作的账目）。"""

    thread_id: str
    account: str
    title: str
    created_at: datetime
    updated_at: datetime


def _to_thread(row: ChatThread) -> ThreadRecord:
    return ThreadRecord(
        thread_id=row.thread_id,
        account=row.account,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyThreadRepository:
    """对话会话索引仓储（12.1 chat_threads 表；会话列表/恢复/删除的账目层）。

    upsert 语义：不存在 → insert（title = 首条用户消息截断 40 字符）；
    已存在 → **只更新 updated_at**（title 不覆盖——首条消息才是标题，
    后续消息不改变会话名）。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def upsert(self, thread_id: str, account: str, title: str) -> None:
        """登记/刷新会话索引（存在 → 只更新 updated_at；title 首插后不覆盖）。"""
        with self._factory() as session:
            row = session.get(ChatThread, thread_id)
            if row is None:
                session.add(
                    ChatThread(thread_id=thread_id, account=account, title=title)
                )
            else:
                row.updated_at = datetime.now(timezone.utc)
            session.commit()

    def get(self, thread_id: str) -> ThreadRecord | None:
        with self._factory() as session:
            row = session.get(ChatThread, thread_id)
            return _to_thread(row) if row is not None else None

    def list_by_account(self, account: str, limit: int = 100) -> list[ThreadRecord]:
        """账号的会话列表（updated_at DESC；最多 limit 条）。"""
        stmt = (
            select(ChatThread)
            .where(ChatThread.account == account)
            .order_by(ChatThread.updated_at.desc())
            .limit(limit)
        )
        with self._factory() as session:
            rows = session.scalars(stmt).all()
        return [_to_thread(r) for r in rows]

    def delete(self, thread_id: str) -> bool:
        """删除索引行（影响行数 > 0 → True；不存在 → False）。"""
        with self._factory() as session:
            row = session.get(ChatThread, thread_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


# ---------------------------------------------------------------------------
# review_records（7.4；D20 补建）
# ---------------------------------------------------------------------------


def _to_review(record: ReviewRow) -> ReviewRecord:
    return ReviewRecord(
        review_id=record.review_id,
        task_id=record.task_id,
        requested_by=record.requested_by,
        requested_at=record.requested_at,
        reviewed_by=record.reviewed_by,
        reviewed_at=record.reviewed_at,
        decision=record.decision,
        reviewed_payload=dict(record.reviewed_payload)
        if record.reviewed_payload is not None
        else None,
        reviewed_payload_hash=record.reviewed_payload_hash,
        changed_after_review=record.changed_after_review,
        invalidated_at=record.invalidated_at,
        rejection_reason=record.rejection_reason,
    )


class SqlAlchemyReviewRepository:
    """ReviewRepository 的 SQLAlchemy 实现（12.1 review_records 表，7.4）。

    复核记录可更新（批准/拒绝/失效回填 decision/invalidated_at 等）——
    只有 audit_events 是 append-only（11.1）。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def create(self, record: ReviewRecord) -> ReviewRecord:
        row = ReviewRow(
            review_id=record.review_id,
            task_id=record.task_id,
            requested_by=record.requested_by,
            reviewed_by=record.reviewed_by,
            reviewed_at=record.reviewed_at,
            decision=record.decision,
            reviewed_payload=record.reviewed_payload,
            reviewed_payload_hash=record.reviewed_payload_hash,
            changed_after_review=record.changed_after_review,
            invalidated_at=record.invalidated_at,
            rejection_reason=record.rejection_reason,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return _to_review(row)

    def get(self, review_id: str) -> ReviewRecord | None:
        with self._factory() as session:
            row = session.get(ReviewRow, review_id)
            return _to_review(row) if row is not None else None

    def update(self, review_id: str, **fields) -> ReviewRecord:
        """按字段名更新复核记录（状态机/领域层已校验，此处只持久化）。"""
        column_map = {
            "reviewed_by": "reviewed_by",
            "reviewed_at": "reviewed_at",
            "decision": "decision",
            "reviewed_payload": "reviewed_payload",
            "reviewed_payload_hash": "reviewed_payload_hash",
            "changed_after_review": "changed_after_review",
            "invalidated_at": "invalidated_at",
            "rejection_reason": "rejection_reason",
        }
        values: dict[str, object] = {}
        for key, value in fields.items():
            if key not in column_map:
                raise ValueError(f"unknown review field: {key}")
            values[key] = value
        with self._factory() as session:
            row = session.get(ReviewRow, review_id)
            if row is None:
                raise KeyError(f"review not found: {review_id}")
            for col, value in values.items():
                setattr(row, col, value)
            session.commit()
            session.refresh(row)
        return _to_review(row)

    def list_by(
        self,
        *,
        task_id: str | None = None,
        pending_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[ReviewRecord], int]:
        """复核记录列表（复核队列 = pending_only：decision 未决定且未失效）；
        返回 (items, total)。"""
        stmt = select(ReviewRow)
        if task_id is not None:
            stmt = stmt.where(ReviewRow.task_id == task_id)
        if pending_only:
            stmt = stmt.where(
                ReviewRow.decision.is_(None),
                ReviewRow.invalidated_at.is_(None),
            )
        with self._factory() as session:
            total = len(
                session.scalars(
                    stmt.with_only_columns(ReviewRow.review_id)
                ).all()
            )
            rows = session.scalars(
                stmt.order_by(ReviewRow.requested_at.desc())
                .offset(offset or 0)
                .limit(limit)
            ).all()
        return [_to_review(r) for r in rows], total


# ---------------------------------------------------------------------------
# audit_events（11.1，只追加）与 task_attempts（11.2，attempt/result）
# ---------------------------------------------------------------------------


def _to_audit_event(event: AuditEvent) -> AuditEventRecord:
    return AuditEventRecord(
        audit_id=event.audit_id,
        task_id=event.task_id,
        operation_id=event.operation_id,
        trace_id=event.trace_id,
        actor_account=event.actor_account,
        actor_role=event.actor_role,
        event_type=event.event_type,
        duty_code=event.duty_code,
        policy_key=event.policy_key,
        policy_version=event.policy_version,
        policy_hash=event.policy_hash,
        target_type=event.target_type,
        target_id=event.target_id,
        request_summary=event.request_summary,
        structured_payload=dict(event.structured_payload) if event.structured_payload else None,
        before_snapshot=dict(event.before_snapshot) if event.before_snapshot else None,
        after_snapshot=dict(event.after_snapshot) if event.after_snapshot else None,
        source=event.source,
        result_code=event.result_code,
        created_at=event.created_at,
    )


class SqlAlchemyAuditRepository:
    """AuditRepository 的 SQLAlchemy 实现（12.1 audit_events，只追加）。

    只提供 append 与只读查询——不定义任何 update/delete 方法（11.1：
    普通应用账号不能 update/delete）；DB 层另有触发器强制
    （迁移 92d64bb42eca），直连 SQL 亦不可覆盖。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def append(self, event: AuditEventRecord) -> AuditEventRecord:
        row = AuditEvent(
            audit_id=event.audit_id,
            task_id=event.task_id,
            operation_id=event.operation_id,
            trace_id=event.trace_id,
            actor_account=event.actor_account,
            actor_role=event.actor_role,
            event_type=event.event_type,
            duty_code=event.duty_code,
            policy_key=event.policy_key,
            policy_version=event.policy_version,
            policy_hash=event.policy_hash,
            target_type=event.target_type,
            target_id=event.target_id,
            request_summary=event.request_summary,
            structured_payload=event.structured_payload,
            before_snapshot=event.before_snapshot,
            after_snapshot=event.after_snapshot,
            source=event.source,
            result_code=event.result_code,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return _to_audit_event(row)

    def list_by(
        self,
        *,
        task_id: str | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEventRecord]:
        stmt = select(AuditEvent)
        if task_id is not None:
            stmt = stmt.where(AuditEvent.task_id == task_id)
        if operation_id is not None:
            stmt = stmt.where(AuditEvent.operation_id == operation_id)
        if trace_id is not None:
            stmt = stmt.where(AuditEvent.trace_id == trace_id)
        if event_type is not None:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit)
        with self._factory() as session:
            rows = session.scalars(stmt).all()
        return [_to_audit_event(r) for r in rows]

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        task_id: str | None = None,
        operation_id: str | None = None,
        event_type: str | None = None,
    ) -> tuple[list[AuditEventRecord], int]:
        """业务审计分页查询（2.7.3 GET /api/v1/audit；created_at 倒序，
        11.1 append-only 只读）。"""
        filters = []
        if task_id is not None:
            filters.append(AuditEvent.task_id == task_id)
        if operation_id is not None:
            filters.append(AuditEvent.operation_id == operation_id)
        if event_type is not None:
            filters.append(AuditEvent.event_type == event_type)
        count_stmt = select(func.count()).select_from(AuditEvent).where(*filters)
        stmt = (
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        with self._factory() as session:
            total = session.scalar(count_stmt) or 0
            rows = session.scalars(stmt).all()
        return [_to_audit_event(r) for r in rows], int(total)


def _to_attempt(attempt: TaskAttempt) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        operation_id=attempt.operation_id,
        attempt_no=attempt.attempt_no,
        target_type=attempt.target_type,
        target_id=attempt.target_id,
        field_diffs=dict(attempt.field_diffs) if attempt.field_diffs else None,
        idempotency_key=attempt.idempotency_key,
        expected_updated_at=attempt.expected_updated_at,
        created_at=attempt.created_at,
        result_status=attempt.result_status,
        error_code=attempt.error_code,
        requires_operator_action=attempt.requires_operator_action,
        result_summary=attempt.result_summary,
        result_at=attempt.result_at,
    )


class SqlAlchemyAttemptRepository:
    """AttemptRepository 的 SQLAlchemy 实现（12.1 task_attempts）。

    task_attempts 不是 append-only：result 调用后回填同一条 attempt
    （11.2 规则 2）；attempt_no 唯一约束（operation_id, attempt_no）由
    DB 保证（uq_task_attempts_operation_no），result_status 枚举由
    CheckConstraint 保证。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def append_attempt(self, record: TaskAttemptRecord) -> TaskAttemptRecord:
        row = TaskAttempt(
            attempt_id=record.attempt_id,
            task_id=record.task_id,
            operation_id=record.operation_id,
            attempt_no=record.attempt_no,
            target_type=record.target_type,
            target_id=record.target_id,
            field_diffs=record.field_diffs,
            idempotency_key=record.idempotency_key,
            expected_updated_at=record.expected_updated_at,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return _to_attempt(row)

    def record_result(
        self,
        attempt_id: str,
        *,
        result_status: str,
        error_code: str | None = None,
        requires_operator_action: bool | None = None,
        result_summary: str | None = None,
    ) -> TaskAttemptRecord:
        with self._factory() as session:
            row = session.get(TaskAttempt, attempt_id)
            if row is None:
                raise KeyError(f"attempt not found: {attempt_id}")
            row.result_status = result_status
            row.error_code = error_code
            row.requires_operator_action = requires_operator_action
            row.result_summary = result_summary
            row.result_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
        return _to_attempt(row)

    def list_by_operation(self, operation_id: str) -> list[TaskAttemptRecord]:
        stmt = (
            select(TaskAttempt)
            .where(TaskAttempt.operation_id == operation_id)
            .order_by(TaskAttempt.attempt_no)
        )
        with self._factory() as session:
            rows = session.scalars(stmt).all()
        return [_to_attempt(r) for r in rows]

    def get(self, attempt_id: str) -> TaskAttemptRecord | None:
        with self._factory() as session:
            row = session.get(TaskAttempt, attempt_id)
            return _to_attempt(row) if row is not None else None
