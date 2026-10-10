"""AuditService 测试（D8/RV-11：11.1 事件、11.2 attempt/result 时序）。

核心验证点（任务书）：
- attempt 调用前写、result 调用后写（10.4 顺序 7/11，created_at 时序）；
- attempt_no 递增（每次实际调用独立 attempt，网络重试不覆盖，11.2 规则 3）；
- result_status 四值校验（11.2 规则 2）；
- operation_id/trace_id 贯通 attempt/result/审计事件（L1179 追查链）；
- 审计失败抛 AuditFailureError 携带 operation_id（10.4 不假报成功）。
"""

from __future__ import annotations

import pytest

from administrative_agent.audit.service import (
    EVENT_ATTEMPT,
    EVENT_EXPORT,
    EVENT_RESULT,
    AuditEventRecord,
    AuditFailureError,
    AuditService,
)
from tests.audit.conftest import (
    OP_ID,
    TASK_ID,
    TRACE_ID,
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
    attempt_kwargs,
)


# ---- 11.1 record_event ----


def test_record_event_full_fields(audit_service: AuditService) -> None:
    event = audit_service.record_event(
        event_type="attempt",
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        task_id=TASK_ID,
        operation_id=OP_ID,
        duty_code="R1",
        policy_key="order-submit",
        policy_version="v1",
        policy_hash="abc123",
        target_type="order",
        target_id="SO-1001",
        request_summary="submit order",
        structured_payload={"attempt_no": 1},
        before_snapshot={"status": "draft"},
        after_snapshot={"status": "submitted"},
        source="agent",
        result_code=None,
    )
    # 11.1 关键字段齐备
    assert event.audit_id.startswith("aud-")
    assert event.task_id == TASK_ID
    assert event.operation_id == OP_ID
    assert event.trace_id == TRACE_ID
    assert event.actor_account == "manager01"
    assert event.actor_role == "manager"
    assert event.event_type == "attempt"
    assert event.duty_code == "R1"
    assert event.policy_key == "order-submit"
    assert event.policy_version == "v1"
    assert event.policy_hash == "abc123"
    assert event.target_type == "order"
    assert event.target_id == "SO-1001"
    assert event.request_summary == "submit order"
    assert event.structured_payload == {"attempt_no": 1}
    assert event.before_snapshot == {"status": "draft"}
    assert event.after_snapshot == {"status": "submitted"}
    assert event.source == "agent"
    assert event.created_at is not None


def test_record_event_redacts_payload(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    event = audit_service.record_event(
        event_type="attempt",
        trace_id=TRACE_ID,
        actor_account="u",
        actor_role="r",
        structured_payload={"api_key": "sk-live-abc", "ok": 1},
        before_snapshot={"password": "hunter2"},
    )
    assert event.structured_payload["api_key"].startswith("***")
    assert event.structured_payload["ok"] == 1
    assert event.before_snapshot["password"].startswith("***")
    # 落库记录同样已脱敏（明文不落 audit_events）
    raw = audit_repo.list_by(trace_id=TRACE_ID)[0]
    assert "sk-live-abc" not in repr(raw)


def test_record_event_failure_raises_audit_failure_error_with_operation_id(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    audit_repo.fail_next = True
    with pytest.raises(AuditFailureError) as exc:
        audit_service.record_event(
            event_type="attempt",
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
            operation_id=OP_ID,
        )
    assert exc.value.operation_id == OP_ID
    # 失败不留下半条记录
    assert audit_repo._events == []


# ---- 11.2 record_attempt（调用前写） ----


def test_record_attempt_writes_before_call(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    assert attempt.attempt_id.startswith("atp-")
    assert attempt.attempt_no == 1
    assert attempt.task_id == TASK_ID
    assert attempt.operation_id == OP_ID
    assert attempt.target_type == "order"
    assert attempt.target_id == "SO-1001"
    assert attempt.created_at is not None
    assert attempt.result_status is None  # result 尚未回填
    # 配套审计事件（attempt）同步落库，trace 一致
    event = audit_repo.list_by(event_type=EVENT_ATTEMPT, trace_id=TRACE_ID)[0]
    assert event.operation_id == OP_ID
    assert event.task_id == TASK_ID
    assert event.target_id == "SO-1001"
    assert event.structured_payload["attempt_no"] == 1
    assert event.structured_payload["field_diffs"] == {"status": ("draft", "submitted")}
    assert event.structured_payload["idempotency_key"] == "idem-1"


def test_record_attempt_no_increments_per_actual_call(
    audit_service: AuditService,
) -> None:
    first = audit_service.record_attempt(**attempt_kwargs())
    second = audit_service.record_attempt(**attempt_kwargs())
    assert first.attempt_no == 1
    assert second.attempt_no == 2  # 重试是独立 attempt，不覆盖（11.2 规则 3）
    assert first.attempt_id != second.attempt_id
    attempts = audit_service.attempts(OP_ID)
    assert [a.attempt_no for a in attempts] == [1, 2]


def test_record_attempt_redacts_field_diffs(audit_service: AuditService) -> None:
    attempt = audit_service.record_attempt(
        **attempt_kwargs(field_diffs={"api_key": ("old", "new"), "status": ("a", "b")})
    )
    assert attempt.field_diffs["api_key"].startswith("***")
    assert attempt.field_diffs["status"] == ("a", "b")


def test_record_attempt_failure_raises_with_operation_id(
    audit_service: AuditService,
    attempt_repo: InMemoryAttemptRepository,
) -> None:
    attempt_repo.fail_next = True
    with pytest.raises(AuditFailureError) as exc:
        audit_service.record_attempt(**attempt_kwargs())
    assert exc.value.operation_id == OP_ID


# ---- 11.2 record_result（调用后写） ----


def test_record_result_backfills_attempt_after_call(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    result = audit_service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="success",
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        result_summary="SO-1001 submitted",
    )
    # 回填同一行：attempt_id 不变、result 字段填充、created_at 保留
    assert result.attempt_id == attempt.attempt_id
    assert result.attempt_no == 1
    assert result.created_at == attempt.created_at
    assert result.result_status == "success"
    assert result.result_summary == "SO-1001 submitted"
    assert result.result_at is not None
    # 时序：attempt（调用前）早于 result（调用后）
    assert result.created_at < result.result_at
    # 配套 result 审计事件，operation_id 关联回 attempt
    event = audit_repo.list_by(event_type=EVENT_RESULT, trace_id=TRACE_ID)[0]
    assert event.operation_id == OP_ID
    assert event.result_code == "success"
    assert event.structured_payload["attempt_no"] == 1


def test_record_result_merges_structured_payload(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    """D14 5.3 步骤 7：订单响应/other_effects 经 structured_payload 并入
    result 事件（脱敏后），保留既有 attempt_no/error_code 基础键。"""
    attempt = audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="success",
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        result_summary="created metal order 5000",
        structured_payload={
            "order_id": 5000,
            "replayed": False,
            "order_type": "metal",
            "other_effects": [
                {"type": "receivable_current_impact", "customer_id": 11, "amount": "0.00"}
            ],
        },
    )
    event = audit_repo.list_by(event_type=EVENT_RESULT, trace_id=TRACE_ID)[0]
    payload = event.structured_payload
    assert payload["attempt_no"] == 1
    assert payload["error_code"] is None
    assert payload["order_id"] == 5000
    assert payload["replayed"] is False
    assert payload["order_type"] == "metal"
    assert payload["other_effects"][0]["customer_id"] == 11
    assert event.result_code == "success"


def test_record_result_structured_payload_redacted(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    """structured_payload 落库前脱敏（11.2 规则 4：密码/密钥不入审计）。"""
    attempt = audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="failed",
        trace_id=TRACE_ID,
        actor_account="u",
        actor_role="r",
        error_code="SOME_ERROR",
        structured_payload={"order_id": 1, "erp_api_key": "sk-secret-123"},
    )
    event = audit_repo.list_by(event_type=EVENT_RESULT, trace_id=TRACE_ID)[0]
    assert event.structured_payload["erp_api_key"] != "sk-secret-123"
    assert event.structured_payload["order_id"] == 1


def test_record_result_structured_payload_none_keeps_basic_keys(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    """不传 structured_payload：事件载荷保持基础键（向后兼容，D12 调用方）。"""
    attempt = audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="conflict",
        trace_id=TRACE_ID,
        actor_account="u",
        actor_role="r",
        error_code="ORDER_IDEMPOTENCY_KEY_CONFLICT",
    )
    event = audit_repo.list_by(event_type=EVENT_RESULT, trace_id=TRACE_ID)[0]
    assert set(event.structured_payload) == {"attempt_no", "error_code", "requires_operator_action"}
    assert event.structured_payload["error_code"] == "ORDER_IDEMPOTENCY_KEY_CONFLICT"


def test_result_status_enum_four_values(audit_service: AuditService) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    for status in ("success", "replayed", "failed", "conflict"):
        result = audit_service.record_result(
            attempt_id=attempt.attempt_id,
            result_status=status,
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
        )
        assert result.result_status == status


def test_result_status_rejects_unknown(audit_service: AuditService) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    with pytest.raises(ValueError):
        audit_service.record_result(
            attempt_id=attempt.attempt_id,
            result_status="pending",  # 非四值
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
        )


def test_result_without_prior_attempt_raises(audit_service: AuditService) -> None:
    with pytest.raises(AuditFailureError):
        audit_service.record_result(
            attempt_id="atp-no-such",
            result_status="success",
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
        )


def test_result_failure_raises_with_operation_id(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    audit_repo.fail_next = True  # 审计事件写失败
    with pytest.raises(AuditFailureError) as exc:
        audit_service.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
        )
    assert exc.value.operation_id == OP_ID


# ---- 追查链（L1179）：trace 一致 + 查询 ----


def test_attempt_result_events_share_trace_and_operation(
    audit_service: AuditService,
    audit_repo: InMemoryAuditRepository,
) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="success",
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
    )
    events = audit_repo.list_by(operation_id=OP_ID)
    assert {e.event_type for e in events} == {EVENT_ATTEMPT, EVENT_RESULT}
    assert all(e.trace_id == TRACE_ID for e in events)
    assert all(e.operation_id == OP_ID for e in events)
    assert all(e.task_id == TASK_ID for e in events)


def test_events_filter_and_limit(audit_service: AuditService) -> None:
    for i in range(3):
        attempt = audit_service.record_attempt(**attempt_kwargs())
        audit_service.record_result(
            attempt_id=attempt.attempt_id,
            result_status="success",
            trace_id=TRACE_ID,
            actor_account="u",
            actor_role="r",
        )
    assert len(audit_service.events(trace_id=TRACE_ID, limit=4)) == 4
    assert len(audit_service.events(event_type=EVENT_ATTEMPT)) == 3
    assert len(audit_service.events(event_type=EVENT_RESULT)) == 3
    assert audit_service.events(operation_id="op-nope") == []


def test_attempts_sorted_by_attempt_no(audit_service: AuditService) -> None:
    audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_attempt(**attempt_kwargs())
    audit_service.record_attempt(**attempt_kwargs())
    assert [a.attempt_no for a in audit_service.attempts(OP_ID)] == [1, 2, 3]


def test_get_attempt_roundtrip(audit_service: AuditService) -> None:
    attempt = audit_service.record_attempt(**attempt_kwargs())
    assert audit_service.get_attempt(attempt.attempt_id) == attempt
    assert audit_service.get_attempt("atp-nope") is None


# ---- EVENT_EXPORT：列名与显式敏感字段进结构化载荷（2026-09-15）----


def test_record_export_writes_columns_and_sensitive_fields(
    audit_service: AuditService, audit_repo: InMemoryAuditRepository
) -> None:
    """record_export 必须把本次导出列与显式点名敏感字段写进 structured_payload。"""
    audit_service.record_export(
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        entity="customers",
        filter=None,
        row_count=1,
        file_id="f" * 32,
        columns=["company_name", "bank_account"],
        sensitive_fields=["bank_account"],
    )
    (event,) = audit_repo.list_by(event_type=EVENT_EXPORT)
    assert event.target_id == "f" * 32
    p = event.structured_payload
    assert p is not None
    assert p["entity"] == "customers"
    assert p["columns"] == ["company_name", "bank_account"]
    assert p["sensitive_fields"] == ["bank_account"]


def test_record_export_columns_optional_defaults_none(
    audit_service: AuditService, audit_repo: InMemoryAuditRepository
) -> None:
    """不传 columns/sensitive_fields（旧调用方）——键仍在，值为 None（additive）。"""
    audit_service.record_export(
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        entity="customers",
        filter=None,
        row_count=0,
        file_id="a" * 32,
    )
    (event,) = audit_repo.list_by(event_type=EVENT_EXPORT)
    p = event.structured_payload
    assert p is not None
    assert p["columns"] is None
    assert p["sensitive_fields"] is None
