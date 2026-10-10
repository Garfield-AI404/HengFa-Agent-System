"""会话循环图（Phase 2：写工具 interrupt 确认流）。

图装配层（LangGraph 惯例分层）——本模块只保留
build_graph + 两条条件边路由 + 常量；节点实现位于 nodes/（model_node/
tools_node/confirm_node），运行时装配与共享工具位于 configuration.py
（RuntimeBag/_UnconfiguredDecisionClient/_emit/_tool_ctx/_new_tool_call_id），
工具注册表位于 tools/。文件末尾 ``from .configuration import RuntimeBag``
re-export 保持 ``from administrative_agent.agent.graph import RuntimeBag``
外部导入路径不变（routes_chat/worker）。

目标架构（docs/plan/harness-phase0-report.md / 方案 §A）：
- **model 节点**（nodes/model_node.py）：读 State.messages 全量历史 →
  组装 system prompt（BASE_SYSTEM_PROMPT + 工具索引表 + 已加载规则包 +
  json_mode 字段说明，schema 说明经 ``build_select_schema`` 动态生成）→
  ``ModelDecisionClient.generate_decision`` → 追加 AIMessage 并输出
  ``pending_action``；**40k token 预算**（``MAX_MODEL_INPUT_CHARS``，
  ~3.5 字符/token，drop-front 截断）防历史无限膨胀；成功路径发射
  ``agent_reply`` 事件（前端 SSE 展示最终回复）；
- **记忆压缩（2026-08-24 定稿）**：START 后条件边 ``route_to_compression``
  （挂起防御 + 上下文占用达档位阈值 30/60/90% 且该档配额未满——每档 2 次，
  90 档豁免配额=强制）路由到 compression 节点：待压段（保留最近 5 条）
  全量存档（chat_archive，memory_lookup 查询源）+ 模型压缩（SummaryResult
  {摘要, 目录}，json_mode + max_tokens 2000）→ 摘要/目录注入后续 system
  prompt（不占 messages）+ 已归档消息 RemoveMessage 移出 State；失败降级
  （存档失败或压缩调用失败）→ 不压缩、改用 drop-front 截断兜底 +
  memory_compression_failed 事件（不阻断对话）；
- **条件边**：``pending_action.next_action == "call_tool"`` → tool_args
  （参数轮二次调用，见下），否则 END（reply/needs_input 均直接结束本轮）；
- **tool_args 节点**（nodes/tool_args_node.py）：
  模型① 选定工具名后，注入该工具完整参数 JSON Schema + 规则包族正文
  （``spec.policy_family`` → 各包正文），二次模型调用生成参数
  （``ToolArguments``，json_mode）→ 填充 ``pending_action.tool_arguments``
  （保留其余字段）→ 路由 tools 节点零改动继续；失败（未知工具/生成失败/
  工具名不一致）→ 错误 ToolMessage + ``pending_action`` 清空 → 回 model
  自纠（循环不终止）；
- **tools 节点**（nodes/tools_node.py）：按 tool_name 分派注册表工具
  （pydantic 确定性校验 → 写工具分支 / 非写工具直接 run；参数已由
  tool_args 节点生成填充到 ``pending_action.tool_arguments``，本节点
  零改动继续执行）。**写工具分支
  （Phase 2）**：
  1. scope 校验（8.4/10.5：manager 角色 + 2.4 最小 scope，服务端注入）；
  2. 优雅停机检查（15.3 步骤 7 / D25：``lifecycle.accepting_new_writes()``）；
  3. 任务服务缺失 → harness_misconfigured（fail closed）；
  4. ``_ensure_task``：state.task_id 复用（13.2）或新建写任务
     （NewTaskSpec：thread_id/actor/role/duty_code/原始消息/结构化意图/
     is_write=True——D6 自动取串行锁）；
  5. ``_wait_for_execution_right``：pending 排队（8.3 第 3 条）时
     100ms 轮询任务状态（30s 上限 + 停机检查），晋升 running 后继续；
  6. ``spec.prepare``：只读前置 + 确认卡构造（失败 → mark_failed +
     错误 ToolMessage，fail closed 不落 ERP）；
  7. ``request_confirmation(task_id, payload_hash)`` → 任务
     waiting_confirmation + ``pending_confirmation``（确认卡六要素 +
     payload_hash + 执行上下文 execution）写入 State 并 checkpoint，
     路由到 confirm 节点；
- **confirm 节点**（nodes/confirm_node.py，Phase 2 新增）：持有
  ``interrupt`` 的确认点。首轮执行时 interrupt 抛 GraphInterrupt → 图挂起
  （发射 ``confirmation_requested`` 事件）；恢复（POST /api/v1/chat/confirm
  → Command(resume={"approved", "payload_hash"})）时重执行本节点，
  interrupt 返回决策值：
  * payload_hash 与卡片不一致（7.3 变化失效）→ 发射 ``re_requested``
    并再次 interrupt 重挂（等待重新确认）；
  * approved=False → ``tasks.confirm(approved=False)``（cancelled，8.1）
    + 拒绝 ToolMessage + ``tool_result`` 事件；
  * approved=True → ``tasks.confirm(approved=True)``（running）→
    ``spec.run(ctx, args=None, task_id, execution)`` 接真实 Executor
    → ToolMessage(json.dumps(summary)) + ``tool_result`` 事件 +
    policy_keys 追加 + pending_confirmation 清空；
- **中断语义（实测验证，LangGraph 1.2.11）**：resume 值与节点内 interrupt
  按调用顺序匹配——hash 失效重挂后再次恢复时，首个 interrupt 返回旧值、
  新值在第二个 interrupt 位置消费（多一轮循环 + 一个多余的 re_requested
  事件，接受为 cosmetic，见 tests/agent/test_interrupt.py）；graph.stream
  在重挂时**不** raise（返回部分 updates），confirm 端点用 stream 因此
  hash 失效路径正常返回 200，客户端从流感知 re_requested；
- **fail closed**：模型不可用/调用失败 → 追加 AIMessage 错误说明 +
  reply + ``agent_error`` 事件（绝不静默）；remaining_steps 耗尽
  （13.3：预算按每条用户消息重置为 REMAINING_STEPS=25、model 每轮
  决策 -1，耗尽只可能来自单条消息内的失控循环——防死循环上限）→
  追加耗尽文案（按 task_id 区分，见 model_node）后 END；未知工具/
  非法参数/执行异常 → 错误 ToolMessage（模型下轮自纠）；
- **runtime 闭包**：``RuntimeBag``（configuration.py）携带 client/tools/
  tasks/audit/context/sink/metadata/mcp_client/lock；runtime=None 时用空
  client 桩（可编译——worker.py 启动检查 build_graph(checkpointer) 兼容；
  调用时 fail closed）。

Phase 2 边界：submit_message（旧流水线）不重写（Phase 3）；reconcile
prepare 因无 plan/review 上下文 fail closed（Phase 3 接入 D19/D20）。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from administrative_agent.agent.configuration import RuntimeBag, _UnconfiguredDecisionClient
from administrative_agent.agent.nodes import (
    compression_node,
    confirm_node,
    model_node,
    tool_args_node,
    tools_node,
)
from administrative_agent.agent.nodes.compression_node import route_to_compression
from administrative_agent.agent.state import ConversationState
from administrative_agent.agent.tools import TOOL_REGISTRY

# ---------------------------------------------------------------------------
# 路由与编译
# ---------------------------------------------------------------------------


def route_after_model(state: ConversationState) -> str:
    """条件边：next_action=call_tool → tool_args（参数轮二次调用）；
    否则（reply/needs_input）END。"""
    pending = state.get("pending_action") or {}
    if pending.get("next_action") == "call_tool":
        return "tool_args"
    return END


def route_after_tool_args(state: ConversationState) -> str:
    """条件边：pending_action 非空 → tools（参数已生成，分派执行）；
    否则（失败清空 pending_action）→ model（模型自纠）。"""
    if state.get("pending_action"):
        return "tools"
    return "model"


def route_after_tools(state: ConversationState) -> str:
    """条件边：pending_confirmation 非空 → confirm（挂起确认卡）；否则过
    压缩判定——达标且配额未满 → compression，未达标 → model。

    [2026-08-28 两级数据访问 1c] 压缩判定扩展：tools → model 与
    confirm → model 回环边也经 route_to_compression（原仅 START 条件
    边判定）。工具循环期大体积 ToolMessage（查询结果等）进上下文后即可
    触发压缩，不必等下一轮 START。
    """
    if state.get("pending_confirmation"):
        return "confirm"
    return route_to_compression(state)


def build_graph(
    checkpointer: Any | None = None, runtime: RuntimeBag | None = None
) -> Any:
    """构造并编译会话循环图（Phase 2：model → tool_args → tools → (confirm) → model）。

    - ``checkpointer`` 非空时编译进图：每个节点（super-step）执行后自动
      保存 checkpoint（8.5；对话历史以 thread_id 粒度整段重放）；挂起的
      interrupt（confirm 节点）同样持久化——恢复经
      ``CheckpointService.new_graph_config(thread_id)`` + Command(resume)；
    - ``runtime`` 为 RuntimeBag 闭包；None → 空 client 桩（可编译，
      worker.py 启动检查 build_graph(checkpointer=saver) 兼容；调用时
      fail closed）。
    """
    bag = runtime or RuntimeBag()
    if bag.client is None:
        bag.client = _UnconfiguredDecisionClient()
    if bag.tools is None:
        bag.tools = TOOL_REGISTRY

    graph = StateGraph(ConversationState)
    graph.add_node("model", lambda state: model_node(state, bag))
    graph.add_node("tool_args", lambda state: tool_args_node(state, bag))
    graph.add_node("tools", lambda state: tools_node(state, bag))
    graph.add_node("confirm", lambda state: confirm_node(state, bag))
    graph.add_node("compression", lambda state: compression_node(state, bag))
    # [2026-08-24 定稿] START 条件边：记忆压缩判定（达标且配额未满 → compression）
    graph.add_conditional_edges(
        START,
        route_to_compression,
        {"compression": "compression", "model": "model"},
    )
    # [2026-08-28 两级数据访问 1c] tools/confirm → model 回环边也过压缩判定
    # （route_after_tools 的 model 分支改由 route_to_compression 裁决）——
    # 不新增节点，直接复用现有 compression 节点；compression 后固定
    # add_edge("compression", "model")，判定边本身不会循环。
    # 死循环确认：compression no-op（待压段为空）返回 {} 直接进 model；
    # 90 档豁免配额=强制——若压缩后仍达标（最近 5 条大消息），每次回 model
    # 前都触发 → compression no-op → model，可接受（不阻断、不无限循环，
    # 因为 compression 永不路由回自己；有真实待压段时每轮压缩都移出消息，
    # 占用随之下降）。remaining_steps 只由 model 节点消费（预算按每条
    # 用户消息重置、confirmation resume 不重置，见 state.py REMAINING_STEPS），
    # compression 不改步数。
    graph.add_edge("compression", "model")
    graph.add_conditional_edges("model", route_after_model, {"tool_args": "tool_args", END: END})
    graph.add_conditional_edges(
        "tool_args", route_after_tool_args, {"tools": "tools", "model": "model"}
    )
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"confirm": "confirm", "compression": "compression", "model": "model"},
    )
    graph.add_conditional_edges(
        "confirm",
        route_after_tools,
        {"confirm": "confirm", "compression": "compression", "model": "model"},
    )
    return graph.compile(checkpointer=checkpointer)


from .configuration import RuntimeBag  # noqa: E402,F401  # re-export 兼容（routes_chat/worker 导入路径不变）
