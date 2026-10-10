"""导出域单元测试：domains/export.py 行富化（_enrich_rows）。

[2026-09-08 ERP 附加款] 专项覆盖 total_price 分支并入行内附加款的五种
行形态（reviewer 审核意见 2）：

- surcharge_amount "88.80"（字符串）→ total_price = 基础金额 + 88.80；
- 缺键 / None / 空串 → 附加款按 0（基础金额不变化）；
- 不可转数值（"abc"）→ 该行 total_price 留空 None（与实现一致：
  float 转换异常 → 列留空，不抛错不阻断导出）；
- 另覆盖：total_weight 不可得 → total_price 留空（同口径）；total_weight
  正常计算（单件重量 × 数量）；关联列（customer_name）照常回填。

行结构按 ERP query 返回形态：数值字段为字符串（"2.0000"/"3.00"/
"10.0000"），关联外键 customer_id 整数；metal_orders 同时注册关联列与
计算列；powder_orders 只注册 supplier_name 关联（计算列未注册——
total_price 并入只发生在 metal 分支）。

[2026-09-10 P1-10] 总计行按重量单位分桶（_append_subtotal_rows）不跨单位
混加；判桶按**行内数据**（row.get("weight_unit")）而非输出列。
[2026-09-10 P1-10 回退] metal 默认导出列移除 weight_unit（EXPORT_DEFAULT_
COLUMNS 7 列，表头不再出「重量单位」）——分桶不受影响（默认导出取整行，
行内恒带 weight_unit 键）：端到端「默认列导出仍产出两个分桶总计行」为本次
回退的硬回归证据；显式 fields=["weight_unit"] 仍可导出该列（别名保留）。

[2026-09-10 P1-10 复核补] 显式 fields 路径：①判桶按**数据**（行内
weight_unit 键）而非输出列；②显式 fields 含非 ERP 原生列（关联列
customer_name / 计算列 total_weight）时，取数走整行（对齐默认路径），
输出列仍严格 = 显式 fields（无 unit 泄漏）；③未登记实体（powder 重量
固定 kg）单行总计不变。本文件覆盖「单单位 / 多单位 / 单位缺失 / 显式
fields 含计算重量列」四路径 + 无单位泄漏。
"""

from __future__ import annotations

import pytest

from administrative_agent.domains.export import (
    _append_subtotal_row,
    _enrich_rows,
    _numeric_columns,
)
from administrative_agent.domains.export_fields import EXPORT_SUBTOTAL_COLUMNS
from administrative_agent.mcp.metadata import FieldSpec


class _FakeResult:
    """与 MCP call_tool 结果形状兼容的最小子集（ok/data.items/total）。"""

    def __init__(self, items: list[dict]) -> None:
        self.ok = True
        self.data = {"items": items, "total": len(items)}


class _FakeErp:
    """call_tool("query", {entity, ...}) → 关联表固定行（名称映射用）。"""

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def call_tool(self, name: str, args: dict):
        assert name == "query"
        return _FakeResult(self._rows.get(args["entity"], []))


ERP = _FakeErp({"customers": [{"id": 1, "company_name": "客户一"}]})


def _base_row(**extra: object) -> dict:
    """钣金行：单重 2 × 数量 3 → 总重量 6；单价 10 → 基础金额 60。"""
    row = {
        "id": 1,
        "customer_id": 1,
        "model": "MK-01",
        "weight_per_unit": "2.0000",
        "quantity": "3.00",
        "unit_price": "10.0000",
    }
    row.update(extra)
    return row


def _enrich(rows: list[dict]) -> list[dict]:
    return _enrich_rows(ERP, "metal_orders", rows)


def test_total_price_includes_surcharge_string() -> None:
    """surcharge "88.80"：total_price = 60.00（基础） + 88.80 = 148.80。"""
    rows = _enrich([_base_row(surcharge_amount="88.80")])
    assert rows[0]["total_weight"] == 6.0
    assert rows[0]["total_price"] == pytest.approx(148.8)
    assert rows[0]["customer_name"] == "客户一"  # 关联列照常回填


def test_total_price_surcharge_missing_means_zero() -> None:
    """缺 surcharge 键 → 按 0（基础金额 60.00 不变）。"""
    rows = _enrich([_base_row()])
    assert rows[0]["total_price"] == 60.0


def test_total_price_surcharge_null_means_zero() -> None:
    """surcharge=None → 按 0。"""
    rows = _enrich([_base_row(surcharge_amount=None)])
    assert rows[0]["total_price"] == 60.0


def test_total_price_surcharge_empty_string_means_zero() -> None:
    """surcharge="" → 按 0。"""
    rows = _enrich([_base_row(surcharge_amount="")])
    assert rows[0]["total_price"] == 60.0


def test_total_price_surcharge_non_numeric_leaves_blank() -> None:
    """surcharge 不可转数值（"abc"）→ total_price 留空 None（与总重量列
    同口径：该列留空，不抛错不阻断导出；total_weight 仍正常计算）。"""
    rows = _enrich([_base_row(surcharge_amount="abc")])
    assert rows[0]["total_weight"] == 6.0
    assert rows[0]["total_price"] is None


def test_total_price_weight_missing_leaves_blank() -> None:
    """total_weight 不可得（weight_per_unit 缺失）→ total_price 留空。"""
    row = _base_row(surcharge_amount="10.00")
    del row["weight_per_unit"]
    rows = _enrich([row])
    assert rows[0]["total_weight"] is None
    assert rows[0]["total_price"] is None


def test_powder_rows_no_total_price_branch() -> None:
    """powder_orders 只注册 supplier_name 关联，无计算列 → 不触发附加款
    并入（回归防护：total_price 分支仅 metal_orders 注册）。"""
    erp = _FakeErp({"suppliers": [{"id": 1, "name": "供应商一"}]})
    rows = [{"id": 20, "supplier_id": 1, "color_code": "R-01",
             "total_weight": "50.00", "surcharge_amount": "99.00"}]

    out = _enrich_rows(erp, "powder_orders", rows)

    assert out is rows  # 原地富化
    assert out[0]["supplier_name"] == "供应商一"
    assert "total_price" not in out[0]  # 塑粉无 total_price/total_weight 计算列
    assert out[0]["surcharge_amount"] == "99.00"  # 行内原值不动（塑粉本无此列）


# ---------------------------------------------------------------------------
# [2026-09-10 P1-10] 默认列含重量单位 + 总计行不跨重量单位混加
# ---------------------------------------------------------------------------


def test_metal_default_columns_exclude_weight_unit() -> None:
    """metal_orders 默认导出列为 8 列、精确列序（[2026-10-10] 入库日期置末、
    单套重量 weight_per_unit 插在数量前）、不含 weight_unit（[2026-09-10 P1-10
    回退] 表头不再出「重量单位」；分桶按行内数据，不依赖该输出列）；
    powder_orders 默认列不变。weight_unit 仍在别名表——显式 fields 可导出。"""
    from administrative_agent.domains.export_fields import (
        EXPORT_DEFAULT_COLUMNS,
        EXPORT_FIELD_ALIASES,
    )

    assert EXPORT_DEFAULT_COLUMNS["metal_orders"] == (
        "customer_name",
        "model",
        "unit_price",
        "weight_per_unit",
        "quantity",
        "total_weight",
        "total_price",
        "in_date",
    )
    assert "weight_unit" not in EXPORT_DEFAULT_COLUMNS["metal_orders"]
    # [2026-10-10] weight_per_unit 与 ERP 前端 UI 用词一致：单套重量
    assert EXPORT_FIELD_ALIASES["metal_orders"]["weight_per_unit"] == "单套重量"
    # 别名保留：显式 fields=["weight_unit"] 仍可导出（表头中文名不变）
    assert EXPORT_FIELD_ALIASES["metal_orders"]["weight_unit"] == "重量单位"
    assert EXPORT_DEFAULT_COLUMNS["powder_orders"] == (
        "supplier_name",
        "color_code",
        "total_weight",
        "price_per_kg",
        "in_date",
    )


def test_metal_default_header_labels_and_order() -> None:
    """metal_orders 默认列表头中文名 + 列序（走 header_label 解析，确保改的
    确实是导出表头而非仅常量）：首行为
    「客户名称,型号,单价,单套重量,数量,总重量,总价格,入库日期」。"""
    from administrative_agent.domains.export_fields import (
        EXPORT_DEFAULT_COLUMNS,
        header_label,
    )

    headers = [
        header_label("metal_orders", f)
        for f in EXPORT_DEFAULT_COLUMNS["metal_orders"]
    ]
    assert headers == [
        "客户名称",
        "型号",
        "单价",
        "单套重量",
        "数量",
        "总重量",
        "总价格",
        "入库日期",
    ]
    # 明确点位：入库日期置末、单套重量在数量前
    assert headers[-1] == "入库日期"
    assert headers.index("单套重量") == headers.index("数量") - 1


def _write_rows_and_read_back(
    columns: list[str],
    rows: list[dict],
    tmp_path,
    *,
    sum_cols: frozenset[str] | None = None,
) -> list[list]:
    """复用真实写入路径的组件（_numeric_columns/_append_row/_
    _append_subtotal_rows）落盘后读回全部行（含总计行）。"""
    from openpyxl import Workbook, load_workbook

    from administrative_agent.domains.export import (
        _append_row,
        _append_subtotal_rows,
        _numeric_columns,
    )

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="导出")
    ws.append(columns)
    numeric_cols = _numeric_columns(columns, rows, sum_cols)
    for row in rows:
        _append_row(ws, columns, row, numeric_cols, sum_cols=sum_cols)
    _append_subtotal_rows(
        ws, columns, numeric_cols, "总计", rows, sum_cols=sum_cols
    )
    path = tmp_path / "subtotal.xlsx"
    wb.save(path)
    wb.close()
    sheet = load_workbook(path, read_only=True).active
    out = [list(r) for r in sheet.iter_rows(values_only=True)]
    sheet.parent.close()
    return out


_METAL_SUM_COLS = frozenset({"total_weight", "total_price"})


def test_subtotal_single_unit_single_row(tmp_path) -> None:
    """单一重量单位 → 单行总计（原行为），合计两列。"""
    columns = ["model", "total_weight", "total_price", "weight_unit"]
    rows = [
        {"model": "A", "total_weight": 2.0, "total_price": 20.0, "weight_unit": "kg"},
        {"model": "B", "total_weight": 3.0, "total_price": 35.0, "weight_unit": "kg"},
    ]
    out = _write_rows_and_read_back(columns, rows, tmp_path, sum_cols=_METAL_SUM_COLS)
    assert out[0] == columns
    assert out[-1][0] == "总计"
    assert out[-1][1] == 5.0
    assert out[-1][2] == 55.0


def test_subtotal_multi_unit_buckets_no_mixing(tmp_path) -> None:
    """kg 与 m² 混行 → 按单位分桶各一行（标签「总计（kg）」），每桶只合计
    本桶——不同物理量绝不相加。"""
    columns = ["model", "total_weight", "total_price", "weight_unit"]
    rows = [
        {"model": "A", "total_weight": 2.0, "total_price": 20.0, "weight_unit": "kg"},
        {"model": "B", "total_weight": 4.0, "total_price": 200.0, "weight_unit": "m²"},
        {"model": "C", "total_weight": 3.0, "total_price": 30.0, "weight_unit": "kg"},
    ]
    out = _write_rows_and_read_back(columns, rows, tmp_path, sum_cols=_METAL_SUM_COLS)
    assert len(out) == 1 + 3 + 2  # 表头 + 数据 + 两个单位桶总计行
    assert [r[0] for r in out[4:]] == ["总计（kg）", "总计（m²）"]  # 单位出现序
    assert out[4][1] == 5.0 and out[4][2] == 50.0  # kg 桶：2+3 / 20+30
    assert out[5][1] == 4.0 and out[5][2] == 200.0  # m² 桶原样
    # 不跨单位混加：kg 桶绝非全量相加（5.0 ≠ 9.0）
    assert out[4][1] != 9.0


def test_subtotal_missing_unit_gets_own_bucket(tmp_path) -> None:
    """未注明单位（None/空串）的行单独成桶（「总计（未注明单位）」），不并入
    任何单位桶。"""
    columns = ["model", "total_weight", "weight_unit"]
    rows = [
        {"model": "A", "total_weight": 2.0, "weight_unit": "kg"},
        {"model": "B", "total_weight": 7.0, "weight_unit": None},
        {"model": "C", "total_weight": 1.5, "weight_unit": "  "},
    ]
    out = _write_rows_and_read_back(
        columns, rows, tmp_path, sum_cols=frozenset({"total_weight"})
    )
    assert [r[0] for r in out[4:]] == ["总计（kg）", "总计（未注明单位）"]
    assert out[4][1] == 2.0  # kg 桶只有 A
    assert out[5][1] == 8.5  # None 与空白归一同一「未注明单位」桶：7+1.5


def test_subtotal_without_unit_column_keeps_single_row(tmp_path) -> None:
    """输出不含 weight_unit 列（如显式 fields 未要该列）→ 单行总计（原
    行为不变）。"""
    columns = ["model", "total_weight"]
    rows = [
        {"model": "A", "total_weight": 2.0},
        {"model": "B", "total_weight": 4.0},
    ]
    out = _write_rows_and_read_back(
        columns, rows, tmp_path, sum_cols=frozenset({"total_weight"})
    )
    assert out[-1][0] == "总计"
    assert out[-1][1] == 6.0


# ---------------------------------------------------------------------------
# 端到端：ExcelExporter 默认导出（表头含重量单位 + 多单位分桶总计行）
# ---------------------------------------------------------------------------


class _ExportMeta:
    """与生产 ``EntityMetadata`` 同形：``fields`` 为 ``dict[str, FieldSpec]``。

    [2026-09-15] 此前用 ``list[str]``——而敏感过滤器 ``_is_sensitive`` 只认
    mapping（非 mapping 显式报错，见 domains/export_fields.py）；替身必须与
    生产同形，否则测不到真实路径、还可能掩盖「过滤器静默不排除」的缺陷。
    """

    def __init__(self, fields: list[str]) -> None:
        self.fields = {
            name: FieldSpec.from_raw(name, {"type": "str"}) for name in fields
        }


class _ExportCache:
    """EntityCache 最小替身（validate_export_fields 只消费 get_entity.fields）。

    只放 **ERP 原生字段**：total_weight/customer_name 等关联/计算列是 Agent
    侧本地富化列，绝不进元数据（真实 list_entities 元数据同样是原生字段；
    显式请求这类列靠导出器走整行取数 + 本地富化，不需在元数据内）。
    """

    def __init__(self) -> None:
        self._entities = {
            "metal_orders": _ExportMeta([
                "id", "customer_id", "model", "unit_price", "quantity",
                "weight_per_unit", "weight_unit", "in_date", "surcharge_amount",
            ]),
            "customers": _ExportMeta(["id", "company_name"]),
        }

    def get_entity(self, name: str):
        return self._entities.get(name)


#: 各实体 ERP 原生字段白名单（与 _ExportCache 元数据一致）。关联/计算列不在
#: 其中——替身据此对未知字段返回失败，贴近真实 ERP 的 UNKNOWN_FIELD 行为，
#: 防止「显式 fields 原样透传 ERP」的回归被放过。
_NATIVE_FIELDS: dict[str, set[str]] = {
    "metal_orders": {
        "id", "customer_id", "model", "unit_price", "quantity",
        "weight_per_unit", "weight_unit", "in_date", "surcharge_amount",
    },
    "customers": {"id", "company_name"},
}


class _ExportResult:
    def __init__(self, data: dict) -> None:
        self.ok = True
        self.data = data


class _ExportError:
    def __init__(self, message: str) -> None:
        self.message = message


class _ExportErrorResult:
    """ERP 失败结果（ok=False + error.message；导出器据此转 ExportError）。"""

    def __init__(self, message: str) -> None:
        self.ok = False
        self.data = None
        self.error = _ExportError(message)


class _ExportErp:
    """client.call_tool：metal_orders 主查询全量行；customers 关联表按 id。

    [复核] 贴近真实 ERP：请求 fields 含非原生列（customer_name/total_weight
    等）→ 返回 ok=False（UNKNOWN_FIELD）——导出器必须只请求元数据内字段
    （或整行不带 fields）才能成功。
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def call_tool(self, name: str, args: dict):
        assert name == "query"
        self.calls.append(dict(args))
        entity = args["entity"]
        requested = args.get("fields")
        if requested:
            native = _NATIVE_FIELDS.get(entity, set())
            unknown = [f for f in requested if f not in native]
            if unknown:
                return _ExportErrorResult(
                    "UNKNOWN_FIELD: " + ", ".join(unknown)
                )
        if entity == "customers":
            ids = sorted({r["customer_id"] for r in self.rows})
            items = [{"id": i, "company_name": f"客户{i}"} for i in ids]
            return _ExportResult({"items": items, "total": len(items)})
        return _ExportResult(
            {"items": [dict(r) for r in self.rows], "total": len(self.rows)}
        )


class _ExportAudit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_export(self, **kwargs) -> None:
        self.records.append(kwargs)


def test_exporter_default_metal_export_no_unit_column_but_buckets_totals(
    tmp_path,
) -> None:
    """默认导出（fields 缺省）端到端 [2026-09-10 P1-10 回退，2026-10-10 列序更新]：

    ① 表头为 8 列、**不含「重量单位」**，列序为 客户名称/型号/单价/单套重量/
       数量/总重量/总价格/入库日期（入库日期置末、单套重量在数量前）；
    ② kg 与 m² 混行时总计行仍按单位分桶（「总计（kg）」「总计（m²）」）、
       各自只合计本桶——证明删输出列不影响分桶（判桶取自行内数据）。"""
    from openpyxl import load_workbook

    from administrative_agent.domains.export import ExcelExporter

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
        {"id": 2, "customer_id": 7, "model": "A-2", "unit_price": "50.0000",
         "quantity": "1.00", "weight_per_unit": "4.0000", "in_date": "2026-08-02",
         "weight_unit": "m²"},
    ]
    exporter = ExcelExporter(
        client=_ExportErp(erp_rows),
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(entity="metal_orders")
    assert result.row_count == 2

    sheet = load_workbook(result.path, read_only=True).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    sheet.parent.close()
    # ① 默认 8 列、精确列序、无「重量单位」
    assert rows[0] == [
        "客户名称", "型号", "单价", "单套重量", "数量", "总重量", "总价格", "入库日期",
    ]
    assert "重量单位" not in rows[0]
    # ② 仍按单位分桶，各自只合计本桶（判桶行内数据，不依赖输出列）
    labels = [r[0] for r in rows[3:]]
    assert labels == ["总计（kg）", "总计（m²）"]
    assert rows[3][5] == 2.0 and rows[3][6] == pytest.approx(20.0)  # kg 桶
    assert rows[4][5] == 4.0 and rows[4][6] == pytest.approx(200.0)  # m² 桶
    assert rows[3][5] != 6.0  # 绝非 2+4 的混加


def test_exporter_explicit_weight_unit_column_still_exportable(tmp_path) -> None:
    """[2026-09-10 P1-10 回退] weight_unit 虽退出默认列，但别名保留、显式
    fields=["weight_unit"] 仍可导出：表头为「重量单位」。"""
    from openpyxl import load_workbook

    from administrative_agent.domains.export import ExcelExporter

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
        {"id": 2, "customer_id": 7, "model": "A-2", "unit_price": "50.0000",
         "quantity": "1.00", "weight_per_unit": "4.0000", "in_date": "2026-08-02",
         "weight_unit": "m²"},
    ]
    erp = _ExportErp(erp_rows)
    exporter = ExcelExporter(
        client=erp,
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export-unit",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(entity="metal_orders", fields=["model", "weight_unit"])
    assert result.row_count == 2
    metal_calls = [c for c in erp.calls if c["entity"] == "metal_orders"]
    assert metal_calls and metal_calls[0].get("fields") == ["model", "weight_unit"]
    _assert_no_unknown_fields_requested(erp)

    sheet = load_workbook(result.path, read_only=True).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    sheet.parent.close()
    assert rows[0] == ["型号", "重量单位"]  # 显式要求的单位列表头为中文
    assert rows[1][1] == "kg" and rows[2][1] == "m²"


# ---------------------------------------------------------------------------
# [2026-09-10 P1-10 复核补] 显式 fields 路径：内部补取单位字段按数据分桶
# ---------------------------------------------------------------------------


def test_subtotal_buckets_by_data_when_unit_not_in_columns(tmp_path) -> None:
    """判桶按**数据**（行内 weight_unit 键）而非输出列：输出列不含
    weight_unit、但行数据带该键 → 仍按 kg/m² 分桶（不跨单位混加）。"""
    columns = ["model", "total_weight"]  # 输出列不含 weight_unit
    rows = [
        {"model": "A", "total_weight": 2.0, "weight_unit": "kg"},
        {"model": "B", "total_weight": 4.0, "weight_unit": "m²"},
        {"model": "C", "total_weight": 3.0, "weight_unit": "kg"},
    ]
    out = _write_rows_and_read_back(
        columns, rows, tmp_path, sum_cols=frozenset({"total_weight"})
    )
    assert out[0] == columns  # 表头无 weight_unit（不泄漏）
    assert [r[0] for r in out[4:]] == ["总计（kg）", "总计（m²）"]
    assert out[4][1] == 5.0  # kg 桶：2+3
    assert out[5][1] == 4.0  # m² 桶原样
    assert out[4][1] != 9.0  # 绝非全量混加


def test_subtotal_rows_without_any_unit_keeps_single_row(tmp_path) -> None:
    """数据行完全拿不到单位键（未登记实体如 powder、或元数据无单位字段）
    → 单行总计（原行为不变），不误分成多桶。"""
    columns = ["model", "total_weight"]
    rows = [
        {"model": "A", "total_weight": 2.0},
        {"model": "B", "total_weight": 4.0},
    ]
    out = _write_rows_and_read_back(
        columns, rows, tmp_path, sum_cols=frozenset({"total_weight"})
    )
    assert out[-1][0] == "总计"
    assert out[-1][1] == 6.0


def _assert_no_unknown_fields_requested(erp: _ExportErp) -> None:
    """断言导出器从未向 ERP 请求过任何非原生列（否则替身已返回 ok=False）。"""
    for call in erp.calls:
        requested = call.get("fields")
        if requested:
            native = _NATIVE_FIELDS.get(call["entity"], set())
            assert all(f in native for f in requested), (
                f"{call['entity']} 请求了未知字段：{requested}"
            )


def test_exporter_explicit_join_and_computed_columns_succeed(tmp_path) -> None:
    """端到端（[复核] 可达存量缺陷修复）：显式 fields 含关联列
    customer_name 与计算列 total_weight——
    ① 导出成功（这两列 ERP 不认，若原样透传 → UNKNOWN_FIELD 整单失败）；
    ② 输出列严格 = 显式 fields，关联/计算列值由 _enrich_rows 本地补齐；
    ③ 从未向 ERP 请求非原生列（替身对未知字段 fail，且事后复核 calls）。"""
    from openpyxl import load_workbook

    from administrative_agent.domains.export import ExcelExporter

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
        {"id": 2, "customer_id": 7, "model": "A-2", "unit_price": "50.0000",
         "quantity": "1.00", "weight_per_unit": "4.0000", "in_date": "2026-08-02",
         "weight_unit": "m²"},
    ]
    erp = _ExportErp(erp_rows)
    exporter = ExcelExporter(
        client=erp,
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export-join",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(
        entity="metal_orders", fields=["customer_name", "model", "total_weight"]
    )
    assert result.row_count == 2
    # ③ 从不请求非原生列（替身已对未知字段 fail closed；此处再显式复核）
    _assert_no_unknown_fields_requested(erp)

    sheet = load_workbook(result.path, read_only=True).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    sheet.parent.close()
    # ② 输出列严格 = 显式 fields；关联列本地富化、计算列本地算出
    assert rows[0] == ["客户名称", "型号", "总重量"]
    assert rows[1][0] == "客户7" and rows[2][0] == "客户7"
    assert rows[1][2] == 2.0  # 1.0 × 2
    assert rows[2][2] == 4.0  # 4.0 × 1


def test_exporter_explicit_computed_weight_buckets_without_leak(tmp_path) -> None:
    """端到端：显式 fields=["model","total_weight"]（含计算列 total_weight、
    不含 weight_unit）——
    ① 取数走整行（对齐默认路径），行内带 weight_unit；
    ② 输出列严格 = 显式 fields（weight_unit 不泄漏进表头）；
    ③ 总计行仍按 kg/m² 分桶，绝不混加。"""
    from openpyxl import load_workbook

    from administrative_agent.domains.export import ExcelExporter

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
        {"id": 2, "customer_id": 7, "model": "A-2", "unit_price": "50.0000",
         "quantity": "1.00", "weight_per_unit": "4.0000", "in_date": "2026-08-02",
         "weight_unit": "m²"},
    ]
    erp = _ExportErp(erp_rows)
    exporter = ExcelExporter(
        client=erp,
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export-explicit",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(entity="metal_orders", fields=["model", "total_weight"])
    assert result.row_count == 2

    # ① 含非原生列 → 不传 fields（整行取数），绝不请求未知字段
    metal_calls = [c for c in erp.calls if c["entity"] == "metal_orders"]
    assert metal_calls and "fields" not in metal_calls[0]
    _assert_no_unknown_fields_requested(erp)

    sheet = load_workbook(result.path, read_only=True).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    sheet.parent.close()
    # ② 输出列严格 = 显式 fields（无 weight_unit 泄漏）
    assert rows[0] == ["型号", "总重量"]
    # 表头 + 2 数据行 + 2 分桶总计行
    labels = [r[0] for r in rows[3:]]
    assert labels == ["总计（kg）", "总计（m²）"]
    assert rows[3][1] == 2.0  # kg 桶：1.0×2
    assert rows[4][1] == 4.0  # m² 桶：4.0×1
    assert rows[3][1] != 6.0  # 绝非 2+4 的混加


def test_exporter_explicit_all_native_fields_passed_through(tmp_path) -> None:
    """端到端：显式 fields 全部为 ERP 原生字段（无关联/计算列）→ 原样透传
    ERP（保持既有取数优化；所有请求字段均 ∈ 元数据）。"""
    from administrative_agent.domains.export import ExcelExporter

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
    ]
    erp = _ExportErp(erp_rows)
    exporter = ExcelExporter(
        client=erp,
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export-native",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(entity="metal_orders", fields=["model", "quantity"])
    assert result.row_count == 1
    metal_calls = [c for c in erp.calls if c["entity"] == "metal_orders"]
    assert metal_calls and metal_calls[0].get("fields") == ["model", "quantity"]
    _assert_no_unknown_fields_requested(erp)


# ===========================================================================
# [2026-09-10 P3] 导出总计行 sum_cols 语义（export_fields.EXPORT_SUBTOTAL_COLUMNS）
#   - transactions：只合计 amount，关联主键 customer_id/supplier_id 不参与求和；
#   - transaction_categories：显式登记空集 = 「显式不合计」（[2026-09-12
#     回归修复] 兜底路径已删除：sum_cols=None 现视为空集、不再全数值列求和）。
# ===========================================================================

def _sheet():
    """真实 Worksheet（`_append_subtotal_row` 写 WriteOnlyCell + number_format，
    依赖 openpyxl 样式描述符，需要真实工作表承接）。"""
    from openpyxl import Workbook

    return Workbook().active


def _rows(ws) -> list[list]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def test_subtotal_transactions_sums_amount_only_not_party_ids() -> None:
    """transactions 总计行只合计 amount；customer_id/supplier_id（关联主键，
    数值型）绝不进合计项（旧行为 sum_cols 未登记 → 兜底全数值列，id 被求和）。"""
    columns = ["tx_type", "amount", "customer_id", "supplier_id", "tx_date"]
    rows = [
        {"tx_type": "收入", "amount": 1650.0, "customer_id": 16,
         "supplier_id": None, "tx_date": "2026-09-01"},
        {"tx_type": "支出", "amount": 350.0, "customer_id": None,
         "supplier_id": 7, "tx_date": "2026-09-02"},
    ]
    numeric_cols = _numeric_columns(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["transactions"]
    )
    # amount / customer_id / supplier_id 均被判定为数值列（id 为整数）
    assert numeric_cols == [False, True, True, True, False]

    assert EXPORT_SUBTOTAL_COLUMNS["transactions"] == frozenset({"amount"})
    sheet = _sheet()
    _append_subtotal_row(
        sheet, columns, numeric_cols, "总计", rows,
        sum_cols=EXPORT_SUBTOTAL_COLUMNS.get("transactions"),
    )
    values = _rows(sheet)[0]
    assert values[0] == "总计"
    assert values[1] == pytest.approx(2000.0)  # amount 合计
    assert values[2] == "" and values[3] == ""  # 关联 id 不出现在合计项
    assert values[4] == ""


def test_subtotal_transaction_categories_empty_set_means_explicit_no_sum() -> None:
    """transaction_categories 显式登记 frozenset()：无金额列 → 不产生任何
    数值合计（否则 id/sort_order 被误合计）。"""
    columns = ["name", "sort_order", "id"]
    rows = [
        {"name": "收入", "sort_order": 1, "id": 1},
        {"name": "支出", "sort_order": 2, "id": 2},
    ]
    numeric_cols = _numeric_columns(
        columns, rows, EXPORT_SUBTOTAL_COLUMNS["transaction_categories"]
    )
    assert numeric_cols == [False, True, True]

    registered = EXPORT_SUBTOTAL_COLUMNS["transaction_categories"]
    assert registered == frozenset()

    sheet = _sheet()
    _append_subtotal_row(
        sheet, columns, numeric_cols, "总计", rows,
        sum_cols=EXPORT_SUBTOTAL_COLUMNS.get("transaction_categories"),
    )
    assert _rows(sheet)[0] == ["总计", "", ""]

    # [2026-09-12 回归修复] 兜底路径已删除：sum_cols=None（无实体再取此值）
    # 现视为空集 → 不再全数值列求和（对照旧行为 ["总计", 3, 3]）
    fallback = _sheet()
    _append_subtotal_row(fallback, columns, numeric_cols, "总计", rows, sum_cols=None)
    assert _rows(fallback)[0] == ["总计", "", ""]


# ===========================================================================
# 表头样式 + 冻结首行（用户需求：改表头样式 + 冻结首行，其余格式不动）
# ===========================================================================


def _read_header_styled(path: str):
    """非 read_only 载入（样式可读），返回 (worksheet, workbook)。"""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    return wb.active, wb


def test_header_cell_applies_all_styles(tmp_path) -> None:
    """_header_cell 单测：加粗 / 实心浅色底纹 / 水平垂直居中 / 四边细边框。

    经真实 write_only 落盘 + load_workbook 读回，确保样式确实写进文件
    （而非只设了内存对象）。"""
    from openpyxl import Workbook

    from administrative_agent.domains.export import (
        HEADER_FILL_COLOR,
        _header_cell,
    )

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="导出")
    ws.append([_header_cell(ws, "型号"), _header_cell(ws, "数量")])
    ws.append(["A", 1])
    path = tmp_path / "header.xlsx"
    wb.save(path)
    wb.close()

    sheet, wb2 = _read_header_styled(str(path))
    try:
        for col in (1, 2):
            cell = sheet.cell(row=1, column=col)
            assert cell.font.bold is True
            assert cell.fill.fill_type == "solid"
            assert cell.fill.fgColor.rgb == f"00{HEADER_FILL_COLOR}"
            assert cell.alignment.horizontal == "center"
            assert cell.alignment.vertical == "center"
            assert cell.border.left.style == "thin"
            assert cell.border.right.style == "thin"
            assert cell.border.top.style == "thin"
            assert cell.border.bottom.style == "thin"
    finally:
        wb2.close()


def test_header_styles_and_freeze_panes_end_to_end(tmp_path) -> None:
    """端到端：ExcelExporter 导出后表头行带全部样式、首行已冻结，且数据行
    与总计行**不**套表头样式（只动表头）。"""
    from administrative_agent.domains.export import (
        HEADER_FILL_COLOR,
        ExcelExporter,
    )

    erp_rows = [
        {"id": 1, "customer_id": 7, "model": "A-1", "unit_price": "10.0000",
         "quantity": "2.00", "weight_per_unit": "1.0000", "in_date": "2026-08-01",
         "weight_unit": "kg"},
    ]
    exporter = ExcelExporter(
        client=_ExportErp(erp_rows),
        cache=_ExportCache(),
        audit=_ExportAudit(),
        trace_id="thread:t-export-style",
        actor_account="manager01",
        actor_role="manager",
        export_dir=str(tmp_path),
    )
    result = exporter.export(entity="metal_orders", fields=["model", "quantity"])

    sheet, wb = _read_header_styled(result.path)
    try:
        # 冻结首行
        assert sheet.freeze_panes == "A2"
        # 表头 2 列全部带样式（加粗/底纹/居中/细边框）
        for col in (1, 2):
            cell = sheet.cell(row=1, column=col)
            assert cell.font.bold is True
            assert cell.fill.fill_type == "solid"
            assert cell.fill.fgColor.rgb == f"00{HEADER_FILL_COLOR}"
            assert cell.alignment.horizontal == "center"
            assert cell.alignment.vertical == "center"
            assert cell.border.left.style == "thin"
            assert cell.border.right.style == "thin"
            assert cell.border.top.style == "thin"
            assert cell.border.bottom.style == "thin"
        # 数据行不套表头样式（加粗/底纹/边框均无）
        data = sheet.cell(row=2, column=1)
        assert not data.font.bold
        assert data.fill.fill_type is None
        assert data.border.left.style is None
    finally:
        wb.close()


def test_header_empty_columns_does_not_raise_with_styles(tmp_path) -> None:
    """列解析为空（零列导出）时表头为空列表——带样式的写入路径不得抛错，
    冻结首行仍生效（保持既有「零列导出」行为）。"""
    from openpyxl import load_workbook

    from administrative_agent.domains import export as export_mod
    from administrative_agent.domains.export import HEADER_FREEZE_PANES

    exporter = export_mod.ExcelExporter(
        client=object(), cache=object(), audit=object(),
        trace_id="t", actor_account="a", actor_role="manager",
        export_dir=str(tmp_path),
    )
    path = tmp_path / "empty.xlsx"
    # 直接走写盘内部方法：columns=[] → header=[]，rows=[]（零列零行）
    exporter._write_workbook(str(path), "metal_orders", [], [])

    wb = load_workbook(str(path))
    try:
        sheet = wb.active
        assert sheet.freeze_panes == HEADER_FREEZE_PANES
        # 空表头不产生任何带值的单元格（无数据行）
        assert all(c.value is None for row in sheet.iter_rows() for c in row)
    finally:
        wb.close()
