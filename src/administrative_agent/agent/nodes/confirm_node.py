"""confirm 节点：持有 ``interrupt`` 的确认点。

由 agent/graph.py 拆分后位于 nodes/confirm_node.py——
首轮执行时 interrupt 抛 GraphInterrupt → 图挂起（发射
``confirmation_requested`` 事件）；恢复（POST /api/v1/chat/confirm →
Command(resume={"approved", "payload_hash"})）时重执行本节点，
interrupt 返回决策值。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from administrative_agent.agent.configuration import (
    RuntimeBag,
    _emit,
    _mark_audit_write_failed,
    _new_tool_call_id,
    _tool_ctx,
)
from administrative_agent.agent.nodes.tools_node import _bump_retry, _retry_key
from administrative_agent.agent.state import ConversationState
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_ERROR,
    EVENT_CONFIRMATION_REQUESTED,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_RUNNING,
)


def confirm_node(state: ConversationState, runtime: RuntimeBag) -> dict[str, Any]:
    """confirm 节点：持有 interrupt 的轻确认点（Phase 2）。

    首轮执行（tools → confirm）：``interrupt(card)`` 抛 GraphInterrupt →
    图挂起（任务已在 tools 节点 request_confirmation 为 waiting_confirmation，
    确认卡在 checkpoint 中，发射 ``confirmation_requested`` 事件）。

    恢复执行（Command(resume={"approved", "payload_hash"})）：节点重执行，
    本节点只持有 interrupt 与决策处理（建任务/准备/request_confirmation 都
    在 tools 节点完成——重执行不重复触发）：
    - payload_hash 与卡片不一致（7.3 内容变化 → 原确认失效）→ 发射
      ``re_requested`` 并再次 interrupt 重挂（等待重新确认）；
    - approved=False → ``tasks.confirm(approved=False)``（cancelled，8.1）
      + 拒绝 ToolMessage + ``tool_result`` 事件（ok=False）；
    - approved=True → ``tasks.confirm(approved=True)``（running）→
      ``spec.run`` 接真实 Executor（载荷 = pending_confirmation.execution，
      即"批准的卡片"）→ ToolMessage + ``tool_result`` 事件 + policy_keys
      追加 + pending_confirmation 清空。

    中断语义说明（实测验证）：langgraph 按节点内 interrupt 调用顺序匹配
    resume 值——hash 失效重挂后再次恢复时，首个 interrupt 返回旧值、新值
    在第二个 interrupt 位置消费（循环内多一次 interrupt 调用，见下方
    mismatch continue 分支；事件序列不含多余 re_requested——重挂事件只在
    实际重挂时发射一次，tests/agent/test_interrupt.py 注释）。
    """
    pc = state.get("pending_confirmation")
    if not pc:
        return {"current_step": "confirm_skipped", "pending_confirmation": None}
    task_id = pc["task_id"]
    tool_name = pc["tool_name"]
    card = pc["card"]
    # [2026-08-31 修复确认卡重复循环] 写操作连续失败计数（与 tools 节点同
    # 口径，_retry_key 从执行载荷提取目标标识；确认路径的执行失败/确认失效
    # 同样计入，同一写操作成功清零——达上限后 tools 节点拦截自动重试）
    counts = state.get("write_retry_counts") or {}
    retry_key = _retry_key(tool_name, pc.get("execution") or {})

    # 恢复值消费循环：interrupt 返回决策值 → 7.3 hash 校验。
    # - 首轮执行（无恢复值）：interrupt 抛 GraphInterrupt → 发射
    #   confirmation_requested（iteration==0）/re_requested（重挂）→ 图挂起；
    # - 恢复执行：interrupt 返回恢复值；hash 失效（7.3 内容变化 → 原确认
    #   失效）→ continue 再 interrupt（重挂等待重新确认——新值在下一
    #   个 interrupt 位置消费，cosmetic 多一轮，见节点 docstring）。
    iteration = 0
    while True:
        try:
            resume = interrupt(card)
        except GraphInterrupt:
            if iteration == 0:
                _emit(runtime, EVENT_CONFIRMATION_REQUESTED, {
                    "task_id": task_id, "tool_name": tool_name,
                    "card": card, "payload_hash": pc["payload_hash"],
                })
            else:
                _emit(runtime, "re_requested", {
                    "task_id": task_id, "tool_name": tool_name,
                    "card": card, "payload_hash": pc["payload_hash"],
                })
            raise
        if str(resume.get("payload_hash") or "") != pc["payload_hash"]:
            iteration += 1
            continue  # 确认已失效：循环内再次 interrupt（重挂/消费新值）
        break

    approved = bool(resume.get("approved"))

    # 任务状态守卫：确认期间状态被外部推进（失败/取消）→ 不执行、不确认
    record = runtime.tasks.get_task(task_id)
    if record is None:
        content = json.dumps({"ok": False, "error": "写任务不存在（可能已被清理）"},
                             ensure_ascii=False)
        _emit(runtime, EVENT_AGENT_ERROR, {"task_id": task_id, "error": "task_not_found"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "confirm_task_missing",
        }
    if record.status != TaskStatus.WAITING_CONFIRMATION:
        content = json.dumps(
            {"ok": False, "error": f"确认期间任务状态已变化"
             f"（status={record.status.value}），未执行写入（fail closed）"},
            ensure_ascii=False,
        )
        _emit(runtime, EVENT_AGENT_ERROR, {"task_id": task_id, "error": "task_state_changed"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "confirm_state_changed",
            # 确认路径失败（状态已变化）→ 连续失败计数 +1（用户点击确认未生效）
            "write_retry_counts": _bump_retry(counts, retry_key),
        }

    if not approved:
        # 用户拒绝（8.1：cancelled）→ 拒绝 ToolMessage + tool_result 事件
        runtime.tasks.confirm(task_id, approved=False, thread_id=pc.get("thread_id"))
        content = json.dumps({
            "ok": False, "tool": tool_name, "task_id": task_id,
            "status": "cancelled", "error": "用户拒绝确认，操作已取消（8.1）",
        }, ensure_ascii=False)
        _emit(runtime, EVENT_TOOL_RESULT, {
            "task_id": task_id, "tool_name": tool_name,
            "ok": False, "status": "cancelled",
        })
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "confirm_rejected",
        }

    # approved=True：任务回 running → 执行（载荷 = execution，批准的卡片）
    spec = (runtime.tools or {}).get(tool_name)
    if spec is None:
        content = json.dumps({"ok": False, "error": f"工具 {tool_name} 已不在注册表中"},
                             ensure_ascii=False)
        _emit(runtime, EVENT_AGENT_ERROR, {"tool_name": tool_name, "error": "unknown_tool"})
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "tool_unknown",
        }
    runtime.tasks.confirm(task_id, approved=True, thread_id=pc.get("thread_id"))
    _emit(runtime, EVENT_TOOL_RUNNING, {"task_id": task_id, "tool_name": tool_name})
    ctx = _tool_ctx(runtime)
    try:
        result = spec.run(ctx, args=None, task_id=task_id, execution=pc["execution"])
    except Exception as exc:
        # 审计落库失败（AuditFailureError）时补任务级标记；其余异常原样处理
        _mark_audit_write_failed(runtime, task_id, exc)
        content = json.dumps(
            {"ok": False, "tool": tool_name, "task_id": task_id,
             "error": f"工具执行失败：{exc}"}, ensure_ascii=False)
        _emit(runtime, EVENT_TOOL_RESULT, {
            "task_id": task_id, "tool_name": tool_name, "ok": False,
        })
        return {
            "messages": [ToolMessage(content=content, tool_call_id=_new_tool_call_id())],
            "pending_action": None,
            "pending_confirmation": None,
            "current_step": "tool_failed",
            # 执行异常 → 连续失败计数 +1（达上限后 tools 节点拦截自动重试）
            "write_retry_counts": _bump_retry(counts, retry_key),
        }
    summary = {"ok": result.ok, **result.summary}
    _emit(runtime, EVENT_TOOL_RESULT, {
        "task_id": task_id, "tool_name": tool_name, "ok": result.ok,
        "summary": result.summary,
    })
    out: dict[str, Any] = {
        "messages": [
            ToolMessage(
                content=json.dumps(summary, ensure_ascii=False),
                tool_call_id=_new_tool_call_id(),
            )
        ],
        "pending_action": None,
        "pending_confirmation": None,
        "current_step": "tool_done",
    }
    if result.ok:
        # 同一写操作执行成功 → 连续失败计数清零（覆盖语义整表返回）
        out["write_retry_counts"] = {**counts, retry_key: 0}
    else:
        # 执行返回失败（确认失效/版本冲突/结果未知/部分失败——确认卡循环
        # 源头）→ 连续失败计数 +1
        out["write_retry_counts"] = _bump_retry(counts, retry_key)
    out["policy_keys"] = list(state.get("policy_keys") or []) + [pc.get("policy_key")]
    task_id_from_summary = result.summary.get("task_id")
    if task_id_from_summary:
        out["task_id"] = task_id_from_summary
    return out
