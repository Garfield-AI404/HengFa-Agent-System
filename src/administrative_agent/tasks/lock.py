"""8.3/13.3 串行写任务锁（进程内线程安全，D6）。

语义（与规格逐条对应）：
- 串行域 = ``thread_id``（manager 会话对应的对话线程）：同一时刻每个线程
  至多一个写任务持有执行权（8.3 第 1 条"同一个 manager 会话同一时刻只允许
  一个写任务处于 running"）。
- 同一 ``task_id`` 不允许两个执行器并发（13.3 第 3 条）：task 级互斥独立于
  线程域，跨线程重复获取同一任务也拒绝。
- 排队（8.3 第 3 条）：同一线程的后续写任务按 FIFO 排队，保持 pending 不进入
  running；release 时返回队头供服务层晋升调度。
- **waiting 期间锁保留**：任务从 running 进入 waiting_user_input/
  waiting_confirmation/waiting_review 时不释放锁——等待任务仍是同一条执行流，
  8.3 第 1 条只约束 running 数量，锁保留保证 resume 后无需重新竞争且排队任务
  不会抢先（口径详见 RV-09 材料 §8/§10）。
- 不同 thread 互不影响；只读任务不经本锁（8.3 第 3 条：无关新查询可独立执行）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class LockResult:
    """acquire 的结果。acquired=False 时 reason 说明拒绝原因。"""

    acquired: bool
    holder_task_id: str | None  # 当前持有者（未获得执行权时可见）
    queued: bool  # 是否已进入 FIFO 排队（acquired=False 且 reason="queued"）
    reason: str  # acquired / task_busy / queued


class WriteTaskLock:
    """写任务串行锁：线程级互斥 + task 级互斥 + FIFO 排队。"""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        # thread_id -> 持有执行权的 task_id
        self._holders: dict[str, str] = {}
        # thread_id -> FIFO 排队任务（task_id 列表）
        self._queues: dict[str, list[str]] = {}
        # task 级互斥：正在运行的 task_id 集合（跨线程去重）
        self._running_tasks: set[str] = set()

    def acquire(self, thread_id: str, task_id: str) -> LockResult:
        """尝试为 task 获取写执行权。

        - 成功：登记持有者与 task 级互斥，返回 acquired=True；
        - task 级互斥冲突（同一 task 已在执行）：拒绝，reason="task_busy"；
        - 线程域被占：进入 FIFO 排队（幂等，不重复入队），reason="queued"。
        """
        with self._mutex:
            if task_id in self._running_tasks:
                return LockResult(
                    acquired=False,
                    holder_task_id=self._holders.get(thread_id),
                    queued=False,
                    reason="task_busy",
                )
            holder = self._holders.get(thread_id)
            if holder is not None and holder != task_id:
                queue = self._queues.setdefault(thread_id, [])
                if task_id not in queue:
                    queue.append(task_id)
                return LockResult(
                    acquired=False,
                    holder_task_id=holder,
                    queued=True,
                    reason="queued",
                )
            self._holders[thread_id] = task_id
            self._running_tasks.add(task_id)
            return LockResult(
                acquired=True, holder_task_id=task_id, queued=False, reason="acquired"
            )

    def release(self, thread_id: str, task_id: str) -> str | None:
        """释放 task 的执行权；返回排队中的队头 task_id（无则 None）。

        释放后由服务层把队头任务迁移 pending→running 并重新 acquire
        （服务层调度职责；本锁只维护互斥与队列状态）。
        """
        with self._mutex:
            self._running_tasks.discard(task_id)
            if self._holders.get(thread_id) == task_id:
                del self._holders[thread_id]
            queue = self._queues.get(thread_id)
            if not queue:
                return None
            next_task = queue.pop(0)
            if not queue:
                del self._queues[thread_id]
            return next_task

    def holder(self, thread_id: str) -> str | None:
        """当前线程域的执行权持有者（无则 None）。"""
        with self._mutex:
            return self._holders.get(thread_id)

    def is_running(self, task_id: str) -> bool:
        """task 级互斥查询（13.3：同一 task 不允许两个执行器并发）。"""
        with self._mutex:
            return task_id in self._running_tasks
