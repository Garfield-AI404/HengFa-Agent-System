"""审计落库失败的任务级标记（10.4 末段 / L1179 追查链 / 11.5 指标）。

``AuditFailureError`` 的契约写明「ERP 已成功而本地审计失败时，调用方必须
标记任务需人工复核并保留 operation_id」。写工具执行的两条路径此前只把异常
转成 ``tool_failed`` 的 ToolMessage，任务行停留在 running，该契约未落地。

本文件覆盖新增的 ``_mark_audit_write_failed`` 与接入它的两条路径：

- ``tools_node._run_without_confirmation``（[2026-08-28 考勤免确认] 直接执行）
- ``confirm_node``（确认后执行）

**核心守卫**：标记**只**在 ``AuditFailureError`` 时新增。其余异常必须维持
原行为（不标记任务），否则就等于改动了既有失败语义——这是本文件最重要的
两条反向断言（test_non_audit_error_is_not_marked /
test_run_path_leaves_task_unmarked_for_other_errors）。
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from administrative_agent.agent.configuration import (
    ERROR_CODE_AUDIT_WRITE_FAILED,
    RuntimeBag,
    _mark_audit_write_failed,
)
from administrative_agent.agent.tools.base import PreparedToolWrite, ToolSpec
from administrative_agent.audit.service import AuditFailureError
from administrative_agent.tasks.machine import TaskStatus

# 坑：`agent/nodes/__init__.py` re-export 了与子模块**同名**的节点函数
# （tools_node / confirm_node）。因此 `from ...nodes import tools_node` 和
# `import ...nodes.tools_node as m` 拿到的都是**函数**而不是模块——父包的
# 同名属性被 re-export 覆盖了。要拿到模块本身必须走 importlib。
tools_node_mod = importlib.import_module("administrative_agent.agent.nodes.tools_node")
confirm_node_mod = importlib.import_module(
    "administrative_agent.agent.nodes.confirm_node"
)

TASK_ID = "task-audit-1"
TOOL_NAME = "order_create"
PAYLOAD_HASH = "hash-abc123"
OP_ID = "op-L1179-xyz"


class _EmptyArgs(BaseModel):
    """args_schema 占位（本测试不经过参数校验）。"""


class FakeTasks:
    """TaskService 替身：记录 mark_failed / confirm 调用。"""

    def __init__(self, status: TaskStatus = TaskStatus.WAITING_CONFIRMATION) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._status = status

    def mark_failed(self, task_id: str, error_code: str, error_summary: str) -> None:
        self.calls.append(("mark_failed", task_id, error_code, error_summary))

    def confirm(self, task_id: str, approved: bool, thread_id: str | None = None) -> None:
        self.calls.append(("confirm", task_id, approved))

    def get_task(self, task_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(task_id=task_id, status=self._status)

    @property
    def marked(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "mark_failed"]


def _spec_raising(exc: BaseException) -> ToolSpec:
    """构造 run 时抛出指定异常的写工具。"""

    def run(
        ctx: Any,
        args: Any = None,
        task_id: str | None = None,
        execution: dict | None = None,
    ) -> Any:
        raise exc

    return ToolSpec(
        name=TOOL_NAME,
        description="测试替身",
        args_schema=_EmptyArgs,
        required_scopes=frozenset(),
        requires_manager=True,
        is_write=True,
        duty_code="R1",
        policy_key_of=lambda args: "order_create",
        run=run,
        prepare=None,
    )


def _prepared() -> PreparedToolWrite:
    return PreparedToolWrite(
        card={"tool": TOOL_NAME},
        payload_hash=PAYLOAD_HASH,
        execution={"draft": {}},
        policy_key="order_create",
    )


# ---------------------------------------------------------------------------
# Layer 1：_mark_audit_write_failed 自身
# ---------------------------------------------------------------------------


def test_mark_audit_write_failed_with_operation_id() -> None:
    """审计失败 → 标记 AUDIT_WRITE_FAILED，摘要保留 operation_id（L1179）。"""
    tasks = FakeTasks()
    runtime = RuntimeBag(tasks=tasks)
    error = AuditFailureError(OP_ID, "审计事件写入失败：db down")

    assert _mark_audit_write_failed(runtime, TASK_ID, error) is True

    assert len(tasks.marked) == 1
    _, task_id, error_code, summary = tasks.marked[0]
    assert task_id == TASK_ID
    assert error_code == ERROR_CODE_AUDIT_WRITE_FAILED == "AUDIT_WRITE_FAILED"
    # 摘要必须保留 operation_id 供追查，这是契约的硬要求
    assert OP_ID in summary
    assert "审计落库失败" in summary


def test_mark_audit_write_failed_without_operation_id() -> None:
    """operation_id 缺失（如 attempt 不存在）不阻断标记。"""
    tasks = FakeTasks()
    runtime = RuntimeBag(tasks=tasks)

    assert _mark_audit_write_failed(runtime, TASK_ID, AuditFailureError(None, "attempt 不存在")) is True

    assert len(tasks.marked) == 1
    assert tasks.marked[0][2] == ERROR_CODE_AUDIT_WRITE_FAILED
    # 无 operation_id 时不追加空的追查段
    assert "operation_id" not in tasks.marked[0][3]


def test_non_audit_error_is_not_marked() -> None:
    """非审计异常一律不标记——不能改变既有失败语义（核心守卫）。"""
    tasks = FakeTasks()
    runtime = RuntimeBag(tasks=tasks)

    assert _mark_audit_write_failed(runtime, TASK_ID, RuntimeError("ERP 超时")) is False
    assert tasks.marked == []


def test_mark_failure_does_not_propagate() -> None:
    """标记自身失败（状态已变 / tasks 未接线）不得掩盖原始异常。"""
    runtime = RuntimeBag(tasks=None)  # 未接线：属性访问会抛 AttributeError

    # 不得抛出
    assert (
        _mark_audit_write_failed(runtime, TASK_ID, AuditFailureError(OP_ID, "db down"))
        is True
    )


# ---------------------------------------------------------------------------
# Layer 2：两条写工具执行路径
# ---------------------------------------------------------------------------


def test_run_path_marks_and_keeps_tool_failed() -> None:
    """免确认路径：审计失败 → 任务标记 + 返回结构完全不变。"""
    tasks = FakeTasks()
    runtime = RuntimeBag(tasks=tasks)
    spec = _spec_raising(AuditFailureError(OP_ID, "审计事件写入失败：db down"))

    out = tools_node_mod._run_without_confirmation(
        state={},
        runtime=runtime,
        spec=spec,
        tool_name=TOOL_NAME,
        task_id=TASK_ID,
        prepared=_prepared(),
    )

    # 新增：任务行落到 failed + AUDIT_WRITE_FAILED（此前停留在 running）
    assert len(tasks.marked) == 1
    assert tasks.marked[0][2] == ERROR_CODE_AUDIT_WRITE_FAILED
    assert OP_ID in tasks.marked[0][3]

    # 不变：返回结构与原实现逐字段一致
    assert out["current_step"] == "tool_failed"
    assert out["pending_action"] is None
    assert out["pending_confirmation"] is None
    content = out["messages"][0].content
    assert "工具执行失败" in content


def test_run_path_leaves_task_unmarked_for_other_errors() -> None:
    """免确认路径：普通异常仍不标记任务（核心守卫，防回归）。"""
    tasks = FakeTasks()
    runtime = RuntimeBag(tasks=tasks)
    spec = _spec_raising(RuntimeError("ERP 连接超时"))

    out = tools_node_mod._run_without_confirmation(
        state={},
        runtime=runtime,
        spec=spec,
        tool_name=TOOL_NAME,
        task_id=TASK_ID,
        prepared=_prepared(),
    )

    assert tasks.marked == []  # 关键：不新增标记
    assert out["current_step"] == "tool_failed"  # 行为不变


def test_confirm_path_marks_and_keeps_tool_failed() -> None:
    """确认后执行路径：审计失败 → 任务标记 + 返回结构完全不变。"""
    tasks = FakeTasks(status=TaskStatus.WAITING_CONFIRMATION)
    runtime = RuntimeBag(tasks=tasks, tools={TOOL_NAME: _spec_raising(
        AuditFailureError(OP_ID, "审计事件写入失败：db down")
    )})
    state = {
        "pending_confirmation": {
            "task_id": TASK_ID,
            "tool_name": TOOL_NAME,
            "card": {"tool": TOOL_NAME},
            "payload_hash": PAYLOAD_HASH,
            "execution": {"draft": {}},
            "policy_key": "order_create",
            "thread_id": "thread-1",
        }
    }

    # interrupt 是 LangGraph 机制：patch 成「已批准且 hash 一致」
    with patch.object(
        confirm_node_mod,
        "interrupt",
        return_value={"approved": True, "payload_hash": PAYLOAD_HASH},
    ):
        out = confirm_node_mod.confirm_node(state, runtime)

    # 新增：任务行标记
    assert len(tasks.marked) == 1
    assert tasks.marked[0][2] == ERROR_CODE_AUDIT_WRITE_FAILED
    assert OP_ID in tasks.marked[0][3]

    # 不变：返回结构与原实现一致
    assert out["current_step"] == "tool_failed"
    assert out["pending_action"] is None
    assert out["pending_confirmation"] is None
    assert "工具执行失败" in out["messages"][0].content


def test_confirm_path_leaves_task_unmarked_for_other_errors() -> None:
    """确认后执行路径：普通异常仍不标记任务（核心守卫）。"""
    tasks = FakeTasks(status=TaskStatus.WAITING_CONFIRMATION)
    runtime = RuntimeBag(tasks=tasks, tools={TOOL_NAME: _spec_raising(
        RuntimeError("ERP 连接超时")
    )})
    state = {
        "pending_confirmation": {
            "task_id": TASK_ID,
            "tool_name": TOOL_NAME,
            "card": {"tool": TOOL_NAME},
            "payload_hash": PAYLOAD_HASH,
            "execution": {"draft": {}},
            "policy_key": "order_create",
            "thread_id": "thread-1",
        }
    }

    with patch.object(
        confirm_node_mod,
        "interrupt",
        return_value={"approved": True, "payload_hash": PAYLOAD_HASH},
    ):
        out = confirm_node_mod.confirm_node(state, runtime)

    assert tasks.marked == []
    assert out["current_step"] == "tool_failed"


# ---------------------------------------------------------------------------
# Layer 3：指标口径
# ---------------------------------------------------------------------------


def test_error_code_is_stable_aggregation_key() -> None:
    """11.5 指标按 final_error_code 聚合：常量值必须稳定。"""
    assert ERROR_CODE_AUDIT_WRITE_FAILED == "AUDIT_WRITE_FAILED"
