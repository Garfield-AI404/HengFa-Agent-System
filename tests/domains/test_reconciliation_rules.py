"""D19 R6 匹配与差异分类纯规则测试（6.4 步骤 5-9、L843/L845、RV-29）。

RV-29 通过标准逐条：
- 唯一匹配：同键命中 1 个 ERP 订单 → matched（字段一致）或 field_diff；
- 缺失：无命中 → erp_missing（钣金标记 create_order_bundle，D21 场景 9）；
- 字段差异：含前后值（excel_value/erp_value）；数值规范化比较避免假差异
  （12 与 12.00 不算差异）；
- 匹配歧义：同键命中多个 → ambiguous，携带候选、绝不自动选一个；
- L843：原 weight_per_unit=0 钣金订单差异 → zero_weight_updatable +
  update_order 标记；非零旧值不一致仍列差异（regular）不静默覆盖；
- L845：塑粉价格/重量/色号差异必须标记 update_order；
- 客户/供货商名称无法唯一匹配 → customer_unmatched（报告表达不阻断）；
- 行缺匹配字段 → key_incomplete（不猜测匹配）。

纯函数层：无网络、无数据库、无写入口（fail closed）。
"""

from __future__ import annotations

from administrative_agent.business.matching import MatchOutcome
from administrative_agent.business.reconciliation_rules import (
    AMBIGUOUS,
    CUSTOMER_UNMATCHED,
    DIFF_KIND_REGULAR,
    DIFF_KIND_ZERO_WEIGHT_UPDATABLE,
    ERP_MISSING,
    FIELD_DIFF,
    KEY_INCOMPLETE,
    MATCHED,
    WRITE_CREATE_ORDER_BUNDLE,
    WRITE_UPDATE_ORDER,
    classify_excel_row,
    compare_excel_to_erp,
    norm_value,
)

HASH = "sha256-demo-file"
VERSION = 3


def outcome(customer_id: int = 101, status: str = "matched", ref: str = "示例五金",
            candidates: tuple[tuple[int, str], ...] = ()):
    if status == "matched":
        return MatchOutcome(ref=ref, status=status, entity_id=customer_id,
                            matched_name=ref, candidates=((customer_id, ref),))
    return MatchOutcome(ref=ref, status=status, candidates=candidates)


def classify(*, entity="metal_orders", values, orders=(), subject=None,
             compare=("weight_per_unit", "quantity", "unit_price", "status",
                      "total_amount"), row_number=5, **kwargs):
    return classify_excel_row(
        entity=entity,
        row_number=row_number,
        source_file_hash=HASH,
        mapping_version=VERSION,
        excel_values=values,
        subject_outcome=subject or outcome(),
        erp_orders=orders,
        compare_fields=compare,
        **kwargs,
    )


def metal_order(order_id=11, customer_id=101, model="M-1", in_date="2026-06-02",
                weight="3.00", quantity="10", price="12.50", status="完成",
                total="375.00"):
    return {
        "id": order_id, "customer_id": customer_id, "model": model,
        "in_date": in_date, "weight_per_unit": weight, "quantity": quantity,
        "unit_price": price, "status": status, "total_amount": total,
    }


# ---- 数值规范化比较（避免假差异） ----


def test_norm_value_ignores_trailing_zeros():
    assert norm_value("12.00") == norm_value("12")
    assert norm_value("12.50") == norm_value("12.5")
    assert norm_value("375.00") == norm_value("375")
    assert norm_value("完成") == "完成"  # 非数字原样


def test_decimal_format_difference_is_not_a_false_diff():
    """Excel 12.00 与 ERP 12 → 数值一致，不产生假差异。"""
    values = {"model": "M-1", "in_date": "2026-06-02",
              "unit_price": "12.00", "quantity": "10", "status": "完成"}
    entry = classify(values=values, orders=[metal_order(price="12")])
    assert entry.classification == MATCHED
    assert entry.diffs == ()


# ---- 唯一匹配（RV-29：唯一匹配） ----


def test_unique_match_no_diffs():
    values = {"model": "M-1", "in_date": "2026-06-02",
              "weight_per_unit": "3.00", "quantity": "10",
              "unit_price": "12.50", "status": "完成"}
    entry = classify(values=values, orders=[metal_order()])
    assert entry.classification == MATCHED
    assert entry.match_key is not None
    assert entry.match_key.subject_id == 101
    assert entry.match_key.product_ref == "M-1"
    assert entry.match_key.in_date == "2026-06-02"
    assert entry.matched_order_id == 11
    assert entry.write_entry is None
    assert entry.row_number == 5
    assert entry.source_file_hash == HASH
    assert entry.mapping_version == VERSION


# ---- 缺失（RV-29：ERP 缺失） ----


def test_erp_missing_metal_marks_create_order_bundle():
    values = {"model": "M-999", "in_date": "2026-06-30",
              "weight_per_unit": "2.00"}
    entry = classify(values=values, orders=[metal_order()])
    assert entry.classification == ERP_MISSING
    assert entry.matched_order_id is None
    assert entry.write_entry == WRITE_CREATE_ORDER_BUNDLE  # D21 场景 9
    assert "ERP 中无该匹配键订单" in entry.message


# ---- 匹配歧义（RV-29：歧义不自动选） ----


def test_ambiguous_never_auto_selects():
    """同键命中多个 ERP 订单 → ambiguous：候选全部列出、绝不自动选一个。"""
    orders = [
        metal_order(order_id=21),
        metal_order(order_id=22),
    ]
    values = {"model": "M-1", "in_date": "2026-06-02"}
    entry = classify(values=values, orders=orders)
    assert entry.classification == AMBIGUOUS
    assert entry.matched_order_id is None
    assert entry.write_entry is None
    assert "21" in entry.message and "22" in entry.message
    assert "不自动选择" in entry.message


# ---- 字段差异（RV-29：前后值） ----


def test_field_diff_carries_before_and_after_values():
    """字段差异含前后值（excel_value/erp_value）+ 定位信息。"""
    orders = [metal_order(weight="3.00")]
    values = {"model": "M-1", "in_date": "2026-06-02", "weight_per_unit": "4.00"}
    entry = classify(values=values, orders=orders)
    assert entry.classification == FIELD_DIFF
    assert entry.matched_order_id == 11
    assert len(entry.diffs) == 1
    diff = entry.diffs[0]
    assert diff.field == "weight_per_unit"
    assert diff.excel_value == "4.00" and diff.erp_value == "3.00"  # 前后值
    assert diff.kind == DIFF_KIND_REGULAR  # 非零旧值 → 不一致，不静默
    assert entry.write_entry is None  # 非零旧值不一致不标记静默更新


def test_nonzero_old_weight_diff_not_silent():
    """L843：非零旧重量不一致仍列差异（regular），不能静默覆盖。"""
    orders = [metal_order(weight="3.00")]
    values = {"model": "M-1", "in_date": "2026-06-02", "weight_per_unit": "3.50"}
    entry = classify(values=values, orders=orders)
    assert entry.classification == FIELD_DIFF
    assert entry.diffs[0].kind == DIFF_KIND_REGULAR
    assert not entry.diffs[0].updatable_without_review


def test_zero_old_weight_updatable_and_marked_update_order():
    """L843：原 weight_per_unit=0 的钣金订单 → zero_weight_updatable +
    update_order 标记（D21 执行路径，本单元只标记）。"""
    orders = [metal_order(weight="0")]
    values = {"model": "M-1", "in_date": "2026-06-02", "weight_per_unit": "5.00"}
    entry = classify(values=values, orders=orders)
    assert entry.classification == FIELD_DIFF
    assert entry.write_entry == WRITE_UPDATE_ORDER
    diff = entry.diffs[0]
    assert diff.kind == DIFF_KIND_ZERO_WEIGHT_UPDATABLE
    assert diff.updatable_without_review
    assert "可更新" in entry.message


def test_powder_diff_marks_update_order_for_price_weight_color():
    """L845：塑粉价格/重量/色号差异必须使用 update_order（不能通用 update）。"""
    powder_order = {
        "id": 31, "supplier_id": 5, "color_code": "RAL-9016",
        "in_date": "2026-06-10", "total_weight": "500",
        "price_per_kg": "28.00", "total_amount": "14000.00",
    }
    # 重量差异 → update_order
    values = {"color_code": "RAL-9016", "in_date": "2026-06-10",
              "total_weight": "550", "price_per_kg": "28.00"}
    entry = classify(entity="powder_orders", values=values, orders=[powder_order],
                     compare=("total_weight", "price_per_kg", "total_amount"),
                     subject=outcome(5, ref="江城塑粉厂"))
    assert entry.classification == FIELD_DIFF
    assert entry.write_entry == WRITE_UPDATE_ORDER
    assert "update_order" in entry.message
    # 价格差异 → update_order
    values2 = {"color_code": "RAL-9016", "in_date": "2026-06-10",
               "total_weight": "500", "price_per_kg": "30.00"}
    entry2 = classify(entity="powder_orders", values=values2, orders=[powder_order],
                      compare=("total_weight", "price_per_kg", "total_amount"),
                      subject=outcome(5, ref="江城塑粉厂"))
    assert entry2.write_entry == WRITE_UPDATE_ORDER
    # 金额差异（派生字段，Excel 不直接更新）→ 不标记 update_order
    values3 = {"color_code": "RAL-9016", "in_date": "2026-06-10",
               "total_weight": "500", "price_per_kg": "28.00",
               "total_amount": "14100.00"}
    entry3 = classify(entity="powder_orders", values=values3, orders=[powder_order],
                      compare=("total_weight", "price_per_kg", "total_amount"),
                      subject=outcome(5, ref="江城塑粉厂"))
    assert entry3.classification == FIELD_DIFF
    assert entry3.write_entry is None  # 非价格/重量/色号差异不标记


def test_powder_unique_match_no_diff():
    powder_order = {
        "id": 31, "supplier_id": 5, "color_code": "RAL-9016",
        "in_date": "2026-06-10", "total_weight": "500",
        "price_per_kg": "28.00", "total_amount": "14000.00",
    }
    values = {"color_code": "RAL-9016", "in_date": "2026-06-10",
              "total_weight": "500", "price_per_kg": "28.00"}
    entry = classify(entity="powder_orders", values=values, orders=[powder_order],
                     compare=("total_weight", "price_per_kg"),
                     subject=outcome(5, ref="江城塑粉厂"))
    assert entry.classification == MATCHED


def test_powder_erp_missing_no_write_marker():
    """塑粉缺失：本单元不定义创建路径（与钣金 create_order_bundle 区分），
    write_entry=None，D21 按复核决定。"""
    values = {"color_code": "RAL-9999", "in_date": "2026-06-30",
              "total_weight": "100"}
    entry = classify(entity="powder_orders", values=values, orders=[],
                     subject=outcome(5, ref="江城塑粉厂"))
    assert entry.classification == ERP_MISSING
    assert entry.write_entry is None


# ---- 主体匹配失败 / 键不完整（报告表达不阻断） ----


def test_customer_unmatched_expressed_not_blocking():
    """客户名称 ambiguous/not_found → customer_unmatched：报告表达、不阻断，
    不执行任何写入（歧义不自动选，D13 语义沿用到 R6）。"""
    entry = classify(values={"model": "M-1", "in_date": "2026-06-02"},
                     orders=[metal_order()],
                     subject=outcome(status="not_found"))
    assert entry.classification == CUSTOMER_UNMATCHED
    assert entry.match_key is None
    assert entry.write_entry is None
    assert "不执行任何写入" in entry.message

    amb = outcome(status="ambiguous", ref="同名客户",
                  candidates=((201, "同名客户"), (202, "同名客户")))
    entry2 = classify(values={"model": "M-1", "in_date": "2026-06-02"},
                      orders=[metal_order()], subject=amb)
    assert entry2.classification == CUSTOMER_UNMATCHED
    assert "候选" in entry2.message


def test_key_incomplete_no_guess():
    """行缺匹配字段（如 in_date）→ key_incomplete，不猜测匹配。"""
    entry = classify(values={"model": "M-1"}, orders=[metal_order()])
    assert entry.classification == KEY_INCOMPLETE
    assert entry.match_key is None
    assert entry.write_entry is None


# ---- 比较纯函数 ----


def test_compare_excel_to_erp_skips_fields_absent_in_excel():
    diffs = compare_excel_to_erp(
        entity="metal_orders",
        excel_values={"weight_per_unit": "3.00"},
        erp_values=metal_order(),
        compare_fields=("weight_per_unit", "quantity", "unit_price"),
    )
    assert diffs == ()  # 未出现的字段不参与比较
