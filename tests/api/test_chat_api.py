"""chat 端点 API 测试：异步提交契约、SSE 事件流、取消、会话索引。

## 2026-08-29 重写说明

原文件因导入 `EVENT_MODEL_STUB` / `EVENT_TASK_CREATED` / `EVENT_TASK_INTENT`
/ `EVENT_TASK_QUEUED` / `EVENT_TASK_WAITING_INPUT` 而整体无法收集——这五个
事件随 D26「规则化意图分类 → LLM 直接决策」改造全部删除。原文件断言的
编排语义（`intent.duty/operation` 由服务端规则判定、歧义 → waiting_user_input、
只读 → MODEL_PIPELINE_NOT_CONNECTED）所依赖的中间件已经不存在。

被测的**端点与协议**全部存活，故按新契约重写，覆盖分四组：

| 组 | 覆盖 | 变更点 |
|---|---|---|
| POST /messages | 异步契约、401/403、CSRF、503 fail closed | `task_id` 由立即建任务改为 **None**（task_id 图未跑完未知）；不再有 `intent` 字段 |
| GET /streams/{id} | 事件五要素、sequence 递增、Last-Event-ID 续传、SESSION_EXPIRED、404、权限 | 事件类型换了一批，协议本身（L530/L531/L534）未变 |
| POST /streams/{id}/cancel | task_cancelled 收尾、CSRF、404 | 未变 |
| GET /history、/threads | 重放空快照、thread_id 必填、账号隔离 | **新增**：此前零覆盖 |

**关于确定性**：SSE 协议组（五要素 / sequence / 续传 / 过期）直接经
`StreamStore` 造流，不走后台图线程——断言的是 SSE 端点的协议语义，
与图执行时序无关，否则会退化成 sleep 轮询的脆弱用例。图执行语义由
`tests/api/test_injection_api.py`（`chat_harness`）覆盖。

**关于权限顺序**：`require_manager` 是 FastAPI 依赖，先于 handler 体内的
`check_csrf` 求值，故「无 cookie」必为 401 而非 403——即使同时缺 Origin。

**关于 `_ACTIVE_THREADS`（并发防护登记）**：它是 `routes_chat` 的**模块级
全局 set**，`client` 夹具不清它，后台图线程在 `finally` 里才释放。这带来
两个写法要求（踩过一次 503 用例被前一个用例的残留登记顶成 409）：
1. 需要 POST 成功的用例一律显式注入 `ScriptedDecisionClient`——否则走
   默认真实客户端单例，既不确定也可能发起真实调用；
2. 每个 POST 用例用**各自的 thread_id**，不共用常量，避免用例间耦合。
"""

from __future__ import annotations

import json

from administrative_agent.auth.models import Role
from administrative_agent.main import app
from administrative_agent.persistence.repositories import SqlAlchemyThreadRepository
from administrative_agent.web.chat_streams import (
    EVENT_AGENT_REPLY,
    EVENT_AGENT_STARTED,
    EVENT_SESSION_EXPIRED,
    EVENT_TASK_CANCELLED,
)

from tests.api.conftest import (
    DEVELOPER,
    MGR,
    VALID_ORIGIN,
    ScriptedDecisionClient,
    cookies,
    headers,
)

THREAD = "thread-chat-test"
SEED_STREAM = "stream-seeded"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict]:
    """text/event-stream → 事件 dict 列表（id/event/data 三段解析）。"""
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        evt: dict[str, object] = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                evt["id"] = line[4:]
            elif line.startswith("event: "):
                evt["event"] = line[7:]
            elif line.startswith("data: "):
                evt["data"] = json.loads(line[6:])
        events.append(evt)
    return events


def _seed_stream(*, count: int = 3, thread_id: str = THREAD) -> str:
    """直接造一条含 count 个事件的流（绕开后台图线程，结果确定）。"""
    streams = app.state.chat_streams
    streams.create(SEED_STREAM, thread_id=thread_id)
    for i in range(count):
        streams.append(
            SEED_STREAM,
            event_type=EVENT_AGENT_STARTED if i == 0 else EVENT_AGENT_REPLY,
            task_id=None,
            payload={"i": i + 1},
            thread_id=thread_id,
        )
    return SEED_STREAM


def _submit(client, message: str, *, thread: str = THREAD, origin: str = VALID_ORIGIN) -> dict:
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": message, "thread_id": thread},
        cookies=MGR,
        headers=headers(origin),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _stream(client, stream_id: str, **extra_headers) -> list[dict]:
    resp = client.get(
        f"/api/v1/chat/streams/{stream_id}",
        cookies=MGR,
        headers={**headers(), **extra_headers},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(resp.text)


# ---------------------------------------------------------------------------
# POST /api/v1/chat/messages：异步契约与权限
# ---------------------------------------------------------------------------


def test_submit_returns_async_contract(client, chat_harness):
    """D26 异步契约：POST 立即返回，task_id 为 None（图未跑完未知）。"""
    app.state.model_decision_client = ScriptedDecisionClient()
    body = _submit(client, "查询今天的订单", thread="thread-contract")
    assert set(body) == {"thread_id", "stream_id", "task_id"}
    assert body["thread_id"] == "thread-contract"
    assert body["stream_id"].startswith("stream-")
    assert body["task_id"] is None  # 对话轮次不预建任务


def test_submit_generates_thread_id_when_omitted(client, chat_harness):
    """thread_id 缺省 → 服务端生成 `thread-{account}-{uuid8}`。"""
    app.state.model_decision_client = ScriptedDecisionClient()
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "查询今天的订单"},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    thread_id = resp.json()["thread_id"]
    assert thread_id.startswith("thread-manager01-")
    assert len(thread_id) == len("thread-manager01-") + 8


def test_submit_requires_manager(client):
    """依赖先于 handler 求值：无会话 401，developer 403（与 CSRF 无关）。"""
    assert client.post(
        "/api/v1/chat/messages", json={"message": "x"}, headers=headers()
    ).status_code == 401
    assert client.post(
        "/api/v1/chat/messages",
        json={"message": "x"},
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403


def test_submit_requires_csrf(client):
    """写 API 缺同源 Origin → 403 + 稳定错误响应四要素。"""
    resp = client.post(
        "/api/v1/chat/messages", json={"message": "x"}, cookies=MGR
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "CSRF_VALIDATION_FAILED"
    assert set(body) == {"error_code", "message", "details", "trace_id"}


def test_submit_503_when_checkpoint_not_wired(client):
    """checkpoint 未接线 → 同步 503（fail closed，不降级为内存存储）。"""
    resp = client.post(
        "/api/v1/chat/messages",
        json={"message": "x", "thread_id": "thread-no-checkpoint"},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# SSE：五要素 / sequence / Last-Event-ID 续传（2.7.4 L530-531）
# ---------------------------------------------------------------------------


def test_sse_event_has_five_fields(client):
    """L530：data 五要素齐全，且 id/event 行与 data 内字段一致。"""
    events = _stream(client, _seed_stream())
    assert len(events) == 3
    for e in events:
        data = e["data"]
        assert set(data) == {"event_id", "event_type", "task_id", "sequence", "payload"}
        assert e["id"] == data["event_id"]
        assert e["event"] == data["event_type"]


def test_sse_sequence_starts_at_one_and_increments(client):
    """sequence 流内严格从 1 递增（第 N 个事件 = N）。"""
    events = _stream(client, _seed_stream(count=4))
    assert [e["data"]["sequence"] for e in events] == [1, 2, 3, 4]


def test_sse_last_event_id_resumes_and_skips_duplicates(client):
    """L531：带 Last-Event-ID 只续传其后的事件；重复 sequence 不重发。"""
    stream_id = _seed_stream(count=4)
    events = _stream(client, stream_id)

    resumed = _stream(client, stream_id, **{"Last-Event-ID": events[1]["id"]})
    assert [e["data"]["sequence"] for e in resumed] == [3, 4]

    # 已收到最后一个 → 无新事件
    assert _stream(client, stream_id, **{"Last-Event-ID": events[-1]["id"]}) == []


def test_sse_unparseable_last_event_id_replays_all(client):
    """无法识别的 Last-Event-ID → 从头全量重放（前端按 sequence 去重）。"""
    stream_id = _seed_stream(count=3)
    events = _stream(client, stream_id, **{"Last-Event-ID": "unknown-id"})
    assert [e["data"]["sequence"] for e in events] == [1, 2, 3]


def test_sse_session_expired_appended_at_tail(client):
    """L534：流被标记过期 → 事件末尾追加 SESSION_EXPIRED。"""
    stream_id = _seed_stream(count=2)
    app.state.chat_streams.mark_expired(stream_id)

    events = _stream(client, stream_id)
    assert events[-1]["data"]["event_type"] == EVENT_SESSION_EXPIRED
    assert events[-1]["data"]["payload"]["reason"] == "session_expired"
    assert events[-1]["data"]["sequence"] == 3  # 紧接已产出事件之后


def test_sse_stream_not_found(client):
    resp = client.get(
        "/api/v1/chat/streams/stream-unknown", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "STREAM_NOT_FOUND"


def test_sse_requires_manager(client):
    assert client.get(
        "/api/v1/chat/streams/stream-x", headers=headers()
    ).status_code == 401
    assert client.get(
        "/api/v1/chat/streams/stream-x",
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403


# ---------------------------------------------------------------------------
# POST /streams/{id}/cancel（L506：取消当前生成 ≠ 取消已执行写操作）
# ---------------------------------------------------------------------------


def test_cancel_stream_ends_generation_without_deleting_events(client):
    """取消以 task_cancelled 收尾，已产出事件不删除（前端可续播）。"""
    stream_id = _seed_stream(count=2)

    resp = client.post(
        f"/api/v1/chat/streams/{stream_id}/cancel", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "已取消当前生成。"

    events = _stream(client, stream_id)
    assert [e["data"]["event_type"] for e in events] == [
        EVENT_AGENT_STARTED,
        EVENT_AGENT_REPLY,
        EVENT_TASK_CANCELLED,
    ]
    assert events[-1]["data"]["payload"]["reason"] == "generation_cancelled"
    assert [e["data"]["sequence"] for e in events] == [1, 2, 3]


def test_cancel_stream_requires_csrf_and_manager(client):
    resp = client.post("/api/v1/chat/streams/stream-x/cancel", cookies=MGR)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"
    assert client.post(
        "/api/v1/chat/streams/stream-x/cancel", headers=headers()
    ).status_code == 401


def test_cancel_stream_not_found(client):
    resp = client.post(
        "/api/v1/chat/streams/stream-unknown/cancel",
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "STREAM_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /history、/threads（此前零覆盖）
# ---------------------------------------------------------------------------


def test_history_returns_empty_shape_when_no_snapshot(client, chat_harness):
    """无 checkpoint 快照 → 空历史三元组（不猜测、不重放）。"""
    resp = client.get(
        f"/api/v1/chat/history?thread_id={THREAD}", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "messages": [],
        "pending_confirmation": None,
        "stream_id": None,
    }


def test_history_requires_thread_id(client, chat_harness):
    """thread_id 为必填 Query（min_length=1）→ 缺参数 422。"""
    assert (
        client.get("/api/v1/chat/history", cookies=MGR, headers=headers()).status_code
        == 422
    )


def test_threads_lists_only_current_account(client):
    """会话索引按账号隔离：只返回当前账号的会话。"""
    repo = SqlAlchemyThreadRepository(app.state.session_factory)
    repo.upsert("thread-mine", "manager01", "我的会话")
    repo.upsert("thread-other", "someone-else", "他人的会话")

    resp = client.get("/api/v1/chat/threads", cookies=MGR, headers=headers())
    assert resp.status_code == 200
    thread_ids = [t["thread_id"] for t in resp.json()["threads"]]
    assert thread_ids == ["thread-mine"]  # 他人会话不可见


def test_threads_requires_manager(client):
    assert client.get("/api/v1/chat/threads", headers=headers()).status_code == 401
    assert (
        client.get(
            "/api/v1/chat/threads",
            cookies=cookies(Role.DEVELOPER, DEVELOPER),
            headers=headers(),
        ).status_code
        == 403
    )
