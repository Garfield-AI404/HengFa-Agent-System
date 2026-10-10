"""D25/RV-39 优雅停机与写调用收尾测试（规格 15.3 步骤 7）。

- D26 异步契约：停机后新写任务在图内执行层被拒（tools_node 写分支，
  建任务之前），经流内 agent_error{error: shutting_down} 送达；
  不产生任务记录（8.2 不落库）；
- 停机期间只读任务仍可提交（"停止接收新写任务"只限写路径）；
- lifespan 启动复位停机状态：重启（新 TestClient/lifespan）后恢复
  接收新写任务；
- lifespan 退出时等待进行中写调用（drain_writes）：写任务中途停机
  验证收尾（RV-39 通过标准"发写任务中途停机验证收尾落审计"的协调层
  实证——计数归零语义见 tests/unit/test_lifecycle.py）；
- MCP 写工具调用计入进行中计数、只读不计入（mcp/client.py 插桩点）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent import lifecycle
from administrative_agent.auth.models import Role
from administrative_agent.config import Settings
from administrative_agent.main import app
from administrative_agent.mcp.client import MCPClient, ToolResult
from administrative_agent.persistence.models import Base
from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.web.chat_streams import StreamStore

from tests.api.conftest import MANAGER, VALID_ORIGIN, _unset_state, cookies, headers

THREAD = "thread-graceful"
MGR = cookies(Role.MANAGER, MANAGER)


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


@pytest.fixture()
def stateful_app():
    """注入 SQLite 状态的主 app（lifespan 由 ``with TestClient(app)`` 触发）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_conn, record):  # noqa: ANN001
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat()
        )

    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    app.state.session_factory = TestingSessionLocal
    app.state.chat_streams = StreamStore()
    app.state.write_lock = WriteTaskLock()
    try:
        yield app
    finally:
        del app.state.session_factory
        del app.state.chat_streams
        del app.state.write_lock
        # [2026-09-19] 必须同清 stream_event_sink：lifespan 启动会经
        # ``_wire_stream_event_sink`` 注入进程级单例（``default_sink()``，
        # 与 routes_chat 的 ``_DEFAULT_STREAMS`` 同一实例）。清掉它做到三件
        # 事：清掉他处注入的 sink、保持 teardown 对称、避免跨用例串值
        # （本 fixture 之后的用例不会拿到本用例残留的 sink）。
        #
        # 用 ``_state.pop(..., None)`` 而非 ``del``：Starlette State 的
        # ``__delattr__`` 是 ``del self._state[key]``，键不存在时抛
        # ``KeyError``；而 lifespan 启动可能早在 ``main.py`` wiring 之前就
        # 抛（基础设施失败 → 异常传播 fail closed），此时 sink 从未注入，
        # ``del`` 的 KeyError 会把真正的根因异常盖掉。pop 缺省 None 即幂等。
        #
        # 本行**不治**「单例被关闭」：``_shutdown_stream_sink`` 已给该单例
        # **永久**置 ``_closed``，且 ``default_sink()`` 每次返回同一实例，
        # 故「属性被删 → 再 default_sink()」拿回的是**同一个已关闭实例**，
        # 与属性残留的后果相同。此处不做「注入独立 sink」那种更大改动。
        _unset_state("stream_event_sink")
        Base.metadata.drop_all(engine)


def _submit(
    client: TestClient, message: str, *, thread: str | None = None
) -> tuple[int, dict]:
    """提交消息；thread 缺省用模块级 THREAD。

    多个用例共用同一 thread_id 会互相干扰：D26 起图在后台线程 drain，
    线程活动期间再提交同一 thread 会 409 THREAD_BUSY（防重复提交），
    使后续用例拿到与被测行为无关的状态码。故用例间用不同 thread。
    """
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": message, "thread_id": thread or THREAD},
        cookies=MGR,
        headers=headers(),
    )
    return resp.status_code, resp.json()


def _hold_inflight(seconds: float) -> None:
    with lifecycle.in_flight_write():
        time.sleep(seconds)


def _wait_for_event(streams, stream_id: str, predicate, what: str) -> None:
    """轮询流直至 predicate(event) 命中（图在 daemon 线程 drain）。"""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        events = streams.events(stream_id)
        if any(predicate(e) for e in events):
            return
        time.sleep(0.05)
    pytest.fail(
        f"{what}："
        + str([(e.event_type, e.payload) for e in streams.events(stream_id)])
    )


def _wait_for_error(streams, stream_id: str, error: str) -> None:
    """轮询直至流内出现 agent_error{error: <error>}（如 shutting_down）。"""
    _wait_for_event(
        streams,
        stream_id,
        lambda e: e.event_type == "agent_error"
        and (e.payload or {}).get("error") == error,
        f"未收到 agent_error({error})",
    )


# ---------------------------------------------------------------------------
# 停机门禁：新写任务拒绝，只读放行
# ---------------------------------------------------------------------------


def test_write_message_rejected_during_shutdown(stateful_app):
    """停机后新写任务在图内执行层被拒，不产生任务记录、不碰 ERP。

    D26 契约变更：chat 异步化后提交入口无法同步判定写/读（决策由图内
    模型产生），门禁移至 tools_node 写分支第 2 步——位于任务创建之前
    （tools_node.py「2. 优雅停机」），因此 8.2「被拒的写任务不进串行
    队列、不落任务记录」语义保持完整。拒绝经流内 agent_error
    {error: shutting_down} 送达前端（替代 D25 的同步 503）。
    """
    from administrative_agent.agent.checkpoint import CheckpointService
    from langgraph.checkpoint.memory import MemorySaver

    from tests.api.conftest import (
        FakeMCPClient,
        ScriptedDecisionClient,
        _unset_state,
    )

    # lifespan 见已有注入即跳过真实接线（main.py:80 约定）
    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    erp = FakeMCPClient()
    app.state.mcp_client = erp
    app.state._state.pop("mcp_metadata", None)  # noqa: SLF001
    # 脚本化决策选中写工具（模拟用户请求写操作）
    app.state.model_decision_client = ScriptedDecisionClient(
        tool_name="order_create",
        tool_arguments={
            "order_type": "metal",
            "customer_name": "示例机械有限公司",
            "model": "MK-01",
        },
    )
    try:
        with TestClient(stateful_app) as client:
            lifecycle.begin_shutdown()
            status_code, body = _submit(
                client, "记录今天的考勤", thread="thread-graceful-write-blocked"
            )
            # D26 异步契约：POST 受理返回 200，拒绝在流内送达
            assert status_code == 200
            assert body["stream_id"]

            streams = app.state.chat_streams
            _wait_for_error(streams, body["stream_id"], "shutting_down")

            # 不落任务记录（8.2；被拒的写任务不进串行队列）
            resp = client.get("/api/v1/tasks", cookies=MGR, headers=headers())
            assert resp.status_code == 200
            assert resp.json()["items"] == []

            # 门禁在 prepare 之前：ERP 零调用
            assert erp.calls == []
    finally:
        for key in (
            "checkpoint_service",
            "mcp_client",
            "mcp_metadata",
            "model_decision_client",
        ):
            _unset_state(key)


def test_read_message_accepted_during_shutdown(stateful_app):
    """停机只拒新写任务；只读任务仍可提交（等待当前调用收尾的语义）。"""
    with TestClient(stateful_app) as client:
        lifecycle.begin_shutdown()
        status_code, body = _submit(
            client, "查询今天的订单", thread="thread-graceful-read"
        )
        assert status_code == 200
        # D26 起 chat 异步化：提交只返回受理凭据（stream_id），task_id 由
        # 后台图执行产生；intent 字段已随规则化分类器移除。
        assert body["stream_id"]


def test_write_message_ok_when_not_shutting_down(stateful_app):
    """非停机状态写任务正常受理（门禁默认放行，不改变既有行为）。"""
    with TestClient(stateful_app) as client:
        status_code, body = _submit(
            client, "记录今天的考勤", thread="thread-graceful-write-ok"
        )
        assert status_code == 200
        assert body["stream_id"]


# ---------------------------------------------------------------------------
# lifespan：启动复位 + 退出等待进行中写调用收尾（15.3 步骤 7）
# ---------------------------------------------------------------------------


def test_restart_resets_shutdown_state(stateful_app):
    """lifespan 启动复位停机状态：重启后恢复接收新写任务（RV-39 重启语义）。

    停机中：写决策被图内门禁拒（agent_error shutting_down，见上一用例）；
    重启（新 lifespan，main.py:162 lifecycle.reset()）：同一写请求进入
    正常确认流（confirmation_requested 落流）。
    """
    from administrative_agent.agent.checkpoint import CheckpointService
    from langgraph.checkpoint.memory import MemorySaver

    from tests.api.conftest import (
        FakeMCPClient,
        ScriptedDecisionClient,
        _unset_state,
    )

    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    erp = FakeMCPClient()
    app.state.mcp_client = erp
    app.state._state.pop("mcp_metadata", None)  # noqa: SLF001
    app.state.model_decision_client = ScriptedDecisionClient(
        tool_name="order_create",
        tool_arguments={
            "order_type": "metal",
            "customer_name": "示例机械有限公司",
            "model": "MK-01",
            "quantity": 2,
            "weight_unit": "kg",
        },
    )
    try:
        with TestClient(stateful_app) as client:
            lifecycle.begin_shutdown()
            status_code, body = _submit(
                client, "记录今天的考勤", thread="thread-graceful-restart-a"
            )
            assert status_code == 200
            _wait_for_error(app.state.chat_streams, body["stream_id"],
                            "shutting_down")

        # 新进程（新 lifespan 上下文）：启动复位 → 恢复接收
        # （第一次 lifespan 退出会复位注入键，需重挂；prepare 的客户
        # 唯一匹配需要客户行）
        app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
        app.state.mcp_client = erp
        app.state._state.pop("mcp_metadata", None)  # noqa: SLF001
        erp.rows_by_entity["customers"] = [
            {
                "id": 11,
                "company_name": "示例机械有限公司",
                "unit_price": 120.0,
                "price_type": "含税",
            }
        ]
        # 脚本客户端有状态（首轮 call_tool 已被停机段消耗）——换新实例
        app.state.model_decision_client = ScriptedDecisionClient(
            tool_name="order_create",
            tool_arguments={
                "order_type": "metal",
                "customer_name": "示例机械有限公司",
                "model": "MK-01",
                "quantity": 2,
                "weight_unit": "kg",
            },
        )
        with TestClient(stateful_app) as client2:
            assert lifecycle.accepting_new_writes() is True
            status_code, body = _submit(
                client2, "记录今天的考勤", thread="thread-graceful-restart-b"
            )
            assert status_code == 200
            _wait_for_event(
                app.state.chat_streams,
                body["stream_id"],
                lambda e: e.event_type == "confirmation_requested",
                "重启后写请求未进入确认流",
            )
    finally:
        for key in (
            "checkpoint_service",
            "mcp_client",
            "mcp_metadata",
            "model_decision_client",
        ):
            _unset_state(key)


def test_lifespan_exit_waits_for_inflight_write(stateful_app):
    """写调用进行中退出 lifespan：drain 等待其结束才停机（收尾落审计窗口）。

    时序：先进入 lifespan（启动复位），再启动写调用线程（计数进入），
    然后退出（lifespan shutdown 执行 begin_shutdown + drain_writes）。
    """
    started = time.monotonic()
    thread: threading.Thread | None = None

    with TestClient(stateful_app):
        thread = threading.Thread(target=_hold_inflight, args=(0.4,))
        thread.start()
        time.sleep(0.05)  # 确保计数已进入
        # 退出 with 块：lifespan shutdown → begin_shutdown + drain_writes

    elapsed = time.monotonic() - started
    assert thread is not None
    thread.join()
    assert elapsed >= 0.3  # 停机路径确实等待了进行中写调用
    assert lifecycle.inflight_count() == 0
    assert lifecycle.accepting_new_writes() is False  # 停机状态保持


def test_lifespan_exit_immediate_when_idle(stateful_app):
    """无进行中写调用时退出 lifespan：drain 立即通过（不拖慢正常停机）。"""
    started = time.monotonic()
    with TestClient(stateful_app):
        pass
    assert time.monotonic() - started < 2.0
    assert lifecycle.accepting_new_writes() is False


# ---------------------------------------------------------------------------
# MCP 写工具插桩：进行中计数（收尾落审计的计数边界）
# ---------------------------------------------------------------------------


def test_mcp_write_tool_tracked_read_tool_not(monkeypatch):
    """mcp/client.py：写工具调用计入 in_flight，只读不计入。"""
    client = MCPClient(
        Settings(
            erp_mcp_url="http://erp-mcp.test/mcp",
            erp_mcp_api_key="test-key-not-for-production",
        )
    )
    observed: list[tuple[str, int]] = []

    def fake_impl(tool_name, arguments, operation_id, trace_id):
        observed.append((tool_name, lifecycle.inflight_count()))
        return ToolResult(ok=True)

    monkeypatch.setattr(client, "_call_tool_impl", fake_impl)

    client.call_tool("create", {"employee_id": 1}, operation_id="op-1")
    client.call_tool("create_order_bundle", {}, operation_id="op-2")
    client.call_tool("query", {})
    client.call_tool("list_entities", {})
    client.close()

    # 写工具调用期间计数为 1；只读工具期间为 0
    assert observed == [
        ("create", 1),
        ("create_order_bundle", 1),
        ("query", 0),
        ("list_entities", 0),
    ]
    assert lifecycle.inflight_count() == 0


def test_mcp_write_tool_tracked_on_error_path(monkeypatch):
    """写工具调用异常同样配对递减，不残留计数。"""

    def fake_impl(tool_name, arguments, operation_id, trace_id):
        raise RuntimeError("transport failure")

    client = MCPClient(
        Settings(
            erp_mcp_url="http://erp-mcp.test/mcp",
            erp_mcp_api_key="test-key-not-for-production",
        )
    )
    monkeypatch.setattr(client, "_call_tool_impl", fake_impl)
    with pytest.raises(RuntimeError):
        client.call_tool("update", {}, operation_id="op-3")
    client.close()
    assert lifecycle.inflight_count() == 0
