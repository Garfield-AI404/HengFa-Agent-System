"""确认卡改进方案第 2 批守护测试（§3.2 超限例外 / §3.5 处置阶梯 / §3.7 迟到）。

判据纪律（仓库血泪教训 ``prompt-text-guard-false-locks``）：锚点必须把
**条件与后果绑在同一连续小句内**（按句末标点 ``[。；！？\\n]`` 切小句），
不用裸子串（会被邻近同字词救活）、不用宽松正则（空隙足够塞进否定词）。
多个独立 ``in`` 挡不住「解除绑定」，故关键关系一律用「同一小句内共存」
的连续子串钉。
禁令类关系另加**负向**判据 ``_all_prefixed``：正向 `in` 挡不住「保留原禁令、
另处补一句反写」——故凡出现某禁令片段处，其每一处都必须紧跟在否定词之后。
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import BaseModel, Field, ValidationError

from administrative_agent.agent.configuration import RuntimeBag
from administrative_agent.agent.nodes.tools_node import tools_node
from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools import TOOL_REGISTRY
from administrative_agent.agent.tools.base import (
    MAX_BATCH_ITEMS,
    batch_overflow_message,
    batch_overflow_user_detail,
)
from administrative_agent.web.chat_streams import EVENT_AGENT_ERROR


def _clauses(text: str) -> list[str]:
    """按句末标点/换行切小句（与「锚点与后果同处一小句」的判据同构）。

    字符集含 ``！？``：若正文改用感叹/疑问句而本判据只切 ``。；\\n``，本应
    分属两句的片段会被并成一个小句 → 同小句绑定判据被**放宽（假绿方向）**。
    刻意**不含 ``：``**——冒号常引出后果（如「…如下：」），切在其处会削弱绑定。
    """
    return [c for c in re.split(r"[。；！？\n]", text) if c.strip()]


def _has_clause_with_all(text: str, *fragments: str) -> bool:
    """是否存在**同一小句**同时含全部片段（用于绑条件与后果）。"""
    return any(all(fragment in clause for fragment in fragments)
               for clause in _clauses(text))


def _all_prefixed(text: str, fragment: str, prefix: str) -> bool:
    """``text`` 中**每一处** ``fragment`` 都紧跟在 ``prefix`` 之后（无例外）。

    负向判据用：既要求 fragment 至少出现一次，又要求**没有**任何未被 prefix
    修饰的实例（裸 `not in` 只能挡「全删」，挡不住「反写一处、正写仍在」）。
    """
    indices = [m.start() for m in re.finditer(re.escape(fragment), text)]
    return bool(indices) and all(
        text[max(0, i - len(prefix)):i] == prefix for i in indices
    )


# ===========================================================================
# §3.2 超限：6 份规则包的「超限（>20 条）例外」
# ===========================================================================

_OVERFLOW_EXCEPTION_KEYS = (
    "R2.order.create.metal",
    "R2.order.create.powder",
    "R3.order.update.metal",
    "R3.order.update.powder",
    "R7.finance.create",
    "R7.finance.update",
)


@pytest.mark.parametrize("key", _OVERFLOW_EXCEPTION_KEYS)
def test_overflow_exception_present_in_each_package(key: str) -> None:
    """6 份规则包都在「禁止拆成多次调用」后补了「超限（>20 条）例外」。

    判据绑在同一小句内：例外标记 + 「不得自行删减 / 不得只提交前 20 条」
    同句；出口句「先向用户说明实际条数」+「询问是否分批确认」同句。
    另加**负向**（``_all_prefixed``）：直接反写会红；「保留原禁令、另处补一句
    反写」这类**同族逃逸**也会被这个负向判据抓住（此前只有正向同句断言时它绿）。

    [P5 双真源] 例外标记里的条数从 ``MAX_BATCH_ITEMS`` **插值**、不写字面量：
    将来改动常量而规则包未同步 → 本断言即脱钩报红。
    """
    text = policy_for(key).text
    exception_marker = f"超限（>{MAX_BATCH_ITEMS} 条）例外"
    assert _has_clause_with_all(
        text, exception_marker, "不得自行删减条目", "不得只提交前 20 条"
    ), f"{key} 缺少「超限例外 → 不得自行删减」同句约束"
    assert _has_clause_with_all(
        text, "必须先向用户说明实际条数", "询问是否分批确认"
    ), f"{key} 缺少「先说明条数 → 询问分批」同句约束"
    # 本批只追加例外，原有「禁止拆成多次调用」句仍在（不得被例外取代）
    assert "禁止把同轮多条拆成多次调用" in text, f"{key} 原「禁止拆分」约束丢失"
    # 负向：禁令片段在全份正文里每一处都必须紧跟否定词（挡「另处补反写」）
    assert _all_prefixed(text, "自行删减条目", "不得"), (
        f"{key} 「自行删减条目」存在未受「不得」约束的实例（另处补反写？）"
    )
    assert _all_prefixed(text, "只提交前 20 条", "不得"), (
        f"{key} 「只提交前 20 条」存在未受「不得」约束的实例（另处补反写？）"
    )


# ===========================================================================
# §3.2 超限：纯函数 batch_overflow_message
# ===========================================================================

class _BatchProbe(BaseModel):
    items: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_ITEMS)


def _too_long_error(count: int = MAX_BATCH_ITEMS + 1) -> ValidationError:
    try:
        _BatchProbe(items=list(range(count)))
    except ValidationError as exc:
        return exc
    raise AssertionError("构造用例失效：未触发超限校验")


def _too_short_error() -> ValidationError:
    try:
        _BatchProbe(items=[])
    except ValidationError as exc:
        return exc
    raise AssertionError("构造用例失效：未触发下限校验")


def test_batch_overflow_message_has_three_elements() -> None:
    """21 条 → 模型面文案：三要素 + 实际条数 + 禁令/出口绑同小句 + 禁令不得反写。

    [真假锁修复] 原断言只查 4 个 token，「不得自行删减条目后重提」（禁令）与
    「向用户说明实际条数，并询问是否分批确认」（出口）恰落在 token 之外：把禁令
    反写成「可自行删减…」或把出口改成「可直接分批提交」都全绿。现改为同小句绑定
    （``_has_clause_with_all``）+ 负向（无「不得」修饰的「自行删减」不得出现）。
    """
    message = batch_overflow_message(_too_long_error(21))
    assert message is not None
    # 三要素 + 实际条数
    for token in ("上限 20", "未写入", "分批", "21"):
        assert token in message
    # 禁令：某小句同时含「不得自行删减条目」与「重提」
    assert _has_clause_with_all(
        message, "不得自行删减条目", "重提"
    ), "禁令未与「重提」绑同句"
    # 出口：某小句同时含「向用户说明实际条数」与「询问是否分批确认」
    assert _has_clause_with_all(
        message, "向用户说明实际条数", "询问是否分批确认"
    ), "出口未绑同句"
    # 负向：不得出现无「不得」修饰的「自行删减」（防禁令反写而 token 仍在）
    assert _all_prefixed(message, "自行删减", "不得"), (
        "出现无「不得」修饰的「自行删减」——禁令被反写"
    )


def test_batch_overflow_user_detail_second_person_and_voice_separated() -> None:
    """用户面短文案：三要素 + 第二人称「请回复」+ 不含模型口吻指令。"""
    detail = batch_overflow_user_detail(_too_long_error(21))
    assert detail is not None
    for token in ("上限 20", "未写入", "分批", "21"):
        assert token in detail
    assert "请回复" in detail, "用户面文案必须是第二人称（请回复）"
    # 语域分离：写给模型的禁令不得漏进用户面
    assert "不得自行删减" not in detail, "模型口吻指令漏进用户可见文案"


def test_batch_overflow_user_detail_shares_predicate_with_message() -> None:
    """两个文案共用同一取数：命中/未命中完全一致（仅语域不同）。"""
    exc = _too_long_error(21)
    assert batch_overflow_user_detail(exc) is not None
    assert batch_overflow_message(exc) is not None
    assert batch_overflow_user_detail(_too_short_error()) is None
    assert batch_overflow_user_detail(None) is None
    assert batch_overflow_user_detail([]) is None


def test_batch_overflow_message_accepts_errors_list() -> None:
    """既接受 ValidationError，也接受其 ``.errors()`` 列表，结果一致。"""
    exc = _too_long_error(21)
    assert batch_overflow_message(exc) == batch_overflow_message(exc.errors())


def test_batch_overflow_message_respects_limit_param() -> None:
    """``limit`` 参数生效（文案按传入上限而非硬编码 20）。"""
    message = batch_overflow_message(_too_long_error(21), limit=5)
    assert message is not None
    assert "上限 5" in message


def test_batch_overflow_message_continues_when_first_count_missing() -> None:
    """[L6] 某条匹配但取不到条数 → ``continue``（不因一条取不到就放弃后面的）。"""
    errors = [
        {"type": "too_long", "loc": ("items",), "input": None, "ctx": {}},
        {"type": "too_long", "loc": ("items",), "input": list(range(23)),
         "ctx": {"actual_length": 23}},
    ]
    message = batch_overflow_message(errors)
    assert message is not None
    assert "23" in message


def test_batch_overflow_message_none_for_non_overflow_error() -> None:
    """非超限的校验失败（下限/类型错）→ ``None``（保持原有行为）。"""
    assert batch_overflow_message(_too_short_error()) is None
    assert batch_overflow_message([]) is None
    assert batch_overflow_message(None) is None


def test_batch_overflow_message_none_for_non_items_loc() -> None:
    """``too_long`` 但 loc 末段不是 ``items`` → ``None``（不扩大接管范围）。"""
    errors = [{
        "type": "too_long",
        "loc": ("name",),
        "input": "x" * 30,
        "ctx": {"actual_length": 30},
    }]
    assert batch_overflow_message(errors) is None


def test_batch_overflow_message_none_when_count_unavailable() -> None:
    """条数取不到（ctx 无 actual_length 且 input 非列表）→ ``None``（宁可不显示）。"""
    errors = [{
        "type": "too_long",
        "loc": ("items",),
        "input": None,
        "ctx": {},
    }]
    assert batch_overflow_message(errors) is None


# ===========================================================================
# §3.5 处置阶梯：R4.query 规则 5
# ===========================================================================


def test_r4_query_has_step_clause_as_new_rule() -> None:
    """规则 5「目标定位与提问边界」已新增，且原规则 1–4 仍在（只追加不改写）。"""
    package = policy_for("R4.query")
    assert len(package.rules) == 5, "R4.query 应恰有 5 条规则"
    assert package.rules[0].startswith("查询字段"), "规则 1（第 1 批刚改）被动"
    assert package.rules[3].startswith("先查后答"), "规则 4 被动"
    assert package.rules[4].startswith("目标定位与提问边界"), "规则 5 未落位"


def test_r4_query_step3_binds_no_nonexistence_to_requery() -> None:
    """阶梯 (3)：不得凭一次返回否定存在，须换条件重查——绑在同一小句。"""
    text = policy_for("R4.query").text
    assert _has_clause_with_all(
        text,
        "不得依据一次查询的返回就下「不存在」的结论",
        "换条件重查",
    ), "R4.query 阶梯 (3) 未把「不得凭一次返回否定存在」与「换条件重查」绑同句"
    # 负向：该禁令片段在全份正文里每一处都必须紧跟「不得」（挡另处补反写）
    assert _all_prefixed(
        text, "依据一次查询的返回就下「不存在」的结论", "不得"
    ), "R4.query 阶梯 (3) 禁令存在未受「不得」约束的实例（另处补反写？）"


def test_r4_query_step1_name_to_id_and_step2_self_disambiguate() -> None:
    """阶梯 (1) 名称先解析成 ID；(2) 结果多条先自行消歧、不直接反问。

    负向只对**禁令**片段「直接反问」（正文用词是「不要」）施加；「自行消歧」是
    **正向**指令（要求模型自行消歧），合法地出现在非否定语境，故**不**套负向
    判据——强行套会弱化判据（为让它绿而放宽），非本意。
    """
    text = policy_for("R4.query").text
    assert _has_clause_with_all(text, "必须先解析成对应 ID", "再查业务实体")
    assert _has_clause_with_all(text, "多于一条", "自行消歧", "不要直接反问")
    assert _all_prefixed(text, "直接反问", "不要"), (
        "R4.query 阶梯 (2) 「直接反问」存在未受「不要」约束的实例（另处补反写？）"
    )


# ===========================================================================
# §3.7 迟到条款 + 单位口径收口：R1.attendance.daily
# ===========================================================================


def test_attendance_rule_unit_is_hours() -> None:
    """规则 3：单位收口为小时，不再写「小时转分钟」。

    「不要换算成分钟填写」与后果「9 小时写成 540 是错的」同在
    ``；`` 后的同一小句内（该句正是 540 陷阱的反向钉）。
    """
    text = policy_for("R1.attendance.daily").text
    assert _has_clause_with_all(text, "一律按小时填写")
    assert _has_clause_with_all(
        text, "不要换算成分钟填写", "9 小时写成 540 是错的"
    ), "规则 3 未把「不要换算成分钟」与「540 是错的」绑同句"
    assert "小时转分钟" not in text, "旧「小时转分钟」口径不得回退"
    # 负向：该禁令片段在全份正文里每一处都必须紧跟「不要」（挡另处补反写）
    assert _all_prefixed(text, "换算成分钟填写", "不要"), (
        "规则 3 「换算成分钟填写」存在未受「不要」约束的实例（另处补反写？）"
    )


def test_attendance_rule7_late_converts_to_leave_hours() -> None:
    """规则 7：「迟到 N 小时」按请假 N 小时记录（落 leave_hours）——绑同句。"""
    text = policy_for("R1.attendance.daily").text
    assert _has_clause_with_all(
        text, "「迟到 N 小时」按请假 N 小时记录", "落 leave_hours"
    ), "规则 7 未把「迟到→请假」与「落 leave_hours」绑同句"


def test_attendance_rule9_forbids_silent_drop() -> None:
    """规则 9：严禁因无对应字段而静默丢弃；无字段可落必须反问——绑同句。"""
    text = policy_for("R1.attendance.daily").text
    assert _has_clause_with_all(
        text, "严禁因为「系统里没有对应字段」就丢弃用户已经提到的信息"
    ), "规则 9 缺「严禁因无字段而丢弃信息」同句约束"
    assert _has_clause_with_all(
        text, "无字段可落时必须向用户反问确认", "不得静默丢弃"
    ), "规则 9 未把「无字段可落→反问」与「不得静默丢弃」绑同句"
    # 负向：两个「静默 X」禁令片段在全份正文里每一处都必须紧跟「不得」
    assert _all_prefixed(text, "静默丢弃", "不得"), (
        "规则 9 「静默丢弃」存在未受「不得」约束的实例（另处补反写？）"
    )
    assert _all_prefixed(text, "静默改写成别的语义", "不得"), (
        "规则 9 「静默改写成别的语义」存在未受「不得」约束的实例（另处补反写？）"
    )


def test_attendance_rule6_absence_becomes_9h_leave() -> None:
    """规则 6（§3.7 的 A2 验收依赖）：「今日没来」→ 按请假 9 小时记录，必须钉住。

    [零守护修复] 该规则本批刚改写，此前无任何断言——审核实测删掉规则 6 后仍全绿
    （`business_terms` 的「分钟」由规则 3 的否定句供上、规则 3/7/9 断言全过）。
    """
    text = policy_for("R1.attendance.daily").text
    # 「今日没来」与后果绑在同一小句
    assert _has_clause_with_all(
        text, "今日没来", "按请假 9 小时记录"
    ), "规则 6 未把「今日没来」与「按请假 9 小时记录」绑同句"
    # 用户本人写的等值括注必须保留（同时修掉 test_prompts 里「分钟」词元形同
    # 虚设的 L3：该词元此前只靠规则 3 的否定句「不要换算成分钟填写」供上，
    # 本断言把「540 分钟」这处正向依据钉住——**不删「分钟」词元**）
    assert _has_clause_with_all(
        text, "9 小时", "540 分钟"
    ), "规则 6 未把「9 小时」与等值括注「540 分钟」绑同句"
    # 用户本人写的业务事实「每日工作时长为 9 小时」也须在同一小句——防只改
    # 一处造成内部不一致（把工作时长改成 8 小时而请假仍按 9 小时）
    assert _has_clause_with_all(
        text, "每日工作时长为 9 小时", "540 分钟"
    ), "规则 6 的「每日工作时长为 9 小时」事实与「540 分钟」不在一句（9 小时被改？）"


# ===========================================================================
# §3.2 超限：走真实 tools_node 验证接线（「用户能看到出口」全靠这条接线）
#
# 既有 tests/unit/test_financial_report.py:640-667（test_tools_node_returns_
# ok_false_for_days_mismatch）只覆盖**非超限**失败——它断言 current_step ==
# "tool_invalid_args" 与 "rent.days" in payload["error"]，但**不触及**超限路径
# 与 detail 键。本段补齐（照该用例走法构造 state/runtime，用 finance_create 的
# items 传 21 条）。
# ===========================================================================

#: tools_node 超限 ToolMessage 正文的中文前缀（与实现逐字一致）。
_OVERFLOW_CONTENT_PREFIX = "工具参数解析失败（确定性校验）："


def _capture_runtime() -> tuple[RuntimeBag, list[tuple[str, dict]]]:
    """构造走 tools_node 的 runtime + 捕获 sink 的事件列表。"""
    events: list[tuple[str, dict]] = []
    runtime = RuntimeBag(
        tools=TOOL_REGISTRY,
        context=AgentContext(
            user_id="manager01",
            user_role="manager",
            scope=frozenset({"erp:query"}),
            thread_id="thread-1",
        ),
        sink=lambda event_type, payload: events.append((event_type, payload)),
    )
    return runtime, events


def _finance_item() -> dict:
    return {"tx_type": "支出", "category": "运费",
            "amount": "1.00", "tx_date": "2026-09-29"}


def test_tools_node_overflow_message_chinese_and_event_detail() -> None:
    """21 条 → tools_node 返回中文文案（无英文 pydantic 原文）+ 事件带 detail。

    [真假锁修复] 断言把禁令/出口也绑到同小句（token 之外的逃逸形态被挡），
    并逐字钉住「模型面正文 = 前缀 + batch_overflow_message」「detail =
    batch_overflow_user_detail」两条文案口径。
    """
    runtime, events = _capture_runtime()
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "finance_create",
            "tool_arguments": {"items": [_finance_item() for _ in range(21)]},
        },
    }
    out = tools_node(state, runtime)

    assert out["current_step"] == "tool_invalid_args"
    assert out["pending_action"] is None
    message = out["messages"][0].content
    payload = json.loads(message)
    assert payload["ok"] is False
    # 三要素 + 实际条数
    for token in ("上限 20", "未写入", "分批", "21"):
        assert token in payload["error"], f"正文缺 {token!r}"
    # 禁令/出口绑同小句（模型面文案判据）
    assert _has_clause_with_all(payload["error"], "不得自行删减条目", "重提")
    assert _has_clause_with_all(
        payload["error"], "向用户说明实际条数", "询问是否分批确认"
    )
    # 英文 pydantic 原文必须消失（正文只拼中文 overflow 文案）
    assert "too_long" not in message
    assert "List should have at most" not in message

    # 事件：错误码不变 + 增设 detail
    errors = [p for et, p in events if et == EVENT_AGENT_ERROR]
    assert len(errors) == 1, f"期望恰一条 agent_error，实际 {len(errors)}"
    ev = errors[0]
    assert ev["error"] == "invalid_arguments"
    assert ev["tool_name"] == "finance_create"
    assert set(ev.keys()) == {"tool_name", "error", "detail"}
    assert isinstance(ev["detail"], str) and ev["detail"]
    # 模型面正文 == 前缀 + batch_overflow_message（逐字；条数/上限同口径）
    probe = _too_long_error(21)
    assert payload["error"] == _OVERFLOW_CONTENT_PREFIX + batch_overflow_message(probe)
    # detail == 用户面短文案（逐字），且不携带模型口吻
    assert ev["detail"] == batch_overflow_user_detail(probe)
    assert "不得自行删减" not in ev["detail"]


def test_tools_node_non_overflow_keeps_legacy_payload_without_detail() -> None:
    """非超限校验失败 → 不增设 detail，正文仍是原 pydantic 说明（不扩大接管）。

    回归钉子：接线只对 too_long/items 生效，其余失败路径逐字保持旧行为。
    """
    runtime, events = _capture_runtime()
    bad_item = {"tx_type": "支出", "category": "运费",
                "amount": "abc", "tx_date": "2026-09-29"}
    state = {
        "messages": [],
        "pending_action": {
            "tool_name": "finance_create",
            "tool_arguments": {"items": [bad_item]},
        },
    }
    out = tools_node(state, runtime)

    assert out["current_step"] == "tool_invalid_args"
    assert out["pending_action"] is None
    payload = json.loads(out["messages"][0].content)
    assert payload["ok"] is False
    assert set(payload.keys()) == {"ok", "error"}, "非超限失败不得增设任何键"
    assert payload["error"].startswith(_OVERFLOW_CONTENT_PREFIX)

    errors = [p for et, p in events if et == EVENT_AGENT_ERROR]
    assert len(errors) == 1
    ev = errors[0]
    assert "detail" not in ev, "非超限失败的事件 payload 不得带 detail 键"
    assert set(ev.keys()) == {"tool_name", "error"}
    assert ev["error"] == "invalid_arguments"
