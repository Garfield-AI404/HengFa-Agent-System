"""8.1 任务状态机：八状态 + 确定性迁移表（D6）。

规则：
- 状态迁移只能由确定性代码执行；模型没有状态机访问路径（模型只经代码定义的
  LangGraph 节点运行，节点不得直接置 succeeded——8.1 末句）。
- 非法迁移抛 ``TaskTransitionError``；终态（succeeded/failed/cancelled）
  不可再迁移。
- 迁移表为静态常量，供 RV-09 材料逐条核对；每个合法迁移的可执行性由
  tests/tasks/test_machine.py 逐条覆盖。

状态语义（8.1 表格）：
- pending                ：已创建，等待调度（含排队中的写任务）
- running                ：正在执行（同一时刻每 manager 会话至多一个写任务，8.3）
- waiting_user_input     ：缺少字段、候选歧义或需要用户回答
- waiting_confirmation   ：等待轻确认
- waiting_review         ：等待 manager 复核
- succeeded              ：已完成并通过最终验证（只能经验证路径，见 TaskService.mark_succeeded）
- failed                 ：已失败，需要人工处理或重新发起
- cancelled              ：用户取消或复核拒绝后终止
"""

from __future__ import annotations

from typing import Final

from administrative_agent.persistence.models import TaskStatus

TERMINAL_STATES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

WAITING_STATES: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.WAITING_USER_INPUT,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_REVIEW,
    }
)

#: 确定性迁移表（8.1）。值集合之外的任何迁移一律非法。
TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_REVIEW,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_USER_INPUT: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.WAITING_CONFIRMATION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.WAITING_REVIEW: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

#: 每条迁移的业务条件（RV-09 材料 §8 逐条引用，测试按此断言）。
TRANSITION_CONDITIONS: Final[dict[tuple[TaskStatus, TaskStatus], str]] = {
    (TaskStatus.PENDING, TaskStatus.RUNNING): "调度/代码启动执行（写任务需先获得串行锁）",
    (TaskStatus.PENDING, TaskStatus.CANCELLED): "用户取消（未开始执行）",
    (TaskStatus.RUNNING, TaskStatus.WAITING_USER_INPUT): "缺少字段、候选歧义或需要用户回答（8.1）",
    (TaskStatus.RUNNING, TaskStatus.WAITING_CONFIRMATION): "等待轻确认（如 R3 默认值确认）",
    (TaskStatus.RUNNING, TaskStatus.WAITING_REVIEW): "等待 manager 复核（R6，7.4）",
    (TaskStatus.RUNNING, TaskStatus.SUCCEEDED): "最终验证已通过且 verification_passed=True"
    "（8.1：模型不能直接把任务标记为 succeeded，只能由验证代码路径设置）",
    (TaskStatus.RUNNING, TaskStatus.FAILED): "失败且需人工处理或重新发起（13.2 达重试上限）",
    (TaskStatus.RUNNING, TaskStatus.CANCELLED): "用户取消",
    (TaskStatus.WAITING_USER_INPUT, TaskStatus.RUNNING): "收到用户回答且显式绑定 task_id（8.3 第 2 条）",
    (TaskStatus.WAITING_USER_INPUT, TaskStatus.CANCELLED): "用户取消等待",
    (TaskStatus.WAITING_USER_INPUT, TaskStatus.FAILED): "用户明确放弃或无法继续",
    (TaskStatus.WAITING_CONFIRMATION, TaskStatus.RUNNING): "确认通过，继续执行（approved）",
    (TaskStatus.WAITING_CONFIRMATION, TaskStatus.CANCELLED): "用户取消确认（拒绝后终止，8.1）",
    (TaskStatus.WAITING_CONFIRMATION, TaskStatus.FAILED): "确认流程异常终止",
    (TaskStatus.WAITING_REVIEW, TaskStatus.RUNNING): "复核通过（approved）后继续执行，或驳回修改后重新提交",
    (TaskStatus.WAITING_REVIEW, TaskStatus.CANCELLED): "复核拒绝后终止（8.1：cancelled 含复核拒绝）",
    (TaskStatus.WAITING_REVIEW, TaskStatus.FAILED): "复核无法完成（如 payload 失效且不可重提）",
}


class TaskTransitionError(ValueError):
    """非法状态迁移（RV-09 通过标准：非法迁移被拒）。"""

    def __init__(self, current: TaskStatus | str, target: TaskStatus | str) -> None:
        # 注意：Python 3.11+ 的 str(枚举) 返回 'TaskStatus.X'，此处枚举取 .value
        self.current = current.value if isinstance(current, TaskStatus) else str(current)
        self.target = target.value if isinstance(target, TaskStatus) else str(target)
        super().__init__(
            f"illegal task transition: {self.current!r} -> {self.target!r}"
        )


def _coerce_status(value: TaskStatus | str) -> TaskStatus:
    """枚举/字符串统一转 TaskStatus（注意 Python 3.11+ 的 str() 会返回
    'TaskStatus.PENDING'，不能直接用于枚举重建）。"""
    if isinstance(value, TaskStatus):
        return value
    return TaskStatus(str(value))


def assert_transition(current: TaskStatus | str, target: TaskStatus | str) -> None:
    """校验迁移合法性；非法迁移抛 TaskTransitionError，合法迁移无副作用。"""
    try:
        cur = _coerce_status(current)
        dst = _coerce_status(target)
    except ValueError:
        raise TaskTransitionError(current, target) from None
    if dst not in TRANSITIONS.get(cur, frozenset()):
        raise TaskTransitionError(cur, dst)
