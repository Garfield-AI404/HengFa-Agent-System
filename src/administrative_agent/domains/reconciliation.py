"""R6 对账计划域（规格 2.6 domains/reconciliation.py；6.4 步骤 1-9、
6.3 规则 5、6.5、执行门槛表 L870、RV-28/RV-29）。

流程（6.4 L826-841，本单元覆盖步骤 1-9，零写）：
1. 上传 Excel + 指定客户和核对范围 → ReconciliationPlan（subject_name、
   period_note）；
2. 按 template_key 读取已发布映射（D18 get_published，只使用 published）；
3. 解析标准化行，保留文件哈希 / 映射版本 / 原始行号（D19 excel_parser）；
4. query ERP 订单（复用 D17 QueryExecutor.fetch_all 自动分页取全）；
5. 客户、型号、日期为主要匹配条件（D13 match_customer/match_supplier
   主体名称唯一匹配 + business/reconciliation_rules 匹配键）；
6. 唯一匹配后比较重量、数量、价格、状态等映射中存在的字段；
7. 无匹配 → "ERP 缺失"；
8. 多候选 → "匹配歧义"，绝不自动选一个继续；
9. 生成差异报告，不写入（本模块不 import 任何写入路径，fail closed）。

零写纪律：本模块只 import 读取与规则层；写入语义（create_order_bundle /
update_order 标记）只以字符串常量出现在差异条目中，D21 执行单元才落地。
差异报告结构（entries 携带 matched_order_id/diffs 前后值/write_entry）
为 D20 复核页与 D21 执行留复用口。

6.5：Excel 在内存中解析（不落盘、不进入长期记忆）；报告只携带标准化
数据、文件哈希、映射版本和行号；审计复用 D8 task_attempts（每次实际
query 调用独立 attempt/result，11.2）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

import structlog

from administrative_agent.business.matching import (
    CustomerCandidate,
    MatchOutcome,
    SupplierCandidate,
    match_customer,
    match_supplier,
)
from administrative_agent.business.reconciliation_rules import (
    DiffEntry,
    ENTITY_MATCH_FIELDS,
    ENTITY_SUBJECT_ENTITY,
    ENTITY_SUBJECT_MATCH,
    ENTITY_SUBJECT_NAME_FIELD,
    classify_excel_row,
)
from administrative_agent.domains.query import (
    DEFAULT_QUERY_LIMIT,
    QueryExecutor,
    build_query_spec,
)
from administrative_agent.files.excel_parser import (
    RowError,
    parse_excel_bytes,
)
from administrative_agent.files.mapping_service import (
    ExcelMappingService,
    MappingError,
)

# D21 执行单元（RV-31/RV-32）导入（循环 import 检查确认：以下模块均不
# import domains.reconciliation；domains/reviews.py 经
# business.reconciliation_plan 间接依赖本模块 → ReviewRecord 只按鸭子
# 类型使用，不 import）
from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.business.defaults import (
    DEFAULT_QUANTITY_UNIT,
    DEFAULT_STATUS,
    MISSING_WEIGHT_PER_UNIT,
)
from administrative_agent.business.reconciliation_rules import (
    ENTITY_PRODUCT_FIELD,
    ENTITY_SUBJECT_ID_FIELD,
    WRITE_CREATE_ORDER_BUNDLE,
    WRITE_UPDATE_ORDER,
    norm_value,
)
from administrative_agent.business.review_gate import check_review_valid
from administrative_agent.domains.orders import (
    DEFAULT_RETRY_LIMIT,
    ERROR_FORBIDDEN_FIELD,
    ERROR_IN_PROGRESS,
    ERROR_KEY_CONFLICT,
    ERROR_NETWORK_UNKNOWN,
    ERROR_UPDATE_MCP_FAILURE,
    ERROR_UPDATE_NETWORK_UNKNOWN,
    ERROR_UPDATE_NOT_FOUND,
    ERROR_UPDATE_VERSION_CONFLICT,
    build_update_changes,
    generate_idempotency_key,
    precheck_update_inputs,
    resolve_idempotency_key,
)
from administrative_agent.mcp.client import ToolResult
from administrative_agent.mcp.scopes import (
    UPDATE_WHITELIST_METAL,
    UPDATE_WHITELIST_POWDER,
    verify_order_bundle_arguments,
    verify_update_order_arguments,
)
from administrative_agent.mcp.errors import ErrorCategory, MCPError
from administrative_agent.tasks.machine import TaskStatus

logger: structlog.stdlib.BoundLogger = structlog.get_logger("domains.reconciliation")

#: R6 对账可处理实体（与 mcp-api.md §4 实体一致；白名单由 QueryExecutor
#: 常驻校验，此处为业务口径校验）
RECONCILE_ENTITIES = frozenset({"metal_orders", "powder_orders"})


@dataclass(frozen=True)
class ReconciliationPlan:
    """6.4 步骤 1：上传 Excel + 指定客户（subject_name）和核对范围。"""

    template_key: str
    entity: str  # metal_orders | powder_orders
    subject_name: str  # 客户名称（钣金）/ 供货商名称（塑粉）
    excel_bytes: bytes
    period_note: str = ""  # 核对范围说明（记录于报告，匹配按精确日期）
    page_limit: int = DEFAULT_QUERY_LIMIT


@dataclass(frozen=True)
class ReconciliationReport:
    """6.4 步骤 9 差异报告（只读；D20 复核页 / D21 执行复用结构）。

    - conversion_errors：6.3 规则 5 行级转换错误（行号 + 列名 + 原因，
      RV-28 通过标准：文件哈希 / 映射版本 / 原始行号可定位）；
    - entries：逐行差异分类（RV-29 四类 + 主体匹配失败/键不完整防御类）。
    """

    task_id: str
    template_key: str
    mapping_version: int
    source_file_hash: str
    entity: str
    subject_match: MatchOutcome
    period_note: str
    row_count: int
    conversion_errors: tuple[RowError, ...]
    entries: tuple[DiffEntry, ...]
    data_completeness: str  # "complete" | "partial"
    limitations: tuple[str, ...] = ()

    @property
    def summary(self) -> dict[str, int]:
        """按差异分类计数（RV-29：唯一匹配/缺失/字段差异/匹配歧义等）。"""
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.classification] = counts.get(entry.classification, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "template_key": self.template_key,
            "mapping_version": self.mapping_version,
            "source_file_hash": self.source_file_hash,
            "entity": self.entity,
            "subject_match": {
                "ref": self.subject_match.ref,
                "status": self.subject_match.status,
                "entity_id": self.subject_match.entity_id,
                "matched_name": self.subject_match.matched_name,
                "candidates": list(self.subject_match.candidates),
            },
            "period_note": self.period_note,
            "row_count": self.row_count,
            "conversion_errors": [
                {
                    "row_number": e.row_number,
                    "excel_column": e.excel_column,
                    "target_field": e.target_field,
                    "reason": e.reason,
                }
                for e in self.conversion_errors
            ],
            "entries": [e.to_dict() for e in self.entries],
            "summary": self.summary,
            "data_completeness": self.data_completeness,
            "limitations": list(self.limitations),
        }


class ReconciliationPlanExecutor:
    """R6 对账计划执行器：读映射 → 解析 → 匹配 → 差异分类 → 只读报告。

    任务状态与审计：复用 QueryExecutor（begin_active → 每次实际 query 调用
    独立 attempt/result → finish succeeded，verification_passed=True）；
    失败路径标记 failed 后原样上抛（错误码沿用既有体系）。
    """

    def __init__(
        self,
        *,
        mapping_service: ExcelMappingService,
        query_executor: QueryExecutor,
    ) -> None:
        self._mappings = mapping_service
        self._query = query_executor

    def execute(self, *, task_id: str, plan: ReconciliationPlan) -> ReconciliationReport:
        self._query.begin_active(task_id)
        if plan.entity not in RECONCILE_ENTITIES:
            self._query.fail(
                task_id,
                "RECONCILE_ENTITY_INVALID",
                f"对账实体 {plan.entity!r} 非法（允许 {sorted(RECONCILE_ENTITIES)}）",
            )
            raise ValueError(
                f"对账实体 {plan.entity!r} 非法（允许 {sorted(RECONCILE_ENTITIES)}）"
            )
        try:
            mapping = self._mappings.get_published(plan.template_key)
        except MappingError as exc:
            # 无 published 映射宁可报错（6.3 规则 1 只使用 published）
            self._query.fail(task_id, exc.error_code, f"对账读取映射失败：{exc}")
            raise

        parsed = parse_excel_bytes(plan.excel_bytes, mapping)
        logger.info(
            "reconciliation_parsed",
            task_id=task_id,
            template_key=plan.template_key,
            mapping_version=mapping.version,
            valid_rows=parsed.valid_count,
            error_rows=parsed.error_count,
        )

        limitations: list[str] = []
        if not parsed.rows:
            limitations.append("Excel 没有可匹配的有效数据行（无合法标准化行）")
            report = self._build_report(
                task_id=task_id,
                plan=plan,
                mapping_version=mapping.version,
                source_file_hash=parsed.source_file_hash,
                subject_outcome=MatchOutcome(ref=plan.subject_name, status="not_found"),
                parsed_rows=parsed.rows,
                conversion_errors=parsed.errors,
                limitations=limitations,
            )
            self._query.finish(task_id)
            return report

        # 步骤 4a：主体（客户/供货商）唯一匹配（D13，按名称）
        subject_entity = ENTITY_SUBJECT_ENTITY[plan.entity]
        subject_full = self._query.fetch_all(
            task_id=task_id,
            spec=build_query_spec(
                entity=subject_entity,
                fields=("id", ENTITY_SUBJECT_NAME_FIELD[subject_entity]),
                limit=plan.page_limit,
            ),
            finalize=False,
        )
        subject_outcome = self._match_subject(plan, subject_full.items)

        # 步骤 4b：ERP 订单取全（fetch_all 自动分页；finalize=False 由本方法收口）
        erp_orders: tuple[Mapping[str, Any], ...] = ()
        if subject_outcome.writable and subject_outcome.entity_id is not None:
            orders_full = self._query.fetch_all(
                task_id=task_id,
                spec=build_query_spec(
                    entity=plan.entity, limit=plan.page_limit
                ),
                finalize=False,
            )
            erp_orders = orders_full.items
            limitations.extend(orders_full.notes)
            data_completeness = "complete" if orders_full.complete else "partial"
            if not orders_full.complete:
                limitations.append(
                    "ERP 订单取数期间数据发生变化，差异结果基于不完整数据，需复核"
                )
        else:
            data_completeness = "complete"
            limitations.append(
                "主体名称未唯一匹配，跳过 ERP 订单查询；差异条目均标记 "
                "customer_unmatched（不执行任何写入）"
            )

        compare_fields = tuple(
            c.target_field
            for c in mapping.columns
            if c.target_field not in ENTITY_MATCH_FIELDS[plan.entity]
        )
        entries = tuple(
            classify_excel_row(
                entity=plan.entity,
                row_number=row.row_number,
                source_file_hash=parsed.source_file_hash,
                mapping_version=mapping.version,
                excel_values=row.values,
                subject_outcome=subject_outcome,
                erp_orders=erp_orders,
                compare_fields=compare_fields,
            )
            for row in parsed.rows
        )
        report = self._build_report(
            task_id=task_id,
            plan=plan,
            mapping_version=mapping.version,
            source_file_hash=parsed.source_file_hash,
            subject_outcome=subject_outcome,
            parsed_rows=parsed.rows,
            entries=entries,
            conversion_errors=parsed.errors,
            data_completeness=data_completeness,
            limitations=limitations,
        )
        self._query.finish(task_id)
        return report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _match_subject(
        self, plan: ReconciliationPlan, rows: Sequence[Mapping[str, Any]]
    ) -> MatchOutcome:
        """6.4 步骤 5：主体名称唯一匹配（D13 复用；ambiguous/not_found
        不自动选、报告表达不阻断）。"""
        mode = ENTITY_SUBJECT_MATCH[plan.entity]
        if mode == "match_customer":
            # customers 实体名称为 company_name（ERP 字段目录核实）
            candidates = [
                CustomerCandidate(
                    customer_id=int(r["id"]), name=str(r["company_name"])
                )
                for r in rows
                if r.get("id") is not None and r.get("company_name")
            ]
            return match_customer(plan.subject_name, candidates)
        candidates = [
            SupplierCandidate(supplier_id=int(r["id"]), name=str(r["name"]))
            for r in rows
            if r.get("id") is not None and r.get("name")
        ]
        return match_supplier(plan.subject_name, candidates)

    def _build_report(
        self,
        *,
        task_id: str,
        plan: ReconciliationPlan,
        mapping_version: int,
        source_file_hash: str,
        subject_outcome: MatchOutcome,
        parsed_rows,
        entries: Sequence[DiffEntry] = (),
        conversion_errors: Sequence[RowError] = (),
        data_completeness: str = "complete",
        limitations: Sequence[str] = (),
    ) -> ReconciliationReport:
        return ReconciliationReport(
            task_id=task_id,
            template_key=plan.template_key,
            mapping_version=mapping_version,
            source_file_hash=source_file_hash,
            entity=plan.entity,
            subject_match=subject_outcome,
            period_note=plan.period_note,
            row_count=len(parsed_rows),
            conversion_errors=tuple(conversion_errors),
            entries=tuple(entries),
            data_completeness=data_completeness,
            limitations=tuple(limitations),
        )


# ---------------------------------------------------------------------------
# D21（RV-31/RV-32）：R6 复核后写入执行（6.4 步骤 11-14、7.2 L871、
# 10.2 L1127 R6.reconciliation.execute 规则包、5.4 规则 6/7 L771、
# 11.2 L1225-1229、14.3 场景 9/10/11、14.1 L1380、RV-30 门禁复用）
# ---------------------------------------------------------------------------

#: 任务级错误码（13.1 同风格；条目级复用 D14/D16 既有码）
ERROR_EXECUTE_REVIEW_INVALID = "R6_EXECUTE_REVIEW_INVALID"
ERROR_EXECUTE_MAPPING_VERSION_CHANGED = "R6_MAPPING_VERSION_CHANGED"
ERROR_EXECUTE_PARTIAL = "R6_EXECUTE_PARTIAL"
ERROR_EXECUTE_ALL_FAILED = "R6_EXECUTE_ALL_FAILED"
ERROR_EXECUTE_CREATE_PAYLOAD_MISSING = "R6_CREATE_PAYLOAD_MISSING"
ERROR_EXECUTE_CREATE_FIELDS_INCOMPLETE = "R6_CREATE_FIELDS_INCOMPLETE"
ERROR_EXECUTE_POWDER_CREATE_REJECTED = "R6_POWDER_CREATE_REJECTED"
ERROR_EXECUTE_UNKNOWN_PATH = "R6_EXECUTE_UNKNOWN_PATH"
#: 复核后目标记录已被他人更新（14.3 场景 10 前置检查：写前查询值与
#: 复核时 erp_value 不一致 → 不覆盖新数据，重新生成差异）
ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW = "R6_ORDER_CHANGED_AFTER_REVIEW"

#: 条目结果状态（11.2 result_status 语义 + R6 无需写标记）
ITEM_SUCCESS = "success"
ITEM_REPLAYED = "replayed"
ITEM_FAILED = "failed"
ITEM_CONFLICT = "conflict"
#: 写前查询确认当前值已符合期望（不调用写工具；D16 规则 2 同语义）
ITEM_UNCHANGED = "no_changes"

#: 写前/重查查询字段（与 D16 _UPDATE_QUERY_FIELDS 同构：白名单 +
#: id/updated_at/total_amount；显式字段，mcp-api.md §4/§7.1）
R6_QUERY_FIELDS = {
    "metal_orders": frozenset(
        UPDATE_WHITELIST_METAL | {"id", "updated_at", "total_amount"}
    ),
    "powder_orders": frozenset(
        UPDATE_WHITELIST_POWDER | {"id", "updated_at", "total_amount"}
    ),
}

#: create 载荷必需字段（行/清单未提供 → 条目 failed 转人工，不静默选默认
#: 值——R2 5.1 客户资料补全/询问语义在批量对账场景退化为转人工）：
#: quantity（mcp-api §6.1 必填 > 0）、price_type/weight_unit（无默认枚举，
#: 静默选 kg/含税会掩盖意图）
CREATE_REQUIRED_FIELDS = ("quantity", "price_type", "weight_unit")


class ReconciliationExecuteError(ValueError):
    """执行前置拒绝（门禁/映射版本变化）：不执行任何写入（fail closed）。

    error_code 供调用方归类（13.1 稳定错误码体系）。
    """

    def __init__(self, code: str, message: str) -> None:
        self.error_code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ExecuteItemResult:
    """单条写入计划条目的执行结果（RV-32：成功与失败逐条可追溯）。

    - status：success/replayed/failed/conflict/no_changes（11.2 四值 +
      R6 已一致）；
    - refetched_changes：版本冲突/结果未知后重新查询重新生成的差异
      （5.4 规则 6/7；内容变化 → 重新复核，14.1 L1380 联动口）；
    - before/after_snapshot：更新前后快照（14.3 场景 7 同语义）。
    """

    row_number: int
    classification: str
    write_path: str
    matched_order_id: int | None
    status: str
    error_code: str | None = None
    error_summary: str | None = None
    idempotency_key: str | None = None
    expected_updated_at: str | None = None
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    refetched_changes: dict[str, Any] | None = None
    requires_operator_action: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "classification": self.classification,
            "write_path": self.write_path,
            "matched_order_id": self.matched_order_id,
            "status": self.status,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "idempotency_key": self.idempotency_key,
            "expected_updated_at": self.expected_updated_at,
            "before_snapshot": dict(self.before_snapshot or {}),
            "after_snapshot": dict(self.after_snapshot or {}),
            "refetched_changes": (
                dict(self.refetched_changes) if self.refetched_changes is not None else None
            ),
            "requires_operator_action": self.requires_operator_action,
            "note": self.note,
        }


@dataclass(frozen=True)
class FinalOrderState:
    """6.4 步骤 14 最终重查：每单当前状态 vs 期望达成。

    - matched：期望写入值已全部达成（更新/创建目标一致）；
    - current：重查时的订单当前值（前后对比依据，场景 7 快照语义）。
    """

    row_number: int
    order_id: int | None
    matched: bool
    note: str
    current: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "order_id": self.order_id,
            "matched": self.matched,
            "note": self.note,
            "current": dict(self.current),
        }


@dataclass(frozen=True)
class ExecuteSummary:
    """R6 复核后写入执行汇总（RV-31/RV-32；步骤 14 最终重查输出）。

    - regenerated_diffs：版本冲突/目标变化重新生成的差异（14.1 L1380
      联动：差异重生成 → 新写入计划 hash 变化 → 原复核失效须重新复核）；
    - task_status：succeeded / failed / running（部分成功
      awaiting_recovery，D12 先例）。
    """

    task_id: str
    plan_hash: str
    review_id: str
    items: tuple[ExecuteItemResult, ...]
    final_recheck: tuple[FinalOrderState, ...]
    success_count: int
    replayed_count: int
    failed_count: int
    conflict_count: int
    unchanged_count: int
    regenerated_diffs: tuple[dict[str, Any], ...]
    task_status: str
    verification_passed: bool
    error_code: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_hash": self.plan_hash,
            "review_id": self.review_id,
            "items": [i.to_dict() for i in self.items],
            "final_recheck": [f.to_dict() for f in self.final_recheck],
            "success_count": self.success_count,
            "replayed_count": self.replayed_count,
            "failed_count": self.failed_count,
            "conflict_count": self.conflict_count,
            "unchanged_count": self.unchanged_count,
            "regenerated_diffs": [dict(d) for d in self.regenerated_diffs],
            "task_status": self.task_status,
            "verification_passed": self.verification_passed,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


class ReconciliationExecuteExecutor:
    """R6 复核后写入执行（6.4 步骤 11-14；10.2 execute 规则包四句）。

    流程（RV-31/RV-32）：
    1. 前置门禁（规则包第 1 句）：is_review_valid 为唯一写入入口——
       approved/未失效/未变化/当前 hash 一致，任一不满足拒绝执行，
       不提供绕过路径（RV-30 通过标准复用；D21 与 D20 approve 同校验）；
    2. 映射版本检查（规则包第 3 句）：执行时 published 映射版本与计划
       mapping_version 一致，否则计划可能过期，拒绝执行；
    3. 步骤 11 重新查询（规则包第 2 句）：每条执行前重新 query 目标
       记录——update 检查 updated_at 与复核时值（场景 10 不覆盖新数据），
       create 确认缺失仍存在（不基于过期结果写入）；
    4. 步骤 12 逐条写入：update_order（L843 零重量可更新、L845 塑粉
       写路径；D16 规则复用：写前快照/expected_updated_at 乐观锁/金额
       重算）与 create_order_bundle（D14 幂等键复用：同 key 重放/冲突/
       in progress/网络未知同 key 重试，场景 11 不重复创建）；
    5. 步骤 13 逐条审计：每次实际调用独立 attempt + result（11.2
       L1225-1229：attempt 记录目标/字段差异/幂等键或 expected_updated_at，
       网络重试不覆盖成一条，result 记录结构化错误码与是否需人工处理）；
    6. 步骤 14 最终重查：全部写操作完成后重新查询并输出最终核对结果
       （每单前后对比、成功/未处理区分）；
    7. 汇总收口：全部成功 → succeeded；部分成功 → 保持 running
       （current_step=awaiting_recovery，D12 先例，恢复/重试沿用既有
       语义）；全部失败 → failed；冲突/失败条目重新生成的差异经
       regenerated_diffs 输出（14.1 联动：重新复核走 D20 既有机制，
       本单元不自动重新复核）。

    本模块不实现自动重新复核、不申请 reconciliation:write scope
    （写走既有 orders:create_bundle/orders:update scope，L167-169）。
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        mapping_service: ExcelMappingService,
        query_executor: QueryExecutor,
        tasks: TaskService,
        audit: AuditService,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        policy: Any | None = policy_for("R6.reconciliation.execute"),
        duty_code: str = "R6",
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        key_factory: Callable[[], str] = generate_idempotency_key,
    ) -> None:
        self._client = client
        self._mappings = mapping_service
        self._query = query_executor
        self._tasks = tasks
        self._audit = audit
        self._trace_id = trace_id
        self._actor_account = actor_account
        self._actor_role = actor_role
        self._policy = policy
        self._duty_code = duty_code
        self._retry_limit = retry_limit
        self._key_factory = key_factory

    # ---- 入口（6.4 步骤 11-14） ----

    def execute(
        self,
        *,
        task_id: str,
        plan: Any,
        review: Any,
        report: ReconciliationReport | None = None,
        rows: Mapping[int, Mapping[str, str]] | None = None,
    ) -> ExecuteSummary:
        """复核后执行写入计划（plan: WritePlan；review: ReviewRecord）。

        rows：row_number → 标准行值（D19 解析上下文；create 条目载荷
        来源，行缺失不编造）；report：D19 差异报告（create 条目匹配键
        来源）。门禁/映射版本不满足 → ReconciliationExecuteError 且
        不执行任何写入（fail closed）。
        """
        self._require_running(task_id)
        self._enforce_gate(task_id, plan, review)
        self._enforce_mapping_version(task_id, plan)

        task = self._tasks.get_task(task_id)
        op_id = task.operation_id
        results: list[ExecuteItemResult] = []
        for item in plan.items:
            if item.write_path == WRITE_UPDATE_ORDER:
                results.append(self._execute_update(task_id, op_id, plan, item))
            elif item.write_path == WRITE_CREATE_ORDER_BUNDLE:
                results.append(
                    self._execute_create(task_id, op_id, plan, item, report, rows)
                )
            else:
                # 未知写路径 fail closed（防御：计划只接受两个执行路径）
                results.append(
                    ExecuteItemResult(
                        row_number=item.row_number,
                        classification=item.classification,
                        write_path=item.write_path,
                        matched_order_id=item.matched_order_id,
                        status=ITEM_FAILED,
                        error_code=ERROR_EXECUTE_UNKNOWN_PATH,
                        error_summary=f"未知写路径 {item.write_path!r}，拒绝执行",
                    )
                )
        results = [r for r in results if r is not None]

        # 步骤 14：全部写操作完成后最终重查（每单前后对比、成功/未处理区分）
        final_recheck = self._final_recheck(task_id, plan, report, rows)
        return self._settle(task_id, plan, review, tuple(results), final_recheck)

    # ---- 前置门禁（规则包第 1/3 句；fail closed 不绕过） ----

    def _enforce_gate(self, task_id: str, plan: Any, review: Any) -> None:
        """is_review_valid 为唯一写入入口：无有效复核拒绝执行（RV-30
        通过标准：旧 review 无法执行变化后的计划；D21 与 approve 同校验）。"""
        reason = check_review_valid(
            review, current_payload_hash=plan.payload_hash
        )
        if reason is None:
            return
        summary = (
            f"拒绝执行写入计划：{reason}（唯一写入入口为有效复核，"
            "不提供绕过路径）"
        )
        self._fail_task(task_id, ERROR_EXECUTE_REVIEW_INVALID, summary)
        logger.warning(
            "reconcile_execute_review_invalid",
            task_id=task_id,
            review_id=getattr(review, "review_id", None),
            reason=reason,
        )
        raise ReconciliationExecuteError(ERROR_EXECUTE_REVIEW_INVALID, summary)

    def _enforce_mapping_version(self, task_id: str, plan: Any) -> None:
        """规则包第 3 句：写入前检查对账映射版本未变化。"""
        try:
            published = self._mappings.get_published(plan.template_key)
        except MappingError as exc:
            summary = f"执行前读取映射失败：{exc}；不执行写入"
            self._fail_task(task_id, ERROR_EXECUTE_MAPPING_VERSION_CHANGED, summary)
            raise ReconciliationExecuteError(
                ERROR_EXECUTE_MAPPING_VERSION_CHANGED, summary
            ) from exc
        if published.version != plan.mapping_version:
            summary = (
                f"对账映射版本已变化（计划 v{plan.mapping_version} vs "
                f"当前 v{published.version}）：写入计划基于过期映射，"
                "需重新对账生成差异并复核；不执行写入"
            )
            self._fail_task(task_id, ERROR_EXECUTE_MAPPING_VERSION_CHANGED, summary)
            raise ReconciliationExecuteError(
                ERROR_EXECUTE_MAPPING_VERSION_CHANGED, summary
            )

    # ---- 步骤 12：逐条写入 ----

    def _execute_update(
        self, task_id: str, op_id: str, plan: Any, item: Any
    ) -> ExecuteItemResult:
        """update_order：写前查询 + 复核后变化检查 + 乐观锁执行。

        L843：计划内 update 条目均为零重量可更新（非零旧值不一致不进
        计划）；L845 塑粉 update_order 写路径同样适用。
        """
        order_type = "metal" if plan.entity == "metal_orders" else "powder"
        order_id = item.matched_order_id
        if order_id is None:
            return self._failed(
                item, ERROR_EXECUTE_UNKNOWN_PATH,
                "update 条目缺少目标记录 id（matched_order_id=None），拒绝执行",
            )

        # 步骤 11：写前重新查询目标记录并检查 updated_at（规则包第 2 句）
        try:
            current = self._query_one(task_id, plan.entity, order_id)
        except MCPError as exc:
            return self._failed(
                item, exc.code,
                f"写前查询订单 #{order_id} 失败（{exc.code}）：{exc.message}；"
                "未执行更新",
            )
        if current is None:
            return self._failed(
                item, ERROR_UPDATE_NOT_FOUND,
                f"写前查询未找到订单 #{order_id}：不更新、不猜测（5.4 规则 1）",
            )

        # 场景 10 前置：复核后目标记录是否已被他人更新（写前值 vs 复核时
        # erp_value 不一致 → 不覆盖新数据，重新生成差异，重新复核）
        changed_after = self._order_values_changed_after_review(current, item)
        if changed_after:
            refetched = build_update_changes(
                current, dict(item.fields), order_type
            )
            return ExecuteItemResult(
                row_number=item.row_number,
                classification=item.classification,
                write_path=item.write_path,
                matched_order_id=order_id,
                status=ITEM_CONFLICT,
                error_code=ERROR_EXECUTE_ORDER_CHANGED_AFTER_REVIEW,
                error_summary=(
                    f"订单 #{order_id} 在复核后被他人更新：不覆盖新数据；"
                    f"重新生成差异 {refetched}，需重新对账复核（14.3 场景 10）"
                ),
                before_snapshot=dict(current),
                refetched_changes=dict(refetched),
            )

        desired = dict(item.fields)
        changes = build_update_changes(current, desired, order_type)
        if not changes:
            return ExecuteItemResult(
                row_number=item.row_number,
                classification=item.classification,
                write_path=item.write_path,
                matched_order_id=order_id,
                status=ITEM_UNCHANGED,
                before_snapshot=dict(current),
                note="写前查询确认订单当前值已符合期望，无需更新"
                "（5.4 规则 2：不调用 update_order）",
            )

        # 输入预检 + 白名单兜底（D16 复用，mcp-api.md §7.1）
        expected_updated_at = str(current.get("updated_at") or "")
        try:
            precheck_update_inputs(changes, current, order_type)
            verify_update_order_arguments(
                {
                    "order_type": order_type,
                    "order_id": order_id,
                    "changes": changes,
                    "expected_updated_at": expected_updated_at,
                }
            )
        except MCPError as exc:
            return self._failed(
                item, ERROR_UPDATE_MCP_FAILURE,
                f"更新输入预检未通过（{exc.code}）：{exc.message}",
                expected_updated_at=expected_updated_at,
                before=current,
            )

        # 步骤 13：调用前 attempt（目标/字段差异/expected_updated_at，11.2）
        attempt = self._record_attempt(
            task_id=task_id, op_id=op_id,
            target_type=f"{order_type}_order", target_id=str(order_id),
            field_diffs=changes, expected_updated_at=expected_updated_at,
        )
        result = self._client.call_tool(
            "update_order",
            {
                "order_type": order_type,
                "order_id": order_id,
                "changes": changes,
                "expected_updated_at": expected_updated_at,
            },
            operation_id=op_id,
            trace_id=self._trace_id,
        )
        self._record_mcp_retries(
            result, task_id, op_id,
            target_type=f"{order_type}_order", target_id=str(order_id),
        )
        if result.ok:
            return self._update_success(item, attempt, result.data, order_type,
                                        order_id, changes, expected_updated_at,
                                        current, task_id=task_id)
        err = result.error
        if err.code == "ORDER_VERSION_CONFLICT":
            return self._update_version_conflict(
                task_id, item, attempt, err, order_type, order_id, desired,
                changes, expected_updated_at, current,
            )
        if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
            return self._update_network_unknown(
                task_id, item, attempt, err, order_type, order_id, desired,
                changes, expected_updated_at, current,
            )
        self._record_result(
            attempt, "failed", err,
            summary=f"update_order 失败（{err.code}）on order {order_id}",
        )
        return self._failed(
            item, err.code,
            f"update_order 失败（{err.code}）：{err.message}",
            expected_updated_at=expected_updated_at, before=current,
        )

    def _execute_create(
        self,
        task_id: str,
        op_id: str,
        plan: Any,
        item: Any,
        report: ReconciliationReport | None,
        rows: Mapping[int, Mapping[str, str]] | None,
    ) -> ExecuteItemResult | None:
        """create_order_bundle：写前缺失确认 + 行值载荷 + D14 幂等键。

        - 塑粉缺失订单不创建（D19 决策 e：write_entry=None 保持，
          最终核对输出列示供 manager 人工处理——防御再拒一次）；
        - 载荷字段来自标准行（rows[row_number]）；行缺失 → 不编造，
          条目 failed 转人工；
        - 缺必需字段（quantity/price_type/weight_unit）→ 不静默选默认
          值，条目 failed 转人工（R2 5.1 客户资料补全/询问语义）；
        - 网络结果未知 → 同一幂等键重试（场景 11：重放 replayed=true
          即确认，不重复创建）；达上限 failed 不循环（13.2）。
        """
        if plan.entity != "metal_orders":
            return self._failed(
                item, ERROR_EXECUTE_POWDER_CREATE_REJECTED,
                "塑粉缺失订单不自动补建（D19 决策 e）：最终核对输出列示"
                "'ERP 缺失未补建（塑粉）'供 manager 人工处理",
            )

        # 步骤 11：写前缺失确认（规则包第 2 句：不基于过期结果写入）
        entry = self._entry_for_row(report, item.row_number)
        match_key = entry.match_key if entry is not None else None
        subject_field = ENTITY_SUBJECT_ID_FIELD[plan.entity]
        product_field = ENTITY_PRODUCT_FIELD[plan.entity]
        if match_key is None:
            return self._failed(
                item, ERROR_EXECUTE_CREATE_PAYLOAD_MISSING,
                "create 条目缺少匹配键（D19 报告未提供），不编造写入目标",
            )
        try:
            existing = self._query_missing(task_id, plan.entity, match_key)
        except MCPError as exc:
            return self._failed(
                item, exc.code,
                f"写前缺失确认查询失败（{exc.code}）：{exc.message}；未创建",
            )
        if existing is not None:
            return ExecuteItemResult(
                row_number=item.row_number,
                classification=item.classification,
                write_path=item.write_path,
                matched_order_id=existing.get("id"),
                status=ITEM_REPLAYED,
                note=f"写前确认订单已存在（id={existing.get('id')}）："
                "不重复创建（场景 11 语义）",
                after_snapshot=dict(existing),
            )

        # 载荷：标准行值（行缺失不编造）
        row = (rows or {}).get(item.row_number)
        if row is None:
            return self._failed(
                item, ERROR_EXECUTE_CREATE_PAYLOAD_MISSING,
                "缺少标准行数据（row_number={item.row_number}）：不编造"
                "写入字段，需人工处理",
            )
        payload = self._build_create_payload(match_key, row)
        missing = [f for f in CREATE_REQUIRED_FIELDS if f not in payload]
        if missing:
            return self._failed(
                item, ERROR_EXECUTE_CREATE_FIELDS_INCOMPLETE,
                f"创建载荷缺必需字段 {missing}（清单/映射未提供）：不静默"
                "选默认值，转人工补全（R2 5.1 客户资料补全/询问语义）",
            )
        try:
            verify_order_bundle_arguments({"order": payload})
        except MCPError as exc:
            return self._failed(
                item, ERROR_FORBIDDEN_FIELD,
                f"创建载荷校验失败（{exc.code}）：{exc.message}",
            )

        # D14 幂等键复用：同 op 内同载荷复用 key（attempt 持久化）
        key = resolve_idempotency_key(
            payload, self._audit.attempts(op_id), self._key_factory
        )
        attempts = 0
        last_unknown: tuple[str, str] | None = None
        while attempts < self._retry_limit:
            attempts += 1
            attempt = self._record_attempt(
                task_id=task_id, op_id=op_id, target_type="metal_order",
                target_id=str(match_key.subject_id), field_diffs=payload,
                idempotency_key=key,
            )
            result = self._client.call_tool(
                "create_order_bundle",
                # 契约 mcp-api.md §6.1 必填 order_type；本路径仅钣金缺失
                # 补建（塑粉已在上方拒绝，D19 决策 e），与 attempt 审计
                # target_type="metal_order" 一致
                {"order": payload, "order_type": "metal", "idempotency_key": key},
                operation_id=op_id,
                trace_id=self._trace_id,
            )
            self._record_mcp_retries(
                result, task_id, op_id, target_type="metal_order",
                target_id=str(match_key.subject_id),
            )
            if result.ok:
                return self._create_success(item, attempt, result.data, key, attempts)
            err = result.error
            if err.code == "IDEMPOTENCY_KEY_CONFLICT":
                self._record_result(
                    attempt, "conflict", err,
                    summary=f"幂等键 {key} 已用于不同载荷：冲突，不创建（13.1）",
                )
                return self._failed(
                    item, ERROR_KEY_CONFLICT,
                    f"幂等键已用于不同载荷（{key}）：不创建订单、不改原记录"
                    "（13.1）；需人工确认意图后生成新操作",
                    key=key,
                )
            if err.code == "IDEMPOTENCY_IN_PROGRESS":
                self._record_result(
                    attempt, "failed", err, operator=True,
                    summary=f"幂等键 {key} 异常遗留 PENDING：不得自动重试，"
                    "需人工核查（requires_operator_action）",
                )
                return self._failed(
                    item, ERROR_IN_PROGRESS,
                    f"幂等键存在异常遗留 PENDING（{key}）：不得自动重试、"
                    "不得更换 key 绕过，需人工核查清理后再用原 key 重试",
                    key=key, operator=True,
                )
            if err.category in (ErrorCategory.NETWORK, ErrorCategory.SERVER):
                # 场景 11：结果未知 → 同一幂等键重试（13.2）
                self._record_result(
                    attempt, "failed", err,
                    summary=f"create_order_bundle 结果未知（{err.code}）："
                    "同幂等键重试（场景 11）",
                )
                last_unknown = (err.code, err.message)
                continue
            self._record_result(
                attempt, "failed", err,
                summary=f"create_order_bundle 失败（{err.code}）",
            )
            return self._failed(
                item, err.code,
                f"create_order_bundle 失败（{err.code}）：{err.message}",
                key=key,
            )
        code, msg = last_unknown or ("RETRY_LIMIT_REACHED", "重试上限")
        return self._failed(
            item, ERROR_NETWORK_UNKNOWN,
            f"create_order_bundle 结果未知且已尝试 {attempts} 次（{code}）："
            f"不重复创建，需人工核查后用原 key 继续（13.2/场景 11）：{msg}",
            key=key,
        )

    # ---- update 分支（D16 语义复用：成功/版本冲突/结果未知） ----

    def _update_success(self, item, attempt, data, order_type, order_id,
                        changes, expected_updated_at, before,
                        task_id: str | None = None) -> ExecuteItemResult:
        order = data.get("order") or {}
        changed_fields = tuple(data.get("changed_fields") or [])
        other_effects = tuple(data.get("other_effects") or [])
        self._record_result(
            attempt, "success", None,
            summary=f"updated {order_type} order {order_id}",
            payload={
                "order_id": order_id,
                "order_type": order_type,
                "changes": dict(changes),
                "changed_fields": list(changed_fields),
                "other_effects": [dict(e) for e in other_effects],
                "before_snapshot": dict(before),
                "after_snapshot": dict(order),
            },
        )
        logger.info(
            "reconcile_execute_update_success",
            task_id=task_id, order_id=order_id,
            changed_fields=list(changed_fields),
        )
        return ExecuteItemResult(
            row_number=item.row_number,
            classification=item.classification,
            write_path=item.write_path,
            matched_order_id=order_id,
            status=ITEM_SUCCESS,
            expected_updated_at=expected_updated_at,
            before_snapshot=dict(before),
            after_snapshot=dict(order),
            note=f"updated {order_type} order {order_id}"
            f"（changed_fields={list(changed_fields)}）",
        )

    def _update_version_conflict(self, task_id, item, attempt, err, order_type,
                                 order_id, desired, changes, expected_updated_at,
                                 before) -> ExecuteItemResult:
        """5.4 规则 6/14.3 场景 10：冲突 → 重新查询 → 重新生成差异；
        差异为空（他人已应用）→ 展示不覆盖；内容变化 → 不覆盖需重新复核。"""
        self._record_result(
            attempt, "conflict", err,
            summary=f"ORDER_VERSION_CONFLICT on order {order_id}；"
            "重新查询重新生成差异（5.4 规则 6）",
        )
        try:
            fresh = self._query_one_by_entity(task_id, order_type, order_id)
        except MCPError as query_err:
            return self._failed(
                item, ERROR_UPDATE_NETWORK_UNKNOWN,
                f"版本冲突后重新查询失败（{query_err.code}）：不覆盖新数据，"
                "需人工核查（5.4 规则 6）",
                expected_updated_at=expected_updated_at, before=before,
            )
        fresh_changes = build_update_changes(fresh or {}, desired, order_type)
        if not fresh_changes:
            return ExecuteItemResult(
                row_number=item.row_number,
                classification=item.classification,
                write_path=item.write_path,
                matched_order_id=order_id,
                status=ITEM_SUCCESS,
                expected_updated_at=expected_updated_at,
                before_snapshot=dict(before),
                after_snapshot=dict(fresh or {}),
                refetched_changes=dict(fresh_changes),
                note=f"版本冲突后重新查询：订单 #{order_id} 已被他人更新为"
                "期望值，按最新快照展示，不重复调用 update_order（规则 6）",
            )
        return ExecuteItemResult(
            row_number=item.row_number,
            classification=item.classification,
            write_path=item.write_path,
            matched_order_id=order_id,
            status=ITEM_CONFLICT,
            error_code=ERROR_UPDATE_VERSION_CONFLICT,
            error_summary=(
                f"订单 #{order_id} 版本冲突（expected_updated_at="
                f"{expected_updated_at}）：已重新生成差异 {fresh_changes}；"
                "内容变化，不覆盖新数据，需重新对账复核后执行"
                "（5.4 规则 6 / 14.3 场景 10）"
            ),
            expected_updated_at=expected_updated_at,
            before_snapshot=dict(before),
            after_snapshot=dict(fresh or {}),
            refetched_changes=dict(fresh_changes),
        )

    def _update_network_unknown(self, task_id, item, attempt, err, order_type,
                                order_id, desired, changes, expected_updated_at,
                                before) -> ExecuteItemResult:
        """5.4 规则 7：结果未知 → 先查询确认，不盲目重试。"""
        self._record_result(
            attempt, "failed", err,
            summary=f"update_order 结果未知（{err.code}）on order {order_id}；"
            "重新查询确认是否已应用（5.4 规则 7）",
        )
        try:
            fresh = self._query_one_by_entity(task_id, order_type, order_id)
        except MCPError as query_err:
            return self._failed(
                item, ERROR_UPDATE_NETWORK_UNKNOWN,
                f"update_order 结果未知（{err.code}）且重新查询失败"
                f"（{query_err.code}）：无法确认是否已应用，不盲目重试，"
                "需人工核查（5.4 规则 7）",
                expected_updated_at=expected_updated_at, before=before,
            )
        fresh_changes = build_update_changes(fresh or {}, desired, order_type)
        if not fresh_changes:
            return ExecuteItemResult(
                row_number=item.row_number,
                classification=item.classification,
                write_path=item.write_path,
                matched_order_id=order_id,
                status=ITEM_SUCCESS,
                expected_updated_at=expected_updated_at,
                before_snapshot=dict(before),
                after_snapshot=dict(fresh or {}),
                refetched_changes=dict(fresh_changes),
                note=f"update_order 结果未知（{err.code}），重新查询确认"
                "期望变更已应用：按最新快照展示，不重复调用（规则 7）",
            )
        return self._failed(
            item, ERROR_UPDATE_NETWORK_UNKNOWN,
            f"update_order 结果未知（{err.code}），重新查询确认未应用；"
            f"当前差异 {fresh_changes}；需重新对账复核，不盲目重试（规则 7）",
            expected_updated_at=expected_updated_at, before=before,
        )

    # ---- create 分支（D14 语义复用） ----

    def _create_success(self, item, attempt, data, key, attempts) -> ExecuteItemResult:
        replayed = bool(data.get("replayed"))
        order = data.get("order") or {}
        order_id = order.get("id") if isinstance(order.get("id"), int) else None
        other_effects = tuple(data.get("other_effects") or [])
        status = ITEM_REPLAYED if replayed else ITEM_SUCCESS
        self._record_result(
            attempt, status, None,
            summary=(
                f"replayed metal order {order_id}（同 key 同载荷重放）"
                if replayed else f"created metal order {order_id}"
            ),
            payload={
                "order_id": order_id,
                "replayed": replayed,
                "order_type": data.get("order_type"),
                "other_effects": [dict(e) for e in other_effects],
                "idempotency_key": key,
            },
        )
        return ExecuteItemResult(
            row_number=item.row_number,
            classification=item.classification,
            write_path=item.write_path,
            matched_order_id=order_id,
            status=status,
            idempotency_key=key,
            after_snapshot=dict(order),
            note=f"created metal order {order_id}" if not replayed
            else f"replayed metal order {order_id}（幂等重放，未重复创建）",
        )

    # ---- 步骤 14：最终重查（全部写操作完成后重新查询） ----

    def _final_recheck(
        self,
        task_id: str,
        plan: Any,
        report: ReconciliationReport | None,
        rows: Mapping[int, Mapping[str, str]] | None,
    ) -> tuple[FinalOrderState, ...]:
        states: list[FinalOrderState] = []
        for item in plan.items:
            order_type = "metal" if plan.entity == "metal_orders" else "powder"
            if item.write_path == WRITE_UPDATE_ORDER:
                try:
                    current = self._query_one(task_id, plan.entity, item.matched_order_id)
                except MCPError as exc:
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=item.matched_order_id,
                        matched=False,
                        note=f"最终重查失败（{exc.code}）：{exc.message}",
                        current={},
                    ))
                    continue
                if current is None:
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=item.matched_order_id,
                        matched=False,
                        note="最终重查未找到该订单",
                        current={},
                    ))
                    continue
                expected = dict(item.fields)
                matched = not build_update_changes(current, expected, order_type)
                states.append(FinalOrderState(
                    row_number=item.row_number,
                    order_id=item.matched_order_id,
                    matched=matched,
                    note=(
                        "期望值已达成" if matched
                        else f"期望差异仍存在：{build_update_changes(current, expected, order_type)}"
                    ),
                    current=dict(current),
                ))
            elif item.write_path == WRITE_CREATE_ORDER_BUNDLE:
                # 创建目标：按匹配键重查（塑粉不创建 → 未补建列示）
                entry = self._entry_for_row(report, item.row_number)
                match_key = entry.match_key if entry is not None else None
                if plan.entity != "metal_orders":
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=None,
                        matched=False,
                        note="ERP 缺失未补建（塑粉）：供 manager 人工处理",
                        current={},
                    ))
                    continue
                if match_key is None:
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=None,
                        matched=False,
                        note="缺少匹配键，无法重查",
                        current={},
                    ))
                    continue
                try:
                    existing = self._query_missing(task_id, plan.entity, match_key)
                except MCPError as exc:
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=None,
                        matched=False,
                        note=f"最终重查失败（{exc.code}）：{exc.message}",
                        current={},
                    ))
                    continue
                if existing is None:
                    states.append(FinalOrderState(
                        row_number=item.row_number,
                        order_id=None,
                        matched=False,
                        note="最终重查未找到该订单（未补建）",
                        current={},
                    ))
                    continue
                row = (rows or {}).get(item.row_number) or {}
                # 期望与创建载荷同口径（_build_create_payload：status 固定
                # 未完成、quantity_unit 默认套、weight_per_unit 缺省 0）
                expected = self._build_create_payload(match_key, row)
                matched = not build_update_changes(existing, expected, "metal")
                states.append(FinalOrderState(
                    row_number=item.row_number,
                    order_id=existing.get("id"),
                    matched=matched,
                    note="期望值已达成" if matched else "期望差异仍存在",
                    current=dict(existing),
                ))
        return tuple(states)

    # ---- 汇总收口（RV-32：不假报全量成功；部分成功 awaiting_recovery） ----

    def _settle(
        self,
        task_id: str,
        plan: Any,
        review: Any,
        results: tuple[ExecuteItemResult, ...],
        final_recheck: tuple[FinalOrderState, ...],
    ) -> ExecuteSummary:
        success = [r for r in results if r.status in (ITEM_SUCCESS, ITEM_REPLAYED)]
        failed = [r for r in results if r.status == ITEM_FAILED]
        conflict = [r for r in results if r.status == ITEM_CONFLICT]
        unchanged = [r for r in results if r.status == ITEM_UNCHANGED]
        regenerated = tuple(
            dict(r.refetched_changes) for r in results
            if r.refetched_changes is not None
        )
        summary = ExecuteSummary(
            task_id=task_id,
            plan_hash=plan.payload_hash,
            review_id=review.review_id,
            items=results,
            final_recheck=final_recheck,
            success_count=len(success),
            replayed_count=len([r for r in results if r.status == ITEM_REPLAYED]),
            failed_count=len(failed),
            conflict_count=len(conflict),
            unchanged_count=len(unchanged),
            regenerated_diffs=regenerated,
            task_status="succeeded",
            verification_passed=True,
        )
        if not failed and not conflict:
            # 全部成功（含无需更新）：最终验证通过（8.1 只能经验证路径）
            self._tasks.mark_succeeded(task_id, verification_passed=True)
            logger.info("reconcile_execute_succeeded", task_id=task_id)
            return summary
        if len(failed) + len(conflict) < len(results) or not results:
            # 部分成功（D12 先例）：任务保持 running(awaiting_recovery)，
            # 恢复执行只处理未完成项（已成功条目幂等重放不重复写）
            self._tasks.update_progress(
                task_id, current_step="awaiting_recovery"
            )
            summary = replace(
                summary,
                task_status="running",
                verification_passed=False,
                error_code=ERROR_EXECUTE_PARTIAL,
                error_summary=(
                    f"部分成功：成功 {len(success)} / 失败 {len(failed)} / "
                    f"冲突 {len(conflict)} / 无需更新 {len(unchanged)}；"
                    "任务保持 running(awaiting_recovery)，恢复执行只处理"
                    "未完成项（D12 先例）；冲突条目重新生成的差异需重新"
                    "对账复核（14.1 L1380）"
                ),
            )
            logger.warning(
                "reconcile_execute_partial",
                task_id=task_id,
                failed=len(failed),
                conflict=len(conflict),
            )
            return summary
        # 全部失败：任务 failed（不假报成功）
        self._fail_task(
            task_id,
            ERROR_EXECUTE_ALL_FAILED,
            f"写入计划全部失败：失败 {len(failed)} / 冲突 {len(conflict)}；"
            "需人工处理后重新发起（13.2/13.1）",
        )
        return replace(
            summary,
            task_status="failed",
            verification_passed=False,
            error_code=ERROR_EXECUTE_ALL_FAILED,
            error_summary=(
                f"全部失败：成功 {len(success)} / 失败 {len(failed)} / "
                f"冲突 {len(conflict)}；需人工处理后重新发起"
            ),
        )

    # ---- 查询辅助（步骤 11/14；QueryExecutor.run_page 自带 attempt/result） ----

    def _query_one(self, task_id: str, entity: str, order_id: int) -> dict[str, Any] | None:
        """按 id 查询单个订单（写前查询；D17 run_page 复用，每次实际调用
        独立 attempt/result，11.2 规则 2/3）。"""
        page = self._query.run_page(
            task_id,
            build_query_spec(
                entity=entity,
                fields=R6_QUERY_FIELDS[entity],
                filter={"id": order_id},
                limit=1,
            ),
        )
        return page.items[0] if page.items else None

    def _query_one_by_entity(
        self, task_id: str, order_type: str, order_id: int
    ) -> dict[str, Any] | None:
        """冲突/未知分支重查（不触碰任务终态；run_page 同带审计）。"""
        return self._query_one(
            task_id,
            "metal_orders" if order_type == "metal" else "powder_orders",
            order_id,
        )

    def _query_missing(
        self, task_id: str, entity: str, match_key: Any
    ) -> dict[str, Any] | None:
        """写前缺失确认（规则包第 2 句）：按匹配键（主体/型号/日期）查询。"""
        subject_field = ENTITY_SUBJECT_ID_FIELD[entity]
        product_field = ENTITY_PRODUCT_FIELD[entity]
        page = self._query.run_page(
            task_id,
            build_query_spec(
                entity=entity,
                fields=R6_QUERY_FIELDS[entity],
                filter={
                    subject_field: match_key.subject_id,
                    product_field: match_key.product_ref,
                    "in_date": match_key.in_date,
                },
                limit=1,
            ),
        )
        return page.items[0] if page.items else None

    # ---- create 载荷（标准行 → 订单字段；不编造、不静默选默认） ----

    def _build_create_payload(
        self, match_key: Any, row: Mapping[str, str]
    ) -> dict[str, Any]:
        """标准行 + 匹配键 → create_order_bundle 载荷。

        - 匹配键三要素（主体 id/型号/日期）来自 D19 match_key；
        - 其余字段取标准行值（映射 target_field）；
        - 创建固定值（5.1/defaults）：status=未完成、quantity_unit 默认
          套、weight_per_unit 缺省 0（0 仅表示待补充，mcp-api §6.1）；
        - price_type/weight_unit 无默认枚举（R2 不可静默），由调用方
          检查 CREATE_REQUIRED_FIELDS。
        """
        payload: dict[str, Any] = {
            "customer_id": match_key.subject_id,
            "model": match_key.product_ref,
            "in_date": match_key.in_date,
        }
        for field, value in row.items():
            if field in {"customer_name", "model", "in_date"}:
                continue  # 匹配键已取（customer_name 为名称非 id）
            payload[field] = value
        payload.setdefault("quantity_unit", DEFAULT_QUANTITY_UNIT)
        payload["status"] = DEFAULT_STATUS
        payload.setdefault("weight_per_unit", str(MISSING_WEIGHT_PER_UNIT))
        return payload

    # ---- 场景 10 前置检查 ----

    @staticmethod
    def _order_values_changed_after_review(current: Mapping[str, Any], item: Any) -> bool:
        """复核后目标记录值变化检查：写前值 vs 复核时 erp_value。

        任一写入字段的当前 ERP 值与复核时（D19 差异）不一致 → 目标记录
        在复核后被更新 → 不覆盖新数据（14.3 场景 10；14.1 联动）。
        """
        for field, _value in item.fields:
            erp_before = next(
                (d.get("erp_value") for d in item.diffs if d.get("field") == field),
                None,
            )
            if erp_before is None:
                continue
            if norm_value(str(current.get(field))) != norm_value(str(erp_before)):
                return True
        return False

    @staticmethod
    def _entry_for_row(report: ReconciliationReport | None, row_number: int):
        if report is None:
            return None
        for entry in report.entries:
            if entry.row_number == row_number:
                return entry
        return None

    # ---- 审计（11.2 L1225-1229：每次实际调用独立 attempt + result） ----

    def _record_mcp_retries(
        self,
        result: ToolResult,
        task_id: str,
        op_id: str,
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        """写工具的传输层瞬时重试留痕（见 AuditService.record_mcp_retries）。"""
        if not result.retries:
            return
        self._audit.record_mcp_retries(
            retries=result.retries,
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            target_type=target_type,
            target_id=target_id,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )

    def _record_attempt(
        self,
        *,
        task_id: str,
        op_id: str,
        target_type: str,
        target_id: str,
        field_diffs: dict[str, Any],
        expected_updated_at: str | None = None,
        idempotency_key: str | None = None,
    ):
        return self._audit.record_attempt(
            task_id=task_id,
            operation_id=op_id,
            trace_id=self._trace_id,
            target_type=target_type,
            target_id=target_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            field_diffs=field_diffs,
        )

    def _record_result(
        self,
        attempt,
        result_status: str,
        error: MCPError | None,
        *,
        summary: str,
        payload: dict[str, Any] | None = None,
        operator: bool | None = None,
    ) -> None:
        self._audit.record_result(
            attempt_id=attempt.attempt_id,
            result_status=result_status,
            trace_id=self._trace_id,
            actor_account=self._actor_account,
            actor_role=self._actor_role,
            error_code=error.code if error else None,
            requires_operator_action=(
                operator if operator is not None
                else bool(error.requires_operator_action) if error else None
            ),
            result_summary=summary,
            structured_payload=payload,
            duty_code=self._duty_code,
            policy_key=self._policy.policy_key if self._policy else None,
            policy_version=self._policy.version if self._policy else None,
            policy_hash=self._policy.hash if self._policy else None,
        )

    # ---- 内部 ----

    def _failed(
        self,
        item: Any,
        error_code: str,
        error_summary: str,
        *,
        key: str | None = None,
        expected_updated_at: str | None = None,
        before: dict[str, Any] | None = None,
        operator: bool = False,
    ) -> ExecuteItemResult:
        return ExecuteItemResult(
            row_number=item.row_number,
            classification=item.classification,
            write_path=item.write_path,
            matched_order_id=item.matched_order_id,
            status=ITEM_FAILED,
            error_code=error_code,
            error_summary=error_summary,
            idempotency_key=key,
            expected_updated_at=expected_updated_at,
            before_snapshot=dict(before) if before is not None else None,
            requires_operator_action=operator,
        )

    def _require_running(self, task_id: str) -> None:
        """执行入口：任务必须 running（8.1 L930；8.3 等待/终态不执行；
        13.3 同 task 不并发由 D6 WriteTaskLock 保证，状态兜底）。"""
        from administrative_agent.tasks.service import TaskNotRunningError

        task = self._tasks.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            raise TaskNotRunningError(task_id, task.status)

    def _fail_task(self, task_id: str, error_code: str, error_summary: str) -> None:
        """任务标记 failed（8.1/13.2：需人工处理或重新发起；不假报成功）。"""
        try:
            self._tasks.mark_failed(
                task_id, error_code=error_code, error_summary=error_summary
            )
        except Exception:
            # 任务已终态（如复核流程异常取消）时忽略（同 D14/D16 口径）
            logger.exception("reconcile_execute_fail_task_error", task_id=task_id)
