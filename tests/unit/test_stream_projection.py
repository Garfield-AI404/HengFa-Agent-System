"""SSE 事件投影白名单单测（A 档：只落结构，绝不落模型输入输出原文）。

纯函数、零 I/O。覆盖方案 §八 单测 1-5，并常驻守护：
- 漂移：PROJECTION 键集 == 全部 EVENT_* 常量值 ∪ {"re_requested"}；
- 表驱动全 20 类：投影后**键集合相等**（用 == 不是 ⊆，否则「未声明键透传」
  抓不到）；
- 原文不落（子串反证 + 相等断言）；
- fail-closed：未声明事件类型落 {} + unmapped；
- 值级守卫：超长截断到具体上界、message/index 不出现但计数键在。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from administrative_agent.agent.prompts.skills import ALL_SKILL_KEYS
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.web import chat_streams, stream_projection
from administrative_agent.web.stream_persistence import (
    _PRECHECK_BYTE_LIMIT,
    _payload_size_estimate,
)
from administrative_agent.web.stream_projection import (
    MAX_VALUE_CHARS,
    PROJECTION,
    RECORD_KIND_PUSHED,
    RECORD_KIND_UNMAPPED,
    TRUNCATED_SUFFIX,
    project,
    project_suppressed,
)


def _event_constants() -> set[str]:
    """扫描 chat_streams 模块全部 ``EVENT_*`` 字符串常量值。"""
    return {
        value
        for name, value in vars(chat_streams).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    }


# ---------------------------------------------------------------------------
# 1. 漂移守护
# ---------------------------------------------------------------------------


def test_projection_covers_all_event_constants():
    """PROJECTION 键集必须恰好 = 全部 EVENT_* 常量值 ∪ {"re_requested"}。

    改/加事件常量而忘了同步本表 → 本用例红（fail-closed 落 {} 会被抓到）。
    """
    assert set(PROJECTION) == _event_constants() | {"re_requested"}


# ---------------------------------------------------------------------------
# 2. 表驱动全 17 类：键集合相等
# ---------------------------------------------------------------------------


CASES: list[tuple[str, dict, set[str]]] = [
    ("task_failed", {"anything": "x"}, set()),
    ("task_cancelled", {"reason": "generation_cancelled", "extra": 1}, {"reason"}),
    ("SESSION_EXPIRED", {"reason": "session_expired"}, {"reason"}),
    ("agent_started", {"thread_id": "thread-mgr-1", "junk": [1]}, {"thread_id"}),
    (
        "tool_running",
        {"task_id": "task-1", "tool_name": "erp_query", "stage": "collecting_arguments"},
        {"task_id", "tool_name", "stage"},
    ),
    (
        "tool_result",
        {"task_id": "task-1", "tool_name": "erp_query", "ok": True,
         "summary": {"amount": 12345, "customer_name": "张三"}},
        {"task_id", "tool_name", "ok"},
    ),
    (
        "confirmation_requested",
        {"task_id": "task-1", "tool_name": "order_create", "payload_hash": "abc",
         "card": {"amount": 1, "customer_name": "张三", "model": "M"},
         "execution": {"secret": "x"}},
        {"task_id", "tool_name", "payload_hash", "re_requested", "card_keys"},
    ),
    ("agent_reply", {"reply_text": "已为您完成查询。", "next_action": "reply"},
     {"reply_chars"}),
    ("agent_error",
     {"error": "model_unavailable", "tool_name": "erp_query",
      "tool_executed": True, "message": "对话处理失败，请稍后重试。"},
     {"error", "tool_name", "tool_executed", "has_message"}),
    # §3.1 三态边界：缺 tool_name（steps_exhausted/model_unavailable 的真实形态）
    # → 键**恒在**且为 "none"（allow 下会「键不出现」——语义反转是本批有意的）
    ("agent_error", {"error": "steps_exhausted", "tool_executed": False},
     {"error", "tool_name", "tool_executed", "has_message"}),
    ("agent_reply_start", {"noise": 1}, set()),
    ("agent_reply_chunk", {"chunk": "客户张三"}, {"chunk_chars"}),
    ("agent_reasoning_chunk", {"chunk": "思考中"}, {"chunk_chars"}),
    (
        "memory_compressed",
        {"segment_no": 2, "tier": 60, "summary_chars": 1480, "archived_messages": 31,
         "index": "【段1】张三", "summary": "摘要正文"},
        {"segment_no", "tier", "summary_chars", "archived_messages", "index_chars"},
    ),
    ("memory_compression_failed", {"error": "summary_failed", "detail": "x"},
     {"error"}),
    (
        "reconcile_report",
        {"report_id": "rpt-1", "row_count": 10, "subject_match_status": "matched",
         "summary_counts": {"amount": 999, "households": 3}},
        {"report_id", "row_count", "subject_match_status"},
    ),
    (
        "export_ready",
        {"file_id": "exp-1", "entity": "metal_orders", "row_count": 10, "total": 100,
         "expires_at": "2026-09-19T00:00:00+00:00", "filename": "张三_订单.xlsx"},
        {"file_id", "entity", "row_count", "total", "expires_at", "filename_chars"},
    ),
    (
        "re_requested",
        {"task_id": "task-1", "tool_name": "order_create", "payload_hash": "abc",
         "card": {"amount": 1, "customer_name": "张三"}},
        {"task_id", "tool_name", "payload_hash", "re_requested", "card_keys"},
    ),
    # re_requested 无 tool_name：改 derive 三态后**键恒出现**（值为 "none"）
    # ——语义反转（allow 下该键会缺席），这是有意的，显式表达。
    (
        "re_requested",
        {"task_id": "task-1", "payload_hash": "abc",
         "card": {"amount": 1, "customer_name": "张三"}},
        {"task_id", "tool_name", "payload_hash", "re_requested", "card_keys"},
    ),
    (
        "compression_decision",
        {"decision": "compressed", "occupancy_pct": 95, "messages_count": 12,
         "segment_messages": 7},
        {"decision", "occupancy_pct", "messages_count", "segment_messages"},
    ),
    (
        "decision_input_summary",
        {"history_raw": 12, "history_sent": 9, "evicted_details": 2,
         "summaries_count": 1, "active_skill": "SK.financial_report"},
        {"history_raw", "history_sent", "evicted_details", "summaries_count",
         "active_skill"},
    ),
    (
        "args_input_summary",
        {"history_raw": 12, "history_sent": 9, "evicted_details": 2,
         "active_skill": None},
        {"history_raw", "history_sent", "evicted_details", "active_skill"},
    ),
]


def test_projection_key_sets_exact():
    """逐类型断言投影后键集合**相等**（== 而非 ⊆）。

    ⊆ 会让「多落一个键」（如放行 chunk 原文）的变异全绿。
    """
    for event_type, raw, expected in CASES:
        out = project(event_type, raw)
        assert set(out.payload) == expected, f"{event_type}: {out.payload!r}"
        assert out.record_kind == RECORD_KIND_PUSHED


def test_projection_covers_all_cases():
    """表驱动必须覆盖全部事件类型（防新增类型忘了补用例）。"""
    assert {c[0] for c in CASES} == set(PROJECTION)


# ---------------------------------------------------------------------------
# 3. chunk 原文不落
# ---------------------------------------------------------------------------


def test_agent_reply_chunk_never_stores_text():
    out = project("agent_reply_chunk", {"chunk": "客户张三欠款12345"})
    assert out.payload == {"chunk_chars": 11}
    dumped = json.dumps(out.payload, ensure_ascii=False)
    assert "张三" not in dumped
    assert "12345" not in dumped


def test_agent_reasoning_chunk_never_stores_text():
    out = project("agent_reasoning_chunk", {"chunk": "先查客户张三再算欠款"})
    dumped = json.dumps(out.payload, ensure_ascii=False)
    assert "张三" not in dumped
    assert set(out.payload) == {"chunk_chars"}


def test_agent_reply_never_stores_text():
    out = project("agent_reply", {"reply_text": "张三欠款12345元"})
    assert out.payload == {"reply_chars": 10}
    assert "张三" not in json.dumps(out.payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 4. fail-closed
# ---------------------------------------------------------------------------


def test_unknown_event_type_fails_closed():
    out = project("some_future_event", {"text": "客户张三", "amount": 12345})
    assert out.payload == {}
    assert out.record_kind == RECORD_KIND_UNMAPPED


# ---------------------------------------------------------------------------
# 5. 值级守卫与剥原文
# ---------------------------------------------------------------------------


def test_value_length_guard_truncates_to_exact_bound():
    out = project("agent_started", {"thread_id": "x" * 500})
    value = out.payload["thread_id"]
    assert len(value) == MAX_VALUE_CHARS + len(TRUNCATED_SUFFIX)
    assert value.endswith(TRUNCATED_SUFFIX)
    assert out.truncated_values == 1
    # 上界内的值不动、不计数
    out2 = project("agent_started", {"thread_id": "y" * MAX_VALUE_CHARS})
    assert out2.payload["thread_id"] == "y" * MAX_VALUE_CHARS
    assert out2.truncated_values == 0


def test_agent_error_message_not_stored_but_flag_present():
    out = project(
        "agent_error",
        {"error": "model_unavailable", "message": "未找到客户 张三"},
    )
    assert "message" not in out.payload
    assert out.payload["has_message"] is True
    assert out.payload["error"] == "model_unavailable"
    # 无 message 时 has_message 仍为 False（键恒在，值稳定）
    out2 = project("agent_error", {"error": "steps_exhausted"})
    assert out2.payload["has_message"] is False


def test_agent_error_unknown_code_becomes_other():
    out = project("agent_error", {"error": "str(exc) 原文泄漏"})
    assert out.payload["error"] == "other"
    assert "str(exc)" not in json.dumps(out.payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "code", ["write_retry_suppressed", "preparation_error"]
)
def test_agent_error_real_codes_pass_through_not_other(code):
    """P1-3：这两个错误码在 tools_node.py 真实存在，必须原样透出而非 ``other``。

    漏掉它们会让面板上这两类失败全部归入 ``other``——正是 fail-closed
    枚举最常见的「静默失配」。断言精确等于该码（不是「!= other」）。
    """
    out = project("agent_error", {"error": code, "tool_name": "finance_create"})
    assert out.payload["error"] == code


def test_memory_compressed_index_not_stored_but_len_present():
    index = "【段1】张三｜欠款12345"
    out = project(
        "memory_compressed",
        {
            "segment_no": 2,
            "tier": 60,
            "summary_chars": 1480,
            "archived_messages": 31,
            "index": index,
            "summary": "摘要正文可能含客户名",
        },
    )
    assert "index" not in out.payload
    assert "summary" not in out.payload
    assert out.payload["index_chars"] == len(index)
    dumped = json.dumps(out.payload, ensure_ascii=False)
    assert "张三" not in dumped
    assert "12345" not in dumped


# ---------------------------------------------------------------------------
# 影子行（suppressed）
# ---------------------------------------------------------------------------


def test_project_suppressed_injects_reason_and_never_stores_text():
    out = project_suppressed(
        "agent_reply", {"reply_text": "中间轮回复张三"}, "interrupted"
    )
    assert out.record_kind == "suppressed"
    assert out.payload["suppressed_reason"] == "interrupted"
    assert out.payload["reply_chars"] == len("中间轮回复张三")
    assert "张三" not in json.dumps(out.payload, ensure_ascii=False)


def test_project_suppressed_unknown_reason_fails_closed():
    out = project_suppressed("agent_reply", {"reply_text": "x"}, "随便写的自由文本")
    assert out.payload["suppressed_reason"] == "unknown"


# ---------------------------------------------------------------------------
# 第 2 步 (c)：tool_name 写入侧收口（三态枚举）
# ---------------------------------------------------------------------------

#: ``tool_name`` 收口涉及的**五条规则**（流 3.1 的四条 + `re_requested`）。
#: 每条都必须把 tool_name 改成 derive 三态，任一条漏改都会让幻觉串落库——
#: 参数化扫全部键 + 全规则。`re_requested` 是原方案漏掉的一处：不补的话
#: 「落库的 tool_name 只可能是枚举值」这条不变量是假的。
_TOOL_NAME_RULES = (
    "tool_running",
    "tool_result",
    "confirmation_requested",
    "agent_error",
    "re_requested",
)


@pytest.mark.parametrize("key", sorted(TOOL_REGISTRY))
def test_registered_tool_names_pass_through_for_all_rules(key):
    """§6.1-3：**参数化全部 21 个注册键**，四条规则都必须原样放行。

    只测 ``"erp_query"`` 对「枚举漏了 20 个成员」完全无感（同款先例见
    ``test_agent_error_real_codes_pass_through_not_other``）。断言精确等于
    键本身（不是「!= other」）。
    """
    for rule in _TOOL_NAME_RULES:
        out = project(rule, {"tool_name": key})
        assert out.payload["tool_name"] == key, (rule, key, out.payload)


def test_tool_registry_keys_equal_spec_names():
    """§6.1-4 静默失配守卫：投影按**键**判定、模型回声的是 ``spec.name``。

    两者分叉 → 该工具全部事件静默变 ``"other"``，而三层测试**全部照绿**
    （期望值取自同一注册表）——本仓「双真源」的典型形态。生产侧守卫在
    ``stream_projection._registered_tool_names``；此处独立断言该不变量。
    """
    assert set(TOOL_REGISTRY) == {s.name for s in TOOL_REGISTRY.values()}


@pytest.mark.parametrize("rule", _TOOL_NAME_RULES)
def test_tool_name_three_state_boundaries(rule):
    """§6.1-5 三态边界：``"other"``（幻觉）/ ``"none"``（缺键、None、非字符串）。

    ⚠️ **语义反转（有意）**：``allow`` 下 ``None``/``int`` 被 ``_is_scalar``
    丢弃、**键不出现**；derive 下**键恒出现**且为 ``"none"``——「本轮没工具」
    与「模型幻觉了个工具名」必须可分（后者正是要抓的）。
    """
    # 已知名 → 原名
    known = sorted(TOOL_REGISTRY)[0]
    assert project(rule, {"tool_name": known}).payload["tool_name"] == known
    # 幻觉串 → "other"（fail closed），且原文不进任何键
    hallucinated = project(rule, {"tool_name": "完全不存在_幻觉工具"})
    assert hallucinated.payload["tool_name"] == "other"
    assert "幻觉" not in json.dumps(hallucinated.payload, ensure_ascii=False)
    # 缺键 / None / 空串 / 非字符串 → "none"
    for raw in ({}, {"tool_name": None}, {"tool_name": ""}, {"tool_name": 123}):
        assert project(rule, raw).payload["tool_name"] == "none", raw


@pytest.mark.parametrize(
    "state_skill,expected",
    [
        ("SK.financial_report", "SK.financial_report"),  # 已知名原样
        (None, "none"),                                    # 主路径：未声明流程
        ("幻觉流程串", "other"),                            # 幻觉 → fail closed
    ],
)
@pytest.mark.parametrize("rule", ["decision_input_summary", "args_input_summary"])
def test_active_skill_three_state(rule, state_skill, expected):
    """§6.4-2：``active_skill`` 三态参数化（决策轮 + 参数轮各覆一遍）。"""
    out = project(rule, {"active_skill": state_skill})
    assert out.payload["active_skill"] == expected


def test_compression_decision_enum_fails_closed():
    """``compression_decision.decision`` 是闭集（未知 → ``other``，无通配符）。"""
    assert (
        project("compression_decision", {"decision": "compressed"}).payload["decision"]
        == "compressed"
    )
    out = project("compression_decision", {"decision": "模型编的四态"})
    assert out.payload["decision"] == "other"
    assert "模型编" not in json.dumps(out.payload, ensure_ascii=False)


#: §6.5 敌意取值：巨大 int + 超长串（derive 键的串会被枚举收敛为短值）。
_HOSTILE_RAW: dict[str, dict] = {
    "compression_decision": {
        "decision": "x" * 10000,
        "occupancy_pct": 10**15,
        "messages_count": 10**15,
        "segment_messages": 10**15,
        "junk": "y" * 10000,
    },
    "decision_input_summary": {
        "history_raw": 10**15,
        "history_sent": 10**15,
        "evicted_details": 10**15,
        "summaries_count": 10**15,
        "active_skill": "x" * 10000,
    },
    "args_input_summary": {
        "history_raw": 10**15,
        "history_sent": 10**15,
        "evicted_details": 10**15,
        "active_skill": "x" * 10000,
    },
}


@pytest.mark.parametrize("rule", sorted(_HOSTILE_RAW))
def test_new_rules_within_precheck_byte_budget(rule):
    """§6.5 字节预算的机器自判（把「扩白名单者自判」变成断言）。

    敌意取值下必须 ``< _PRECHECK_BYTE_LIMIT`` 且 ``truncated_values == 0``。
    后者让「将来有人把某个值改成字符串形态」立刻变红（长串会被值级守卫截断
    并计数）。**不改 ``stream_persistence.py``**——这是测试，不是生产防御。
    """
    out = project(rule, _HOSTILE_RAW[rule])
    assert out.truncated_values == 0, out.payload
    assert _payload_size_estimate(out.payload) < _PRECHECK_BYTE_LIMIT, out.payload


def test_new_rules_allow_at_most_five_keys():
    """§2.1：每条新投影规则的 ``allow`` 键数 ≤5（扩白名单者自判的机器版）。"""
    for rule in ("compression_decision", "decision_input_summary", "args_input_summary"):
        assert len(PROJECTION[rule].allow) <= 5, rule


# ---------------------------------------------------------------------------
# §6.6 顶层零 agent 依赖（把设计不变量变成机器契约）
# ---------------------------------------------------------------------------


def test_module_has_no_toplevel_agent_import():
    """``stream_projection`` 顶层**绝不** import ``agent.*``（只能函数内延迟）。

    顶层反向 import 会与 ``compression_node`` 反向 import ``web`` 形成只靠运气
    不闭合的环（``chat_streams`` 运行时 import 本模块）——一旦闭合就是应用
    import 期直接炸，且**无任何测试会提前变红**。用 AST 精确检查模块级
    import 语句（``__dict__`` 检查抓不到 ``from x import y``）。
    """
    source = Path(stream_projection.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in tree.body:  # 只看模块级（函数体内是允许的延迟 import）
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("administrative_agent.agent"):
                offenders.append(node.module or "")
        elif isinstance(node, ast.Import):
            offenders.extend(
                a.name
                for a in node.names
                if a.name.startswith("administrative_agent.agent")
            )
    assert offenders == [], offenders
