"""交叉验证：审计事件不随 checkpoint 清理（D8/RV-11 任务书项 ⑧，8.5/L996）。

checkpoint 清理只操作 langgraph checkpoint 表（saver.delete_thread）；
audit_events / task_attempts 属于 Agent 业务表，不随清理删除——
清理执行前后审计记录与 attempt 记录必须原样保留。
"""

from __future__ import annotations

from datetime import timedelta

from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.tasks.machine import TaskStatus
from tests.audit.conftest import (
    OP_ID,
    TASK_ID,
    TRACE_ID,
    InMemoryAuditRepository,
)
from tests.checkpoint.conftest import (
    NOW as CP_NOW,
    FakeSaver,
    make_task,
)
from tests.tasks.conftest import InMemoryTaskRepository


def test_cleanup_deletes_expired_checkpoint_but_keeps_audit_records() -> None:
    saver = FakeSaver()
    repo = InMemoryTaskRepository()
    task = make_task(
        repo,
        task_id=TASK_ID,
        thread_id="thread-audit-1",
        status=TaskStatus.SUCCEEDED,
        completed_at=CP_NOW - timedelta(days=31),  # 超过 30 天保留期
    )
    # 线程内仅一条快照（终态 + 到期 → 整线程可删）
    saver.put(
        {"configurable": {"thread_id": "thread-audit-1", "task_id": TASK_ID}},
        {"v": 1, "ts": (CP_NOW - timedelta(days=31)).isoformat(), "id": "ckpt-expired",
         "channel_values": {"current_step": "done"}, "pending_sends": []},
        {"source": "loop", "task_id": TASK_ID},
        None,
    )
    # 同一任务的审计记录（模拟 10.4 顺序 7/11 已落库）
    audit_repo = InMemoryAuditRepository()
    audit_repo.append(_fake_event())
    saved_event = audit_repo.list_by(trace_id=TRACE_ID)
    assert len(saved_event) == 1

    # 执行清理：到期终态快照应被删除
    service = CheckpointService(saver, repo)
    result = service.cleanup(30, now=CP_NOW)
    assert result.deleted_snapshot_count == 1
    assert "thread-audit-1" in saver.deleted_thread_ids

    # 审计/attempt 记录原样保留（不随 checkpoint 清理）
    assert audit_repo.list_by(trace_id=TRACE_ID) == saved_event
    assert audit_repo._events == saved_event
    # task 记录本身也不受清理影响（只删 checkpoint 表）
    assert repo.get(TASK_ID) is not None


def _fake_event():
    """构造一条审计事件（11.1 最小字段）。"""
    from administrative_agent.audit.service import AuditEventRecord

    return AuditEventRecord(
        audit_id="aud-crosstest0001",
        task_id=TASK_ID,
        operation_id=OP_ID,
        trace_id=TRACE_ID,
        actor_account="manager01",
        actor_role="manager",
        event_type="attempt",
        target_type="order",
        target_id="SO-1001",
        source="agent",
    )


def test_cleanup_protected_thread_also_keeps_audit_records() -> None:
    """受保护线程（waiting/running）快照不删，审计记录自然保留——对照验证。"""
    saver = FakeSaver()
    repo = InMemoryTaskRepository()
    make_task(repo, task_id="task-x", thread_id="thread-x", status=TaskStatus.WAITING_CONFIRMATION)
    saver.put(
        {"configurable": {"thread_id": "thread-x", "task_id": "task-x"}},
        {"v": 1, "ts": CP_NOW.isoformat(), "id": "ck1",
         "channel_values": {"current_step": "placeholder_done"}, "pending_sends": []},
        {"source": "loop", "task_id": "task-x"},
        None,
    )
    # 写入审计记录
    audit_repo = InMemoryAuditRepository()
    audit_repo.append(_fake_event())
    events_before = audit_repo.list_by(trace_id=TRACE_ID)

    service = CheckpointService(saver, repo)
    result = service.cleanup(30, now=CP_NOW)
    assert result.protected_snapshot_count >= 1
    assert audit_repo.list_by(trace_id=TRACE_ID) == events_before
