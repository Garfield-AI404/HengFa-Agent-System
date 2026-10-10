"""真实 PostgreSQL 集成测试（D8/RV-11 通过标准：不可覆盖可实测）。

默认跳过（未设置 TEST_AGENT_DATABASE_URL 或容器未启动）；在 D7 演示
容器（hengfa-pg-15432）或等效 PG 上执行：

    TEST_AGENT_DATABASE_URL=postgresql://agent:agent_demo_pw@127.0.0.1:15432/agent \
        uv run pytest tests/audit/test_pg_integration.py -q

验证点（任务书 D 类型证据）：
- DB 层 append-only 触发器拒绝 UPDATE/DELETE（直连 SQL 亦不可覆盖）；
- 裸 SQL INSERT 不填 source 得到 server_default 'agent'（RV-04 决策 c）；
- SqlAlchemy 仓储 attempt/result 回填同一行、attempt_no 唯一约束、
  result_status CheckConstraint；
- 完整链路（attempt 调用前 → result 调用后）trace/operation 一致；
- 落库明文不泄漏（脱敏在应用层完成，DB 里只有 *** 占位）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from administrative_agent.audit.service import AuditService
from administrative_agent.persistence.database import normalize_db_url
from administrative_agent.persistence.repositories import (
    SqlAlchemyAttemptRepository,
    SqlAlchemyAuditRepository,
)

TEST_URL = os.getenv("TEST_AGENT_DATABASE_URL")
if TEST_URL is None:
    pytest.skip(
        "TEST_AGENT_DATABASE_URL 未设置：真实 PG 集成测试跳过"
        "（演示容器证据见 scripts/d8_audit_demo.py）",
        allow_module_level=True,
    )

#: 每运行唯一后缀：audit_events/task_attempts 只追加（或 attempt 唯一约束），
#: 固定 id 二次运行会撞主键/唯一约束——集成测试行全部带本后缀。
import uuid as _uuid

_RUN_ID = _uuid.uuid4().hex[:8]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_schema(engine: Engine) -> None:
    """表缺失时应用 Alembic 迁移（到 head，含 92d64bb42eca 触发器）。"""
    if "audit_events" in inspect(engine).get_table_names():
        return
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(normalize_db_url(TEST_URL), pool_pre_ping=True)
    try:
        with eng.connect():
            pass
    except DBAPIError as exc:  # pragma: no cover - 容器未启动
        pytest.skip(f"PostgreSQL 不可达（{exc}）")
    _ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(scope="module")
def seed_tasks(engine: Engine) -> None:
    """audit_events/task_attempts 的 task_id 有 FK→tasks（12.1）：预置任务行。

    幂等（ON CONFLICT DO NOTHING），演示容器可重复执行。
    """
    stmt = text(
        "INSERT INTO tasks (task_id, thread_id, operation_id, actor_account,"
        " actor_role, duty_code, original_user_message, structured_intent,"
        " current_step, status) VALUES (:tid, 'thread-pg-d8', 'op-pg-seed-1',"
        " 'manager01', 'manager', 'R1', 'seed', '{}'::jsonb, 'created', 'pending')"
        " ON CONFLICT (task_id) DO NOTHING"
    )
    with engine.begin() as conn:
        for tid in (
            "task-pg-repo-00000001",
            "task-pg-chain-00000001",
            "task-pg-ck-00000001",
        ):
            conn.execute(stmt, {"tid": tid})


def _unique_audit_id(prefix: str) -> str:
    """每运行唯一（audit_events 只追加不可删，固定 id 二次运行会撞主键）。"""
    import uuid

    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _insert_event(engine: Engine, *, audit_id: str, include_source: bool = True) -> None:
    if include_source:
        stmt = text(
            "INSERT INTO audit_events (audit_id, trace_id, actor_account, actor_role,"
            " event_type, source, created_at) VALUES (:aid, :tid, 'u', 'r', 'attempt',"
            " 'agent', now())"
        )
    else:
        stmt = text(
            "INSERT INTO audit_events (audit_id, trace_id, actor_account, actor_role,"
            " event_type, created_at) VALUES (:aid, :tid, 'u', 'r', 'attempt', now())"
        )
    with engine.begin() as conn:
        conn.execute(stmt, {"aid": audit_id, "tid": f"trace-{audit_id}"})


# ---- 不可覆盖（11.1 通过标准） ----


def test_trigger_rejects_update(engine: Engine) -> None:
    audit_id = _unique_audit_id("aud-pg-upd-")
    _insert_event(engine, audit_id=audit_id)
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE audit_events SET source = 'hacker' WHERE audit_id = :aid"),
                {"aid": audit_id},
            )
    assert "append-only" in str(exc.value)


def test_trigger_rejects_delete(engine: Engine) -> None:
    audit_id = _unique_audit_id("aud-pg-del-")
    _insert_event(engine, audit_id=audit_id)
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM audit_events WHERE audit_id = :aid"),
                {"aid": audit_id},
            )
    assert "append-only" in str(exc.value)


def test_bare_insert_gets_source_default(engine: Engine) -> None:
    """RV-04 决策 c / O-1：无 DB server_default 时裸 INSERT 会被 NOT NULL 拒；
    92d64bb42eca 补默认后得到 'agent'。"""
    audit_id = _unique_audit_id("aud-pg-default-")
    _insert_event(engine, audit_id=audit_id, include_source=False)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT source FROM audit_events WHERE audit_id = :aid"),
            {"aid": audit_id},
        ).one()
    assert row.source == "agent"


# ---- 仓储层（SqlAlchemy 实现） ----


def test_repository_append_and_list_by(factory, seed_tasks) -> None:
    repo = SqlAlchemyAuditRepository(factory)
    audit_id = f"aud-pg-repo-{_RUN_ID}"
    event = repo.append(_event_record(audit_id, operation_id=f"op-pg-repo-{_RUN_ID}"))
    assert event.audit_id == audit_id
    assert event.created_at is not None  # DB server_default now()
    rows = repo.list_by(operation_id=f"op-pg-repo-{_RUN_ID}", limit=5)
    assert len(rows) == 1
    assert rows[0].trace_id == f"trace-pg-repo-{_RUN_ID}"


def test_attempt_result_backfill_same_row(factory, seed_tasks) -> None:
    repo = SqlAlchemyAttemptRepository(factory)
    attempt = repo.append_attempt(
        _attempt_record(
            f"atp-pg-backfill-{_RUN_ID}", attempt_no=1, operation_id=f"op-pg-bf-{_RUN_ID}"
        )
    )
    assert attempt.result_status is None
    result = repo.record_result(
        attempt.attempt_id,
        result_status="success",
        result_summary="SO-1 submitted",
    )
    # 回填同一行：attempt_id/attempt_no/created_at 不变，result 字段填充
    assert result.attempt_id == attempt.attempt_id
    assert result.attempt_no == 1
    assert result.created_at == attempt.created_at
    assert result.result_status == "success"
    assert result.result_at is not None
    assert result.result_at > result.created_at
    assert repo.get(attempt.attempt_id).result_summary == "SO-1 submitted"


def test_attempt_no_unique_constraint(factory, seed_tasks) -> None:
    repo = SqlAlchemyAttemptRepository(factory)
    op_id = f"op-pg-uq-{_RUN_ID}"
    repo.append_attempt(_attempt_record(f"atp-pg-unique-1-{_RUN_ID}", attempt_no=1, operation_id=op_id))
    with pytest.raises(IntegrityError):
        repo.append_attempt(_attempt_record(f"atp-pg-unique-2-{_RUN_ID}", attempt_no=1, operation_id=op_id))


def test_result_status_check_constraint(engine: Engine, seed_tasks) -> None:
    """绕过应用层校验的裸 SQL 也被 DB CheckConstraint 拒绝（双层防护）。"""
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO task_attempts (attempt_id, task_id, operation_id,"
                    " attempt_no, target_type, created_at, result_status)"
                    " VALUES ('atp-pg-ck-00000001', 'task-pg-ck-00000001',"
                    " 'op-pg-ck-000001', 1, 'order', now(), 'pending')"
                )
            )


# ---- 完整链路：attempt 调用前 → result 调用后，trace 一致 ----


def test_service_full_chain_on_real_db(factory, seed_tasks) -> None:
    audit_repo = SqlAlchemyAuditRepository(factory)
    attempt_repo = SqlAlchemyAttemptRepository(factory)
    service = AuditService(audit_repo, attempt_repo)
    trace_id = f"trace-pg-chain-{_RUN_ID}"
    operation_id = f"op-pg-chain-{_RUN_ID}"
    task_id = "task-pg-chain-00000001"

    attempt = service.record_attempt(
        task_id=task_id,
        operation_id=operation_id,
        trace_id=trace_id,
        target_type="order",
        target_id="SO-1001",
        actor_account="manager01",
        actor_role="manager",
        duty_code="R1",
        field_diffs={"status": ("draft", "submitted")},
        idempotency_key="idem-pg-1",
    )
    result = service.record_result(
        attempt_id=attempt.attempt_id,
        result_status="success",
        trace_id=trace_id,
        actor_account="manager01",
        actor_role="manager",
        result_summary="SO-1001 submitted",
    )
    assert result.result_status == "success"

    # 审计事件两条（attempt/result），trace/operation 与任务一致（L1179）
    events = audit_repo.list_by(operation_id=operation_id)
    assert {e.event_type for e in events} == {"attempt", "result"}
    assert all(e.trace_id == trace_id for e in events)
    assert all(e.task_id == task_id for e in events)
    assert all(e.actor_account == "manager01" for e in events)
    result_event = next(e for e in events if e.event_type == "result")
    assert result_event.result_code == "success"

    # attempt 记录结果回填
    attempts = attempt_repo.list_by_operation(operation_id)
    assert len(attempts) == 1
    assert attempts[0].result_status == "success"
    assert attempts[0].idempotency_key == "idem-pg-1"


def test_redaction_at_rest_never_leaks_plaintext(factory, engine: Engine) -> None:
    """脱敏在应用层完成：明文 secret 不得出现在 DB 里（14.4 凭据纪律）。"""
    audit_repo = SqlAlchemyAuditRepository(factory)
    attempt_repo = SqlAlchemyAttemptRepository(factory)
    service = AuditService(audit_repo, attempt_repo)
    secret = "sk-live-topsecret-0000"
    event = service.record_event(
        event_type="attempt",
        trace_id="trace-pg-redact000001",
        actor_account="u",
        actor_role="r",
        operation_id="op-pg-redact000001",
        structured_payload={"api_key": secret, "attempt_no": 1},
    )
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT structured_payload::text FROM audit_events"
                " WHERE audit_id = :aid"
            ),
            {"aid": event.audit_id},
        ).one()
    assert secret not in row[0]
    assert "***" in row[0]


# ---- 记录构造 helper ----


def _event_record(audit_id: str, *, operation_id: str | None = None):
    from administrative_agent.audit.service import AuditEventRecord

    return AuditEventRecord(
        audit_id=audit_id,
        task_id="task-pg-repo-00000001",
        operation_id=operation_id or f"op-pg-repo-{_RUN_ID}",
        trace_id=f"trace-pg-repo-{_RUN_ID}",
        actor_account="u",
        actor_role="r",
        event_type="attempt",
        target_type="order",
        target_id="SO-1",
        source="agent",
    )


def _attempt_record(attempt_id: str, *, attempt_no: int, operation_id: str):
    from administrative_agent.audit.service import TaskAttemptRecord

    return TaskAttemptRecord(
        attempt_id=attempt_id,
        task_id="task-pg-repo-00000001",
        operation_id=operation_id,
        attempt_no=attempt_no,
        target_type="order",
        target_id="SO-1",
        idempotency_key="idem-pg",
    )
