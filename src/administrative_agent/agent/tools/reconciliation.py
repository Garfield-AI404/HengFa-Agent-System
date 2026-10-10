"""对账域工具（R6）：参数/确认卡/准备/执行。

对账域专用：参数模型、build_reconciliation_confirmation_card 确认卡、
prepare/execute 两段式接线（fail closed 纪律不变）。

- ``reconcile_analyze``（只读工具，独立注册）：附件（AttachmentStore）→
  published 映射 → D19 解析 → ReconciliationPlanExecutor 只读对账 →
  报告落库（ReconcileReportStore, status=analyzed）→ 返回报告摘要与
  report_id，用户确认后调 reconciliation_execute；
- ``prepare_reconcile_confirm``：以报告重建写入计划（WritePlan），确认卡
  payload_hash = plan.payload_hash（7.4 复核 hash 口径一致）；差异为空
  → 确定性 fail closed（不空跑）；
- ``execute_reconciliation``：重建 plan/review——对话确认 = D20 有效复核
  （决策已定 approved，requested_by=reviewed_by=共享 manager 账号，
  7.5 不虚构自然人）；重建后 payload_hash 必须与确认时一致（不一致说明
  报告已变化 → fail closed 拒绝执行）；经 ReconciliationExecuteExecutor
  执行写入，成功后报告状态 executed；失败/部分成功（ExecuteSummary
  task_status=failed/running）→ 报告回退 analyzed（13.2：可重新确认
  重试，已完成条目按幂等/写前重查跳过）；报告已过期（TTL）→ fail
  closed 拒绝执行。

批次 1 的 Phase 2 边界桩（prepare_reconciliation / ReconciliationExecuteArgs）
保留（仅脚本探针引用，文档标注 legacy），注册表走新实现。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from administrative_agent.agent.prompts.policies import policy_for
from administrative_agent.agent.tools.base import (
    PreparedToolWrite,
    ToolContext,
    ToolPreparationError,
    ToolResultSummary,
    _require_wiring,
    _summary_for,
)
from administrative_agent.business.reconciliation_plan import WritePlan, build_write_plan
from administrative_agent.business.review_gate import DECISION_APPROVED
from administrative_agent.config import settings
from administrative_agent.domains.orders import (
    ConfirmationCard,
    card_payload_hash,
    localize_confirmation_fields,
)
from administrative_agent.domains.query import QueryExecutor
from administrative_agent.domains.reconcile_reports import (
    REPORT_STATUS_ANALYZED,
    REPORT_STATUS_CONFIRMED,
    REPORT_STATUS_EXECUTED,
    ReconcileReportRecord,
    report_from_json,
    report_to_json,
    rows_from_json,
)
from administrative_agent.domains.reconciliation import (
    ReconciliationExecuteExecutor,
    ReconciliationPlan,
    ReconciliationPlanExecutor,
)
from administrative_agent.domains.reviews import ReviewRecord
from administrative_agent.files.excel_parser import parse_excel_bytes
from administrative_agent.files.mapping_service import MappingError
from administrative_agent.tasks.records import NewTaskSpec

logger: structlog.stdlib.BoundLogger = structlog.get_logger("tools.reconciliation")

#: reconcile_analyze 只读任务的 duty_code（R6 对账域分析阶段；TaskService
#: 无 duty_code 枚举校验，沿用 R1-R6 职责编号口径）
ANALYZE_DUTY_CODE = "R6"


class ReconciliationExecuteArgs(BaseModel):
    """[legacy] reconciliation_execute 参数（Phase 2 边界遗留，仅脚本探针
    引用；对话对账新路径使用 ReconcileConfirmArgs）。"""

    template_key: str = Field(..., description="映射模板键（published 映射）")
    mapping_version: int = Field(..., gt=0, description="计划使用的映射版本")
    source_file_hash: str = Field(..., description="来源 Excel 文件哈希")
    entity: str = Field(..., description="对账实体（orders 或 attendance）")
    item_count: int = Field(..., ge=0, description="写入计划条目数")


class ReconcileAnalyzeArgs(BaseModel):
    """reconcile_analyze 参数（对话对账批次 2：上传 Excel 后的只读分析）。"""

    file_id: str = Field(..., description="对账 Excel 附件 id（上传返回的 attachment_id）")
    template_key: str = Field(..., description="映射模板键（须已有 published 映射）")
    entity: Literal["metal_orders", "powder_orders"] = Field(
        ..., description="对账实体：metal_orders（钣金订单）/powder_orders（塑粉订单）"
    )
    subject_name: str = Field(
        ..., description="对账主体名称（钣金=客户名、塑粉=供货商名，按名称唯一匹配）"
    )
    period_note: str = Field("", description="核对范围说明（记录于报告，可选）")


class ReconcileConfirmArgs(BaseModel):
    """reconciliation_execute 参数（对话对账批次 2：分析报告确认后执行）。"""

    report_id: str = Field(..., description="对账报告 id（reconcile_analyze 返回的 report_id）")


def build_reconciliation_confirmation_card(
    *,
    template_key: str,
    mapping_version: int,
    source_file_hash: str,
    entity: str,
    item_count: int,
    classification_counts: tuple[tuple[str, int], ...] = (),
    payload_hash: str = "",
) -> ConfirmationCard:
    """对账写入确认卡（6.4 步骤 10 摘要；[2026-08-31 批次 2] 升级）。

    新语义：payload_hash 传 plan.payload_hash（7.4 复核 hash 口径）——
    确认卡 hash 与复核记录 hash 一致，任何计划业务变化都会同时使确认
    卡与复核失效；未传 payload_hash（旧调用形态）时回退按卡片六要素
    计算。fields_to_write 追加差异分类计数元组（复核页展示口径）。

    [2026-09-10 修复] hash_fields：显式声明「哈希用原始载荷」（无隐藏主键、
    无派生展示字段）。注意：本地化会改键名（template_key→模板键、
    source_file_hash→源文件哈希、mapping_version→映射版本、entity→对账实体、
    item_count→条目数，见 CONFIRMATION_FIELD_LABELS），故本卡 hash 与改造前
    口径不同；但本类卡执行期不复算 hash——is_confirmation_valid 仅在
    domains/orders.py 改单执行器与 domains/finance.py 收支改单执行器调用，
    确认环节比对的是 checkpoint 内同一份存储值，故无功能影响。
    """
    raw_fields = (
        ("template_key", template_key),
        ("mapping_version", mapping_version),
        ("source_file_hash", source_file_hash),
        ("entity", entity),
        ("item_count", item_count),
    ) + tuple(sorted(classification_counts))
    card = ConfirmationCard(
        operation_type="执行对账写入",
        target=f"对账计划 {template_key}@v{mapping_version}（{entity}，{item_count} 条）",
        fields_to_write=localize_confirmation_fields(raw_fields),
        hash_fields=raw_fields,
        auto_filled=(),
        expected_impact=(
            "按已复核的写入计划逐条执行 create/update 差异（R6）；"
            "不自动重新复核（RV-30：唯一写入入口为有效复核）。"
        ),
        limitations=(
            "映射版本在写入前再次校验，变化则拒绝执行（fail closed）；",
            "create 条目载荷以 D19 解析的标准行为准，行缺失不编造；",
            "部分成功按任务恢复机制处理（13.2）。",
        ),
        payload_hash=payload_hash,
    )
    if not payload_hash:
        card = replace(card, payload_hash=card_payload_hash(card))
    return card


# ===========================================================================
# reconcile_analyze：只读对账分析（附件 → 解析 → 匹配 → 差异报告落库）
# ===========================================================================


def reconcile_analyze(ctx: ToolContext, args: ReconcileAnalyzeArgs) -> ToolResultSummary:
    """对话对账只读分析：附件 → published 映射 → 解析 → 对账 → 报告落库。

    fail closed：前置校验失败（依赖未接线/附件不存在/跨线程/无 published
    映射）抛 ToolPreparationError（图内转错误 ToolMessage）；解析/对账/
    落库异常返回 ok=False（图内转错误 ToolMessage，模型向用户说明）。
    """
    # ---- 前置校验（fail closed） ----
    _require_wiring(ctx, tool="reconcile_analyze")
    if ctx.metadata is None:
        raise ToolPreparationError(
            "工具 reconcile_analyze 未接线 ERP 实体元数据（metadata），不能执行"
        )
    if (
        ctx.attachment_store is None
        or ctx.report_store is None
        or ctx.mapping_service is None
    ):
        raise ToolPreparationError(
            "工具 reconcile_analyze 未接线附件/报告/映射服务"
            "（harness 运行时缺 attachment_store/report_store/mapping_service），不能执行"
        )
    if ctx.context is None:
        raise ToolPreparationError("缺少会话上下文（context 未接线），不能执行")
    thread_id = ctx.context.thread_id

    record = ctx.attachment_store.get(args.file_id)
    if record is None or _expired(record):
        raise ToolPreparationError(
            f"附件 {args.file_id!r} 不存在或已过期（fail closed），需重新上传对账文件"
        )
    if record.thread_id != thread_id:
        raise ToolPreparationError(
            f"附件 {args.file_id!r} 属于其他会话线程，拒绝使用（跨线程附件隔离，fail closed）"
        )
    try:
        data = _read_attachment_bytes(record)
    except ToolPreparationError:
        raise
    try:
        mapping = ctx.mapping_service.get_published(args.template_key)
    except MappingError as exc:
        raise ToolPreparationError(
            f"模板 {args.template_key!r} 无 published 映射，需先创建映射模板"
            f"（mapping_template）并发布后再对账：{exc}"
        ) from exc
    # 工具层先行解析：parsed.rows 供报告 rows_json 落库（execute 阶段
    # create 载荷来源）；对账计划执行器内部另有同源解析（同一映射与
    # 字节，结果一致——执行器只返回报告不返回 ParsedResult）
    try:
        parsed = parse_excel_bytes(data, mapping)
    except Exception as exc:
        return ToolResultSummary(
            ok=False, summary={"error": f"Excel 解析失败：{exc}（fail closed，未写入）"}
        )

    # ---- 只读任务 + 对账计划执行（复用 D19/D17：QueryExecutor 审计） ----
    query_executor = _build_query_executor(ctx, trace_id=f"thread:{thread_id}")
    plan_executor = ReconciliationPlanExecutor(
        mapping_service=ctx.mapping_service,
        query_executor=query_executor,
    )
    task_id = _create_analyze_task(ctx, args)
    try:
        report = plan_executor.execute(
            task_id=task_id,
            plan=ReconciliationPlan(
                template_key=args.template_key,
                entity=args.entity,
                subject_name=args.subject_name,
                excel_bytes=data,
                period_note=args.period_note,
            ),
        )
    except Exception as exc:
        _fail_task_quietly(query_executor, task_id, f"对账分析失败：{exc}")
        return ToolResultSummary(
            ok=False, summary={"error": f"对账分析失败：{exc}（fail closed，未写入）"}
        )

    # ---- 报告落库（status=analyzed；跨轮确认载体） ----
    try:
        json_cols = report_to_json(
            report, rows={r.row_number: dict(r.values) for r in parsed.rows}
        )
        created = ctx.report_store.create(
            ReconcileReportRecord(
                thread_id=thread_id,
                file_id=args.file_id,
                template_key=report.template_key,
                mapping_version=report.mapping_version,
                entity=report.entity,
                subject_name=args.subject_name,
                period_note=args.period_note,
                source_file_hash=report.source_file_hash,
                row_count=report.row_count,
                summary_json=json_cols["summary_json"],
                conversion_errors_json=json_cols["conversion_errors_json"],
                entries_json=json_cols["entries_json"],
                rows_json=json_cols["rows_json"],
                limitations_json=json_cols["limitations_json"],
                data_completeness=report.data_completeness,
                status=REPORT_STATUS_ANALYZED,
            )
        )
    except Exception as exc:
        _fail_task_quietly(query_executor, task_id, f"对账报告落库失败：{exc}")
        return ToolResultSummary(
            ok=False, summary={"error": f"对账报告落库失败：{exc}（fail closed）"}
        )

    # ---- 报告事件（sink；_harness_sink 对未知事件类型按原名透传，
    #      发射失败不阻断工具结果——事件仅供展示） ----
    if ctx.sink is not None:
        try:
            ctx.sink(
                "reconcile_report",
                {
                    "report_id": created.report_id,
                    "summary_counts": report.summary,
                    "row_count": report.row_count,
                    "subject_match_status": report.subject_match.status,
                },
            )
        except Exception:
            logger.warning(
                "reconcile_report_sink_failed",
                report_id=created.report_id,
                exc_info=True,
            )

    # ---- 成功落库后顺带清理过期报告（TTL；与附件上传顺带清理同模式，
    #      best effort——清理失败不阻断本次分析结果） ----
    try:
        ctx.report_store.delete_expired()
    except Exception:
        logger.warning("report_delete_expired_failed", exc_info=True)

    return ToolResultSummary(
        ok=True,
        summary={
            "report_id": created.report_id,
            "entity": report.entity,
            "subject_name": args.subject_name,
            "subject_match_status": report.subject_match.status,
            "row_count": report.row_count,
            "summary_counts": report.summary,
            "data_completeness": report.data_completeness,
            "limitations": list(report.limitations),
            "conversion_error_count": len(report.conversion_errors),
            "next": "用户确认后调用 reconciliation_execute（参数 report_id）",
        },
    )


# ===========================================================================
# reconciliation_execute：确认卡（WritePlan）+ 复核记录 + 执行写入
# ===========================================================================


def prepare_reconcile_confirm(
    ctx: ToolContext, args: ReconcileConfirmArgs
) -> PreparedToolWrite:
    """对账写确认前准备：报告校验 → 重建计划 → 确认卡（payload_hash=
    plan.payload_hash，7.4 口径）。差异为空 → fail closed 不空跑。"""
    _require_wiring(ctx, tool="reconciliation_execute")
    if (
        ctx.report_store is None
        or ctx.mapping_service is None
        or ctx.review_repo is None
    ):
        raise ToolPreparationError(
            "工具 reconciliation_execute 未接线报告/映射/复核仓储"
            "（harness 运行时缺 report_store/mapping_service/review_repo），不能执行"
        )
    if ctx.context is None:
        raise ToolPreparationError("缺少会话上下文（context 未接线），不能执行")

    record = ctx.report_store.get(args.report_id)
    if record is None:
        raise ToolPreparationError(
            f"对账报告 {args.report_id!r} 不存在（可能已过期清理），需重新对账"
            "（fail closed）"
        )
    if _expired(record):
        raise ToolPreparationError(
            f"对账报告 {args.report_id!r} 已过期，需重新对账（fail closed）"
        )
    if record.thread_id != ctx.context.thread_id:
        raise ToolPreparationError(
            f"对账报告 {args.report_id!r} 属于其他会话线程，拒绝执行"
            "（跨线程报告隔离，fail closed）"
        )
    if record.status != REPORT_STATUS_ANALYZED:
        raise ToolPreparationError(
            f"对账报告 {args.report_id!r} 当前状态为 {record.status!r}，"
            "仅 analyzed 报告可确认执行（fail closed）"
        )

    report = report_from_json(record)
    rows = rows_from_json(record.rows_json)
    plan = build_write_plan(report)
    if not plan.items:
        raise ToolPreparationError(
            "差异报告无可执行写入条目（无差异），无需执行"
            "（fail closed：不空跑）"
        )

    card = build_reconciliation_confirmation_card(
        template_key=report.template_key,
        mapping_version=report.mapping_version,
        source_file_hash=report.source_file_hash,
        entity=report.entity,
        item_count=plan.count,
        classification_counts=tuple(report.summary.items()),
        payload_hash=plan.payload_hash,
    )
    return PreparedToolWrite(
        card=card.to_dict(),
        payload_hash=plan.payload_hash,
        execution={
            "tool": "reconciliation_execute",
            "report_id": args.report_id,
            "plan_hash": plan.payload_hash,
        },
        policy_key="R6.reconciliation.execute",
    )


def execute_reconciliation(
    ctx: ToolContext, args: Any, *, task_id: str, execution: dict[str, Any]
) -> ToolResultSummary:
    """对账写执行：重建 plan/review（对话确认 = D20 有效复核）→ 执行。

    fail closed：报告缺失/已过期/重建失败/plan hash 与确认时不一致 →
    ok=False 不执行任何写入；复核记录或报告状态迁移失败 → ok=False。

    收口（13.2 恢复机制落点）：执行成功后报告状态 executed；失败/部分
    成功（task_status failed/running=awaiting_recovery）→ 报告回退
    analyzed（可重新走确认卡重试，已完成条目按幂等/写前重查跳过），
    回退失败只 warning 不阻断。
    """
    _require_wiring(ctx, tool="reconciliation_execute")
    err_prefix = {"tool": "reconciliation_execute", "task_id": task_id}
    if (
        ctx.report_store is None
        or ctx.mapping_service is None
        or ctx.review_repo is None
    ):
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": "对账执行未接线报告/映射/复核仓储"
                         "（harness 运行时缺依赖），不能执行（fail closed）",
            },
        )

    report_id = execution.get("report_id")
    if not report_id:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": "执行上下文缺少 report_id（fail closed，不执行写入）",
            },
        )
    record = ctx.report_store.get(report_id)
    if record is None:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": f"对账报告 {report_id!r} 不存在（可能已过期清理），"
                         "拒绝执行（fail closed）",
            },
        )
    if _expired(record):
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": f"对账报告 {report_id!r} 已过期，需重新对账"
                         "（fail closed，不执行写入）",
            },
        )
    try:
        report = report_from_json(record)
        rows = rows_from_json(record.rows_json)
        plan = build_write_plan(report)
    except Exception as exc:
        return ToolResultSummary(
            ok=False,
            summary={**err_prefix, "error": f"对账报告重建失败：{exc}（fail closed）"},
        )
    if not plan.items:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": "差异报告无可执行写入条目，无需执行（fail closed）",
            },
        )

    # plan hash 与确认时一致（7.3/7.4：报告已变化 → 原确认失效，拒绝执行）
    confirmed_hash = execution.get("plan_hash") or ""
    if not confirmed_hash or plan.payload_hash != confirmed_hash:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": f"对账报告 {report_id} 内容已变化（plan hash 与确认时"
                         "不一致）：原确认失效，需重新对账确认（fail closed）",
            },
        )

    # ---- 对话确认 = D20 有效复核（决策已定：approved，7.5 共享账号） ----
    now = datetime.now(timezone.utc)
    try:
        review = ctx.review_repo.create(
            ReviewRecord(
                review_id=f"rev-{uuid.uuid4().hex[:16]}",
                task_id=task_id,
                requested_by=ctx.context.user_id,
                requested_at=now,
                reviewed_by=ctx.context.user_id,
                reviewed_at=now,
                decision=DECISION_APPROVED,
                reviewed_payload=plan.to_payload(),
                reviewed_payload_hash=plan.payload_hash,
            )
        )
    except Exception as exc:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": f"复核记录落库失败：{exc}；未执行写入（fail closed）",
            },
        )
    try:
        ctx.report_store.update_status(
            report_id, REPORT_STATUS_CONFIRMED, plan_hash=plan.payload_hash
        )
    except Exception as exc:
        return ToolResultSummary(
            ok=False,
            summary={
                **err_prefix,
                "error": f"对账报告状态迁移失败（{exc}）：未执行写入（fail closed）",
            },
        )

    # ---- 构造并执行（门禁/映射版本校验由执行器 fail closed） ----
    trace_id = execution.get("trace_id") or ""
    try:
        executor = ReconciliationExecuteExecutor(
            client=ctx.client,
            mapping_service=ctx.mapping_service,
            query_executor=_build_query_executor(ctx, trace_id=trace_id),
            tasks=ctx.tasks,
            audit=ctx.audit,
            trace_id=trace_id,
            actor_account=ctx.context.user_id,
            actor_role=ctx.context.user_role,
            policy=policy_for("R6.reconciliation.execute"),
        )
        summary = executor.execute(
            task_id=task_id, plan=plan, review=review, report=report, rows=rows
        )
    except Exception as exc:
        return ToolResultSummary(
            ok=False,
            summary={**err_prefix, "error": f"对账写入执行失败：{exc}（fail closed）"},
        )

    # ---- 收口：成功 → executed；失败/部分成功 → 回退 analyzed（可重试） ----
    # 状态判定按 ExecuteSummary.task_status（succeeded / failed /
    # running=部分成功 awaiting_recovery，见 domains/reconciliation.py
    # _settle）；executed 为终态，failed/partial 卡 confirmed 会永久无法
    # 重试（prepare_reconcile_confirm 只接受 analyzed），故回退待确认。
    data = summary.to_dict()
    if summary.task_status == "succeeded":
        try:
            ctx.report_store.update_status(report_id, REPORT_STATUS_EXECUTED)
        except Exception:
            logger.warning(
                "reconcile_report_status_executed_failed",
                report_id=report_id,
                exc_info=True,
            )
    else:
        try:
            ctx.report_store.update_status(report_id, REPORT_STATUS_ANALYZED)
        except Exception:
            # 回退失败不阻断（与 executed 收口同款）：报告仍卡 confirmed，
            # 由 TTL 清理兜底；下次执行时 plan hash 校验仍会拒绝
            logger.warning(
                "reconcile_report_status_rollback_failed",
                report_id=report_id,
                exc_info=True,
            )
        else:
            data = {
                **data,
                "rollback_note": "报告已回退待确认，可重新确认重试"
                                 "（已完成条目按幂等/写前重查跳过）",
            }
    return _summary_for(ctx, execution, task_id, data)


# ===========================================================================
# [legacy] Phase 2 边界桩（保留：scripts/test_write_tools.py 探针引用）
# ===========================================================================


def prepare_reconciliation(
    ctx: ToolContext, args: ReconciliationExecuteArgs
) -> PreparedToolWrite:
    """[legacy 2026-08-31] 对话对账接入前（批次 1）的确定性 fail closed
    桩——ExecuteSummary 需要已复核 WritePlan + ReviewRecord 上下文；批次 2
    后新确认路径为 prepare_reconcile_confirm，本桩仅由脚本探针引用。
    """
    _require_wiring(ctx, tool="reconciliation_execute")
    raise ToolPreparationError(
        "对账写入需要已复核的写入计划与复核记录（D19/D20 流程上下文），"
        "当前对话流程未提供——请先完成对账计划与复核流程后重试"
        "（fail closed：不编造计划）"
    )


# ===========================================================================
# 内部辅助
# ===========================================================================


def _expired(record: Any) -> bool:
    """记录是否已过期（expires_at 非空且 <= now，与仓储清理口径一致）。

    SQLite 测试库回读 naive datetime 时补时区后比较（与
    routes_chat._attachment_expired 同口径，checkpoint.py _ensure_aware
    同款——避免 naive/aware 直接比较抛 TypeError）。
    """
    expires_at = getattr(record, "expires_at", None)
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _read_attachment_bytes(record: Any) -> bytes:
    """附件内容读取（storage_path 为 attachment_dir 下的相对路径；
    读取失败 fail closed）。"""
    path = os.path.join(settings.attachment_dir, record.storage_path or "")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ToolPreparationError(
            f"附件文件读取失败（{path}）：{exc}（fail closed）"
        ) from exc


def _build_query_executor(ctx: ToolContext, *, trace_id: str) -> QueryExecutor:
    """QueryExecutor 构造（R6 对账域；审计随 task_attempts，11.2）。"""
    return QueryExecutor(
        client=ctx.client,
        cache=ctx.metadata,
        tasks=ctx.tasks,
        audit=ctx.audit,
        trace_id=trace_id,
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
        duty_code=ANALYZE_DUTY_CODE,
    )


def _create_analyze_task(ctx: ToolContext, args: ReconcileAnalyzeArgs) -> str:
    """只读任务创建（8.3：只读不持锁不排队；QueryExecutor.begin_active
    要求任务记录已存在，故必须先落任务行）。"""
    thread_id = ctx.context.thread_id
    try:
        record = ctx.tasks.create_task(
            NewTaskSpec(
                thread_id=thread_id,
                actor_account=ctx.context.user_id,
                actor_role=ctx.context.user_role,
                duty_code=ANALYZE_DUTY_CODE,
                original_user_message=(
                    f"对账分析（reconcile_analyze）：模板 {args.template_key}，"
                    f"主体 {args.subject_name}"
                ),
                structured_intent={
                    "tool": "reconcile_analyze",
                    "arguments": args.model_dump(),
                },
                is_write=False,
            )
        )
    except Exception as exc:
        raise ToolPreparationError(
            f"对账分析任务创建失败（fail closed）：{exc}"
        ) from exc
    return record.task_id


def _fail_task_quietly(query_executor: QueryExecutor, task_id: str, summary: str) -> None:
    """分析失败兜底：任务标记 failed（终态已定则忽略——失败路径本身
    已由执行器/本层收口）。"""
    try:
        query_executor.fail(task_id, "R6_ANALYZE_FAILED", summary)
    except Exception:
        pass
