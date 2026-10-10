"""agent 工具子集解析层 API 接线测试（多 agent 演进 · 机制前置）。

钉住「本轮只建机制不接线」：``POST /chat/messages`` 提交路径经
``_harness_runtime`` → ``resolve_tools`` 解析工具集，但三个调用点一律走
参数默认值 → ``resolve_tools`` 收到 ``None`` → 默认（全量）注册表；装配层
随后还会经 ``without_unconfigured_rag_tools`` 过滤（未接线 RAG 时剔除三个
``rag_*`` 工具），故最终模型可见集不等于全量注册表。
"""

from __future__ import annotations

from typing import Any

from administrative_agent.agent import toolsets
from administrative_agent.main import app
from administrative_agent.mcp.client import ToolResult
from administrative_agent.web import routes_chat

from tests.api.conftest import (
    MGR,
    ScriptedDecisionClient,
    _unset_state,
    headers,
)
from tests.api.test_auth_multi_manager import _wait_run_finished


class _PromptRecorder(ScriptedDecisionClient):
    """记录每轮 system_prompt（F3 判据钉在**模型真正看到的工具枚举表**上，
    而不是「过滤函数被调用过」——后者被邻近逻辑救活即假绿）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.system_prompts: list[str] = []

    def generate_decision(self, *, system_prompt: str = "", **kwargs: Any):  # noqa: ANN201
        self.system_prompts.append(system_prompt)
        return super().generate_decision(system_prompt=system_prompt, **kwargs)


class _FakeRagClient:
    """RAG 工具已接线时的假客户端（本组用例只关心工具集可见性，不调它）。"""

    def call_tool(self, tool_name: str, payload: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, data={})


def _run_and_collect_prompt(client) -> str:
    rec = _PromptRecorder()
    app.state.model_decision_client = rec
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单"},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    _wait_run_finished(resp.json()["thread_id"])
    return "\n".join(rec.system_prompts)


def test_submit_passes_agent_kind_none_to_default_registry(
    client, chat_harness, monkeypatch
) -> None:
    """提交路径调 ``resolve_tools(None)``（来源未定只留口子）。

    本用例只钉**传参恒为 None**（``seen == [None]``），**不**断言返回值身份：
    装配层随后还会经 ``without_unconfigured_rag_tools`` 过滤（未接线 RAG 时
    返回的是过滤后的**新 dict**，已非全量注册表对象），故不做 ``is TOOL_REGISTRY``
    这类身份断言。
    """
    app.state.model_decision_client = ScriptedDecisionClient()
    seen: list[object] = []
    real = toolsets.resolve_tools

    def recorder(kind):
        seen.append(kind)
        return real(kind)

    monkeypatch.setattr(routes_chat, "resolve_tools", recorder)
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单"},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    _wait_run_finished(resp.json()["thread_id"])

    assert seen == [None]  # 本轮恒传 None（来源未定，只留口子）


# ---------------------------------------------------------------------------
# F3：装配层 without_unconfigured_rag_tools 过滤有覆盖（reviewer 变异实测原为零覆盖）
# ---------------------------------------------------------------------------


def test_rag_tools_hidden_when_client_unwired(client, chat_harness) -> None:
    """rag_client 未接线 → 四个 rag_* **不在**模型可见工具枚举表里。

    判据钉在 system_prompt（模型真正看到的表），而非「过滤函数被调用过」。
    变异证明：把 routes_chat.py:548 还原成裸 ``resolve_tools(agent_kind)``，
    本用例即红（rag_* 会重新出现在表里）。
    """
    _unset_state("rag_client")  # 默认未接线（lifespan 未跑）
    prompt = _run_and_collect_prompt(client)
    for name in ("rag_remember", "rag_recall", "rag_forget", "rag_search_documents"):
        assert f"- {name}：" not in prompt, f"未配置时 {name} 仍暴露给模型"
    assert "- erp_query：" in prompt  # 对照组：其余工具照常在


def test_rag_tools_visible_when_client_wired(client, chat_harness) -> None:
    """rag_client 已接线 → 四个 rag_* **在**模型可见工具枚举表里。"""
    app.state.rag_client = _FakeRagClient()
    try:
        prompt = _run_and_collect_prompt(client)
    finally:
        _unset_state("rag_client")
    for name in ("rag_remember", "rag_recall", "rag_forget", "rag_search_documents"):
        assert f"- {name}：" in prompt, f"已配置时 {name} 未暴露给模型"
