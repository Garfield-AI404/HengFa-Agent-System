"""ERP 字段目录（规格 2.6 domains/erp_field_catalog.py）。

供 agent 建议「Excel 列 → ERP 字段」映射时引用（对账/补建载荷字段来源）；
字段名与 mcp-api.md §6.1（create_order_bundle 输入）/§7.1
（update_order 可修改字段白名单）一致——两份契约字段集一致，本目录
并集取业务字段；系统字段与派生金额（id/created_at/created_by/
updated_by/updated_at/deleted_at/amount/total_amount，mcp-api.md §5/
§6.1/§7.1）禁止传入，不列入目录。
"""

from __future__ import annotations

#: 实体 → 字段目录（字段名 → (中文名, 必填与否, 类型说明)）
#:
#: 必填口径来自 mcp-api.md §6.1 创建契约：显式「必填」或「必须 …」
#: （如 quantity 必须大于 0 且无默认值）→ True；「默认 …」或可 null → False。
#: 类型说明引用 §6.1/§7.1 字段规则（枚举/数值精度/长度上限）。
ERP_FIELD_CATALOG: dict[str, dict[str, tuple[str, bool, str]]] = {
    "metal_orders": {
        "customer_id": ("客户 ID", True, "int；客户存在且未软删除（§6.1 必填）"),
        "model": ("型号", True, "str；去除首尾空格后非空，≤100 字符（§6.1 必填）"),
        "unit_price": ("单价", False, "decimal；≥0，≤4 位小数，默认 0（§6.1）"),
        "price_type": ("价格类型", False, "enum：含税/不含税（§6.1/§7.1）"),
        "status": ("状态", False, "enum：未完成/完成；创建时只能为未完成（§6.1/§7.1）"),
        "weight_per_unit": ("单件重量", False, "decimal；≥0，≤4 位小数，默认 0（0=待补充，§6.1）"),
        "weight_unit": ("重量单位", False, "enum：kg/m²（§6.1/§7.1）"),
        "quantity": ("数量", True, "decimal；必须大于 0，≤2 位小数（§6.1）"),
        "quantity_unit": ("数量单位", False, "enum：套/个（§6.1/§7.1）"),
        # [2026-09-08 ERP 上线] 附加款：可写业务字段（非派生金额——派生的是
        # 行总额 total_amount，仍禁传）；仅钣金，塑粉订单无此字段
        "surcharge_amount": (
            "附加款", False,
            "decimal(10,2)；≥0，≤2 位小数，默认 0.00；仅钣金，"
            "塑粉无此字段（§6.1/§7.1；可省略）",
        ),
        "in_date": ("入库日期", False, "date YYYY-MM-DD；创建可 null，更新不可为 null（§6.1/§7.1）"),
        "out_date": ("出库日期", False, "date YYYY-MM-DD 或 null（§6.1/§7.1）"),
        "notes": ("备注", False, "str 或 null（§6.1/§7.1）"),
    },
    "powder_orders": {
        "supplier_id": ("供货商 ID", True, "int；供货商存在且未软删除（§6.1 必填）"),
        "color_code": ("色号", True, "str；去除首尾空格后非空，≤50 字符（§6.1 必填）"),
        "total_weight": ("总重量", False, "decimal；≥0，≤4 位小数，默认 0（0=待补充，§6.1）"),
        "price_per_kg": ("每公斤价格", False, "decimal；≥0，≤4 位小数，默认 0（0=待补充，§6.1）"),
        "in_date": ("入库日期", False, "date YYYY-MM-DD；创建可 null，更新不可为 null（§6.1/§7.1）"),
        "notes": ("备注", False, "str 或 null（§6.1/§7.1）"),
    },
}

#: 目录覆盖的实体（与 domains/reconciliation.RECONCILE_ENTITIES 一致）
CATALOG_ENTITIES: frozenset[str] = frozenset(ERP_FIELD_CATALOG)
