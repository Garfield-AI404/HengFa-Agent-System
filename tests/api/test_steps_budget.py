"""remaining_steps 步数预算回归测试（[2026-09-09 生产故障修复]）。

背景：remaining_steps 原为**线程级累计预算**——每次模型决策 -1、跨整条
会话永不重置（routes_chat._initial_inputs 曾用 setdefault 保留 checkpoint
旧值）。某条消息耗尽后，该线程所有后续提问都被 model 节点拦截
（"已达本轮执行上限"，steps_exhausted），用户被迫另开新会话，属生产
故障。业务决定：**额度按每条用户消息重置**——每条新 human 消息进入图
执行时无条件恢复为 REMAINING_STEPS(25)；单条消息内的模型-工具循环仍在
一次图执行内连续递减，防死循环拦截语义不变。

本文件钉住该语义的两条边界（harness 与轮询写法同 test_injection_api——
图在后台 daemon 线程 drain，用例轮询 checkpoint 收敛，不 sleep 定值）：

1. **单条消息内耗尽拦截仍生效**：always_call_tool 脚本驱动模型连续决策
   25 轮后，下一轮 model 入口被 steps_exhausted 拦截——终态
   current_step=steps_exhausted、remaining_steps==0、历史追加
   "已达本轮执行上限"、SSE 流产出 agent_error(steps_exhausted)；
2. **同线程下一条新消息恢复满额**（本修复的核心断言）：耗尽后用户在同
   一线程再发消息，提交入口无条件写回 REMAINING_STEPS——消息正常执行
   两轮决策后 remaining_steps == 25 - 2（该终值只有"从满额起扣"才可能），
   终局回复为脚本 reply 而非耗尽拦截文案（修复前该消息在 model 节点
   入口直接拦截、remaining_steps 保持 0）。

确定性要点：两条消息共用同一 thread_id 以复现「跨消息」语义；第二条
消息等第一条的后台线程 drain 完（checkpoint 终态可见）再提交，并带
THREAD_BUSY 短窗口重试兜底（终态写入与后台线程释放并发锁之间是毫秒级
竞态）。
"""

from __future__ import annotations

import time

import pytest

from administrative_agent.agent.state import REMAINING_STEPS
from administrative_agent.main import app
from tests.api.conftest import (
    MGR,
    VALID_ORIGIN,
    ScriptedDecisionClient,
    headers,
)

#: 无 task_id 时的耗尽文案（[2026-09-09] 文案区分：有 task_id 时另用
#: "任务已执行完成，但当前消息的执行额度已用完…"；本文件用例均为只读
#: 工具轮次耗尽，task_id 恒为空）
EXHAUST_TEXT = "已达本轮执行上限，请发送新消息继续。"
SCRIPTED_REPLY = "已为您完成查询。"


def _submit(client, message: str, *, thread: str) -> dict:
    """POST /chat/messages（图后台线程 drain；返回 stream_id）。"""
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": message, "thread_id": thread},
        cookies=MGR,
        headers=headers(VALID_ORIGIN),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_state(thread: str, predicate, *, timeout: float = 20.0) -> dict:
    """轮询 checkpoint 直至满足谓词（后台线程 drain 的确定性收敛点）。

    谓词收到 ``CheckpointService.load_thread`` 的完整 channel_values
    （messages/remaining_steps/current_step）；超时即 fail。
    """
    service = app.state.checkpoint_service
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.load_thread(thread)
        if state is not None and predicate(state):
            return state
        time.sleep(0.05)
    pytest.fail(f"等待 checkpoint 终态超时（{timeout}s）：thread={thread}")


def _submit_new_message(client, message: str, *, thread: str) -> dict:
    """提交下一条消息（上一条的后台线程须已 drain 完；409 竞态短重试）。"""
    deadline = time.monotonic() + 5.0
    while True:
        resp = client.post(
            "/api/v1/chat/messages",
            json={"message": message, "thread_id": thread},
            cookies=MGR,
            headers=headers(VALID_ORIGIN),
        )
        if resp.status_code == 200:
            return resp.json()
        assert resp.status_code == 409, resp.text  # 只容忍并发锁未释放
        assert time.monotonic() < deadline, "上一条消息的后台线程未释放并发锁"
        time.sleep(0.05)


def _wait_terminal_reply(stream_id: str, *, timeout: float = 15.0) -> dict:
    """轮询流直至终局 agent_reply 落流（缓冲 flush 收敛，非 call_tool 中间轮）。

    终局 agent_reply 由 ``_harness_sink`` 缓冲、后台线程 drain 结束后的
    flush() 才落流——**晚于**最后一个 checkpoint 写入（模型节点返回后、
    flush 前），终态轮询已就绪时该事件可能尚未追加，必须轮询不能一次读。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in app.state.chat_streams.events(stream_id):
            if event.event_type != "agent_reply":
                continue
            payload = event.payload or {}
            if payload.get("next_action") != "call_tool":
                return payload
        time.sleep(0.05)
    pytest.fail(f"等待终局 agent_reply 超时（{timeout}s）：stream={stream_id}")


class TestStepsBudget:
    def test_exhaustion_blocks_within_message_and_resets_next_message(
        self, client, chat_harness
    ):
        """核心回归：耗尽只拦单条消息，不锁死整条线程。

        消息 1 脚本化模型每轮都 call_tool(erp_query)（最坏情形：模型陷入
        工具死循环）→ 25 轮后 steps_exhausted 拦截终局；消息 2 换回正常
        脚本（查一轮 + 回复）→ 额度已恢复，正常出终局回复。
        """
        thread = "thread-steps-budget-1"
        app.state.model_decision_client = ScriptedDecisionClient(
            tool_name="erp_query", always_call_tool=True
        )
        body1 = _submit(client, "消息1：循环查询", thread=thread)

        # 断言 1a：单条消息内耗尽拦截仍生效——checkpoint 终态
        # steps_exhausted、remaining_steps 归 0、历史追加"已达本轮执行上限"
        exhausted = _wait_state(
            thread,
            lambda s: s.get("current_step") == "steps_exhausted"
            and s.get("remaining_steps") == 0,
        )
        assert EXHAUST_TEXT in exhausted["messages"][-1].content
        # 断言 1b：fail closed 事件已落流（model 节点先发事件、后写
        # checkpoint，终态可见时事件必已追加）
        stream1 = app.state.chat_streams.events(body1["stream_id"])
        # 只读轮次耗尽 → task_id 恒空 → tool_executed=False（payload 携带
        # tool_executed 布尔，对齐 model_unavailable 的区分口径）
        assert {"error": "steps_exhausted", "tool_executed": False} in [
            e.payload for e in stream1 if e.event_type == "agent_error"
        ]
        calls_after_msg1 = len(chat_harness.calls)

        # 消息 2：同一线程、正常脚本（首轮查 erp_query、次轮 reply）。
        # 修复前：checkpoint 旧值 0 经 setdefault 原样带入 → model 入口
        # 直接拦截；修复后：提交入口无条件写回 REMAINING_STEPS → 两轮
        # 决策各 -1，终态剩 25 - 2
        app.state.model_decision_client = ScriptedDecisionClient(
            tool_name="erp_query", reply_text=SCRIPTED_REPLY
        )
        body2 = _submit_new_message(client, "消息2：正常查询", thread=thread)
        done = _wait_state(
            thread,
            lambda s: s.get("current_step") == "model_done"
            and s.get("remaining_steps") == REMAINING_STEPS - 2,
        )
        # 终局回复是脚本 reply 而非耗尽拦截文案（历史里无新拦截消息）
        assert done["messages"][-1].content == SCRIPTED_REPLY
        assert EXHAUST_TEXT not in done["messages"][-1].content
        # 消息 2 正常走完工具轮（erp_query 到达假 ERP，调用数比消息 1
        # 结束后有增长）
        assert len(chat_harness.calls) > calls_after_msg1
        # 消息 2 的流以正常终局 agent_reply 收尾（终局回复由 flush 落流，
        # 晚于 checkpoint 终态——轮询等待，见 _wait_terminal_reply 注释）
        terminal = _wait_terminal_reply(body2["stream_id"])
        assert SCRIPTED_REPLY in terminal.get("reply_text", "")
