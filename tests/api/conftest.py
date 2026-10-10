"""D22 前端整合 API 测试共享夹具（chat/tasks/audit 端点）。

SQLite 内存库 + app.state.session_factory 注入（同 test_reviews_api.py
模式）；app.state.chat_streams 注入独立 StreamStore（每个 client 全新，
测试间事件流不串扰）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import BigInteger, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent import lifecycle
from administrative_agent.agent.checkpoint import CheckpointService
from administrative_agent.agent.model_client import (
    AgentDecision,
    SummaryResult,
    ToolArguments,
)
from administrative_agent.auth.models import Account, Role
from administrative_agent.auth.service import SESSION_COOKIE_NAME, create_session
from administrative_agent.main import app
from administrative_agent.mcp.client import ToolResult
from administrative_agent.persistence.models import Base
from administrative_agent.tasks.lock import WriteTaskLock
from administrative_agent.web.chat_streams import StreamStore
from administrative_agent.web.stream_persistence import StreamEventSink

VALID_ORIGIN = "http://127.0.0.1:5173"
MANAGER = "manager01"
DEVELOPER = "dev01"


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    """进程级停机状态隔离（D25，15.3 步骤 7）。

    优雅停机后 lifecycle 置位停机标志（拒绝新写任务）；生产进程随即退出，
    但 pytest 同进程跑多个 TestClient（如 test_health 的 with 块触发
    lifespan 收尾），停机标志会泄漏到后续用例——每个用例前复位，
    等价于"每个 TestClient 都是新进程"（main.py lifespan startup 同口径）。
    """
    lifecycle.reset()
    yield
    lifecycle.reset()


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_biginteger_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


#: 事件落库 sink 的独立 SQLite 表（**裸 DDL**，避免为纯 API 测试注册
#: JSONB/BigInteger 之外的方言垫片；created_at 用 CURRENT_TIMESTAMP 而非
#: server_default=now() → 无需每连接注册 now() 函数）。
_STREAM_EVENTS_DDL = """
CREATE TABLE chat_stream_events (
    event_id VARCHAR(64) NOT NULL,
    stream_id VARCHAR(64) NOT NULL,
    thread_id VARCHAR(128),
    task_id VARCHAR(64),
    sequence INTEGER NOT NULL DEFAULT 0,
    event_type VARCHAR(64) NOT NULL,
    record_kind VARCHAR(16) NOT NULL DEFAULT 'pushed',
    payload JSON NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id)
)
"""


def _scope_from(factory):
    """sessionmaker → 一次事务的 contextmanager（供 sink 写入用）。"""

    @contextmanager
    def _scope() -> Iterator:
        session = factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


@pytest.fixture()
def client() -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_conn, record):  # noqa: ANN001
        """SQLite 兼容：models 的 server_default=text('now()') → 当前时间。"""
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat()
        )

    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    # [2026-09-19 可观测性第 0 步] 落库 sink 用**独立** SQLite 引擎（不与 app
    # 的 StaticPool 单连接争用）；每个 client 一个全新实例，避免计数跨用例泄漏。
    evt_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with evt_engine.begin() as conn:
        conn.execute(text(_STREAM_EVENTS_DDL))
    evt_factory = sessionmaker(bind=evt_engine, autoflush=False, expire_on_commit=False)
    evt_sink = StreamEventSink(
        session_factory_provider=lambda: _scope_from(evt_factory),
        batch_interval_ms=20,
    )
    app.state.session_factory = TestingSessionLocal
    app.state.chat_streams = StreamStore(sink=evt_sink)
    app.state.stream_event_sink = evt_sink
    app.state.stream_event_engine = evt_engine
    app.state.write_lock = WriteTaskLock()
    try:
        yield TestClient(app)
    finally:
        evt_sink.shutdown(2)
        evt_engine.dispose()
        del app.state.session_factory
        del app.state.chat_streams
        del app.state.stream_event_sink
        del app.state.stream_event_engine
        del app.state.write_lock
        Base.metadata.drop_all(engine)


def cookies(role: Role, account: str = MANAGER) -> dict:
    """TestClient cookies kwarg（{SESSION_COOKIE_NAME: value}）。"""
    return {SESSION_COOKIE_NAME: create_session(Account(account=account, role=role))}


def headers(origin: str | None = VALID_ORIGIN) -> dict:
    """请求头：同源 Origin（写 API 的 CSRF 校验需命中白名单）。"""
    return {"Origin": origin} if origin else {}


#: 常用组合：manager 会话 + 同源 Origin（写 API 标准请求）
MGR = cookies(Role.MANAGER)


# ---------------------------------------------------------------------------
# chat 图测试注入（D26 异步契约：POST 毫秒级返回 + daemon 线程 drain 图）。
# 三层注入点均为生产代码预留（routes_chat docstring 明示）：
#   app.state.checkpoint_service  — CheckpointService(MemorySaver())，免 Postgres
#   app.state.mcp_client          — FakeMCPClient（记录全部 MCP 调用）
#   app.state.model_decision_client — ScriptedDecisionClient（脚本化模型决策）
# EntityCache 由 routes_chat._mcp_metadata 按 mcp_client 懒构造（list_entities
# 信封兼容）。注入键在 fixture finally 中清理——防 app.state 跨测试泄漏。
# ---------------------------------------------------------------------------

class FakeMCPClient:
    """确定性假 ERP 客户端：记录全部 call_tool 调用供断言。

    - call_tool('query', ...) → 按 entity 返回预置行（缺省空表）；
    - call_tool('list_entities') → 返回 metal_orders/customers 元数据信封；
    - 写工具调用同样只记录不执行——注入测试断言「ERP 未收到任何写调用」
      即以此记录为准。
    """

    #: 两个查询路径会用到的实体的最小字段目录（list_entities 信封）
    ENTITIES: list[dict] = [
        {
            "name": "metal_orders",
            "label": "钣金订单",
            "readable": True,
            "creatable": True,
            "updatable": True,
            "deletable": False,
            "fields": {
                name: {"type": type_, "label": label}
                for name, type_, label in [
                    ("id", "int", "ID"),
                    ("model", "string", "型号"),
                    ("in_date", "date", "进厂日期"),
                    ("status", "string", "状态"),
                    ("company_name", "string", "客户名称"),
                    ("unit_price", "decimal", "单价"),
                    ("price_type", "string", "价格类型"),
                ]
            },
        },
        {
            "name": "customers",
            "label": "客户",
            "readable": True,
            "creatable": False,
            "updatable": False,
            "deletable": False,
            "fields": {
                name: {"type": type_, "label": label}
                for name, type_, label in [
                    ("id", "int", "ID"),
                    ("company_name", "string", "客户名称"),
                    ("unit_price", "decimal", "单价"),
                    ("price_type", "string", "价格类型"),
                ]
            },
        },
    ]

    def __init__(self, *, rows_by_entity: dict[str, list[dict]] | None = None):
        self.rows_by_entity = rows_by_entity or {}
        self.calls: list[dict] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult:
        self.calls.append({"tool": tool_name, "arguments": dict(arguments or {})})
        if tool_name == "list_entities":
            return ToolResult(ok=True, data={"entities": self.ENTITIES})
        if tool_name == "query":
            entity = (arguments or {}).get("entity", "")
            rows = self.rows_by_entity.get(entity, [])
            return ToolResult(ok=True, data={"items": rows, "total": len(rows)})
        return ToolResult(ok=True, data={})

    def list_entities(
        self, operation_id: str | None = None, trace_id: str | None = None
    ) -> ToolResult:
        return self.call_tool("list_entities", {})


class ScriptedDecisionClient:
    """脚本化模型决策客户端（替代真实 LLM 的确定性桩）。

    首轮 generate_decision 返回 call_tool(script 工具)，其余轮返回
    reply 文本——注入测试验证的是服务端确定性防护层（工具注册表 /
    pydantic 参数校验 / 实体白名单 / 确认门控），不依赖模型行为。
    """

    def __init__(
        self,
        tool_name: str = "erp_query",
        tool_arguments: dict | None = None,
        reply_text: str = "已为您完成查询。",
        *,
        always_call_tool: bool = False,
    ):
        self.tool_name = tool_name
        self.tool_arguments = tool_arguments or {
            "entity": "metal_orders",
            "fetch_all": False,
        }
        self.reply_text = reply_text
        self.always_call_tool = always_call_tool
        self.decision_calls = 0

    def generate_decision(self, **_: object) -> AgentDecision:
        self.decision_calls += 1
        if self.decision_calls == 1 or self.always_call_tool:
            return AgentDecision(
                reply_text="",
                next_action="call_tool",
                tool_name=self.tool_name,
            )
        return AgentDecision(reply_text=self.reply_text, next_action="reply")

    def generate_tool_arguments(
        self,
        *,
        system_prompt: str,
        messages: list,
        schema_instruction: str,
        **_: object,
    ) -> ToolArguments:
        return ToolArguments(
            tool_name=self.tool_name, tool_arguments=self.tool_arguments
        )

    def generate_summary(self, *, messages: list) -> SummaryResult:
        return SummaryResult(summary="测试摘要", index="【段1】测试｜注入")


def _unset_state(key: str) -> None:
    """从 app.state 删除属性（Starlette State 属性存于 _state 字典，
    ``__dict__.pop`` 是空操作——必须走 _state 或 __delattr__）。"""
    app.state._state.pop(key, None)  # noqa: SLF001


@pytest.fixture()
def chat_harness() -> FakeMCPClient:
    """chat 图完整注入：MemorySaver checkpoint + FakeMCPClient。

    返回 FakeMCPClient 供用例断言 ERP 调用记录；需要脚本化模型时用例内
    再挂 ``app.state.model_decision_client = ScriptedDecisionClient(...)``。
    """
    app.state.checkpoint_service = CheckpointService(MemorySaver(), None)
    erp = FakeMCPClient()
    app.state.mcp_client = erp
    # 清掉此前 lifespan 可能留下的真实 EntityCache（包着已关闭的真实
    # MCPClient）——routes_chat._mcp_metadata 见非空即用，不清会让查询
    # 全部撞死在元数据刷新上（顺序依赖的根源）
    _unset_state("mcp_metadata")
    try:
        yield erp
    finally:
        for key in (
            "checkpoint_service",
            "mcp_client",
            "mcp_metadata",
            "model_decision_client",
        ):
            _unset_state(key)
