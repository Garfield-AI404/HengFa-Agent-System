"""audit 服务测试共享夹具（D8/RV-11）：in-memory 仓储 + 服务。

与 tests/tasks/ 同模式：逻辑与持久化解耦（协议注入），SQLAlchemy 实现
与 DB 层 append-only 触发器由 tests/audit/test_pg_integration.py 在真实
PostgreSQL 上验证（演示容器）。
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from administrative_agent.audit.service import (
    AuditEventRecord,
    AuditService,
    TaskAttemptRecord,
)

NOW = datetime(2026, 8, 22, 10, 0, 0, tzinfo=timezone.utc)


class InMemoryAuditRepository:
    """AuditRepository 协议的内存实现（记录追加顺序供时序断言）。"""

    def __init__(self) -> None:
        self._events: list[AuditEventRecord] = []
        self.fail_next = False  # 置 True 模拟 DB 不可用（审计失败路径）
        self.clock = [NOW]

    def append(self, event: AuditEventRecord) -> AuditEventRecord:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("audit db unavailable")
        saved = dataclasses.replace(event, created_at=self.clock[-1])
        self.clock.append(self.clock[-1] + timedelta(seconds=1))
        self._events.append(saved)
        return saved

    def list_by(
        self,
        *,
        task_id: str | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEventRecord]:
        rows = [
            e for e in self._events
            if (task_id is None or e.task_id == task_id)
            and (operation_id is None or e.operation_id == operation_id)
            and (trace_id is None or e.trace_id == trace_id)
            and (event_type is None or e.event_type == event_type)
        ]
        rows.sort(key=lambda e: e.created_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[:limit] if limit else rows

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        task_id: str | None = None,
        operation_id: str | None = None,
        event_type: str | None = None,
    ) -> tuple[list[AuditEventRecord], int]:
        """分页查询（created_at 倒序——与 SqlAlchemy 实现一致）。"""
        rows = [
            e for e in self._events
            if (task_id is None or e.task_id == task_id)
            and (operation_id is None or e.operation_id == operation_id)
            and (event_type is None or e.event_type == event_type)
        ]
        rows.sort(key=lambda e: e.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[offset:offset + limit], len(rows)


class InMemoryAttemptRepository:
    """AttemptRepository 协议的内存实现（result 回填同一记录）。"""

    def __init__(self) -> None:
        self._attempts: dict[str, TaskAttemptRecord] = {}
        self.fail_next = False
        self.clock = [NOW]

    def append_attempt(self, record: TaskAttemptRecord) -> TaskAttemptRecord:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("attempt db unavailable")
        saved = dataclasses.replace(record, created_at=self.clock[-1])
        self.clock.append(self.clock[-1] + timedelta(seconds=1))
        self._attempts[record.attempt_id] = saved
        return saved

    def record_result(
        self,
        attempt_id: str,
        *,
        result_status: str,
        error_code: str | None = None,
        requires_operator_action: bool | None = None,
        result_summary: str | None = None,
    ) -> TaskAttemptRecord:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("attempt db unavailable")
        record = self._attempts[attempt_id]
        saved = dataclasses.replace(
            record,
            result_status=result_status,
            error_code=error_code,
            requires_operator_action=requires_operator_action,
            result_summary=result_summary,
            result_at=self.clock[-1],
        )
        self._attempts[attempt_id] = saved
        return saved

    def list_by_operation(self, operation_id: str) -> list[TaskAttemptRecord]:
        return [
            copy.deepcopy(a) for a in self._attempts.values()
            if a.operation_id == operation_id
        ]

    def get(self, attempt_id: str) -> TaskAttemptRecord | None:
        record = self._attempts.get(attempt_id)
        return copy.deepcopy(record) if record else None


@pytest.fixture
def audit_repo() -> InMemoryAuditRepository:
    return InMemoryAuditRepository()


@pytest.fixture
def attempt_repo() -> InMemoryAttemptRepository:
    return InMemoryAttemptRepository()


@pytest.fixture
def audit_service(
    audit_repo: InMemoryAuditRepository,
    attempt_repo: InMemoryAttemptRepository,
) -> AuditService:
    return AuditService(audit_repo, attempt_repo)


#: 演示用共享身份/关联键（与 scripts/d8_audit_demo.py 语义一致）
TRACE_ID = "trace-d8demo0000000001"
OP_ID = "op-d8demo0000000001"
TASK_ID = "task-d8demo00000001"


def attempt_kwargs(**overrides) -> dict:
    base = dict(
        task_id=TASK_ID,
        operation_id=OP_ID,
        trace_id=TRACE_ID,
        target_type="order",
        actor_account="manager01",
        actor_role="manager",
        duty_code="R1",
        target_id="SO-1001",
        field_diffs={"status": ("draft", "submitted")},
        idempotency_key="idem-1",
    )
    base.update(overrides)
    return base
