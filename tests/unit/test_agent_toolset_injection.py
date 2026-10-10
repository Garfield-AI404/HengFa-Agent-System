"""agent 工具子集注入机制端到端单测（多 agent 演进 · 机制前置）。

用**真实工具名**（不合成假工具名）验证「工具子集」真的收窄了决策轮
可用工具集，并让子集外的工具在 tool_args / tools 两节点都被 unknown
安全网拦下（模型幻觉工具名不落 ERP）。

覆盖：
- 决策轮 system prompt 的**唯一一份**工具枚举表按子集收窄；
- tool_args 节点：子集外工具 → ``tool_unknown`` + 错误 ToolMessage +
  ``pending_action=None``；
- tools 节点：同上，且**不创建任何任务**。
"""

from __future__ import annotations

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.model_node import _build_system_prompt
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.nodes.tools_node import tools_node
from administrative_agent.agent.toolsets import resolve_tools


class _RecordingTasks:
    """任务服务桩：记录任何创建/变更调用（断言子集外工具**零副作用**）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):  # noqa: ANN204
        def _record(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.calls.append(name)
            return None

        return _record


def _pending(tool_name: str) -> dict:
    return {"pending_action": {"tool_name": tool_name, "tool_arguments": {}}}


def test_subset_narrows_decision_prompt_tool_table() -> None:
    """决策轮工具枚举表按子集收窄：财务档在表内、销售档工具不在表内。

    这是「决策轮工具枚举表收窄」的证明——枚举表由 ``tools.values()`` 顺序
    出文本，子集替换即少一行。
    """
    runtime = RuntimeBag(tools=resolve_tools("finance"))
    text = _build_system_prompt(runtime, {"active_skill": None}, "")
    assert "- finance_create：" in text
    assert "- order_create：" not in text


def test_out_of_subset_tool_returns_unknown_tool() -> None:
    """tool_args 节点：子集外工具 → tool_unknown + 未知工具 + 无挂起动作。"""
    runtime = RuntimeBag(tools=resolve_tools("finance"))
    state = {"messages": [], **_pending("order_create")}
    out = tool_args_node(state, runtime)
    assert out["current_step"] == "tool_unknown"
    assert "未知工具" in out["messages"][0].content
    assert out["pending_action"] is None


def test_out_of_subset_tool_not_dispatched_at_tools_node() -> None:
    """tools 节点：子集外工具 → tool_unknown，且**没有任何任务被创建**。"""
    tasks = _RecordingTasks()
    runtime = RuntimeBag(tools=resolve_tools("finance"), tasks=tasks)
    state = {"messages": [], **_pending("order_create")}
    out = tools_node(state, runtime)
    assert out["current_step"] == "tool_unknown"
    assert "未知工具" in out["messages"][0].content
    assert out["pending_action"] is None
    assert tasks.calls == []


def test_subset_tool_still_dispatches() -> None:
    """反向哨兵：子集**内**工具不被 unknown 拦下（子集不是「全拦」）。

    只读工具 erp_query 无破坏性，直接进入执行（``tool_done`` 或工具级错误
    兜底，均非 ``tool_unknown``）。
    """
    runtime = RuntimeBag(tools=resolve_tools("finance"), mcp_client=None)
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "erp_query",
            "tool_arguments": {"entity": "metal_orders", "fetch_all": False},
        },
    }
    out = tools_node(state, runtime)
    assert out["current_step"] != "tool_unknown"
