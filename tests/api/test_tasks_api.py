"""D22 tasks 端点 API 测试（规格 2.7.3 L507-511；8.1/8.2/8.3；7.3）。

覆盖（D22 任务书测试要求）：
- GET /api/v1/tasks：分页（items/total/limit/offset）、status 过滤、
  非法 status 422、8.2 字段口径；
- GET /api/v1/tasks/:taskId：详情（task + 步骤审计事件 + attempt/result）、
  404 TASK_NOT_FOUND；
- POST replies：waiting_user_input 成功 → running；非等待状态 409
  TASK_INVALID_STATE；回答数量不符 400 TASK_ANSWERS_MISMATCH；404；
- POST confirm：approved=True → running；False → cancelled；payload_hash
  不一致 409 CONFIRM_HASH_MISMATCH（7.3 L886 原确认失效）；非等待状态
  409；
- POST cancel：waiting/pending 成功 → cancelled；终态 409 TASK_INVALID_STATE；
  404；
- 安全：全部端点 manager（401/403）、写 API CSRF（缺 Origin 403）、
  统一错误响应结构（error_code/message/details/trace_id）。
"""

from __future__ import annotations

from administrative_agent.auth.models import Role

from tests.api.conftest import (
    DEVELOPER,
    MGR,
    headers,
    cookies,
)

from administrative_agent.tasks.records import NewTaskSpec, TaskRecord

THREAD = "thread-tasks-test"

#: 歧义订单的追问（8.2 pending_questions 口径）
ORDER_TYPE_QUESTION = "请确认订单类型：钣金还是塑粉？"


def _tasks() -> TaskServiceForTest:
    """造数入口：绕过 chat 端点，直接经 TaskService 构造任务。

    D26 起 POST /chat/messages 改为异步（图在后台线程 drain，响应 task_id
    恒为 None），Tasks API 的用例不应再经 chat 端点造数——那会让本文件的
    用例依赖 LLM 图执行，并引入 THREAD_BUSY 竞态。

    本文件的被测对象是 Tasks API 的查询、状态迁移与序列化，任务状态由
    夹具显式给定，不再由执行结果间接产生。
    """
    from administrative_agent.main import app

    return TaskServiceForTest(app)


def _make_task(**overrides) -> TaskRecord:
    """造一个任务（默认只读 pending；is_write=True 时取串行锁转 running）。"""
    spec = dict(
        thread_id=THREAD,
        actor_account="manager01",
        actor_role="manager",
        duty_code="R1",
        original_user_message="记录今天的考勤",
        structured_intent={"duty": "R1"},
        is_write=False,
    )
    spec.update(overrides)
    return _tasks().create_task(NewTaskSpec(**spec))


def _waiting_task(
    *, thread: str = THREAD, questions: list[str] | None = None, **overrides
) -> TaskRecord:
    """造一个 waiting_user_input 任务（写任务取锁 running → need_input）。"""
    spec = dict(
        thread_id=thread,
        duty_code="R2",
        original_user_message="下个订单",
        structured_intent={"duty": "R2", "operation": "create_order"},
        is_write=True,
    )
    spec.update(overrides)
    task = _make_task(**spec)
    return _tasks().need_input(task.task_id, questions or [ORDER_TYPE_QUESTION])


def _failed_task(
    *,
    thread: str = THREAD,
    error_code: str = "MODEL_PIPELINE_NOT_CONNECTED",
    **overrides,
) -> TaskRecord:
    """造一个 failed 终态任务。

    状态机要求 failed 必须经 running（machine.py:76），故先迁移到 running
    再置 failed。默认只读任务不持串行锁，同线程可造多个而互不排队。
    completed_at 与锁释放由 transition 对终态统一处理。
    """
    spec = dict(
        thread_id=thread,
        duty_code="R1",
        original_user_message="记录今天的考勤",
        structured_intent={"duty": "R1"},
        is_write=False,
    )
    spec.update(overrides)
    service = _tasks()
    task = _make_task(**spec)
    service.transition(task.task_id, "running")
    return service.transition(task.task_id, "failed", final_error_code=error_code)


def _replies(client, task_id: str, answers: list[str]) -> object:
    return client.post(
        f"/api/v1/tasks/{task_id}/replies",
        json={"answers": answers},
        cookies=MGR,
        headers=headers(),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/tasks：分页与过滤
# ---------------------------------------------------------------------------


def test_list_tasks_paginated_and_filtered(client):
    ids = [_failed_task().task_id for _ in range(3)]
    waiting = _waiting_task()

    resp = client.get(
        "/api/v1/tasks?limit=2&offset=0", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    # created_at 倒序：最新两个（最后一个考勤任务 + 等待输入任务）
    assert {i["task_id"] for i in body["items"]} == {ids[-1], waiting.task_id}

    resp = client.get(
        "/api/v1/tasks?status=waiting_user_input", cookies=MGR, headers=headers()
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "waiting_user_input"

    # 8.2 字段口径（列表项与详情同构）
    item = body["items"][0]
    assert item["pending_questions"] == [ORDER_TYPE_QUESTION]
    assert item["actor_account"] == "manager01"


def test_list_tasks_requires_manager_and_validation(client):
    assert client.get("/api/v1/tasks", headers=headers()).status_code == 401
    assert client.get(
        "/api/v1/tasks",
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403
    resp = client.get(
        "/api/v1/tasks?status=not_a_status", cookies=MGR, headers=headers()
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_list_tasks_returns_failed_tasks_with_error_fields(client):
    _failed_task()
    resp = client.get("/api/v1/tasks", cookies=MGR, headers=headers())
    item = resp.json()["items"][0]
    assert item["status"] == "failed"
    assert item["final_error_code"] == "MODEL_PIPELINE_NOT_CONNECTED"
    assert item["completed_at"] is not None


# ---------------------------------------------------------------------------
# GET /api/v1/tasks/:taskId：详情（步骤/attempt/result）
# ---------------------------------------------------------------------------


def test_task_detail_with_steps_attempts_and_result(client):
    """详情组装：task + 审计事件（步骤）+ attempt/result（11.2）。"""
    task = _failed_task()
    task_id = task.task_id

    # 直接经审计服务补写步骤与 attempt/result（chat 编排的审计记录属
    # 执行器单元；本端点只读组装）
    from administrative_agent.audit.service import AuditService
    from administrative_agent.main import app
    from administrative_agent.persistence.repositories import (
        SqlAlchemyAttemptRepository,
        SqlAlchemyAuditRepository,
    )

    audit = AuditService(
        SqlAlchemyAuditRepository(app.state.session_factory),
        SqlAlchemyAttemptRepository(app.state.session_factory),
    )
    audit.record_event(
        event_type="task_execution",
        trace_id="trace-tasks-api-1",
        actor_account="manager01",
        actor_role="manager",
        task_id=task_id,
        operation_id=task.operation_id,
        request_summary="execution started",
        structured_payload={"step": "execute"},
    )

    resp = client.get(f"/api/v1/tasks/{task_id}", cookies=MGR, headers=headers())
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["task"]["task_id"] == task_id
    assert detail["task"]["status"] == "failed"
    assert [e["event_type"] for e in detail["events"]] == ["task_execution"]
    assert detail["events"][0]["task_id"] == task_id
    # attempt/result 为空（造数任务未产生工具调用）——字段存在
    assert detail["attempts"] == []


def test_task_detail_not_found(client):
    resp = client.get("/api/v1/tasks/task-unknown", cookies=MGR, headers=headers())
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "TASK_NOT_FOUND"
    assert set(body) == {"error_code", "message", "details", "trace_id"}


# ---------------------------------------------------------------------------
# POST replies（8.3 第 2 条：显式绑定 task_id）
# ---------------------------------------------------------------------------


def test_replies_answer_and_resume(client):
    task = _waiting_task()
    resp = _replies(client, task.task_id, ["钣金"])
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "running"
    assert out["pending_questions"] is None


def test_replies_reject_non_waiting_task(client):
    """非 waiting_user_input 任务：409 TASK_INVALID_STATE（不猜测挂起任务）。"""
    task = _failed_task()  # failed 终态
    resp = _replies(client, task.task_id, ["是"])
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "TASK_INVALID_STATE"


def test_replies_answers_mismatch(client):
    task = _waiting_task()
    resp = _replies(client, task.task_id, ["钣金", "多余的答案"])
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "TASK_ANSWERS_MISMATCH"


def test_replies_not_found_and_csrf(client):
    assert _replies(client, "task-unknown", ["是"]).status_code == 404
    resp = client.post(
        "/api/v1/tasks/task-x/replies",
        json={"answers": ["是"]},
        cookies=MGR,
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# POST confirm（7.3 轻确认：confirmed_payload_hash 机制）
# ---------------------------------------------------------------------------


def test_confirm_approve_and_reject(client):
    """等待确认任务 → approved=True running / False cancelled。"""
    from administrative_agent.main import app
    from administrative_agent.tasks.records import NewTaskSpec

    tasks = TaskServiceForTest(app)
    task = tasks.request_confirmation(
        tasks.create_task(
            NewTaskSpec(
                thread_id=THREAD,
                actor_account="manager01",
                actor_role="manager",
                duty_code="R3",
                original_user_message="修改订单 SO-1001 的重量",
                structured_intent={"duty": "R3", "operation": "update_order"},
                is_write=True,
            )
        ).task_id,
        payload_hash="a" * 32,
    )
    assert task.status.value == "waiting_confirmation"

    # 一致 hash 批准 → running
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/confirm",
        json={"approved": True, "payload_hash": "a" * 32},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    # 释放串行锁（8.3：只有终态释放；running 任务取消 → cancelled）
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/cancel",
        cookies=MGR,
        headers=headers(),
    )
    assert resp.json()["status"] == "cancelled"

    # 第二个任务：拒绝 → cancelled
    task2 = tasks.request_confirmation(
        tasks.create_task(
            NewTaskSpec(
                thread_id=THREAD,
                actor_account="manager01",
                actor_role="manager",
                duty_code="R3",
                original_user_message="修改订单 SO-1002 的重量",
                structured_intent={"duty": "R3", "operation": "update_order"},
                is_write=True,
            )
        ).task_id,
        payload_hash="b" * 32,
    )
    resp = client.post(
        f"/api/v1/tasks/{task2.task_id}/confirm",
        json={"approved": False},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_confirm_hash_mismatch(client):
    """7.3 L886：内容变化 → 原确认失效，必须重新确认（409）。"""
    from administrative_agent.main import app
    from administrative_agent.tasks.records import NewTaskSpec

    tasks = TaskServiceForTest(app)
    task = tasks.request_confirmation(
        tasks.create_task(
            NewTaskSpec(
                thread_id=THREAD,
                actor_account="manager01",
                actor_role="manager",
                duty_code="R3",
                original_user_message="修改订单 SO-1003 的重量",
                structured_intent={"duty": "R3", "operation": "update_order"},
                is_write=True,
            )
        ).task_id,
        payload_hash="c" * 32,
    )
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/confirm",
        json={"approved": True, "payload_hash": "d" * 32},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONFIRM_HASH_MISMATCH"


def test_confirm_requires_waiting_state(client):
    """非 waiting_confirmation 任务 → 409 TASK_INVALID_STATE。"""
    task = _failed_task()  # failed 终态
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/confirm",
        json={"approved": True},
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "TASK_INVALID_STATE"


# ---------------------------------------------------------------------------
# POST cancel（8.1：取消尚未执行的任务）
# ---------------------------------------------------------------------------


def test_cancel_waiting_and_queued_task(client):
    task = _waiting_task(thread="thread-cancel")
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/cancel",
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # 排队写任务可取消（pending → cancelled；8.3 队列让位）
    # 同线程内锁被等待中的任务持有，后提交的写任务进入 FIFO 队列
    holder = _waiting_task(thread="thread-cancel-2")
    queued = _make_task(
        thread_id="thread-cancel-2",
        duty_code="R2",
        original_user_message="下一张钣金订单",
        structured_intent={"duty": "R2", "operation": "create_order"},
        is_write=True,
    )
    resp = client.post(
        f"/api/v1/tasks/{queued.task_id}/cancel",
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    # 队列中的取消不影响持有者
    assert (
        client.get(
            f"/api/v1/tasks/{holder.task_id}", cookies=MGR, headers=headers()
        ).json()["task"]["status"]
        == "waiting_user_input"
    )


def test_cancel_terminal_task_conflict(client):
    task = _failed_task()  # failed 终态
    resp = client.post(
        f"/api/v1/tasks/{task.task_id}/cancel",
        cookies=MGR,
        headers=headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "TASK_INVALID_STATE"


def test_cancel_not_found_and_csrf(client):
    resp = client.post("/api/v1/tasks/task-unknown/cancel", cookies=MGR, headers=headers())
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TASK_NOT_FOUND"
    resp = client.post("/api/v1/tasks/task-x/cancel", cookies=MGR)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_VALIDATION_FAILED"


def test_tasks_endpoints_require_manager(client):
    assert client.post(
        "/api/v1/tasks/task-x/replies",
        json={"answers": ["是"]},
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403
    assert client.post(
        "/api/v1/tasks/task-x/confirm",
        json={"approved": True},
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403
    assert client.post(
        "/api/v1/tasks/task-x/cancel",
        cookies=cookies(Role.DEVELOPER, DEVELOPER),
        headers=headers(),
    ).status_code == 403


# ---------------------------------------------------------------------------
# 测试辅助：复用 chat 路由的共享锁构建 TaskService
# ---------------------------------------------------------------------------


class TaskServiceForTest:
    """直接构造 TaskService（共享 app.state 注入的串行锁，与 API 侧一致）。"""

    def __init__(self, app) -> None:
        from administrative_agent.persistence.repositories import (
            SqlAlchemyTaskRepository,
        )
        from administrative_agent.tasks.service import TaskService

        self._tasks = TaskService(
            SqlAlchemyTaskRepository(app.state.session_factory),
            app.state.write_lock,
        )

    def create_task(self, spec):
        return self._tasks.create_task(spec)

    def request_confirmation(self, task_id, payload_hash):
        return self._tasks.request_confirmation(task_id, payload_hash)

    def need_input(self, task_id, questions):
        """running → waiting_user_input（供 replies/cancel 用例造前置状态）。"""
        return self._tasks.need_input(task_id, questions)

    def transition(self, task_id, to_status, **fields):
        """通用状态迁移（终态会自动补 completed_at 并释放写锁）。"""
        return self._tasks.transition(task_id, to_status, **fields)
