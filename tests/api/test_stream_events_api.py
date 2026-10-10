"""SSE 事件流落库集成测试（SQLite 端到端；方案 §八 集成 11-16）。

用 ``tests/api/conftest.py`` 的 ``client`` 夹具——它把**独立的**落库 sink
（写入 ``app.state.stream_event_engine`` 上的 ``chat_stream_events`` 表）注入到
``app.state.chat_streams`` 与 ``app.state.stream_event_sink``。

关键不变式（C1「落库集合 ⊋ 推送集合」）：
- 被展示门控抑制的中间轮 ``agent_reply`` 走影子通道落库（``record_kind=
  'suppressed'``），但**绝不入 ``_streams``** → 影子行不影响真实事件的
  ``sequence``/``event_id``（§五「不重复计数」证明）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from administrative_agent.auth.models import Account, Role
from administrative_agent.config import settings
from administrative_agent.main import app
from administrative_agent.web.chat_streams import StreamStore
from administrative_agent.web.routes_chat import _harness_sink, _run_submit_background

from tests.api.conftest import (
    DEVELOPER,
    MGR,
    ScriptedDecisionClient,
    _unset_state,
    cookies,
    headers,
)

DEV = cookies(Role.DEVELOPER, DEVELOPER)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _rows(stream_id: str) -> list[dict]:
    """查独立事件表（读发生在 flush_now 之后，writer 空闲 → 无并发争用）。"""
    engine = app.state.stream_event_engine
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT event_id, event_type, record_kind, sequence, payload"
                " FROM chat_stream_events WHERE stream_id = :s ORDER BY event_id"
            ),
            {"s": stream_id},
        )
        return [
            {
                "event_id": r[0],
                "event_type": r[1],
                "record_kind": r[2],
                "sequence": r[3],
                "payload": json.loads(r[4]) if isinstance(r[4], str) else r[4],
            }
            for r in result.fetchall()
        ]


def _flush() -> None:
    assert app.state.stream_event_sink.flush_now(2.0)


def _suffixes(event_ids: list[str]) -> list[str]:
    """剥掉 stream 前缀，只留 ``e####``/``x####``/``c####`` 段比对。"""
    return [eid.rsplit("-", 1)[-1] for eid in event_ids]


# ---------------------------------------------------------------------------
# 11. 端到端：POST → 事件落库，首行 stream_created（M12 守护）
# ---------------------------------------------------------------------------


def test_post_message_events_persisted(client, chat_harness):
    app.state.model_decision_client = ScriptedDecisionClient()
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单", "thread_id": "thread-evt-e2e"},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    stream_id = resp.json()["stream_id"]

    streams = app.state.chat_streams
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(e.event_type == "agent_reply" for e in streams.events(stream_id)):
            break
        time.sleep(0.05)
    else:  # pragma: no cover - 图未按期收尾
        pytest.fail(f"图未在 15s 内收尾：{[e.event_type for e in streams.events(stream_id)]}")

    _flush()
    rows = {r["event_id"]: r for r in _rows(stream_id)}

    # 首行 stream_created（event_id = {stream_id}-c0001），M12 守护
    created = rows[f"{stream_id}-c0001"]
    assert created["record_kind"] == "stream_created"

    # 真实事件行：event_id 前缀 -e####，sequence 与流内一致
    real = [r for r in rows.values() if r["record_kind"] in ("pushed", "unmapped")]
    assert real, "无真实事件落库"
    for r in real:
        assert r["event_id"].startswith(f"{stream_id}-e")
    mem = streams.events(stream_id)
    assert {r["event_id"] for r in real} >= {e.event_id for e in mem}
    # payload 键集合落在白名单内（agent_started 只允许 thread_id）
    started = rows[[eid for eid, r in rows.items() if r["event_type"] == "agent_started"][0]]
    assert set(started["payload"]) <= {"thread_id"}


# ---------------------------------------------------------------------------
# 12. 抑制行 + 基线对照（M6/M7 守护）
# ---------------------------------------------------------------------------


def test_suppressed_shadow_row_does_not_perturb_stream(client):
    streams = app.state.chat_streams
    # A 组：触发抑制（interrupted）
    sid_a = "stream-sup-a"
    streams.create(sid_a, thread_id="thread-sup-a")
    emit_a, flush_a = _harness_sink(streams, sid_a, "thread-sup-a")
    emit_a("agent_started", {"thread_id": "thread-sup-a"})
    emit_a("agent_reply", {"reply_text": "中间轮回复张三", "next_action": "reply"})
    flush_a(interrupted=True)

    # B 组：同场景但不触发抑制（不发 candidate）
    sid_b = "stream-sup-b"
    streams.create(sid_b, thread_id="thread-sup-b")
    emit_b, _flush_b = _harness_sink(streams, sid_b, "thread-sup-b")
    emit_b("agent_started", {"thread_id": "thread-sup-b"})

    _flush()

    # 基线对照：两组真实事件 event_id 序列逐元素相等（影子行不入流）
    real_a = [e.event_id for e in streams.events(sid_a)]
    real_b = [e.event_id for e in streams.events(sid_b)]
    assert _suffixes(real_a) == _suffixes(real_b) == ["e0001"]

    # 影子行精确 1 条，三元组精确匹配；B 组 0 条
    sup_a = [r for r in _rows(sid_a) if r["record_kind"] == "suppressed"]
    assert len(sup_a) == 1
    row = sup_a[0]
    assert row["event_type"] == "agent_reply"
    # 影子行独立命名空间 -x#### 且 sequence=0（不占真实 sequence）
    assert row["event_id"] == f"{sid_a}-x0001"
    assert row["sequence"] == 0
    assert row["payload"]["suppressed_reason"] == "interrupted"
    assert row["payload"]["reply_chars"] == len("中间轮回复张三")
    assert "张三" not in json.dumps(row["payload"], ensure_ascii=False)
    assert [r for r in _rows(sid_b) if r["record_kind"] == "suppressed"] == []

    # 抑制行不推给浏览器：SSE 响应里没有它
    resp = client.get(
        f"/api/v1/chat/streams/{sid_a}", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 200
    assert "suppressed" not in resp.text
    assert "中间轮回复张三" not in resp.text


# ---------------------------------------------------------------------------
# 13. 无重复计数（M8 守护）：锚在内存列表这一独立可信源
# ---------------------------------------------------------------------------


def test_no_double_counting_agent_reply(client):
    streams = app.state.chat_streams
    sid = "stream-count"
    streams.create(sid, thread_id="thread-count")
    emit, flush = _harness_sink(streams, sid, "thread-count")
    emit("agent_started", {"thread_id": "thread-count"})
    emit("agent_reply", {"reply_text": "被抑制的中间轮", "next_action": "reply"})
    flush(interrupted=True)
    _flush()

    in_memory = len([e for e in streams.events(sid) if e.event_type == "agent_reply"])
    suppressed = len(
        [r for r in _rows(sid) if r["record_kind"] == "suppressed"
         and r["event_type"] == "agent_reply"]
    )
    in_db = len([r for r in _rows(sid) if r["event_type"] == "agent_reply"])
    assert in_memory == 0
    assert suppressed == 1
    assert in_db == in_memory + suppressed == 1


# ---------------------------------------------------------------------------
# 14. 终局抑制分支（final_step=model_failed / steps_exhausted）
#
# P1-2：该分支覆盖「模型失败 / 步数耗尽」——本批 C1（落库集合 ⊋ 推送集合）
# 存在的全部理由就是这个场景。此前零覆盖：删掉该 record_suppressed 整段仍全绿。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("final_step", ["model_failed", "steps_exhausted"])
def test_final_step_suppression_records_shadow_row(client, final_step):
    streams = app.state.chat_streams
    sid = f"stream-final-{final_step}"
    thread = f"thread-final-{final_step}"
    streams.create(sid, thread_id=thread)
    emit, flush = _harness_sink(streams, sid, thread)
    emit("agent_started", {"thread_id": thread})
    emit("agent_reply", {"reply_text": "更早轮的回复", "next_action": "reply"})
    # 终局轮为失败/耗尽：缓冲的是**更早轮**的回复，不得冒充终局
    flush(interrupted=False, final_step=final_step)
    _flush()

    rows = _rows(sid)
    sup = [r for r in rows if r["record_kind"] == "suppressed"]
    assert len(sup) == 1, [(r["record_kind"], r["event_type"]) for r in rows]
    row = sup[0]
    assert (
        row["event_type"],
        row["record_kind"],
        row["payload"]["suppressed_reason"],
    ) == ("agent_reply", "suppressed", f"final_step:{final_step}")
    # 该中间轮回复绝不入真实流（不冒充终局）
    assert [e for e in streams.events(sid) if e.event_type == "agent_reply"] == []
    # SSE 也不推（影子行只落库）
    resp = client.get(f"/api/v1/chat/streams/{sid}", cookies=MGR, headers=headers())
    assert resp.status_code == 200
    assert "更早轮的回复" not in resp.text
    assert "suppressed" not in resp.text


# ---------------------------------------------------------------------------
# 14b. 取消中途（P2-3）：终局 flush 在已取消流上抛 → 影子行 reason=interrupted
#
# 背景：本批新增的四处 flush(interrupted=True) 里，``final_step`` 两条抑制分支
# **生产不可达**（model_node 只在 next_action=="call_tool" 时发带标记的
# agent_reply，graph.py 把不带 next_action 的送 END；而 _emit 恰好对
# call_tool 直接落流、对其余进缓冲——两条件互斥）。**真正可达的是取消路径**：
# 用户 cancel 后流被标记取消，后台图跑到终局 flush(interrupted=False) →
# _append_sse 的 streams.append 抛 StreamCancelledError（赋值不执行、候选仍在）
# → 生产 except StreamCancelledError 分支的 flush(interrupted=True) 落 1 条
# reason="interrupted" 影子行。此前该分支零覆盖：删掉它仍全绿。
# ---------------------------------------------------------------------------


class _CancelMidDrainGraph:
    """stub 图：drain 中发终局 agent_reply（进缓冲，暂不入流），可选地取消流。

    走 ``_run_submit_background`` 的真实控制流（生产函数），从而真正覆盖
    「终局 flush 抛 StreamCancelledError → except 分支 flush(interrupted=True)」。
    """

    def __init__(self, emit, streams, stream_id, *, cancel: bool) -> None:
        self._emit = emit
        self._streams = streams
        self._sid = stream_id
        self._cancel = cancel

    def stream(self, inputs, config, stream_mode):  # noqa: ANN001
        # 终局轮 agent_reply（next_action != call_tool）→ 进缓冲候选，不入流
        self._emit("agent_reply", {"reply_text": "终局回复", "next_action": "reply"})
        if self._cancel:
            # 用户 cancel：追加 task_cancelled 终态并标记流已取消
            self._streams.cancel(self._sid)
        return iter(())  # 无 __interrupt__ → interrupted=False

    def get_state(self, config):  # noqa: ANN001
        return SimpleNamespace(values={"current_step": "reply"})


class _ReqStub:
    """最小 Request 替身：``_session_factory`` 只读 ``request.app.state``。"""

    app = app


def test_cancel_mid_drain_records_interrupted_shadow_row(client):
    streams = app.state.chat_streams
    mgr = Account(account="manager01", role=Role.MANAGER)

    def _drive(sid: str, thread: str, *, cancel: bool) -> None:
        streams.create(sid, thread_id=thread)
        emit, flush = _harness_sink(streams, sid, thread)
        emit("agent_started", {"thread_id": thread})
        _run_submit_background(
            _ReqStub(),
            mgr,
            thread,
            "查询",
            sid,
            streams,
            {},
            {},
            flush,
            _CancelMidDrainGraph(emit, streams, sid, cancel=cancel),
        )

    sid_cancel, thread_cancel = "stream-cancel-x", "thread-cancel-x"
    sid_ctrl, thread_ctrl = "stream-cancel-ctrl", "thread-cancel-ctrl"
    _drive(sid_cancel, thread_cancel, cancel=True)
    _drive(sid_ctrl, thread_ctrl, cancel=False)
    _flush()

    # 取消组：恰好 1 条影子行，三元组精确匹配（agent_reply / suppressed / interrupted）
    sup = [r for r in _rows(sid_cancel) if r["record_kind"] == "suppressed"]
    assert len(sup) == 1, [(r["record_kind"], r["event_type"]) for r in _rows(sid_cancel)]
    row = sup[0]
    assert (
        row["event_type"],
        row["record_kind"],
        row["payload"]["suppressed_reason"],
    ) == ("agent_reply", "suppressed", "interrupted")
    assert row["event_id"] == f"{sid_cancel}-x0001"  # 独立命名空间，不占真实 sequence
    assert row["sequence"] == 0
    assert row["payload"]["reply_chars"] == len("终局回复")
    assert "终局回复" not in json.dumps(row["payload"], ensure_ascii=False)

    # 对照组（不取消）：无影子行
    assert [r for r in _rows(sid_ctrl) if r["record_kind"] == "suppressed"] == []

    # 不扰流：两组真实事件 event_id 序列逐元素相同（影子行不占 -e 槽位）
    assert _suffixes([e.event_id for e in streams.events(sid_cancel)]) == _suffixes(
        [e.event_id for e in streams.events(sid_ctrl)]
    ) == ["e0001", "e0002"]

    # 落库的真实行（pushed/stream_created）与内存流逐元素一致
    real_db = [
        r["event_id"]
        for r in _rows(sid_cancel)
        if r["record_kind"] in ("pushed", "stream_created")
    ]
    assert set(real_db) >= {e.event_id for e in streams.events(sid_cancel)}


class _GraphRaisesMidDrain:
    """stub 图：drain 中先发终局 agent_reply（进缓冲），再抛非取消异常。"""

    def __init__(self, emit) -> None:
        self._emit = emit

    def stream(self, inputs, config, stream_mode):  # noqa: ANN001
        self._emit("agent_reply", {"reply_text": "中间轮回复", "next_action": "reply"})
        raise RuntimeError("graph boom")

    def get_state(self, config):  # noqa: ANN001
        raise AssertionError("异常路径不应到达 get_state")


def test_graph_exception_mid_drain_records_interrupted_shadow_row(client):
    """图执行抛非取消异常时的 except 分支（routes_chat ``except Exception``）同样
    收敛缓冲 → 1 条 reason="interrupted" 的影子行（此前零覆盖：删掉该 flush 仍全绿）。"""
    streams = app.state.chat_streams
    mgr = Account(account="manager01", role=Role.MANAGER)
    sid, thread = "stream-exc-x", "thread-exc-x"
    streams.create(sid, thread_id=thread)
    emit, flush = _harness_sink(streams, sid, thread)
    emit("agent_started", {"thread_id": thread})

    _run_submit_background(
        _ReqStub(), mgr, thread, "查询", sid, streams, {}, {}, flush,
        _GraphRaisesMidDrain(emit),
    )
    _flush()

    sup = [r for r in _rows(sid) if r["record_kind"] == "suppressed"]
    assert len(sup) == 1, [(r["record_kind"], r["event_type"]) for r in _rows(sid)]
    assert (
        sup[0]["event_type"],
        sup[0]["payload"]["suppressed_reason"],
    ) == ("agent_reply", "interrupted")
    # 异常安全网：流内落 agent_error{chat_run_failed}（不受影子行影响）
    assert [e.event_type for e in streams.events(sid)] == [
        "agent_started",
        "agent_error",
    ]


# ---------------------------------------------------------------------------
# 15. Developer health：stream_events 字段 + dependencies 仍恰好 4 项
# ---------------------------------------------------------------------------


def test_developer_health_reports_stream_events(client):
    resp = client.get("/api/v1/developer/health", cookies=DEV)
    assert resp.status_code == 200
    body = resp.json()
    assert "stream_events" in body
    assert body["stream_events"] is not None  # conftest 已注入独立 sink
    assert set(body["stream_events"]) == {
        "enabled", "queued", "written", "dropped", "write_failures",
        "unmapped_events", "oversize_dropped", "pruned_rows",
        "last_write_age_seconds", "degraded_reason", "last_error_kind",
    }
    # dependencies 刻意不加项：仍恰好 4 项
    assert {d["name"] for d in body["dependencies"]} == {
        "erp_mcp", "agent_database", "checkpoint_database", "session",
    }


def test_developer_health_forwards_last_error_kind(client):
    """``last_error_kind`` 必须**带值**透到面板（P1-2）。

    上一条只断言键集合——而 ``StreamEventSinkStatus.last_error_kind`` 有默认
    ``None``，路由若漏传该字段键仍在（值为 None）→ 键集合断言仍绿。这条用带
    值的 stats 钉住**路由确实转发该值**：漏传即红，面板就能区分
    「投影↔约束漂移 / 表未迁移 / 真连不上」而不必翻容器日志。
    """
    from administrative_agent.web.stream_persistence import StreamEventSinkStats

    orig = app.state.stream_event_sink

    class _FakeSink:
        def stats(self) -> StreamEventSinkStats:
            return StreamEventSinkStats(
                enabled=True,
                queued=0,
                written=0,
                dropped=0,
                write_failures=3,
                unmapped_events=0,
                oversize_dropped=0,
                pruned_rows=0,
                last_write_age_seconds=None,
                degraded_reason="database_unavailable",
                last_error_kind="OperationalError",
            )

    app.state.stream_event_sink = _FakeSink()
    try:
        body = client.get("/api/v1/developer/health", cookies=DEV).json()["stream_events"]
        assert body["last_error_kind"] == "OperationalError"
        assert body["degraded_reason"] == "database_unavailable"
    finally:
        app.state.stream_event_sink = orig


def test_developer_health_stream_events_null_when_not_wired(client):
    orig = app.state.stream_event_sink
    orig_streams = app.state.chat_streams
    _unset_state("stream_event_sink")
    app.state.chat_streams = StreamStore()  # 无 sink
    try:
        resp = client.get("/api/v1/developer/health", cookies=DEV)
        assert resp.status_code == 200
        assert resp.json()["stream_events"] is None
    finally:
        app.state.stream_event_sink = orig
        # 必须一并还原 chat_streams（否则后续用例拿到无 sink 的 StreamStore，
        # 落库断言集体空转——顺序敏感的假绿根源）。
        app.state.chat_streams = orig_streams


# ---------------------------------------------------------------------------
# 16. 脱敏面：stats 序列化后无敏感子串 / 无 DB URL 值
# ---------------------------------------------------------------------------


def test_stream_stats_has_no_sensitive_substrings(client):
    payload = json.dumps(asdict(app.state.stream_event_sink.stats()), default=str)
    lowered = payload.lower()
    for token in ("password", "secret", "token", "api_key", "credential", "pwd"):
        assert token not in lowered, f"统计里出现敏感子串 {token!r}"
    if settings.agent_database_url:
        assert settings.agent_database_url not in payload
