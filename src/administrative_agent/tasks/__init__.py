"""任务生命周期服务（规格 8.1/8.2/8.3、13.3）。

- machine.py   ：8.1 八状态确定性迁移表与非法迁移拒绝
- lock.py      ：8.3/13.3 串行写任务锁（manager 会话级 + task 级互斥、FIFO 排队）
- records.py   ：TaskRecord 服务层 DTO
- service.py   ：8.2/8.3 task 记录服务（创建、状态迁移、resume/rebind 显式绑定、
                 终态锁释放与队列晋升）

目录归属说明（D6 任务书第 4 项"建议 tasks/ 或按 2.6 归属，你定并说明"）：
- 规格 2.6 树中任务状态机无单一归属（graph.py 是图连接、middleware/task_lock.py
  是图中间件、persistence/repositories.py 是持久化）；任务状态机是跨图/路由的
  领域服务层逻辑，故按任务书建议新增 tasks/ 包承载；
- 持久化仍按 2.6 树归属 persistence/repositories.py（task/audit/review/mapping
  持久化），tasks/ 经 Protocol 依赖仓储，不直接依赖 SQLAlchemy。
"""

from administrative_agent.tasks.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    TaskStatus,
    TaskTransitionError,
    assert_transition,
)
from administrative_agent.tasks.lock import LockResult, WriteTaskLock
from administrative_agent.tasks.records import NewTaskSpec, TaskRecord
from administrative_agent.tasks.service import (
    TaskRepository,
    TaskService,
    TaskThreadMismatchError,
    VerificationRequiredError,
)

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "WAITING_STATES",
    "TaskStatus",
    "TaskTransitionError",
    "assert_transition",
    "LockResult",
    "WriteTaskLock",
    "NewTaskSpec",
    "TaskRecord",
    "TaskRepository",
    "TaskService",
    "TaskThreadMismatchError",
    "VerificationRequiredError",
]
