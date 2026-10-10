"""13.1-13.4 故障恢复集成测试（D24；16.6 RV-37）。

逐条对应规格 13.1 错误分类 / 13.2 重试边界 / 13.3 超时与并发 /
13.4 降级。深度分支（幂等四态、版本冲突重算、网络未知恢复、映射版本
门禁、锁单元语义）在 tests/contract/*、tests/domains/*、tests/tasks/
test_lock.py 与 tests/audit/test_hooks.py 已覆盖，本套件补齐执行器级
故障证据（13.4 清单缺口）：Agent DB 不可用零写、ERP 不可达任务终止、
只读 query fail-fast 单调用、超时参数收敛、写任务串行锁晋升。

已登记限制（RV-37 缺陷清单，信息级）：
- 13.2-1 只读 query 指数退避：已于 2026-08-27 落地（项目负责人批准，
  连接失败/404/502/503/504 与只读任意 5xx 在传输层有限重试，见
  mcp/client.py::_should_retry），原"后端无实现、D5 fail-fast 单调用"
  占位断言已改为重试后的稳定契约；
- 13.3-5 remaining_steps "低于 2 停止新调用"：graph 循环为占位节点消费
  一步（RV-10 材料 D11+），无完整循环可测；
- 13.4-2 模型不可用保 task/checkpoint：模型层降级规则分类已测
  （tests/intent/test_model_interface.py），task/checkpoint 保留专测缺
  （正常路径自动保存由 tests/checkpoint/test_graph_integration.py 覆盖）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.audit.service import AuditFailureError, AuditService
from administrative_agent.config import Settings
from administrative_agent.domains.attendance import AttendanceExecutor
from administrative_agent.domains.reconciliation import (
    ERROR_EXECUTE_MAPPING_VERSION_CHANGED,
    ReconciliationExecuteError,
)
from administrative_agent.mcp.client import MCPClient, _build_timeout
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.mcp.metadata import EntityCache
from administrative_agent.tasks.machine import TaskStatus
from administrative_agent.tasks.service import TaskService

from tests.audit.conftest import InMemoryAttemptRepository, InMemoryAuditRepository
from tests.tasks.conftest import InMemoryTaskRepository
from tests.integration.conftest import (
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ATTENDANCE_DATE,
    EMPLOYEES,
    ERP_MCP_URL,
    FakeAttendanceErp,
    TRACE_ID,
    create_task,
    order_task,
    scenario1_plan,
)


def build_executor(erp: FakeAttendanceErp, *, attempt_repo=None, client=None):
    """组装执行器（连接/审计可注入故障源；respx 拦截作用域由调用方控制）。"""
    settings = Settings()
    client = client or MCPClient(settings)
    metadata = EntityCache(client, ttl_seconds=3600)
    tasks = TaskService(InMemoryTaskRepository())
    audit = AuditService(
        InMemoryAuditRepository(), attempt_repo or InMemoryAttemptRepository()
    )
    executor = AttendanceExecutor(
        client=client,
        metadata=metadata,
        tasks=tasks,
        audit=audit,
        trace_id=TRACE_ID,
        actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE,
        policy=policy_for("R1.attendance.daily"),
        retry_limit=3,
    )
    return client, tasks, audit, executor


# ---------------------------------------------------------------------------
# 13.1-7 / 13.4-1：ERP 不可达 —— 只解释不写，任务终止不假报成功
# ---------------------------------------------------------------------------


def test_13_1_erp_unreachable_task_fails_no_writes() -> None:
    """ERP 连接失败（13.1-7 网络结果未知前一步：连接即失败）→ 执行器在
    元数据检查即终止：任务 failed + MCPError 抛出 + 零写调用。"""
    erp = FakeAttendanceErp()
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client, tasks, audit, executor = build_executor(erp)
        try:
            task = create_task(tasks)
            with pytest.raises(MCPError) as exc_info:
                executor.execute(
                    task_id=task.task_id,
                    attendance_date=ATTENDANCE_DATE,
                    plan=scenario1_plan(),
                    employees=EMPLOYEES,
                )
            assert exc_info.value.code == "MCP_CONNECT_FAILED"
            assert exc_info.value.category == ErrorCategory.NETWORK
            record = tasks.get_task(task.task_id)
            assert record.status == TaskStatus.FAILED
            assert record.final_error_code == "MCP_CONNECT_FAILED"
            # 13.4-1：零写入（未触达任何写工具）
            assert erp.created_calls == []
            assert erp.updated_calls == []
        finally:
            client.close()


# ---------------------------------------------------------------------------
# 13.4-4：Agent DB 不可用 —— 审计先行，禁止写，不假报成功
# ---------------------------------------------------------------------------


def test_13_4_audit_db_down_stops_before_write() -> None:
    """Agent DB 不可用：record_attempt 先于 ERP 写调用（10.4 顺序 7）→
    AuditFailureError 传播（不假报成功），零写调用；任务保持 running
    （人工复核标记由调用方 mark_manual_review_required 负责，
    tests/audit/test_hooks.py 已测）。"""
    erp = FakeAttendanceErp()
    attempt_repo = InMemoryAttemptRepository()
    attempt_repo.fail_next = True  # 模拟 DB 不可用（审计失败路径）
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        client, tasks, audit, executor = build_executor(erp, attempt_repo=attempt_repo)
        try:
            task = create_task(tasks)
            with pytest.raises(AuditFailureError):
                executor.execute(
                    task_id=task.task_id,
                    attendance_date=ATTENDANCE_DATE,
                    plan=scenario1_plan(),
                    employees=EMPLOYEES,
                )
            # 审计先行：第一条 attempt 即中止，未发出任何写调用
            assert erp.created_calls == []
            assert erp.updated_calls == []
            # 不假报成功：任务未被标记 succeeded
            record = tasks.get_task(task.task_id)
            assert record.status == TaskStatus.RUNNING
            assert record.final_error_code is None
        finally:
            client.close()


# ---------------------------------------------------------------------------
# 13.2-1：只读 query 瞬时故障重试（2026-08-27 已实现，取代 D5 fail-fast 占位）
# ---------------------------------------------------------------------------


def test_13_2_read_query_transient_retry_then_fail(monkeypatch) -> None:
    """只读 query 连接失败：传输层重试至上限后失败，结果仍归类 NETWORK。

    契约变更（2026-08-27 项目负责人批准，见 mcp/client.py::_should_retry
    与模块 docstring）：连接失败表示请求确定未达服务端，读写工具都做有限
    重试（最多 erp_mcp_max_retries 次，指数退避 1/2/4s）。本用例原为
    13.2-1「后端无实现、D5 fail-fast 单调用」的现状占位，现已落地，故
    改为断言重试后的稳定契约：
    - 传输尝试数 = 1 + 上限（respx 同时拦截 initialize，每次尝试 1 个 POST）；
    - 用尽重试后返回最后一次结果，错误归类不变（NETWORK、retryable=False）
      —— 即上层"不得自行重试"的判定基础未变；
    - 退避序列为指数（真实 sleep 已替换，不拖慢测试）。
    """
    from tests.conftest import mcp_max_retries

    delays: list[int] = []
    monkeypatch.setattr("administrative_agent.mcp.client.time.sleep", delays.append)

    settings = Settings()
    client = MCPClient(settings)
    try:
        with respx.mock:
            respx.post(ERP_MCP_URL).mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            result = client.call_tool("query", {"entity": "attendance"})
            assert respx.calls.call_count == 1 + mcp_max_retries()
        # 指数退避 1s/2s/4s……（重试次数 = 上限）
        assert delays == [1 << i for i in range(mcp_max_retries())]
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "MCP_CONNECT_FAILED"
        assert result.error.category == ErrorCategory.NETWORK
        assert result.error.retryable is False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 13.3-1：超时参数收敛（_build_timeout 直接断言，契约层缺口）
# ---------------------------------------------------------------------------


def test_13_3_timeout_convergence_hard_bound(monkeypatch) -> None:
    """连接/读取/总超时收敛为 httpx 四组件（13.3-1）：write==read、
    pool==connect，且最坏墙钟 2×connect+2×read 不超过总超时硬上界。"""
    monkeypatch.setenv("ERP_MCP_URL", "http://erp-mcp.test/mcp")
    monkeypatch.setenv("ERP_MCP_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("ERP_MCP_READ_TIMEOUT", "30")
    monkeypatch.setenv("ERP_MCP_TOTAL_TIMEOUT", "60")
    settings = Settings()
    timeout = _build_timeout(settings)
    assert timeout.connect == 5.0
    assert timeout.read == timeout.write
    assert timeout.pool == timeout.connect
    # 总超时硬上界：最坏墙钟时长不超配置（连接×2 + 读取×2）
    assert 2 * timeout.connect + 2 * timeout.read <= 60.0
    # 收敛：read 被压缩到 (total - 2×connect)/2 = 25
    assert timeout.read == 25.0


def test_13_3_timeout_convergence_tiny_total_floor(monkeypatch) -> None:
    """总超时极小值：连接/读取压缩到总超时的下限（0.1s 地板生效），
    不出现负超时或超出总超时的非法组合。"""
    monkeypatch.setenv("ERP_MCP_URL", "http://erp-mcp.test/mcp")
    monkeypatch.setenv("ERP_MCP_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("ERP_MCP_READ_TIMEOUT", "30")
    monkeypatch.setenv("ERP_MCP_TOTAL_TIMEOUT", "0.05")  # 低于地板
    settings = Settings()
    timeout = _build_timeout(settings)
    assert timeout.connect == 0.025  # total/4（total 取 0.1 地板）
    assert timeout.read == 0.025  # (0.1 - 2×0.025)/2
    assert timeout.read == timeout.write
    assert 2 * timeout.connect + 2 * timeout.read <= 0.1 + 1e-9


# ---------------------------------------------------------------------------
# 13.3-2：manager 写任务串行锁 —— 排队不并行，前序终态后自动晋升
# ---------------------------------------------------------------------------


def test_13_3_write_tasks_serialized_second_promoted_after_terminal(ctx) -> None:
    """并发写任务串行：第二个写任务排队（pending）不进入 running；
    第一个任务执行终态后队头自动晋升（D6 串行锁 + FIFO）。"""
    erp, tasks, audit, executor, _ = ctx
    first = create_task(tasks)
    second = create_task(tasks)
    assert tasks.get_task(first.task_id).status == TaskStatus.RUNNING
    assert tasks.get_task(second.task_id).status == TaskStatus.PENDING  # 排队

    summary = executor.execute(
        task_id=first.task_id,
        attendance_date=ATTENDANCE_DATE,
        plan=scenario1_plan(),
        employees=EMPLOYEES,
    )
    assert summary.verification_passed is True
    assert tasks.get_task(first.task_id).status == TaskStatus.SUCCEEDED
    # 队头晋升：第二个任务自动取得锁进入 running
    assert tasks.get_task(second.task_id).status == TaskStatus.RUNNING
    # 串行保证：第一个执行期间第二个从未并行写（ERP 侧仅第一任务 5 条）
    assert len(erp.created_calls) == 5


# ---------------------------------------------------------------------------
# 13.4-3：映射版本不可用 → R6 拒绝执行零写入（执行器级证据；
# 专项断言 tests/domains/test_reconciliation_execute.py::test_gate_rejects_
# mapping_version_changed L462）
# ---------------------------------------------------------------------------


def test_13_4_mapping_version_unavailable_r6_evidence(rctx14) -> None:
    """R6 执行前置门禁：复核后映射已发布 v2（计划基于 v1）→ 拒绝执行，
    ReconciliationExecuteError + 任务 failed + 零 ERP 调用。"""
    from tests.domains.test_reconciliation_execute import (
        create_diff_entry,
        create_item,
        make_report,
        make_write_plan,
    )

    erp, tasks, audit, reviews, executor, mappings = rctx14
    # 执行时映射已发布 v2：v1 retire 后发布 v2
    published = mappings.get_published("recon-metal")
    mappings.retire(actor="manager01", version_id=published.version_id)
    result = mappings.import_draft(
        actor="manager01", template_key="recon-metal", sheet_name="对账单",
        header_row=1, source_file_hash="sha256-mapping-v2",
        columns=[
            {"excel_column": "型号", "target_field": "model", "data_type": "string",
             "is_required": True},
            {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
             "is_required": True},
        ],
    )
    mappings.publish(actor="manager01", version_id=result.version.version_id)
    task = order_task(
        tasks, duty="R6",
        structured_intent={"duty": "R6", "operation": "reconcile_execute"},
    )
    plan = make_write_plan(task.task_id, [create_item()])  # 计划基于 v1
    review = reviews.request_review(
        task_id=plan.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    review = reviews.approve(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,
    )
    calls_before = len(erp.calls)
    with pytest.raises(ReconciliationExecuteError) as exc:
        executor.execute(
            task_id=task.task_id, plan=plan, review=review,
            report=make_report(create_diff_entry()),
        )
    assert exc.value.error_code == ERROR_EXECUTE_MAPPING_VERSION_CHANGED
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert erp.calls[calls_before:] == []  # 门禁在任何查询/写入之前拒绝
