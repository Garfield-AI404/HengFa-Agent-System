"""checkpoint 服务测试共享夹具（D7/RV-10）。

FakeSaver 模拟 langgraph-checkpoint-postgres 的 PostgresSaver 行为
（get_tuple/list/delete_thread/setup/put + metadata 中自动带 task_id），
使清理/隔离/回写逻辑可在无 PG 容器下确定性测试。
真实 PostgreSQL 运行证据由 scripts/d7_checkpoint_demo.py 提供
（任务书 D 类型演示：docker compose 起真实 PG + 进程重启恢复）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
from langgraph.checkpoint.base import CheckpointTuple

from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.tasks.records import TaskRecord
from administrative_agent.tasks.machine import TaskStatus

from tests.tasks.conftest import InMemoryTaskRepository

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def iso(ts: datetime) -> str:
    return ts.isoformat()


def make_snapshot(
    *,
    thread_id: str,
    task_id: str,
    ckpt_id: str,
    ts: datetime,
    values: dict[str, Any] | None = None,
) -> CheckpointTuple:
    """构造一条 checkpoint 快照（metadata 模拟 LangGraph 自动带 task_id）。"""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_id": ckpt_id}}
    return CheckpointTuple(
        checkpoint={
            "v": 1,
            "ts": iso(ts),
            "id": ckpt_id,
            "channel_values": values or {"current_step": "placeholder_done"},
            "pending_sends": [],
        },
        config=config,
        metadata={"source": "loop", "step": 1, "task_id": task_id},
        parent_config=None,
        pending_writes=[],
    )


class FakeSaver:
    """PostgresSaver 行为的内存模拟（记录调用供清理语义断言）。"""

    def __init__(self) -> None:
        self._by_thread: dict[str, list[CheckpointTuple]] = {}
        self.setup_calls = 0
        self.deleted_thread_ids: list[str] = []

    # ---- 模拟的 PostgresSaver 接口 ----

    def setup(self) -> None:
        self.setup_calls += 1

    def put(self, config, checkpoint, metadata, new_versions) -> None:
        cfg = config["configurable"]
        thread_id = cfg["thread_id"]
        meta = dict(metadata)
        if "task_id" in cfg and "task_id" not in meta:
            meta["task_id"] = cfg["task_id"]
        tup = CheckpointTuple(
            checkpoint=dict(checkpoint),
            config=config,
            metadata=meta,
            parent_config=None,
            pending_writes=[],
        )
        self._by_thread.setdefault(thread_id, []).append(tup)

    def get_tuple(self, config):
        cfg = config["configurable"]
        rows = self._by_thread.get(cfg["thread_id"], [])
        if not rows:
            return None
        ckpt_id = cfg.get("checkpoint_id")
        if ckpt_id:
            for tup in rows:
                if tup.checkpoint["id"] == ckpt_id:
                    return tup
            return None
        return max(rows, key=lambda t: _snapshot_ts(t))

    def list(self, config=None, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        if config is None:
            rows = [t for bucket in self._by_thread.values() for t in bucket]
        else:
            rows = list(self._by_thread.get(config["configurable"]["thread_id"], []))
        rows = sorted(rows, key=lambda t: _snapshot_ts(t))
        if before is not None:
            before_ts = _snapshot_ts(before)
            rows = [t for t in rows if _snapshot_ts(t) < before_ts]
        if filter:
            rows = [
                t for t in rows
                if all(t.metadata.get(k) == v for k, v in filter.items())
            ]
        if limit is not None:
            rows = rows[:limit]
        return iter(rows)

    def delete_thread(self, thread_id: str) -> None:
        self.deleted_thread_ids.append(thread_id)
        self._by_thread.pop(thread_id, None)

    # ---- 测试辅助 ----

    def snapshot_count(self, thread_id: str | None = None) -> int:
        if thread_id is None:
            return sum(len(v) for v in self._by_thread.values())
        return len(self._by_thread.get(thread_id, []))


def _snapshot_ts(tup: CheckpointTuple) -> datetime:
    ts = tup.checkpoint["ts"]
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_task(
    repo: InMemoryTaskRepository,
    *,
    task_id: str,
    thread_id: str = "thread-mgr-1",
    status: TaskStatus = TaskStatus.RUNNING,
    completed_at: datetime | None = None,
    checkpoint_id: str | None = None,
) -> TaskRecord:
    """直接构造任务记录（跳过状态机，专注 checkpoint 语义测试）。"""
    record = TaskRecord(
        task_id=task_id,
        thread_id=thread_id,
        operation_id=f"op-{task_id[-4:]}",
        actor_account="manager01",
        actor_role="manager",
        duty_code="R1",
        original_user_message="记录今天的考勤",
        structured_intent={"duty": "R1", "date": "2026-08-21"},
        status=status,
        checkpoint_id=checkpoint_id,
    )
    record.created_at = record.updated_at = NOW
    record.completed_at = completed_at
    return repo.create(record)


@pytest.fixture
def saver() -> FakeSaver:
    return FakeSaver()


@pytest.fixture
def repo() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


@pytest.fixture
def cp_service(saver: FakeSaver, repo: InMemoryTaskRepository) -> CheckpointService:
    return CheckpointService(saver, repo)


@pytest.fixture
def cp_service_no_repo(saver: FakeSaver) -> CheckpointService:
    return CheckpointService(saver)
