"""D20 R6 manager 复核域测试（6.4 步骤 10、7.4、8.1、11.3、RV-30）。

覆盖（任务书测试要求）：
- 复核发起：review 记录（12 字段）+ 任务 waiting_review + 审计
  review_requested；幂等复用；hash 不同冲突；
- 批准：decision=approved + 任务 running + 审计 review_approved；
  L514"批准当前 payload_hash"（hash 不一致拒绝）；
- 拒绝：rejection_reason 必填 + 任务 cancelled（8.1）+ 审计含原因；
- 失效（7.4 步骤 1/2）：changed_after_review=true + invalidated_at；
  失效后门禁拒绝（旧 review 无法执行变化后的计划，RV-30 通过标准）；
- 重新复核（7.4 步骤 3/4）：旧复核失效 + 新 review + 审计
  review_rerequested；
- 审计字段（11.3）：requested_by/reviewed_by/payload_hash/
  changed_after_review/rejection_reason/时间；
- 复核队列（pending_only）与终态任务拒绝发起。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from administrative_agent.audit.service import AuditService
from administrative_agent.business.matching import MatchOutcome
from administrative_agent.business.reconciliation_plan import WritePlan, build_write_plan
from administrative_agent.business.reconciliation_rules import (
    FIELD_DIFF,
    WRITE_UPDATE_ORDER,
    DiffEntry,
    FieldDiff,
    MatchKey,
)
from administrative_agent.business.review_gate import check_review_valid
from administrative_agent.domains.reconciliation import ReconciliationReport
from administrative_agent.domains.reviews import (
    EVENT_REVIEW_APPROVED,
    EVENT_REVIEW_INVALIDATED,
    EVENT_REVIEW_REJECTED,
    EVENT_REVIEW_REREQUESTED,
    EVENT_REVIEW_REQUESTED,
    REVIEW_ID_PREFIX,
    ReviewAlreadyPendingError,
    ReviewHashMismatchError,
    ReviewInvalidStateError,
    ReviewNotFoundError,
    ReviewRecord,
    ReviewRejectionReasonRequiredError,
    ReviewService,
    ReviewTaskNotReviewableError,
)
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskService
from tests.audit.conftest import InMemoryAttemptRepository, InMemoryAuditRepository
from tests.tasks.conftest import InMemoryTaskRepository, write_spec

_HASH = "sha256-demo-file"
_ENTITY = "metal_orders"


def entry(classification=FIELD_DIFF, *, row_number: int = 2,
          write_entry: str | None = WRITE_UPDATE_ORDER,
          diffs=(("weight_per_unit", "4.00", "3.00"),)) -> DiffEntry:
    return DiffEntry(
        classification=classification,
        row_number=row_number,
        source_file_hash=_HASH,
        mapping_version=3,
        match_key=MatchKey(entity=_ENTITY, subject_id=101, product_ref="M-1",
                           in_date="2026-06-02"),
        matched_order_id=11,
        diffs=tuple(
            FieldDiff(field=f, excel_value=x, erp_value=e) for f, x, e in diffs
        ),
        write_entry=write_entry,
    )


def build_report(entries) -> ReconciliationReport:
    return ReconciliationReport(
        task_id="task-test-0001",
        template_key="recon-metal",
        mapping_version=3,
        source_file_hash=_HASH,
        entity=_ENTITY,
        subject_match=MatchOutcome(ref="示例五金", status="matched",
                                   entity_id=101, matched_name="示例五金",
                                   candidates=((101, "示例五金"),)),
        period_note="2026 年 6 月",
        row_count=len(entries),
        conversion_errors=(),
        entries=tuple(entries),
        data_completeness="complete",
    )

TRACE_ID = "trace-d20-test-000001"
ACTOR = "manager01"
ROLE = "manager"


class InMemoryReviewRepository:
    """ReviewRepository 协议的内存实现（确定性、可并发断言）。"""

    def __init__(self) -> None:
        self._store: dict[str, ReviewRecord] = {}
        self.clock = [datetime(2026, 8, 23, 9, 0, 0, tzinfo=timezone.utc)]

    def _tick(self) -> datetime:
        dt = self.clock[-1]
        self.clock.append(
            datetime.fromtimestamp(dt.timestamp() + 1, tz=timezone.utc)
        )
        return dt

    def create(self, record: ReviewRecord) -> ReviewRecord:
        saved = copy.deepcopy(record)
        saved = ReviewRecord(
            review_id=saved.review_id, task_id=saved.task_id,
            requested_by=saved.requested_by, requested_at=self._tick(),
            reviewed_by=saved.reviewed_by, reviewed_at=saved.reviewed_at,
            decision=saved.decision, reviewed_payload=saved.reviewed_payload,
            reviewed_payload_hash=saved.reviewed_payload_hash,
            changed_after_review=saved.changed_after_review,
            invalidated_at=saved.invalidated_at,
            rejection_reason=saved.rejection_reason,
        )
        self._store[saved.review_id] = saved
        return copy.deepcopy(saved)

    def get(self, review_id: str) -> ReviewRecord | None:
        record = self._store.get(review_id)
        return copy.deepcopy(record) if record else None

    def update(self, review_id: str, **fields) -> ReviewRecord:
        record = self._store[review_id]
        values = dict(
            review_id=record.review_id, task_id=record.task_id,
            requested_by=record.requested_by, requested_at=record.requested_at,
            reviewed_by=record.reviewed_by, reviewed_at=record.reviewed_at,
            decision=record.decision, reviewed_payload=record.reviewed_payload,
            reviewed_payload_hash=record.reviewed_payload_hash,
            changed_after_review=record.changed_after_review,
            invalidated_at=record.invalidated_at,
            rejection_reason=record.rejection_reason,
        )
        for key, value in fields.items():
            if key not in values:
                raise ValueError(f"unknown review field: {key}")
            if key == "reviewed_at" and value is None:
                value = self._tick()
            values[key] = value
        updated = ReviewRecord(**values)
        self._store[review_id] = updated
        return copy.deepcopy(updated)

    def list_by(
        self,
        *,
        task_id: str | None = None,
        pending_only: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[ReviewRecord], int]:
        rows = [
            copy.deepcopy(r) for r in self._store.values()
            if (task_id is None or r.task_id == task_id)
            and (not pending_only or (r.decision is None and r.invalidated_at is None))
        ]
        rows.sort(key=lambda r: r.requested_at or datetime.min.replace(tzinfo=timezone.utc))
        total = len(rows)
        return rows[offset or 0:(offset or 0) + limit] if limit else rows[offset or 0:], total


@pytest.fixture()
def services():
    task_repo = InMemoryTaskRepository()
    tasks = TaskService(task_repo)
    audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
    reviews = ReviewService(
        review_repo=InMemoryReviewRepository(), tasks=tasks, audit=audit
    )
    return tasks, audit, reviews


def _running_task(tasks: TaskService) -> object:
    return tasks.create_task(
        write_spec(
            thread_id="thread-mgr-20",
            duty_code="R6",
            original_user_message="核对示例五金 6 月清单并执行差异",
            structured_intent={"duty": "R6", "operation": "reconcile_execute"},
        )
    )


def _plan() -> WritePlan:
    entries = [
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "4.00", "3.00"),)),
    ]
    return build_write_plan(build_report(entries))


def _audit_events(audit, event_type: str):
    return audit.events(trace_id=TRACE_ID, event_type=event_type)


# ---- 发起（6.4 步骤 10） ----


def test_request_review_creates_full_record(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    plan = _plan()
    record = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    # 12 字段齐全（7.4）
    assert record.review_id.startswith(REVIEW_ID_PREFIX)
    assert record.task_id == task.task_id
    assert record.requested_by == ACTOR
    assert record.requested_at is not None
    assert record.reviewed_by is None and record.reviewed_at is None
    assert record.decision is None
    assert record.reviewed_payload == plan.to_payload()
    assert record.reviewed_payload_hash == plan.payload_hash
    assert record.changed_after_review is False
    assert record.invalidated_at is None
    assert record.rejection_reason is None
    # 任务状态 → waiting_review（复用 D6）
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_REVIEW
    assert tasks.get_task(task.task_id).reviewed_payload_hash == plan.payload_hash
    # 审计 review_requested（11.3）
    events = _audit_events(audit, EVENT_REVIEW_REQUESTED)
    assert len(events) == 1
    payload = events[0].structured_payload
    assert payload["review_id"] == record.review_id
    assert payload["payload_hash"] == plan.payload_hash
    assert payload["requested_by"] == ACTOR


def test_request_review_idempotent_same_hash(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    plan = _plan()
    first = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    second = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    assert second.review_id == first.review_id
    assert len(_audit_events(audit, EVENT_REVIEW_REQUESTED)) == 1  # 不重复审计


def test_request_review_conflicts_on_hash_change(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    plan = _plan()
    reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    changed = build_write_plan(build_report([
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "5.00", "3.00"),)),
    ]))
    with pytest.raises(ReviewAlreadyPendingError):
        reviews.request_review(
            task_id=task.task_id, plan=changed,
            actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
        )


def test_request_review_terminal_task_rejected(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    tasks.mark_succeeded(task.task_id, verification_passed=True)
    with pytest.raises(ReviewTaskNotReviewableError):
        reviews.request_review(
            task_id=task.task_id, plan=_plan(),
            actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
        )


# ---- 批准（2.7.3 L514） ----


def test_approve_resumes_task_and_audits(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    plan = _plan()
    record = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    approved = reviews.approve(
        review_id=record.review_id,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,  # 批准当前 payload_hash（L514）
    )
    assert approved.decision == "approved"
    assert approved.reviewed_by == ACTOR
    assert approved.reviewed_at is not None
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING  # 复核通过续执行
    event = _audit_events(audit, EVENT_REVIEW_APPROVED)[0]
    assert event.structured_payload["reviewed_by"] == ACTOR


def test_approve_hash_mismatch_rejected(services):
    """L514：hash 不一致不得批准（提示重新复核）。"""
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    with pytest.raises(ReviewHashMismatchError) as exc:
        reviews.approve(
            review_id=record.review_id,
            actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
            expected_payload_hash="x" * 64,
        )
    assert "payload hash 不匹配" in str(exc.value)
    # 未批准：任务仍 waiting_review
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_REVIEW


def test_approve_already_decided_rejected(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    reviews.approve(review_id=record.review_id,
                    actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)
    with pytest.raises(ReviewInvalidStateError):
        reviews.approve(review_id=record.review_id,
                        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)


def test_approve_invalidated_review_rejected(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    reviews.invalidate(review_id=record.review_id,
                       actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)
    with pytest.raises(ReviewInvalidStateError):
        reviews.approve(review_id=record.review_id,
                        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)


def test_approve_not_found(services):
    tasks, audit, reviews = services
    with pytest.raises(ReviewNotFoundError):
        reviews.approve(review_id="rev-missing",
                        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)


# ---- 拒绝（8.1：复核拒绝后终止） ----


def test_reject_requires_reason(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    with pytest.raises(ReviewRejectionReasonRequiredError):
        reviews.reject(review_id=record.review_id, rejection_reason="  ",
                       actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)


def test_reject_cancels_task_and_audits_reason(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    rejected = reviews.reject(
        review_id=record.review_id, rejection_reason="清单单价与客户价目不符",
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    assert rejected.decision == "rejected"
    assert rejected.rejection_reason == "清单单价与客户价目不符"
    assert rejected.reviewed_by == ACTOR
    assert tasks.get_task(task.task_id).status == TaskStatus.CANCELLED  # 8.1
    event = _audit_events(audit, EVENT_REVIEW_REJECTED)[0]
    payload = event.structured_payload
    assert payload["rejection_reason"] == "清单单价与客户价目不符"
    assert payload["reviewed_by"] == ACTOR


# ---- 失效与重新复核（7.4 5 步；14.1 L1380 场景） ----


def test_invalidate_marks_old_review(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    invalidated = reviews.invalidate(
        review_id=record.review_id,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    assert invalidated.changed_after_review is True
    assert invalidated.invalidated_at is not None
    event = _audit_events(audit, EVENT_REVIEW_INVALIDATED)[0]
    assert event.structured_payload["changed_after_review"] is True
    # 任务状态不变（未复核前不得写入，7.4 步骤 5）
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_REVIEW
    # 已批准的复核失效后同样被门禁拒绝——旧 review 无法执行（RV-30）
    assert check_review_valid(invalidated, current_payload_hash=invalidated.reviewed_payload_hash) is not None


def test_invalidate_twice_rejected(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    reviews.invalidate(review_id=record.review_id,
                       actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)
    with pytest.raises(ReviewInvalidStateError):
        reviews.invalidate(review_id=record.review_id,
                           actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)


def test_payload_change_invalidates_then_requires_rereview(services):
    """14.1 L1380 场景：payload hash 变化 → 复核失效 → 重新复核。
    未重新复核前旧 review 无法执行（RV-30 通过标准）。"""
    tasks, audit, reviews = services
    task = _running_task(tasks)
    plan = _plan()
    record = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    # 业务变化：重新解析生成新差异（新 hash）
    changed_plan = build_write_plan(build_report([
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "5.00", "3.00"),)),
    ]))
    assert changed_plan.payload_hash != record.reviewed_payload_hash
    # 门禁：旧 review 对新计划失效（即使已批准）
    approved = reviews.approve(review_id=record.review_id,
                               actor_account=ACTOR, actor_role=ROLE,
                               trace_id=TRACE_ID)
    reason = check_review_valid(approved, current_payload_hash=changed_plan.payload_hash)
    assert reason is not None and "不一致" in reason
    # 业务流程：失效旧复核 → 重新请求（7.4 步骤 1-4）
    reviews.invalidate(review_id=record.review_id,
                       actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)
    new_record = reviews.re_request_review(
        task_id=task.task_id, plan=changed_plan,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    assert new_record.review_id != record.review_id
    assert new_record.reviewed_payload_hash == changed_plan.payload_hash
    # 旧 review 已失效 + 标记变化；任务重新等待复核
    old_after = reviews.get_review(record.review_id)
    assert old_after.changed_after_review is True and old_after.invalidated_at is not None
    assert tasks.get_task(task.task_id).status == TaskStatus.WAITING_REVIEW
    # 未复核前不得写入：门禁对旧 review 五条件全拒
    assert check_review_valid(old_after, current_payload_hash=changed_plan.payload_hash) is not None
    # 重新复核通过后才能执行
    new_approved = reviews.approve(
        review_id=new_record.review_id,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
        expected_payload_hash=changed_plan.payload_hash,
    )
    assert new_approved.decision == "approved"
    assert tasks.get_task(task.task_id).status == TaskStatus.RUNNING
    assert check_review_valid(new_approved, current_payload_hash=changed_plan.payload_hash) is None
    # 审计链：发起/批准/失效/重新发起 四类事件齐全（11.3）
    assert len(_audit_events(audit, EVENT_REVIEW_REQUESTED)) == 1
    assert len(_audit_events(audit, EVENT_REVIEW_APPROVED)) == 2
    assert len(_audit_events(audit, EVENT_REVIEW_INVALIDATED)) == 1
    assert len(_audit_events(audit, EVENT_REVIEW_REREQUESTED)) == 1


def test_rerequest_with_pending_old_review_invalidates_it(services):
    """重新复核前旧复核仍在等待 → 先失效（7.4 步骤 1/2）再重新发起。"""
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    changed = build_write_plan(build_report([
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "6.00", "3.00"),)),
    ]))
    new_record = reviews.re_request_review(
        task_id=task.task_id, plan=changed,
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    old_after = reviews.get_review(record.review_id)
    assert old_after.invalidated_at is not None
    assert old_after.changed_after_review is True
    assert new_record.pending  # 新复核在队列中
    queue, total = reviews.list_reviews(pending_only=True)
    assert total == 1 and queue[0].review_id == new_record.review_id


# ---- 队列与详情（2.7.3 GET /reviews） ----


def test_review_queue_and_detail(services):
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID,
    )
    queue, total = reviews.list_reviews(pending_only=True)
    assert total == 1 and queue[0].review_id == record.review_id
    detail = reviews.get_review(record.review_id)
    assert detail is not None and detail.reviewed_payload == _plan().to_payload()
    # 决定后不在待复核队列
    reviews.approve(review_id=record.review_id,
                    actor_account=ACTOR, actor_role=ROLE, trace_id=TRACE_ID)
    queue, total = reviews.list_reviews(pending_only=True)
    assert total == 0
    all_records, total_all = reviews.list_reviews()
    assert total_all == 1


def test_shared_manager_account_not_fabricated(services):
    """7.5：requested_by/reviewed_by 记录共享 manager 账号，不虚构自然人。"""
    tasks, audit, reviews = services
    task = _running_task(tasks)
    record = reviews.request_review(
        task_id=task.task_id, plan=_plan(),
        actor_account="shared_manager", actor_role=ROLE, trace_id=TRACE_ID,
    )
    approved = reviews.approve(review_id=record.review_id,
                               actor_account="shared_manager", actor_role=ROLE,
                               trace_id=TRACE_ID)
    assert approved.requested_by == "shared_manager"
    assert approved.reviewed_by == "shared_manager"  # 同账号，明确接受的限制
