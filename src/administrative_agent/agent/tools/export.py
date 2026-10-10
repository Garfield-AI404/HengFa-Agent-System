"""export_data 工具。

- export_data：导出 ERP 实体为 Excel 文件（只读工具 is_write=False）：
  * 权限在 run 内自行校验（fail closed：manager 角色 + erp:query scope，
    8.4/10.5——与 erp_query 同款，非写工具不进写工具分支）；
  * 实体/字段校验复用 domains/export_fields.validate_export_fields
    （fail closed：实体白名单 + 元数据 + 未知字段拒绝，不宽容剔除——
    导出整表落盘，缺列宁可拒绝）；
  * 默认导出列**排除**元数据 sensitive=True 的字段
    （真源 FieldSpec.sensitive，不另立清单）；用户显式 fields 点名敏感字段时
    **照旧放行**，并在结果 note 里附「本次按显式请求导出敏感字段：<字段名>」
    （只列字段名，绝不含字段值），让调用方与模型看得见该行为发生；
  * 行数/翻页超限 → ExportLimitError（提示加过滤条件，不截断）；
  * 审计在 ExcelExporter 内完成（EVENT_EXPORT 单条事件；审计失败 →
    删已落盘文件后抛错，10.4）；
  * 成功经 sink 发射 "export_ready" 事件（前端渲染导出卡片 + 下载链接；
    发射失败不阻断结果——事件仅供展示，同 reconcile_report 模式）；
  * filename 可选：用户对话明确指定文件名时透传（可含中文；导出器自动
    补 .xlsx 后缀并校验，非法字符/超长 fail closed 拒绝导出）；缺省按
    「客户名-时间」构造文件名；
  * summary 不含 items（tools_node 对含 items 的 summary 归档
    tool_query_archive——导出结果文件化，不占上下文预算）。
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from administrative_agent.agent.tools.base import ToolContext, ToolResultSummary
from administrative_agent.agent.tools.query import default_order_by
from administrative_agent.domains.export import (
    ExcelExporter,
    ExportError,
    ExportLimitError,
    ExportNotConfiguredError,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("agent.tools.export")


class ExportDataArgs(BaseModel):
    """export_data 参数：实体名（必填）+ 可选字段/筛选/排序/文件名。"""

    entity: str = Field(
        description=(
            "实体名：metal_orders（钣金订单）/powder_orders（塑粉订单）/"
            "customers（客户）/suppliers（供货商）/employees（员工）/"
            "transactions（收支记录）/transaction_categories（收支分类）/"
            "customer_contacts（客户联系人）/attendance（考勤记录）/ar_payments"
            "（应收收款）/ap_payments（应付付款）/salary_payments（工资发放）/"
            "powder_inventory（塑粉库存色号：净库存直接查 stock 字段，勿自行"
            "按公式另算）/powder_consumption"
            "（塑粉消耗记录）/ar_settlements"
            "（应收平账）"
        )
    )
    fields: list[str] | None = Field(
        None,
        description=(
            "要导出的字段清单（缺省 = 实体默认导出列——metal/powder 按 "
            "R4.export.fields，客户名称/供货商名称与总重量/总价格由导出器"
            "自动关联/计算；其余实体缺省导出全部业务字段（不含系统字段，"
            "且默认排除敏感字段如电话/银行账号/税号）；显式点名敏感字段时"
            "仍会导出并在结果中注明；未知字段会拒绝导出，不宽容剔除）"
        ),
    )
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            '字段筛选：等值如 {"status": "完成"}；范围用 gte/lte 对象如 '
            '{"in_date": {"gte": "2026-08-01", "lte": "2026-08-31"}}；'
            "日期格式 YYYY-MM-DD。支持操作符 eq/ne/gt/gte/lt/lte/like"
            "（模糊包含，% 通配）/in（数组）/between（区间）；同一字段多"
            "操作符为 AND。"
        ),
    )
    order_by: str | list[str] | None = Field(
        None,
        description=(
            "排序字段名（如 in_date/updated_at）；降序在字段名前加连字符"
            '（如 -in_date、-id），可传数组如 ["status","-in_date"]；'
            "不要写 'in_date desc'（会被 ERP 当未知字段拒绝）。不填时按"
            "实体默认序（订单类 metal_orders/powder_orders 默认按入库日期"
            '降序并追加 id 作稳定 tiebreaker：["-in_date","-id"]，最近入库'
            "在前且同日订单翻页稳定不重复/不漏；其余实体 -id 最新在前）。"
        ),
    )
    filename: str | None = Field(
        None,
        description=(
            "导出文件名（可选）：用户对话中明确指定文件名时使用；未指定时按"
            "「客户名-时间」构造（如 方元-2026年7月）并经本参数传入。可含中文；"
            "导出器自动补 .xlsx 后缀并校验合法性（非法字符/超长 → 拒绝导出）；"
            "未传时后端兜底为 {entity}_导出_{YYYYMMDD}.xlsx"
        ),
    )


def _with_note(summary: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """把被本地剔除的计算列 filter/order_by note 并入 summary（措辞与
    erp_query 一致：多条以「；」连接）；无剔除项则原样返回。"""
    if notes:
        summary["note"] = "；".join(notes)
    return summary


def run_export_data(ctx: ToolContext, args: ExportDataArgs) -> ToolResultSummary:
    """导出 ERP 实体为 Excel（只读工具：权限/接线/执行全程 fail closed）。"""
    # 1. 权限（非写工具分支无 scope 校验，本工具自行 fail closed）
    if (
        ctx.context is None
        or ctx.context.user_role != "manager"
        or "erp:query" not in (ctx.context.scope or frozenset())
    ):
        return ToolResultSummary(
            ok=False,
            summary={"error": "权限不足：导出需要 manager 角色与 erp:query scope（8.4/10.5）"},
        )
    # 2. 依赖接线（harness 运行时缺依赖不静默）
    if ctx.client is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 客户端未接线（harness 运行时缺 client），不能执行"},
        )
    if ctx.metadata is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "ERP 实体元数据未接线（harness 运行时缺 metadata），不能执行"},
        )
    if ctx.audit is None:
        return ToolResultSummary(
            ok=False,
            summary={"error": "审计服务未接线（harness 运行时缺 audit），不能执行"},
        )
    # 3. 执行导出（ExcelExporter 内完成翻页/上限/落盘/清理/审计；
    # fields/filter 直接透传，filter=None 即全量导出；
    # [2026-09-10 排序口径/P2-4] 订单类实体未指定 order_by 时注入
    # ["-in_date","-id"]，与 erp_query 默认序一致——导出文件也按入库日期
    # 降序且同日按 id 稳定排序，翻页不重复/不漏行）
    #
    # [computed 契约] 与 erp_query（agent/tools/query.py）**同款**本地剔除：
    # filter 的 computed 键、order_by 的 computed 条目在本地剔除 + note，
    # 不透传 ERP（ERP 对计算列 filter/order_by 一律硬拒 → 整轮重跑浪费）。
    # filter **值**原样保留（mcp-api.md §4：不解析、不 eval、不拼接 SQL）。
    # 元数据未接线/实体未知 → 跳过剔除，交 validate_export_fields fail closed
    # （不在此重复报错；未知字段仍不宽容剔除——导出整表落盘语义不变）。
    notes: list[str] = []
    try:
        entity_meta = ctx.metadata.get_entity(args.entity)
    except Exception:
        entity_meta = None
    filt = args.filter
    #: 本次**显式点名导出**的敏感字段（默认导出已排除敏感字段，故未点名时为空）
    #: ——同一份既用于结果 note，也传给 ExcelExporter 写进耐久审计
    #: （structured_payload.sensitive_fields），**只算一次**，两处口径一致。
    explicit_sensitive: list[str] = []
    if entity_meta is not None:
        non_computed_fields = {
            name for name, spec in entity_meta.fields.items() if not spec.computed
        }
        if filt:
            computed_keys = [
                k for k in filt
                if k in entity_meta.fields and entity_meta.fields[k].computed
            ]
            if computed_keys:
                notes.append(
                    "已忽略计算列筛选键（实时计算列不可用于 filter，仅可经 "
                    "fields 取回）: " + ", ".join(computed_keys)
                )
            filt = {k: v for k, v in filt.items() if k in non_computed_fields} or None
        # [2026-09-15] 显式请求的敏感字段：默认导出列已排除敏感字段（见
        # domains/export_fields.validate_export_fields），但 fields 显式点名
        # 时**照旧放行**——这里附一句说明，让调用方与模型看得见「本次是点名
        # 要的敏感列」这件事发生了。说明只列**字段名**，绝不携带字段值。
        if args.fields:
            explicit_sensitive = [
                f for f in args.fields
                if f in entity_meta.fields and entity_meta.fields[f].sensitive
            ]
            if explicit_sensitive:
                notes.append(
                    "本次按显式请求导出敏感字段：" + "、".join(explicit_sensitive)
                )
    order_by = args.order_by or default_order_by(args.entity)
    if order_by and entity_meta is not None:
        # 判前缀前先剥掉前导 "-"（"-stock" 与 "stock" 同一字段）；保持原入参
        # 形态（字符串仍是字符串，数组仍是数组），不因剔除改变下发参数类型。
        raw_order = [order_by] if isinstance(order_by, str) else list(order_by)
        computed_order = [
            item for item in raw_order
            if isinstance(item, str)
            and item.lstrip("-") in entity_meta.fields
            and entity_meta.fields[item.lstrip("-")].computed
        ]
        if computed_order:
            notes.append(
                "已忽略计算列排序字段（实时计算列不可用于 order_by，仅可经 "
                "fields 取回）: " + ", ".join(computed_order)
            )
        kept_order = [item for item in raw_order if item not in computed_order]
        if kept_order:
            if isinstance(order_by, str):
                order_by = kept_order[0] if len(kept_order) == 1 else kept_order
            else:
                order_by = kept_order
        else:
            order_by = None
    exporter = ExcelExporter(
        client=ctx.client,
        cache=ctx.metadata,
        audit=ctx.audit,
        trace_id=f"thread:{ctx.context.thread_id}",
        actor_account=ctx.context.user_id,
        actor_role=ctx.context.user_role,
    )
    try:
        result = exporter.export(
            entity=args.entity,
            fields=args.fields,
            filter=filt,
            order_by=order_by,
            filename=args.filename,
            sensitive_fields=explicit_sensitive or None,
        )
    except (ExportLimitError, ExportNotConfiguredError) as exc:
        return ToolResultSummary(
            ok=False, summary=_with_note({"error": str(exc)}, notes)
        )
    except ExportError as exc:
        return ToolResultSummary(
            ok=False, summary=_with_note({"error": f"导出失败：{exc}"}, notes)
        )
    except Exception as exc:  # MCPError（字段/实体校验）等
        msg = getattr(exc, "message", None) or str(exc)
        return ToolResultSummary(
            ok=False, summary=_with_note({"error": f"导出失败：{msg}"}, notes)
        )
    # 4. sink 事件（export_ready → SSE → 前端导出卡片；发射失败不阻断结果）
    payload: dict[str, Any] = {
        "file_id": result.file_id,
        "filename": result.filename,
        "entity": args.entity,
        "row_count": result.row_count,
        "total": result.total,
        "expires_at": result.expires_at.isoformat(),
    }
    if ctx.sink is not None:
        try:
            ctx.sink("export_ready", payload)
        except Exception:
            logger.warning(
                "export_ready_sink_failed", file_id=result.file_id, exc_info=True
            )
    # note（被剔除的计算列 filter/order_by）只进模型可见的 summary，不进
    # sink 事件载荷（前端导出卡片消费 payload 的既有键，不扩大契约面）。
    return ToolResultSummary(ok=True, summary=_with_note(dict(payload), notes))
