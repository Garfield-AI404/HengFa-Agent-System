"""agent 类型 → 工具子集解析层单测（多 agent 演进 · 机制前置）。

覆盖 ``administrative_agent.agent.toolsets`` 的三条刻意选择：

1. ``resolve_tools(None)`` **身份断言**（``is TOOL_REGISTRY``）——「零行为
   变更」的最强表达，不是 ``==`` 比较；
2. 未知名 → ``ValueError``（fail closed，不静默降级为全量）；
3. 子集按 ``TOOL_REGISTRY`` 注册顺序重建（顺序即提示词内容）。

另钉住本轮验收不变式：``OFFICE`` 档 == 注册表全集；非 office 档为
**暂定 · 未接线 · 未经业务签核** 的非空真子集（用例名带 ``provisional``）。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.toolsets import (
    AGENT_TOOLSETS,
    AgentKind,
    resolve_tools,
)


def test_resolve_tools_none_returns_registry_object() -> None:
    """``resolve_tools(None) is TOOL_REGISTRY``——同一对象（零变更的身份证据）。

    下游所有 ``is TOOL_REGISTRY`` 身份断言、顺序、内容全部不动；若改成
    ``dict(TOOL_REGISTRY)`` 或子集重建即红。
    """
    assert resolve_tools(None) is TOOL_REGISTRY


def test_office_toolset_covers_full_registry() -> None:
    """内勤档 == 注册表全集（本轮验收不变式 G3）。"""
    assert AGENT_TOOLSETS[AgentKind.OFFICE] == frozenset(TOOL_REGISTRY)


def test_provisional_toolsets_are_nonempty_strict_subsets() -> None:
    """非 office 档（**暂定 · 未接线 · 未经业务签核**）非空且为真子集。

    只断言集合形状（``0 < len < len(TOOL_REGISTRY)`` 且真子集）——**不**
    断言具体工具名，否则业务签核定稿改清单就要改测试。
    """
    total = len(TOOL_REGISTRY)
    for kind in (AgentKind.SALES, AgentKind.FINANCE):
        names = AGENT_TOOLSETS[kind]
        assert 0 < len(names) < total, (kind, len(names), total)
        assert names < frozenset(TOOL_REGISTRY), kind  # 真子集（严格小于）


def test_subset_preserves_registry_order() -> None:
    """子集按注册顺序重建——顺序即提示词枚举表内容。"""
    subset = resolve_tools("finance")
    expected = [n for n in TOOL_REGISTRY if n in AGENT_TOOLSETS[AgentKind.FINANCE]]
    assert list(subset.keys()) == expected


def test_unknown_kind_raises_value_error() -> None:
    """未知名 → ValueError（fail closed，不静默降级为全量、不返回 {}）。"""
    with pytest.raises(ValueError):
        resolve_tools("nope")
    # 错误信息含中文并列出可选取值
    with pytest.raises(ValueError, match="未知的 agent 类型"):
        resolve_tools("nope")


def test_subset_dict_is_independent_of_registry() -> None:
    """子集 dict 与注册表独立：改返回 dict 不影响 ``TOOL_REGISTRY``。"""
    subset = resolve_tools("finance")
    before = dict(TOOL_REGISTRY)
    subset.pop("finance_create")
    subset["__injected__"] = None  # type: ignore[assignment]
    assert dict(TOOL_REGISTRY) == before


def test_agent_kind_str_enum_deserializes() -> None:
    """``AgentKind(str, Enum)`` 同构 Role：字符串值可直接构造（顺带校验）。"""
    assert AgentKind("office") is AgentKind.OFFICE
    assert AgentKind("sales").value == "sales"
    assert AgentKind("finance") is AgentKind.FINANCE
