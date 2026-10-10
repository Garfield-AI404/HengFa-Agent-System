"""CheckpointService 恢复/隔离/回写/清理测试（D7/RV-10）。

对应规格：8.5（thread_id+task_id 关联、恢复优先结构化字段、
保护状态、终态保留 30 天可配置、审计不随清理）、
8.3 第 2/4 条（显式绑定、不重新解析 messages）、
RV-10 通过标准（跨任务隔离：旧任务信息不注入新任务）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.tasks.machine import TaskStatus

from tests.checkpoint.conftest import (
    NOW,
    make_snapshot,
    make_task,
)


# ---- 恢复：锚点优先 ----

def test_load_uses_anchored_checkpoint_id(cp_service, saver, repo) -> None:
    task = make_task(repo, task_id="task-0001", status=TaskStatus.RUNNING)
    snapshot = make_snapshot(
        thread_id=task.thread_id,
        task_id=task.task_id,
        ckpt_id="ckp-latest",
        ts=NOW,
        values={"current_step": "placeholder_done", "remaining_steps": 24,
                "structured_intent": {"duty": "R1"}},
    )
    saver.put(snapshot.config, snapshot.checkpoint, snapshot.metadata, {})
    repo.update(task.task_id, checkpoint_id="ckp-latest")

    state = cp_service.load_for_task(task.thread_id, task.task_id)
    assert state is not None
    assert state["current_step"] == "placeholder_done"
    assert state["remaining_steps"] == 24
    assert state["structured_intent"] == {"duty": "R1"}


def test_load_anchor_stale_falls_back_to_thread_scan(cp_service, saver, repo) -> None:
    """tasks.checkpoint_id 指向已失效快照（如被清理）：回退线程扫描。"""
    task = make_task(repo, task_id="task-0002", status=TaskStatus.RUNNING)
    repo.update(task.task_id, checkpoint_id="ckp-gone")
    snapshot = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id,
        ckpt_id="ckp-alive", ts=NOW, values={"current_step": "placeholder_done"},
    )
    saver.put(snapshot.config, snapshot.checkpoint, snapshot.metadata, {})

    state = cp_service.load_for_task(task.thread_id, task.task_id)
    assert state is not None
    assert state["current_step"] == "placeholder_done"


def test_load_without_repo_uses_thread_scan(cp_service_no_repo, saver) -> None:
    """无 task_repository 时按线程扫描恢复（checkpoint_id 锚点不可用）。"""
    snapshot = make_snapshot(
        thread_id="thread-mgr-1", task_id="task-0003",
        ckpt_id="ckp-1", ts=NOW, values={"current_step": "placeholder_done"},
    )
    saver.put(snapshot.config, snapshot.checkpoint, snapshot.metadata, {})

    state = cp_service_no_repo.load_for_task("thread-mgr-1", "task-0003")
    assert state is not None
    assert state["current_step"] == "placeholder_done"


# ---- 恢复：跨任务隔离（RV-10 通过标准） ----

def test_load_rejects_snapshot_of_other_task(cp_service, saver, repo) -> None:
    """同线程仅有旧任务快照：load 新任务必须返回 None，不注入旧任务信息。"""
    make_task(repo, task_id="task-old", status=TaskStatus.SUCCEEDED,
              completed_at=NOW - timedelta(days=1))
    make_task(repo, task_id="task-new", status=TaskStatus.RUNNING)
    old = make_snapshot(
        thread_id="thread-mgr-1", task_id="task-old",
        ckpt_id="ckp-old", ts=NOW - timedelta(minutes=5),
        values={"current_step": "placeholder_done", "structured_intent": {"old": True}},
    )
    saver.put(old.config, old.checkpoint, old.metadata, {})

    state = cp_service.load_for_task("thread-mgr-1", "task-new")
    assert state is None


def test_load_picks_latest_snapshot_of_own_task(cp_service, saver, repo) -> None:
    """同任务多次快照：取最新（8.5 恢复语义）。"""
    task = make_task(repo, task_id="task-0004", status=TaskStatus.RUNNING)
    for ckpt_id, ts, step in [
        ("ckp-1", NOW - timedelta(minutes=2), "placeholder_done"),
        ("ckp-2", NOW, "exited"),
    ]:
        snap = make_snapshot(
            thread_id=task.thread_id, task_id=task.task_id,
            ckpt_id=ckpt_id, ts=ts, values={"current_step": step},
        )
        saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    state = cp_service.load_for_task(task.thread_id, task.task_id)
    assert state["current_step"] == "exited"


def test_load_rejects_thread_mismatch(cp_service, saver, repo) -> None:
    """task 属于其他线程：显式绑定校验拒绝（8.3 第 2 条防跨会话操作）。"""
    task = make_task(repo, task_id="task-0005", thread_id="thread-other",
                     status=TaskStatus.RUNNING)
    with pytest.raises(ValueError, match="belongs to thread"):
        cp_service.load_for_task("thread-mgr-1", task.task_id)


def test_load_not_found_returns_none(cp_service, saver, repo) -> None:
    make_task(repo, task_id="task-0006", status=TaskStatus.RUNNING)
    state = cp_service.load_for_task("thread-mgr-1", "task-0006")
    assert state is None


def test_load_returns_structured_fields_for_business(cp_service, saver, repo) -> None:
    """恢复返回 8.4 结构化字段快照（调用方优先用结构化业务字段，
    不重新解析 messages 生成业务参数——8.3 第 4 条）。"""
    task = make_task(repo, task_id="task-0007", status=TaskStatus.RUNNING)
    snapshot = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1", ts=NOW,
        values={
            "messages": [{"role": "user", "content": "旧消息不重新解析"}],
            "task_id": task.task_id,
            "operation_id": "op-0007",
            "current_step": "placeholder_done",
            "structured_intent": {"duty": "R1", "date": "2026-08-21"},
            "pending_order": {"order_no": "SO-1"},
            "remaining_steps": 23,
        },
    )
    saver.put(snapshot.config, snapshot.checkpoint, snapshot.metadata, {})
    repo.update(task.task_id, checkpoint_id="ckp-1")

    state = cp_service.load_for_task(task.thread_id, task.task_id)
    # 结构化业务字段原样返回（无需解析 messages）
    assert state["structured_intent"] == {"duty": "R1", "date": "2026-08-21"}
    assert state["pending_order"] == {"order_no": "SO-1"}
    assert state["operation_id"] == "op-0007"


# ---- 写入集成：graph config 与 checkpoint_id 回写 ----

def test_new_graph_config_binds_thread_and_task(saver) -> None:
    svc = CheckpointService(saver)
    cfg = svc.new_graph_config("thread-1", "task-9")
    assert cfg == {"configurable": {"thread_id": "thread-1", "task_id": "task-9"}}


def test_latest_checkpoint_id_picks_newest(cp_service, saver, repo) -> None:
    task = make_task(repo, task_id="task-0008", status=TaskStatus.RUNNING)
    for ckpt_id, ts in [("ckp-1", NOW - timedelta(minutes=1)), ("ckp-2", NOW)]:
        snap = make_snapshot(
            thread_id=task.thread_id, task_id=task.task_id, ckpt_id=ckpt_id, ts=ts,
        )
        saver.put(snap.config, snap.checkpoint, snap.metadata, {})
    assert cp_service.latest_checkpoint_id(task.thread_id, task.task_id) == "ckp-2"


def test_latest_checkpoint_id_ignores_other_task(cp_service, saver, repo) -> None:
    task = make_task(repo, task_id="task-0009", status=TaskStatus.RUNNING)
    other = make_snapshot(
        thread_id=task.thread_id, task_id="task-other",
        ckpt_id="ckp-other", ts=NOW,
    )
    saver.put(other.config, other.checkpoint, other.metadata, {})
    assert cp_service.latest_checkpoint_id(task.thread_id, task.task_id) is None


def test_attach_writes_task_checkpoint_id(cp_service, saver, repo) -> None:
    task = make_task(repo, task_id="task-0010", status=TaskStatus.RUNNING)
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1", ts=NOW,
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    assert cp_service.attach_checkpoint_id(task.thread_id, task.task_id) == "ckp-1"
    assert repo.get(task.task_id).checkpoint_id == "ckp-1"


def test_attach_without_repo_returns_latest(cp_service_no_repo, saver) -> None:
    snap = make_snapshot(
        thread_id="thread-mgr-1", task_id="task-0011", ckpt_id="ckp-1", ts=NOW,
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})
    assert cp_service_no_repo.attach_checkpoint_id("thread-mgr-1", "task-0011") == "ckp-1"


def test_setup_calls_saver_setup(cp_service, saver) -> None:
    cp_service.setup()
    assert saver.setup_calls == 1


# ---- 清理：8.5 保留策略 ----

def test_cleanup_keeps_running_task(cp_service, saver, repo) -> None:
    task = make_task(repo, task_id="task-c1", status=TaskStatus.RUNNING)
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1",
        ts=NOW - timedelta(days=60),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 1
    assert result.deleted_threads == []
    assert result.protected_snapshot_count == 1


@pytest.mark.parametrize("status", [
    TaskStatus.WAITING_CONFIRMATION,
    TaskStatus.WAITING_REVIEW,
])
def test_cleanup_keeps_waiting_states(cp_service, saver, repo, status) -> None:
    task = make_task(repo, task_id=f"task-{status.value}", status=status)
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1",
        ts=NOW - timedelta(days=90),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 1
    assert result.deleted_threads == []


@pytest.mark.parametrize("status, completed_delta", [
    (TaskStatus.SUCCEEDED, timedelta(days=31)),
    (TaskStatus.FAILED, timedelta(days=40)),
    (TaskStatus.CANCELLED, timedelta(days=30, seconds=1)),
])
def test_cleanup_deletes_expired_terminal(cp_service, saver, repo, status, completed_delta) -> None:
    task = make_task(
        repo, task_id=f"task-{status.value}",
        status=status, completed_at=NOW - completed_delta,
    )
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1",
        ts=NOW - timedelta(days=31),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 0
    assert result.deleted_threads == [task.thread_id]
    assert result.deleted_snapshot_count == 1


def test_cleanup_keeps_recent_terminal(cp_service, saver, repo) -> None:
    task = make_task(
        repo, task_id="task-c2", status=TaskStatus.SUCCEEDED,
        completed_at=NOW - timedelta(days=10),
    )
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1", ts=NOW,
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 1
    assert result.deleted_threads == []
    assert result.protected_snapshot_count == 1


def test_cleanup_retention_boundary(cp_service, saver, repo) -> None:
    """恰在 30 天整 → 到期删除；差 1 秒 → 保留（不同线程独立判定）。"""
    expired = make_task(
        repo, task_id="task-c3", status=TaskStatus.SUCCEEDED,
        completed_at=NOW - timedelta(days=30),
    )
    kept = make_task(
        repo, task_id="task-c4", thread_id="thread-kept",
        status=TaskStatus.SUCCEEDED,
        completed_at=NOW - timedelta(days=30) + timedelta(seconds=1),
    )
    for task in (expired, kept):
        snap = make_snapshot(
            thread_id=task.thread_id, task_id=task.task_id,
            ckpt_id=f"ckp-{task.task_id}", ts=NOW - timedelta(days=30),
        )
        saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 1
    assert result.deleted_threads == [expired.thread_id]


def test_cleanup_keeps_pending_task(cp_service, saver, repo) -> None:
    """排队中（pending，未终态）任务不清理。"""
    task = make_task(repo, task_id="task-c5", status=TaskStatus.PENDING)
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1",
        ts=NOW - timedelta(days=60),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 1
    assert result.deleted_threads == []


def test_cleanup_mixed_thread_kept_whole(cp_service, saver, repo) -> None:
    """线程内任一快照受保护：整线程保留（删除粒度线程级，只走公开 API）。"""
    running = make_task(repo, task_id="task-c6", status=TaskStatus.RUNNING)
    expired = make_task(
        repo, task_id="task-c7", thread_id=running.thread_id,
        status=TaskStatus.SUCCEEDED, completed_at=NOW - timedelta(days=60),
    )
    for task in (running, expired):
        snap = make_snapshot(
            thread_id=task.thread_id, task_id=task.task_id,
            ckpt_id=f"ckp-{task.task_id}", ts=NOW - timedelta(days=60),
        )
        saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count(running.thread_id) == 2
    assert result.deleted_threads == []
    assert result.protected_threads == [running.thread_id]


def test_cleanup_orphan_snapshot_by_ts(cp_service, saver, repo) -> None:
    """metadata 无 task_id 的孤儿快照：按快照时间兜底（超期删/未超期留）。"""
    for thread_id, ckpt_id, ts in [
        ("thread-orphan-old", "ckp-old", NOW - timedelta(days=45)),
        ("thread-orphan-fresh", "ckp-fresh", NOW - timedelta(days=5)),
    ]:
        snap = make_snapshot(
            thread_id=thread_id, task_id="task-x", ckpt_id=ckpt_id,
            ts=ts, values={"current_step": "placeholder_done"},
        )
        meta = dict(snap.metadata)
        meta.pop("task_id")
        snap = snap._replace(metadata=meta)
        saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count("thread-orphan-old") == 0
    assert saver.snapshot_count("thread-orphan-fresh") == 1
    assert result.orphan_deleted_snapshot_count == 1


def test_cleanup_orphan_task_record_by_ts(cp_service, saver, repo) -> None:
    """task 记录已不存在的快照：按快照时间兜底删除。"""
    snap = make_snapshot(
        thread_id="thread-gone", task_id="task-gone", ckpt_id="ckp-1",
        ts=NOW - timedelta(days=45),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    result = cp_service.cleanup(30, now=NOW)
    assert saver.snapshot_count() == 0
    assert result.orphan_deleted_snapshot_count == 1


def test_cleanup_keeps_task_records_untouched(cp_service, saver, repo) -> None:
    """8.5：审计事件、复核记录、映射版本不随 checkpoint 清理——
    cleanup 只调依赖的 delete_thread，task 记录保持原样。"""
    task = make_task(
        repo, task_id="task-c8", status=TaskStatus.SUCCEEDED,
        completed_at=NOW - timedelta(days=60),
    )
    snap = make_snapshot(
        thread_id=task.thread_id, task_id=task.task_id, ckpt_id="ckp-1",
        ts=NOW - timedelta(days=60),
    )
    saver.put(snap.config, snap.checkpoint, snap.metadata, {})

    cp_service.cleanup(30, now=NOW)
    # task 记录仍在（审计/复核/映射表由 D8+ 管理，本清理不动 Agent 表）
    assert repo.get(task.task_id) is not None
    assert saver.deleted_thread_ids == [task.thread_id]
    # 清理路径只有 delete_thread 一种删除手段
    assert saver.snapshot_count() == 0


def test_cleanup_without_repo_conservative(cp_service_no_repo, saver) -> None:
    """无 repo：带 task_id 的快照无法查证任务状态 → 保守保留；
    仅无 task_id 的孤儿按快照时间兜底。"""
    with_task = make_snapshot(
        thread_id="thread-a", task_id="task-any", ckpt_id="ckp-1",
        ts=NOW - timedelta(days=90),
    )
    saver.put(with_task.config, with_task.checkpoint, with_task.metadata, {})
    orphan = make_snapshot(
        thread_id="thread-b", task_id="task-x", ckpt_id="ckp-2",
        ts=NOW - timedelta(days=90),
    )
    orphan_meta = dict(orphan.metadata)
    orphan_meta.pop("task_id")
    orphan = orphan._replace(metadata=orphan_meta)
    saver.put(orphan.config, orphan.checkpoint, orphan.metadata, {})

    result = cp_service_no_repo.cleanup(30, now=NOW)
    assert saver.snapshot_count("thread-a") == 1
    assert saver.snapshot_count("thread-b") == 0
    assert result.orphan_deleted_snapshot_count == 1


def test_cleanup_negative_retention_raises(cp_service) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        cp_service.cleanup(-1, now=NOW)
