"""考勤写工具（R1 agent 工具层）参数校验与确认卡开关测试。

与 tests/unit/test_attendance_rules.py 分层：后者测 business/attendance_rules
纯业务规则（计划生成），本文件测 agent/tools 层——入参侧 fail closed 与
注册表确认开关。

两条防线：
1. ``attendance_date`` 必须是 YYYY-MM-DD 且日期真实存在（2026-08-30 修复
   "str 字段无格式校验、坏日期直接透传 ERP"缺陷）。校验点在 Args 层：
   ``tools_node`` 第 254-267 行 ``spec.args_schema.model_validate`` 失败即
   转错误 ToolMessage（current_step=tool_invalid_args），不进 prepare、不触
   ERP。
2. ``TOOL_REGISTRY["attendance_plan_execute"].require_confirmation is True``
   （2026-08-30 撤回 2026-08-28 的"考勤免确认"决策——"免确认 × plan_attendance
   全员覆盖"会一次写出全员不可删除考勤）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.attendance import AttendancePlanExecuteArgs

EMPLOYEE = ["张三"]

#: 形状非 ISO：正则拦截
BAD_SHAPE = [
    "2026/08/30",      # 斜杠分隔（实测透传 ERP 的坏日期）
    "",                # 空串
    "2026-8-30",       # 单位数月/日（严格拦）
    "20260830",        # 紧凑格式：Python 3.11 fromisoformat 接受，正则必须拦
    "2026-08-30T00:00:00",  # 带时间
    "2026-08-30 ",     # 尾随空格
    "今天",
]

#: 形状合法但日期非法：fromisoformat 拦截（缺了它 2026-13-40 会穿透正则）
BAD_DATE_VALUE = [
    "2026-13-40",
    "2026-02-30",
    "2026-00-10",
    "2026-08-32",
]


@pytest.mark.parametrize("bad", BAD_SHAPE)
def test_attendance_date_rejects_non_iso_shape(bad: str) -> None:
    """非 YYYY-MM-DD 形状一律拒绝（含 fromisoformat 也吃的紧凑格式）。"""
    with pytest.raises(ValidationError) as exc:
        AttendancePlanExecuteArgs(attendance_date=bad, employees=EMPLOYEE)
    assert "attendance_date" in str(exc.value)


@pytest.mark.parametrize("bad", BAD_DATE_VALUE)
def test_attendance_date_rejects_impossible_date(bad: str) -> None:
    """形状合法但日期不存在（如 2026-13-40）→ 第二道校验拦截。"""
    with pytest.raises(ValidationError):
        AttendancePlanExecuteArgs(attendance_date=bad, employees=EMPLOYEE)


def test_attendance_date_accepts_iso_date() -> None:
    """合法 ISO 日期原值通过（不改写、不归一化）。"""
    args = AttendancePlanExecuteArgs(
        attendance_date="2026-08-30", employees=EMPLOYEE
    )
    assert args.attendance_date == "2026-08-30"


def test_attendance_plan_execute_requires_confirmation() -> None:
    """防回归：考勤必须走确认卡（2026-08-30 撤回免确认决策）。

    require_confirmation=False 曾让"免确认 × 全员覆盖"叠加成一次执行写出
    13 条不可删除考勤。钉住 True，等于钉住 tools_node 第 7 步
    ``if spec.require_confirmation`` 确认分支对考勤生效。
    """
    spec = TOOL_REGISTRY["attendance_plan_execute"]
    assert spec.require_confirmation is True


def test_all_write_tools_require_confirmation() -> None:
    """防回归：全部写工具均需确认卡（当前注册表无免确认工具）。"""
    write_specs = [s for s in TOOL_REGISTRY.values() if s.is_write]
    assert write_specs, "注册表至少应有 R1/R2/R3/R6 四个写工具"
    assert all(s.require_confirmation for s in write_specs)
