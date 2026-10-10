"""[2026-10-05 确认卡改进第 3 批] 展示级键 source_quote / source_note 单元测试。

覆盖（对应方案 §3.3 / §3.6）：
- ConfirmationCard.to_dict()：source_note 未设置（None）时不落 JSON 键
  （旧卡零回归）；
- 收支卡（创建/更新）带 source_note；订单卡/考勤卡不带；
- source_note 不进 payload_hash（加不加它，哈希逐字节相同）；
- ``_tool_ctx`` 的 last_user_message：不传 state 为空串；传 state 取最近一条
  human 全文。
写分支 source_quote 的端到端接线见 tests/integration/test_finance_batch_workflow.py。
"""

from __future__ import annotations

from dataclasses import replace

from langchain_core.messages import HumanMessage

from administrative_agent.agent.configuration import RuntimeBag, _tool_ctx
from administrative_agent.agent.tools.attendance import build_attendance_confirmation_card
from administrative_agent.agent.tools.finance import build_finance_create_confirmation_card
from administrative_agent.business.attendance_rules import AttendancePlan, PlannedRecord
from administrative_agent.domains.finance import (
    FINANCE_SOURCE_NOTE,
    build_finance_create_batch_confirmation_card,
    build_finance_update_batch_confirmation_card,
    build_finance_update_confirmation_card,
)
from administrative_agent.domains.orders import (
    ConfirmationCard,
    build_update_confirmation_card,
    card_payload_hash,
    payload_hash_for,
)


def _bare_card(**overrides) -> ConfirmationCard:
    base = dict(
        operation_type="op",
        target="t",
        fields_to_write=(("a", 1),),
        auto_filled=(),
        expected_impact="impact",
        limitations=(),
        payload_hash="",
    )
    base.update(overrides)
    return ConfirmationCard(**base)


# ---------------------------------------------------------------------------
# ConfirmationCard.to_dict 条件输出（None 不落键）
# ---------------------------------------------------------------------------


def test_to_dict_omits_source_note_when_none() -> None:
    data = _bare_card().to_dict()
    assert "source_note" not in data
    # 键序不变（旧卡 JSON 逐字节等价）
    assert list(data) == [
        "operation_type", "target", "fields_to_write", "auto_filled",
        "expected_impact", "limitations", "payload_hash",
    ]


def test_to_dict_emits_source_note_when_set() -> None:
    data = _bare_card(source_note="提示").to_dict()
    assert data["source_note"] == "提示"


def test_source_note_does_not_affect_hash() -> None:
    """加不加 source_note，payload_hash 逐字节相同（绝不进哈希）。"""
    plain = replace(_bare_card(hash_fields=(("a", 1),)),
                    payload_hash="x")  # 占位；下面以 card_payload_hash 重算
    plain = replace(plain, payload_hash=card_payload_hash(plain))
    noted = replace(plain, source_note=FINANCE_SOURCE_NOTE)
    assert card_payload_hash(noted) == plain.payload_hash
    # 且等于独立计算口径
    assert plain.payload_hash == payload_hash_for("op", "t", (("a", 1),))


# ---------------------------------------------------------------------------
# 收支卡带 source_note；订单/考勤卡不带
# ---------------------------------------------------------------------------


def test_finance_create_card_has_source_note_and_hash_unaffected() -> None:
    payload = {"tx_type": "支出", "category": "运费", "amount": "100.00",
               "tx_date": "2026-09-10"}
    card = build_finance_create_confirmation_card(payload)
    assert card.to_dict()["source_note"] == FINANCE_SOURCE_NOTE
    assert card.payload_hash == payload_hash_for(
        card.operation_type, card.hash_target or card.target, card.hash_fields
    )


def test_finance_update_card_source_note_requires_category_field() -> None:
    """更新卡：改动含 category → 挂；只改金额（无分类行）→ 不挂。"""
    with_category = build_finance_update_confirmation_card(
        record_id=5,
        before={"tx_date": "2026-09-01", "category": "运费"},
        changes={"category": "房租"},
        expected_updated_at="T0",
    )
    assert with_category.to_dict()["source_note"] == FINANCE_SOURCE_NOTE

    amount_only = build_finance_update_confirmation_card(
        record_id=5,
        before={"tx_date": "2026-09-01", "category": "运费"},
        changes={"amount": "150.00"},
        expected_updated_at="T0",
    )
    assert "source_note" not in amount_only.to_dict()


def test_finance_batch_cards_source_note_requires_category_in_a_row() -> None:
    payload = {"tx_type": "支出", "category": "运费", "amount": "100.00",
               "tx_date": "2026-09-10"}
    create_card = build_finance_create_batch_confirmation_card([
        ("支出·运费（2026-09-10）", dict(payload), ()),
        ("支出·运费（2026-09-10）", dict(payload), ()),
    ])
    assert create_card.to_dict()["source_note"] == FINANCE_SOURCE_NOTE

    # 批量更新：任一行含 category → 挂
    with_category = build_finance_update_batch_confirmation_card([
        {"record_id": 1, "before": {"tx_date": "2026-09-01", "category": "运费"},
         "changes": {"amount": "1.00"}, "expected_updated_at": "T0"},
        {"record_id": 2, "before": {"tx_date": "2026-09-01", "category": "房租"},
         "changes": {"category": "水电"}, "expected_updated_at": "T0"},
    ])
    assert with_category.to_dict()["source_note"] == FINANCE_SOURCE_NOTE

    # 批量更新：全部条目只改金额（无分类字段）→ 整卡不挂
    amount_only = build_finance_update_batch_confirmation_card([
        {"record_id": 1, "before": {"tx_date": "2026-09-01", "category": "运费"},
         "changes": {"amount": "1.00"}, "expected_updated_at": "T0"},
        {"record_id": 2, "before": {"tx_date": "2026-09-01", "category": "房租"},
         "changes": {"amount": "2.00"}, "expected_updated_at": "T0"},
    ])
    assert "source_note" not in amount_only.to_dict()


def test_finance_source_note_project_alone_does_not_trigger() -> None:
    """只改 project（无 category）→ 不挂（project 不计入「含分类」）。"""
    card = build_finance_update_confirmation_card(
        record_id=5,
        before={"tx_date": "2026-09-01", "category": "运费"},
        changes={"project": "电费"},
        expected_updated_at="T0",
    )
    assert "source_note" not in card.to_dict()


def test_order_update_card_omits_source_note() -> None:
    card = build_update_confirmation_card(
        order_type="metal", order_id=10,
        before={"model": "MK-01"}, changes={"quantity": "2"},
        expected_updated_at="T0",
    )
    assert "source_note" not in card.to_dict()


def test_attendance_card_omits_source_note() -> None:
    plan = AttendancePlan(
        attendance_date="2026-08-22",
        created=(PlannedRecord(employee_ref="张三",
                               leave_minutes=60, action="created"),),
    )
    card = build_attendance_confirmation_card("2026-08-22", plan)
    assert "source_note" not in card.to_dict()


# ---------------------------------------------------------------------------
# _tool_ctx.last_user_message 接线
# ---------------------------------------------------------------------------


def test_tool_ctx_last_user_message_defaults_empty() -> None:
    ctx = _tool_ctx(RuntimeBag())
    assert ctx.last_user_message == ""


def test_tool_ctx_last_user_message_from_state() -> None:
    state = {"messages": [HumanMessage(content="旧话"),
                          HumanMessage(content="把数量改成 5 个")]}
    ctx = _tool_ctx(RuntimeBag(), state=state)
    assert ctx.last_user_message == "把数量改成 5 个"


def test_tool_ctx_last_user_message_empty_when_no_human() -> None:
    ctx = _tool_ctx(RuntimeBag(), state={"messages": []})
    assert ctx.last_user_message == ""
