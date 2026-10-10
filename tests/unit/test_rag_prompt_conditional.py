"""RAG 长期记忆正文**条件注入**的两轮装配守护测试（2026-10-10）。

背景：``ebe7414`` 起的 base_prompt 无条件教模型「先 rag_recall 再回答」，而
RAG 未配置时装配层已把 ``rag_*`` 工具从模型可见工具集剔除（
``without_unconfigured_rag_tools``）——模型被告知使用一个手里没有的工具，转而
编造。故长期记忆正文与工具的可见性必须**同源**（同一判据 ``rag_client is None``）：

- 决策轮 prompt（``model_node._build_system_prompt``）
- 参数轮 prompt（``tool_args_node.tool_args_node``）

必须**两轮都**正确——只改一处＝另一半的模型仍被误导。

判据设计（按本仓「锚点与后果绑同一小句 / 同一注入面」纪律）：
- 断言取自**真实注入面**：两轮组装出的 system_prompt 全串（不手抄常量）；
- 未配置态钉整体后果——**全串不得出现 ``rag_``**（含第 6 条那个从句；提到一个
  不存在的工具同样是误导）；
- 配置态钉反向：RAG 正文在位；并用 sha256 与「配置态逐字节基线」比对。
"""

from __future__ import annotations

import hashlib

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.model_node import _build_system_prompt
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.rag_memory import without_unconfigured_rag_tools

#: 配置态逐字节基线（sha256）。取改动前两轮渲染；本批**不得**改变配置态一个
#: 字节。若哪天有意的正文改动使基线失配，需人工复核后同步更新（勿当噪声删掉）。
_CONFIGURED_DECISION_SHA = (
    "af8a5aa4960ce37e7a744b73b4dfb61fefce2e61db3f5167fd2398525f784e5a"
)
_CONFIGURED_ARGS_SHA = (
    "6d2a8c3a3f1e2fa11a9593a1d476fd38e23336513aff67653193297e300cfb6f"
)


class _FakeRagClient:
    """RAG 已接线的占位（只用于 ``rag_client is not None`` 判据，不发起调用）。"""


class _CapturingClient:
    """捕获参数轮 system_prompt 的假模型客户端。"""

    def __init__(self) -> None:
        self.system_prompt: str | None = None

    def generate_tool_arguments(
        self, *, system_prompt, messages, schema_instruction, thread_id=None, task_id=None
    ):  # noqa: ANN001, ANN201
        self.system_prompt = system_prompt

        class _Result:
            tool_name = "erp_query"
            tool_arguments = {"entity": "metal_orders", "fetch_all": False}

        return _Result()


def _decision_prompt(rag_client) -> str:  # noqa: ANN001
    """决策轮 system prompt（与装配层同款：工具先经 without_unconfigured_rag_tools）。"""
    tools = without_unconfigured_rag_tools(TOOL_REGISTRY, rag_client)
    runtime = RuntimeBag(tools=tools, rag_client=rag_client)
    state = {"active_skill": None, "policy_keys": ["R4.query"]}
    return _build_system_prompt(runtime, state, "SCHEMA-X")


def _args_prompt(rag_client) -> str:  # noqa: ANN001
    """参数轮 system prompt（经节点真实组装，用假客户端捕获）。"""
    client = _CapturingClient()
    tools = without_unconfigured_rag_tools(TOOL_REGISTRY, rag_client)
    runtime = RuntimeBag(tools=tools, rag_client=rag_client, client=client)
    state = {
        "messages": [],
        "pending_action": {"tool_name": "erp_query", "tool_arguments": {}},
    }
    out = tool_args_node(state, runtime)
    assert out["current_step"] == "tool_args_done", out
    assert client.system_prompt is not None
    return client.system_prompt


# ---------------------------------------------------------------------------
# 未配置态：两轮都不得出现 rag_
# ---------------------------------------------------------------------------


def test_decision_round_has_no_rag_text_when_unconfigured() -> None:
    prompt = _decision_prompt(None)
    assert "rag_" not in prompt, (
        "RAG 未配置时决策轮 prompt 仍出现 rag_ 字样——模型被告知使用一个已被"
        "摘除的工具（[长期记忆口径]/[分寸]/第 6 条从句任一残留都会命中）"
    )
    # 反向哨兵：不是「整段被删」——查证规则其它内容仍在（同一次注入面内对照）
    assert "不得读明细心算" in prompt


def test_args_round_has_no_rag_text_when_unconfigured() -> None:
    prompt = _args_prompt(None)
    assert "rag_" not in prompt, (
        "RAG 未配置时参数轮 prompt 仍出现 rag_ 字样——只改了决策轮的注入点，"
        "参数轮是第二个注入点，必须同改"
    )
    assert "不能删除订单" in prompt  # 第 6 条其它语义仍在


# ---------------------------------------------------------------------------
# 配置态：RAG 正文在位，且逐字节等于改动前基线
# ---------------------------------------------------------------------------


def test_configured_rounds_carry_rag_text() -> None:
    assert "rag_recall" in _decision_prompt(_FakeRagClient())
    assert "rag_forget" in _decision_prompt(_FakeRagClient())
    assert "rag_forget" in _args_prompt(_FakeRagClient())


def test_configured_decision_prompt_is_byte_identical_to_baseline() -> None:
    prompt = _decision_prompt(_FakeRagClient())
    assert hashlib.sha256(prompt.encode()).hexdigest() == _CONFIGURED_DECISION_SHA, (
        "RAG 已配置时决策轮 prompt 与改动前不再逐字节相同——条件注入不得改动"
        "配置态文案（含换行/顺序）"
    )


def test_configured_args_prompt_is_byte_identical_to_baseline() -> None:
    prompt = _args_prompt(_FakeRagClient())
    assert hashlib.sha256(prompt.encode()).hexdigest() == _CONFIGURED_ARGS_SHA, (
        "RAG 已配置时参数轮 prompt 与改动前不再逐字节相同"
    )


# ---------------------------------------------------------------------------
# 正文可见性与工具可见性同源（同一判据）
# ---------------------------------------------------------------------------


def test_rag_text_visibility_tracks_tool_visibility() -> None:
    """正文里有无 rag_ ⇔ 工具集里有无 rag_*（两轮各自成立）。

    若哪天有人在两处或一处另立判据（如按 env 变量、按 settings 直接读），
    会出现「工具摘了、正文还在」或反之——本用例即红。
    """
    for rag_client in (None, _FakeRagClient()):
        tools = without_unconfigured_rag_tools(TOOL_REGISTRY, rag_client)
        tool_has_rag = any(name.startswith("rag_") for name in tools)
        for prompt in (_decision_prompt(rag_client), _args_prompt(rag_client)):
            assert ("rag_" in prompt) == tool_has_rag, (
                "正文 rag_ 可见性与工具 rag_* 可见性不一致（判据必须同源："
                "rag_client is None）"
            )
