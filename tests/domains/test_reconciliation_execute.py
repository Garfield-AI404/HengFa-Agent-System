"""D21 R6 复核后写入执行测试（6.4 步骤 11-14、7.2 L871、10.2
R6.reconciliation.execute 规则包四句、5.4 规则 6/7 L771、11.2 L1225-1229、
14.3 场景 9/10/11、14.1 L1380、RV-31/RV-32）。

覆盖（任务书测试要求）：
- 前置门禁：无有效复核（未批准/已拒绝/已失效/hash 变化）→ 拒绝执行 +
  零写实证（唯一写入入口，无绕过路径）+ 任务 failed；
- 映射版本检查：published 版本 ≠ 计划 mapping_version → 拒绝执行；
- 创建缺失订单（场景 9 主线）：写前缺失确认 → create_order_bundle →
  成功/重放/冲突/IN_PROGRESS/网络未知同 key 重试（场景 11）→ 最终重查；
- 塑粉缺失不创建（D19 决策 e 防御再拒）+ 载荷缺必需字段转人工（不静默）；
- 更新已有订单：零重量可更新（L843）→ update_order + 金额重算；写前
  查询值 vs 复核时值变化（场景 10 不覆盖新数据）；调用瞬间版本冲突 →
  重新查询重新生成差异；网络未知 → 重查确认（已应用 success / 未应用
  failed）；写前已一致 → 不调用写工具；
- 审计逐条：每次实际调用独立 attempt（网络重试不覆盖成一条，11.2
  规则 3）；attempt 记录目标/字段差异/幂等键或 expected_updated_at；
- 汇总收口：全部成功 succeeded（仅验证路径）；部分成功保持
  running(awaiting_recovery)（D12 先例）+ 成功/失败/冲突计数不假报；
  全部失败 failed；
- 步骤 14 最终重查：每单 matched/未补建（塑粉）输出；
- 任务必须 running 才执行（8.1/13.3 兜底）。

复用 tests/domains/conftest.py 的 FakeOrdersErp（D5 规则：测试夹具，
不代表 ERP 真实运行态）——本文件扩展多字段 query filter（create 缺失
确认查询按匹配键过滤）与 InMemoryReviewRepository（test_reviews）。
"""

from __future__ import annotations

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

from administrative_agent.agent.prompts.policies import (    policy_for,)
from administrative_agent.audit.service import (  # noqa: E402
    EVENT_MCP_RETRY,
    AuditService,
)
from administrative_agent.business.matching import MatchOutcome  # noqa: E402
from administrative_agent.business.reconciliation_plan import (  # noqa: E402
    WritePlan,
    WritePlanItem,
)
from administrative_agent.business.reconciliation_rules import (  # noqa: E402
    DIFF_KIND_ZERO_WEIGHT_UPDATABLE,
    ERP_MISSING,
    FIELD_DIFF,
    WRITE_CREATE_ORDER_BUNDLE,
    WRITE_UPDATE_ORDER,
    DiffEntry,
    FieldDiff,
    MatchKey,
)
from administrative_agent.config import Settings  # noqa: E402
from administrative_agent.domains.query import QueryExecutor  # noqa: E402
from administrative_agent.domains.reconciliation import (  # noqa: E402
    ERROR_EXECUTE_ALL_FAILED,
    ERROR_EXECUTE_CREATE_FIELDS_INCOMPLETE,
    ERROR_EXECUTE_CREATE_PAYLOAD_MISSING,
    ERROR_EXECUTE_MAPPING_VERSION_CHANGED,
    ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW,
    ERROR_EXECUTE_PARTIAL,
    ERROR_EXECUTE_POWDER_CREATE_REJECTED,
    ERROR_EXECUTE_REVIEW_INVALID,
    ITEM_CONFLICT,
    ITEM_FAILED,
    ITEM_REPLAYED,
    ITEM_SUCCESS,
    ITEM_UNCHANGED,
    ReconciliationExecuteError,
    ReconciliationExecuteExecutor,
    ReconciliationReport,
)
from administrative_agent.domains.reviews import ReviewService  # noqa: E402
from administrative_agent.files.mapping_service import (  # noqa: E402
    ExcelMappingService,
)
from administrative_agent.mcp.client import MCPClient  # noqa: E402
from administrative_agent.mcp.metadata import EntityCache  # noqa: E402
from administrative_agent.tasks.machine import TaskStatus  # noqa: E402
from administrative_agent.tasks.service import (  # noqa: E402
    TaskNotRunningError,
    TaskService,
)
from tests.audit.conftest import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryAuditRepository,
)
from tests.domains.conftest import (  # noqa: E402
    ACTOR_ACCOUNT,
    ACTOR_ROLE,
    ERP_MCP_URL,
    TRACE_ID,
    FakeOrdersErp,
)
from tests.conftest import mcp_max_retries  # noqa: E402
from tests.domains.test_mapping_service import InMemoryMappingStore  # noqa: E402
from tests.domains.test_query import create_task  # noqa: E402
from tests.domains.test_reviews import InMemoryReviewRepository  # noqa: E402
from tests.tasks.conftest import InMemoryTaskRepository  # noqa: E402

#: 固定幂等键（测试注入 key_factory；真实运行由 generate_idempotency_key）
KEY_D21 = "d21-test-key-000001"

_SHA = "sha256-d21-demo"
_ENTITY = "metal_orders"
#: 复核时（D19 差异）写入字段值
_FIELD_DIFFS = (
    {"field": "weight_per_unit", "excel_value": "5.00", "erp_value": "0",
     "kind": DIFF_KIND_ZERO_WEIGHT_UPDATABLE},
)

#: 标准行（row_number → D19 解析后的映射 target_field 值；M-999 缺失行）
CREATE_ROW = {
    2: {
        "model": "M-999", "in_date": "2026-06-30", "weight_per_unit": "2.00",
        "quantity": "5", "unit_price": "10.00", "status": "完成", "notes": "",
        "price_type": "含税", "weight_unit": "kg",
    }
}


class MultiFieldQueryErp(FakeOrdersErp):
    """D21 扩展：query 支持匹配键组合过滤（写前缺失确认按
    customer_id+model+in_date 查询；D5 规则：测试夹具）。"""

    def _dispatch(self, name: str, args: dict) -> object:
        if name == "query":
            self.calls.append(args)
            entity = args.get("entity")
            filt = args.get("filter") or {}
            items = [
                dict(o) for o in self.orders.values()
                if all(str(o.get(k)) == str(v) for k, v in filt.items())
            ]
            fields = args.get("fields")
            if fields:
                items = [
                    {k: v for k, v in it.items() if k in fields} for it in items
                ]
            return self._ok(
                {
                    "items": items, "total": len(items),
                    "limit": args.get("limit", 50), "offset": args.get("offset", 0),
                    "entity": entity,
                }
            )
        return super()._dispatch(name, args)


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


def published_service():
    service = ExcelMappingService(InMemoryMappingStore())
    result = service.import_draft(
        actor="manager01", template_key="recon-metal", sheet_name="对账单",
        header_row=1, source_file_hash="sha256-mapping",
        columns=[
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
            {"excel_column": "价格类型", "target_field": "price_type",
             "data_type": "enum", "enum_mapping": {"含税": "含税", "不含税": "不含税"}},
            {"excel_column": "重量单位", "target_field": "weight_unit",
             "data_type": "enum", "enum_mapping": {"kg": "kg", "m²": "m²"}},
        ],
    )
    service.publish(actor="manager01", version_id=result.version.version_id)
    return service


def match_outcome() -> MatchOutcome:
    return MatchOutcome(
        ref="示例五金", status="matched", entity_id=101,
        matched_name="示例五金", candidates=((101, "示例五金"),),
    )


def create_diff_entry(*, row_number: int = 2) -> DiffEntry:
    return DiffEntry(
        classification=ERP_MISSING,
        row_number=row_number,
        source_file_hash=_SHA,
        mapping_version=1,
        match_key=MatchKey(
            entity=_ENTITY, subject_id=101, product_ref="M-999",
            in_date="2026-06-30",
        ),
        matched_order_id=None,
        write_entry=WRITE_CREATE_ORDER_BUNDLE,
    )


def update_diff_entry(*, row_number: int = 1, erp_value: str = "0",
                      excel_value: str = "5.00") -> DiffEntry:
    return DiffEntry(
        classification=FIELD_DIFF,
        row_number=row_number,
        source_file_hash=_SHA,
        mapping_version=1,
        match_key=MatchKey(entity=_ENTITY, subject_id=101, product_ref="M-ZW",
                           in_date="2026-06-20"),
        matched_order_id=11,
        diffs=(
            FieldDiff(field="weight_per_unit", excel_value=excel_value,
                      erp_value=erp_value,
                      kind=DIFF_KIND_ZERO_WEIGHT_UPDATABLE),
        ),
        write_entry=WRITE_UPDATE_ORDER,
    )


def make_report(*entries: DiffEntry) -> ReconciliationReport:
    return ReconciliationReport(
        task_id="task-d21-report",
        template_key="recon-metal",
        mapping_version=1,
        source_file_hash=_SHA,
        entity=_ENTITY,
        subject_match=match_outcome(),
        period_note="2026 年 6 月",
        row_count=len(entries),
        conversion_errors=(),
        entries=tuple(entries),
        data_completeness="complete",
    )


def make_write_plan(task_id: str, items: list[WritePlanItem]) -> WritePlan:
    return WritePlan(
        task_id=task_id,
        template_key="recon-metal",
        mapping_version=1,
        source_file_hash=_SHA,
        entity=_ENTITY,
        items=tuple(items),
    )


def create_item() -> WritePlanItem:
    return WritePlanItem(
        row_number=2, classification=ERP_MISSING,
        write_path=WRITE_CREATE_ORDER_BUNDLE, matched_order_id=None,
        fields=(), diffs=(),
    )


def update_item(*, weight: str = "5.00", erp_weight: str = "0") -> WritePlanItem:
    """update 条目：fields=期望值、diffs=复核时（D19）前后值。

    erp_weight 需与复核时 ERP 值一致——不一致会触发场景 10 前置检查
    （复核后变化不覆盖），由各测试显式控制。
    """
    diffs = (
        {"field": "weight_per_unit", "excel_value": weight,
         "erp_value": erp_weight, "kind": DIFF_KIND_ZERO_WEIGHT_UPDATABLE},
    )
    return WritePlanItem(
        row_number=1, classification=FIELD_DIFF,
        write_path=WRITE_UPDATE_ORDER, matched_order_id=11,
        fields=(("weight_per_unit", weight),), diffs=diffs,
    )


def seed_update_order(erp: MultiFieldQueryErp) -> None:
    """ERP 已有订单 id=11：M-ZW 原重量 0（L843 零重量可更新素材）。"""
    erp.seed_update_order(
        11,
        {
            "customer_id": 101, "model": "M-ZW", "status": "未完成",
            "unit_price": "12.50", "quantity": "10", "weight_per_unit": "0",
            "weight_unit": "kg", "price_type": "含税", "in_date": "2026-06-20",
            "out_date": None, "notes": "",
        },
    )


@pytest.fixture
def erp() -> MultiFieldQueryErp:
    fake = MultiFieldQueryErp()
    seed_update_order(fake)
    return fake


@pytest.fixture
def rctx(erp: MultiFieldQueryErp):
    """组装 D21 执行上下文。yield (erp, tasks, audit, reviews, exec, mappings)。"""
    with respx.mock:
        respx.post(ERP_MCP_URL).mock(side_effect=erp.handler)
        settings = Settings()
        client = MCPClient(settings)
        try:
            cache = EntityCache(client)
            tasks = TaskService(InMemoryTaskRepository())
            audit = AuditService(InMemoryAuditRepository(), InMemoryAttemptRepository())
            query_exec = QueryExecutor(
                client=client, cache=cache, tasks=tasks, audit=audit,
                trace_id=TRACE_ID, actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE, duty_code="R6",
            )
            mappings = published_service()
            reviews = ReviewService(
                review_repo=InMemoryReviewRepository(), tasks=tasks, audit=audit
            )
            executor = ReconciliationExecuteExecutor(
                client=client, mapping_service=mappings,
                query_executor=query_exec, tasks=tasks, audit=audit,
                trace_id=TRACE_ID, actor_account=ACTOR_ACCOUNT,
                actor_role=ACTOR_ROLE, policy=policy_for("R6.reconciliation.execute"),
                key_factory=lambda: KEY_D21,
            )
            yield erp, tasks, audit, reviews, executor, mappings
        finally:
            client.close()


def approved_review(rctx, tasks, reviews, plan) -> object:
    """发起 + 批准复核（approve → 任务 running，D21 可执行）。"""
    review = reviews.request_review(
        task_id=plan.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    return reviews.approve(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
        expected_payload_hash=plan.payload_hash,
    )


def run_execute(rctx, *, task=None, items=None, report=None, rows=CREATE_ROW,
                plan=None):
    """组装并执行：任务（running）→ 复核（approved）→ execute。"""
    _, tasks, _, reviews, executor, _ = rctx
    if task is None:
        task = create_task(tasks, duty="R6", is_write=True)
    if plan is None:
        plan = make_write_plan(task.task_id, items)
    review = approved_review(rctx, tasks, reviews, plan)
    summary = executor.execute(
        task_id=task.task_id, plan=plan, review=review, report=report, rows=rows
    )
    return task, plan, review, summary


def write_calls(erp: MultiFieldQueryErp) -> list[dict]:
    """写工具调用（create_order_bundle/update_order）断言辅助。"""
    return [
        c for c in erp.calls
        if c.get("order") is not None or "changes" in c
    ]


def write_attempts(audit, op_id: str) -> list:
    """写调用 attempt（create/update；查询 attempt 为 target_type=query，
    共用 operation_id，按 target_type 区分）。"""
    return [a for a in audit.attempts(op_id) if a.target_type == "metal_order"]


def write_results(audit, task_id: str, op_id: str) -> list:
    """写调用对应 result 事件（按 attempt_no 匹配）。"""
    nos = {a.attempt_no for a in write_attempts(audit, op_id)}
    return [
        r for r in audit.events(task_id=task_id, event_type="result")
        if r.structured_payload.get("attempt_no") in nos
    ]


# ---------------------------------------------------------------------------
# 前置门禁（规则包第 1/3 句；RV-30 通过标准：唯一写入入口无绕过）
# ---------------------------------------------------------------------------


def test_gate_rejects_invalidated_review_zero_write(rctx):
    """已失效复核（7.4 步骤 1/2：复核后计划业务变化）→ 门禁拒绝 +
    任务 failed + 零写实证（唯一写入入口，无绕过路径，RV-30 通过标准）。"""
    erp, tasks, _, reviews, executor, _ = rctx
    task = create_task(tasks, duty="R6", is_write=True)
    plan = make_write_plan(task.task_id, [create_item(), update_item()])
    # 先批准（任务 running）再失效（7.4 步骤 1/2：计划业务变化）
    review = approved_review(rctx, tasks, reviews, plan)
    reviews.invalidate(
        review_id=review.review_id, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    # execute 接收调用方（路由层）从仓储读取的最新复核记录
    latest = reviews.get_review(review.review_id)
    calls_before = len(erp.calls)
    with pytest.raises(ReconciliationExecuteError) as exc:
        executor.execute(task_id=task.task_id, plan=plan, review=latest,
                         report=make_report(create_diff_entry()))
    assert exc.value.error_code == ERROR_EXECUTE_REVIEW_INVALID
    assert "已失效" in exc.value.message
    # 任务 failed（流程错误终止，需人工处理）
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert tasks.get_task(task.task_id).final_error_code == ERROR_EXECUTE_REVIEW_INVALID
    # 零写：执行期间没有任何写工具调用
    assert erp.calls[calls_before:] == []


def test_rejected_review_task_cannot_execute(rctx):
    """已拒绝复核（8.1：复核拒绝后终止）→ 任务 cancelled 终态，执行器
    状态兜底拒绝（TaskNotRunningError），写入不可能发生。"""
    erp, tasks, _, reviews, executor, _ = rctx
    task = create_task(tasks, duty="R6", is_write=True)
    plan = make_write_plan(task.task_id, [create_item()])
    review = reviews.request_review(
        task_id=task.task_id, plan=plan, actor_account=ACTOR_ACCOUNT,
        actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    reviews.reject(
        review_id=review.review_id, rejection_reason="差异有误，重新对账",
        actor_account=ACTOR_ACCOUNT, actor_role=ACTOR_ROLE, trace_id=TRACE_ID,
    )
    calls_before = len(erp.calls)
    with pytest.raises(TaskNotRunningError):
        executor.execute(task_id=task.task_id, plan=plan, review=review,
                         report=make_report(create_diff_entry()))
    assert tasks.get_task(task.task_id).status == TaskStatus.CANCELLED
    assert erp.calls[calls_before:] == []


def test_gate_rejects_hash_changed_plan(rctx):
    """复核后计划变化（payload hash 不一致）→ 拒绝执行（RV-30 通过标准）。"""
    erp, tasks, _, reviews, executor, _ = rctx
    task = create_task(tasks, duty="R6", is_write=True)
    plan_old = make_write_plan(task.task_id, [create_item()])
    # 旧复核批准后计划又变化（新条目）→ 原复核不适用于新计划
    approved_review(rctx, tasks, reviews, plan_old)
    plan_new = make_write_plan(
        task.task_id, [create_item(), update_item(weight="8.00")]
    )
    calls_before = len(erp.calls)
    with pytest.raises(ReconciliationExecuteError) as exc:
        executor.execute(task_id=task.task_id, plan=plan_new,
                         review=reviews.list_reviews()[0][0],
                         report=make_report(create_diff_entry()))
    assert exc.value.error_code == ERROR_EXECUTE_REVIEW_INVALID
    assert "hash" in exc.value.message
    assert erp.calls[calls_before:] == []


def test_gate_rejects_mapping_version_changed(rctx):
    """published 映射版本 ≠ 计划 mapping_version → 拒绝执行（规则包第 3 句）。"""
    erp, tasks, _, reviews, executor, mappings = rctx
    # 执行时映射已发布 v2（计划基于 v1）：v1 retire 后发布 v2
    published = mappings.get_published("recon-metal")
    mappings.retire(actor="manager01", version_id=published.version_id)
    result = mappings.import_draft(
        actor="manager01", template_key="recon-metal", sheet_name="对账单",
        header_row=1, source_file_hash="sha256-mapping-v2",
        columns=[
            {"excel_column": "型号", "target_field": "model", "data_type": "string",
             "is_required": True},
            {"excel_column": "日期", "target_field": "in_date", "data_type": "date",
             "is_required": True},
        ],
    )
    mappings.publish(actor="manager01", version_id=result.version.version_id)
    task = create_task(tasks, duty="R6", is_write=True)
    plan = make_write_plan(task.task_id, [create_item()])  # 计划基于 v1
    review = approved_review(rctx, tasks, reviews, plan)
    calls_before = len(erp.calls)
    with pytest.raises(ReconciliationExecuteError) as exc:
        executor.execute(task_id=task.task_id, plan=plan, review=review,
                         report=make_report(create_diff_entry()))
    assert exc.value.error_code == ERROR_EXECUTE_MAPPING_VERSION_CHANGED
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert erp.calls[calls_before:] == []


# ---------------------------------------------------------------------------
# 创建缺失订单（6.4 步骤 12 create_order_bundle；14.3 场景 9/11）
# ---------------------------------------------------------------------------


def test_create_missing_order_succeeds_and_final_recheck(rctx):
    """场景 9 主线：写前缺失确认 → create_order_bundle → 成功 + 最终重查。"""
    erp, tasks, audit, _, _, _ = rctx
    task, plan, review, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry())
    )
    # 创建载荷：匹配键 + 行值 + 创建固定值（5.1/defaults）
    orders = [
        o for o in erp.orders.values()
        if o.get("customer_id") == 101 and o.get("model") == "M-999"
    ]
    assert len(orders) == 1
    order = orders[0]
    assert order["in_date"] == "2026-06-30"
    assert order["weight_per_unit"] == "2.00"
    assert order["quantity"] == "5"
    assert order["unit_price"] == "10.00"
    assert order["price_type"] == "含税"
    assert order["weight_unit"] == "kg"
    assert order["status"] == "未完成"  # 创建固定未完成（5.1，不采用行值）
    assert order["quantity_unit"] == "套"  # defaults 默认
    # 幂等键持久化（attempt 记录 idempotency_key）
    assert summary.items[0].status == ITEM_SUCCESS
    assert summary.items[0].idempotency_key == KEY_D21
    # 任务 succeeded（8.1 只能经验证路径）
    assert summary.task_status == "succeeded"
    assert summary.verification_passed is True
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    # 步骤 14：最终重查 matched + 订单 id 回填
    final = summary.final_recheck[0]
    assert final.matched is True
    assert final.order_id == order["id"]
    # 写前缺失确认（filter 多字段）+ 最终重查
    query_calls = [
        c for c in erp.calls if "filter" in c
        and c.get("filter", {}).get("model") == "M-999"
    ]
    assert len(query_calls) == 2  # 写前缺失确认 + 最终重查
    # 审计：写调用 attempt（含幂等键）+ 对应 result
    attempts = write_attempts(audit, task.operation_id)
    assert len(attempts) == 1
    assert attempts[0].idempotency_key == KEY_D21
    assert attempts[0].target_id == "101"  # 客户 id（创建目标定位）
    assert len(write_results(audit, task.task_id, task.operation_id)) == 1


def test_create_network_unknown_same_key_replay_scenario11(rctx):
    """场景 11：结果未知 → 同一幂等键重试 → 重放确认，不重复创建。"""
    erp, tasks, audit, _, _, _ = rctx
    erp.fail_network_unknown(KEY_D21)
    task, _, _, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry())
    )
    assert summary.items[0].status == ITEM_REPLAYED
    assert summary.replayed_count == 1
    assert summary.task_status == "succeeded"
    # 只创建了 1 个订单（重放不重复创建）
    created = [
        o for o in erp.orders.values()
        if o.get("customer_id") == 101 and o.get("model") == "M-999"
    ]
    assert len(created) == 1
    # 2026-08-29 口径（11.2 规则 3 可追溯性）：task_attempts 一行 = 业务层
    # 一次逻辑调用。瞬时重试已在 MCP 协议层内部完成（2026-08-27 批准），
    # 不计入业务 attempt，改由持久化审计事件 event_type=mcp_retry 留痕——
    # 连接失败属"结果未知"（13.1），写操作是否执行一度不可知，不能只留在
    # 有界 500 条且进程内存的结构化日志环形缓冲里。
    attempts = write_attempts(audit, task.operation_id)
    assert len(attempts) == 1
    assert attempts[0].idempotency_key == KEY_D21
    retries = audit.events(task_id=task.task_id, event_type=EVENT_MCP_RETRY)
    assert len(retries) == 1
    assert retries[0].operation_id == task.operation_id
    assert retries[0].structured_payload["tool"] == "create_order_bundle"
    assert retries[0].structured_payload["error_code"] == "MCP_CONNECT_FAILED"
    # 两次都是 create_order_bundle 调用（同 key）
    calls = write_calls(erp)
    assert len(calls) == 2
    assert all(c.get("idempotency_key") == KEY_D21 for c in calls)


def test_create_key_conflict_rejected_no_overwrite(rctx):
    """幂等键已用于不同载荷 → 冲突：不创建、不改原记录（13.1）。"""
    erp, tasks, _, _, _, _ = rctx
    erp.seed_order(KEY_D21, {"customer_id": 999, "model": "OTHER"})
    calls_before = len(erp.calls)
    task, _, _, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry())
    )
    item = summary.items[0]
    assert item.status == ITEM_FAILED
    assert item.error_code == "ORDER_IDEMPOTENCY_KEY_CONFLICT"
    assert summary.task_status == "failed"
    assert summary.failed_count == 1
    # 原 key 记录未被触碰（key 仍绑定原载荷/原订单）
    assert erp.orders[erp.keys[KEY_D21]["order_id"]]["customer_id"] == 999
    # 只调用了 1 次 create（冲突即停，不重试不同载荷）
    assert len(write_calls(erp)) == 1
    # 条目级错误码（ORDER_IDEMPOTENCY_KEY_CONFLICT）已在上方断言；
    # 任务级收口为全部失败（R6_EXECUTE_ALL_FAILED，不假报成功）
    assert tasks.get_task(task.task_id).status == TaskStatus.FAILED
    assert tasks.get_task(task.task_id).final_error_code == ERROR_EXECUTE_ALL_FAILED


def test_create_in_progress_requires_operator_action(rctx):
    """IDEMPOTENCY_IN_PROGRESS：不得自动重试、不得换 key 绕过。"""
    erp, tasks, _, _, _, _ = rctx
    erp.fail_in_progress(KEY_D21)
    _, _, _, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry())
    )
    item = summary.items[0]
    assert item.status == ITEM_FAILED
    assert item.error_code == "ORDER_IDEMPOTENCY_IN_PROGRESS"
    assert item.requires_operator_action is True
    assert summary.task_status == "failed"


def test_create_missing_required_fields_goes_manual(rctx):
    """载荷缺必需字段（无 price_type）→ 不静默选默认，转人工（R2 语义）。"""
    erp, tasks, _, _, _, _ = rctx
    rows = {
        2: {
            "model": "M-999", "in_date": "2026-06-30", "weight_per_unit": "2.00",
            "quantity": "5", "unit_price": "10.00", "status": "完成",
            "notes": "", "weight_unit": "kg",  # 缺 price_type
        }
    }
    calls_before = len(erp.calls)
    _, _, _, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry()), rows=rows
    )
    item = summary.items[0]
    assert item.status == ITEM_FAILED
    assert item.error_code == ERROR_EXECUTE_CREATE_FIELDS_INCOMPLETE
    assert "price_type" in item.error_summary
    # 无任何 create 调用（fail closed 不编造）
    assert erp.calls[calls_before:] and all(
        "order" not in c for c in erp.calls[calls_before:]
    )


def test_create_missing_row_data_goes_manual(rctx):
    """标准行缺失（rows 无该行）→ 不编造载荷，转人工。"""
    _, tasks, _, _, _, _ = rctx
    _, _, _, summary = run_execute(
        rctx, items=[create_item()], report=make_report(create_diff_entry()), rows=None
    )
    item = summary.items[0]
    assert item.status == ITEM_FAILED
    assert item.error_code == ERROR_EXECUTE_CREATE_PAYLOAD_MISSING
    assert tasks.get_task(summary.task_id).status == TaskStatus.FAILED


def test_create_powder_missing_not_built(rctx):
    """塑粉缺失订单不补建（D19 决策 e）：条目拒绝 + 最终核对列示未补建。"""
    erp, tasks, _, _, executor, mappings = rctx
    # 塑粉映射发布（映射版本检查需要 published）
    result = mappings.import_draft(
        actor="manager01", template_key="recon-powder", sheet_name="对账单",
        header_row=1, source_file_hash="sha256-mapping-powder",
        columns=[
            {"excel_column": "色号", "target_field": "color_code",
             "data_type": "string", "is_required": True},
            {"excel_column": "日期", "target_field": "in_date",
             "data_type": "date", "is_required": True},
            {"excel_column": "重量", "target_field": "total_weight",
             "data_type": "decimal"},
            {"excel_column": "单价", "target_field": "price_per_kg",
             "data_type": "decimal"},
        ],
    )
    mappings.publish(actor="manager01", version_id=result.version.version_id)
    task = create_task(tasks, duty="R6", is_write=True)
    plan = WritePlan(
        task_id=task.task_id, template_key="recon-powder",
        mapping_version=1, source_file_hash=_SHA, entity="powder_orders",
        items=(
            WritePlanItem(row_number=3, classification=ERP_MISSING,
                          write_path=WRITE_CREATE_ORDER_BUNDLE,
                          matched_order_id=None, fields=(), diffs=()),
        ),
    )
    reviews = rctx[3]
    review = approved_review(rctx, tasks, reviews, plan)
    entry = DiffEntry(
        classification=ERP_MISSING, row_number=3, source_file_hash=_SHA,
        mapping_version=1,
        match_key=MatchKey(entity="powder_orders", subject_id=5,
                           product_ref="RAL-9016", in_date="2026-06-10"),
        write_entry=WRITE_CREATE_ORDER_BUNDLE,
    )
    report = ReconciliationReport(
        task_id=task.task_id, template_key="recon-powder", mapping_version=1,
        source_file_hash=_SHA, entity="powder_orders",
        subject_match=MatchOutcome(ref="江城塑粉厂", status="matched",
                                   entity_id=5, matched_name="江城塑粉厂",
                                   candidates=((5, "江城塑粉厂"),)),
        period_note="2026 年 6 月", row_count=1, conversion_errors=(),
        entries=(entry,), data_completeness="complete",
    )
    summary = executor.execute(
        task_id=task.task_id, plan=plan, review=review, report=report, rows={}
    )
    assert summary.items[0].status == ITEM_FAILED
    assert summary.items[0].error_code == ERROR_EXECUTE_POWDER_CREATE_REJECTED
    # 最终核对列示"未补建（塑粉）"
    final = summary.final_recheck[0]
    assert final.matched is False
    assert "未补建（塑粉）" in final.note
    # 无任何写调用（塑粉拒绝发生在任何 ERP 交互之前，fail closed）
    assert write_calls(erp) == []


# ---------------------------------------------------------------------------
# 更新已有订单（6.4 步骤 12 update_order；L843/L845；14.3 场景 10）
# ---------------------------------------------------------------------------


def test_update_zero_weight_updatable_succeeds(rctx):
    """L843：原重量 0 可更新 → update_order 成功 + 金额重算（ERP 计算）。"""
    erp, tasks, audit, _, _, _ = rctx
    before = erp.orders[11]["updated_at"]
    task, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_SUCCESS
    assert item.write_path == WRITE_UPDATE_ORDER
    assert item.matched_order_id == 11
    # 写前快照（before_snapshot 原重量 0）
    assert item.before_snapshot["weight_per_unit"] == "0"
    # 更新后 + 金额重算（§7.2：10 × 5 × 12.50 = 625.00）
    assert erp.orders[11]["weight_per_unit"] == "5.00"
    assert erp.orders[11]["total_amount"] == "625.00"
    assert item.after_snapshot["total_amount"] == "625.00"
    # updated_at 版本推进（步骤 11 检查 updated_at）
    assert erp.orders[11]["updated_at"] != before
    assert item.expected_updated_at == before
    # 审计 attempt：目标/字段差异/expected_updated_at（11.2）
    attempts = write_attempts(audit, task.operation_id)
    assert len(attempts) == 1
    assert attempts[0].target_id == "11"
    assert attempts[0].expected_updated_at == before
    assert attempts[0].field_diffs == {"weight_per_unit": "5.00"}
    # 最终重查 matched
    assert summary.final_recheck[0].matched is True
    assert summary.task_status == "succeeded"


def test_update_already_consistent_no_write_call(rctx):
    """写前查询确认当前值已符合期望 → 不调用 update_order（5.4 规则 2）。"""
    erp, tasks, audit, _, _, _ = rctx
    # 复核时与当前均为 5.00（与期望一致；写前查询确认无需更新）
    erp.orders[11]["weight_per_unit"] = "5.00"
    calls_before = len(erp.calls)
    task, _, _, summary = run_execute(
        rctx, items=[update_item(weight="5.00", erp_weight="5.00")]
    )
    item = summary.items[0]
    assert item.status == ITEM_UNCHANGED
    assert "无需更新" in item.note
    assert len(write_calls(erp)) == 0
    assert erp.calls[calls_before:], "写前查询必须发生（重新查询规则）"
    # 无写 attempt（未调用写工具，11.2 只记录实际调用）
    assert write_attempts(audit, task.operation_id) == []
    assert summary.final_recheck[0].matched is True
    assert summary.task_status == "succeeded"


def test_update_version_conflict_regenerates_diffs_no_overwrite(rctx):
    """场景 10：调用瞬间版本冲突 → 重查重生成差异，不覆盖新数据。"""
    erp, tasks, _, _, _, _ = rctx
    # 他人已在调用瞬间把重量改为 8.00
    erp.fail_version_conflict_at_call(11, {"weight_per_unit": "8.00"})
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_CONFLICT
    assert item.error_code == "ORDER_UPDATE_VERSION_CONFLICT"
    # 重新生成的差异（5.4 规则 6：期望 5.00 vs 当前 8.00）
    assert item.refetched_changes == {"weight_per_unit": "5.00"}
    assert item.after_snapshot["weight_per_unit"] == "8.00"
    # 新数据未被覆盖
    assert erp.orders[11]["weight_per_unit"] == "8.00"
    # 只调用 1 次 update_order（冲突后不盲目重试）
    assert len(write_calls(erp)) == 1
    # 14.1 联动：refetched_changes 输出供重新复核
    assert summary.regenerated_diffs == ({"weight_per_unit": "5.00"},)
    assert summary.conflict_count == 1
    assert summary.task_status == "failed"  # 全部失败（单条目）


def test_update_version_conflict_applied_by_other_success(rctx):
    """冲突后重查确认他人已应用期望值 → 按最新快照展示，不重复调用。"""
    erp, tasks, _, _, _, _ = rctx
    # 他人抢先更新的恰是期望值（5.00）
    erp.fail_version_conflict_at_call(11, {"weight_per_unit": "5.00"})
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_SUCCESS
    assert "不重复调用" in item.note
    assert item.refetched_changes == {}
    assert erp.orders[11]["weight_per_unit"] == "5.00"
    assert len(write_calls(erp)) == 1
    assert summary.task_status == "succeeded"


def test_update_changed_after_review_no_overwrite(rctx):
    """场景 10 前置：复核后目标已被他人更新 → 不覆盖，重新生成差异。"""
    erp, tasks, _, _, _, _ = rctx
    # 复核之后、执行之前，他人把重量改为 7.00（写前查询能发现）
    erp.apply_other_writer_update(11, {"weight_per_unit": "7.00"})
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_CONFLICT
    assert item.error_code == ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW
    assert item.refetched_changes == {"weight_per_unit": "5.00"}
    # 不覆盖新数据
    assert erp.orders[11]["weight_per_unit"] == "7.00"
    # 未调用 update_order（写前检查已拦截）
    assert len(write_calls(erp)) == 0
    assert summary.conflict_count == 1


def test_update_network_unknown_applied_success(rctx):
    """结果未知 → 重查确认已应用 → 按快照展示，不重复调用（规则 7）。

    断言口径（2026-08-29 调整）：2026-08-27 起传输层对连接失败做有限
    重试（项目负责人批准，mcp/client.py::_should_retry），业务层已看不到
    "结果未知"——同 operation_id 重试时 expected_updated_at 已陈旧，ERP
    按 mcp-api.md §7.2 行锁返回 ORDER_VERSION_CONFLICT，于是改走版本
    冲突重查分支。两条分支终态等价（ITEM_SUCCESS + 已应用快照 +
    "按最新快照展示，不重复调用"），故不再按分支文案与调用次数断言，改为
    断言真正要保证的不变量：
    - 变更确实生效（幂等，未重复叠加）；
    - 业务层只发起 1 次逻辑更新——所有 update_order 调用复用同一
      expected_updated_at（传输层重试同一请求，不计业务重试）；
    - 任务成功。
    """
    erp, tasks, _, _, _, _ = rctx
    erp.fail_network_unknown_update(11)
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_SUCCESS
    # 两条分支共用收尾语义（版本冲突重查 / 结果未知重查）
    assert "按最新快照展示" in item.note
    assert "不重复调用" in item.note
    assert erp.orders[11]["weight_per_unit"] == "5.00"
    # 同一次逻辑更新：所有调用复用同一 expected_updated_at
    update_calls = [c for c in erp.calls if "expected_updated_at" in c]
    assert update_calls
    assert len(update_calls) <= 1 + mcp_max_retries()
    assert len({c["expected_updated_at"] for c in update_calls}) == 1
    assert summary.task_status == "succeeded"


def test_update_network_unknown_not_applied_failed(rctx):
    """规则 7：结果未知 → 重查确认未应用 → failed，不盲目重试。"""
    erp, tasks, _, _, _, _ = rctx
    erp.fail_network_persistent_update(11)
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    item = summary.items[0]
    assert item.status == ITEM_FAILED
    assert item.error_code == "ORDER_UPDATE_NETWORK_UNKNOWN"
    assert "不盲目重试" in item.error_summary
    assert erp.orders[11]["weight_per_unit"] == "0"  # 未应用
    assert summary.task_status == "failed"


# ---------------------------------------------------------------------------
# 汇总收口（RV-32：逐条可追溯，不假报全量成功；部分成功 awaiting_recovery）
# ---------------------------------------------------------------------------


def test_partial_failure_keeps_running_awaiting_recovery(rctx):
    """部分成功（create 成功 + update 失败）→ running(awaiting_recovery)
    + 成功/失败计数逐条可追溯（D12 先例，不假报全量成功）。"""
    erp, tasks, _, _, _, _ = rctx
    erp.fail_network_persistent_update(11)
    task, _, _, summary = run_execute(
        rctx,
        items=[create_item(), update_item()],
        report=make_report(create_diff_entry(), update_diff_entry()),
    )
    assert summary.success_count == 1  # create
    assert summary.failed_count == 1   # update
    assert summary.conflict_count == 0
    # 已成功条目不因失败而回滚/重报
    statuses = [i.status for i in summary.items]
    assert statuses == [ITEM_SUCCESS, ITEM_FAILED]
    assert [i.error_code for i in summary.items][1] == "ORDER_UPDATE_NETWORK_UNKNOWN"
    # 任务保持 running（current_step=awaiting_recovery，D12 先例）
    assert summary.task_status == "running"
    assert summary.verification_passed is False
    assert summary.error_code == ERROR_EXECUTE_PARTIAL
    record = tasks.get_task(task.task_id)
    assert record.status == TaskStatus.RUNNING
    assert record.current_step == "awaiting_recovery"
    # create 已完成（幂等重放可恢复），update 未完成项继续处理
    assert erp.orders[11]["weight_per_unit"] == "0"


def test_all_failed_marks_task_failed(rctx):
    """全部条目失败 → 任务 failed（不假报成功）。"""
    erp, tasks, _, _, _, _ = rctx
    erp.fail_network_persistent_update(11)
    _, _, _, summary = run_execute(rctx, items=[update_item()])
    assert summary.task_status == "failed"
    assert summary.error_code == ERROR_EXECUTE_ALL_FAILED
    assert tasks.get_task(summary.task_id).status == TaskStatus.FAILED
    assert tasks.get_task(summary.task_id).final_error_code == ERROR_EXECUTE_ALL_FAILED


def test_empty_plan_succeeds_without_writes(rctx):
    """空计划（无写入条目）→ 直接 succeeded，无写调用。"""
    erp, tasks, _, _, _, _ = rctx
    calls_before = len(erp.calls)
    task, _, _, summary = run_execute(rctx, items=[])
    assert summary.success_count == 0
    assert summary.task_status == "succeeded"
    assert tasks.get_task(task.task_id).status == TaskStatus.SUCCEEDED
    assert erp.calls[calls_before:] == []


# ---------------------------------------------------------------------------
# 步骤 14 最终重查与审计逐条
# ---------------------------------------------------------------------------


def test_final_recheck_reports_each_order_state(rctx):
    """步骤 14：全部写操作后重新查询，每单前后对比（matched/未处理）。"""
    erp, tasks, _, _, _, _ = rctx
    erp.fail_network_persistent_update(11)
    task, _, _, summary = run_execute(
        rctx,
        items=[create_item(), update_item()],
        report=make_report(create_diff_entry(), update_diff_entry()),
    )
    by_row = {f.row_number: f for f in summary.final_recheck}
    assert len(by_row) == 2
    # create：已补建 → matched
    created = by_row[2]
    assert created.matched is True
    assert created.order_id is not None
    # update：执行失败 → 差异仍存在
    updated = by_row[1]
    assert updated.matched is False
    assert updated.order_id == 11
    assert "差异仍存在" in updated.note


def test_audit_each_write_call_independent(rctx):
    """审计逐条：每次业务层写调用独立 attempt/result（含字段差异与
    幂等键/expected_updated_at，11.2 L1225-1229）。

    2026-08-29 口径：瞬时重试下沉到 MCP 协议层后，create 的"结果未知"
    重试不再产生第 2 条 attempt（业务层只发起一次逻辑创建），改由
    event_type=mcp_retry 审计事件留痕。因此本用例 2 条 attempt =
    create 1 + update 1，另有 1 条 mcp_retry 事件承载被吞掉的那次
    传输尝试（11.2 规则 3 可追溯性：连接失败属"结果未知"，写操作是否
    执行一度不可知）。
    """
    erp, tasks, audit, _, _, _ = rctx
    erp.fail_network_unknown(KEY_D21)  # create 传输层重试 1 次
    task, _, _, summary = run_execute(
        rctx,
        items=[create_item(), update_item()],
        report=make_report(create_diff_entry(), update_diff_entry()),
    )
    attempts = write_attempts(audit, task.operation_id)
    assert len(attempts) == 2  # create 1 + update 1
    # 独立序号（查询 attempt 夹在中间）：严格递增
    assert attempts[0].attempt_no < attempts[1].attempt_no
    # 传输层重试留痕：create 的那次瞬时失败单独成事件，与 operation 同链
    retries = audit.events(task_id=task.task_id, event_type=EVENT_MCP_RETRY)
    assert len(retries) == 1
    assert retries[0].operation_id == task.operation_id
    assert retries[0].structured_payload["tool"] == "create_order_bundle"
    # update attempt 记录 expected_updated_at + 字段差异
    upd = [a for a in attempts if a.target_id == "11"]
    assert len(upd) == 1
    assert upd[0].field_diffs == {"weight_per_unit": "5.00"}
    assert upd[0].expected_updated_at is not None
    # create attempt 记录幂等键
    creates = [a for a in attempts if a.target_id == "101"]
    assert len(creates) == 1
    assert all(a.idempotency_key == KEY_D21 for a in creates)
    # result 每条对应（11.2 四值）
    assert len(write_results(audit, task.task_id, task.operation_id)) == 2
    statuses = sorted(a.result_status for a in attempts)
    assert statuses == ["replayed", "success"]
    # summary 汇总与审计一致：重放计入成功，失败为中间态（规则 3 独立记录）
    assert summary.success_count == 2  # replayed 计入成功
    assert summary.replayed_count == 1
    assert summary.failed_count == 0
    assert summary.task_status == "succeeded"


def test_execute_requires_running_task(rctx):
    """任务非 running（终态）→ 拒绝执行（8.1/13.3 状态兜底）。"""
    _, tasks, _, _, executor, _ = rctx
    task = create_task(tasks, duty="R6", is_write=True)
    tasks.mark_failed(task.task_id, error_code="X", error_summary="manual")
    plan = make_write_plan(task.task_id, [])
    from administrative_agent.domains.reviews import ReviewRecord

    review = ReviewRecord(review_id="rev-x", task_id=task.task_id,
                          requested_by=ACTOR_ACCOUNT,
                          reviewed_payload_hash=plan.payload_hash)
    with pytest.raises(TaskNotRunningError):
        executor.execute(task_id=task.task_id, plan=plan, review=review)
