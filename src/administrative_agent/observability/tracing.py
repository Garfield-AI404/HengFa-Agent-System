"""operation_id/trace_id 生成（规格 2.6 observability/tracing.py）。

- ``new_operation_id`` / ``new_trace_id``：每次调用/任务的稳定标识
  （7.4/L1179：Agent 业务审计与 ERP 技术日志通过 operation_id、trace_id
  关联；10.4 顺序 7/11 落审计）。
- ``stable_operation_id``：4.4 要求的多条 CRUD 子操作稳定 operation_id
  （如 ``{task_id}:attendance:{date}:{employee_id}``），用于 attempt 关联
  与恢复防重。

D5 起生成器位于 mcp/client.py（前缀与语义相同）；本模块为 2.6 代码树
权威位置，mcp/client.py 改为从本模块导入并保留 re-export（既有调用方
`from administrative_agent.mcp.client import new_operation_id` 不受影响）。
"""

from __future__ import annotations

import uuid


def new_operation_id() -> str:
    """新 operation_id（op- 前缀 + 16 位 hex，8.2/L1179 追查链）。"""
    return f"op-{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    """新 trace_id（trace- 前缀 + 16 位 hex）。"""
    return f"trace-{uuid.uuid4().hex[:16]}"


def stable_operation_id(task_id: str, *segments: str) -> str:
    """4.4 稳定子操作标识：``{task_id}:{segment1}:{segment2}:…``。

    与 4.4 示例格式一致（``{task_id}:attendance:{date}:{employee_id}``）；
    段内不得含冒号，由调用方保证（业务字段校验属 D11+）。
    稳定标识用于：attempt/result 关联、中断恢复跳过已完成项、防重复创建
    （4.4：真正防重仍依赖写前查询、唯一约束和写后验证，本标识只用于关联）。
    """
    if not task_id:
        raise ValueError("task_id is required for stable_operation_id")
    return ":".join((task_id, *segments))
