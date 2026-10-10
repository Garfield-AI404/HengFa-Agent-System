"""[2026-09-12 一张卡多条明细 阶段 3/5] 收支批量卡参数/建卡/哈希单元测试。

覆盖：
- 参数模型：扁平简写 ↔ items 二选一（同时非空 fail closed）、上限、
  更新同卡 id 重复拒绝、金额/日期/fk 校验；
- 单条逐字节零回归：prepare 扁平路径的 execution 键完全等于改造前，卡片
  等于直接调旧构造函数；
- 批量卡：同内容两行外层键唯一且都进哈希、item_id 稳定且重复加 #n；
- 更新批量卡：party_display 不入哈希（执行期重查名称退化不误拒）、
  无差异条目 action=unchanged。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from administrative_agent.agent.tools.base import ToolContext
from administrative_agent.agent.tools.finance import (
    FinanceCreateArgs,
    FinanceCreateItemArgs,
    FinanceUpdateArgs,
    FinanceUpdateItemArgs,
    _create_payload,
    build_finance_create_confirmation_card,
    prepare_finance_create,
    prepare_finance_update,
)
from administrative_agent.domains.finance import (
    _TRANSACTION_QUERY_FIELDS,
    _payload_signature_key,
    build_finance_create_batch_confirmation_card,
    build_finance_update_batch_confirmation_card,
)
from administrative_agent.domains.orders import is_confirmation_valid

CUSTOMER_ID = 16
CUSTOMER_NAME = "昌盛"


class _FakeErp:
    """prepare 只读 fake：按 entity 返回预置行；等值 filter + fields 裁剪。"""

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def call_tool(self, name: str, args: dict):
        assert name == "query"
        entity = args["entity"]
        rows = self._rows.get(entity, [])
        filt = args.get("filter") or {}
        items = [dict(r) for r in rows if all(r.get(k) == v for k, v in filt.items())]
        fields = args.get("fields")
        if fields:
            items = [{k: it.get(k) for k in fields} for it in items]

        class _Result:
            ok = True
            data: dict[str, Any] = {"items": items}

        return _Result()


def _ctx(erp: _FakeErp) -> ToolContext:
    return ToolContext(client=erp, tasks=object(), audit=object())


_CATEGORIES = {"transaction_categories": [
    {"id": 1, "tx_type": "支出", "category_name": "运费"},
    {"id": 2, "tx_type": "支出", "category_name": "房租"},
    {"id": 3, "tx_type": "收入", "category_name": "回款"},
]}


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------


def test_create_flat_fields_normalize_to_single_item() -> None:
    args = FinanceCreateArgs(
        tx_type="支出", category="运费", amount="100", tx_date="2026-09-10",
    )
    items = args.effective_items()
    assert len(items) == 1
    assert items[0].amount == "100.00"
    assert items[0].tx_type == "支出"


def test_create_items_normalize_to_many() -> None:
    args = FinanceCreateArgs(items=[
        {"tx_type": "支出", "category": "运费", "amount": 100, "tx_date": "2026-09-10"},
        {"tx_type": "收入", "category": "回款", "amount": "200", "tx_date": "2026-09-11"},
    ])
    assert [it.amount for it in args.effective_items()] == ["100.00", "200.00"]


def test_create_items_and_flat_conflict_fail_closed() -> None:
    with pytest.raises(ValidationError):
        FinanceCreateArgs(
            tx_type="支出", category="运费", amount="100", tx_date="2026-09-10",
            items=[{"tx_type": "支出", "category": "运费", "amount": 1, "tx_date": "2026-09-10"}],
        )


def test_create_missing_flat_field_fail_closed() -> None:
    with pytest.raises(ValidationError):
        FinanceCreateArgs(tx_type="支出", category="运费", amount="100")


def test_create_items_max_length() -> None:
    from administrative_agent.agent.tools.base import MAX_BATCH_ITEMS
    too_many = [
        {"tx_type": "支出", "category": "运费", "amount": 1, "tx_date": "2026-09-10"}
        for _ in range(MAX_BATCH_ITEMS + 1)
    ]
    with pytest.raises(ValidationError):
        FinanceCreateArgs(items=too_many)


def test_update_duplicate_id_in_card_rejected() -> None:
    with pytest.raises(ValidationError):
        FinanceUpdateArgs(items=[
            {"id": 5, "amount": "150"},
            {"id": 5, "project": "X"},
        ])


def test_update_flat_normalizes_to_single_item() -> None:
    args = FinanceUpdateArgs(id=5, amount="150")
    items = args.effective_items()
    assert len(items) == 1
    assert items[0].id == 5
    assert items[0].amount == "150.00"


def test_update_requires_id_or_items() -> None:
    with pytest.raises(ValidationError):
        FinanceUpdateArgs(amount="150")


# ---------------------------------------------------------------------------
# 单条逐字节零回归（prepare 委托旧路径）
# ---------------------------------------------------------------------------


def test_single_create_prepare_byte_identical_execution() -> None:
    """扁平单条：execution 键完全等于改造前（无 items/batch_size）。"""
    erp = _FakeErp({**_CATEGORIES, "customers": [
        {"id": CUSTOMER_ID, "company_name": CUSTOMER_NAME},
    ]})
    prep = prepare_finance_create(_ctx(erp), FinanceCreateArgs(
        tx_type="支出", category="运费", amount="100",
        tx_date="2026-09-10", customer_id=CUSTOMER_ID,
    ))
    assert set(prep.execution.keys()) == {
        "tool", "tx_type", "category", "tx_date", "payload", "policy_key",
    }
    payload = {
        "tx_type": "支出", "category": "运费", "amount": "100.00",
        "tx_date": "2026-09-10", "customer_id": CUSTOMER_ID,
    }
    assert prep.execution["payload"] == payload
    # 卡片等于直接调旧构造函数（未改动的单条建卡函数）→ 逐字节等价
    old_card = build_finance_create_confirmation_card(
        payload, party_display=(("customer_name", CUSTOMER_NAME),),
    )
    assert prep.card == old_card.to_dict()
    assert prep.payload_hash == old_card.payload_hash


def test_single_update_prepare_byte_identical_execution() -> None:
    erp = _FakeErp({"transactions": [{
        "id": 5, "tx_type": "支出", "category": "运费", "amount": "100.00",
        "tx_date": "2026-09-01", "project": None, "notes": None,
        "customer_id": None, "supplier_id": None, "updated_at": "T0",
    }]})
    prep = prepare_finance_update(_ctx(erp), FinanceUpdateArgs(id=5, amount="150"))
    assert set(prep.execution.keys()) == {
        "tool", "id", "desired", "expected_updated_at", "policy_key",
    }
    assert prep.execution["id"] == 5


# ---------------------------------------------------------------------------
# 批量卡：外层键唯一 / 都进哈希
# ---------------------------------------------------------------------------


def test_create_batch_card_unique_rows_and_both_hashed() -> None:
    """同内容两行：展示外层键唯一（不折叠丢行），hash_fields 两条都在。"""
    payload = {"tx_type": "支出", "category": "运费", "amount": "100.00",
               "tx_date": "2026-09-10"}
    card = build_finance_create_batch_confirmation_card([
        ("支出·运费（2026-09-10）", dict(payload), ()),
        ("支出·运费（2026-09-10）", dict(payload), ()),
    ])
    rows = dict(card.fields_to_write)
    assert list(rows) == [
        "1. 支出·运费（2026-09-10）", "2. 支出·运费（2026-09-10）",
    ]
    hash_targets = dict(card.hash_fields)
    assert set(hash_targets) == {"detail-1", "detail-2"}
    assert len(hash_targets) == 2  # 两条都进哈希（序号键不冲突）
    assert card.row_header_label == "明细"
    assert card.row_quantifier == "条"


def test_create_batch_item_ids_unique_for_duplicate_content() -> None:
    """同内容两行 → item_id 稳定唯一（#1/#2）；单条扁平与批量同内容同 id。"""
    erp = _FakeErp(_CATEGORIES)
    prep = prepare_finance_create(_ctx(erp), FinanceCreateArgs(items=[
        {"tx_type": "支出", "category": "运费", "amount": "100", "tx_date": "2026-09-10"},
        {"tx_type": "支出", "category": "运费", "amount": "100", "tx_date": "2026-09-10"},
    ]))
    ids = [it["item_id"] for it in prep.execution["items"]]
    assert ids == [
        "支出:运费:2026-09-10:100.00#1",
        "支出:运费:2026-09-10:100.00#2",
    ]
    # 同内容两次 prepare 产出同一序列（恢复执行不重复写）
    prep2 = prepare_finance_create(_ctx(erp), FinanceCreateArgs(items=[
        {"tx_type": "支出", "category": "运费", "amount": "100", "tx_date": "2026-09-10"},
        {"tx_type": "支出", "category": "运费", "amount": "100", "tx_date": "2026-09-10"},
    ]))
    assert [it["item_id"] for it in prep2.execution["items"]] == ids


def test_create_batch_prepare_aggregates_all_failures() -> None:
    """预检失败：聚合列出全部失败条目（整卡不挂）。"""
    erp = _FakeErp({**_CATEGORIES, "customers": []})
    with pytest.raises(Exception) as excinfo:
        prepare_finance_create(_ctx(erp), FinanceCreateArgs(items=[
            {"tx_type": "支出", "category": "不存在分类", "amount": "1", "tx_date": "2026-09-10"},
            {"tx_type": "支出", "category": "运费", "amount": "2", "tx_date": "2026-09-10",
             "customer_id": 999},
        ]))
    msg = str(excinfo.value)
    assert "第 1 条" in msg and "第 2 条" in msg
    assert "不存在分类" in msg and "999" in msg


# ---------------------------------------------------------------------------
# 更新批量卡：party_display 不入哈希；无差异 unchanged
# ---------------------------------------------------------------------------


def _update_entry(record_id=5, *, changes, party_display=()):
    return {
        "record_id": record_id,
        "before": {"tx_date": "2026-09-01", "category": "运费"},
        "changes": changes,
        "expected_updated_at": "T0",
        "party_display": party_display,
    }


def test_update_batch_hash_ignores_party_display_fluctuation() -> None:
    """关联方名称 prepare 期「昌盛」、执行期退化为占位 → 整卡哈希不变。"""
    card_ok = build_finance_update_batch_confirmation_card([
        _update_entry(changes={"customer_id": CUSTOMER_ID},
                      party_display=(("customer_name", CUSTOMER_NAME),)),
    ])
    card_deg = build_finance_update_batch_confirmation_card([
        _update_entry(changes={"customer_id": CUSTOMER_ID},
                      party_display=(("customer_name", f"客户ID {CUSTOMER_ID}"),)),
    ])
    assert card_ok.payload_hash == card_deg.payload_hash
    assert is_confirmation_valid(card_deg, card_ok.payload_hash) is True
    row = dict(card_deg.fields_to_write)["1. 2026-09-01 运费"]
    assert "关联客户" in row["字段和值"]


def test_update_batch_unchanged_action_for_no_diff() -> None:
    card = build_finance_update_batch_confirmation_card([
        _update_entry(record_id=5, changes={"amount": "150.00"}),
        _update_entry(record_id=6, changes={}),
    ])
    rows = dict(card.fields_to_write)
    assert list(rows) == ["1. 2026-09-01 运费", "2. 2026-09-01 运费"]
    assert rows["1. 2026-09-01 运费"]["操作"] == "更新"
    assert rows["2. 2026-09-01 运费"]["操作"] == "保持"


def test_update_batch_hash_binds_each_record_identity() -> None:
    card_a = build_finance_update_batch_confirmation_card([
        _update_entry(record_id=111, changes={"amount": "1.00"}),
        _update_entry(record_id=112, changes={"amount": "1.00"}),
    ])
    card_b = build_finance_update_batch_confirmation_card([
        _update_entry(record_id=111, changes={"amount": "1.00"}),
        _update_entry(record_id=113, changes={"amount": "1.00"}),
    ])
    assert card_a.payload_hash != card_b.payload_hash


# ---------------------------------------------------------------------------
# [2026-09-12 加固] 隐式契约守护：args 可更新字段 ⊆ 执行器写前查询字段集
# ---------------------------------------------------------------------------


def test_update_item_args_fields_covered_by_query_fields() -> None:
    """FinanceUpdateItemArgs 的可更新字段必须都在执行器写前查询字段集内。

    隐式契约（两条路径重建同一张确认卡哈希）：
    - prepare 侧用 ``_query_all``（**不带 fields**，ERP 返回全字段）取快照；
    - 执行器侧 ``_query_current`` 用显式白名单 ``_TRANSACTION_QUERY_FIELDS`` 取快照；
    - 两者都喂给 ``build_update_changes`` 逐字段比较现值——若某个 args 可更新
      字段不在白名单里，执行器读到 ``None``、prepare 读到真实值 → 两侧 changes
      不同 → 整卡哈希永远对不上 → **批量/单条更新退化为「确认永远失效」，功能
      完全不可用**。

    口径：可更新字段 = ``FinanceUpdateItemArgs`` 的全部字段去掉 ``id``——``id``
    是目标记录主键（只用于定位与哈希身份绑定，不是「期望变化字段」，故排除）。
    """
    updatable = set(FinanceUpdateItemArgs.model_fields) - {"id"}
    missing = sorted(updatable - set(_TRANSACTION_QUERY_FIELDS))
    assert not missing, (
        f"FinanceUpdateItemArgs 可更新字段 {missing} 不在 _TRANSACTION_QUERY_FIELDS"
        f"（{sorted(_TRANSACTION_QUERY_FIELDS)}）内。后果：执行器写前重查取不到该"
        "字段现值（读到 None），与 prepare 全字段快照算出的 changes 不一致 → 确认卡"
        "哈希永远对不上 → 更新功能不可用。请在 _TRANSACTION_QUERY_FIELDS 补上该字段。"
    )


def test_create_payload_keys_covered_by_query_fields() -> None:
    """``_create_payload`` 产出的键必须都在执行器写前预查字段集内（与 update 侧对称）。

    写前签名预查 ``_verify_applied``/``_signature_matches`` 对载荷**每个键**调
    ``row.get(key)`` 做等值比较。若 ``_create_payload`` 新增一个可选字段却不进
    ``_TRANSACTION_QUERY_FIELDS``，查询回读的行里该键恒为 ``None`` →
    ``_signature_matches`` 恒不匹配 → ``matches`` 恒为 0 → **写前预查静默失效**
    （恢复期「防重复写入」的兜底被削弱，且无任何报警）。故用本测试钉死。

    口径：取全部可能键——必填四项 + 两个 fk 变体（customer_id/supplier_id
    互斥，须分两次构造才能覆盖两者）+ project/notes。
    """
    base = {"tx_type": "支出", "category": "运费", "amount": "100",
            "tx_date": "2026-09-10", "project": "p", "notes": "n"}
    keys: set[str] = set()
    for fk in ({"customer_id": 1}, {"supplier_id": 2}):
        keys |= set(_create_payload(FinanceCreateItemArgs(**base, **fk)))
    missing = sorted(keys - set(_TRANSACTION_QUERY_FIELDS))
    assert not missing, (
        f"_create_payload 产出的字段 {missing} 不在 _TRANSACTION_QUERY_FIELDS"
        f"（{sorted(_TRANSACTION_QUERY_FIELDS)}）内。后果：写前签名预查回读的行里"
        "该键恒为 None → _signature_matches 恒不匹配 → 预查静默失效（恢复期防重复"
        "写入兜底被削弱）。请在 _TRANSACTION_QUERY_FIELDS 补上该字段。"
    )


def test_payload_signature_key_normalizes_amount_numerically() -> None:
    """分组键对金额按**数值**归组：「100」与「100.00」必须落同一分组。

    与 ``_amount_equals``（预查的金额比较口径）同语义；否则同金额不同写法的
    两条明细会被拆成两分组、各自出现次序为 1，使「按出现次序对齐」的防重复
    写入语义错位。

    说明：经 ``FinanceCreateItemArgs`` 上游 ``normalize_amount`` 后载荷金额恒为
    "X.YY"，故「同一批混入 100 与 100.00」在 args 入口不可达；本用例直接对
    分组函数断言以证明归组不分裂。
    """
    base = {"tx_type": "支出", "category": "运费", "tx_date": "2026-09-10"}
    assert _payload_signature_key({**base, "amount": "100"}) == _payload_signature_key(
        {**base, "amount": "100.00"}
    )
    # 不同金额不得归为同组（防过度归并）
    assert _payload_signature_key({**base, "amount": "100.00"}) != _payload_signature_key(
        {**base, "amount": "100.01"}
    )
