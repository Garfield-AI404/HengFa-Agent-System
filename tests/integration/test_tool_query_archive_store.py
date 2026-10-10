"""[2026-10-06 §3.8 读工具调用参数落审计] tool_query_archive 扩展两列：

- ``arguments``（JSONB NULL）：本次读工具调用模型传的入参；
- ``ok``（BOOLEAN NULL）：本次调用成败。

覆盖 ArchiveStore 落库 / 读取语义（DB 层，非节点层——节点层见
``test_detail_eviction.py`` 第 5 节）：
1. append_tool_query 写入 arguments/ok，get_tool_query 正常回读（读取路径
   不引用新列，故新旧行同形）；
2. **历史行**（arguments=NULL / ok=NULL，即默认缺参落库的旧形态）读取不炸；
3. 语义变化钉住：0 行 / 失败调用落行后 ``get_tool_query`` 由 ``None`` 变为
   ``([], 0, entity)``（memory_lookup 据 P2 取到此值后返回 ok=True 的空结果）；
4. delete_thread 覆盖含新列的行（11.4 随会话删除）。

DB：SQLite 内存库 + JSONB/BigInteger 方言垫片（同 tests/api/conftest 模式，
让 models 的 Base.metadata 可在 SQLite 建表）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent.agent.memory.archive import ArchiveStore
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.memory import MemoryLookupArgs, run_memory_lookup
from administrative_agent.persistence.models import Base, ToolQueryArchive


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_biginteger_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def factory():
    """SQLite 内存库 sessionmaker（含 models 全部表）。"""
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
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def store(factory):
    return ArchiveStore(factory)


def test_append_and_read_roundtrip_with_new_columns(store, factory) -> None:
    """arguments/ok 落库后 get_tool_query 正常回读；DB 里确实写入了两列。"""
    store.append_tool_query(
        thread_id="t1",
        query_id="q1",
        entity="metal_orders",
        count=1,
        items=[{"id": 1}],
        arguments={"entity": "metal_orders", "filter": {"customer_id": 7}},
        ok=True,
    )
    assert store.get_tool_query("t1", "q1") == ([{"id": 1}], 1, "metal_orders")
    session = factory()
    try:
        row = session.query(ToolQueryArchive).filter_by(query_id="q1").one()
        assert row.arguments == {"entity": "metal_orders", "filter": {"customer_id": 7}}
        assert row.ok is True
    finally:
        session.close()


def test_historical_null_columns_read_does_not_break(store, factory) -> None:
    """历史行（缺省 arguments/ok → NULL）读取不炸；两列确为 NULL。"""
    store.append_tool_query(
        thread_id="t-old", query_id="q-old", entity="customers",
        count=2, items=[{"id": 1}, {"id": 2}],
    )
    session = factory()
    try:
        row = session.query(ToolQueryArchive).filter_by(query_id="q-old").one()
        assert row.arguments is None
        assert row.ok is None
    finally:
        session.close()
    # 读取路径不引用新列 → 历史行照常回读
    assert store.get_tool_query("t-old", "q-old") == (
        [{"id": 1}, {"id": 2}], 2, "customers"
    )


def test_zero_row_row_now_returns_empty_tuple_not_none(store) -> None:
    """§3.8 语义变化：0 行成功落行后 get_tool_query 返回 ([], 0, entity)
    （旧行为 None）。"""
    store.append_tool_query(
        thread_id="t1", query_id="q0", entity="metal_orders",
        count=0, items=[], arguments={"entity": "metal_orders"}, ok=True,
    )
    assert store.get_tool_query("t1", "q0") == ([], 0, "metal_orders")


def test_failed_row_returns_empty_tuple(store) -> None:
    """失败（ok=False）落行同样可由 get_tool_query 取回（ok=False 不参与读取）。"""
    store.append_tool_query(
        thread_id="t1", query_id="qf", entity="suppliers",
        count=0, items=[], arguments={"entity": "suppliers"}, ok=False,
    )
    assert store.get_tool_query("t1", "qf") == ([], 0, "suppliers")


def test_unknown_query_id_still_none(store) -> None:
    """未落行的 query_id 仍是 None（与 0 行行区分）。"""
    assert store.get_tool_query("t1", "nope") is None


def test_delete_thread_removes_rows_with_new_columns(store) -> None:
    """delete_thread 覆盖含新列的行（11.4 随会话删除）。"""
    store.append_tool_query(
        thread_id="t1", query_id="q1", entity="metal_orders",
        count=1, items=[{"id": 1}], arguments={"a": 1}, ok=True,
    )
    store.delete_thread("t1")
    assert store.get_tool_query("t1", "q1") is None


def test_memory_lookup_zero_row_archive_returns_ok_empty() -> None:
    """memory_lookup 消费点：取到 ([], 0, entity) 时返回 ok=True 的空结果
    （不再是「查询存档不存在或为空」错误）——0 行存档经本工具可见的行为钉住。"""

    class _ZeroRowArchive:
        def get_tool_query(self, thread_id: str, query_id: str):
            return ([], 0, "metal_orders")

    ctx = ToolContext(
        context=AgentContext(
            user_id="manager01", user_role="manager",
            scope=frozenset(), thread_id="t1",
        ),
        archive=_ZeroRowArchive(),
    )
    result = run_memory_lookup(ctx, MemoryLookupArgs(query_id="q0"))
    assert result.ok is True
    assert result.summary["total"] == 0
    assert result.summary["count"] == 0
    assert result.summary["items"] == []
