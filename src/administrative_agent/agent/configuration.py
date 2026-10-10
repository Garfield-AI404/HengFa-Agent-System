"""会话循环图运行时装配与共享工具。

由 agent/graph.py 拆分后位于 configuration.py——运行时
闭包（RuntimeBag）、空 client 桩（_UnconfiguredDecisionClient）与图内
共享工具（_emit/_tool_ctx/_new_tool_call_id）集中于此（LangGraph 惯例：
配置与装配独立于节点实现）。

线程级配置演进位：RuntimeBag 按请求注入依赖（client/tools/tasks/audit/
context/sink/metadata/mcp_client/lock），不进入 LangGraph State；后续
演进（线程级配置/模型选择等）在此扩展。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from administrative_agent.agent.model_client import (
    AgentDecision,
    ModelDecisionClient,
    ModelUnavailableError,
)
from administrative_agent.agent.state import AgentContext
from administrative_agent.agent.tools.base import EventSink, ToolContext, ToolSpec
from administrative_agent.audit.service import AuditFailureError

logger = structlog.get_logger(__name__)

#: 审计落库失败的任务级错误码（11.5 指标聚合键）
ERROR_CODE_AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"


@dataclass
class RuntimeBag:
    """图执行运行时闭包（请求级依赖注入；不进入 LangGraph State）。

    【线程级配置演进位：未来线程级配置（模型/参数/工具集按线程切换）在
    此扩展——当前字段即请求级注入的依赖槽。】

    - client：模型决策客户端（None → 空桩，调用 fail closed）；
    - tools：工具注册表（None → 空注册表，任何 call_tool 分派返回错误
      ToolMessage）；默认全量 TOOL_REGISTRY；
    - tasks/audit/metadata：TaskService/AuditService/EntityCache（Phase 2
      写工具接线；None → 写工具 fail closed 不执行）；
    - context：不可变 AgentContext（身份/权限，8.4 末段；None → 写工具
      scope 校验失败）；
    - sink：图内事件回调（confirmation_requested/tool_running/tool_result/
      agent_reply/agent_error 等，Phase 2 由路由层映射为 SSE 事件）；
    - mcp_client：MCPClient（Phase 2：准备阶段 ERP 只读查询 + 执行阶段
      真实 Executor 的写调用；None → 写工具 fail closed）；
    - rag_client：[2026-09-22 RAG 长期记忆接入] RAG 长期记忆 MCP 客户端
      （与 mcp_client 独立实例；None → rag_* 工具 fail closed，且装配层
      未配置时不向模型暴露这三个工具）；
    - lock：WriteTaskLock（8.3 串行域；测试经 app.state.write_lock 注入
      与提交链路共享——缺省 None 时 TaskService 自建，跨请求无效，仅
      harness 单次执行场景可用）。
    - archive：会话记忆存档（ArchiveStore，压缩节点写、memory_lookup 读；
      None → 压缩降级/查询失败）。
    - attachment_store/report_store/mapping_service/review_repo：
      [2026-08-31 对话对账批次 2] 对话对账与映射工具依赖槽（AttachmentStore/
      ReconcileReportStore/ExcelMappingService/ReviewRepository），经
      ``_tool_ctx`` 透传进 ToolContext；None → 相关工具 fail closed。
    """

    client: ModelDecisionClient | None = None
    tools: dict[str, ToolSpec] | None = None
    tasks: Any | None = None
    audit: Any | None = None
    context: AgentContext | None = None
    sink: EventSink | None = None
    metadata: Any | None = None
    mcp_client: Any | None = None
    rag_client: Any | None = None  # RAG 长期记忆 MCP 客户端（与 mcp_client 独立实例；None → rag_* 工具 fail closed）
    lock: Any | None = None
    archive: Any | None = None  # 会话记忆存档（ArchiveStore，压缩节点写、memory_lookup 读；None → 压缩降级/查询失败）
    attachment_store: Any | None = None  # 附件仓储（AttachmentStore；对话对账附件读取）
    report_store: Any | None = None  # 对账报告仓储（ReconcileReportStore）
    mapping_service: Any | None = None  # 映射服务（ExcelMappingService）
    review_repo: Any | None = None  # 复核记录仓库（ReviewRepository）


class _UnconfiguredDecisionClient:
    """runtime=None 时的空 client 桩：编译可用（worker.py 启动检查），
    任何调用 fail closed（ModelUnavailableError → model 节点错误回复）。

    [2026-08-24 二次改造] 与 ModelDecisionClient 新签名保持同构
    （generate_decision 带 schema_instruction；generate_tool_arguments
    参数轮）——空桩路径同样 fail closed。
    """

    def generate_decision(
        self,
        *,
        system_prompt: str,
        messages: list,
        schema_instruction: str,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentDecision:
        raise ModelUnavailableError(
            "runtime 未配置模型客户端（build_graph(runtime=None) 空桩）："
            "fail closed，不发起调用"
        )

    def generate_tool_arguments(
        self,
        *,
        system_prompt: str,
        messages: list,
        schema_instruction: str,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        raise ModelUnavailableError(
            "runtime 未配置模型客户端（build_graph(runtime=None) 空桩）："
            "fail closed，不发起调用"
        )


def _emit(runtime: RuntimeBag, event_type: str, payload: dict[str, Any]) -> None:
    """经 sink 发射图内事件（sink 未配置时静默跳过——不阻塞图执行）。"""
    if runtime.sink is not None:
        runtime.sink(event_type, payload)


def _tool_ctx(runtime: RuntimeBag, state: Any | None = None) -> ToolContext:
    """构造工具执行闭包（client=tools 阶段 MCP 客户端；sink 透传）。

    [2026-10-05 确认卡改进第 3 批] 可选 ``state``：仅写分支传入会话 State，
    用于把最近一条用户原话经 ``last_user_message`` 递给 prepare（卡面
    source_quote 展示级对照）。其余调用点不传 → 空串（旧行为零变化）。

    延迟 import ``_last_human_content``（其定义在 tools_node，而 tools_node
    反向 import 本模块）以规避循环导入。
    """
    last_user_message = ""
    if state is not None:
        from administrative_agent.agent.nodes.tools_node import _last_human_content

        last_user_message = _last_human_content(state)
    return ToolContext(
        context=runtime.context,
        tasks=runtime.tasks,
        audit=runtime.audit,
        metadata=runtime.metadata,
        sink=runtime.sink,
        client=runtime.mcp_client,
        rag_client=runtime.rag_client,
        archive=runtime.archive,
        attachment_store=runtime.attachment_store,
        report_store=runtime.report_store,
        mapping_service=runtime.mapping_service,
        review_repo=runtime.review_repo,
        last_user_message=last_user_message,
    )


def _new_tool_call_id() -> str:
    return f"tool-call-{uuid.uuid4().hex[:16]}"


def _mark_audit_write_failed(
    runtime: RuntimeBag, task_id: str, exc: BaseException
) -> bool:
    """审计落库失败时把任务标记为需人工复核（10.4 末段 / L1179 追查链）。

    ``AuditFailureError`` 的契约写明：「ERP 已成功而本地审计失败时，调用方
    必须标记任务需人工复核并保留 operation_id」。但写工具执行的两条路径
    （免确认 ``tools_node._run_without_confirmation``、确认后
    ``confirm_node``）此前只把异常转成 ``tool_failed`` 的 ToolMessage，
    **任务行停留在 running**：既不满足该契约，也让串行锁迟迟不释放——后续
    写任务要在 ``_wait_for_execution_right`` 排队等到 30s 超时才被取消。

    补上标记后：任务行落到 ``failed`` + ``AUDIT_WRITE_FAILED``，11.5 指标
    可按 ``final_error_code`` 聚合，锁也就此释放。

    只在 ``AuditFailureError`` 时标记，其余异常一律维持原行为（不新增
    标记），避免改变既有失败语义。返回的 ToolMessage、``current_step``、
    流事件均不受影响。

    标记自身失败只记日志：兜底动作不能掩盖原始异常。

    覆盖范围：仅写工具（有任务行）。读工具（``erp_query``）不产生任务行，
    无处可标记——11.5 指标口径是「写任务审计失败数」，读工具审计失败不在
    聚合范围内，属已知盲区。

    Returns:
        是否确为审计落库失败（供调用方决定是否记指标/日志）。
    """
    if not isinstance(exc, AuditFailureError):
        return False
    summary = f"审计落库失败，需人工复核：{exc.message}"
    if exc.operation_id:
        summary = f"{summary}（operation_id={exc.operation_id}）"
    try:
        runtime.tasks.mark_failed(
            task_id,
            error_code=ERROR_CODE_AUDIT_WRITE_FAILED,
            error_summary=summary,
        )
    except Exception:  # 状态已变（如外部已取消）或 tasks 未接线：不阻塞返回
        logger.exception("audit_write_failed_mark_error", task_id=task_id)
    return True
