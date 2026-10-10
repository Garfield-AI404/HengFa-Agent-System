"""[2026-09-12 只读扩展] 新 7 实体明细裁剪白名单（tools/crop.py）。

旧行为：QUERY_ITEM_WHITELIST 未登记新实体 → crop_items 直接 return items
（明细不裁剪，notes/金额/联系电话等全量进上下文），且与规则包
R4.query.md 规则 1「缺省时返回全部字段（按业务白名单裁剪后返回）」不符。

本模块逐一实体断言：登记后明细只保留白名单业务字段，notes 与系统字段被
裁掉，且键序 = 白名单序（确定性输出）。

[2026-09-13 R1.salary 取数] employees 白名单由 ("id","name") 扩为放行
employee_no/position/daily_wage/work_hours_per_day —— 相关用例一并纳入本模块。
"""

from __future__ import annotations

import pytest

from administrative_agent.agent.tools.crop import (
    QUERY_ITEM_WHITELIST,
    crop_items,
)

#: 实体 → (ERP 行样例, 期望裁剪后键序)
_CASES: dict[str, tuple[dict, tuple[str, ...]]] = {
    # [2026-09-13 R1.salary 取数] employees 由 ("id","name") 扩为放行工号/岗位/
    # 日工资/每日工时：R1.salary.calculation 第 2 条现为「日薪/每日工作时长由
    # erp_salary_calc 内部读取」，这些字段仍须在裁剪白名单放行，否则工具取数
    # 拿不到（旧白名单在裁剪阶段把 daily_wage 裁掉，财报「工资账款」算不出，
    # 生产实测一次财报打了 31 次 ERP 调用反复重试）。phone/
    # emergency_phone 仍排除（个人联系方式不进上下文）。
    "employees": (
        {
            "id": 7, "name": "张三", "employee_no": "10086", "position": "喷粉工",
            "daily_wage": "260.00", "work_hours_per_day": "8.00",
            "phone": "13800000000", "emergency_phone": "13900000000",
            "is_active": True, "notes": "n",
            "created_at": "x", "updated_at": "y", "created_by": 1, "updated_by": 2,
        },
        # [2026-09-13 is_active 放行] 在职状态追加到白名单末尾（键序即裁剪后
        # 字段序）：参数轮字段目录（EntityCache 元数据）本就含 is_active，
        # 旧白名单在裁剪阶段静默丢弃致模型看得到、拿不到。
        ("id", "name", "employee_no", "position", "daily_wage",
         "work_hours_per_day", "is_active"),
    ),
    "customer_contacts": (
        {
            "id": 1, "customer_id": 9, "contact_name": "张三",
            "contact_phone": "13800000000", "is_primary": True,
            "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-02T00:00:00",
        },
        ("id", "customer_id", "contact_name", "contact_phone", "is_primary"),
    ),
    "attendance": (
        {
            "id": 1, "employee_id": 7, "att_date": "2026-09-01",
            "leave_minutes": 60, "overtime_minutes": 90,
            "created_at": "x", "updated_at": "y",
        },
        ("id", "employee_id", "att_date", "leave_minutes", "overtime_minutes"),
    ),
    "ar_payments": (
        {
            "id": 1, "customer_id": 9, "amount": "100.00",
            "pay_date": "2026-09-01", "notes": "备注不应保留",
            "created_at": "x",
        },
        ("id", "customer_id", "amount", "pay_date"),
    ),
    "ap_payments": (
        {
            "id": 1, "supplier_id": 3, "amount": "100.00",
            "pay_date": "2026-09-01", "notes": "n", "created_at": "x",
        },
        ("id", "supplier_id", "amount", "pay_date"),
    ),
    "salary_payments": (
        {
            "id": 1, "employee_id": 7, "year": 2025, "month": 9,
            "amount": "4052.00", "paid_at": "2026-09-01T00:00:00",
            "notes": "n", "created_at": "x",
        },
        ("id", "employee_id", "year", "month", "amount", "paid_at"),
    ),
    "powder_inventory": (
        {
            "id": 1, "color_code": "9010", "warn_threshold": "10.0000",
            "notes": "n", "created_by": 1, "updated_by": 2,
            "created_at": "x", "updated_at": "y",
            "order_total": "100.0000", "consumed_total": "30.0000",
            "stock": "70.0000",
        },
        ("id", "color_code", "warn_threshold",
         "order_total", "consumed_total", "stock"),
    ),
    "powder_consumption": (
        {
            "id": 1, "inventory_id": 5, "color_code": "9010",
            "consume_date": "2026-09-01", "quantity": "12.5000",
            "notes": "n", "created_by": 1, "updated_by": 2,
            "created_at": "x", "updated_at": "y",
        },
        ("id", "inventory_id", "color_code", "consume_date", "quantity"),
    ),
    # [2026-09-16 当期应收口径开放] ar_settlements（应收平账/抹零，只读）：
    # 只保留 id/customer_id/settled_amount/received_amount/settle_date；notes、
    # 三个 *_before 快照列、payment_id 与系统字段一律裁掉（精简上下文）。
    "ar_settlements": (
        {
            "id": 1, "customer_id": 9, "outstanding_before": "500.00",
            "total_amount_before": "1000.00", "paid_amount_before": "500.00",
            "received_amount": "0.00", "settled_amount": "500.00",
            "payment_id": 3, "settle_date": "2026-09-01", "notes": "n",
            "created_by": 1, "created_at": "x",
        },
        ("id", "customer_id", "settled_amount", "received_amount", "settle_date"),
    ),
}


@pytest.mark.parametrize("entity", sorted(_CASES))
def test_new_entity_items_are_cropped(entity: str) -> None:
    row, expected_keys = _CASES[entity]
    out = crop_items([dict(row)], entity)
    # 键序 = 白名单序（确定性输出）
    assert tuple(out[0].keys()) == expected_keys
    assert tuple(QUERY_ITEM_WHITELIST[entity]) == expected_keys
    # 裁剪：notes 与审计/系统列绝不保留
    assert "notes" not in out[0]
    for system_field in ("created_at", "updated_at", "created_by", "updated_by"):
        assert system_field not in out[0]
    # 只保留期望字段（其余键丢弃）
    assert set(out[0]) == set(expected_keys)


def test_employees_salary_fields_kept_contacts_dropped() -> None:
    """[2026-09-13 R1.salary 取数] employees 明细：daily_wage/work_hours_per_day
    不被裁剪（R1.salary.calculation 第 2 条工资账款取数依赖）；phone/
    emergency_phone 仍被裁剪（个人联系方式不进上下文）。

    [2026-09-13 is_active 放行] is_active（在职状态）现随白名单放行、值被
    保留——旧断言 ``"is_active" not in out[0]``（钉住旧裁剪行为）已反转。"""
    row = {
        "id": 7, "name": "张三", "employee_no": "10086", "position": "喷粉工",
        "daily_wage": "260.00", "work_hours_per_day": "8.00",
        "phone": "13800000000", "emergency_phone": "13900000000",
        "is_active": True,
    }
    out = crop_items([dict(row)], "employees")
    assert out[0]["daily_wage"] == "260.00"
    assert out[0]["work_hours_per_day"] == "8.00"
    assert out[0]["employee_no"] == "10086"
    assert out[0]["position"] == "喷粉工"
    # [2026-09-13 is_active 放行] 在职状态字段现保留，且值原样透传
    assert "is_active" in out[0]
    assert out[0]["is_active"] is True
    # 个人联系方式仍排除（不进上下文）
    assert "phone" not in out[0]
    assert "emergency_phone" not in out[0]


def test_unregistered_entity_still_returns_items_unchanged() -> None:
    """未登记实体保持原样返回（保守，不误删）——回归防护。"""
    rows = [{"id": 1, "foo": "bar", "notes": "kept"}]
    out = crop_items(rows, "file_attachments")
    assert out == rows
