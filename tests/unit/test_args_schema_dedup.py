"""[2026-09-16 参数轮去重] args_schema docstring 重复注入消除守护。

背景：模型可见的参数轮提示词里，``args_schema`` 类 docstring 曾被注入两份——
Pydantic v2 会把类 docstring 自动复制进 ``model_json_schema()`` 的**顶层**
``description`` 键，而 ``build_args_schema`` 与 ``tool_args_node`` 的工具卡又
各 append 了一遍 ``__doc__``。同一段口径文字因此在每个参数轮出现多次
（schema 内 1 + append 1）× 两个生成点，还被 ``_with_schema_instruction``
复制进末条 human 消息。

方案 A（用户拍板）：剔除 schema 序列化的**顶层** ``description``，保留代码里
两处 ``__doc__`` append。本文件按本仓「prompt 守护测试假锁」纪律设计，断言
**锚定到具体生成点**而非裸子串：

- 顶层去重：每个工具的 ``_args_schema_json`` 顶层无 ``description`` 键；
- 字段级保留：**只**剔顶层，字段/嵌套级 ``description`` 一律不动（防手滑剔成
  递归删除——那会抹掉模型填充参数的必读说明）；
- append 仍在：两个生成点各自的 ``__doc__`` append 都必须在（分别断言，
  删任一处即红）。
"""

from __future__ import annotations

import json
from typing import Any

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.model_client import (
    ToolArguments,
    _args_schema_json,
    build_args_schema,
)
from administrative_agent.agent.nodes.tool_args_node import tool_args_node
from administrative_agent.agent.tools import TOOL_REGISTRY


class _RecordingClient:
    """记录参数轮调用入参的空 client（不触网，只截获 system_prompt）。"""

    def __init__(self) -> None:
        self.system_prompt: str | None = None

    def generate_tool_arguments(
        self, *, system_prompt: str, messages: list, schema_instruction: str, **_: Any
    ) -> ToolArguments:
        self.system_prompt = system_prompt
        return ToolArguments(tool_name="__stub__", tool_arguments={})


def _captured_prompt(name: str) -> str:
    """真跑一遍 tool_args_node，截获其 system_prompt（含工具卡）。"""
    recorder = _RecordingClient()
    runtime = RuntimeBag(tools=TOOL_REGISTRY, client=recorder)
    tool_args_node(
        {"pending_action": {"tool_name": name}, "messages": []},  # type: ignore[arg-type]
        runtime,
    )
    assert recorder.system_prompt is not None, f"{name}：节点未发起参数生成调用"
    return recorder.system_prompt


# ---------------------------------------------------------------------------
# 断言一：顶层 description 被剔除（变异① 去掉 pop → 红）
# ---------------------------------------------------------------------------


def test_no_top_level_description_for_all_tools() -> None:
    """全部 22 工具：``_args_schema_json`` 解析后顶层**无** ``description`` 键。"""
    assert len(TOOL_REGISTRY) == 22
    for name, spec in TOOL_REGISTRY.items():
        payload = json.loads(_args_schema_json(spec))
        assert "description" not in payload, (
            f"{name}：_args_schema_json 顶层仍含 description 键（去重未生效）"
        )


# ---------------------------------------------------------------------------
# 断言二：字段级 description 仍在（变异③ 改成递归删除 → 红）
# ---------------------------------------------------------------------------


def test_field_level_descriptions_preserved_all_tools() -> None:
    """**只**剔顶层：字段/嵌套级 description 一个不少（防递归删除手滑）。

    判据与实现同构：把 ``model_json_schema()`` 仅 pop 掉顶层 description 后
    序列化，须与 ``_args_schema_json`` **逐字节相等**。递归删除任何一层都会
    破坏该等式。
    """
    for name, spec in TOOL_REGISTRY.items():
        raw = spec.args_schema.model_json_schema()
        if getattr(spec.args_schema, "__doc__", None):
            raw.pop("description", None)
        expected = json.dumps(raw, ensure_ascii=False)
        assert _args_schema_json(spec) == expected, f"{name}：非仅剔顶层"


def test_nested_field_description_present() -> None:
    """具名锚点：``render_financial_report`` 的 ``_DynamicBlock.rows`` 字段说明
    （「九个封闭类目」）必须仍在参数 JSON 里——模型据此填 dynamic.rows。"""
    spec = TOOL_REGISTRY["render_financial_report"]
    payload = json.loads(_args_schema_json(spec))
    dynamic = payload["$defs"]["_DynamicBlock"]["properties"]["rows"]
    assert "九个封闭类目" in dynamic.get("description", ""), (
        "render_financial_report 的 dynamic.rows 字段说明被误删（递归剔除？）"
    )
    # 顶层仍无 description（与断言一一致口径）
    assert "description" not in payload


# ---------------------------------------------------------------------------
# 断言三：两处 __doc__ append 仍在提示词里（变异② 删任一处 → 红）
# ---------------------------------------------------------------------------


def test_build_args_schema_keeps_docstring_append() -> None:
    """生成点一：``build_args_schema`` 的 ``参数说明：{doc}`` append 未丢。

    断言精确到「标签 + docstring 全文」这一小段（不是裸子串），避免邻近同字词
    把断言救活。docstring 非空是前提（全部 21 工具均有）。
    """
    for name, spec in TOOL_REGISTRY.items():
        doc = getattr(spec.args_schema, "__doc__", None)
        assert doc, f"{name}：本测试前提是 args_schema 有 docstring"
        assert f"参数说明：{doc}" in build_args_schema(spec), (
            f"{name}：build_args_schema 丢了 docstring append"
        )


def test_tool_card_keeps_docstring_append() -> None:
    """生成点二：``tool_args_node`` 工具卡的 ``{doc}`` append 未丢。

    工具卡紧接在 schema JSON 后 append doc（``…参数 JSON Schema：{json}\\n{doc}``），
    故断言这一整段连续子串出现在真跑出的 system_prompt 里。
    """
    for name, spec in TOOL_REGISTRY.items():
        doc = getattr(spec.args_schema, "__doc__", None)
        assert doc, f"{name}：本测试前提是 args_schema 有 docstring"
        prompt = _captured_prompt(name)
        marker = f"参数 JSON Schema：{_args_schema_json(spec)}\n{doc}"
        assert marker in prompt, (
            f"{name}：tool_args 工具卡丢了 docstring append"
        )


def test_schema_instruction_uses_deduped_json() -> None:
    """参数轮的 schema_instruction（进末条 human 消息）走的是去重后的 JSON。"""
    for name, spec in TOOL_REGISTRY.items():
        instruction = build_args_schema(spec)
        assert _args_schema_json(spec) in instruction
        assert "参数说明：" in instruction


def test_decision_round_schema_unaffected() -> None:
    """参数轮去重对决策轮零影响（回归证据）：决策轮不含参数 schema。

    [2026-09-16 工具描述去重] 原断言 ``spec.description in build_select_schema(...)``
    的**前提已被本次改动移除**——决策轮工具枚举表不再内联于 ``build_select_schema``
    （该处改为指针句「tool_name 只能取上方『可用工具』表中的名称」，指向
    ``model_node._build_system_prompt`` 的「可用工具」段）。故**把该断言改指向
    真实注入路径**：用决策轮真正的 system prompt 组装函数 ``_build_system_prompt``
    取工具表段，逐工具断言其 description 出现。本用例本意是「参数轮去重对决策轮
    零影响」，核心不变式是决策轮**仍能看到全部工具描述**——描述只是从 schema 说明
    搬到了工具表段（且只出现一次），故改指向后语义更强而非更弱。
    核心负向不变式 ``决策轮 schema 不含 '"$defs"' 参数 schema`` 原样保留。
    """
    from administrative_agent.agent.model_client import build_select_schema
    from administrative_agent.agent.nodes.model_node import _build_system_prompt

    text = build_select_schema(TOOL_REGISTRY)
    # 决策轮不含 args_schema 序列化（无 schema JSON 顶层特征键）
    assert '"$defs"' not in text

    runtime = RuntimeBag(tools=TOOL_REGISTRY)
    prompt = _build_system_prompt(runtime, {}, "")
    for name, spec in TOOL_REGISTRY.items():
        assert spec.description in prompt, (
            f"{name}：决策轮 system prompt 未出现其 description（工具表段）"
        )


def test_decision_round_schema_drops_description_table() -> None:
    """[2026-09-16 工具描述去重] 负向断言：``build_select_schema`` 不得再内联
    任一工具的 ``description``（防有人把枚举表抄回去——那会让同一份描述在决策轮
    的 schema 说明与「可用工具」段各出现一次 + 末条 human 消息再一份）。

    锚点选「工具 description 全文」而非裸子串：抄回必逐字复现，删表则整段缺席。
    """
    from administrative_agent.agent.model_client import build_select_schema

    text = build_select_schema(TOOL_REGISTRY)
    for name, spec in TOOL_REGISTRY.items():
        assert spec.description not in text, (
            f"{name}：build_select_schema 又内联了该工具 description（去重回潮）"
        )
    # 表已换成指针句（指向「可用工具」段）——表缺了、指向仍在
    assert "上方『可用工具』表" in text
