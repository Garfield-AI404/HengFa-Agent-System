"""D19 R6 对账计划执行器测试（6.4 步骤 1-9、6.5、RV-28/RV-29、零写）。

覆盖：
- 全流程：读 published 映射 → 内存解析 → 客户唯一匹配 → ERP 订单取全 →
  匹配与差异分类 → 只读差异报告（四类齐全 + L843/L845 标记）；
- 零写：全程只发出 query 调用（respx 假服务断言），任务 succeeded 仅经
  报告生成路径（执行门槛 L870：差异报告直接生成只读）；
- RV-28：转换错误进报告（行号 + 列名 + 原因），文件哈希/映射版本定位；
- 客户匹配失败 → customer_unmatched 报告表达不阻断，跳过订单查询；
- 无 published 映射 → 报错 + 任务 failed（6.3 规则 1 只使用 published）；
- 分页数据变化 → data_completeness=partial + limitations 显式说明；
- 审计链：每次实际 query 调用独立 attempt/result（11.2）。

复用 tests/domains/test_query.py 的 FakeQueryErp 假服务（D5 规则：
测试夹具，不代表 ERP 真实运行态）与 test_mapping_service 的内存存储。
"""

from __future__ import annotations

import io
import os

os.environ["ERP_MCP_URL"] = "http://erp-mcp.test"
os.environ["ERP_MCP_API_KEY"] = "test-api-key-not-for-production"
os.environ["ERP_MCP_JWT"] = ""
os.environ["ERP_MCP_CONNECT_TIMEOUT"] = "5"
os.environ["ERP_MCP_READ_TIMEOUT"] = "30"
os.environ["ERP_MCP_TOTAL_TIMEOUT"] = "60"
os.environ["ERP_MCP_METADATA_TTL"] = "3600"

import pytest  # noqa: E402
import respx  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from administrative_agent.audit.service import AuditService  # noqa: E402
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.query import QueryExecutor  # noqa: E402
from administrative_agent.domains.reconciliation import (  # noqa: E402
    ReconciliationPlan,
    ReconciliationPlanExecutor,
    ReconciliationReport,
)
from administrative_agent.files.mapping_service import (  # noqa: E402
    ExcelMappingService,
    MappingNotPublishedError,
)
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.mcp.metadata import EntityCache  # noqa: E402
from administrative_agent.tasks.machine import TaskStatus  # noqa: E402
from administrative_agent.tasks.service import TaskService  # noqa: E402
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.domains.test_mapping_service import InMemoryMappingStore  # noqa: E402
from tests.domains.test_query import (  # noqa: E402
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ERP_MCP_URL,
    TRACE_ID,
    FakeQueryErp,
    create_task,
)
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

METAL_HEADER = ("型号", "日期", "单重", "数量", "单价", "状态", "备注")


def metal_mapping_columns():
    return [
        {"excel_column": "型号", "target_field": "model", "data_type": "string",
         "is_required": True},
        {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
         "is_required": True},
        {"excel_column": "单重", "target_field": "weight_per_unit",
         "data_type": "decimal", "conversion_rule": "trim"},
        {"excel_column": "数量", "target_field": "quantity", "data_type": "int"},
        {"excel_column": "单价", "target_field": "unit_price", "data_type": "decimal"},
        {"excel_column": "状态", "target_field": "status", "data_type": "enum",
         "enum_mapping": {"完成": "完成", "进行中": "进行中"}},
        {"excel_column": "备注", "target_field": "notes", "data_type": "string",
         "default_value": ""},
    ]


def published_service(*, template_key="recon-metal", columns=None):
    service = ExcelMappingService(InMemoryMappingStore())
    result = service.import_draft(
        actor="manager01", template_key=template_key, sheet_name="对账单",
        header_row=1, source_file_hash="sha256-mapping",
        columns=columns or metal_mapping_columns(),
    )
    service.publish(actor="manager01", version_id=result.version.version_id)
    return service


def build_xlsx(rows, header=METAL_HEADER, sheet_name="对账单") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


#: 演示对账单（6 行有效 + 1 行转换错误）：
#: M-1 一致（matched）/ M-2 单重 4.00 vs ERP 3.00（field_diff 非零旧值）/
#: M-999 ERP 缺失（erp_missing）/ M-AMB 同键双订单（ambiguous）/
#: M-ZW 原重量 0 可更新（zero_weight_updatable）/ M-2 重复行一致 /
#: M-BAD 非法日期（转换错误行）
DEMO_ROWS = [
    ("M-1", "2026-06-02", 3.00, 10, 12.50, "完成", ""),
    ("M-2", "2026-06-03", 4.00, 10, 12.50, "完成", ""),
    ("M-999", "2026-06-30", 2.00, 5, 10.00, "完成", ""),
    ("M-AMB", "2026-06-15", 3.00, 10, 12.50, "完成", ""),
    ("M-ZW", "2026-06-20", 5.00, 10, 12.50, "完成", ""),
    ("M-2", "2026-06-03", 3.00, 10, 12.50, "完成", ""),
    ("M-BAD", "2026-13-99", 3.00, 10, 12.50, "完成", ""),
]
DEMO_XLSX = build_xlsx(DEMO_ROWS)


def add_extra_metal_rows(erp: FakeQueryErp) -> None:
    """补充 ERP 假数据：同键歧义双订单 + 原重量 0 订单（场景 9 素材）。"""
    erp.rows["metal_orders"].append({
        "id": 51, "customer_id": 101, "model": "M-AMB", "status": "完成",
        "unit_price": "12.50", "quantity": "10", "weight_per_unit": "3.00",
        "weight_unit": "kg", "price_type": "按重量", "total_amount": "375.00",
        "in_date": "2026-06-15", "out_date": None, "notes": "",
        "updated_at": "2026-07-01 08:51:00",
    })
    erp.rows["metal_orders"].append({
        "id": 52, "customer_id": 101, "model": "M-AMB", "status": "完成",
        "unit_price": "12.50", "quantity": "10", "weight_per_unit": "3.00",
        "weight_unit": "kg", "price_type": "按重量", "total_amount": "375.00",
        "in_date": "2026-06-15", "out_date": None, "notes": "",
        "updated_at": "2026-07-01 08:52:00",
    })
    erp.rows["metal_orders"].append({
        "id": 53, "customer_id": 101, "model": "M-ZW", "status": "完成",
        "unit_price": "12.50", "quantity": "10", "weight_per_unit": "0",
        "weight_unit": "kg", "price_type": "按重量", "total_amount": "0.00",
        "in_date": "2026-06-20", "out_date": None, "notes": "",
        "updated_at": "2026-07-01 08:53:00",
    })


@pytest.fixture
def erp() -> FakeQueryErp:
    fake = FakeQueryErp()
    # 演示归属：示例五金(101) 拥有 M-1/M-2（假服务默认 customer_id=100+oid），
    # 其余订单归 永盛钣金(103)，不参与本演示的客户匹配
    for row in fake.rows["metal_orders"]:
        row["customer_id"] = 101 if row["id"] in (1, 2) else 103
    add_extra_metal_rows(fake)
    return fake


@pytest.fixture
def actx(erp: FakeQueryErp):
    """组装 R6 执行器上下文。yield (erp, tasks, audit, exec, query, client, mappings)。"""
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            cache = EntityCache(client)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            query_exec = QueryExecutor(
                client=client,
                cache=cache,
                tasks=tasks,
                audit=audit,
                trace_id=TRACE_ID,
                actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE,
                duty_code="R6",
            )
            mappings = published_service()
            reconciler = ReconciliationPlanExecutor(
                mapping_service=mappings, query_executor=query_exec
            )
            yield erp, tasks, audit, reconciler, query_exec, client, mappings
        finally:
            client.close()


def make_plan(**overrides) -> ReconciliationPlan:
    base = dict(
        template_key="recon-metal",
        entity="metal_orders",
        subject_name="示例五金",
        excel_bytes=DEMO_XLSX,
        period_note="2026 年 6 月（核对范围说明）",
    )
    base.update(overrides)
    return ReconciliationPlan(**base)


# ---- 全流程：匹配与差异分类（6.4 步骤 5-9） ----


def test_full_flow_report_covers_all_categories(erp, actx):
    """唯一匹配/缺失/字段差异/匹配歧义四类齐全 + 转换错误 + 定位信息。"""
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(task_id=task.task_id, plan=make_plan())
    assert isinstance(report, ReconciliationReport)
    assert report.template_key == "recon-metal"
    assert report.entity == "metal_orders"
    assert report.subject_match.status == "matched"
    assert report.subject_match.entity_id == 101  # 示例五金
    assert report.period_note == "2026 年 6 月（核对范围说明）"
    # RV-28：报告可定位文件哈希（sha256 hex）与映射版本
    assert len(report.source_file_hash) == 64
    assert report.mapping_version == 1
    assert report.row_count == 6  # 转换错误行不进标准行
    # RV-28：行级转换错误（行号 + 列名 + 原因）
    assert len(report.conversion_errors) == 1
    err = report.conversion_errors[0]
    assert err.row_number == 8  # 表头 1 + 数据 7 行 → 错误行号 8
    assert err.excel_column == "日期"
    assert "日期转换失败" in err.reason
    # 四类齐全（RV-29）
    by_class: dict[str, int] = report.summary
    assert by_class["matched"] == 2      # M-1、M-2 重复行（值与 ERP 一致）
    assert by_class["field_diff"] == 2   # M-2 单重 4.00 vs 3.00（非零旧值）+
                                         # M-ZW 原重量 0 可更新（L843）
    assert by_class["erp_missing"] == 1  # M-999
    assert by_class["ambiguous"] == 1    # M-AMB
    # L843：M-ZW 原重量 0 → zero_weight_updatable + update_order 标记
    zw = [e for e in report.entries if e.match_key and e.match_key.product_ref == "M-ZW"]
    assert len(zw) == 1
    assert zw[0].classification == "field_diff"
    assert zw[0].diffs[0].kind == "zero_weight_updatable"
    assert zw[0].write_entry == "update_order"
    # L843 非零旧值：M-2 差异不标记静默更新
    m2 = [e for e in report.entries if e.match_key and e.match_key.product_ref == "M-2"
          and e.diffs]
    assert len(m2) == 1
    assert m2[0].diffs[0].kind == "regular"
    assert m2[0].write_entry is None
    # ERP 缺失 → create_order_bundle（D21 场景 9 执行标记）
    miss = [e for e in report.entries if e.classification == "erp_missing"]
    assert miss[0].write_entry == "create_order_bundle"
    # 歧义：候选订单 id 51/52 在消息中，绝不自动选
    amb = [e for e in report.entries if e.classification == "ambiguous"][0]
    assert amb.matched_order_id is None
    assert "51" in amb.message and "52" in amb.message
    # 任务收口 succeeded（只读任务 PENDING 直接执行）
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 审计：customers 查询 1 次 + metal_orders fetch_all 1 页（11.2）
    results = actx[2].events(task_id=task.task_id, event_type="result")
    assert len(results) == 2


def test_full_flow_zero_write(erp, actx):
    """零写实证：执行期间只发出 query 调用（无 create/update/create_order_bundle/
    update_order），差异报告直接生成只读（执行门槛 L870）。"""
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    reconciler.execute(task_id=task.task_id, plan=make_plan())
    assert erp.calls, "对账必须查询 ERP"
    assert all(c.get("entity") in {"customers", "metal_orders", "powder_orders",
                                   "suppliers"} for c in erp.calls)
    # 所有调用都是 query 实体读取：无任何写工具名出现（假服务无写实现，
    # 若编排层尝试写调用会 METHOD_NOT_FOUND → 任务 failed）
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_customer_unmatched_expressed_and_skips_order_query(erp, actx):
    """客户名称无法唯一匹配 → 报告表达不阻断（任务书口径），跳过订单查询，
    无写路径；任务仍 succeeded。"""
    _, tasks, _, reconciler, _, _, _ = actx
    calls_before = len(erp.calls)
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(
        task_id=task.task_id, plan=make_plan(subject_name="不存在的客户")
    )
    assert report.subject_match.status == "not_found"
    assert report.summary.get("customer_unmatched") == 6
    assert report.entries[0].write_entry is None
    assert any("主体名称未唯一匹配" in n for n in report.limitations)
    # 只查询了 customers，未查 metal_orders
    entities = [c.get("entity") for c in erp.calls[calls_before:]]
    assert entities == ["customers"]
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


def test_ambiguous_customer_never_auto_selected(erp, actx):
    """客户名称同名（ambiguous）→ 全部候选表达，不自动选、不查询订单。"""
    # 同名客户需同时给出 company_name（_match_subject 按该字段取候选名，
    # 见 domains/reconciliation.py 步骤 4a）与 name（demo 契约兼容字段）
    erp.rows["customers"].append(
        {"id": 201, "name": "示例五金", "company_name": "示例五金", "region": "华北"}
    )
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(task_id=task.task_id, plan=make_plan())
    assert report.subject_match.status == "ambiguous"
    assert report.summary.get("customer_unmatched") == 6
    assert any("候选" in e.message for e in report.entries)


# ---- RV-28：解析与转换错误进报告 ----


def test_no_valid_rows_report_limitations(erp, actx):
    """Excel 全部为错误行 → 报告表达限制，不查询 ERP。"""
    xlsx = build_xlsx([("M-1", "2026-13-99", 3.00, 10, 12.50, "完成", "")])
    _, tasks, _, reconciler, _, _, _ = actx
    calls_before = len(erp.calls)
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(task_id=task.task_id, plan=make_plan(excel_bytes=xlsx))
    assert report.row_count == 0
    assert len(report.conversion_errors) == 1
    assert any("没有可匹配的有效数据行" in n for n in report.limitations)
    assert erp.calls[calls_before:] == []  # 无有效行不触网
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


# ---- 契约与失败路径 ----


def test_mapping_not_published_fails_task(erp, actx):
    """无 published 映射 → MappingNotPublishedError + 任务 failed
    （6.3 规则 1 只使用 published，宁可报错不猜测）。"""
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    with pytest.raises(MappingNotPublishedError):
        reconciler.execute(
            task_id=task.task_id, plan=make_plan(template_key="no-such-key")
        )
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_invalid_entity_rejected(erp, actx):
    """实体不在 R6 允许集 → 拒绝 + 任务 failed（fail closed）。"""
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    with pytest.raises(ValueError):
        reconciler.execute(
            task_id=task.task_id, plan=make_plan(entity="attendance")
        )
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


def test_erp_failure_marks_failed_and_preserves_error_code(erp, actx):
    """ERP 查询失败 → MCPError 原样上抛 + 任务 failed（错误码沿用既有体系）。"""
    erp.fail_next.append(("ERP_BUSY", "server busy"))
    from administrative_agent.mcp.errors import MCPError

    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    with pytest.raises(MCPError) as exc:
        reconciler.execute(task_id=task.task_id, plan=make_plan())
    assert exc.value.code == "ERP_BUSY"
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED


# ---- 数据完整性 ----


def test_drift_marks_partial_with_limitations(erp, actx):
    """分页期间数据变化 → data_completeness=partial + limitations 显式说明
    （6.2 第 4 条口径沿用到对账）。"""
    erp.drift_at_call["metal_orders"] = 2
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(
        task_id=task.task_id, plan=make_plan(page_limit=4)
    )
    assert report.data_completeness == "partial"
    assert any("数据发生变化" in n or "不完整" in n for n in report.limitations)


# ---- 塑粉（L845：update_order 标记） ----


POWDER_HEADER = ("色号", "日期", "重量", "单价", "备注")

POWDER_COLUMNS = [
    {"excel_column": "色号", "target_field": "color_code", "data_type": "string",
     "is_required": True},
    {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
     "is_required": True},
    {"excel_column": "重量", "target_field": "total_weight", "data_type": "decimal"},
    {"excel_column": "单价", "target_field": "price_per_kg", "data_type": "decimal"},
    {"excel_column": "备注", "target_field": "notes", "data_type": "string",
     "default_value": ""},
]


def test_powder_flow_marks_update_order(erp, actx):
    """塑粉对账：ERP 订单 total_weight 500 vs Excel 550 → 字段差异 +
    update_order 标记（L845，本单元只标记不执行）。"""
    _, tasks, _, _, query_exec, _, _ = actx
    # 塑粉映射（独立 template_key）发布
    powder_service = published_service(
        template_key="recon-powder", columns=POWDER_COLUMNS
    )
    recon = ReconciliationPlanExecutor(
        mapping_service=powder_service, query_executor=query_exec
    )
    task = create_task(tasks, duty="R6")
    xlsx = build_xlsx(
        [("RAL-9016", "2026-06-10", 550, 28.00, "")], header=POWDER_HEADER
    )
    report = recon.execute(
        task_id=task.task_id,
        plan=ReconciliationPlan(
            template_key="recon-powder",
            entity="powder_orders",
            subject_name="江城塑粉厂",
            excel_bytes=xlsx,
            period_note="2026 年 6 月",
        ),
    )
    assert report.subject_match.status == "matched"
    assert report.subject_match.entity_id == 5  # 江城塑粉厂
    entry = report.entries[0]
    assert entry.classification == "field_diff"
    assert entry.diffs[0].field == "total_weight"
    assert entry.diffs[0].excel_value == "550"
    assert entry.diffs[0].erp_value == "500"
    assert entry.write_entry == "update_order"  # L845
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED


# ---- 报告结构（D20/D21 复用口） ----


def test_report_dict_shape_for_review_and_execution(erp, actx):
    """差异报告结构供 D20 复核页与 D21 执行复用：entries 携带行号/匹配键/
    订单 id/差异前后值/write_entry。"""
    _, tasks, _, reconciler, _, _, _ = actx
    task = create_task(tasks, duty="R6")
    report = reconciler.execute(task_id=task.task_id, plan=make_plan())
    d = report.to_dict()
    assert set(d) >= {
        "task_id", "template_key", "mapping_version", "source_file_hash",
        "entity", "subject_match", "period_note", "row_count",
        "conversion_errors", "entries", "summary", "data_completeness",
        "limitations",
    }
    entry = next(e for e in d["entries"] if e["classification"] == "field_diff")
    assert entry["row_number"] == 3  # M-2（表头 1 + 第 2 数据行）
    assert entry["match_key"]["product_ref"] == "M-2"
    assert entry["matched_order_id"] == 2
    assert entry["diffs"][0]["excel_value"] == "4"  # Excel 数值 4.00 原样读取
    assert entry["diffs"][0]["erp_value"] == "3.00"
