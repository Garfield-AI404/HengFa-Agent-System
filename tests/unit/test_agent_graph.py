"""D6 agent 图骨架测试：State 结构、不可变 context、路由决策、最小图连通性。

## 2026-08-29 重写说明

原文件因导入 `agent.state` 的 `TaskState` / `init_state` 而整体无法收集——
这两个符号已于 2026-08-24 harness 改造时随任务式流水线（legacy）删除，
State 类型改为 `ConversationState`（TypedDict, total=False），图也从
「entry → placeholder → exit」占位骨架换成
「model → tool_args → tools → (confirm) → model」会话循环。

被测对象**全部存活**，只是形态变了，故按新架构重写而非删除：

| 原用例 | 处置 |
|---|---|
| `test_init_state_fields_and_remaining_steps` | 重写：State 改为直接构造 dict，不再有 `init_state`；`structured_intent` / `operation_id` 业务字段已不进 State |
| `test_state_has_no_identity_keys` | 保留（不变量未变，8.4 末段） |
| `test_agent_context_is_frozen_and_scope_aware` | 原样保留 |
| `test_graph_compiles_and_invokes` | 重写：断言点从占位节点 `exited` 改为空桩 client 的 fail closed 路径 |
| `test_graph_steps_decrement_until_exhausted` | 重写：占位节点消费步数的语义已无；改为钉住「步数耗尽 → steps_exhausted」 |
| `test_graph_rejects_missing_required_fields` | **语义已反转**：`ConversationState` 是 `total=False`，缺任何字段都不报 ValueError，而是降级到 steps_exhausted。按现状改写，并注释说明为何不再是 ValueError |
| — | 新增 4 条覆盖 3 个路由函数（`route_after_model` / `route_after_tool_args` / `route_after_tools`），此前**零覆盖** |

以下断言均经探针实测确认（不是按代码推测），其中两条反直觉：

1. **fail closed 分支不扣 `remaining_steps`**：model 节点在
   `ModelCallError` / 步数耗尽两条分支直接 return，不含
   `remaining_steps` 键 → 值保持不变。
2. **完全空的 state 不报错**，`remaining_steps` 缺省为 0 → 直接
   steps_exhausted。旧用例期望 `ValueError`，那是 `TaskState` 有必填字段
   时的行为，现已不成立。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from administrative_agent.config import Settings, settings
from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.graph import (
    build_graph,
    route_after_model,
    route_after_tool_args,
    route_after_tools,
)
from administrative_agent.agent.model_client import (
    AgentDecision,
    SummaryResult,
    ToolArguments,
)
import importlib

# ⚠️ 必须 importlib 取模块：包 ``nodes/__init__.py`` 把 ``compression_node``
# 这个**子模块名**重新绑定成了同名**函数**，``import ...compression_node as X``
# 会拿到函数（父包属性优先），拿不到可 patch 的模块对象。
compression_node_module = importlib.import_module(
    "administrative_agent.agent.nodes.compression_node"
)
from administrative_agent.agent.nodes.compression_node import (
    _matching_tier,
    _occupancy,
    compression_node,
)
from administrative_agent.agent.nodes.model_node import (
    _filter_stale_details,
    _truncate_history,
    model_node,
)
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.state import (
    REMAINING_STEPS,
    AgentContext,
    ConversationState,
)
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.web.chat_streams import (
    EVENT_ARGS_INPUT_SUMMARY,
    EVENT_COMPRESSION_DECISION,
    EVENT_DECISION_INPUT_SUMMARY,
    EVENT_MEMORY_COMPRESSED,
    EVENT_MEMORY_COMPRESSION_FAILED,
)
from administrative_agent.web.stream_projection import project


def _state(**overrides: object) -> dict:
    """可执行的会话 State（`total=False`，按需给出最少字段即可）。"""
    base: dict = {"messages": [], "remaining_steps": REMAINING_STEPS}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# State 结构（8.4）
# ---------------------------------------------------------------------------


def test_remaining_steps_constant_is_25() -> None:
    """13.3：剩余步骤上限 25；model 节点入口 remaining<=0 即拦截本轮。"""
    assert REMAINING_STEPS == 25


def test_state_has_no_identity_keys() -> None:
    """8.4 末段：user_id/user_role/scope 不允许写入 State。"""
    for key in ("user_id", "user_role", "scope"):
        assert key not in ConversationState.__annotations__


def test_state_carries_conversation_recovery_fields() -> None:
    """8.5：`messages` 是唯一恢复单元；task_id 可空（纯对话轮次无任务）。"""
    annotations = ConversationState.__annotations__
    assert "messages" in annotations
    assert "task_id" in annotations
    assert "pending_confirmation" in annotations  # Phase 2 挂起确认卡
    # 任务式流水线的业务字段已不进 State
    assert "structured_intent" not in annotations


def test_agent_context_is_frozen_and_scope_aware() -> None:
    """D4 身份模型：frozen dataclass + scope 判定；身份不进 State。"""
    ctx = AgentContext(user_id="manager01", user_role="manager", scope=frozenset({"erp:query"}))
    assert ctx.user_id == "manager01"
    assert ctx.has_scope("erp:query")
    assert not ctx.has_scope("attendance:create")
    with pytest.raises(Exception):
        ctx.user_id = "dev01"  # frozen：不可变
    # 正向对照：身份三件套在 Context 上、不在 State 上
    assert not (set(AgentContext.__annotations__) & set(ConversationState.__annotations__))


# ---------------------------------------------------------------------------
# 路由函数（此前零覆盖）
# ---------------------------------------------------------------------------


def test_route_after_model_branches_on_next_action() -> None:
    """call_tool → tool_args（参数轮二次调用）；reply / needs_input / 缺省 → END。"""
    assert route_after_model({"pending_action": {"next_action": "call_tool"}}) == "tool_args"
    assert route_after_model({"pending_action": {"next_action": "reply"}}) == END
    assert route_after_model({"pending_action": {"next_action": "needs_input"}}) == END
    assert route_after_model({}) == END  # pending_action 缺省 → 不调用工具


def test_route_after_tool_args_routes_back_to_model_on_failure() -> None:
    """参数生成失败会清空 pending_action → 回 model 自纠（循环不终止）。"""
    assert route_after_tool_args({"pending_action": {"next_action": "call_tool"}}) == "tools"
    assert route_after_tool_args({"pending_action": None}) == "model"
    assert route_after_tool_args({}) == "model"


def test_route_after_tools_pending_confirmation_goes_to_confirm() -> None:
    """Phase 2：写工具挂起确认 → confirm 节点（interrupt 点）。"""
    state = _state(pending_confirmation={"summary": "确认创建订单"})
    assert route_after_tools(state) == "confirm"


def test_route_after_tools_pending_confirmation_beats_compression() -> None:
    """挂起防御优先于压缩判定：确认卡等待用户时绝不进 compression。

    `route_to_compression` 首行就是这道防御——若顺序颠倒，挂起的确认卡
    会因压缩重写 messages 而与 `payload_hash` 失配（7.3 变化失效）。

    对照组的构造要点：占用统计 `_msg_chars` 只认 LangChain 消息对象，
    传裸 dict（`{"role": "user", "content": ...}`）**占用恒为 0**，
    断言会假通过。这里用真实 `HumanMessage`，阈值从配置推导。
    """
    budget = Settings().dialogue_budget_chars
    huge = _state(messages=[HumanMessage(content="x" * int(budget * 0.71))])
    assert route_after_tools(huge) == "compression"  # 对照组：确实该压

    suspended = dict(huge, pending_confirmation={"summary": "确认创建订单"})
    assert route_after_tools(suspended) == "confirm"  # 挂起优先于压缩


# ---------------------------------------------------------------------------
# 最小图连通性与 fail closed
# ---------------------------------------------------------------------------


def test_graph_compiles_without_runtime() -> None:
    """`build_graph()` 无 runtime 可编译（worker.py 启动检查依赖此路径）。"""
    graph = build_graph()
    assert graph is not None
    assert type(graph).__name__ == "CompiledStateGraph"


def test_graph_fail_closed_when_model_unavailable() -> None:
    """未配模型客户端 → 空桩 raise → 追加错误说明并 reply，绝不静默。"""
    result = build_graph().invoke(_state())
    assert result["current_step"] == "model_failed"
    assert result["pending_action"] == {"next_action": "reply"}
    assert "未执行任何操作" in result["messages"][-1].content
    # fail closed 分支不扣步数（model 节点该分支不返回 remaining_steps）
    assert result["remaining_steps"] == REMAINING_STEPS


def test_graph_fail_closed_text_differs_when_task_already_executed() -> None:
    """2026-08-28 文案区分：本回合已落 ERP 时，说"未执行任何操作"是误导。

    `task_id` 非空即代表已执行过写工具（写工具执行时回填 State）。
    """
    result = build_graph().invoke(_state(task_id="task-already-run"))
    assert result["current_step"] == "model_failed"
    content = result["messages"][-1].content
    assert "任务已执行完成" in content
    assert "未执行任何操作" not in content


def test_graph_steps_exhausted_stops_the_turn() -> None:
    """13.3 低步数语义：remaining_steps 耗尽 → 追加提示后结束本轮。

    [2026-09-09 文案] 无 task_id（本轮未执行写工具）→
    "已达本轮执行上限，请发送新消息继续。"（新语义下同线程发新消息
    即恢复额度，不再引导开新会话）。
    """
    result = build_graph().invoke(_state(remaining_steps=0))
    assert result["current_step"] == "steps_exhausted"
    assert "已达本轮执行上限，请发送新消息继续。" in result["messages"][-1].content


def test_graph_steps_exhausted_text_differs_when_task_already_executed() -> None:
    """[2026-09-09 文案区分] 本轮已执行写工具（task_id 非空）时，耗尽文案
    说明结果可能已生效（同 model_unavailable 分支的 task_id 区分口径）。"""
    result = build_graph().invoke(
        _state(remaining_steps=0, task_id="task-already-run")
    )
    assert result["current_step"] == "steps_exhausted"
    content = result["messages"][-1].content
    assert "任务已执行完成" in content
    assert "请发送新消息继续询问结果" in content
    assert "请开启新对话" not in content


def test_graph_degrades_instead_of_raising_on_missing_fields() -> None:
    """`ConversationState` 是 `total=False`：缺字段不报 ValueError，降级处理。

    旧用例断言 `graph.invoke({"remaining_steps": 25})` 抛 ValueError——那是
    `TaskState` 有必填业务字段（task_id/operation_id/trace_id）时的行为。
    现 State 全部可选，缺失 `remaining_steps` 时按 0 处理 → steps_exhausted。
    按现状写断言：这里守的是「不崩、且告知用户」，不是「拒绝执行」。
    """
    result = build_graph().invoke({})
    assert result["current_step"] == "steps_exhausted"
    assert "已达本轮执行上限，请发送新消息继续。" in result["messages"][-1].content


# ---------------------------------------------------------------------------
# 压缩事件取证：memory_compressed 载荷必须含摘要正文与 index
# ---------------------------------------------------------------------------


class _FakeArchive:
    """压缩存档桩：只记调用，写不落盘（fail closed 判定要求 append 成功）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def append(self, *, thread_id: str, segment_no: int, messages: list) -> None:
        self.calls.append(
            {"thread_id": thread_id, "segment_no": segment_no, "messages": messages}
        )


class _FakeClient:
    """模型客户端桩：``generate_summary`` 回固定字面量（含超摘要上限的长正文）。"""

    def __init__(self, summary: str, index: str) -> None:
        self._summary = summary
        self._index = index

    def generate_summary(self, *, messages: list) -> SummaryResult:
        return SummaryResult(summary=self._summary, index=self._index)


def test_memory_compressed_event_carries_truncated_summary_and_index() -> None:
    """压缩事件必须携带**写入 state 的那份**摘要正文与 index（可取证）。

    背景：摘要是跨轮唯一事实载体（明细原文隔轮被删）；此前事件只发
    ``summary_chars``（长度，不是正文），「摘要把未确认写成已确认」「摘要在
    明细上算错」类失效在生产日志与 SSE 里看不到摘要写了什么，无法区分
    「摘要写错」与「模型读错」。

    断言三件事：
    1. 新增 ``summary`` 非空、``index`` 非空；
    2. ``summary`` 等于**截断后**写入 ``state["summaries"]`` 的那份
       （≠ ``result.summary`` 原始值——若误传原始值，超限正文会让本断言变红）；
    3. 既有 4 键（``segment_no`` / ``tier`` / ``summary_chars`` /
       ``archived_messages``）一字不动（防止补字段时误删/改名）。
    """
    budget = Settings().dialogue_budget_chars
    max_chars = Settings().summary_max_chars
    keep = Settings().memory_recent_messages
    # 8 条各约 0.05*budget → 占用约 0.4，越过最低档 0.30；8 > keep=5，
    # 待压段非空（全 ai 消息，不触发「最新 human 保留」前缀截断）
    msgs = [AIMessage(content="y" * int(budget * 0.05)) for _ in range(8)]
    state = _state(
        messages=msgs, summaries=[], compression_stats={}, summary_boundary=0
    )

    raw_index = "Q17 钣金订单查询"
    raw_summary = "已确认创建订单1000kg；" + "z" * (max_chars + 200)  # 故意超上限
    emitted: list[tuple[str, dict]] = []
    runtime = RuntimeBag(
        client=_FakeClient(raw_summary, raw_index),
        archive=_FakeArchive(),
        sink=lambda event_type, payload: emitted.append((event_type, payload)),
    )

    out = compression_node(state, runtime)

    events = [p for (t, p) in emitted if t == "memory_compressed"]
    assert len(events) == 1, "压缩成功路径必须恰发一条 memory_compressed"
    payload = events[0]

    # 2) 等于写进 state 的那份（截断后），且非空
    written = out["summaries"][-1]["summary"]
    assert written == raw_summary[:max_chars]
    assert payload["summary"] == written and payload["summary"]
    assert payload["index"] == raw_index and payload["index"]

    # 3) 既有 4 键保留（不改名/不删除）
    assert payload["segment_no"] == 1
    assert payload["tier"] == 30
    assert payload["summary_chars"] == len(written)
    assert payload["archived_messages"] == len(msgs) - keep


# ---------------------------------------------------------------------------
# 第 2 步 (a)-1：_occupancy 提取的锁（防重构漂移）
# ---------------------------------------------------------------------------


def test_occupancy_is_the_single_implementation(monkeypatch) -> None:
    """§6.3-1 **单一实现锁**：patch 掉 `_occupancy` 必须影响 `_matching_tier`。

    这是**唯一**能证明「真的复用了同一个纯函数」的判据——把 `_occupancy` 换成
    0.99 → `_matching_tier` 必命中 90 档（低档配额全满时才会走到 90，
    而 `_matching_tier` 里 `if tier == 90: return 90` 的分支又排在配额判定
    **之前**）。若
    `_matching_tier` 内联了自己的一份计算，patch 无效 → 走真实占用（此处远低于
    最低档）→ 首次比较即 break → 返回 None → **红**。

    「两个函数在多数 state 上返回值相等」是**假锁**：两份拷贝在未 patch 时
    天然相等。
    """
    monkeypatch.setattr(compression_node_module, "_occupancy", lambda s: 0.99)
    # 低档配额打满，强制逐级查找走到 90
    full = {str(t): settings.max_compressions_per_tier for t in (30, 60)}
    state = _state(messages=[AIMessage(content="x")], compression_stats=full)
    assert _matching_tier(state) == 90


def test_occupancy_zero_budget_does_not_raise(monkeypatch) -> None:
    """§6.3-2 零除锁：`dialogue_budget_chars=0`（env 可达）不得抛，且落 0.0。

    去掉 `_occupancy` 里的 `if budget and messages else 0.0` 守卫即
    ``ZeroDivisionError``——这是本批最容易丢的一行。
    """
    monkeypatch.setattr(settings, "dialogue_budget_chars", 0)
    state = _state(messages=[AIMessage(content="占用非零")])
    assert _occupancy(state) == 0.0
    assert _matching_tier(state) is None


# ---------------------------------------------------------------------------
# 第 2 步 (a)-2：compression_decision 判定台账（5 条出口各恰一条）
# ---------------------------------------------------------------------------


class _FailingSummaryClient:
    """摘要模型桩：调用即抛（覆盖「模型压缩失败」出口）。"""

    def generate_summary(self, *, messages: list) -> SummaryResult:
        raise RuntimeError("摘要模型不可用")


def _budget() -> int:
    return Settings().dialogue_budget_chars


def _occupied_messages(n: int = 8, ratio: float = 0.05) -> list:
    """n 条各约 ``ratio * budget`` 的 ai 消息（占用越过最低档，待压段非空）。"""
    return [AIMessage(content="y" * int(_budget() * ratio)) for _ in range(n)]


def _run_compression(state: dict, *, archive: object, client: object):
    emitted: list[tuple[str, dict]] = []
    runtime = RuntimeBag(
        archive=archive,
        client=client,
        sink=lambda t, p: emitted.append((t, p)),
    )
    return compression_node(state, runtime), emitted


def _expected_occupancy_pct(messages: list) -> int:
    """独立复算占用百分比（用 ``len(content)``，不抄实现的 ``_msg_chars``）。"""
    return int(sum(len(m.content) for m in messages) / _budget() * 100)


def _exit_no_segment():
    # 消息数 <= memory_recent_messages → seg 为空
    state = _state(messages=[AIMessage(content="a"), AIMessage(content="b")])
    out, emitted = _run_compression(state, archive=None, client=None)
    return out, emitted, state, "no_segment", True


def _exit_tier_gone():
    # seg 非空（6 > keep=5）**且** 占用低于最低档（各 1 字符，占用≈0）→ _matching_tier None。
    # ⚠️ 这个边界很窄：改 compression_tiers / dialogue_budget_chars 会让本用例变红，是好事。
    msgs = [AIMessage(content="x") for _ in range(Settings().memory_recent_messages + 1)]
    state = _state(messages=msgs)
    out, emitted = _run_compression(state, archive=None, client=None)
    return out, emitted, state, "tier_gone", True


def _exit_archive_failed():
    state = _state(messages=_occupied_messages())
    out, emitted = _run_compression(state, archive=None, client=None)  # archive 未接线
    return out, emitted, state, "failed", False


def _exit_model_failed():
    state = _state(messages=_occupied_messages())
    out, emitted = _run_compression(
        state, archive=_FakeArchive(), client=_FailingSummaryClient()
    )
    return out, emitted, state, "failed", False


def _exit_compressed():
    state = _state(messages=_occupied_messages())
    out, emitted = _run_compression(
        state, archive=_FakeArchive(), client=_FakeClient("摘要正文", "Q1 索引")
    )
    return out, emitted, state, "compressed", False


_EXIT_BUILDERS = {
    "no_segment": _exit_no_segment,
    "tier_gone": _exit_tier_gone,
    "archive_failed": _exit_archive_failed,
    "model_failed": _exit_model_failed,
    "compressed": _exit_compressed,
}


@pytest.mark.parametrize("case", sorted(_EXIT_BUILDERS))
def test_compression_decision_exactly_once_per_exit(case) -> None:
    """§6.2 完备性：5 条出口**每条恰好一条** ``compression_decision``。

    ⚠️ no-op 出口**必须同时断言 ``out == {}``**（见参数 ``expect_noop``）——
    只断言「有一条事件」的话，把 ``return {}`` 改成正常压缩路径**也会绿**
    （``decision`` 变成 ``compressed``，不比对值就发现不了）。

    ⚠️ 按**类型过滤**后再数（``[p for t, p in emitted if t == EVENT]``）——
    写成 ``len(emitted) == 1`` 会让「新增事件」这个动作本身变红，是把测试
    写反了。
    """
    out, emitted, state, expected_decision, expect_noop = _EXIT_BUILDERS[case]()

    decisions = [p for t, p in emitted if t == EVENT_COMPRESSION_DECISION]
    assert len(decisions) == 1, (case, emitted)
    payload = decisions[0]

    # (iii) 值精确断言（用 ==，不用 != "other"）；占用在测试里独立复算
    assert payload["decision"] == expected_decision, payload
    assert payload["occupancy_pct"] == _expected_occupancy_pct(state["messages"])
    assert payload["messages_count"] == len(state["messages"])
    assert payload["segment_messages"] == len(
        compression_node_module._pending_segment(state["messages"])
    )

    # (i) no-op 出口确实走了 no-op 分支
    if expect_noop:
        assert out == {}, (case, out)
    else:
        assert out != {}


@pytest.mark.parametrize("case", ["archive_failed", "model_failed"])
def test_compression_decision_precedes_failed_result(case) -> None:
    """§6.2 顺序锁：失败出口的 ``compression_decision`` 必须在失败结果**之前**。

    否则时间线会读成「失败了，然后决定失败」；放前面则统一读成 **决定 → 结果**。
    """
    _out, emitted, _state_, _d, _n = _EXIT_BUILDERS[case]()
    types = [t for t, _ in emitted]
    assert types.index(EVENT_COMPRESSION_DECISION) < types.index(
        EVENT_MEMORY_COMPRESSION_FAILED
    )


def test_compression_decision_precedes_success() -> None:
    """成功轮同样：决定行在 ``memory_compressed`` 之前。"""
    _out, emitted, _state_, _d, _n = _exit_compressed()
    types = [t for t, _ in emitted]
    assert types.index(EVENT_COMPRESSION_DECISION) < types.index(
        EVENT_MEMORY_COMPRESSED
    )


# ---------------------------------------------------------------------------
# 第 2 步 (b)：模型输入摘要（决策轮 / 参数轮）
# ---------------------------------------------------------------------------


class _StubDecisionClient:
    """决策客户端桩（非 ModelDecisionClient → 不挂流式回调）。"""

    def __init__(self, decision: AgentDecision) -> None:
        self._decision = decision

    def generate_decision(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: object
    ) -> AgentDecision:
        return self._decision


class _StubArgsClient:
    """参数客户端桩：回填 node 期望的工具名（避免一致性检查报错）。"""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: object
    ) -> ToolArguments:
        return ToolArguments(tool_name=self._tool_name, tool_arguments={})


def _emitting_runtime(**kwargs):
    emitted: list[tuple[str, dict]] = []
    kwargs["sink"] = lambda t, p: emitted.append((t, p))
    return RuntimeBag(**kwargs), emitted


def _summary_state(active_skill: str | None) -> dict:
    """含一条 detail 明细的 4 消息状态（驱逐数非零）+ 2 段摘要。"""
    msgs = [
        AIMessage(content="a"),
        ToolMessage(
            content="只读明细", tool_call_id="c1", additional_kwargs={"detail": True}
        ),
        HumanMessage(content="问题"),
        AIMessage(content="b"),
    ]
    state = _state(
        messages=msgs,
        remaining_steps=5,
        summaries=[{"segment_no": 1}, {"segment_no": 2}],
    )
    if active_skill is not None:
        state["active_skill"] = active_skill
    return state


@pytest.mark.parametrize(
    "active_skill,expected",
    [("SK.financial_report", "SK.financial_report"), (None, "none"), ("幻觉流程串", "other")],
)
def test_decision_input_summary_counts_and_skill(active_skill, expected) -> None:
    """§6.4：决策轮摘要键值——**同层复算，不硬编码条数**。

    硬编码条数会在 ``memory_recent_messages`` 变更时静默变成错值（本仓
    「断言与被改变量不同层」的教训）。``active_skill`` 三态经 ``project`` 断言。
    """
    state = _summary_state(active_skill)
    raw = list(state["messages"])
    visible = _filter_stale_details(raw)
    sent, _ = _truncate_history(visible)

    runtime, emitted = _emitting_runtime(
        client=_StubDecisionClient(
            AgentDecision(reply_text="好", next_action="reply")
        )
    )
    model_node(state, runtime)

    payloads = [p for t, p in emitted if t == EVENT_DECISION_INPUT_SUMMARY]
    assert len(payloads) == 1, emitted
    payload = payloads[0]
    assert payload["history_raw"] == len(raw)
    assert payload["history_sent"] == len(sent)
    assert payload["evicted_details"] == len(raw) - len(visible)
    assert payload["evicted_details"] == 1  # 该构造确实驱逐了一条明细
    assert payload["summaries_count"] == len(state["summaries"])
    # active_skill 三态经投影层收敛（emit 的是原始值，可由 project 归一）
    assert (
        project(EVENT_DECISION_INPUT_SUMMARY, payload).payload["active_skill"]
        == expected
    )


@pytest.mark.parametrize(
    "active_skill,expected",
    [("SK.financial_report", "SK.financial_report"), (None, "none"), ("幻觉流程串", "other")],
)
def test_args_input_summary_counts_and_skill(active_skill, expected) -> None:
    """§6.4：参数轮摘要（3 键 + active_skill）；参数轮**同样**覆盖三态。"""
    state = _summary_state(active_skill)
    state["pending_action"] = {"tool_name": "erp_query"}
    raw = list(state["messages"])
    visible = _filter_stale_details(raw)
    sent, _ = _truncate_history(visible)

    runtime, emitted = _emitting_runtime(
        tools=TOOL_REGISTRY, client=_StubArgsClient("erp_query")
    )
    tool_args_node(state, runtime)

    payloads = [p for t, p in emitted if t == EVENT_ARGS_INPUT_SUMMARY]
    assert len(payloads) == 1, emitted
    payload = payloads[0]
    assert payload["history_raw"] == len(raw)
    assert payload["history_sent"] == len(sent)
    assert payload["evicted_details"] == len(raw) - len(visible)
    assert set(payload) == {"history_raw", "history_sent", "evicted_details", "active_skill"}
    assert (
        project(EVENT_ARGS_INPUT_SUMMARY, payload).payload["active_skill"] == expected
    )
