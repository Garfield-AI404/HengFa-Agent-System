"""tasks 服务测试共享夹具：in-memory 仓储 + 锁 + 服务。

说明：Agent 表用 PostgreSQL JSONB（12.1），本机无 PG 测试容器；
状态机/锁/服务逻辑与持久化解耦（TaskRepository Protocol），
本测试集用 InMemoryTaskRepository 覆盖全部 D6 语义。
SQLAlchemy 实现（persistence/repositories.py）的结构正确性由 D3 迁移
实测保证（models.py 未改动）。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.tasks.records import NewTaskSpec, TaskRecord
from administrative_agent.tasks.service import TaskService


class InMemoryTaskRepository:
    """TaskRepository 协议的内存实现（确定性、可并发断言）。"""

    def __init__(self) -> None:
        self._store: dict[str, TaskRecord] = {}

    def create(self, record: TaskRecord) -> TaskRecord:
        snapshot = copy.deepcopy(record)
        snapshot.created_at = snapshot.updated_at = datetime.now(timezone.utc)
        self._store[record.task_id] = snapshot
        return copy.deepcopy(snapshot)

    def get(self, task_id: str) -> TaskRecord | None:
        record = self._store.get(task_id)
        return copy.deepcopy(record) if record else None

    def update(self, task_id: str, **fields) -> TaskRecord:
        record = self._store[task_id]
        updated = copy.deepcopy(record)
        for key, value in fields.items():
            if not hasattr(updated, key):
                raise ValueError(f"unknown task field: {key}")
            setattr(updated, key, value)
        updated.updated_at = datetime.now(timezone.utc)
        self._store[task_id] = updated
        return copy.deepcopy(updated)

    def list_by_thread(self, thread_id: str, limit: int | None = None) -> list[TaskRecord]:
        rows = [r for r in self._store.values() if r.thread_id == thread_id]
        rows.sort(key=lambda r: (r.created_at or datetime.min).timestamp(), reverse=True)
        return copy.deepcopy(rows[:limit] if limit else rows)

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        rows = [
            r for r in self._store.values()
            if status is None or r.status.value == status
        ]
        rows.sort(key=lambda r: (r.created_at or datetime.min).timestamp(), reverse=True)
        return copy.deepcopy(rows[offset:offset + limit]), len(rows)


@pytest.fixture
def repo() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def write_lock() -> WriteTaskLock:
    return WriteTaskLock()


@pytest.fixture
def service(repo: InMemoryTaskRepository, write_lock: WriteTaskLock) -> TaskService:
    return TaskService(repo, write_lock)


def write_spec(**overrides) -> NewTaskSpec:
    """写任务默认规格（可覆盖为只读/不同线程）。"""
    base = dict(
        thread_id="thread-mgr-1",
        actor_account="manager01",
        actor_role="manager",
        duty_code="R1",
        original_user_message="记录今天的考勤",
        structured_intent={"duty": "R1", "date": "2026-08-21"},
        is_write=True,
    )
    base.update(overrides)
    return NewTaskSpec(**base)
