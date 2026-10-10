"""D20 R6 manager 复核 API 测试（规格 2.7.3 API 契约 + 7.4/8.1 + RV-30）。

SQLite 内存库 + app.state.session_factory 注入（同 test_mappings_api.py
模式）。覆盖（任务书测试要求）：
- GET /api/v1/reviews 分页复核队列（pending_only 过滤 + total）
- GET /api/v1/reviews/:reviewId 详情（12 字段）与 404
- POST approve：批准当前 payload_hash（一致成功 / 不一致 409）、
  已决定/已失效 409、任务 waiting_review → running
- POST reject：拒绝原因必填（领域 400）、任务 → cancelled
- 安全：全部端点 manager 权限（developer 403 / 未登录 401）、
  写 API CSRF 校验（缺 Origin 403）、统一错误响应结构
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent.auth.models import Account, Role
from administrative_agent.auth.service import SESSION_COOKIE_NAME, create_session
from administrative_agent.audit.service import AuditService
from administrative_agent.business.reconciliation_plan import WritePlan, build_write_plan
from administrative_agent.business.reconciliation_rules import (
    FIELD_DIFF,
    WRITE_UPDATE_ORDER,
    DiffEntry,
    FieldDiff,
    MatchKey,
)
from administrative_agent.domains.reconciliation import ReconciliationReport
from administrative_agent.domains.reviews import ReviewService
from administrative_agent.main import app
from administrative_agent.persistence.models import Base
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyTaskRepository,
)
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.records import NewTaskSpec
from administrative_agent.tasks.service import TaskService


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_biginteger_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


VALID_ORIGIN = "http://127.0.0.1:5173"
MANAGER = "manager01"


@pytest.fixture()
def client() -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_conn, record):  # noqa: ANN001
        """SQLite 兼容：models 的 server_default=text('now()') → 当前时间。"""
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat()
        )

    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    app.state.session_factory = TestingSessionLocal
    try:
        yield TestClient(app)
    finally:
        del app.state.session_factory
        Base.metadata.drop_all(engine)


def _cookies(role: Role, account: str = MANAGER) -> dict:
    return {SESSION_COOKIE_NAME: create_session(Account(account=account, role=role))}


def _headers(origin: str | None = VALID_ORIGIN) -> dict:
    return {"Origin": origin} if origin else {}


def _setup_review(session_factory) -> tuple[str, str, str]:
    """建 running 任务 + 发起复核；返回 (task_id, review_id, payload_hash)。"""
    tasks = TaskService(SqlAlchemyTaskRepository(session_factory))
    audit = AuditService(
        SqlAlchemyAuditRepository(session_factory),
        SqlAlchemyAttemptRepository(session_factory),
    )
    reviews = ReviewService(
        review_repo=SqlAlchemyReviewRepository(session_factory),
        tasks=tasks, audit=audit,
    )
    task = tasks.create_task(
        NewTaskSpec(
            thread_id="thread-mgr-20",
            actor_account=MANAGER,
            actor_role="manager",
            duty_code="R6",
            original_user_message="核对示例五金 6 月清单并执行差异",
            structured_intent={"duty": "R6", "operation": "reconcile_execute"},
            is_write=True,
        )
    )
    plan = _plan(task.task_id)
    record = reviews.request_review(
        task_id=task.task_id, plan=plan,
        actor_account=MANAGER, actor_role="manager", trace_id="trace-d20-api-1",
    )
    return task.task_id, record.review_id, plan.payload_hash


def _plan(task_id: str) -> WritePlan:
    entries = [
        DiffEntry(
            classification=FIELD_DIFF, row_number=2,
            source_file_hash="sha256-demo-file", mapping_version=3,
            match_key=MatchKey(entity="metal_orders", subject_id=101,
                               product_ref="M-1", in_date="2026-06-02"),
            matched_order_id=11,
            diffs=(FieldDiff(field="weight_per_unit", excel_value="4.00",
                             erp_value="3.00"),),
            write_entry=WRITE_UPDATE_ORDER,
        ),
    ]
    return build_write_plan(ReconciliationReport(
        task_id=task_id, template_key="recon-metal", mapping_version=3,
        source_file_hash="sha256-demo-file", entity="metal_orders",
        subject_match=None, period_note="2026 年 6 月", row_count=1,
        conversion_errors=(), entries=tuple(entries), data_completeness="complete",
    ))


def _factory(client: TestClient):
    return client.app.state.session_factory


# ---- GET 队列与详情 ----


def test_list_reviews_queue_paginated(client):
    session_factory = _factory(client)
    task_id, review_id, _ = _setup_review(session_factory)
    resp = client.get("/api/v1/reviews", cookies=_cookies(Role.MANAGER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 50 and body["offset"] == 0
    item = body["items"][0]
    assert item["review_id"] == review_id
    assert item["task_id"] == task_id
    assert item["decision"] is None
    # 决定后不在队列（pending_only 语义）
    resp = client.post(
        f"/api/v1/reviews/{review_id}/approve",
        json={"payload_hash": _plan(task_id).payload_hash},
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp.status_code == 200
    resp = client.get("/api/v1/reviews", cookies=_cookies(Role.MANAGER))
    assert resp.json()["total"] == 0
    resp = client.get("/api/v1/reviews?pending=false",
                      cookies=_cookies(Role.MANAGER))
    assert resp.json()["total"] == 1


def test_get_review_detail_and_404(client):
    session_factory = _factory(client)
    task_id, review_id, payload_hash = _setup_review(session_factory)
    resp = client.get(f"/api/v1/reviews/{review_id}", cookies=_cookies(Role.MANAGER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_id"] == review_id
    assert body["task_id"] == task_id
    assert body["requested_by"] == MANAGER
    assert body["requested_at"] is not None
    assert body["reviewed_by"] is None
    assert body["decision"] is None
    assert body["reviewed_payload_hash"] == payload_hash
    assert body["reviewed_payload"]["items"][0]["write_path"] == WRITE_UPDATE_ORDER
    assert body["changed_after_review"] is False
    assert body["invalidated_at"] is None
    assert body["rejection_reason"] is None
    # 404 稳定错误码
    resp = client.get("/api/v1/reviews/rev-missing",
                      cookies=_cookies(Role.MANAGER))
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "REVIEW_NOT_FOUND"
    assert "message" in resp.json() and "trace_id" in resp.json()


# ---- POST approve ----


def test_approve_with_current_hash(client):
    session_factory = _factory(client)
    task_id, review_id, payload_hash = _setup_review(session_factory)
    resp = client.post(
        f"/api/v1/reviews/{review_id}/approve",
        json={"payload_hash": payload_hash},
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["reviewed_by"] == MANAGER
    assert body["reviewed_at"] is not None
    # 任务恢复 running（D21 才能执行）
    task = SqlAlchemyTaskRepository(session_factory).get(task_id)
    assert task.status == TaskStatus.RUNNING


def test_approve_hash_mismatch_rejected(client):
    """2.7.3 L514：批准当前 payload_hash——不一致拒绝并提示重新复核。"""
    session_factory = _factory(client)
    task_id, review_id, _ = _setup_review(session_factory)
    resp = client.post(
        f"/api/v1/reviews/{review_id}/approve",
        json={"payload_hash": "x" * 64},
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "REVIEW_HASH_MISMATCH"
    assert "重新复核" in resp.json()["message"]
    # 任务仍等待复核（未批准）
    task = SqlAlchemyTaskRepository(session_factory).get(task_id)
    assert task.status == TaskStatus.WAITING_REVIEW


def test_approve_already_decided_conflict(client):
    session_factory = _factory(client)
    task_id, review_id, payload_hash = _setup_review(session_factory)
    client.post(f"/api/v1/reviews/{review_id}/approve",
                json={"payload_hash": payload_hash},
                cookies=_cookies(Role.MANAGER), headers=_headers())
    resp = client.post(f"/api/v1/reviews/{review_id}/approve",
                       json={}, cookies=_cookies(Role.MANAGER), headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "REVIEW_INVALID_STATE"


# ---- POST reject ----


def test_reject_requires_reason(client):
    session_factory = _factory(client)
    task_id, review_id, _ = _setup_review(session_factory)
    # schema 拦截空字符串（422 VALIDATION_ERROR）
    resp = client.post(f"/api/v1/reviews/{review_id}/reject",
                       json={"rejection_reason": ""},
                       cookies=_cookies(Role.MANAGER), headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    # 领域拦截纯空白（400 REVIEW_REJECTION_REASON_REQUIRED）
    resp = client.post(f"/api/v1/reviews/{review_id}/reject",
                       json={"rejection_reason": "   "},
                       cookies=_cookies(Role.MANAGER), headers=_headers())
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "REVIEW_REJECTION_REASON_REQUIRED"


def test_reject_cancels_task(client):
    session_factory = _factory(client)
    task_id, review_id, _ = _setup_review(session_factory)
    resp = client.post(
        f"/api/v1/reviews/{review_id}/reject",
        json={"rejection_reason": "清单单价与客户价目不符"},
        cookies=_cookies(Role.MANAGER), headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "rejected"
    assert body["rejection_reason"] == "清单单价与客户价目不符"
    assert body["reviewed_by"] == MANAGER
    # 8.1：复核拒绝后终止
    task = SqlAlchemyTaskRepository(session_factory).get(task_id)
    assert task.status == TaskStatus.CANCELLED


# ---- 安全（2.7.3：manager + CSRF；10.5 拒绝路径） ----


def test_reviews_require_manager_role(client):
    session_factory = _factory(client)
    _, review_id, _ = _setup_review(session_factory)
    # 未登录 → 401
    for path in ("/api/v1/reviews", f"/api/v1/reviews/{review_id}"):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "UNAUTHENTICATED"
    # developer → 403 FORBIDDEN（只读与写端点全部拒绝）
    dev = _cookies(Role.DEVELOPER, account="dev01")
    assert client.get("/api/v1/reviews", cookies=dev).status_code == 403
    assert client.get(f"/api/v1/reviews/{review_id}", cookies=dev).status_code == 403
    resp = client.post(f"/api/v1/reviews/{review_id}/approve", json={},
                       cookies=dev, headers=_headers())
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "FORBIDDEN"


def test_write_apis_require_csrf(client):
    session_factory = _factory(client)
    _, review_id, _ = _setup_review(session_factory)
    for path, body in (
        (f"/api/v1/reviews/{review_id}/approve", {}),
        (f"/api/v1/reviews/{review_id}/reject", {"rejection_reason": "原因"}),
    ):
        resp = client.post(path, json=body,
                           cookies=_cookies(Role.MANAGER), headers=_headers(None))
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"
    # 恶意 Origin 同样拒绝（fail closed）
    resp = client.post(
        f"/api/v1/reviews/{review_id}/approve", json={},
        cookies=_cookies(Role.MANAGER),
        headers=_headers("https://evil.example.com"),
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_error_response_structure_stable(client):
    """2.7.3：稳定 error_code/message/details/trace_id 四件套。"""
    resp = client.get("/api/v1/reviews/rev-missing", cookies=_cookies(Role.MANAGER))
    body = resp.json()
    assert set(body) == {"error_code", "message", "details", "trace_id"}
    assert body["error_code"] == "REVIEW_NOT_FOUND"
