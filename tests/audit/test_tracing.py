"""operation_id/trace_id 生成测试（D8/RV-11：7.4/L1179 关联键、4.4 稳定 id）。"""

from __future__ import annotations

import re

from administrative_agent.observability.tracing import (
    new_operation_id,
    new_trace_id,
    stable_operation_id,
)


def test_new_operation_id_format() -> None:
    op = new_operation_id()
    assert re.fullmatch(r"op-[0-9a-f]{16}", op), op


def test_new_trace_id_format() -> None:
    trace = new_trace_id()
    assert re.fullmatch(r"trace-[0-9a-f]{16}", trace), trace


def test_ids_unique() -> None:
    ops = {new_operation_id() for _ in range(50)}
    traces = {new_trace_id() for _ in range(50)}
    assert len(ops) == 50
    assert len(traces) == 50


def test_stable_operation_id_4_4_format() -> None:
    op = stable_operation_id("task-1", "attendance", "2026-08-22", "emp-001")
    assert op == "task-1:attendance:2026-08-22:emp-001"


def test_stable_operation_id_stability() -> None:
    a = stable_operation_id("t", "a", "b")
    b = stable_operation_id("t", "a", "b")
    assert a == b


def test_stable_operation_id_requires_task_id() -> None:
    import pytest

    with pytest.raises(ValueError):
        stable_operation_id("", "attendance")
