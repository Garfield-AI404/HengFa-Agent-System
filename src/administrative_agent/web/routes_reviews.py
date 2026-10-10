"""R6 manager 复核 API（规格 2.7.3 API 契约 + 7.4/8.1；模块树 L214
routes_reviews.py）。

- GET   /api/v1/reviews                    分页查询复核队列（待复核）
- GET   /api/v1/reviews/{review_id}        复核详情和差异
- POST  /api/v1/reviews/{review_id}/approve  批准当前 payload_hash（L514）
- POST  /api/v1/reviews/{review_id}/reject   拒绝并提交原因

角色与安全：
- 全部端点 require_manager（2.7.2 /reviews 页面 manager 允许、developer
  拒绝；2.7.3 四个契约端点均 manager）；
- 写 API（approve/reject）校验 CSRF（Origin/Referer 白名单，fail closed，
  D4 check_csrf）；
- 统一错误响应 error_code/message/details/trace_id（2.7.3；领域异常映射
  为稳定错误码，前端分支只读 error_code）。

Agent 边界（RV-30 通过标准）：本模块是 manager 人工复核入口，不创建任何
Agent 工具/意图；复核批准只把任务恢复到 running（D21 才能执行写入），
本模块无 ERP 写路径。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from administrative_agent.auth.dependencies import require_manager
from administrative_agent.auth.models import Account
from administrative_agent.domains.reviews import (
    ReviewError,
    ReviewHashMismatchError,
    ReviewInvalidStateError,
    ReviewNotFoundError,
    ReviewRejectionReasonRequiredError,
    ReviewService,
)
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyTaskRepository,
)
from administrative_agent.tasks.service import TaskService
from administrative_agent.audit.service import AuditService
from administrative_agent.web.routes_auth import check_csrf
from administrative_agent.web.schemas import (
    ReviewApproveRequest,
    ReviewListResponse,
    ReviewOut,
    ReviewRejectRequest,
    new_trace_id,
)

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

#: 列表分页上限（与 D17/D18 分页语义一致：最大 200）
MAX_LIST_LIMIT = 200


def _session_factory(request: Request) -> Any:
    """会话工厂依赖：优先 app.state（测试注入 SQLite 工厂），
    生产默认 SessionLocal（persistence.database）。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from administrative_agent.persistence.database import SessionLocal

        factory = SessionLocal
    return factory


def _service(request: Request) -> ReviewService:
    factory = _session_factory(request)
    return ReviewService(
        review_repo=SqlAlchemyReviewRepository(factory),
        tasks=TaskService(SqlAlchemyTaskRepository(factory)),
        audit=AuditService(
            SqlAlchemyAuditRepository(factory),
            SqlAlchemyAttemptRepository(factory),
        ),
    )


def _review_out(record) -> ReviewOut:
    return ReviewOut(**record.to_dict())


def _review_error_to_http(exc: ReviewError) -> HTTPException:
    """领域异常 → 稳定错误码（2.7.3：前端分支只读 error_code）。"""
    if isinstance(exc, ReviewNotFoundError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ReviewHashMismatchError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, ReviewRejectionReasonRequiredError):
        http_status = status.HTTP_400_BAD_REQUEST
    else:  # ReviewInvalidStateError / ReviewAlreadyPendingError 等
        http_status = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=http_status,
        detail={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": None,
        },
    )


# ---------------------------------------------------------------------------
# 查询（GET）
# ---------------------------------------------------------------------------


@router.get("", response_model=ReviewListResponse)
def list_reviews(
    request: Request,
    pending_only: bool = Query(default=True, alias="pending"),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: Any = Depends(require_manager),
) -> ReviewListResponse:
    """复核队列（2.7.3：分页查询；默认只列待复核，可带 ?pending=false
    查全部记录）。"""
    items, total = _service(request).list_reviews(
        pending_only=pending_only, limit=limit, offset=offset
    )
    return ReviewListResponse(
        items=[_review_out(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{review_id}", response_model=ReviewOut)
def get_review(
    request: Request, review_id: str, _: Any = Depends(require_manager)
) -> ReviewOut:
    """复核详情（7.4 十二字段 + reviewed_payload 写入计划/差异）。"""
    try:
        record = _service(request).get_review(review_id)
    except ReviewError as exc:
        raise _review_error_to_http(exc) from exc
    if record is None:
        raise _review_error_to_http(ReviewNotFoundError(review_id))
    return _review_out(record)


# ---------------------------------------------------------------------------
# 写（manager + CSRF；2.7.3 稳定错误契约）
# ---------------------------------------------------------------------------


@router.post("/{review_id}/approve", response_model=ReviewOut)
def approve_review(
    request: Request,
    review_id: str,
    body: ReviewApproveRequest,
    account: Account = Depends(require_manager),
) -> ReviewOut:
    """批准复核（2.7.3 L514：批准当前 payload_hash）。

    body.payload_hash 可选：提供时服务端校验与复核记录一致，不一致 →
    REVIEW_HASH_MISMATCH（计划已变化，提示重新复核，绝不批准旧 hash）。
    批准后任务 waiting_review → running（D21 才能执行写入）。
    """
    check_csrf(request)
    try:
        record = _service(request).approve(
            review_id=review_id,
            actor_account=account.account,
            actor_role=account.role.value,
            trace_id=new_trace_id(),
            expected_payload_hash=body.payload_hash,
        )
    except ReviewError as exc:
        raise _review_error_to_http(exc) from exc
    return _review_out(record)


@router.post("/{review_id}/reject", response_model=ReviewOut)
def reject_review(
    request: Request,
    review_id: str,
    body: ReviewRejectRequest,
    account: Account = Depends(require_manager),
) -> ReviewOut:
    """拒绝复核并提交原因（2.7.3；7.4 rejection_reason 必填）。

    拒绝后任务 waiting_review → cancelled（8.1：复核拒绝后终止）。
    """
    check_csrf(request)
    try:
        record = _service(request).reject(
            review_id=review_id,
            rejection_reason=body.rejection_reason,
            actor_account=account.account,
            actor_role=account.role.value,
            trace_id=new_trace_id(),
        )
    except ReviewError as exc:
        raise _review_error_to_http(exc) from exc
    return _review_out(record)
