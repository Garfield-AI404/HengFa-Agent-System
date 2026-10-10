"""工具注册表基础层：共享类型、常量与查询辅助。

事件回调类型（EventSink）、scope 常量、禁止字段、ToolSpec/ToolContext/
ToolResultSummary/PreparedToolWrite/ToolPreparationError 五个基础类型，
以及跨域共享的查询辅助（_today/_require_wiring/_err_text/_query_all/
_match_unique/_summary_for）集中在此；域实现位于 attendance.py / orders.py
/ reconciliation.py，注册表组装位于 tools/__init__.py。

参数 pydantic 模型（order_create/order_update 的 metal/powder 禁止字段
validator 等）与 scope 常量维持 Phase 1 语义——已迁移至本模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel

from administrative_agent.agent.state import AgentContext
from administrative_agent.tasks.machine import TaskStatus

#: 图内事件回调（sink）：(event_type, payload)；Phase 2 由路由层映射为
#: SSE 事件（agent_started/tool_running/tool_result/confirmation_requested/
#: agent_reply/agent_error）。
EventSink = Callable[[str, dict], None]

#: 写操作所需最小 scope（已迁移至本模块；原 agent/intent.py 的
#: OPERATION_SCOPES 于 2026-08-24 删除，值原样迁移至此）。
#:   attendance_plan_execute → {"attendance:create", "attendance:update"}
#:   create_order           → {"orders:create_bundle"}
#:   update_order           → {"orders:update"}
#:   reconcile_execute      → {"orders:create_bundle", "orders:update"}
#:   finance_create         → {"finance:create"}   （R7；scope 名为占位符，
#:                            待与 ERP 权限表核对——见 scopes.py 注释）
#:   finance_update         → {"finance:update"}
SCOPE_ATTENDANCE_WRITE = frozenset({"attendance:create", "attendance:update"})
SCOPE_ORDER_CREATE = frozenset({"orders:create_bundle"})
SCOPE_ORDER_UPDATE = frozenset({"orders:update"})
SCOPE_RECONCILE_EXECUTE = frozenset({"orders:create_bundle", "orders:update"})

#: R7 收支写（finance_create/finance_update；scope 名待 ERP 权限表核对——
#: 与 mcp/scopes.py REQUIRED_SCOPES、web/routes_chat.py _MANAGER_SCOPE 三处一致）
SCOPE_FINANCE_CREATE = frozenset({"finance:create"})
SCOPE_FINANCE_UPDATE = frozenset({"finance:update"})

#: erp:query（2.4 六项之一：ERP 只读查询；erp_query 工具校验）
SCOPE_QUERY = frozenset({"erp:query"})

#: [2026-09-12 一张卡多条明细] 批量卡单卡明细上限（口径 5：一张卡最多 20 条）。
#: 4 条路径（order_create/order_update/finance_create/finance_update）的 args
#: 模型共用（items 列表 min_length=1, max_length=MAX_BATCH_ITEMS）。
MAX_BATCH_ITEMS = 20


def batch_overflow_message(
    exc_or_errors: Any, *, limit: int = MAX_BATCH_ITEMS
) -> str | None:
    """批量 items 超过单卡上限时的**模型面**中文结构化错误文案；否则 ``None``。

    输入 pydantic ``ValidationError``（或其 ``.errors()`` 列表）：仅当存在
    「``type == "too_long"`` 且 ``loc`` 末段为 ``items``」的错误时，返回一段
    含「上限 {limit}」「未写入」「分批」三要素、并**明令禁止自行删减后重提**的
    中文文案——用于把裸英文 pydantic 报错改成可照做的处置指引（超限 fail-closed）。
    其余任何校验失败一律返回 ``None``（本函数不扩大接管范围，未命中保持原行为）。

    条数 ``n`` 优先取自 ``too_long`` 错误的 ``ctx["actual_length"]``，取不到时
    回退用 ``input`` 的长度（见 ``_overflow_item_count_from``）。
    """
    count = _overflow_item_count_from(exc_or_errors)
    if count is None:
        return None
    return (
        f"本次提交 {count} 条，超过一张确认卡的上限 {limit} 条；"
        f"本次未写入任何记录。请向用户说明实际条数，并询问是否分批确认"
        f"（分两张卡，例如先确认前 {limit} 条、用户同意后再出余下）；"
        f"不得自行删减条目后重提。"
    )


def batch_overflow_user_detail(
    exc_or_errors: Any, *, limit: int = MAX_BATCH_ITEMS
) -> str | None:
    """批量 items 超限时的**用户面**短文案；否则 ``None``。

    与 ``batch_overflow_message`` 共用私有取数（``_overflow_item_count_from``），
    命中条件与条数完全一致，仅**语域**不同：本函数写给最终用户（第二人称「请回复」，
    不含「不得自行删减…重提」这类写给模型的指令）——因为该文案会经 ``agent_error``
    的 ``detail`` 键原样拼进用户可见的错误气泡。含「上限 {limit}」「未写入」「分批」
    三要素。
    """
    count = _overflow_item_count_from(exc_or_errors)
    if count is None:
        return None
    return (
        f"本次提交 {count} 条，超过一张确认卡的上限 {limit} 条"
        f"（本次未写入任何记录）。请回复是否分批确认"
        f"（分两张卡，例如先回复「先确认前 {limit} 条」）。"
    )


def _overflow_item_count_from(exc_or_errors: Any) -> int | None:
    """从 ValidationError/错误列表取「items 超限」的实际条数；无匹配 → ``None``。

    判定：``type == "too_long"`` 且 ``loc`` 末段为 ``items``（不按工具名硬编码）。
    逐条扫描：**某条匹配但取不到条数时 ``continue``**（继续看后面是否还有可用条目，
    不因一条取不到就放弃）。
    """
    if isinstance(exc_or_errors, BaseException):
        errors_fn = getattr(exc_or_errors, "errors", None)
        errors = errors_fn() if callable(errors_fn) else None
    else:
        errors = exc_or_errors
    if not errors:
        return None
    for err in errors:
        if not isinstance(err, Mapping) or err.get("type") != "too_long":
            continue
        loc = err.get("loc") or ()
        if not loc or loc[-1] != "items":
            continue
        count = _overflow_item_count(err)
        if count is None:
            continue
        return count
    return None


def _overflow_item_count(err: Mapping[str, Any]) -> int | None:
    """从单条 ``too_long`` 错误取实际条数（``ctx.actual_length`` → ``input`` 长度）。"""
    ctx = err.get("ctx")
    if isinstance(ctx, Mapping):
        actual = ctx.get("actual_length")
        if isinstance(actual, int) and not isinstance(actual, bool):
            return actual
    inp = err.get("input")
    if isinstance(inp, (list, tuple, set, frozenset)):
        return len(inp)
    return None

#: 业务类型禁止字段（已迁移至本模块；原 agent/intent.py L136-137 定义
#: 于 2026-08-24 删除，语义原样迁移）：
#:   metal 订单不得携带供货商/色号等塑粉侧字段；
#:   powder 订单不得携带客户侧字段（10.3 末段示例：order_type=metal 却
#:   出现 supplier_id/color_code 必须拒绝并要求重新分类，L1368 fail closed）。
_METAL_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {"supplier_name", "color_code", "price_per_kg", "total_weight"}
)
_POWDER_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {"customer_name", "model", "unit_price", "price_type", "weight_unit",
     "quantity", "quantity_unit", "weight_per_unit", "surcharge_amount"}
)


@dataclass(frozen=True)
class ToolSpec:
    """会话循环图工具登记项（tools 节点分派与确定性校验依据）。

    - ``prepare``（Phase 2 新增）：写工具的确认前准备（只读前置 + 确认卡
      构造）；写工具必须提供，未提供时图内 fail closed；
    - ``run``：执行（Phase 2 写工具签名 ``run(ctx, args, *, task_id,
      execution)``；非写工具保持 ``run(ctx, args)``）；
    - ``policy_family``：规则包族（tool_args 节点按族注入规则正文约束
      参数生成；空元组 = 无规则包，如 memory_lookup）。族内键必须是
      注册 policy_key（tools/__init__.py import 时断言守护）。
    - ``require_confirmation``：写工具是否需要 interrupt 确认卡（默认
      True，fail closed）。[2026-08-28 考勤免确认] 曾把考勤配成 False
      （直接执行跳过确认卡）；2026-08-30 撤回该决策——因"免确认 ×
      plan_attendance 全员覆盖"叠加，一次执行会不经确认写出全员（13 条）
      不可删除考勤。现全部写工具（R1/R2/R3/R6）均为 True，考勤与 R2/R3
      走同一套确认路径（tools_node.prepare→interrupt→confirm），
      payload_hash 失效机制、confirm 节点路由、审计留痕均未改变。
    """

    name: str  # 工具名（AgentDecision.tool_name 枚举；SCHEMA_INSTRUCTION 表）
    description: str  # 给模型的工具说明（model 节点注入 system prompt）
    args_schema: type[BaseModel]  # 参数 pydantic 模型（确定性校验兜底）
    required_scopes: frozenset[str]  # 2.4 最小 scope 集（context.scope 必须包含）
    requires_manager: bool  # 10.5：业务写操作需要 manager 角色
    is_write: bool  # 写操作（Phase 2：interrupt 确认 + 首次建任务 + 审计）
    duty_code: str  # R1-R6（任务记录 duty_code）
    policy_key_of: Callable[[dict], str]  # 参数 → policy_key（规则包加载）
    run: Callable[..., "ToolResultSummary"]  # 执行（Phase 2 接真实 Executor）
    prepare: Callable[[Any, Any], "PreparedToolWrite"] | None = None  # 确认前准备
    policy_family: tuple[str, ...] = ()  # 规则包族（tool_args 节点注入正文；空=无规则包）
    # 写工具是否需要 interrupt 确认卡（默认 True = 需确认，fail closed）。
    # 历史：2026-08-28 曾把考勤配为 False（免确认直写）；2026-08-30 因
    # "免确认 × 全员覆盖"叠加风险撤回，现无任何工具免确认。False 语义仍
    # 保留（直接执行、跳过确认卡；prepare/执行/审计/串行锁路径不变），
    # 供未来复用，但配置时须先评估"不经确认的写入范围"。
    require_confirmation: bool = True


@dataclass(frozen=True)
class ToolContext:
    """工具执行的闭包（身份/权限/依赖由请求级注入，不入 State）。

    - context：不可变 AgentContext（user_id/user_role/scope/thread_id）；
    - tasks/audit：TaskService/AuditService（Executor 构造依赖）；
    - client/metadata：MCPClient/EntityCache（Phase 2 接线；ERP 查询与
      写入契约校验）；
    - archive：会话记忆存档（ArchiveStore，memory_lookup 查询源）；
    - sink：图内事件回调（EventSink），工具执行过程事件经此发射；
    - attachment_store/report_store/mapping_service/review_repo：[2026-08-31
      对话对账批次 2] 对话对账与映射工具依赖槽——附件仓储
      （AttachmentStore）、对账报告仓储（ReconcileReportStore）、映射服务
      （ExcelMappingService）、复核记录仓库（ReviewRepository）均请求级
      注入，None → 相关工具 fail closed 不执行。
    - rag_client：[2026-09-22 RAG 长期记忆接入] RAG 长期记忆 MCP 客户端
      （与 ERP client 是两个独立实例）；None → rag_remember/rag_recall/
      rag_forget 三个工具 fail closed（装配层未配置时也不向模型暴露它们）。
    """

    context: AgentContext | None = None
    tasks: Any | None = None
    audit: Any | None = None
    metadata: Any | None = None
    sink: EventSink | None = None
    client: Any | None = None  # MCPClient（Phase 2）
    archive: Any | None = None  # 会话记忆存档（ArchiveStore；memory_lookup 查询源，None → 查询失败）
    attachment_store: Any | None = None  # 附件仓储（AttachmentStore；reconcile_analyze/mapping 工具读取）
    report_store: Any | None = None  # 对账报告仓储（ReconcileReportStore）
    mapping_service: Any | None = None  # 映射服务（ExcelMappingService）
    review_repo: Any | None = None  # 复核记录仓库（ReviewRepository，persistence/repositories.py 的 SqlAlchemyReviewRepository 形态）
    rag_client: Any | None = None  # RAG 长期记忆 MCP 客户端（rag_remember/recall/forget；None → 相关工具 fail closed，且装配层不向模型暴露）
    #: [2026-10-05 确认卡改进第 3 批] 最近一条用户原话（请求级注入）。
    #: 仅供写分支把「用户原话对照」整卡级挂到确认卡展示层（source_quote），
    #: 绝不进 payload_hash；默认空串 = 未提供（旧行为零变化）。
    last_user_message: str = ""


@dataclass
class ToolResultSummary:
    """工具执行汇总（Phase 2 与 Executor 输出对接：ok + summary 结构）。

    - ok=False 时 summary 含 "error" 说明（图内转为错误 ToolMessage）；
    - summary 中带 "task_id"（写工具执行产生任务时回填 State.task_id；
      Phase 2：建任务发生在 tools 节点，这里保留 task_id 供展示）。
    """

    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedToolWrite:
    """写工具确认前准备结果（tools 节点 → State.pending_confirmation）。

    - card：确认卡（ConfirmationCard.to_dict()，六要素 + payload_hash）；
    - payload_hash：卡片哈希（7.3 变化失效依据；确认/执行必须一致）；
    - execution：执行上下文（draft/plan 序列化）——批准后 run 以此执行，
      保证"批准的卡片"即"执行的载荷"；
    - policy_key：本工具的规则包 key（11.1 policy 三列注入）。
    """

    card: dict[str, Any]
    payload_hash: str
    execution: dict[str, Any]
    policy_key: str


class ToolPreparationError(RuntimeError):
    """工具准备失败（匹配歧义/未命中/查询失败/资料缺失/上下文缺失）。

    图内 tools 节点捕获后转错误 ToolMessage（fail closed：不落 ERP，
    模型向用户说明后自纠）；写任务标记 failed 释放串行锁。
    """


# ===========================================================================
# 共享查询辅助（被 attendance/orders/reconciliation 两个以上域引用，集中于此）
# ===========================================================================


def _today() -> str:
    """业务日期（in_date 缺省等场景的"今天"）。"""
    return date.today().isoformat()


def _require_wiring(ctx: ToolContext, *, tool: str) -> None:
    """执行前依赖完备性（fail closed：缺依赖不执行、不静默）。"""
    if ctx.client is None:
        raise ToolPreparationError(
            f"工具 {tool} 未接线 MCP 客户端（harness 运行时缺 client），不能执行"
        )
    if ctx.tasks is None or ctx.audit is None:
        raise ToolPreparationError(
            f"工具 {tool} 未接线任务/审计服务（harness 运行时缺 tasks/audit），不能执行"
        )


def _err_text(result: Any) -> str:
    err = getattr(result, "error", None)
    return getattr(err, "message", str(err)) if err else "未知错误"


def _query_all(
    ctx: ToolContext,
    *,
    entity: str,
    fields: Iterable[str] | None = None,
    filter: Mapping[str, Any] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """声明式只读查询取全（mcp-api.md §4；fail closed：查询失败不执行）。

    取默认返回序（ERP 未指定 order_by 时的契约序）；需要排序的调用方
    （tools/query.py）自行经 mcp-api.md §4 order_by 参数实现。
    """
    arguments: dict[str, Any] = {"entity": entity, "limit": limit}
    if fields:
        arguments["fields"] = list(fields)
    if filter:
        arguments["filter"] = dict(filter)
    result = ctx.client.call_tool("query", arguments)
    if not result.ok:
        raise ToolPreparationError(
            f"{entity} 查询失败（fail closed）：{_err_text(result)}"
        )
    return [dict(r) for r in (result.data.get("items") or [])]


def _match_unique(
    ctx: ToolContext,
    *,
    entity: str,
    ref: str,
    match_fn: Callable[[str, list], Any],
    make_candidate: Callable[[Mapping], Any],
    label: str,
    name_field: str = "name",
) -> Any:
    """实体名称唯一匹配（4.3 顺序 2 / 5.1 / 5.2；歧义与未命中 fail closed）。

    ``make_candidate`` 接收单行记录（Mapping），返回 (entity_id, name)
    候选对象——调用方传适配 lambda（行 → CustomerCandidate 等），
    不做隐式关键字构造。

    名称字段按实体可配（``name_field``，默认 "name"）：suppliers/employees
    为 name，customers 为 company_name。
    """
    rows = _query_all(ctx, entity=entity, fields=("id", name_field))
    candidates = [
        make_candidate(r)
        for r in rows
        if r.get("id") is not None and r.get(name_field)
    ]
    outcome = match_fn(ref, candidates)
    if not outcome.writable:
        if outcome.status == "ambiguous":
            names = "、".join(name for _, name in outcome.candidates)
            raise ToolPreparationError(
                f"{label} {ref!r} 存在多个同名候选（{names}），"
                "需用户确认后重试（不自动选择，fail closed）"
            )
        raise ToolPreparationError(
            f"{label} {ref!r} 未找到精确匹配，需核对名称后重试（fail closed）"
        )
    return outcome


def _summary_for(
    ctx: ToolContext, execution: dict[str, Any], task_id: str, data: dict[str, Any]
) -> ToolResultSummary:
    """Executor 输出 → ToolResultSummary（ok 以任务终态为准，不静默）。"""
    record = ctx.tasks.get_task(task_id)
    ok = record is not None and record.status == TaskStatus.SUCCEEDED
    return ToolResultSummary(
        ok=ok,
        summary={"tool": execution["tool"], "task_id": task_id, **data},
    )
