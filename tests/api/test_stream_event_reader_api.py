"""任务时间线读取端点集成测试（可观测性第 1 步；方案 §7.1 用例 1-16）。

覆盖两个新端点：

- ``GET /api/v1/developer/stream-events/threads``（最近会话列表）
- ``GET /api/v1/developer/stream-events/threads/{thread_id}``（事件流水）

夹具：``tests/api/conftest.py`` 的 ``client`` 已注入
``app.state.stream_event_engine``（写入端那个 engine，裸 DDL 建表）——
本文件用 ``text()`` 直插该表，**显式给 ``created_at``**（否则测不出版级并列、
也测不出次级排序键）。

⚠️ ``tests/api`` 里并发跑两个后台图线程会挂死（StaticPool 共享单连接）：
用例 15 涉及真实 ``POST /chat/messages``，**第二个 POST 前必须等第一个收尾**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from administrative_agent.auth.models import Role
from administrative_agent.main import app
from administrative_agent.web import stream_event_reader

from tests.api.conftest import (
    DEVELOPER,
    MGR,
    ScriptedDecisionClient,
    _unset_state,
    cookies,
    headers,
)

DEV = cookies(Role.DEVELOPER, DEVELOPER)

LIST_URL = "/api/v1/developer/stream-events/threads"


def _detail_url(thread_id: str) -> str:
    return f"{LIST_URL}/{thread_id}"


#: 响应字段的**精确集合**（锁 ``extra="forbid"`` 与字段名；多一个少一个都红）
LIST_BODY_KEYS = {"items", "total", "limit", "offset", "untracked_events"}
LIST_ITEM_KEYS = {
    "thread_id", "event_count", "stream_count", "chunk_count",
    "error_count", "suppressed_count", "unmapped_count",
    "first_seen", "last_seen",
}
DETAIL_BODY_KEYS = {
    "thread_id", "items", "total", "limit", "offset",
    "expected_events", "missing_events",
}
DETAIL_ITEM_KEYS = {
    "event_id", "stream_id", "sequence", "event_type", "record_kind",
    "task_id", "payload", "created_at",
}

#: 时间戳字面量：SQLite 存 TEXT，同格式下字典序 == 时序。
TS_A = "2026-09-18 02:11:07.000000"
TS_B = "2026-09-18 02:00:00.000000"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _row(
    event_id: str,
    stream_id: str,
    thread_id: str | None,
    event_type: str,
    *,
    record_kind: str = "pushed",
    sequence: int = 0,
    created_at: str = TS_A,
    payload: dict | None = None,
    task_id: str | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "stream_id": stream_id,
        "thread_id": thread_id,
        "task_id": task_id,
        "sequence": sequence,
        "event_type": event_type,
        "record_kind": record_kind,
        "created_at": created_at,
        "payload": payload or {},
    }


def _seed(rows: list[dict]) -> None:
    """直插事件表（显式 created_at；**不**经投影，供精确控制计数与顺序）。"""
    engine = app.state.stream_event_engine
    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO chat_stream_events"
                    " (event_id, stream_id, thread_id, task_id, sequence,"
                    "  event_type, record_kind, payload, created_at)"
                    " VALUES (:event_id, :stream_id, :thread_id, :task_id,"
                    "  :sequence, :event_type, :record_kind, :payload,"
                    "  :created_at)"
                ),
                {
                    "event_id": r["event_id"],
                    "stream_id": r["stream_id"],
                    "thread_id": r["thread_id"],
                    "task_id": r["task_id"],
                    "sequence": r["sequence"],
                    "event_type": r["event_type"],
                    "record_kind": r["record_kind"],
                    "payload": json.dumps(r["payload"], ensure_ascii=False),
                    "created_at": r["created_at"],
                },
            )


def _flush() -> None:
    assert app.state.stream_event_sink.flush_now(2.0)


def _get_list(client, **params):
    return client.get(LIST_URL, cookies=DEV, params=params)


def _get_detail(client, thread_id: str, **params):
    return client.get(_detail_url(thread_id), cookies=DEV, params=params)


# ---------------------------------------------------------------------------
# 1. 鉴权三态 × 两个端点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [LIST_URL, _detail_url("thread-auth")])
def test_auth_three_states(client, url):
    """无 cookie → 401 UNAUTHENTICATED；manager → 403 FORBIDDEN（details None）；
    developer → 200。"""
    resp = client.get(url)
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"

    resp = client.get(url, cookies=MGR)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "FORBIDDEN"
    assert body["details"] is None

    resp = client.get(url, cookies=DEV)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. 响应形状（精确集合相等）
# ---------------------------------------------------------------------------


def test_response_shape_exact_keys(client):
    _seed([
        _row("s-shape-c0001", "s-shape", "thread-shape", "stream_created",
             record_kind="stream_created"),
        _row("s-shape-e0001", "s-shape", "thread-shape", "agent_started",
             sequence=1),
    ])

    body = _get_list(client).json()
    assert set(body) == LIST_BODY_KEYS
    assert set(body["items"][0]) == LIST_ITEM_KEYS

    detail = _get_detail(client, "thread-shape").json()
    assert set(detail) == DETAIL_BODY_KEYS
    assert set(detail["items"][0]) == DETAIL_ITEM_KEYS


# ---------------------------------------------------------------------------
# 2b. 时间字段真的带了 UTC 偏移（钉 `_iso_utc` 的**接线**，不只是纯函数语义）
# ---------------------------------------------------------------------------


def test_timestamps_carry_utc_offset(client):
    """三处取值点（``created_at`` / ``first_seen`` / ``last_seen``）的响应串
    **必须带 ``+00:00``**。

    这条堵的正是 §5.1 点名的陷阱：``_iso_utc`` 的纯函数语义测得再全，也
    **锁不住它有没有被接上**——把 ``_iso_utc(x)`` 换成 ``x.isoformat()``
    （naive 不带偏移）会让前端 ``new Date('…T02:11:07')`` 按**本地时区**解析
    → 时间线整体偏 8 小时，而本机/CI 恰好是 UTC 时**测不出来**。

    **非恒真**：种进库的 ``created_at`` 是本用例**显式指定**的字面量
    （不是 ``now()``、也不是 DEFAULT），故 naive 直出会得到不带偏移的串 → 会红。
    """
    _seed([
        _row("ts-e0001", "ts-s1", "thread-ts", "agent_started", sequence=1,
             created_at="2026-09-18 02:11:07.421337"),
    ])

    created = _get_detail(client, "thread-ts").json()["items"][0]["created_at"]
    assert created == "2026-09-18T02:11:07.421337+00:00"
    assert created.endswith("+00:00")

    summary = _get_list(client).json()["items"][0]
    assert summary["first_seen"] == "2026-09-18T02:11:07.421337+00:00"
    assert summary["last_seen"] == "2026-09-18T02:11:07.421337+00:00"
    for key in ("first_seen", "last_seen"):
        assert summary[key].endswith("+00:00"), (key, summary[key])


# ---------------------------------------------------------------------------
# 3. 聚合精确值（六个计数两两不同）
# ---------------------------------------------------------------------------


def test_aggregate_counts_exact(client):
    """种 2 会话，故意让六个计数**两两不同**——否则「别的字段凑巧同数」
    会救活断言（本仓踩过）。"""
    rows = []
    # s1：1 stream_created + 5 agent_started + 3 chunk + 4 error = 13 行
    rows.append(_row("a-s1-c0001", "a-s1", "thread-counts-a", "stream_created",
                     record_kind="stream_created"))
    seq = 0
    for _ in range(5):
        seq += 1
        rows.append(_row(f"a-s1-e{seq:04d}", "a-s1", "thread-counts-a",
                         "agent_started", sequence=seq))
    for _ in range(3):
        seq += 1
        rows.append(_row(f"a-s1-e{seq:04d}", "a-s1", "thread-counts-a",
                         "agent_reply_chunk", sequence=seq))
    for et in ("agent_error", "task_failed", "memory_compression_failed",
               "agent_error"):
        seq += 1
        rows.append(_row(f"a-s1-e{seq:04d}", "a-s1", "thread-counts-a", et,
                         sequence=seq))
    # s2：5 suppressed + 6 unmapped = 11 行
    for i in range(5):
        rows.append(_row(f"a-s2-x{i + 1:04d}", "a-s2", "thread-counts-a",
                         "agent_reply", record_kind="suppressed"))
    for i in range(6):
        rows.append(_row(f"a-s2-u{i + 1:04d}", "a-s2", "thread-counts-a",
                         "tool_running", record_kind="unmapped", sequence=i + 1))
    # b 会话：1 行
    rows.append(_row("b-s1-e0001", "b-s1", "thread-counts-b", "agent_started",
                     sequence=1, created_at=TS_B))
    _seed(rows)

    body = _get_list(client).json()
    assert body["total"] == 2
    by_id = {i["thread_id"]: i for i in body["items"]}
    a = by_id["thread-counts-a"]
    assert a["event_count"] == 24
    assert a["stream_count"] == 2
    assert a["chunk_count"] == 3
    assert a["error_count"] == 4
    assert a["suppressed_count"] == 5
    assert a["unmapped_count"] == 6
    b = by_id["thread-counts-b"]
    assert (b["event_count"], b["stream_count"], b["chunk_count"],
            b["error_count"], b["suppressed_count"], b["unmapped_count"]) == (
        1, 1, 0, 0, 0, 0,
    )


# ---------------------------------------------------------------------------
# 4. 列表排序（last_seen 倒序 + thread_id 升序次级键）
# ---------------------------------------------------------------------------


def test_list_order_last_seen_desc_thread_id_asc(client):
    ts = "2026-09-18 05:00:00.000000"
    _seed([
        # 两条 last_seen 完全相同 → 必须按 thread_id 升序
        _row("o1-e0001", "o1", "thread-b", "agent_started", sequence=1,
             created_at=ts),
        _row("o2-e0001", "o2", "thread-a", "agent_started", sequence=1,
             created_at=ts),
        _row("o3-e0001", "o3", "thread-c", "agent_started", sequence=1,
             created_at="2026-09-18 04:00:00.000000"),
    ])
    body = _get_list(client).json()
    assert [i["thread_id"] for i in body["items"]] == [
        "thread-a", "thread-b", "thread-c",
    ]


# ---------------------------------------------------------------------------
# 5. 孤儿行（thread_id IS NULL）
# ---------------------------------------------------------------------------


def test_null_thread_rows_excluded_and_counted(client):
    _seed([
        _row("n-e0001", "n-s1", "thread-null-a", "agent_started", sequence=1),
        _row("n-orphan-1", "n-s9", None, "agent_started", sequence=1),
    ])
    body = _get_list(client).json()
    assert [i["thread_id"] for i in body["items"]] == ["thread-null-a"]
    assert body["total"] == 1
    assert body["untracked_events"] == 1


# ---------------------------------------------------------------------------
# 6. 详情顺序（完整排序键；created_at 全相同 + 乱序插入）
# ---------------------------------------------------------------------------


def test_timeline_order_uses_full_sort_key(client):
    ts = "2026-09-18 03:00:00.000000"
    # 乱序插入：s-b 先，s-a 的 seq2 先于 seq1
    _seed([
        _row("s-b-e0002", "s-b", "thread-order", "agent_error",
             sequence=2, created_at=ts),
        _row("s-b-e0001", "s-b", "thread-order", "agent_started",
             sequence=1, created_at=ts),
        _row("s-a-e0002", "s-a", "thread-order", "tool_running",
             sequence=2, created_at=ts),
        _row("s-a-e0001", "s-a", "thread-order", "agent_started",
             sequence=1, created_at=ts),
    ])
    body = _get_detail(client, "thread-order").json()
    assert [i["event_id"] for i in body["items"]] == [
        "s-a-e0001", "s-a-e0002", "s-b-e0001", "s-b-e0002",
    ]
    assert body["total"] == 4


# ---------------------------------------------------------------------------
# 7. missing_events（独立算）
# ---------------------------------------------------------------------------


def test_missing_events_computed_independently(client):
    """``expected`` / ``missing`` 独立算（不用被测算法）。

    ⚠️ 最高 sequence **刻意落在 ``record_kind='unmapped'`` 的行上**：这样若有人
    把「真实流事件」判据从 ``record_kind ∈ ('pushed','unmapped')`` 收窄成
    ``'pushed'``，``expected`` 会掉到 2、``missing`` 变 0 → 立刻红。
    （``unmapped`` 从 ``_submit_event`` 来，带真实 ``sequence``，与生产一致。）
    """
    _seed([
        _row("m-c0001", "m-s1", "thread-missing", "stream_created",
             record_kind="stream_created"),
        _row("m-e0001", "m-s1", "thread-missing", "agent_started", sequence=1),
        _row("m-e0002", "m-s1", "thread-missing", "tool_running", sequence=2),
        _row("m-e0005", "m-s1", "thread-missing", "agent_reply", sequence=5,
             record_kind="unmapped"),
    ])
    body = _get_detail(client, "thread-missing").json()
    assert body["total"] == 4
    assert body["expected_events"] == 5  # max seq over real kinds（含 unmapped）
    assert body["missing_events"] == 2   # 5 - 3 实际真实事件
    assert [i["sequence"] for i in body["items"]] == [0, 1, 2, 5]


# ---------------------------------------------------------------------------
# 8. 类型保真（bool 不是 1；list 等值）
# ---------------------------------------------------------------------------


def test_payload_type_fidelity(client):
    _seed([
        _row("t-e0001", "t-s1", "thread-types", "tool_result", sequence=1,
             payload={"task_id": "t1", "tool_name": "erp_query", "ok": True}),
        _row("t-e0002", "t-s1", "thread-types", "confirmation_requested",
             sequence=2,
             payload={"card_keys": ["amount", "customer"], "re_requested": True}),
    ])
    items = {i["event_id"]: i for i in _get_detail(client, "thread-types").json()["items"]}
    assert items["t-e0001"]["payload"]["ok"] is True  # 不是 1
    assert items["t-e0002"]["payload"]["card_keys"] == ["amount", "customer"]
    assert items["t-e0002"]["payload"]["re_requested"] is True


# ---------------------------------------------------------------------------
# 9. ⚠️ 口径锁：**不关联会话归属**（具体值子串反证，**不能**断言键名）
# ---------------------------------------------------------------------------


def test_no_thread_title_or_account_leaks(client):
    """种一条 ``chat_threads`` 行（``title`` = 具体敏感串、``account`` =
    可辨识值），事件行指向同一 thread_id → 两个端点的响应全文都不得出现它们。

    **这才是能真红的锁**：本步刻意不关联会话表（``title`` 是用户首条消息
    原文、``account`` 是身份数据，都超出「只看骨架」口径）。将来有人图省事
    给 reader 加个关联取标题，这条立刻红。

    **不能写成「直插原文 payload → 断言不出现」**：本步明确**不做读侧再
    投影**，读取层会**忠实返回**库里存的东西；而库里存不进原文（写入侧
    投影白名单 + DB 三条 CHECK 两道锁）。那种写法只在读侧消毒时才成立，
    属于方案 §7.5 自己列的「恒真、什么都锁不住」的假锁。

    **不能断言键名**：``"title" not in text`` 之类会被同字词救活
    （本仓复发过 4 次的假锁模式）→ **必须用具体值**。

    ⚠️ 本用例只往**主库**种 ``chat_threads``；reader 读的是事件库
    （``app.state.stream_event_engine``），那里**没有**这张表——所以将来
    真被加上关联，要么 500、要么标题上屏，**两条路径都会红**。
    """
    thread_id = "thread-owner-leak"
    account = "acct-leak-9f3a"
    title = "客户张三欠款30000"

    factory = app.state.session_factory
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO chat_threads"
                " (thread_id, account, title, created_at, updated_at)"
                " VALUES (:t, :a, :ti, :c, :c)"
            ),
            {"t": thread_id, "a": account, "ti": title, "c": TS_A},
        )
        session.commit()

    _seed([
        _row("lk-e0001", "lk-s1", thread_id, "agent_started", sequence=1),
        _row("lk-e0002", "lk-s1", thread_id, "agent_reply", sequence=2),
    ])

    listing = _get_list(client)
    detail = _get_detail(client, thread_id)
    for resp in (listing, detail):
        assert resp.status_code == 200, resp.text
        assert "张三" not in resp.text
        assert "30000" not in resp.text
        assert account not in resp.text

    # 非恒真：事件确实被读出（否则「不出现」可能只是因为端点什么都没返回）
    body = detail.json()
    assert body["total"] == 2
    assert [i["event_id"] for i in body["items"]] == ["lk-e0001", "lk-e0002"]
    assert body["thread_id"] == thread_id
    assert listing.json()["items"][0]["thread_id"] == thread_id


# ---------------------------------------------------------------------------
# 10. 错误计数闭集
# ---------------------------------------------------------------------------


def test_error_count_is_closed_set(client):
    """``agent_error`` 计数；``task_cancelled`` / ``SESSION_EXPIRED``
    **不算故障**。"""
    _seed([
        _row("e-e0001", "e-s1", "thread-err", "agent_error", sequence=1),
        _row("e-e0002", "e-s1", "thread-err", "task_cancelled", sequence=2),
        _row("e-e0003", "e-s1", "thread-err", "SESSION_EXPIRED", sequence=3),
    ])
    body = _get_list(client).json()
    assert body["items"][0]["error_count"] == 1
    assert body["items"][0]["event_count"] == 3


# ---------------------------------------------------------------------------
# 11. 分页（total 与 items 同口径）
# ---------------------------------------------------------------------------


def test_pagination_timeline(client):
    _seed([
        _row(f"p-e{i:04d}", "p-s1", "thread-page", "agent_started",
             sequence=i)
        for i in range(1, 6)
    ])
    page = _get_detail(client, "thread-page", limit=2, offset=0).json()
    assert len(page["items"]) == 2
    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 0

    tail = _get_detail(client, "thread-page", limit=2, offset=4).json()
    assert [i["event_id"] for i in tail["items"]] == ["p-e0005"]
    assert tail["total"] == 5

    empty = _get_detail(client, "thread-page", limit=2, offset=9).json()
    assert empty["items"] == []
    assert empty["total"] == 5


def test_pagination_threads(client):
    _seed([
        _row(f"q{i}-e0001", f"q{i}-s1", f"thread-p{i}", "agent_started",
             sequence=1,
             created_at=f"2026-09-18 0{i}:00:00.000000")
        for i in range(1, 6)
    ])
    page = _get_list(client, limit=2, offset=0).json()
    assert len(page["items"]) == 2
    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 0

    tail = _get_list(client, limit=2, offset=4).json()
    assert len(tail["items"]) == 1
    assert tail["total"] == 5

    empty = _get_list(client, limit=2, offset=9).json()
    assert empty["items"] == []
    assert empty["total"] == 5


# ---------------------------------------------------------------------------
# 12. 参数校验（断言 error_code，不只断言 status）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "params"),
    [
        (LIST_URL, {"limit": 0}),
        (LIST_URL, {"limit": 201}),
        (LIST_URL, {"offset": -1}),
        (_detail_url("thread-v"), {"limit": 0}),
        (_detail_url("thread-v"), {"limit": 2001}),
        (_detail_url("thread-v"), {"offset": -1}),
        (_detail_url("t" * 129), {}),
    ],
)
def test_validation_errors_422(client, url, params):
    resp = client.get(url, cookies=DEV, params=params)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 13. 未知 / 过期 thread_id → 200 + 空（不是 404）
# ---------------------------------------------------------------------------


def test_unknown_thread_returns_200_empty(client):
    _seed([_row("k-e0001", "k-s1", "thread-known", "agent_started", sequence=1)])
    resp = _get_detail(client, "thread-does-not-exist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["expected_events"] == 0
    assert body["missing_events"] == 0


# ---------------------------------------------------------------------------
# 14. 空库
# ---------------------------------------------------------------------------


def test_empty_db(client):
    body = _get_list(client).json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["untracked_events"] == 0


# ---------------------------------------------------------------------------
# 15. 端到端（真实 POST → 落库 → 新端点读回）
# ---------------------------------------------------------------------------


def test_end_to_end_chat_message_appears_in_list(client, chat_harness):
    """**纯合成插入测不出「写侧 thread_id 与读侧口径是否对得上」**。

    ⚠️ 只发一次 POST —— ``tests/api`` 里并发跑两个后台图线程会挂死。
    """
    thread_id = "thread-timeline-e2e"
    app.state.model_decision_client = ScriptedDecisionClient()
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单", "thread_id": thread_id},
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

    body = _get_list(client).json()
    ids = [i["thread_id"] for i in body["items"]]
    assert thread_id in ids, ids
    summary = next(i for i in body["items"] if i["thread_id"] == thread_id)
    assert summary["event_count"] > 0
    assert summary["stream_count"] == 1

    # 该会话能用详情端点读回，且首行是建流行
    detail = _get_detail(client, thread_id).json()
    assert detail["total"] == summary["event_count"]
    assert detail["items"][0]["record_kind"] == "stream_created"


# ---------------------------------------------------------------------------
# 9b. 绝不用主库 session_factory 兜底（读错库会伪装成「没有数据」）
# ---------------------------------------------------------------------------


class _ReqStub:
    """最小 Request 替身：``_event_scope`` 只读 ``request.app.state``。"""

    app = app


def test_event_scope_never_falls_back_to_session_factory(client):
    """``stream_event_engine`` 缺失时**必须**走 ``SessionLocal``，绝不碰
    ``app.state.session_factory``。

    后者在测试里指向**主 SQLite 库**，那张 ``chat_stream_events`` 由
    ``Base.metadata.create_all`` 建出却**是空表**——一旦兜底过去，DB 故障 /
    读错库会被静默伪装成「没有数据」（本仓刚吃过的假绿模式）。
    """
    from administrative_agent.web import routes_developer

    called: list[int] = []

    def _forbidden():
        called.append(1)
        raise AssertionError("不得使用 app.state.session_factory")

    orig_factory = app.state.session_factory
    orig_engine = app.state.stream_event_engine
    app.state.session_factory = _forbidden
    _unset_state("stream_event_engine")
    try:
        # 本地无 PG / 无 URL 都无所谓：本用例只断言 session_factory 未被触碰
        try:
            with routes_developer._event_scope(_ReqStub())():
                pass
        except Exception:
            pass
        assert called == [], "读侧兜底到了 app.state.session_factory（主库空表）"
    finally:
        app.state.session_factory = orig_factory
        app.state.stream_event_engine = orig_engine


# ---------------------------------------------------------------------------
# 16. 静态口径断言（reader 源码不得出现会话表 / JOIN）
# ---------------------------------------------------------------------------


def test_reader_source_has_no_thread_table_or_join():
    """「用测试锁住不做某事」：reader 只许读一张事件表——出现 ``ChatThread`` /
    ``chat_threads`` / ``join(`` 即说明有人把会话归属关联进来了。"""
    src = Path(stream_event_reader.__file__).read_text(encoding="utf-8")
    for token in ("ChatThread", "chat_threads", "join("):
        assert token not in src, f"reader 源码出现禁用字样：{token!r}"
