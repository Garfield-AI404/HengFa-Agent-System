"""8.2 task 记录服务层 DTO（字段与 D3 tasks 表一一对应）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from administrative_agent.tasks.machine import TaskStatus


@dataclass(frozen=True)
class NewTaskSpec:
    """创建任务的入参（8.2 字段子集；其余字段由服务层初始化）。"""

    thread_id: str
    actor_account: str  # 真实账号（12.5，不做模型推断）
    actor_role: str  # manager/developer
    duty_code: str  # R1-R6 职责编号
    original_user_message: str  # 审计需要的最小文本（8.2：不保存隐藏思考）
    structured_intent: dict[str, Any] = field(default_factory=dict)
    is_write: bool = False  # 写任务需串行锁（8.3/13.3）；只读任务独立执行


@dataclass
class TaskRecord:
    """task 记录（8.2 全部字段；timezone-aware datetime 由仓储层负责）。"""

    task_id: str
    thread_id: str
    operation_id: str
    actor_account: str
    actor_role: str
    duty_code: str
    original_user_message: str
    structured_intent: dict[str, Any] = field(default_factory=dict)
    current_step: str = "created"
    status: TaskStatus = TaskStatus.PENDING
    pending_questions: list[str] | None = None
    execution_plan: dict[str, Any] | None = None
    confirmed_payload_hash: str | None = None
    reviewed_payload_hash: str | None = None
    checkpoint_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    final_error_code: str | None = None
    final_error_summary: str | None = None
