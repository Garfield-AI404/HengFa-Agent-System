"""tasks 端点（规格 2.7.3 L507-511；D22 前端整合）。

- GET   /api/v1/tasks                    分页查询任务（8.2 字段）
- GET   /api/v1/tasks/{task_id}          任务详情：步骤/attempt/result
- POST  /api/v1/tasks/{task_id}/replies  回答 pending_questions（8.3
  第 2 条：显式绑定 task_id，不猜测"最近挂起任务"）
- POST  /api/v1/tasks/{task_id}/confirm  提交轻确认（7.3：confirmed_
  payload_hash 机制——执行内容变化后原确认失效必须重新确认；
  任务线程存在挂起确认（checkpoint
  pending_confirmation）时委托 /chat/confirm 同款图恢复路径，见
  confirm_task docstring）
- POST  /api/v1/tasks/{task_id}/cancel   取消尚未执行的任务（8.1 状态机）

角色与安全：全部端点 require_manager（2.7.3 表 manager 行）；写 API
（replies/confirm/cancel）校验 CSRF（D4 check_csrf，fail closed）；
统一错误响应 error_code/message/details/trace_id（2.7.3）。

任务详情"步骤"来自审计事件（audit_events，11.1 只追加）、attempt/result
来自 task_attempts（11.2）——本模块无写审计路径，只读组装。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from administrative_agent.audit.service import AuditService
from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyTaskRepository,
)
from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.tasks.machine import TaskStatus, TaskTransitionError
from administrative_agent.tasks.service import (
    TaskAnswersMismatchError,
    TaskConfirmHashMismatchError,
    TaskNotWaitingInputError,
    TaskService,
    TaskThreadMismatchError,
)
from administrative_agent.web.routes_auth import check_csrf
from administrative_agent.web.routes_chat import (
    _enter_thread,
    _leave_thread,
    resume_thread,
)
from administrative_agent.web.schemas import (
    ConfirmRequest,
    ErrorCode,
    RepliesRequest,
    TaskAttemptOut,
    TaskDetailOut,
    TaskEventOut,
    TaskListResponse,
    TaskOut,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

#: 列表分页上限（与 D17/D18 分页语义一致：最大 200）
MAX_LIST_LIMIT = 200


_DEFAULT_WRITE_LOCK = WriteTaskLock()


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），默认 SessionLocal。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _write_lock(request: Request) -> WriteTaskLock:
    """串行写锁依赖（跨请求共享：cancel/confirm 终态释放锁并晋升队头，
    8.3 第 3 条——锁实例必须与 chat 提交侧一致，测试经 app.state 注入）。"""
    lock = getattr(request.app.state, "write_lock", None)
    if lock is None:
        lock = _DEFAULT_WRITE_LOCK
    return lock


def _service(request: Request) -> tuple[TaskService, AuditService]:
    factory = _session_factory(request)
    return (
        TaskService(SqlAlchemyTaskRepository(factory), _write_lock(request)),
        AuditService(
            SqlAlchemyAuditRepository(factory),
            SqlAlchemyAttemptRepository(factory),
        ),
    )


def _task_out(record) -> TaskOut:
    return TaskOut(
        task_id=record.task_id,
        thread_id=record.thread_id,
        operation_id=record.operation_id,
        actor_account=record.actor_account,
        actor_role=record.actor_role,
        duty_code=record.duty_code,
        original_user_message=record.original_user_message,
        structured_intent=dict(record.structured_intent or {}),
        current_step=record.current_step,
        status=record.status.value,
        pending_questions=list(record.pending_questions)
        if record.pending_questions
        else None,
        execution_plan=dict(record.execution_plan) if record.execution_plan else None,
        confirmed_payload_hash=record.confirmed_payload_hash,
        reviewed_payload_hash=record.reviewed_payload_hash,
        checkpoint_id=record.checkpoint_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
        final_error_code=record.final_error_code,
        final_error_summary=record.final_error_summary,
    )


def _task_event_out(event) -> TaskEventOut:
    return TaskEventOut(
        audit_id=event.audit_id,
        event_type=event.event_type,
        task_id=event.task_id,
        operation_id=event.operation_id,
        duty_code=event.duty_code,
        policy_key=event.policy_key,
        target_type=event.target_type,
        target_id=event.target_id,
        request_summary=event.request_summary,
        structured_payload=dict(event.structured_payload)
        if event.structured_payload
        else None,
        before_snapshot=dict(event.before_snapshot) if event.before_snapshot else None,
        after_snapshot=dict(event.after_snapshot) if event.after_snapshot else None,
        source=event.source,
        result_code=event.result_code,
        created_at=event.created_at,
    )


def _task_attempt_out(attempt) -> TaskAttemptOut:
    return TaskAttemptOut(
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


def _task_error_to_http(exc: Exception) -> HTTPException:
    """领域/服务异常 → 稳定错误码（2.7.3：前端分支只读 error_code）。"""
    if isinstance(exc, TaskAnswersMismatchError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.TASK_ANSWERS_MISMATCH.value,
                "message": str(exc),
                "details": None,
            },
        )
    if isinstance(exc, TaskConfirmHashMismatchError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.CONFIRM_HASH_MISMATCH.value,
                "message": str(exc),
                "details": None,
            },
        )
    if isinstance(exc, TaskThreadMismatchError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.TASK_INVALID_STATE.value,
                "message": str(exc),
                "details": None,
            },
        )
    if isinstance(exc, (TaskNotWaitingInputError, TaskTransitionError)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": ErrorCode.TASK_INVALID_STATE.value,
                "message": str(exc),
                "details": None,
            },
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": ErrorCode.TASK_INVALID_STATE.value,
            "message": "任务状态不允许该操作。",
            "details": None,
        },
    )


def _require_task(tasks: TaskService, task_id: str):
    try:
        record = tasks.get_task(task_id)
    except KeyError:
        record = None
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.TASK_NOT_FOUND.value,
                "message": "任务不存在。",
                "details": None,
            },
        )
    return record


# ---------------------------------------------------------------------------
# 查询（GET；manager）
# ---------------------------------------------------------------------------


@router.get("", response_model=TaskListResponse)
def list_tasks(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Any = Depends(require_manager),
) -> TaskListResponse:
    """任务分页（2.7.3 L507；8.2 字段；created_at 倒序）。

    status 过滤可选（8.1 状态值，如 ?status=pending）；非法状态值 →
    422 VALIDATION_ERROR（_coerce_status 拒绝）。
    """
    try:
        items, total = _service(request)[0].list_tasks(
            limit=limit, offset=offset, status=status_filter
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": ErrorCode.VALIDATION_ERROR.value,
                "message": f"非法状态过滤值：{status_filter!r}。",
                "details": None,
            },
        ) from None
    return TaskListResponse(
        items=[_task_out(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(
    request: Request, task_id: str, _: Any = Depends(require_manager)
) -> TaskDetailOut:
    """任务详情（2.7.3 L508）：8.2 字段 + 步骤（审计事件）+ attempt/result。"""
    tasks, audit = _service(request)
    record = _require_task(tasks, task_id)
    events = audit.events(task_id=task_id, limit=200)
    attempts = audit.attempts(record.operation_id)
    return TaskDetailOut(
        task=_task_out(record),
        events=[_task_event_out(e) for e in events],
        attempts=[_task_attempt_out(a) for a in attempts],
    )


# ---------------------------------------------------------------------------
# 写（manager + CSRF；2.7.3 稳定错误契约）
# ---------------------------------------------------------------------------


@router.post("/{task_id}/replies", response_model=TaskOut)
def reply_questions(
    request: Request,
    task_id: str,
    body: RepliesRequest,
    account: Account = Depends(require_manager),
) -> TaskOut:
    """回答 pending_questions（2.7.3 L509）。

    显式绑定 task_id（8.3 第 2 条，不猜测"最近挂起任务"）：只接受
    waiting_user_input 任务；answers 数量与 pending_questions 一致；
    回答后恢复 running（waiting_user_input → running）。
    thread_id 以任务记录为准（服务端权威，不信任请求体）。
    """
    check_csrf(request)
    tasks = _service(request)[0]
    _require_task(tasks, task_id)
    try:
        record = tasks.answer_questions(
            task_id, body.answers, thread_id=None
        )
    except (
        TaskNotWaitingInputError,
        TaskAnswersMismatchError,
        TaskTransitionError,
    ) as exc:
        raise _task_error_to_http(exc) from exc
    return _task_out(record)


@router.post("/{task_id}/confirm", response_model=TaskOut)
def confirm_task(
    request: Request,
    task_id: str,
    body: ConfirmRequest,
    account: Account = Depends(require_manager),
) -> TaskOut:
    """提交轻确认（2.7.3 L510；对接 7.3；任务详情页确认按钮兜底入口）。

    [2026-08-24 harness 改造] F-1：任务线程存在挂起确认（checkpoint
    pending_confirmation 与任务一致）时，**委托 ``/chat/confirm`` 同款
    ``resume_thread`` 图恢复路径**（规格 2.7.3/8.8 措辞）——真实 Executor
    执行（载荷 = 已批准卡片）+ pending_confirmation 清除，杜绝"转 running
    但无执行"的悬挂任务；事件经 sink 进线程最新流（无则新建）。
    委托失败契约与 /chat/confirm 同口径：任务状态非 waiting_confirmation
    → 409 TASK_INVALID_STATE；图执行异常 → 500 CONFIRM_RESUME_FAILED。

    **无挂起确认**（非 harness 任务/纯状态机任务/checkpoint 未接线）→
    维持 ``confirm_with_hash`` 纯状态机迁移（历史兼容）：body.payload_hash
    可选，提供时校验与任务 confirmed_payload_hash 一致（7.3 L886：
    实际执行内容/目标记录/关键字段变化 → 原确认失效，必须重新确认——
    CONFIRM_HASH_MISMATCH 409）；approved=True → waiting_confirmation →
    running；False → cancelled（8.1）。

    body.payload_hash 在委托路径下保持历史语义：缺省时以挂起卡片的
    payload_hash 恢复（等价"不校验直接批准当前卡片"）；提供且与挂起卡片
    不一致 → 图重挂（200 + 流内 re_requested，客户端重新确认——同
    /chat/confirm 语义）。
    """
    check_csrf(request)
    tasks = _service(request)[0]
    record = _require_task(tasks, task_id)
    # [thread 归属校验] 任务归属他人 → 404（复用 _require_task 错误体，不
    # 泄露任务存在性）。actor_account 已在 TaskRecord 上，无需新查询。
    # 注：`/tasks` 其余端点（列表/详情/取消/复核）的归属是**独立缺口**，
    # 本轮不动——归属校验只在 confirm 与 /chat/* 落点。
    if record.actor_account != account.account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.TASK_NOT_FOUND.value,
                "message": "任务不存在。",
                "details": None,
            },
        )

    # 委托路径：checkpoint 已接线且线程挂起确认即本任务 → 同 /chat/confirm
    # resume 路径（同步 drain；事件进线程最新流或新建）。thread_id 以任务
    # 记录为准（服务端权威，不信任请求体）。
    service = getattr(request.app.state, "checkpoint_service", None)
    if service is not None:
        restored = service.load_thread(record.thread_id)
        pc = (restored or {}).get("pending_confirmation")
        if pc is not None and pc.get("task_id") == task_id:
            if record.status != TaskStatus.WAITING_CONFIRMATION:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error_code": ErrorCode.TASK_INVALID_STATE.value,
                        "message": f"任务状态为 {record.status.value}，不在等待确认中"
                        "（已确认/已取消/已失败均不可重复确认）。",
                        "details": None,
                    },
                )
            _enter_thread(record.thread_id)
            try:
                resume_thread(
                    request,
                    account,
                    record.thread_id,
                    approved=body.approved,
                    payload_hash=body.payload_hash or pc["payload_hash"],
                )
            finally:
                _leave_thread(record.thread_id)
            return _task_out(_require_task(tasks, task_id))

    try:
        record = tasks.confirm_with_hash(
            task_id,
            approved=body.approved,
            payload_hash=body.payload_hash,
            thread_id=None,
        )
    except (
        TaskConfirmHashMismatchError,
        TaskTransitionError,
    ) as exc:
        raise _task_error_to_http(exc) from exc
    return _task_out(record)


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(
    request: Request,
    task_id: str,
    account: Account = Depends(require_manager),
) -> TaskOut:
    """取消尚未执行的任务（2.7.3 L511）。

    8.1：pending/running/waiting_* → cancelled（终态不可再迁移 →
    TASK_INVALID_STATE 409；"尚未执行"由状态机保证——终态任务不可取消）。
    """
    check_csrf(request)
    tasks = _service(request)[0]
    _require_task(tasks, task_id)
    try:
        record = tasks.cancel(task_id, reason="user_cancelled")
    except TaskTransitionError as exc:
        raise _task_error_to_http(exc) from exc
    return _task_out(record)
