"""R6 manager 复核域（规格 2.6 domains/reviews.py；6.4 步骤 10 L837、
7.4 L888-913、7.5 L915-917、8.1 L931/L934、11.3 L1233、RV-30）。

流程（7.4 业务变化处理 5 步 L907-913）：
- 发起复核（步骤 10）：写入计划 → 创建 review 记录（12 字段）→ 任务
  running → waiting_review（复用 D6 request_review）；幂等：同任务
  已有未决定且 hash 相同的复核直接复用；hash 不同必须先失效旧复核；
- 批准（2.7.3 L514"批准当前 payload_hash"）：仅未决定且未失效的复核
  可批准；调用方提供当前 hash 时必须与 reviewed_payload_hash 一致；
  decision=approved → 任务 waiting_review → running（D21 续执行）；
- 拒绝（8.1：复核拒绝后终止）：rejection_reason 必填；
  decision=rejected → 任务 → cancelled；
- 失效（7.4 步骤 1/2）：计划业务变化 → changed_after_review=true、
  invalidated_at 置位；任务状态不变（未复核前不得写入，步骤 5）；
- 重新复核（步骤 3/4）：失效后生成新差异（D19 重新执行）→ 再次发起
  复核（新 payload hash）→ 未复核前不得写入。

审计（11.3 L1233）：发起/批准/拒绝/失效/重新复核全部追加审计事件，
至少记录 requested_by、reviewed_by、时间、payload_hash、
changed_after_review 和拒绝原因（复用 D8 通道；事件类型本模块扩展）。

门禁：``business.review_gate.check_review_valid`` 为本单元与 D21
execute 共用校验（RV-30 通过标准：旧 review 无法执行变化后的计划）。
本模块无任何 ERP 写入口（写入执行属 D21）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

import structlog

from administrative_agent.audit.service import AuditService
from administrative_agent.business.reconciliation_plan import WritePlan
from administrative_agent.business.review_gate import (
    DECISION_APPROVED,
    DECISION_REJECTED,
)
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskService

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.reviews")

#: 11.3 复核审计事件类型（D8 audit/service.py 枚举扩展：attempt/result 之外）
EVENT_REVIEW_REQUESTED = "review_requested"
EVENT_REVIEW_APPROVED = "review_approved"
EVENT_REVIEW_REJECTED = "review_rejected"
EVENT_REVIEW_INVALIDATED = "review_invalidated"
EVENT_REVIEW_REREQUESTED = "review_rerequested"

#: 复核审计事件全集（11.3 五类；测试断言用）
REVIEW_EVENT_TYPES = frozenset(
    {
        EVENT_REVIEW_REQUESTED,
        EVENT_REVIEW_APPROVED,
        EVENT_REVIEW_REJECTED,
        EVENT_REVIEW_INVALIDATED,
        EVENT_REVIEW_REREQUESTED,
    }
)

REVIEW_ID_PREFIX = "rev-"


# ---------------------------------------------------------------------------
# 领域记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRecord:
    """7.4 复核记录（12 字段全集；与 review_records 表一致）。

    - reviewed_payload：规范化写入计划（WritePlan.to_payload，键序稳定）；
    - reviewed_payload_hash：sha256（7.4；业务变化即 hash 变化）；
    - changed_after_review/invalidated_at：7.4 业务变化处理步骤 1/2；
    - decision：NULL=未复核；approved/rejected（8.1 waiting_review 语义）。
    """

    review_id: str
    task_id: str
    requested_by: str
    requested_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    decision: str | None = None
    reviewed_payload: dict[str, Any] | None = None
    reviewed_payload_hash: str = ""
    changed_after_review: bool = False
    invalidated_at: datetime | None = None
    rejection_reason: str | None = None

    @property
    def pending(self) -> bool:
        """未复核（decision 未决定且未失效；复核队列成员）。"""
        return self.decision is None and self.invalidated_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "task_id": self.task_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "decision": self.decision,
            "reviewed_payload": self.reviewed_payload,
            "reviewed_payload_hash": self.reviewed_payload_hash,
            "changed_after_review": self.changed_after_review,
            "invalidated_at": self.invalidated_at,
            "rejection_reason": self.rejection_reason,
        }


# ---------------------------------------------------------------------------
# 领域错误（API 层映射为稳定 error_code，2.7.3）
# ---------------------------------------------------------------------------


class ReviewError(ValueError):
    """复核领域错误基类（携带 error_code 供 API 稳定映射）。"""

    error_code = "REVIEW_ERROR"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ReviewNotFoundError(ReviewError):
    error_code = "REVIEW_NOT_FOUND"

    def __init__(self, review_id: str) -> None:
        super().__init__(f"复核 {review_id!r} 不存在")
        self.review_id = review_id


class ReviewInvalidStateError(ReviewError):
    error_code = "REVIEW_INVALID_STATE"

    def __init__(self, review_id: str, reason: str) -> None:
        super().__init__(f"复核 {review_id!r} 当前状态不允许该操作：{reason}")
        self.review_id = review_id


class ReviewHashMismatchError(ReviewError):
    error_code = "REVIEW_HASH_MISMATCH"

    def __init__(self, review_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"复核 {review_id!r} 的 payload hash 不匹配：记录 "
            f"{expected[:12]}…，提交 {actual[:12]}…（2.7.3 L514：批准当前 "
            "payload_hash；计划已变化需重新复核）"
        )
        self.review_id = review_id
        self.expected = expected
        self.actual = actual


class ReviewRejectionReasonRequiredError(ReviewError):
    error_code = "REVIEW_REJECTION_REASON_REQUIRED"

    def __init__(self, review_id: str) -> None:
        super().__init__(f"拒绝复核 {review_id!r} 必须提交拒绝原因（7.4 rejection_reason）")
        self.review_id = review_id


class ReviewAlreadyPendingError(ReviewError):
    error_code = "REVIEW_ALREADY_PENDING"

    def __init__(self, task_id: str, review_id: str, reason: str) -> None:
        super().__init__(f"任务 {task_id!r} 已有待复核 {review_id!r}：{reason}")
        self.task_id = task_id
        self.review_id = review_id


class ReviewTaskNotReviewableError(ReviewError):
    error_code = "REVIEW_INVALID_STATE"

    def __init__(self, task_id: str, status: str, reason: str) -> None:
        super().__init__(f"任务 {task_id!r}（{status}）无法发起/推进复核：{reason}")
        self.task_id = task_id


# ---------------------------------------------------------------------------
# 仓储契约（SQLAlchemy 实现见 persistence/repositories.py）
# ---------------------------------------------------------------------------


class ReviewRepository(Protocol):
    """review_records 持久化契约（7.4 十二字段）。"""

    def create(self, record: ReviewRecord) -> ReviewRecord: ...

    def get(self, review_id: str) -> ReviewRecord | None: ...

    def update(self, review_id: str, **fields) -> ReviewRecord: ...

    def list_by(
        self,
        *,
        task_id: str | None = None,
        pending_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[ReviewRecord], int]: ...


# ---------------------------------------------------------------------------
# 复核服务
# ---------------------------------------------------------------------------


class ReviewService:
    """R6 manager 复核编排（6.4 步骤 10 / 7.4 / 8.1 / 11.3）。

    职责：创建/决定/失效复核记录 + 任务状态迁移（复用 D6）+ 复核审计
    （复用 D8）。本服务不接触 HTTP（路由属 D20 web 层）与 ERP（写入属 D21）。
    """

    def __init__(
        self,
        *,
        review_repo: ReviewRepository,
        tasks: TaskService,
        audit: AuditService,
    ) -> None:
        self._reviews = review_repo
        self._tasks = tasks
        self._audit = audit

    # ---- 发起（6.4 步骤 10；7.4 复核记录创建） ----

    def request_review(
        self,
        *,
        task_id: str,
        plan: WritePlan,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str = "R6",
    ) -> ReviewRecord:
        """发起复核：写入计划 → review 记录 + 任务 waiting_review + 审计。

        幂等：同任务已有未决定且未失效、hash 相同的复核 → 直接复用
        （不重复建记录、不重复迁移、不重复审计）。hash 不同（计划已变）
        → 必须先失效旧复核（7.4 步骤 1/2）再重新发起。
        """
        payload = plan.to_payload()
        payload_hash = plan.payload_hash
        existing = self._find_pending(task_id)
        if existing is not None:
            if existing.reviewed_payload_hash == payload_hash:
                logger.info(
                    "review_request_idempotent",
                    review_id=existing.review_id,
                    task_id=task_id,
                )
                return existing
            raise ReviewAlreadyPendingError(
                task_id, existing.review_id,
                "写入计划已变化（payload hash 不同）；先失效旧复核再重新发起",
            )
        record = ReviewRecord(
            review_id=f"{REVIEW_ID_PREFIX}{uuid.uuid4().hex[:16]}",
            task_id=task_id,
            requested_by=actor_account,
            reviewed_payload=payload,
            reviewed_payload_hash=payload_hash,
        )
        record = self._reviews.create(record)
        self._move_to_waiting_review(task_id, payload_hash)
        self._audit_review(
            event_type=EVENT_REVIEW_REQUESTED,
            review=record,
            actor_account=actor_account,
            actor_role=actor_role,
            trace_id=trace_id,
            duty_code=duty_code,
            extra={"requested_by": actor_account},
        )
        logger.info(
            "review_requested",
            review_id=record.review_id,
            task_id=task_id,
            payload_hash=payload_hash[:16],
        )
        return record

    # ---- 批准（2.7.3 L514：批准当前 payload_hash） ----

    def approve(
        self,
        *,
        review_id: str,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str = "R6",
        expected_payload_hash: str | None = None,
    ) -> ReviewRecord:
        """批准复核：decision=approved → 任务 waiting_review → running。

        - 只允许未决定且未失效的复核（已决定/已失效 → 拒绝）；
        - expected_payload_hash 提供时（L514 批准当前 payload_hash）
          必须与 reviewed_payload_hash 一致，不一致拒绝并提示重新复核；
        - 7.5：reviewed_by 记录共享 manager 账号，不虚构自然人。
        """
        record = self._require_pending(review_id)
        if (
            expected_payload_hash is not None
            and expected_payload_hash != record.reviewed_payload_hash
        ):
            raise ReviewHashMismatchError(
                review_id, record.reviewed_payload_hash, expected_payload_hash
            )
        updated = self._reviews.update(
            review_id,
            decision=DECISION_APPROVED,
            reviewed_by=actor_account,
            reviewed_at=_now(),
        )
        try:
            self._tasks.review(record.task_id, approved=True)
        except Exception as exc:
            # 任务状态不允许（如已被取消）：复核决定已落库，标记任务异常
            logger.error(
                "review_approve_task_transition_failed",
                review_id=review_id,
                task_id=record.task_id,
                error=type(exc).__name__,
            )
            raise ReviewTaskNotReviewableError(
                record.task_id, "?", f"批准后任务无法恢复执行：{exc}"
            ) from exc
        self._audit_review(
            event_type=EVENT_REVIEW_APPROVED,
            review=updated,
            actor_account=actor_account,
            actor_role=actor_role,
            trace_id=trace_id,
            duty_code=duty_code,
            extra={"reviewed_by": actor_account},
        )
        logger.info(
            "review_approved",
            review_id=review_id,
            task_id=record.task_id,
        )
        return updated

    # ---- 拒绝（8.1：复核拒绝后终止） ----

    def reject(
        self,
        *,
        review_id: str,
        rejection_reason: str,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str = "R6",
    ) -> ReviewRecord:
        """拒绝复核：rejection_reason 必填；任务 waiting_review → cancelled。"""
        record = self._require_pending(review_id)
        if not rejection_reason or not rejection_reason.strip():
            raise ReviewRejectionReasonRequiredError(review_id)
        updated = self._reviews.update(
            review_id,
            decision=DECISION_REJECTED,
            reviewed_by=actor_account,
            reviewed_at=_now(),
            rejection_reason=rejection_reason.strip(),
        )
        try:
            self._tasks.review(record.task_id, approved=False)
        except Exception as exc:
            logger.error(
                "review_reject_task_transition_failed",
                review_id=review_id,
                task_id=record.task_id,
                error=type(exc).__name__,
            )
            raise ReviewTaskNotReviewableError(
                record.task_id, "?", f"拒绝后任务无法终止：{exc}"
            ) from exc
        self._audit_review(
            event_type=EVENT_REVIEW_REJECTED,
            review=updated,
            actor_account=actor_account,
            actor_role=actor_role,
            trace_id=trace_id,
            duty_code=duty_code,
            extra={
                "reviewed_by": actor_account,
                "rejection_reason": updated.rejection_reason,
            },
        )
        logger.info(
            "review_rejected",
            review_id=review_id,
            task_id=record.task_id,
            rejection_reason=updated.rejection_reason,
        )
        return updated

    # ---- 失效（7.4 步骤 1/2：计划业务变化） ----

    def invalidate(
        self,
        *,
        review_id: str,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str = "R6",
    ) -> ReviewRecord:
        """计划业务变化 → 旧复核失效（7.4 步骤 1/2）。

        changed_after_review=true、invalidated_at 置位；任务状态不变
        （未复核前不得写入，步骤 5——门禁 check_review_valid 拒绝
        一切失效复核的执行）。
        """
        record = self._require(review_id)
        if record.invalidated_at is not None:
            raise ReviewInvalidStateError(
                review_id, "复核已失效（invalidated_at 已置位）"
            )
        updated = self._reviews.update(
            review_id,
            changed_after_review=True,
            invalidated_at=_now(),
        )
        self._audit_review(
            event_type=EVENT_REVIEW_INVALIDATED,
            review=updated,
            actor_account=actor_account,
            actor_role=actor_role,
            trace_id=trace_id,
            duty_code=duty_code,
            extra={"changed_after_review": True},
        )
        logger.info(
            "review_invalidated",
            review_id=review_id,
            task_id=record.task_id,
        )
        return updated

    # ---- 重新复核（7.4 步骤 3/4） ----

    def re_request_review(
        self,
        *,
        task_id: str,
        plan: WritePlan,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str = "R6",
    ) -> ReviewRecord:
        """失效旧复核后重新发起（7.4 步骤 3/4：生成新差异 → 重新复核）。

        - 该任务现存全部未决定复核先失效（changed_after_review=true、
          invalidated_at 置位——它们核对的都是变化前的旧计划）；
        - 创建新 review（新 payload hash）；任务状态：running →
          waiting_review，已 waiting_review 不动；
        - 审计 review_rerequested（11.3 明确"重新复核"单独事件）。
        """
        pending, _ = self._reviews.list_by(task_id=task_id, pending_only=True)
        for old in pending:
            self._reviews.update(
                old.review_id,
                changed_after_review=True,
                invalidated_at=_now(),
            )
            self._audit_review(
                event_type=EVENT_REVIEW_INVALIDATED,
                review=self._require(old.review_id),
                actor_account=actor_account,
                actor_role=actor_role,
                trace_id=trace_id,
                duty_code=duty_code,
                extra={"changed_after_review": True},
            )
        record = ReviewRecord(
            review_id=f"{REVIEW_ID_PREFIX}{uuid.uuid4().hex[:16]}",
            task_id=task_id,
            requested_by=actor_account,
            reviewed_payload=plan.to_payload(),
            reviewed_payload_hash=plan.payload_hash,
        )
        record = self._reviews.create(record)
        self._move_to_waiting_review(task_id, record.reviewed_payload_hash)
        self._audit_review(
            event_type=EVENT_REVIEW_REREQUESTED,
            review=record,
            actor_account=actor_account,
            actor_role=actor_role,
            trace_id=trace_id,
            duty_code=duty_code,
            extra={"requested_by": actor_account},
        )
        logger.info(
            "review_rerequested",
            review_id=record.review_id,
            task_id=task_id,
            payload_hash=record.reviewed_payload_hash[:16],
        )
        return record

    # ---- 只读查询（复核队列/详情，2.7.3） ----

    def list_reviews(
        self,
        *,
        pending_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[ReviewRecord], int]:
        """复核列表（队列 = pending_only=True：未决定且未失效）。"""
        return self._reviews.list_by(
            pending_only=pending_only, limit=limit, offset=offset
        )

    def get_review(self, review_id: str) -> ReviewRecord | None:
        return self._reviews.get(review_id)

    # ---- 内部 ----

    def _find_pending(self, task_id: str) -> ReviewRecord | None:
        pending, _ = self._reviews.list_by(task_id=task_id, pending_only=True)
        return pending[0] if pending else None

    def _require(self, review_id: str) -> ReviewRecord:
        record = self._reviews.get(review_id)
        if record is None:
            raise ReviewNotFoundError(review_id)
        return record

    def _require_pending(self, review_id: str) -> ReviewRecord:
        record = self._require(review_id)
        if record.decision is not None:
            raise ReviewInvalidStateError(
                review_id, f"已决定（decision={record.decision!r}）"
            )
        if record.invalidated_at is not None:
            raise ReviewInvalidStateError(review_id, "复核已失效")
        return record

    def _move_to_waiting_review(self, task_id: str, payload_hash: str) -> None:
        """任务 running → waiting_review（D6 request_review 复用）；
        已 waiting_review（旧复核失效后重新发起）不动；终态拒绝。"""
        task = self._tasks.get_task(task_id)
        if task is None:
            raise ReviewTaskNotReviewableError(task_id, "?", "任务不存在")
        if task.status == TaskStatus.WAITING_REVIEW:
            return
        if task.status != TaskStatus.RUNNING:
            raise ReviewTaskNotReviewableError(
                task_id, task.status.value,
                "只有 running（或已 waiting_review）任务可进入复核",
            )
        self._tasks.request_review(task_id, payload_hash)

    def _audit_review(
        self,
        *,
        event_type: str,
        review: ReviewRecord,
        actor_account: str,
        actor_role: str,
        trace_id: str,
        duty_code: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """11.3 复核审计：requested_by/reviewed_by/时间/payload_hash/
        changed_after_review/拒绝原因（结构化载荷 + 摘要文本）。"""
        payload: dict[str, Any] = {
            "review_id": review.review_id,
            "payload_hash": review.reviewed_payload_hash,
            "changed_after_review": review.changed_after_review,
            "requested_by": review.requested_by,
            "reviewed_by": review.reviewed_by,
            "rejection_reason": review.rejection_reason,
        }
        if extra:
            payload.update(extra)
        self._audit.record_event(
            event_type=event_type,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            task_id=review.task_id,
            duty_code=duty_code,
            target_type="review",
            target_id=review.review_id,
            request_summary=(
                f"{event_type} review {review.review_id}"
                f" (task {review.task_id}, hash {review.reviewed_payload_hash[:12]}…)"
            ),
            structured_payload=payload,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)
