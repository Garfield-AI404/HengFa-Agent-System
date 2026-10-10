"""图 × checkpoint 集成测试（D7/RV-10）：真实 LangGraph + MemorySaver。

验证 8.5 写入机制（图节点执行后自动保存、thread_id/task_id 进入
metadata）与 CheckpointService 恢复/隔离在该机制下的行为。
真实 PostgreSQL 持久化由 scripts/d7_checkpoint_demo.py 提供证据。

## 2026-08-29 重写说明

原文件因导入已删的 `init_state` 而整体无法收集。被测能力（图执行自动
写 checkpoint、metadata 关联键、跨任务隔离）**全部存活**，故重写。

两处架构变更导致判别字段必须换掉（均经探针实测确认）：

1. **`operation_id` / `structured_intent` 不再进 checkpoint**。它们不是
   `ConversationState` 的 channel，LangGraph 对非 channel 的输入键直接
   丢弃——`channel_values` 里查不到，断言会假失败。改用仍在 State 中的
   `task_id` / `messages` / `current_step` 作判别。
2. **占位节点消费步数的语义已无**。旧断言 `remaining_steps ==
   REMAINING_STEPS - 1` 依赖「entry → placeholder → exit」骨架；现图为
   会话循环，且 **model 节点 fail closed 分支不返回 `remaining_steps`**
   （值保持不变）。故步数不再作为「图确实跑了」的证据，改用
   `current_step` 与 messages 累积。

`task_id` 之所以能当判别字段：它是 `ConversationState` 的 channel，
且各次执行传不同值，配合 `load_for_task` 的 metadata 过滤，正好复现
RV-10「旧任务快照不注入新任务」的原始意图。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.agent.graph import build_graph
from administrative_agent.agent.state import REMAINING_STEPS
from administrative_agent.tasks.machine import TaskStatus

from tests.checkpoint.conftest import make_task


def _config(*, thread_id: str, task_id: str) -> dict:
    """图执行 config：thread_id + task_id 是 checkpoint 的两个关联键。"""
    return {"configurable": {"thread_id": thread_id, "task_id": task_id}}


def _state(task_id: str, text: str | None = None) -> dict:
    """可执行的会话 State（不再有 init_state，直接构造 dict）。"""
    return {
        "messages": [HumanMessage(content=text)] if text else [],
        "remaining_steps": REMAINING_STEPS,
        "task_id": task_id,
    }


def test_graph_invoke_writes_checkpoint_with_task_metadata() -> None:
    """图执行后 checkpoint 自动保存，metadata 带 task_id（8.5 关联键）。"""
    saver = MemorySaver()
    compiled = build_graph(checkpointer=saver)
    config = _config(thread_id="thread-mgr-1", task_id="task-g1")
    compiled.invoke(_state("task-g1"), config)

    tup = saver.get_tuple(config)
    assert tup is not None
    assert tup.metadata.get("task_id") == "task-g1"

    values = tup.checkpoint["channel_values"]
    # 未配模型客户端 → fail closed 路径（model_failed），但快照已落盘
    assert values["current_step"] == "model_failed"
    # fail closed 分支不返回 remaining_steps → 值保持初始值
    assert values["remaining_steps"] == REMAINING_STEPS
    assert values["task_id"] == "task-g1"


def test_graph_resume_continues_from_checkpoint() -> None:
    """同 thread+task 再次执行：从 checkpoint 续跑（messages 累积）。

    旧断言用「剩余步数连续递减」证明续跑，该语义随占位节点删除已失效；
    改用 messages 累积——`add_messages`  reducer 把新输入追加到快照历史
    之后，是「确实从上次的 state 继续」的直接证据。
    """
    saver = MemorySaver()
    compiled = build_graph(checkpointer=saver)
    config = _config(thread_id="thread-mgr-1", task_id="task-g2")

    first = compiled.invoke(_state("task-g2", "第一句"), config)
    assert len(first["messages"]) == 2  # 用户消息 + model 节点错误回复

    # 第二次只传新消息：步数等其余字段由 checkpoint 提供
    second = compiled.invoke({"messages": [HumanMessage(content="第二句")]}, config)
    assert len(second["messages"]) == 4  # 快照 2 条 + 新用户消息 + 新回复
    assert second["task_id"] == "task-g2"


def test_checkpoint_service_load_from_real_graph(cp_service, saver, repo) -> None:
    """CheckpointService 经真实图执行产生的快照恢复该任务。"""
    task = make_task(repo, task_id="task-g3", status=TaskStatus.RUNNING)
    memory = MemorySaver()
    compiled = build_graph(checkpointer=memory)
    service = CheckpointService(memory, repo)

    compiled.invoke(_state(task.task_id), _config(thread_id=task.thread_id, task_id=task.task_id))
    assert service.attach_checkpoint_id(task.thread_id, task.task_id) is not None

    state = service.load_for_task(task.thread_id, task.task_id)
    assert state is not None
    assert state["task_id"] == task.task_id
    assert state["current_step"] == "model_failed"


def test_cross_task_isolation_in_same_thread() -> None:
    """RV-10 通过标准：同一线程先后两个任务，旧任务快照不注入新任务。

    线程内先执行 task-A（生成快照），再执行 task-B：
    load task-A → 返回 A 的快照；load task-B → B 的快照；
    load 从未执行的 task-C → None（不猜测、不重放）。

    判别字段用 `task_id`（见模块 docstring：`operation_id` /
    `structured_intent` 已不是 channel，进了图也会被丢弃）。
    """
    saver = MemorySaver()
    compiled = build_graph(checkpointer=saver)
    service = CheckpointService(saver)
    thread_id = "thread-mgr-1"

    compiled.invoke(_state("task-g4"), _config(thread_id=thread_id, task_id="task-g4"))
    compiled.invoke(_state("task-g5"), _config(thread_id=thread_id, task_id="task-g5"))

    state_a = service.load_for_task(thread_id, "task-g4")
    state_b = service.load_for_task(thread_id, "task-g5")
    state_c = service.load_for_task(thread_id, "task-g6")

    # 各取各的快照：task-A 的 state 里 task_id 是 A，不会被 B 污染
    assert state_a is not None and state_a["task_id"] == "task-g4"
    assert state_b is not None and state_b["task_id"] == "task-g5"
    assert state_c is None  # 不注入、不猜测
