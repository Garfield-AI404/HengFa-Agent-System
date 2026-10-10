"""SSE 事件流落库 sink 单测（有界队列 / 失败不抛 / 退避限流 / 清理 / 停机）。

覆盖方案 §八 单测 6-10（外加 submit 内部异常守卫 7b）。DB 相关用例用
SQLite 临时表（**裸 DDL**，不经 ORM ``create_all``——避免为纯单测注册
JSONB 方言垫片污染全局）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from administrative_agent.web.stream_persistence import (
    _PAYLOAD_BYTE_LIMIT,
    _PRECHECK_BYTE_LIMIT,
    StreamEventRecord,
    StreamEventSink,
    _payload_size_estimate,
    cleanup_expired_stream_events,
)

_DDL = """
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


def _row(i: int = 0, *, event_type: str = "agent_started") -> StreamEventRecord:
    return StreamEventRecord(
        event_id=f"stream-x-{i:04d}",
        stream_id="stream-x",
        event_type=event_type,
        record_kind="pushed",
        payload={"i": i},
        thread_id="thread-x",
        task_id=None,
        sequence=i,
    )


def _sqlite_factory() -> tuple:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text(_DDL))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory


def _scope_from(factory):
    from contextlib import contextmanager

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


# ---------------------------------------------------------------------------
# 6. 队列满丢最老 + 丢弃计数
# ---------------------------------------------------------------------------


class _BlockingProvider:
    """provider：让后台线程卡在写里，使队列可控地积压。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            self.entered.set()
            self.release.wait(timeout=10)
            session = _FakeSession()
            yield session

        return _scope


class _FakeSession:
    def get_bind(self):  # pragma: no cover - 不会走到写 SQL
        raise RuntimeError("blocked")


def test_queue_overflow_drops_oldest_and_counts():
    provider = _BlockingProvider()
    sink = StreamEventSink(
        session_factory_provider=provider, capacity=10, batch_interval_ms=10
    )
    try:
        # 先入一条让后台线程启动并卡在写里（此后队列由测试独占填充）
        sink.submit(_row(0))
        assert provider.entered.wait(5), "后台线程未进入写路径"
        for i in range(1, 21):  # capacity(10) + 10 条
            sink.submit(_row(i))
        stats = sink.stats()
        assert stats.dropped == 10  # 确切数字，不是 >= 0
        # 保留的是**最后** capacity 条（丢最老，不是丢最新）
        kept = [r.event_id for r in list(sink._q)]
        assert kept == [f"stream-x-{i:04d}" for i in range(11, 21)]
        # 桶计数上限内（本用例 1 个事件类型）
        assert sink._dropped_by_type == {"agent_started": 10}
    finally:
        provider.release.set()
        sink.shutdown(2)


# ---------------------------------------------------------------------------
# 7. DB 写失败不向业务抛（红线）
# ---------------------------------------------------------------------------


class _FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            self.calls += 1
            raise RuntimeError("db down")

        return _scope


def test_failing_provider_raises_when_called_directly():
    """先证明异常注入不是空转：provider 单独调用确实会抛。"""
    provider = _FailingProvider()
    with pytest.raises(RuntimeError):
        with provider()():
            pass


def test_db_failure_never_propagates_to_business_thread():
    provider = _FailingProvider()
    sink = StreamEventSink(
        session_factory_provider=provider, capacity=100, batch_interval_ms=10
    )
    try:
        sink.submit(_row(1))  # 必须正常返回
        sink.flush_now(2.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and sink.stats().write_failures == 0:
            time.sleep(0.01)
        stats = sink.stats()
        assert stats.write_failures > 0
        assert stats.degraded_reason == "database_unavailable"
        assert sink._last_error_kind == "RuntimeError"  # 只存类名，不存 str(exc)
        # last_error_kind 必须**有出口**（P1-2）：degraded_reason 只答「降级了没」，
        # 面板要靠 error_kind 区分「投影↔约束漂移 / 表未迁移 / 真连不上」，
        # 否则第一轮「真因与读数相反」的误诊机制原样复现。
        assert stats.last_error_kind == "RuntimeError"
        # 绝不含 str(exc) 内容（本用例的异常消息是 "db down"）
        assert "db down" not in repr(stats)
    finally:
        sink.shutdown(2)


def test_submit_swallows_internal_exception(monkeypatch):
    """submit 内部异常（此处模拟线程启动失败）绝不外抛——M4 守护点。"""
    sink = StreamEventSink(capacity=10)

    def _boom() -> None:
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(sink, "_ensure_thread", _boom)
    sink.submit(_row(1))  # 不抛
    assert sink.stats().dropped == 1


# ---------------------------------------------------------------------------
# 8. 失败退避 + 日志限流
# ---------------------------------------------------------------------------


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.warnings.append((event, kw))

    def info(self, event: str, **kw) -> None:  # pragma: no cover
        pass


def test_failure_logging_is_throttled(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(
        "administrative_agent.web.stream_persistence.logger", rec
    )
    sink = StreamEventSink(capacity=10)
    for _ in range(100):
        sink._record_failure(RuntimeError("boom"))
    events = [e for e, _ in rec.warnings if e == "stream_event_sink_write_failed"]
    # 限流：首次 + 2 的幂（1,2,4,8,16,32,64）→ 7 条，远小于 100
    assert len(events) < 20
    assert len(events) >= 1
    assert sink.stats().write_failures == 100
    # 桶/降级状态可见
    assert sink.stats().degraded_reason == "database_unavailable"


# ---------------------------------------------------------------------------
# 9. 清理分批
# ---------------------------------------------------------------------------


def test_cleanup_deletes_expired_in_batches():
    engine, factory = _sqlite_factory()
    try:
        now = datetime.now(timezone.utc)
        rows = [
            ("e-old-1", now - timedelta(days=20)),
            ("e-old-2", now - timedelta(days=16)),
            ("e-old-3", now - timedelta(days=15)),
            ("e-new-1", now - timedelta(days=1)),
            ("e-new-2", now),
        ]
        with engine.begin() as conn:
            for eid, created in rows:
                conn.execute(
                    text(
                        "INSERT INTO chat_stream_events"
                        " (event_id, stream_id, event_type, record_kind, payload, created_at)"
                        " VALUES (:e, 's', 'agent_started', 'pushed', '{}', :c)"
                    ),
                    {"e": eid, "c": created.replace(tzinfo=None)},
                )
        deleted, batches = cleanup_expired_stream_events(
            factory, ttl_seconds=14 * 86400, batch_size=2, max_batches=20
        )
        assert deleted == 3  # 结果行数，不是「函数被调用了」
        assert batches >= 2  # 分批确实发生了
        with engine.connect() as conn:
            remaining = [
                r[0]
                for r in conn.execute(
                    text("SELECT event_id FROM chat_stream_events ORDER BY event_id")
                )
            ]
        assert remaining == ["e-new-1", "e-new-2"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 10. 停机 drain
# ---------------------------------------------------------------------------


def test_shutdown_drains_and_then_submit_is_noop():
    engine, factory = _sqlite_factory()
    try:
        sink = StreamEventSink(
            session_factory_provider=lambda: _scope_from(factory),
            capacity=100,
            batch_interval_ms=10,
        )
        for i in range(3):
            sink.submit(_row(i))
        assert sink.flush_now(2.0)
        sink.shutdown(2)
        assert len(sink._q) == 0
        assert sink._closed is True
        assert sink._thread is not None and not sink._thread.is_alive()
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM chat_stream_events")
            ).scalar_one()
        assert count == 3
        # 停机后 submit 是 no-op
        sink.submit(_row(99))
        assert len(sink._q) == 0
    finally:
        engine.dispose()


def test_no_residual_writer_thread_after_shutdown():
    engine, factory = _sqlite_factory()
    try:
        sink = StreamEventSink(
            session_factory_provider=lambda: _scope_from(factory),
            batch_interval_ms=10,
        )
        sink.submit(_row(1))
        sink.flush_now(2.0)
        sink.shutdown(2)
        leftovers = [
            t
            for t in threading.enumerate()
            if t.name == "stream-event-writer" and t.is_alive()
        ]
        assert leftovers == []
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 11. 逐行预检（超限单行丢弃，不拖累整批）+ unmapped 计数
# ---------------------------------------------------------------------------


def _oversize_row(i: int) -> StreamEventRecord:
    """payload 明显超 4096 字节的合法行（event_type 合法，仅体积超限）。"""
    return StreamEventRecord(
        event_id=f"stream-x-big-{i}",
        stream_id="stream-x",
        event_type="agent_started",
        record_kind="pushed",
        payload={"text": "x" * 6000},
        thread_id="thread-x",
        task_id=None,
        sequence=i,
    )


def test_oversize_row_dropped_alone_without_poisoning_batch():
    """单行超限 → 只丢那一行 + 旁路计数；同批正常行照常落库、write_failures=0。"""
    engine, factory = _sqlite_factory()
    try:
        sink = StreamEventSink(
            session_factory_provider=lambda: _scope_from(factory),
            batch_max=50,
            batch_interval_ms=10,
        )
        for i in range(3):
            sink.submit(_row(i))
        sink.submit(_oversize_row(99))
        assert sink.flush_now(5.0)
        stats = sink.stats()
        assert stats.oversize_dropped == 1  # 确切数字
        assert stats.written == 3  # 正常行未被拖累
        assert stats.write_failures == 0  # 没有触发整批失败
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM chat_stream_events")
            ).scalar_one()
        assert count == 3
    finally:
        engine.dispose()


def test_unmapped_event_increments_counter():
    """record_kind='unmapped' 入队即计数（fail-closed 可见性，P2-5）。"""
    engine, factory = _sqlite_factory()
    try:
        sink = StreamEventSink(
            session_factory_provider=lambda: _scope_from(factory),
            batch_interval_ms=10,
        )
        row = StreamEventRecord(
            event_id="stream-x-u0001",
            stream_id="stream-x",
            event_type="some_unknown_event",
            record_kind="unmapped",
            payload={},
            thread_id="thread-x",
            task_id=None,
            sequence=0,
        )
        sink.submit(row)
        assert sink.stats().unmapped_events == 1
        sink.flush_now(2.0)
        assert sink.stats().unmapped_events == 1
    finally:
        sink.shutdown(2)
        engine.dispose()


# ---------------------------------------------------------------------------
# 12. 预检阈值与 DB 上限的常量关系（判据 = 阈值本身，别写成「某函数被调用了」）
# ---------------------------------------------------------------------------


def test_precheck_limit_is_strictly_below_db_limit():
    """逐行预检阈值必须**严格小于** DB 上限。

    理由（见 ``stream_persistence`` 常量注释）：``_payload_size_estimate`` 是紧凑
    JSON 文本长度，恒**小于** PG 约束用的 jsonb ``pg_column_size``（实测单键
    +8、短字符串数组 +14、多键标量约 +9/键）。若预检阈值等于 DB 上限，
    estimate ∈ (上限−delta, 上限] 的行会被误放行 → CheckViolation → **整批**死，
    预检在最该守的边界上失效。故必须偏向误杀（只丢单行 + 计数）。这条红了
    不是测试过时，是把方向改回去了。

    ⚠ 这条只钉**常量关系**；**使用处比较点**（``_split_oversize`` 里的算子）
    由 ``test_precheck_drops_row_in_margin_band`` 钉住——两者缺一都会让
    「precheck 放行、DB 拒批」的 P0 从比较算子上静默回来。
    """
    assert _PRECHECK_BYTE_LIMIT < _PAYLOAD_BYTE_LIMIT


def test_precheck_drops_row_in_margin_band():
    """**使用处比较点**的守护：estimate 落在裕量带 (预检阈值, DB 上限] 的行必须丢。

    这条用例的存在理由：``test_precheck_limit_is_strictly_below_db_limit`` 只钉
    常量关系、``test_payload_byte_limit_matches_migration_contract`` 只钉 4096
    字面量、``test_oversize_row_dropped_alone_without_poisoning_batch`` 的 payload
    （``"x"*6000``）**两个阈值都会丢它**——三处合起来对 ``_split_oversize`` 用的
    是 ``> _PRECHECK_BYTE_LIMIT`` 还是 ``> _PAYLOAD_BYTE_LIMIT`` **零覆盖**。
    把比较点写成后者（= 第一轮 P0 的原始实现）全套件仍全绿，而 estimate ∈
    (4032, 4096] 的行会被放行 → 真 PG ``pg_column_size > 4096`` → CheckViolation
    → **整批**一起死。本用例取落在该带内的 payload，钉死比较点必须是预检阈值。

    为什么裸 SQLite 夹具能区分：它**没有** PG 的 ``pg_column_size`` 约束，旧实现
    会**真的把它写进去**（``written == 1``）→ 必红。
    """
    # payload ≈ 4041 字节，必须落在 (预检阈值, DB 上限] 带内（前提探针）
    payload = {"text": "x" * 4030}
    estimate = _payload_size_estimate(payload)
    assert _PRECHECK_BYTE_LIMIT < estimate <= _PAYLOAD_BYTE_LIMIT, (
        estimate,
        _PRECHECK_BYTE_LIMIT,
        _PAYLOAD_BYTE_LIMIT,
    )

    engine, factory = _sqlite_factory()
    try:
        sink = StreamEventSink(
            session_factory_provider=lambda: _scope_from(factory),
            batch_max=50,
            batch_interval_ms=10,
        )
        sink.submit(
            StreamEventRecord(
                event_id="stream-x-margin",
                stream_id="stream-x",
                event_type="agent_started",
                record_kind="pushed",
                payload=payload,
                thread_id="thread-x",
                task_id=None,
                sequence=0,
            )
        )
        assert sink.flush_now(5.0)
        stats = sink.stats()
        assert stats.oversize_dropped == 1  # 落在裕量带内 → 必须预检丢弃
        assert stats.written == 0  # 绝不放行给 DB（裸 SQLite 会照单全收）
        assert stats.write_failures == 0
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM chat_stream_events")
            ).scalar_one()
        assert count == 0
    finally:
        sink.shutdown(2)
        engine.dispose()


def test_payload_byte_limit_matches_migration_contract():
    """``_PAYLOAD_BYTE_LIMIT`` 是**字面量契约值**，必须与迁移约束一致。

    它是 ``ck_chat_stream_events_payload_size``（``pg_column_size(payload) <= 4096``）
    在 Python 侧的对应值——只改一边会让「应用侧以为的上限」与「DB 实际拒绝的上限」
    分叉。这里钉字面量：红了就是提醒有人只改了单侧（改迁移 + 改这里，或都不改）。
    """
    assert _PAYLOAD_BYTE_LIMIT == 4096

