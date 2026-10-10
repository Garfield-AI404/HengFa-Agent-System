"""[2026-09-12 只读扩展] 导出侧：str→Decimal 归一 + 新实体合计列 + 中文表头。

覆盖三处复核收口：

- 任务 1：ERP MCP 把 Decimal 序列化成**字符串**，旧 `_is_number` 只认
  int/float → 金额列永不判为数值列、总计行永不出金额合计。现按「规范
  数字字符串」归一为 Decimal（`_is_number` / `_to_decimal`），合计走
  Decimal 语义（不用 float 累积）。本文件穷举边界串并断言 transactions
  （既有登记 sum_cols={"amount"}）金额合计恢复。
- 任务 2：新 7 实体登记 EXPORT_SUBTOTAL_COLUMNS，排掉 id/year/month 等
  误求和（salary_payments 的 year/month 不得进合计）。
- 任务 4：9 个新实体导出表头为中文（照抄 ERP registry 标签）；
  [2026-09-16] 追加 ar_settlements（应收平账，只读）同样纳入表头守护。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from administrative_agent.domains.export import (
    _append_row,
    _append_subtotal_row,
    _is_number,
    _numeric_columns,
    _to_decimal,
)
from administrative_agent.domains.export_fields import (
    EXPORT_FIELD_ALIASES,
    EXPORT_SUBTOTAL_COLUMNS,
    header_label,
)

# ---------------------------------------------------------------------------
# 任务 1：_is_number 边界穷举（每种形态的判定与理由）
# ---------------------------------------------------------------------------

#: (值, 期望判定, 理由)
_IS_NUMBER_CASES: tuple[tuple[object, bool, str], ...] = (
    ("", False, "空串不是数值"),
    ("   ", False, "纯空白 strip 后为空串"),
    ("-", False, "仅负号不是数值"),
    ("1,234", False, "千分位非规范串，不得当金额"),
    ("￥100", False, "货币符号非规范串"),
    ("$100", False, "货币符号非规范串"),
    ("12kg", False, "带单位非规范串"),
    ("1e3", False, "科学计数法不认（ERP Decimal 序列化不会是此形态）"),
    ("abc", False, "中文/字母非数值"),
    ("１２", False, "全角数字非 ASCII 规范串"),
    (" 12 ", True, "首尾空白被 strip，规范整数串"),
    ("12", True, "规范整数串"),
    ("0", True, "零是数值"),
    ("-3.5", True, "规范带符号小数"),
    ("+7", True, "规范带正号整数"),
    (".5", True, "规范前导小数"),
    ("12.", True, "规范尾随小数点"),
    ("1650.00", True, "ERP Decimal 序列化形态"),
    (12, True, "int 原语义保留"),
    (-3.5, True, "float 原语义保留"),
    (Decimal("1.20"), True, "Decimal 本身是数值"),
    (True, False, "bool 不当作金额（原语义保留）"),
    (None, False, "None 非数值"),
    (["1"], False, "非标量非数值"),
)


@pytest.mark.parametrize(
    "value,expected,reason",
    _IS_NUMBER_CASES,
    ids=[f"{v!r}" for v, _, _ in _IS_NUMBER_CASES],
)
def test_is_number_boundary(value: object, expected: bool, reason: str) -> None:
    assert _is_number(value) is expected, reason


def test_to_decimal_string_normalization() -> None:
    """数字字符串归一为 Decimal 且保精度；非规范串返回 None。"""
    assert _to_decimal("1650.00") == Decimal("1650.00")
    assert _to_decimal(" 12 ") == Decimal("12")
    assert _to_decimal("abc") is None
    assert _to_decimal("1,234") is None
    # float 经 str() 转 Decimal（不额外引入误差）
    assert _to_decimal(0.1) == Decimal("0.1")


# ---------------------------------------------------------------------------
# 任务 1/2：总计行 Decimal 合计 + 新实体合计口径
# ---------------------------------------------------------------------------


def _run_subtotal(
    columns: list[str],
    rows: list[dict],
    sum_cols: frozenset[str] | None,
) -> tuple[list, list[bool]]:
    """真实工作表上跑 _append_subtotal_row，返回（单元格值序, numeric_cols）。"""
    from openpyxl import Workbook

    ws = Workbook().active
    numeric_cols = _numeric_columns(columns, rows, sum_cols)
    _append_subtotal_row(ws, columns, numeric_cols, "总计", rows, sum_cols=sum_cols)
    values = [ws.cell(row=1, column=i + 1).value for i in range(len(columns))]
    return values, numeric_cols


def test_amount_string_column_sums_decimal_exactly() -> None:
    """字符串金额列（"0.10"/"0.20"）判为数值列并合计为 Decimal("0.30")。

    若用 float 累积会得到 0.30000000000000004——断言精确 Decimal，钉住
    「不用 float() 累积」。
    """
    columns = ["customer_id", "amount"]
    rows = [
        {"customer_id": 1, "amount": "0.10"},
        {"customer_id": 2, "amount": "0.20"},
    ]
    values, numeric = _run_subtotal(columns, rows, frozenset({"amount"}))
    assert numeric == [True, True]
    assert values[1] == Decimal("0.30")
    assert values[1] != Decimal("0.30000000000000004")


def test_transactions_amount_string_total_regression() -> None:
    """[既有生产缺陷修复] transactions 金额列（字符串形态）恢复合计；
    customer_id/supplier_id 仍不参与求和。"""
    columns = ["tx_type", "amount", "customer_id", "supplier_id", "tx_date"]
    rows = [
        {"tx_type": "收入", "amount": "1650.00", "customer_id": 16,
         "supplier_id": None, "tx_date": "2026-09-01"},
        {"tx_type": "支出", "amount": "350.00", "customer_id": None,
         "supplier_id": 7, "tx_date": "2026-09-02"},
    ]
    values, numeric = _run_subtotal(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["transactions"]
    )
    # 金额（字符串）被判数值列；date 列非数值
    assert numeric == [False, True, True, True, False]
    assert values[0] == "总计"
    assert values[1] == Decimal("2000.00")  # 金额合计恢复
    assert values[2] == "" and values[3] == "" and values[4] == ""  # id/日期不合计


def test_salary_payments_year_month_not_summed() -> None:
    """salary_payments：只合计 amount；year/month（数值列）绝不进合计。

    复核实测旧行为（未登记 sum_cols → 全数值列兜底）会写出
    （总计, 4052, 7, ...）即 year/month 被求和——本断言钉住修复。
    """
    columns = ["employee_id", "year", "month", "amount"]
    rows = [
        {"employee_id": 7, "year": 2025, "month": 1, "amount": "4025.00"},
        {"employee_id": 8, "year": 2025, "month": 2, "amount": "27.00"},
    ]
    values, numeric = _run_subtotal(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["salary_payments"]
    )
    assert numeric == [True, True, True, True]  # 全为数值列（含 amount 字符串）
    assert values == ["总计", "", "", Decimal("4052.00")]  # year/month 不合计


def test_explicit_reorder_does_not_sum_id_like_columns() -> None:
    """显式 fields 重排：id 类列（employee_id）不因位置变化被求和；
    只有登记列 amount 合计。"""
    columns = ["year", "employee_id", "amount"]
    rows = [
        {"year": 2025, "employee_id": 7, "amount": "10.00"},
        {"year": 2026, "employee_id": 8, "amount": "5.00"},
    ]
    values, _ = _run_subtotal(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["salary_payments"]
    )
    assert values[0] == "总计"
    assert values[1] == ""  # employee_id 不合计
    assert values[2] == Decimal("15.00")


@pytest.mark.parametrize(
    "entity,expected",
    [
        ("ar_payments", frozenset({"amount"})),
        ("ap_payments", frozenset({"amount"})),
        ("salary_payments", frozenset({"amount"})),
        ("powder_consumption", frozenset({"quantity"})),
        ("attendance", frozenset({"leave_minutes", "overtime_minutes"})),
        ("powder_inventory", frozenset()),
        ("customer_contacts", frozenset()),
    ],
)
def test_new_entity_subtotal_registration(
    entity: str, expected: frozenset[str]
) -> None:
    """新 7 实体已登记合计口径（非 None——不再走全数值列兜底）。"""
    assert EXPORT_SUBTOTAL_COLUMNS[entity] == expected


def test_attendance_sums_leave_and_overtime_minutes() -> None:
    """attendance：请假/加班分钟数合计，employee_id/att_date 不合计。"""
    columns = ["employee_id", "att_date", "leave_minutes", "overtime_minutes"]
    rows = [
        {"employee_id": 7, "att_date": "2026-09-01",
         "leave_minutes": 60, "overtime_minutes": 90},
        {"employee_id": 8, "att_date": "2026-09-02",
         "leave_minutes": 0, "overtime_minutes": 30},
    ]
    values, _ = _run_subtotal(columns, rows, EXPORT_SUBTOTAL_COLUMNS["attendance"])
    assert values == ["总计", "", Decimal("60"), Decimal("120")]


# ---------------------------------------------------------------------------
# 任务 4：9 个新实体导出表头为中文（照抄 ERP registry 标签）
#   [2026-09-16] 追加 ar_settlements（应收平账，只读）
# ---------------------------------------------------------------------------

#: 每个新实体的全部字段（用于断言无一回落英文列名）
_NEW_ENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "transactions": (
        "id", "tx_type", "category", "project", "amount", "tx_date", "notes",
        "customer_id", "supplier_id", "source_type", "source_id",
        "created_by", "updated_by", "created_at", "updated_at",
    ),
    "transaction_categories": (
        "id", "tx_type", "category_name", "sort_order", "created_at",
    ),
    "customer_contacts": (
        "id", "customer_id", "contact_name", "contact_phone", "is_primary",
        "created_at", "updated_at",
    ),
    "attendance": (
        "id", "employee_id", "att_date", "leave_minutes", "overtime_minutes",
        "created_at", "updated_at",
    ),
    "ar_payments": (
        "id", "customer_id", "amount", "pay_date", "notes", "created_at",
    ),
    "ap_payments": (
        "id", "supplier_id", "amount", "pay_date", "notes", "created_at",
    ),
    "salary_payments": (
        "id", "employee_id", "year", "month", "amount", "paid_at", "notes",
        "created_at",
    ),
    "powder_inventory": (
        "id", "color_code", "warn_threshold", "notes", "created_by",
        "updated_by", "created_at", "updated_at",
        "order_total", "consumed_total", "stock",
    ),
    "powder_consumption": (
        "id", "inventory_id", "color_code", "consume_date", "quantity",
        "notes", "created_by", "updated_by", "created_at", "updated_at",
    ),
    # [2026-09-16 当期应收口径开放] ar_settlements（应收平账/抹零，只读）：
    # 12 个字段逐字取自 ERP registry 该实体注册字段（ArSettlement 模型列
    # 减去 deleted_at（软删列不注册）与 deleted_by（_HIDDEN_FIELDS 不注册）；
    # 模型无 updated_at/updated_by）。此前本表未含该实体，「表头不回落英文
    # 列名」这条判据对它不生效。
    "ar_settlements": (
        "id", "customer_id", "outstanding_before", "total_amount_before",
        "paid_amount_before", "received_amount", "settled_amount",
        "payment_id", "settle_date", "notes", "created_by", "created_at",
    ),
}


@pytest.mark.parametrize("entity", sorted(_NEW_ENTITY_FIELDS))
def test_new_entity_headers_are_chinese(entity: str) -> None:
    """新实体（2026-09-12 的 9 个 + 2026-09-16 的 ar_settlements）：登记字段的
    表头为中文（不回落英文列名）。"""
    aliases = EXPORT_FIELD_ALIASES[entity]
    for field in _NEW_ENTITY_FIELDS[entity]:
        label = header_label(entity, field)
        assert label != field, f"{entity}.{field} 表头回落英文"
        assert label == aliases[field]


def test_transactions_category_label_differs_from_file_attachment() -> None:
    """transactions.category 用表级 override「收支分类」（与
    file_attachments.category 同名不同义，不得混用）。"""
    assert header_label("transactions", "category") == "收支分类"


#: [2026-09-13 R1.salary 取数] employees 新增放行的导出字段 → 中文表头
#: （逐字取自 ERP registry._FIELD_LABELS，见 hengfa-erp/backend/app/mcp/registry.py）。
_EMPLOYEES_NEW_HEADERS: tuple[tuple[str, str], ...] = (
    ("employee_no", "工号"),
    ("position", "岗位"),
    ("daily_wage", "日工资"),
    ("work_hours_per_day", "每日工时"),
)


@pytest.mark.parametrize(
    "field,label",
    _EMPLOYEES_NEW_HEADERS,
    ids=[f for f, _ in _EMPLOYEES_NEW_HEADERS],
)
def test_employees_new_field_headers_are_chinese(field: str, label: str) -> None:
    """employees 放行的四个字段导出表头为中文（不回落英文字段名）。

    鉴别力：删掉 EXPORT_FIELD_ALIASES["employees"] 中任一登记，本用例必红。
    """
    aliases = EXPORT_FIELD_ALIASES["employees"]
    assert field in aliases, f"employees.{field} 未登记中文名"
    assert aliases[field] == label
    assert header_label("employees", field) == label
    assert header_label("employees", field) != field


# ---------------------------------------------------------------------------
# [2026-09-12 回归修复] 字符串→数值认定限 sum_cols 登记列：
#   非登记列（电话/工号/银行账号/税号/单价）不得求和、不得套数值格式。
# ---------------------------------------------------------------------------


def _run_data_row(
    columns: list[str],
    row: dict,
    sum_cols: frozenset[str] | None,
) -> tuple[list, list[str]]:
    """真实工作表上跑 _append_row，返回（单元格值序, number_format 序）。"""
    from openpyxl import Workbook

    ws = Workbook().active
    numeric_cols = _numeric_columns(columns, [row], sum_cols)
    _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
    values = [ws.cell(row=1, column=i + 1).value for i in range(len(columns))]
    formats = [ws.cell(row=1, column=i + 1).number_format for i in range(len(columns))]
    return values, formats


#: (实体, 列, 纯数字串值)——这些列本批前不会被判数值列（字符串只认 int/float），
#: 放宽 _is_number 后曾被误判 → 本次修复必须恢复「不合计、不套数值格式」。
_NON_SUMMABLE_NUMERIC_STRINGS: tuple[tuple[str, str, str], ...] = (
    ("suppliers", "phone", "13800138000"),
    ("suppliers", "bank_account", "6222020200112233"),
    ("suppliers", "tax_id", "91330100123456789X"),
    ("employees", "employee_no", "10086"),
    ("employees", "emergency_phone", "13900139000"),
    ("customer_contacts", "contact_phone", "13512345678"),
    ("customers", "unit_price", "13.50"),
)


@pytest.mark.parametrize(
    "entity,column,value",
    _NON_SUMMABLE_NUMERIC_STRINGS,
    ids=[f"{e}.{c}" for e, c, _ in _NON_SUMMABLE_NUMERIC_STRINGS],
)
def test_non_registered_numeric_string_column_not_numeric(
    entity: str, column: str, value: str
) -> None:
    """非 sum_cols 登记列的纯数字串**不**判数值列（本批前语义）。"""
    sum_cols = EXPORT_SUBTOTAL_COLUMNS[entity]
    assert column not in sum_cols  # 前提：该列未登记合计
    values, formats = _run_data_row([column], {column: value}, sum_cols)
    # 单元格原样保留字符串，未被套 #,##0.00
    assert values[0] == value
    assert formats[0] != "#,##0.00"


@pytest.mark.parametrize(
    "entity,column,value",
    _NON_SUMMABLE_NUMERIC_STRINGS,
    ids=[f"{e}.{c}" for e, c, _ in _NON_SUMMABLE_NUMERIC_STRINGS],
)
def test_non_registered_string_column_absent_from_subtotal_row(
    entity: str, column: str, value: str
) -> None:
    """非登记列（电话/工号/银行账号/税号/单价）不得出现在总计行（不求和）。"""
    cols = ["name", column]
    rows = [
        {cols[0]: "A", column: value},
        {cols[0]: "B", column: value},
    ]
    values, numeric = _run_subtotal(cols, rows, EXPORT_SUBTOTAL_COLUMNS[entity])
    assert numeric == [False, False], "纯数字串列不得被判数值列"
    assert values[1] in ("", None), f"非登记列不得求和，实际 {values[1]!r}"


def test_customers_three_entity_registration_empty_set() -> None:
    """customers/suppliers/employees 显式登记空集 —— 消灭 sum_cols=None 兜底。"""
    for entity in ("customers", "suppliers", "employees"):
        assert EXPORT_SUBTOTAL_COLUMNS[entity] == frozenset()


def test_all_queryable_entities_registered() -> None:
    """QUERYABLE_ENTITIES 全部实体（[2026-09-16] 起 15 个）登记完毕
    （None 兜底路径不可达的前提）。"""
    from administrative_agent.mcp.scopes import QUERYABLE_ENTITIES

    missing = QUERYABLE_ENTITIES - set(EXPORT_SUBTOTAL_COLUMNS)
    assert missing == frozenset(), f"未登记合计口径的实体：{sorted(missing)}"


# ---------------------------------------------------------------------------
# [2026-09-12 回归修复] 正向：sum_cols 登记列（字符串形态）仍出正确合计
# ---------------------------------------------------------------------------


def test_registered_string_amount_still_sums_decimal() -> None:
    """transactions.amount（字符串）仍合计，且 Decimal 精确（正向回归守住）。"""
    columns = ["tx_type", "amount"]
    rows = [
        {"tx_type": "收入", "amount": "1650.50"},
        {"tx_type": "支出", "amount": "-350.25"},
        {"tx_type": "支出", "amount": "0.05"},
    ]
    values, numeric = _run_subtotal(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["transactions"]
    )
    assert numeric == [False, True]
    assert values[1] == Decimal("1300.30")  # 1650.50 - 350.25 + 0.05


def test_metal_orders_registered_columns_sum_unit_price_not_numeric() -> None:
    """metal_orders：total_weight/total_price（登记）合计；unit_price（字符串、
    未登记）不判数值列/不套格式——与本批前行为一致。"""
    from administrative_agent.domains.export import NUMERIC_NUMBER_FORMAT

    sum_cols = EXPORT_SUBTOTAL_COLUMNS["metal_orders"]
    columns = ["model", "unit_price", "quantity", "total_weight", "total_price"]
    rows = [
        {"model": "A", "unit_price": "10.0000", "quantity": 2,
         "total_weight": 2.0, "total_price": 20.0},
        {"model": "B", "unit_price": "50.0000", "quantity": 3,
         "total_weight": 6.0, "total_price": 300.0},
    ]
    numeric = _numeric_columns(columns, rows, sum_cols)
    # unit_price（字符串、未登记）→ False；quantity/total_weight/total_price 数值
    assert numeric == [False, False, True, True, True]
    values, _ = _run_subtotal(columns, rows, sum_cols)
    assert values == ["总计", "", "", 8.0, 320.0]  # 只合计登记列

    # 数据行：unit_price 不套数值格式；total_weight 套
    data_values, data_formats = _run_data_row(columns, rows[0], sum_cols)
    assert data_values[1] == "10.0000"
    assert data_formats[1] != NUMERIC_NUMBER_FORMAT
    assert data_formats[3] == NUMERIC_NUMBER_FORMAT


# ---------------------------------------------------------------------------
# [2026-09-13 复核问题 1] 数据格落盘数值类型：
#   ERP 把 Decimal 序列化成字符串，登记列数据格曾以字符串原样落盘 →
#   data_type='s'（文本，不能排序/求和），而总计行是真数值（data_type='n'），
#   口径不一致。修复后登记列数据格写 Decimal（data_type='n'）。
#   以下断言一律 openpyxl.load_workbook **读回文件**（内存对象看着对、落盘
#   是文本正是本 bug 的形态，只断言内存对象抓不住）。
# ---------------------------------------------------------------------------


def test_registered_column_data_cell_persists_as_numeric(tmp_path) -> None:
    """登记列（transactions.amount）数据格落盘为数值格（data_type='n'）。

    改前：字符串 "1650.00" 原样落盘 → data_type='s'、文本左对齐、不能求和。
    改后：_to_decimal 归一转 Decimal → data_type='n'、套 #,##0.00。
    """
    from openpyxl import Workbook, load_workbook

    from administrative_agent.domains.export import NUMERIC_NUMBER_FORMAT

    wb = Workbook()
    ws = wb.active
    columns = ["tx_type", "amount"]
    row = {"tx_type": "收入", "amount": "1650.00"}
    sum_cols = EXPORT_SUBTOTAL_COLUMNS["transactions"]
    numeric_cols = _numeric_columns(columns, [row], sum_cols)
    _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
    path = tmp_path / "out.xlsx"
    wb.save(str(path))

    ws2 = load_workbook(str(path)).active
    amount = ws2.cell(row=1, column=2)
    assert amount.data_type == "n", f"登记列数据格应为数值，实际 {amount.data_type!r}"
    assert amount.number_format == NUMERIC_NUMBER_FORMAT
    assert Decimal(str(amount.value)) == Decimal("1650.00")


def test_non_registered_numeric_string_data_cell_stays_text(tmp_path) -> None:
    """非登记列（suppliers.phone 纯数字串）数据格落盘仍是文本（data_type='s'）。

    钉住：本次归一**只**作用于 sum_cols 登记列，电话/工号/银行账号/税号等
    纯数字串不得被转成数值格。
    """
    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    ws = wb.active
    columns = ["name", "phone"]
    row = {"name": "A", "phone": "13800138000"}
    sum_cols = EXPORT_SUBTOTAL_COLUMNS["suppliers"]
    numeric_cols = _numeric_columns(columns, [row], sum_cols)
    _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
    path = tmp_path / "out.xlsx"
    wb.save(str(path))

    ws2 = load_workbook(str(path)).active
    phone = ws2.cell(row=1, column=2)
    assert phone.data_type == "s", f"非登记列应保持文本，实际 {phone.data_type!r}"
    assert phone.value == "13800138000"


def test_registered_column_none_value_persists_without_error(tmp_path) -> None:
    """登记列遇到 None：回退原值落盘，不抛异常、不留值。

    同行另一条记录金额合法 → 该列仍判数值列；None 行落盘为空，不因归一崩。
    """
    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    ws = wb.active
    columns = ["tx_type", "amount"]
    sum_cols = EXPORT_SUBTOTAL_COLUMNS["transactions"]
    good = {"tx_type": "收入", "amount": "1650.00"}
    blank = {"tx_type": "支出", "amount": None}
    rows = [good, blank]
    numeric_cols = _numeric_columns(columns, rows, sum_cols)
    for row in rows:
        _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
    path = tmp_path / "out.xlsx"
    wb.save(str(path))

    ws2 = load_workbook(str(path)).active
    assert ws2.cell(row=1, column=2).data_type == "n"
    blank_cell = ws2.cell(row=2, column=2)
    assert blank_cell.value is None, f"None 应落盘为空，实际 {blank_cell.value!r}"
