"""记忆压缩节点（2026-08-24 定稿）：START 后条件边判定 + 压缩执行。

- route_to_compression：纯函数判定（挂起防御 → 占用 → 逐级查找达标且配额未满的档位，
  90 档豁免配额=强制压缩）；返回 "compression" 或 "model"；
- compression_node：待压段 = summary_boundary 之后全部消息（保留最近
  memory_recent_messages 条不压）→ 存档（fail closed：存档失败不压缩）→ 模型压缩
  （SummaryResult{summary, index}）→ summaries 追加（超总预算丢最老）→ boundary/
  compression_stats 更新 → 发 memory_compressed 事件；
- 待压段内若含最新一条 role=human 消息（工具循环中间
  压缩场景：human → ai → tool → …，human 位于中部），只归档 human 之前的
  前缀——最新用户问题（含数字/日期细节）及其后消息全部保留，模型下一轮仍
  看原文而非摘要；RemoveMessage 仍是前缀删除（归档段恒为原列表前缀），
  尾部连续性不变式保持。
- 失败降级：存档失败或模型调用失败 → 不压缩、不动 boundary/summaries/stats，改用
  _truncate_history drop-front 兜底 + 发 memory_compression_failed 事件（不阻断对话）。

待压段从 State 移除用 ``RemoveMessage``（LangGraph 官方摘要模式）：messages 通道的
add_messages reducer 是"按 ID 合并"语义——直接返回尾段子集不会删除已归档消息
（实测验证，2026-08-24），必须显式 RemoveMessage 才真正压缩上下文。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph.message import RemoveMessage

from administrative_agent.agent.configuration import RuntimeBag, _emit
from administrative_agent.agent.nodes.model_node import _msg_chars, _truncate_history
from administrative_agent.agent.state import ConversationState
from administrative_agent.config import settings

# 压缩事件为 SSE 透传事件（前端未知事件类型忽略；常量定义于 chat_streams.py）
from administrative_agent.web.chat_streams import (
    EVENT_COMPRESSION_DECISION,
    EVENT_MEMORY_COMPRESSED,
    EVENT_MEMORY_COMPRESSION_FAILED,
)


def _occupancy(state: ConversationState) -> float:
    """对话占用**比值**（非百分比；``>1.0`` 表示消息总量超预算）。

    ⚠️ 三个承重点（勿改）：
    1. ``if budget and messages else 0.0`` 守卫是承重的——``budget``
       （``settings.dialogue_budget_chars``）**可经 env 覆盖为 0**，去掉守卫
       即 ``ZeroDivisionError``；
    2. 返回**比值**而非百分比——``_matching_tier`` 的比较是
       ``occupancy < tier / 100``，改成百分比而忘同步比较就是静默行为变更；
       百分比只在 emit 点做 ``int(occupancy * 100)``；
    3. 必须继续用 ``_msg_chars``（非 str 按 400 计），别改用 ``len()``。

    ``_matching_tier`` 与压缩判定埋点共用（第 2 步 (a)：占用真源唯一）。
    """
    messages = state.get("messages") or []
    budget = settings.dialogue_budget_chars
    return (
        sum(_msg_chars(m) for m in messages) / budget if budget and messages else 0.0
    )


def _matching_tier(state: ConversationState) -> int | None:
    """逐级查找第一个"占用达标且配额未满"的档位（90 档豁免配额=强制）。

    返回触发档位（int）；无达标档位 → None。route_to_compression 与
    compression_node 共用，避免"逐级查找"判定逻辑重复。
    """
    occupancy = _occupancy(state)
    stats = state.get("compression_stats") or {}
    for tier in settings.compression_tiers:
        if occupancy < tier / 100:
            break
        if tier == 90:
            return 90  # 90 档豁免配额 = 强制压缩
        if stats.get(str(tier), 0) < settings.max_compressions_per_tier:
            return tier
    return None


def _pending_segment(messages: list) -> list:
    """待压段：保留最近 ``memory_recent_messages`` 条，其余为待压前缀。

    [2026-08-28 D2] 待压段内若含最新一条 ``role=human`` 消息（工具循环中间
    压缩场景：human → ai → tool → …，human 位于中部），只取其前的前缀——
    最新用户问题及其后消息全部保留（``messages[:h]`` 仍是原列表前缀，尾部
    连续性不变式保持）。``compression_node`` 与 ``_emit_decision`` 共用本函数
    （判定台账里的 ``segment_messages`` 与真正归档的段**同源**，不会漂移）。
    """
    keep = settings.memory_recent_messages
    seg = messages[: len(messages) - keep] if len(messages) > keep else []
    if seg:
        latest_human = next(
            (
                i for i in range(len(messages) - 1, -1, -1)
                if getattr(messages[i], "type", "") == "human"
            ),
            None,
        )
        if latest_human is not None and latest_human < len(seg):
            seg = seg[:latest_human]
    return seg


def _emit_decision(
    runtime: RuntimeBag, state: ConversationState, decision: str
) -> None:
    """发一条 ``compression_decision``（压缩判定台账；**每次进节点恰一条**）。

    ⚠️ **必须是每条出口的第一条 emit**：若照原样追加在失败出口之后，时间线
    会读成「失败了，然后决定失败」；放前面则统一读成 **决定 → 结果**。

    payload 四键形状**只在定义一处**（5 条出口共用，避免字面量漂移）：
    ``decision``（走 ``_enum_of`` 闭集，未知 → ``other``）、``occupancy_pct``
    （``int(比值 * 100)``，可 >100）、``messages_count``、``segment_messages``
    （与 ``compression_node`` 同源，见 ``_pending_segment``）。
    """
    messages = list(state.get("messages") or [])
    _emit(
        runtime,
        EVENT_COMPRESSION_DECISION,
        {
            "decision": decision,
            "occupancy_pct": int(_occupancy(state) * 100),
            "messages_count": len(messages),
            "segment_messages": len(_pending_segment(messages)),
        },
    )


def route_to_compression(state: ConversationState) -> str:
    """START 后条件边：挂起防御（pending_confirmation → model）+ 占用达标且
    配额未满（_matching_tier 非 None）→ "compression"；否则 "model"。"""
    if state.get("pending_confirmation"):
        return "model"
    if _matching_tier(state) is not None:
        return "compression"
    return "model"


def compression_node(state: ConversationState, runtime: RuntimeBag) -> dict[str, Any]:
    """压缩执行节点：待压段 → 存档 → 模型压缩 → summaries/boundary/stats 更新。

    [2026-08-28 D2 修复] 待压段计算排除最新一条 role=human 消息：工具循环
    中间压缩时（messages 超 5 条且占用达标，human → ai → tool → … 的 human
    位于中部），压缩归档会把用户最新问题变成摘要——数字/日期细节丢失，
    模型下一轮只能看摘要。归档段改为"最新 human 之前的前缀"（human 及其后
    全部保留）；human 恰在保留区（最后 memory_recent_messages 条）时行为
    不变。RemoveMessage 仍为前缀删除，尾部连续性不变式保持。

    失败降级（不阻断对话）：存档失败或模型调用失败 → 不压缩、不动
    boundary/summaries/stats，改用 _truncate_history drop-front 兜底 +
    memory_compression_failed 事件（图继续进 model 节点正常回复）。
    """
    messages = list(state.get("messages") or [])
    seg = _pending_segment(messages)
    if not seg:
        # no-op 出口 1/2：待压段为空（含仅剩最新 human 被排除）→ 直接进 model。
        # 留痕（判定台账，第 2 步 (a)）：即使不压缩也发一条 compression_decision，
        # 否则「明细/上下文为什么没被压缩」在机制层无法被回答。
        _emit_decision(runtime, state, "no_segment")
        return {}
    tier = _matching_tier(state)
    if tier is None:
        # no-op 出口 2/2：触发档位消失（并发/重放边界）→ 直接进 model。
        _emit_decision(runtime, state, "tier_gone")
        return {}

    summaries = list(state.get("summaries") or [])
    stats = dict(state.get("compression_stats") or {})
    boundary = int(state.get("summary_boundary") or 0)
    seg_no = max((s.get("segment_no", 0) for s in summaries), default=0) + 1

    # 1) 存档（fail closed：失败 → 降级，不压缩）
    try:
        archive = runtime.archive
        if archive is None:
            raise RuntimeError("archive 未接线")
        archive.append(
            thread_id=runtime.context.thread_id if runtime.context else "",
            segment_no=seg_no,
            messages=[_text_message(m) for m in seg],
        )
    except Exception as exc:
        _emit_decision(runtime, state, "failed")  # 决定行在前的第一条 emit
        _emit(runtime, EVENT_MEMORY_COMPRESSION_FAILED, {"error": str(exc)})
        # 降级：drop-front 兜底（add_messages 按 ID 合并，直接返回子集不会删除
        # 最早消息——显式 RemoveMessage，与正路径同语义）
        truncated, _ = _truncate_history(messages)
        removed = [
            RemoveMessage(id=m.id)
            for m in messages[: len(messages) - len(truncated)]
        ]
        return {"messages": removed, "current_step": "memory_compression_failed"}

    # 2) 模型压缩（失败 → 同降级，drop-front）
    try:
        result = runtime.client.generate_summary(messages=_as_pairs(seg))
    except Exception as exc:
        _emit_decision(runtime, state, "failed")  # 决定行在前的第一条 emit
        _emit(runtime, EVENT_MEMORY_COMPRESSION_FAILED, {"error": str(exc)})
        # 降级：drop-front 兜底（add_messages 按 ID 合并，直接返回子集不会删除
        # 最早消息——显式 RemoveMessage，与正路径同语义）
        truncated, _ = _truncate_history(messages)
        removed = [
            RemoveMessage(id=m.id)
            for m in messages[: len(messages) - len(truncated)]
        ]
        return {"messages": removed, "current_step": "memory_compression_failed"}

    summary_text = (result.summary or "")[: settings.summary_max_chars]

    # 3) summaries 追加 + 丢最老（总摘要字符超 summary_total_budget 时移除最老摘要；
    #    目录条目随之消失，存档仍在——可用 memory_lookup 按段号查询）
    new_entry = {
        "segment_no": seg_no,
        "tier": tier,
        "summary": summary_text,
        "index": result.index,
    }
    summaries.append(new_entry)
    budget_chars = int(settings.dialogue_budget_chars * settings.summary_total_budget)
    while (
        sum(len(s.get("summary", "")) for s in summaries) > budget_chars
        and len(summaries) > 1
    ):
        summaries.pop(0)

    # 4) stats/boundary 更新 + 事件
    stats[str(tier)] = stats.get(str(tier), 0) + 1
    # 决定行在前的第一条 emit（成功轮也发——判定台账完整；memory_compressed
    # 不带 occupancy/messages_count/segment_messages，故本行是净增诊断维度，
    # 并换来「进入节点 ⇒ 恰好一条 compression_decision」这条完备性不变量）
    _emit_decision(runtime, state, "compressed")
    # [2026-09-17 压缩取证] 事件载荷补 index 与 summary 正文：摘要是跨轮唯一
    # 事实载体（明细原文隔轮被删），此前事件只发 summary_chars（长度，非正文）
    # ——「摘要把未确认写成已确认」「摘要在明细上算错」类失效在生产日志与 SSE
    # 事件里看不到摘要写了什么，无法区分「摘要写错」与「模型读错」。summary 用
    # 已截断、真正写进 state 的那份（summary_text），与 result.summary 原始值
    # 可能不同；index 为回查检索标识（短、最有诊断价值）。纯附加键，不改行为。
    _emit(
        runtime,
        EVENT_MEMORY_COMPRESSED,
        {
            "segment_no": seg_no,
            "tier": tier,
            "summary_chars": len(summary_text),
            "archived_messages": len(seg),
            "index": result.index,
            "summary": summary_text,
        },
    )
    return {
        # RemoveMessage 显式移除已归档段（add_messages 按 ID 合并，仅传尾段不会删除）
        "messages": [RemoveMessage(id=m.id) for m in seg],
        "summaries": summaries,
        "summary_boundary": boundary + len(seg),
        "compression_stats": stats,
        "current_step": "memory_compressed",
    }


def _text_message(message: Any) -> dict:
    """LangChain 消息 → {"role": type, "content": str(content)}（存档文本形态）。

    content 非 str 转 str（与 routes_chat._history_message_out 同口径）。
    """
    return {
        "role": getattr(message, "type", "unknown"),
        "content": str(getattr(message, "content", "")),
    }


def _as_pairs(messages: list) -> list:
    """LangChain 消息 → ("human"/"ai", content) 元组列表（压缩调用输入）。

    type 映射：human → "human"，其余（ai/tool/system 等）→ "ai"——tool
    消息在压缩时视为信息内容（压缩调用不做工具调用）。
    """
    out: list[tuple[str, str]] = []
    for message in messages:
        role = "human" if getattr(message, "type", "") == "human" else "ai"
        content = getattr(message, "content", "")
        out.append((role, content if isinstance(content, str) else str(content)))
    return out
