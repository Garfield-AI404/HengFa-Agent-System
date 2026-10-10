"""[2026-09-10 机制层改造] 明细 cap 删除 + 只读查询明细隔轮驱逐。

三块覆盖（用户拍板：删 cap、隔轮驱逐走模型输入视图层、State/checkpoint/
压缩/存档不动）：

1. summarize_tool_result：明细无字符预算（原 16KB 预算删除——超限明细
   全量透传、无紧凑索引/省略提示；字段裁剪仍生效）；非 JSON 兜底仍按
   4096 截断；
2. tools_node 明细标记：已存档只读结果（含 items 且有 query_id 可回查）
   打 additional_kwargs {"detail": True, "query_id": …}；[2026-09-10
   P3-31] **仅明细源** erp_query / memory_lookup / list_entities 打标
   ——erp_calc 聚合结果不打（聚合值小，驱逐会让总量/divisor 跨轮丢失）；
   错误/无存档结果不标记（不驱逐）；memory_lookup query_id 路径回显存档
   标识且兜底回查仍可用（真实工具 + 假存档）；[2026-09-10 复核补]
   list_entities 目录结果此前收窄遗漏（常驻每轮请求的回归）今补回；

3. 模型输入隔轮驱逐：model_node/tool_args_node 组装模型输入时过滤
   「最新 human 之前」的明细 ToolMessage（当轮可见、隔轮驱逐；写工具/
   错误结果不驱逐；无 human 不驱逐）；经 MemorySaver 图两轮执行验证
   State/checkpoint 原文保留、标记经 checkpoint 序列化往返仍生效。

D5：测试夹具（假工具/假存档/记录型决策客户端），不代表真实运行态。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.graph import build_graph
from administrative_agent.agent.model_client import (
    AgentDecision,
    ToolArguments,
)
from administrative_agent.agent.nodes.model_node import (
    _filter_stale_details,
    model_node,
)
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.nodes.tools_node import (
    _archive_read_tool,
    summarize_tool_result,
    tools_node,
)
from administrative_agent.agent.state import REMAINING_STEPS, AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import ToolResultSummary, ToolSpec


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeToolQueryArchive:
    """假 tool_query_archive（ArchiveStore 的同形最小实现）。"""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[list, int, str]] = {}
        self.appends: list[dict] = []

    def append_tool_query(
        self,
        *,
        thread_id: str,
        query_id: str,
        entity: str,
        count: int,
        items: list,
        arguments: dict | None = None,
        ok: bool | None = None,
    ) -> None:
        self.appends.append(
            {"thread_id": thread_id, "query_id": query_id, "entity": entity,
             "count": count, "arguments": arguments, "ok": ok}
        )
        self.rows[query_id] = (list(items), count, entity)

    def get_tool_query(self, thread_id: str, query_id: str) -> tuple[list, int, str] | None:
        return self.rows.get(query_id)


class RecordingDecisionClient:
    """记录每次模型输入的决策客户端（不接真实模型；恒回 reply）。

    - 记录 decision 轮与参数轮的 messages 供断言「模型实际看到了什么」；
    - args_by_tool：按 call_tool 决策的 tool_name 返回参数（参数轮）。
    """

    def __init__(
        self,
        *,
        args_by_tool: dict[str, dict] | None = None,
        tool_name: str | None = None,
        reply: str = "已处理。",
    ) -> None:
        self._args_by_tool = dict(args_by_tool or {})
        self._pending_tool: str | None = tool_name
        self.reply = reply
        self.decision_inputs: list[list] = []
        self.args_inputs: list[list] = []

    def generate_decision(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: Any
    ) -> AgentDecision:
        self.decision_inputs.append(list(messages))
        self._pending_tool = None
        return AgentDecision(reply_text=self.reply, next_action="reply")

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: Any
    ) -> ToolArguments:
        self.args_inputs.append(list(messages))
        name = self._pending_tool or ""
        return ToolArguments(
            tool_name=name, tool_arguments=dict(self._args_by_tool.get(name, {}))
        )


class TwoTurnQueryClient(RecordingDecisionClient):
    """两轮脚本客户端：第一轮调 erp_query、第二轮调 memory_lookup
    （按最新 human 文本 + 当轮是否已有工具结果判定），其余轮 reply。"""

    def __init__(self, *, reply: str = "已处理。") -> None:
        super().__init__(args_by_tool={"erp_query": {}}, reply=reply)
        #: 第一轮查询的存档标识（用例在第一轮结束后回填，第二轮 memory_lookup 用）
        self.lookup_query_id: str = ""

    def generate_decision(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: Any
    ) -> AgentDecision:
        self.decision_inputs.append(list(messages))
        latest_idx = -1
        latest_text = ""
        for i, message in enumerate(messages):
            if getattr(message, "type", "") == "human":
                latest_idx, latest_text = i, str(getattr(message, "content", ""))
        has_tool_after = any(
            getattr(m, "type", "") == "tool" for m in messages[latest_idx + 1:]
        )
        if latest_idx >= 0 and not has_tool_after:
            if "第一轮" in latest_text:
                self._pending_tool = "erp_query"
                return AgentDecision(
                    reply_text="", next_action="call_tool", tool_name="erp_query"
                )
            if "第二轮" in latest_text:
                self._pending_tool = "memory_lookup"
                return AgentDecision(
                    reply_text="", next_action="call_tool", tool_name="memory_lookup"
                )
        self._pending_tool = None
        return AgentDecision(reply_text=self.reply, next_action="reply")

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: Any
    ) -> ToolArguments:
        self.args_inputs.append(list(messages))
        name = self._pending_tool or ""
        if name == "memory_lookup":
            return ToolArguments(
                tool_name=name, tool_arguments={"query_id": self.lookup_query_id}
            )
        return ToolArguments(
            tool_name=name, tool_arguments=dict(self._args_by_tool.get(name, {}))
        )


class _FakeQueryArgs(BaseModel):
    """假只读查询工具参数（恒空——夹具不校验）。"""


def _erp_query_spec(items: list[dict] | None = None) -> ToolSpec:
    """假只读查询工具（名字取真实明细源 erp_query——[2026-09-10 P3-31]
    detail 标记只对 erp_query/memory_lookup 打；结果形态对齐 erp_query：
    entity + items 明细）。"""
    rows = items if items is not None else [
        {"id": 1, "model": "M-01", "weight_per_unit": "10"},
        {"id": 2, "model": "M-02", "weight_per_unit": "20"},
        {"id": 3, "model": "M-03", "weight_per_unit": "30"},
    ]

    def _run(ctx: Any, args: Any) -> ToolResultSummary:
        return ToolResultSummary(
            ok=True,
            summary={"entity": "metal_orders", "count": len(rows),
                     "total": len(rows), "items": rows},
        )

    return ToolSpec(
        name="erp_query",
        description="测试用只读查询",
        args_schema=_FakeQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=False,
        duty_code="R0",
        policy_key_of=lambda args: "R0.none",
        run=_run,
    )


def _list_entities_spec() -> ToolSpec:
    """假目录工具（名字取真实明细源 list_entities——[2026-09-10 复核补]
    detail 标记对其打；结果形态对齐真实工具：entity=catalog + items 条目）。"""
    items = [
        {"name": "metal_orders", "label": "钣金订单",
         "fields": [{"name": "weight_unit", "enum": ["kg", "m²"]}]},
        {"name": "powder_orders", "label": "塑粉订单", "fields": []},
    ]

    def _run(ctx: Any, args: Any) -> ToolResultSummary:
        return ToolResultSummary(
            ok=True,
            summary={"entity": "catalog", "scope": "compact",
                     "count": len(items), "items": items},
        )

    return ToolSpec(
        name="list_entities",
        description="测试用实体目录",
        args_schema=_FakeQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=False,
        duty_code="QRY",
        policy_key_of=lambda args: "QRY.catalog",
        run=_run,
    )


def _erp_calc_spec() -> ToolSpec:
    """假聚合工具（名字取真实聚合源 erp_calc——聚合结果不打 detail 标记，
    见 tests_node 分支 [2026-09-10 P3-31]）。"""
    rows = [{"unit": "kg", "measures": [{"alias": "sum(total_weight)",
                                         "value": "123.00", "n": 2}]}]

    def _run(ctx: Any, args: Any) -> ToolResultSummary:
        return ToolResultSummary(
            ok=True,
            summary={"entity": "erp_calc", "target_entity": "metal_orders",
                     "count": len(rows), "items": rows},
        )

    return ToolSpec(
        name="erp_calc",
        description="测试用聚合计算",
        args_schema=_FakeQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=False,
        duty_code="R0",
        policy_key_of=lambda args: "R0.none",
        run=_run,
    )


def _detail_message(text: str = '{"ok": true}', query_id: str = "tool-call-d1") -> ToolMessage:
    """明细 ToolMessage（与 tools_node 打标形态一致）。"""
    return ToolMessage(
        content=text,
        tool_call_id=query_id,
        additional_kwargs={"detail": True, "query_id": query_id},
    )


def _plain_tool_message(text: str = '{"ok": true, "task_id": "t-1"}') -> ToolMessage:
    """非明细 ToolMessage（写工具结果/错误等，无标记）。"""
    return ToolMessage(content=text, tool_call_id="tool-call-plain")


def _input_chars(messages: list) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", "")
        total += len(content) if isinstance(content, str) else 400
    return total


# ---------------------------------------------------------------------------
# 1. summarize_tool_result：明细全量（无字符预算）
# ---------------------------------------------------------------------------


def test_summarize_full_detail_no_budget_and_plain_fallback() -> None:
    """明细无字符预算：超 16KB 全量透传（无紧凑索引/省略提示）；字段裁剪
    仍生效；非 JSON 兜底仍按 4096 截断。"""
    from administrative_agent.agent.tools.crop import crop_items

    items = [
        {"id": i, "model": "M" + "x" * 600, "pad": "y" * 100}
        for i in range(40)
    ]
    payload = {
        "ok": True, "entity": "metal_orders", "count": len(items),
        "total": len(items), "items": items,
    }
    content = summarize_tool_result(
        json.dumps(payload, ensure_ascii=False), query_id="tool-call-big"
    )
    parsed = json.loads(content)
    assert parsed["items"] == crop_items(items, "metal_orders")  # 裁剪仍生效
    assert len(parsed["items"]) == len(items)  # 一条不丢
    assert parsed["archived"] is True
    assert parsed["query_id"] == "tool-call-big"
    assert "index" not in parsed  # 紧凑索引路径已删除
    assert "…共" not in content
    assert len(content) > 16384  # 原明细预算不再封顶

    # 非 JSON 兜底：仍按 TOOL_RESULT_HISTORY_CHARS(4096) 截断（防御分支）
    raw = "not-json " * 2000
    assert len(summarize_tool_result(raw)) == 4096


# ---------------------------------------------------------------------------
# 2. tools_node 明细标记
# ---------------------------------------------------------------------------


def test_tools_node_marks_archived_detail() -> None:
    """已存档只读结果：additional_kwargs 打 {"detail": True, "query_id"}，
    且 query_id 即 tool_call_id（存档键）、内容含 archived 提示。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(tools={"erp_query": _erp_query_spec()}, archive=archive)
    state = {
        "messages": [],
        "pending_action": {"tool_name": "erp_query", "tool_arguments": {}},
    }
    out = tools_node(state, runtime)
    assert out["current_step"] == "tool_done"
    message = out["messages"][0]
    assert message.additional_kwargs == {
        "detail": True, "query_id": message.tool_call_id
    }
    assert message.tool_call_id in archive.rows  # 存档键 = query_id
    assert json.loads(message.content)["archived"] is True


def test_tools_node_error_and_unarchived_results_not_marked() -> None:
    """错误结果与「无存档」结果不打标记（无回查路径，不参与隔轮驱逐）。"""

    def _error_spec() -> ToolSpec:
        def _run(ctx: Any, args: Any) -> ToolResultSummary:
            return ToolResultSummary(ok=False, summary={"error": "boom"})

        return ToolSpec(
            name="erp_query", description="测试用只读查询",
            args_schema=_FakeQueryArgs, required_scopes=frozenset(),
            requires_manager=False, is_write=False, duty_code="R0",
            policy_key_of=lambda args: "R0.none", run=_run,
        )

    # 错误结果（无 items）
    out = tools_node(
        {"messages": [], "pending_action": {"tool_name": "erp_query", "tool_arguments": {}}},
        RuntimeBag(tools={"erp_query": _error_spec()}, archive=FakeToolQueryArchive()),
    )
    assert out["messages"][0].additional_kwargs == {}

    # 成功有 items 但 archive 未接线（无 query_id 可回查）→ 不标记
    out2 = tools_node(
        {"messages": [], "pending_action": {"tool_name": "erp_query", "tool_arguments": {}}},
        RuntimeBag(tools={"erp_query": _erp_query_spec()}, archive=None),
    )
    assert out2["messages"][0].additional_kwargs == {}


def test_tools_node_calc_result_not_marked_as_detail() -> None:
    """[2026-09-10 P3-31] erp_calc 聚合结果不打 detail 标记——聚合值仅几百
    字符，驱逐收益低且会让「总量可核对 / normalized divisor」跨轮丢失；
    结果仍完整进历史（items 保留、不被隔轮驱逐）。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(tools={"erp_calc": _erp_calc_spec()}, archive=archive)
    out = tools_node(
        {"messages": [], "pending_action": {"tool_name": "erp_calc", "tool_arguments": {}}},
        runtime,
    )
    message = out["messages"][0]
    assert out["current_step"] == "tool_done"
    assert message.additional_kwargs == {}  # 无 detail 标记 → 不参与隔轮驱逐
    parsed = json.loads(message.content)
    assert parsed["items"]  # 聚合条目在原样链路中（模型可见）


def test_tools_node_list_entities_marked_as_detail() -> None:
    """[2026-09-10 复核补] list_entities 目录结果属明细源：已存档结果打
    detail 标记（参与隔轮驱逐）——此前 DETAIL_TOOL_NAMES 收窄遗漏导致 12KB
    目录常驻其后每一轮请求（回归）。与 erp_calc（上一条用例：不打标记）
    形成对照。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(
        tools={"list_entities": _list_entities_spec()}, archive=archive
    )
    out = tools_node(
        {"messages": [],
         "pending_action": {"tool_name": "list_entities", "tool_arguments": {}}},
        runtime,
    )
    message = out["messages"][0]
    assert out["current_step"] == "tool_done"
    assert message.additional_kwargs == {
        "detail": True, "query_id": message.tool_call_id
    }
    assert message.tool_call_id in archive.rows  # 目录已存档（可回查）


def test_memory_lookup_result_marked_and_recoverable() -> None:
    """memory_lookup query_id 路径：结果本身即来自存档 → 打标记（存档标识
    取 summary 回显的 query_id）；被驱逐后仍可再经 memory_lookup 回查。"""
    archive = FakeToolQueryArchive()
    rows = [{"id": 1, "model": "M-01"}, {"id": 2, "model": "M-02"}]
    archive.rows["tool-call-orig"] = (rows, len(rows), "metal_orders")
    runtime = RuntimeBag(
        tools=TOOL_REGISTRY, archive=archive,
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}), thread_id="thread-evict-1",
        ),
    )
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "memory_lookup",
            "tool_arguments": {"query_id": "tool-call-orig"},
        },
    }
    out = tools_node(state, runtime)
    message = out["messages"][0]
    assert out["current_step"] == "tool_done"
    assert message.additional_kwargs == {
        "detail": True, "query_id": "tool-call-orig"
    }
    # 兜底内容仍可取回（items 白名单裁剪后返回）
    parsed = json.loads(message.content)
    assert parsed["items"] == rows and parsed["total"] == len(rows)


# ---------------------------------------------------------------------------
# 3. 模型输入隔轮驱逐（视图层）
# ---------------------------------------------------------------------------


def test_detail_marker_survives_checkpoint_serializer() -> None:
    """明细标记经 checkpoint 序列化往返仍生效（生产 PostgresSaver 与内存
    checkpointer 同一 JsonPlusSerializer serde——驱逐依赖标记，丢标记等于
    隔轮驱逐失效）。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    message = _detail_message("明细", "tool-call-ser")
    blob = serde.dumps_typed(message)
    restored = serde.loads_typed(blob)
    assert restored.additional_kwargs == {"detail": True, "query_id": "tool-call-ser"}
    assert restored.tool_call_id == "tool-call-ser"


def test_filter_stale_details_view_layer() -> None:
    """纯过滤函数：仅「最新 human 之前」的明细被滤掉；写结果/错误消息
    （无标记）保留；最新 human 及其后明细全保留；无 human 不驱逐。"""
    h1 = HumanMessage(content="第一轮问题")
    stale = _detail_message()
    ai = AIMessage(content="第一轮回答")
    plain = _plain_tool_message()
    h2 = HumanMessage(content="第二轮问题")
    current = _detail_message("当轮明细", "tool-call-d2")

    out = _filter_stale_details([h1, stale, ai, plain, h2, current])
    assert out == [h1, ai, plain, h2, current]  # 仅 stale 被驱逐

    # 无 human（图内异常形态）：原样返回，不驱逐
    assert _filter_stale_details([stale, ai]) == [stale, ai]
    # 最新 human 在第 0 位：其前无消息，原样
    assert _filter_stale_details([h1, stale]) == [h1, stale]
    # 无明细可驱逐：原列表对象原样返回（不产生新列表）
    same = [h1, plain, h2]
    assert _filter_stale_details(same) is same


def test_model_node_input_evicts_stale_keeps_in_turn() -> None:
    """model_node 组装模型输入：上一轮明细不进输入；当轮（最新 human 后）
    明细可见；State 列表原样未动（视图层过滤）。"""
    client = RecordingDecisionClient(reply="ok")
    runtime = RuntimeBag(client=client, tools={})
    h1 = HumanMessage(content="第一轮")
    stale = _detail_message("旧明细", "tool-call-old")
    current = _detail_message("本轮明细", "tool-call-new")
    messages = [h1, stale, HumanMessage(content="第二轮"), current]
    state = {"messages": list(messages), "remaining_steps": 5}

    out = model_node(state, runtime)
    assert out["current_step"] == "model_done"
    sent = client.decision_inputs[0]
    sent_tool_ids = [
        getattr(m, "tool_call_id", None) for m in sent if getattr(m, "type", "") == "tool"
    ]
    assert "tool-call-old" not in sent_tool_ids
    assert "tool-call-new" in sent_tool_ids
    human_idxs = [i for i, m in enumerate(sent) if getattr(m, "type", "") == "human"]
    assert [sent[i].content for i in human_idxs] == ["第一轮", "第二轮"]  # 最新 human 保留
    assert _input_chars(sent) < _input_chars(messages)  # 输入体积下降
    assert state["messages"] == messages  # State 未改动


def test_tool_args_node_input_evicts_stale() -> None:
    """tool_args_node 参数轮模型输入同样过滤上一轮明细（两个 messages
    消费点同口径）。"""
    client = RecordingDecisionClient(
        args_by_tool={"erp_query": {}}, tool_name="erp_query",
    )
    runtime = RuntimeBag(client=client, tools={"erp_query": _erp_query_spec()})
    stale = _detail_message("旧明细", "tool-call-old")
    state = {
        "messages": [HumanMessage(content="第一轮"), stale, HumanMessage(content="第二轮")],
        "pending_action": {"tool_name": "erp_query", "next_action": "call_tool"},
    }
    out = tool_args_node(state, runtime)
    assert out["current_step"] == "tool_args_done"
    sent = client.args_inputs[0]
    assert all(getattr(m, "tool_call_id", None) != "tool-call-old" for m in sent)
    assert any(getattr(m, "type", "") == "human" for m in sent)


# ---------------------------------------------------------------------------
# 4. 图两轮执行（MemorySaver）：State/checkpoint 保留 + 隔轮驱逐 + 兜底回查
# ---------------------------------------------------------------------------


def test_graph_two_turns_state_keeps_detail_input_evicts_and_lookup_fallback() -> None:
    """两轮对话端到端：

    - 第一轮：erp_query 明细进 State（标记）且当轮模型可见；
    - 第二轮：新 human 进来后模型输入不含第一轮明细（体积骤降）；State/
      checkpoint 原文保留（明细消息仍在，标记经序列化往返仍生效）；
    - 第二轮内模型经 memory_lookup(query_id) 回查明细成功（兜底可用）。
    """
    saver = MemorySaver()
    archive = FakeToolQueryArchive()
    client = TwoTurnQueryClient()
    tools = {"erp_query": _erp_query_spec(), "memory_lookup": TOOL_REGISTRY["memory_lookup"]}
    runtime = RuntimeBag(
        client=client, tools=tools, archive=archive,
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset({"erp:query"}), thread_id="thread-evict-2",
        ),
    )
    compiled = build_graph(checkpointer=saver, runtime=runtime)
    config = {"configurable": {"thread_id": "thread-evict-2"}}

    # ---- 第一轮：查询 → 明细进 State ----
    first = compiled.invoke(
        {"messages": [HumanMessage(content="第一轮：查一下明细")],
         "remaining_steps": REMAINING_STEPS},
        config,
    )
    details = [m for m in first["messages"] if getattr(m, "type", "") == "tool"]
    assert len(details) == 1
    detail_id = details[0].tool_call_id
    assert details[0].additional_kwargs.get("detail") is True
    assert detail_id in archive.rows  # 存档已写
    # 当轮第二次决策（工具结果之后）能看到明细
    assert any(
        getattr(m, "tool_call_id", None) == detail_id
        for m in client.decision_inputs[1]
    )

    # ---- 第二轮：新 human → 上一轮明细被驱逐（视图层）；模型主动回查 ----
    client.lookup_query_id = detail_id
    second = compiled.invoke(
        {"messages": [HumanMessage(content="第二轮：刚才那些订单有多少行？")],
         "remaining_steps": REMAINING_STEPS, "write_retry_counts": {}},
        config,
    )
    # 第二轮首轮决策输入：不含第一轮明细；含最新 human；体积骤降
    turn1_chars = _input_chars(client.decision_inputs[1])  # 第一轮含明细的输入
    turn2_sent = client.decision_inputs[2]
    sent_ids = [
        getattr(m, "tool_call_id", None)
        for m in turn2_sent if getattr(m, "type", "") == "tool"
    ]
    assert detail_id not in sent_ids
    assert _input_chars(turn2_sent) < turn1_chars  # 体积骤降（明细被驱逐）
    assert any(
        getattr(m, "type", "") == "human" and "第二轮" in str(m.content)
        for m in turn2_sent
    )
    # State/checkpoint 原文保留：明细消息仍在（标记经序列化往返仍生效）
    kept = [m for m in second["messages"] if getattr(m, "type", "") == "tool"]
    assert any(m.tool_call_id == detail_id and m.additional_kwargs.get("detail") for m in kept)
    # 兜底：第二轮 memory_lookup 回查成功（结果在 State 中，内容为存档明细）
    lookup_msgs = [
        m for m in second["messages"]
        if getattr(m, "type", "") == "tool"
        and m.additional_kwargs.get("query_id") == detail_id
        and m.tool_call_id != detail_id
    ]
    assert lookup_msgs, "第二轮 memory_lookup 回查结果缺失（兜底链路断）"
    lookup_parsed = json.loads(lookup_msgs[0].content)
    assert lookup_parsed["items"] == archive.rows[detail_id][0]
    # 第二轮最后一次决策可见 memory_lookup 明细（当轮可见），且能回出答复
    assert any(
        getattr(m, "tool_call_id", None) == lookup_msgs[0].tool_call_id
        for m in client.decision_inputs[-1]
    )
    assert second["messages"][-1].content == "已处理。"


# ---------------------------------------------------------------------------
# 5. [2026-10-06 §3.8 读工具调用参数落审计] 每次只读工具调用落一行
#    （含 0 行成功 / 失败 / 抛异常 / 参数校验失败），arguments/ok 可回放；
#    **模型可见行为一字不变**（query_id/detail 条件未放宽）。
# ---------------------------------------------------------------------------


class _CustomerQueryArgs(BaseModel):
    """带 filter/fields 的只读查询参数（验证入参可回放）。"""

    entity: str = "metal_orders"
    fields: list[str] | None = None
    filter: dict[str, Any] | None = None


def _read_spec(*, ok: bool, summary: dict[str, Any]) -> ToolSpec:
    """可配置结果的只读查询 spec（§3.8 存档测试用）。"""

    def _run(ctx: Any, args: Any) -> ToolResultSummary:
        return ToolResultSummary(ok=ok, summary=dict(summary))

    return ToolSpec(
        name="erp_query",
        description="测试用只读查询",
        args_schema=_CustomerQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=False,
        duty_code="R0",
        policy_key_of=lambda args: "R0.none",
        run=_run,
    )


def _read_state(args: dict) -> dict:
    return {
        "messages": [],
        "pending_action": {"tool_name": "erp_query", "tool_arguments": args},
    }


def test_archives_zero_row_read_call_with_arguments() -> None:
    """0 行成功调用也落一行：ok=True、count=0、items=[]；arguments 为本次
    入参（能读出 filter/fields）；且**模型可见 query_id/detail 不因落库而
    改变**（0 行不挂存档标识）。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(
        tools={
            "erp_query": _read_spec(
                ok=True, summary={"entity": "metal_orders", "count": 0, "items": []}
            )
        },
        archive=archive,
    )
    out = tools_node(
        _read_state(
            {
                "entity": "metal_orders",
                "fields": ["id", "total_amount"],
                "filter": {"customer_id": 7, "in_date": {"gte": "2026-08-01"}},
            }
        ),
        runtime,
    )
    assert out["current_step"] == "tool_done"
    assert len(archive.appends) == 1
    row = archive.appends[0]
    assert row["ok"] is True
    assert row["count"] == 0
    # arguments 可回放：filter/fields 逐字可读
    assert row["arguments"] == {
        "entity": "metal_orders",
        "fields": ["id", "total_amount"],
        "filter": {"customer_id": 7, "in_date": {"gte": "2026-08-01"}},
    }
    # 模型可见行为不变：0 行不挂 query_id / detail
    assert out["messages"][0].additional_kwargs == {}
    # [2026-10-06 §3.8 P1 假锁修复] 钉住 content：0 行成功的读调用虽已落库，
    # 但**不得**把可回查标识写进模型可见正文。删掉门控里的 ``and items``
    # （0 行被当成可回查）会在此变红——此前只断言 additional_kwargs，漏掉了
    # query_id/archived 是经 ToolMessage.content 暴露给模型的。
    visible = json.loads(out["messages"][0].content)
    assert "query_id" not in visible
    assert "archived" not in visible


def test_failed_read_call_with_items_does_not_expose_query_id() -> None:
    """失败但**有明细**（ok=False + 非空 items + entity）仍不得挂 query_id。

    [2026-10-06 §3.8 P1] 钉住门控里的 ``result.ok``：删掉它，失败调用（只要
    items 非空）就会挂上可回查标识、把失败结果写进模型可见正文。既有失败
    用例（items 为空）在缺 ``result.ok`` 时仍被 ``isinstance(items, list)``
    拦住，故需本用例专门覆盖「失败 + 非空 items」这一面。"""

    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(
        tools={
            "erp_query": _read_spec(
                ok=False,
                summary={
                    "entity": "metal_orders",
                    "items": [{"id": 1, "total_amount": 100}],
                    "error": "部分明细读取失败",
                },
            )
        },
        archive=archive,
    )
    out = tools_node(_read_state({"entity": "metal_orders"}), runtime)
    assert out["current_step"] == "tool_done"
    assert len(archive.appends) == 1
    assert archive.appends[0]["ok"] is False
    visible = json.loads(out["messages"][0].content)
    assert "query_id" not in visible
    assert "archived" not in visible
    assert out["messages"][0].additional_kwargs == {}


def test_archives_failed_read_call() -> None:
    """失败（ok=False）调用落行，ok=False、count=0/items=[]；入参可回放；
    模型可见不变（错误结果不打标记）。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(
        tools={"erp_query": _read_spec(ok=False, summary={"error": "ERP 查询失败"})},
        archive=archive,
    )
    out = tools_node(_read_state({"entity": "suppliers"}), runtime)
    assert out["current_step"] == "tool_done"
    assert len(archive.appends) == 1
    row = archive.appends[0]
    assert row["ok"] is False
    assert row["count"] == 0
    assert row["arguments"] == {"entity": "suppliers", "fields": None, "filter": None}
    assert out["messages"][0].additional_kwargs == {}
    # [2026-10-06 §3.8 P1 假锁修复] content 同理不得含可回查标识
    visible = json.loads(out["messages"][0].content)
    assert "query_id" not in visible
    assert "archived" not in visible


def test_archives_execution_exception_read_call() -> None:
    """执行抛异常的读工具调用也落一行（ok=False），入参可回放。"""

    def _run(ctx: Any, args: Any) -> ToolResultSummary:
        raise RuntimeError("MCP 连接超时")

    spec = ToolSpec(
        name="erp_query",
        description="测试用只读查询",
        args_schema=_CustomerQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=False,
        duty_code="R0",
        policy_key_of=lambda args: "R0.none",
        run=_run,
    )
    archive = FakeToolQueryArchive()
    out = tools_node(
        _read_state({"entity": "metal_orders"}),
        RuntimeBag(tools={"erp_query": spec}, archive=archive),
    )
    assert out["current_step"] == "tool_failed"
    assert len(archive.appends) == 1
    assert archive.appends[0]["ok"] is False
    assert archive.appends[0]["arguments"]["entity"] == "metal_orders"


def test_archives_invalid_arguments_read_call() -> None:
    """参数校验失败的读工具调用落行（arguments=模型原始入参，ok=False）。"""
    archive = FakeToolQueryArchive()
    runtime = RuntimeBag(
        tools={"erp_query": _read_spec(ok=True, summary={})}, archive=archive
    )
    out = tools_node(_read_state({"filter": "not-a-dict"}), runtime)
    assert out["current_step"] == "tool_invalid_args"
    assert len(archive.appends) == 1
    # 校验失败无 args 模型 → 存模型原始入参
    assert archive.appends[0]["arguments"] == {"filter": "not-a-dict"}
    assert archive.appends[0]["ok"] is False


def test_archive_helper_skips_write_tools_and_unwired_archive() -> None:
    """_archive_read_tool：写工具不落行；archive 未接线返回 False（不改行为）。"""
    archive = FakeToolQueryArchive()
    write_spec = ToolSpec(
        name="order_create",
        description="写工具",
        args_schema=_FakeQueryArgs,
        required_scopes=frozenset(),
        requires_manager=False,
        is_write=True,
        duty_code="R2",
        policy_key_of=lambda args: "R0.none",
        run=lambda ctx, args: None,
    )
    ok = _archive_read_tool(
        RuntimeBag(tools={}, archive=archive),
        spec=write_spec,
        tool_name="order_create",
        tool_call_id="q-w",
        arguments={"a": 1},
        ok=True,
        summary={"items": [{"id": 1}], "entity": "metal_orders"},
    )
    assert ok is False
    assert archive.appends == []

    ok2 = _archive_read_tool(
        RuntimeBag(tools={}, archive=None),
        spec=_read_spec(ok=True, summary={}),
        tool_name="erp_query",
        tool_call_id="q-n",
        arguments={},
        ok=True,
        summary={},
    )
    assert ok2 is False
