"""D20 写入计划生成测试（6.4 步骤 10 L837、7.4 复核对象、RV-30）。

覆盖：D19 差异 → 写入计划条目映射（只取 write_entry 非 None）、写路径
与字段值（前后值）、create 条目目标 id=None、未知写路径 fail closed、
payload 规范化（键序稳定、hash 确定、不含 generated_at）、幂等键预留。
纯函数层：无网络/无数据库/无写入口。
"""

from __future__ import annotations

from administrative_agent.business.matching import MatchOutcome
from administrative_agent.business.reconciliation_plan import (
    WRITE_PATHS,
    WritePlan,
    build_write_plan,
)
from administrative_agent.business.reconciliation_rules import (
    AMBIGUOUS,
    ERP_MISSING,
    FIELD_DIFF,
    MATCHED,
    WRITE_CREATE_ORDER_BUNDLE,
    WRITE_UPDATE_ORDER,
    DiffEntry,
    FieldDiff,
    MatchKey,
)
from administrative_agent.domains.reconciliation import ReconciliationReport

HASH = "sha256-demo-file"
VERSION = 3
ENTITY = "metal_orders"


def entry(
    classification=MATCHED,
    *,
    row_number: int = 5,
    matched_order_id: int | None = 11,
    write_entry: str | None = None,
    diffs=(),
) -> DiffEntry:
    return DiffEntry(
        classification=classification,
        row_number=row_number,
        source_file_hash=HASH,
        mapping_version=VERSION,
        match_key=MatchKey(entity=ENTITY, subject_id=101, product_ref="M-1",
                           in_date="2026-06-02"),
        matched_order_id=matched_order_id,
        diffs=tuple(
            FieldDiff(field=f, excel_value=x, erp_value=e) for f, x, e in diffs
        ),
        write_entry=write_entry,
    )


def report(entries) -> ReconciliationReport:
    return ReconciliationReport(
        task_id="task-test-0001",
        template_key="recon-metal",
        mapping_version=VERSION,
        source_file_hash=HASH,
        entity=ENTITY,
        subject_match=MatchOutcome(ref="示例五金", status="matched",
                                   entity_id=101, matched_name="示例五金",
                                   candidates=((101, "示例五金"),)),
        period_note="2026 年 6 月",
        row_count=len(entries),
        conversion_errors=(),
        entries=tuple(entries),
        data_completeness="complete",
    )


# ---- 只取 write_entry 非 None 的条目 ----


def test_build_write_plan_only_takes_marked_entries():
    """matched/ambiguous/无标记 diff 不进计划；update/create 标记进计划。"""
    entries = [
        entry(MATCHED, row_number=1),                                   # 无标记
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "4.00", "3.00"),)),
        entry(ERP_MISSING, row_number=3, matched_order_id=None,
              write_entry=WRITE_CREATE_ORDER_BUNDLE),
        entry(AMBIGUOUS, row_number=4, matched_order_id=None),          # 无标记
        entry(FIELD_DIFF, row_number=5, write_entry=None),              # 非零旧值不标记
    ]
    plan = build_write_plan(report(entries))
    assert plan.count == 2
    assert [i.row_number for i in plan.items] == [2, 3]
    assert {i.write_path for i in plan.items} == {
        WRITE_UPDATE_ORDER, WRITE_CREATE_ORDER_BUNDLE
    }


def test_update_item_carries_fields_and_before_after_values():
    """update 条目：目标记录 id、写路径、写入字段值（前后值）、
    幂等键预留 None。"""
    entries = [
        entry(FIELD_DIFF, write_entry=WRITE_UPDATE_ORDER, diffs=(
            ("weight_per_unit", "4.00", "3.00"),
            ("quantity", "10", "8"),
        )),
    ]
    plan = build_write_plan(report(entries))
    item = plan.items[0]
    assert item.matched_order_id == 11
    assert item.write_path == WRITE_UPDATE_ORDER
    assert item.fields == (("weight_per_unit", "4.00"), ("quantity", "10"))
    assert item.diffs[0] == {"field": "weight_per_unit", "excel_value": "4.00",
                             "erp_value": "3.00", "kind": "regular"}
    assert item.idempotency_key is None  # D21 执行前生成（预留）


def test_create_item_has_no_target_and_no_fields():
    """create 条目：目标记录 id=None、无逐字段值（D21 从标准行取）。"""
    entries = [entry(ERP_MISSING, row_number=9, matched_order_id=None,
                     write_entry=WRITE_CREATE_ORDER_BUNDLE)]
    plan = build_write_plan(report(entries))
    item = plan.items[0]
    assert item.matched_order_id is None
    assert item.write_path == WRITE_CREATE_ORDER_BUNDLE
    assert item.fields == ()
    assert item.diffs == ()


def test_unknown_write_path_fails_closed():
    """未知写路径拒绝（防御：计划只接受两个已知写路径）。"""
    entries = [entry(FIELD_DIFF, write_entry="create_customer")]
    try:
        build_write_plan(report(entries))
    except ValueError as exc:
        assert "未知写路径" in str(exc)
    else:
        raise AssertionError("unknown write path must fail closed")


def test_empty_report_gives_empty_plan():
    plan = build_write_plan(report([]))
    assert plan.count == 0
    assert plan.items == ()


# ---- payload 规范化与 hash（7.4 reviewed_payload_hash） ----


def _two_item_plan() -> WritePlan:
    entries = [
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "4.00", "3.00"),)),
        entry(ERP_MISSING, row_number=3, matched_order_id=None,
              write_entry=WRITE_CREATE_ORDER_BUNDLE),
    ]
    return build_write_plan(report(entries))


def test_payload_hash_stable_and_content_driven():
    """同一计划两次构造 hash 一致（可复现）；generated_at 不影响 hash；
    计划变化（字段值）hash 变化——payload hash 变化即复核失效依据。"""
    plan_a, plan_b = _two_item_plan(), _two_item_plan()
    assert plan_a.payload_hash == plan_b.payload_hash
    assert plan_a.payload_hash == plan_a.payload_hash
    assert "generated_at" not in plan_a.to_payload()  # 时间戳不入 hash

    # 计划变化：重量 4.00 → 5.00 → hash 变化（7.4/14.1 L1380 场景）
    changed = report([
        entry(FIELD_DIFF, row_number=2, write_entry=WRITE_UPDATE_ORDER,
              diffs=(("weight_per_unit", "5.00", "3.00"),)),
        entry(ERP_MISSING, row_number=3, matched_order_id=None,
              write_entry=WRITE_CREATE_ORDER_BUNDLE),
    ])
    assert build_write_plan(changed).payload_hash != plan_a.payload_hash


def test_payload_canonical_structure():
    """payload 键序固定（task_id/template_key/mapping_version/
    source_file_hash/entity/items + 条目键序）。"""
    payload = _two_item_plan().to_payload()
    assert tuple(payload) == (
        "task_id", "template_key", "mapping_version", "source_file_hash",
        "entity", "items",
    )
    assert payload["task_id"] == "task-test-0001"
    assert payload["entity"] == ENTITY
    assert payload["mapping_version"] == VERSION
    item = payload["items"][0]
    assert tuple(item) == (
        "row_number", "classification", "matched_order_id", "write_path",
        "fields", "diffs", "idempotency_key",
    )


def test_write_paths_are_exactly_the_two_execution_paths():
    """6.4 步骤 12：缺失订单 create_order_bundle、已有订单 update_order。"""
    assert WRITE_PATHS == {WRITE_CREATE_ORDER_BUNDLE, WRITE_UPDATE_ORDER}
