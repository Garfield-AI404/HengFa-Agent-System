"""8.3/13.3 串行写锁测试：会话级串行、task 级互斥、FIFO 排队、线程安全。"""

from __future__ import annotations

import threading

from administrative_agent.tasks.lock import WriteTaskLock


def test_first_acquire_succeeds() -> None:
    lock = WriteTaskLock()
    result = lock.acquire("thread-1", "task-a")
    assert result.acquired is True
    assert result.reason == "acquired"
    assert lock.holder("thread-1") == "task-a"
    assert lock.is_running("task-a")


def test_second_write_same_thread_queues_fifo() -> None:
    """8.3 第 3 条：新写任务排队到前一个结束/取消；FIFO 顺序。"""
    lock = WriteTaskLock()
    assert lock.acquire("thread-1", "task-a").acquired
    r1 = lock.acquire("thread-1", "task-b")
    r2 = lock.acquire("thread-1", "task-c")
    assert r1.acquired is False and r1.queued is True and r1.reason == "queued"
    assert r2.acquired is False and r2.queued is True
    # FIFO：release task-a 后先晋升 task-b，再 release 后 task-c
    assert lock.release("thread-1", "task-a") == "task-b"
    assert lock.release("thread-1", "task-b") == "task-c"
    assert lock.release("thread-1", "task-c") is None
    assert lock.holder("thread-1") is None


def test_acquire_same_task_rejected_across_threads() -> None:
    """13.3 第 3 条：同一 task 不允许两个执行器并发（task 级互斥跨线程）。"""
    lock = WriteTaskLock()
    assert lock.acquire("thread-1", "task-a").acquired
    result = lock.acquire("thread-2", "task-a")
    assert result.acquired is False
    assert result.queued is False
    assert result.reason == "task_busy"
    # task 级互斥独立于线程域：thread-2 的其他任务不受影响
    assert lock.acquire("thread-2", "task-b").acquired


def test_queued_acquire_is_idempotent() -> None:
    """重复 acquire 排队中的任务不重复入队。"""
    lock = WriteTaskLock()
    assert lock.acquire("thread-1", "task-a").acquired
    r1 = lock.acquire("thread-1", "task-b")
    r2 = lock.acquire("thread-1", "task-b")
    assert r1.queued and r2.queued
    assert lock.release("thread-1", "task-a") == "task-b"


def test_different_threads_independent() -> None:
    """不同 manager 会话（线程）互不影响（8.3 第 1 条按会话串行）。"""
    lock = WriteTaskLock()
    assert lock.acquire("thread-1", "task-a").acquired
    assert lock.acquire("thread-2", "task-b").acquired
    assert lock.acquire("thread-3", "task-c").acquired


def test_release_unknown_task_noop() -> None:
    lock = WriteTaskLock()
    assert lock.acquire("thread-1", "task-a").acquired
    assert lock.release("thread-1", "task-unknown") is None
    assert lock.holder("thread-1") == "task-a"  # 持有者不受影响


def test_concurrent_acquire_only_one_wins() -> None:
    """并发竞争：同一线程同一时刻至多一个写任务获得执行权（8.3 第 1 条）。"""
    lock = WriteTaskLock()
    barrier = threading.Barrier(2)
    winners: list[str] = []

    def contender(task_id: str) -> None:
        barrier.wait()
        if lock.acquire("thread-1", task_id).acquired:
            winners.append(task_id)

    threads = [threading.Thread(target=contender, args=(f"task-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1  # 恰好一个执行权持有者
    winner = winners[0]
    # 其余七个已入队，release 后按顺序逐个晋升
    released = []
    holder = lock.release("thread-1", winner)
    while holder is not None:
        released.append(holder)
        holder = lock.release("thread-1", holder)
    assert len(released) == 7
