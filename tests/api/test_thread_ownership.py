"""thread / task 归属校验 API 测试（多 agent 演进 · 机制前置 · 交付 B）。

判定规则只有一条：``record is not None and record.account != account.account``
→ 404（文案「会话不存在。」，不泄露存在性）；**索引行缺失一律放行**。

为什么不能严格口径（缺失即 404）：``chat_threads`` 索引行有两个写入点——
``_claim_thread``（提交时登记，仅限「缺行且线程无既有内容」）与后台 drain 的
**成功分支**——两者都可能在图执行后才落行，故「有 checkpoint、无索引行」是
合法且可复现的状态，且首次提交时行本就不存在——严格 404 会打红既有用例、
让失败/取消线程恢复看到「历史加载失败」、让新线程首次提交即 404。

多账号风格照 ``test_auth_multi_manager``：直接复用其 helper（不改那个文件）。
"""

from __future__ import annotations

import time

import pytest

from administrative_agent.auth.service import SESSION_COOKIE_NAME
from administrative_agent.main import app
from administrative_agent.persistence.repositories import (
    SqlAlchemyTaskRepository,
    SqlAlchemyThreadRepository,
)
from administrative_agent.tasks.records import TaskRecord
from administrative_agent.web import routes_chat

from tests.api.conftest import ScriptedDecisionClient, headers
from tests.api.test_auth_multi_manager import (
    MGR1,
    MGR1_PW,
    MGR2,
    MGR2_PW,
    _login,
    _set_managers,
    _wait_run_finished,
)

READ_MSG = "查询今天的订单"


@pytest.fixture()
def managers(client, monkeypatch):
    """两个 manager 真实登录 → {account: cookies dict}（发起请求用）。"""
    _set_managers(monkeypatch, (MGR1, MGR1_PW), (MGR2, MGR2_PW))
    out = {}
    for acc, pw in ((MGR1, MGR1_PW), (MGR2, MGR2_PW)):
        resp = _login(client, acc, pw)
        assert resp.status_code == 200, resp.text
        out[acc] = {SESSION_COOKIE_NAME: resp.cookies.get(SESSION_COOKIE_NAME)}
    return out


def _repo() -> SqlAlchemyThreadRepository:
    return SqlAlchemyThreadRepository(app.state.session_factory)


def _spy_upsert(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """spy ``SqlAlchemyThreadRepository.upsert``，返回逐个调用的位置参数列表。

    每个元素 = 一次调用的 ``(thread_id, account, title)`` 元组。用于**确定性**
    断言 claim 是否触发写入——绕开共享 SQLite 连接（``StaticPool``）下
    ``updated_at`` 往返读数不稳定的问题（见 ``test_claim_does_not_refresh
    _existing_row`` docstring）。桩沿用真实实现，不影响读取。
    """
    calls: list[tuple] = []
    real_upsert = SqlAlchemyThreadRepository.upsert

    def _record(self, *args: object, **kwargs: object) -> None:
        calls.append(args)
        return real_upsert(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyThreadRepository, "upsert", _record)
    return calls


def _submit(client, cookies_, thread: str, message: str = READ_MSG):
    return client.post(
        "/api/v1/chat/messages",
        json={"message": message, "thread_id": thread},
        cookies=cookies_,
        headers=headers(),
    )


def _readonly_client() -> None:
    app.state.model_decision_client = ScriptedDecisionClient()


def _start_pending(client, cookies_, thread: str, erp) -> str:
    """提交一个写工具（order_create）→ 等 confirmation_requested → 返回 stream_id。

    interrupt 即 drain 结束，故随后 ``_wait_run_finished`` 会很快返回。
    """
    app.state.model_decision_client = ScriptedDecisionClient(
        tool_name="order_create",
        tool_arguments={
            "order_type": "metal",
            "customer_name": "示例机械有限公司",
            "model": "MK-01",
            "quantity": 2,
            "weight_unit": "kg",
        },
        always_call_tool=True,
    )
    erp.rows_by_entity["customers"] = [
        {"id": 11, "company_name": "示例机械有限公司", "unit_price": 120.0, "price_type": "含税"}
    ]
    resp = _submit(client, cookies_, thread, "帮我创建下一张钣金订单")
    assert resp.status_code == 200, resp.text
    stream_id = resp.json()["stream_id"]
    streams = app.state.chat_streams
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        types = [e.event_type for e in streams.events(stream_id)]
        if "confirmation_requested" in types:
            break
        if "agent_error" in types:
            pytest.fail("写路径未进入确认流：" + str(types))
        time.sleep(0.05)
    else:
        pytest.fail("写路径未进入 interrupt 确认流（无 confirmation_requested）")
    _wait_run_finished(thread)
    return stream_id


class _ExplodingDecisionClient:
    """决策即抛错的模型桩：把 claim 登记与后台成功路径 upsert 解耦。

    提交后后台 drain 在 model 节点抛 ``RuntimeError``（非 ``ModelCallError``，
    不被 model_node 吸收）→ 冒泡到 ``_run_submit_background`` 的 catch-all
    ``except Exception`` → agent_error 终态、**不进 else 成功分支**，故
    ``chat_threads`` 索引行只可能由提交时的 ``_claim_thread`` 产生。用于
    确定性断言「claim 未登记」。
    """

    def generate_decision(self, **_: object) -> object:
        raise RuntimeError("scripted decision failure")


# ---------------------------------------------------------------------------
# POST /chat/messages：提交即登记 + 归属校验
# ---------------------------------------------------------------------------


def test_submit_claims_thread_before_run_finishes(client, chat_harness, managers):
    """提交返回后立即 ``repo.get(tid).account == MGR1``（提交即登记，不等 drain）。"""
    _readonly_client()
    tid = "thread-claim-1"
    resp = _submit(client, managers[MGR1], tid)
    assert resp.status_code == 200, resp.text
    record = _repo().get(tid)
    assert record is not None and record.account == MGR1
    _wait_run_finished(tid)


def test_submit_foreign_thread_404_and_no_side_effects(client, chat_harness, managers):
    """他人线程提交 → 404，且无任何副作用（不占活动锁、checkpoint 未变、ERP 零调用）。"""
    _readonly_client()
    tid = "thread-foreign-1"
    _repo().upsert(tid, MGR2, "他人会话")
    service = app.state.checkpoint_service
    before = service.load_thread(tid)

    resp = _submit(client, managers[MGR1], tid)
    assert resp.status_code == 404
    assert resp.json()["message"] == "会话不存在。"
    with routes_chat._ACTIVE_THREADS_LOCK:
        assert tid not in routes_chat._ACTIVE_THREADS
    assert service.load_thread(tid) == before  # checkpoint 未变
    assert chat_harness.calls == []  # FakeMCPClient 零调用


def test_submit_claim_does_not_rebind_existing_row(client, chat_harness, managers):
    """claim 只在行缺失时写；行已存在 → 归属不改绑（不抢占他人线程）。"""
    from types import SimpleNamespace

    from administrative_agent.auth.models import Account, Role

    tid = "thread-rebind-1"
    _repo().upsert(tid, MGR1, "原始标题")
    # 直接调 _claim_thread（模拟"行已存在时又来一次 claim"，即便来者是 MGR2）
    request = SimpleNamespace(app=app)
    routes_chat._claim_thread(
        request, Account(account=MGR2, role=Role.MANAGER), tid, "新标题"
    )
    record = _repo().get(tid)
    assert record.account == MGR1  # 归属未改绑
    assert record.title == "原始标题"  # 标题也不被覆盖


def test_claim_does_not_refresh_existing_row(client, chat_harness, managers, monkeypatch):
    """**钉住「只在行缺失时写」**：行已存在（他人账号）时 claim **不调用 upsert**。

    复审实测：把 ``_claim_thread`` 里 ``if repo.get(thread_id) is not None:
    return`` 整句删掉（变成对已存在行也 upsert）后，本用例**前身**（以
    ``updated_at`` 是否被刷新为判据）在仓库实际使用的 **8 文件组合命令**下
    **完全不变红**（89 passed）、单文件整跑也只有约一半次数变红。根因是
    ``_repo().get()`` 在 ``StaticPool`` 单条共享 SQLite 连接 +
    ``expire_on_commit=False`` 夹具下**有时读回 claim 写入之前的旧值**，
    前序用例的后台图线程在同一连接上 drain/upsert 会改变这个窗口。

    故本用例改用**确定性的调用观测**——直接 spy ``upsert`` 这个方法调用，
    断言其**未被触发**，不再依赖副作用（``updated_at``）在共享连接上是否
    可见。反向对照见 ``test_claim_upserts_when_row_missing``（防「upsert
    被整体删掉也绿」的假绿）。
    """
    from types import SimpleNamespace

    from administrative_agent.auth.models import Account, Role

    tid = "thread-claim-no-refresh"
    _repo().upsert(tid, MGR2, "他人会话")  # 先落行（此时用真实 upsert）
    before = _repo().get(tid)
    assert before.account == MGR2

    upsert_calls = _spy_upsert(monkeypatch)

    request = SimpleNamespace(app=app)
    routes_chat._claim_thread(
        request, Account(account=MGR1, role=Role.MANAGER), tid, "新标题"
    )

    assert upsert_calls == []  # 行已存在 → claim 未触发任何 upsert
    after = _repo().get(tid)
    assert after.account == MGR2  # 归属未改绑
    assert after.title == before.title  # 标题未被覆盖


def test_claim_upserts_when_row_missing(client, chat_harness, managers, monkeypatch):
    """**反向对照**：行缺失且线程无既有内容 → claim **恰好 upsert 一次**。

    防「把 ``upsert`` 从 ``_claim_thread`` 整体删掉」也让
    ``test_claim_does_not_refresh_existing_row`` 变绿（后者只断言「未被
    调用」，对「从不调用」的退化实现同样通过）。本用例钉住另一面。
    """
    from types import SimpleNamespace

    from administrative_agent.auth.models import Account, Role

    tid = "thread-claim-missing"
    assert _repo().get(tid) is None  # 行缺失
    assert app.state.checkpoint_service.load_thread(tid) is None  # 且无内容

    upsert_calls = _spy_upsert(monkeypatch)

    request = SimpleNamespace(app=app)
    routes_chat._claim_thread(
        request, Account(account=MGR1, role=Role.MANAGER), tid, "新会话"
    )

    assert len(upsert_calls) == 1  # 恰好一次
    assert upsert_calls[0] == (tid, MGR1, "新会话")  # 参数即归属登记
    record = _repo().get(tid)
    assert record is not None and record.account == MGR1


def test_submit_does_not_claim_thread_with_existing_content(
    client, chat_harness, managers
):
    """**修复 1 部分证据（仅失败路径）**：有 checkpoint 内容、无索引行的线程 →
    他人提交不登记归属。

    制造「有内容、无行」：MGR1 先提交并跑完（落 checkpoint），再删索引行
    （复用 ``test_history_failed_thread_without_index_row_replays_messages``
    手法）。随后 MGR2 提交同一 thread_id：后台 drain 用 ``_ExplodingDecisionClient``
    （异常分支不做成功路径 upsert），故索引行只可能由 ``_claim_thread`` 产生——
    断言其仍不存在，证明「线程已有内容 → 不登记」前置生效；进而断言 MGR1 仍能
    读自己的线程（不被永久 404 锁出）。

    **已知残留（如实记录）**：本批只**部分缓解**接管，**非完整收口**。
    - 「无索引行的线程可被他人读取/追加」这一面**仍未收口**——MGR2 的提交仍 200
      并读到 MGR1 的内容（``_require_thread_access`` 宽松主口径不变）；
    - **更严重的一半：drain 成功路径的接管仍然成立**。本用例刻意用
      ``_ExplodingDecisionClient`` 走**异常分支**，因此**只在「跑失败」路径上
      取证**；drain **成功**时，``_run_submit_background`` 的成功分支仍会
      **无条件** ``upsert(thread_id, 提交者, ...)``，而行缺失时 ``upsert`` 是
      INSERT 且用传入的 account → **该行被 INSERT 成 MGR2，MGR1 被永久 404
      锁出、接管照样完成**（提交者发一句能正常跑完的消息即可触发）；
    - 故**本用例绿 ≠ 接管被堵住**：它只证明了失败路径不落行，成功路径未被覆盖，
      仍可接管。完整收口留待后续批次（服务端强制生成 thread_id / 提交时持久化
      归属）。
    """
    _readonly_client()
    tid = "thread-has-content-no-row"
    assert _submit(client, managers[MGR1], tid).status_code == 200
    _wait_run_finished(tid)
    _repo().delete(tid)  # 制造「有 checkpoint、无索引行」
    assert _repo().get(tid) is None
    assert app.state.checkpoint_service.load_thread(tid) is not None  # 确有内容

    app.state.model_decision_client = _ExplodingDecisionClient()
    resp = _submit(client, managers[MGR2], tid)
    assert resp.status_code == 200, resp.text  # 宽松口径：他人线程不 404
    _wait_run_finished(tid)

    # claim 未登记：索引行仍不存在（后台成功分支因 drain 失败未执行）
    assert _repo().get(tid) is None

    # MGR1 仍能读写自己的线程（不被锁出）
    hist = client.get(
        f"/api/v1/chat/history?thread_id={tid}",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert hist.status_code == 200


def test_submit_invalid_attachment_400_does_not_create_index_row(
    client, chat_harness, managers
):
    """附件非法（400）在 claim **之前** → 索引行未被创建（无幽灵会话）。"""
    _readonly_client()
    tid = "thread-bad-attachment"
    resp = client.post(
        "/api/v1/chat/messages",
        json={
            "message": "看下这个表",
            "thread_id": tid,
            "attachment_ids": ["att-missing"],
        },
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 400, resp.text
    assert _repo().get(tid) is None  # claim 未在 400 之前建行


def test_submit_foreign_thread_with_invalid_attachment_404_not_400(
    client, chat_harness, managers
):
    """**钉住「404 优先于 400」的顺序语义**：他人线程 + 非法附件 → 404（非 400）。

    ``submit_message`` 里 ``_require_thread_access`` 刻意排在
    ``_validate_attachments`` **之前**（见该处注释「保证 404 优先于 400」）。
    既有唯一带 ``attachment_ids`` 的用例
    （``test_submit_invalid_attachment_400_does_not_create_index_row``）用的是
    **本账号**线程，在归属校验下本就放行 → 该顺序被颠倒（把附件校验前移）
    它照样绿，故这半边语义此前**无用例保护**。本用例以**他人线程 + 同一个
    无效附件 id** 补齐：若顺序被颠倒，附件校验会先返回 400 而漏出 —— 断言
    404 即可钉住优先序（同时证明不对他人线程做附件表探测）。
    """
    _readonly_client()
    tid = "thread-foreign-bad-attachment"
    _repo().upsert(tid, MGR2, "他人会话")

    resp = client.post(
        "/api/v1/chat/messages",
        json={
            "message": "看下这个表",
            "thread_id": tid,
            "attachment_ids": ["att-missing"],
        },
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404, resp.text  # 404 优先于 400
    assert resp.json()["message"] == "会话不存在。"
    assert _repo().get(tid).account == MGR2  # 归属未被改动（无副作用）


# ---------------------------------------------------------------------------
# GET /chat/history：归属校验 + 缺失行放行
# ---------------------------------------------------------------------------


def test_history_foreign_thread_404(client, chat_harness, managers):
    """他人线程 → 404（不泄露存在性）。"""
    tid = "thread-hist-foreign"
    _repo().upsert(tid, MGR2, "他人")
    resp = client.get(
        f"/api/v1/chat/history?thread_id={tid}",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["message"] == "会话不存在。"


def test_history_missing_index_row_returns_200_empty(client, chat_harness, managers):
    """**显式钉子**：无索引行、无快照 → 200 空三元组（不准退回严格口径）。"""
    resp = client.get(
        "/api/v1/chat/history?thread_id=thread-no-row",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "pending_confirmation": None, "stream_id": None}


def test_history_failed_thread_without_index_row_replays_messages(
    client, chat_harness, managers
):
    """**不误伤恢复路径**：有 checkpoint、无索引行（失败/取消线程的合法态）仍可重放。"""
    _readonly_client()
    tid = "thread-no-row-replay"
    assert _submit(client, managers[MGR1], tid).status_code == 200
    _wait_run_finished(tid)
    _repo().delete(tid)  # 模拟"有 checkpoint、无索引行"
    assert _repo().get(tid) is None

    resp = client.get(
        f"/api/v1/chat/history?thread_id={tid}",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["messages"]  # 历史被重放（不误判为他人线程）


# ---------------------------------------------------------------------------
# POST /chat/confirm：归属校验 + 缺失行放行（本口径核心证据）
# ---------------------------------------------------------------------------


def test_confirm_foreign_thread_404_and_card_untouched(
    client, chat_harness, managers
):
    """他人挂起线程确认 → 404，且挂起卡原样（不被消费）。"""
    tid = "thread-confirm-foreign"
    _start_pending(client, managers[MGR2], tid, chat_harness)
    before = app.state.checkpoint_service.load_thread(tid)
    assert before.get("pending_confirmation")  # 确有挂起卡

    resp = client.post(
        "/api/v1/chat/confirm",
        json={"thread_id": tid, "approved": True, "payload_hash": "x"},
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["message"] == "会话不存在。"
    after = app.state.checkpoint_service.load_thread(tid)
    assert after.get("pending_confirmation") == before.get("pending_confirmation")


def test_confirm_allows_pending_thread_without_index_row(
    client, chat_harness, managers
):
    """**本口径核心证据**：挂起线程无索引行时仍可确认（严格口径会打死它）。"""
    tid = "thread-confirm-no-row"
    _start_pending(client, managers[MGR1], tid, chat_harness)
    _repo().delete(tid)  # 制造"有挂起卡、无索引行"（合法态；首次提交前亦如此）
    pc = app.state.checkpoint_service.load_thread(tid)["pending_confirmation"]

    resp = client.post(
        "/api/v1/chat/confirm",
        json={"thread_id": tid, "approved": True, "payload_hash": pc["payload_hash"]},
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    _wait_run_finished(tid)


# ---------------------------------------------------------------------------
# stream 端点：stream → thread 反向映射归属校验
# ---------------------------------------------------------------------------


def test_stream_events_foreign_stream_404(client, chat_harness, managers):
    """他人线程的流 → 404 STREAM_NOT_FOUND。"""
    _repo().upsert("thread-stream-foreign", MGR2, "他人")
    app.state.chat_streams.create("stream-foreign", thread_id="thread-stream-foreign")
    resp = client.get(
        "/api/v1/chat/streams/stream-foreign",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "STREAM_NOT_FOUND"


def test_cancel_foreign_stream_404_and_not_cancelled(client, chat_harness, managers):
    """取消他人流 → 404，且该流未被取消（无副作用）。"""
    _repo().upsert("thread-cancel-foreign", MGR2, "他人")
    app.state.chat_streams.create("stream-cancel-foreign", thread_id="thread-cancel-foreign")
    resp = client.post(
        "/api/v1/chat/streams/stream-cancel-foreign/cancel",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "STREAM_NOT_FOUND"
    assert app.state.chat_streams.is_cancelled("stream-cancel-foreign") is False


# ---------------------------------------------------------------------------
# POST /tasks/{id}/confirm（compat）：任务归属校验
# ---------------------------------------------------------------------------


def test_tasks_confirm_foreign_task_404(client, chat_harness, managers):
    """他人任务确认 → 404（复用 TASK_NOT_FOUND 错误体）。"""
    tid = "thread-task-foreign"
    _repo().upsert(tid, MGR2, "他人")
    SqlAlchemyTaskRepository(app.state.session_factory).create(
        TaskRecord(
            task_id="task-foreign-1",
            thread_id=tid,
            operation_id="op-1",
            actor_account=MGR2,
            actor_role="manager",
            duty_code="R2",
            original_user_message="创建订单",
        )
    )
    resp = client.post(
        "/api/v1/tasks/task-foreign-1/confirm",
        json={"approved": True},
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# 已知残留 + 回归
# ---------------------------------------------------------------------------


def test_unindexed_thread_is_adoptable_by_first_claimant(
    client, chat_harness, managers
):
    """**已知残留**：无行且无 checkpoint 的 thread_id，先提交者得（当前口径如此）。

    这是有意接受的残留——index 行是首次提交才建立的，无法在"首个提交"时
    区分"新线程"与"他人尚未登记"; 记录此用例以防被误当成 bug。
    """
    _readonly_client()
    tid = "thread-first-claimant"
    assert _repo().get(tid) is None
    assert _submit(client, managers[MGR1], tid).status_code == 200
    _wait_run_finished(tid)
    assert _repo().get(tid).account == MGR1  # 先提交者得


def test_own_thread_flows_unchanged(client, chat_harness, managers):
    """回归：自己线程 submit / history / cancel / confirm 全部 200。"""
    _readonly_client()
    tid = "thread-own-ok"
    resp = _submit(client, managers[MGR1], tid)
    assert resp.status_code == 200
    stream_id = resp.json()["stream_id"]
    _wait_run_finished(tid)

    hist = client.get(
        f"/api/v1/chat/history?thread_id={tid}",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert hist.status_code == 200

    cancel = client.post(
        f"/api/v1/chat/streams/{stream_id}/cancel",
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert cancel.status_code == 200

    # 挂起线程确认（自己）
    ptid = "thread-own-pending"
    _start_pending(client, managers[MGR1], ptid, chat_harness)
    pc = app.state.checkpoint_service.load_thread(ptid)["pending_confirmation"]
    confirm = client.post(
        "/api/v1/chat/confirm",
        json={"thread_id": ptid, "approved": False, "payload_hash": pc["payload_hash"]},
        cookies=managers[MGR1],
        headers=headers(),
    )
    assert confirm.status_code == 200
    _wait_run_finished(ptid)
