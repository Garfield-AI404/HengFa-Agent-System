"""append-only 业务审计与 attempt/result 记录（规格 2.6 audit/service.py）。

- **审计事件（11.1）**：audit_events 只追加——本服务不提供任何 update/
  delete 路径，且 DB 层由触发器强制（迁移 92d64bb42eca）；写入失败抛
  ``AuditFailureError``（10.4：审计失败不能假报成功，调用方不得向用户
  报告成功；ERP 已成功而本地审计失败时经 operation_id 追查，L1179）。
- **attempt/result（11.2）**：``record_attempt`` 在工具调用前写（10.4
  顺序 7），``record_result`` 在调用后回填同一条 attempt 记录（顺序 11，
  task_attempts 允许 UPDATE——它属于执行记录，只有 audit_events 是
  append-only）；每次实际调用独立 attempt（attempt_no 递增，11.2 规则 3）。
- **关联（7.4/L1179）**：operation_id/trace_id 贯通——attempt、result、
  审计事件与 ERP 技术日志同用一对标识。
- 字段来源：11.1 全部字段经 ``record_event``；before/after 快照为已脱敏
  的结构化摘要（audit/snapshots.py，6.5：不保存完整原始文件）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

import structlog

from administrative_agent.audit.redaction import redact_mapping

logger: structlog.stdlib.BoundLogger = structlog.get_logger("audit.service")

#: 11.2 规则 2 的 result 状态枚举
RESULT_STATUSES = frozenset({"success", "replayed", "failed", "conflict"})

#: 审计事件类型（11.1/11.2/11.3；复核事件由 D20 使用同一枚举扩展）
EVENT_ATTEMPT = "attempt"
EVENT_RESULT = "result"
#: 传输层瞬时重试（2026-08-29 补，见 record_mcp_retries）
EVENT_MCP_RETRY = "mcp_retry"
#: 对话对账（2026-08-31 批次 1；record_event 已有 source 参数，
#: 对话复核场景 source=chat 直接传，无需新增方法）
EVENT_ATTACHMENT = "attachment"  # 附件上传
EVENT_RECONCILE_REPORT = "reconcile_report"  # 对账分析报告生成
EVENT_MAPPING = "mapping"  # 映射模板导入/发布/退役
#: 对话导出（2026-09-01 导出功能批次；record_export 封装——单条事件，
#: 不建 attempt/result 对：导出为读操作，11.2 attempt 语义只覆盖写调用）
EVENT_EXPORT = "export"  # 导出完成（Excel 文件生成）
#: RAG 长期记忆增删（2026-09-22 F1；record_rag_memory 封装——单条事件，
#: 不建 attempt/result 对：RAG 记忆库不是 ERP 写语义，不进 _WRITE_TOOLS，
#: 也不走确认卡/payload hash；但 remember 跨系统新增记忆、forget 软删都
#: **改变存储状态**，与纯读的 memory_lookup 不同类，须留一条 agent 侧审计，
#: 让「agent 默默记了什么」事后查得出来）
EVENT_RAG_MEMORY = "rag_memory"  # 长期记忆 remember/forget
#: RAG 长期记忆**召回**（2026-09-23；record_rag_recall 封装——单条事件，不建
#: attempt/result 对：recall 是纯读，不改变存储状态）。与 EVENT_RAG_MEMORY
#: 分开：后者是「改变存储状态」的增删，召回与它语义不同类。补这条是为了让
#: 「这一轮为什么这么答」在 agent 侧可追——记忆是被召回但没记、还是压根没调，
#: 事后能分出来（此前纯读的 recall 无任何痕迹，此处断链）。
EVENT_RAG_RECALL = "rag_recall"  # 长期记忆 recall（纯读）
#: RAG **文档检索**（`search_documents`，纯读；record_rag_search_documents 封装
#: ——单条事件，不建 attempt/result 对）。与 EVENT_RAG_RECALL 分开：召回的是
#: 长期记忆（memories 表），检索的是公司共享知识库的文档片段（documents/chunks
#: 两表），两条线在服务层零交叉、命中的对象也不是同一类（memory_id vs
#: chunk_id/document_id），共用一个事件类型会让事后读者分不清「查的是记忆还是
#: 文档」。补这条的理由与 recall 同：让「这一轮为什么这么答」在 agent 侧可追
#: ——文档是被检索到但没引用、还是压根没调，事后能分出来。
EVENT_RAG_SEARCH = "rag_search"  # 文档检索 search_documents（纯读）


class AuditFailureError(RuntimeError):
    """本地审计写入失败（10.4 末段）。

    携带 operation_id 供 L1179 追查：ERP 已成功而本地审计失败时，
    调用方必须标记任务需人工复核并保留 operation_id。
    """

    def __init__(self, operation_id: str | None, message: str) -> None:
        self.operation_id = operation_id
        self.message = message  # 原始消息（无 operation_id 后缀），供外层包裹复用
        super().__init__(f"{message} (operation_id={operation_id!r})")


@dataclass(frozen=True)
class AuditEventRecord:
    """审计事件记录（11.1 字段全集；created_at 由仓储层落库时间）。"""

    audit_id: str
    trace_id: str
    actor_account: str
    actor_role: str
    event_type: str
    task_id: str | None = None
    operation_id: str | None = None
    duty_code: str | None = None
    policy_key: str | None = None
    policy_version: str | None = None
    policy_hash: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    request_summary: str | None = None
    structured_payload: dict[str, Any] | None = None
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    source: str = "agent"
    result_code: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskAttemptRecord:
    """写操作 attempt 记录（11.2；result 字段调用后回填）。

    与 12.1 task_attempts 表字段一一对应（无 trace_id 列——trace 关联
    经配套审计事件 audit_events.trace_id 承载，L1179 追查链完整）。
    """

    attempt_id: str
    task_id: str
    operation_id: str
    attempt_no: int
    target_type: str
    target_id: str | None = None
    field_diffs: dict[str, Any] | None = None
    idempotency_key: str | None = None
    expected_updated_at: str | None = None
    created_at: datetime | None = None
    result_status: str | None = None
    error_code: str | None = None
    requires_operator_action: bool | None = None
    result_summary: str | None = None
    result_at: datetime | None = None


class AuditRepository(Protocol):
    """audit_events 持久化契约（只追加 + 只读查询）。

    实现见 persistence/repositories.py（SqlAlchemyAuditRepository）；
    测试用 in-memory 实现（tests/audit/）。协议刻意不含 update/delete。
    """

    def append(self, event: AuditEventRecord) -> AuditEventRecord: ...

    def list_by(
        self,
        *,
        task_id: str | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEventRecord]: ...

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        task_id: str | None = None,
        operation_id: str | None = None,
        event_type: str | None = None,
    ) -> tuple[list[AuditEventRecord], int]: ...


class AttemptRepository(Protocol):
    """task_attempts 持久化契约（attempt 调用前写、result 调用后回填）。"""

    def append_attempt(self, record: TaskAttemptRecord) -> TaskAttemptRecord: ...

    def record_result(
        self,
        attempt_id: str,
        *,
        result_status: str,
        error_code: str | None = None,
        requires_operator_action: bool | None = None,
        result_summary: str | None = None,
    ) -> TaskAttemptRecord: ...

    def list_by_operation(self, operation_id: str) -> list[TaskAttemptRecord]: ...

    def get(self, attempt_id: str) -> TaskAttemptRecord | None: ...


class AuditService:
    """业务审计服务（11.1/11.2）。线程安全：仓储操作各自事务边界。

    写入失败一律抛 ``AuditFailureError``——绝不吞错、绝不向调用方假报
    成功（10.4）；调用方（业务节点 D11+ / 演示）捕获后标记任务需人工
    复核，保留 operation_id 追查。
    """

    def __init__(self, audit_repo: AuditRepository, attempt_repo: AttemptRepository) -> None:
        self._audit = audit_repo
        self._attempts = attempt_repo

    # ---- 11.1 事件 ----

    def record_event(
        self,
        *,
        event_type: str,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        task_id: str | None = None,
        operation_id: str | None = None,
        duty_code: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        request_summary: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        before_snapshot: dict[str, Any] | None = None,
        after_snapshot: dict[str, Any] | None = None,
        source: str = "agent",
        result_code: str | None = None,
        policy_key: str | None = None,
        policy_version: str | None = None,
        policy_hash: str | None = None,
    ) -> AuditEventRecord:
        """追加一条审计事件（11.1 字段全集）。

        structured_payload/before/after_snapshot 在落库前脱敏
        （redact_mapping，11.2 规则 4：不记录密码/API Key/完整原始文件）。
        """
        event = AuditEventRecord(
            audit_id=_new_audit_id(),
            task_id=task_id,
            operation_id=operation_id,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            event_type=event_type,
            duty_code=duty_code,
            policy_key=policy_key,
            policy_version=policy_version,
            policy_hash=policy_hash,
            target_type=target_type,
            target_id=target_id,
            request_summary=request_summary,
            structured_payload=redact_mapping(structured_payload),
            before_snapshot=redact_mapping(before_snapshot),
            after_snapshot=redact_mapping(after_snapshot),
            source=source,
            result_code=result_code,
        )
        try:
            saved = self._audit.append(event)
        except Exception as exc:
            # 10.4：审计失败不能假报成功；携带 operation_id 供 L1179 追查
            logger.error(
                "audit_event_failed",
                event_type=event_type,
                task_id=task_id,
                operation_id=operation_id,
                error=type(exc).__name__,
            )
            raise AuditFailureError(operation_id, f"审计事件写入失败：{exc}") from exc
        logger.info(
            "audit_event_written",
            audit_id=saved.audit_id,
            event_type=event_type,
            operation_id=operation_id,
            trace_id=trace_id,
        )
        return saved

    # ---- 11.2 attempt/result ----

    def record_attempt(
        self,
        *,
        task_id: str,
        operation_id: str,
        trace_id: str,
        target_type: str,
        actor_account: str,
        actor_role: str,
        target_id: str | None = None,
        field_diffs: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_updated_at: str | None = None,
        duty_code: str | None = None,
        policy_key: str | None = None,
        policy_version: str | None = None,
        policy_hash: str | None = None,
    ) -> TaskAttemptRecord:
        """工具调用前记录 attempt（10.4 顺序 7）。

        - task_attempts 一行（attempt_no = 已有 attempt 数 + 1：每次业务层
          调用独立 attempt，**不覆盖前一行**，11.2 规则 3）；
        - 同时追加 event_type=attempt 审计事件（11.1：目标、字段差异、
          幂等键/expected_updated_at、trace_id——L1179 追查链）。
        field_diffs 应为脱敏后的差异摘要（audit/snapshots.diff_summary）。

        关于"网络重试"（2026-08-29 澄清）：2026-08-27 起瞬时故障重试下沉
        到 MCP 协议层（mcp/client.py::_call_with_retries），业务层一次
        逻辑调用只写一行 attempt，传输层的重试尝试**不**各自成行（否则
        task_attempts 会混入传输噪声）。这些重试改由
        :meth:`record_mcp_retries` 补记为 event_type=mcp_retry 的持久化
        审计事件——连接失败属"结果未知"（13.1），写操作是否执行一度不可知，
        是可审计性证据，不能只留在有界且易失的日志环形缓冲里。
        """
        prior = self._attempts.list_by_operation(operation_id)
        record = TaskAttemptRecord(
            attempt_id=_new_attempt_id(),
            task_id=task_id,
            operation_id=operation_id,
            attempt_no=len(prior) + 1,
            target_type=target_type,
            target_id=target_id,
            field_diffs=redact_mapping(field_diffs),
            idempotency_key=idempotency_key,
            expected_updated_at=expected_updated_at,
        )
        try:
            saved = self._attempts.append_attempt(record)
            self.record_event(
                event_type=EVENT_ATTEMPT,
                trace_id=trace_id,
                actor_account=actor_account,
                actor_role=actor_role,
                task_id=task_id,
                operation_id=operation_id,
                duty_code=duty_code,
                policy_key=policy_key,
                policy_version=policy_version,
                policy_hash=policy_hash,
                target_type=target_type,
                target_id=target_id,
                request_summary=f"attempt {target_type} (operation {operation_id})",
                structured_payload={
                    "attempt_no": saved.attempt_no,
                    "field_diffs": saved.field_diffs,
                    "idempotency_key": saved.idempotency_key,
                    "expected_updated_at": saved.expected_updated_at,
                },
            )
        except Exception as exc:
            logger.error(
                "attempt_record_failed",
                task_id=task_id,
                operation_id=operation_id,
                error=type(exc).__name__,
            )
            # 内层已是 AuditFailureError 时复用其原始消息（不带自身
            # operation_id 后缀），外层只附加一次 operation_id（L1179）
            detail = exc.message if isinstance(exc, AuditFailureError) else str(exc)
            raise AuditFailureError(operation_id, f"attempt 记录失败：{detail}") from exc
        logger.info(
            "attempt_recorded",
            attempt_id=saved.attempt_id,
            attempt_no=saved.attempt_no,
            operation_id=operation_id,
        )
        return saved

    def record_result(
        self,
        *,
        attempt_id: str,
        result_status: str,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        error_code: str | None = None,
        requires_operator_action: bool | None = None,
        result_summary: str | None = None,
        duty_code: str | None = None,
        policy_key: str | None = None,
        policy_version: str | None = None,
        policy_hash: str | None = None,
        structured_payload: dict[str, Any] | None = None,
    ) -> TaskAttemptRecord:
        """调用返回后立即记录 result（10.4 顺序 11），回填同一条 attempt，
        并追加 event_type=result 审计事件（含 result_code 与职责/策略列）。

        result_status 限 11.2 规则 2 四值（success/replayed/failed/conflict）；
        result 记录失败同样抛 AuditFailureError（不假报成功）。
        duty_code/policy_* 由调用方透传（与 attempt 事件一致，保证审计链
        每个环节都携带职责与策略上下文——D12 执行器按此传入）。
        structured_payload 为调用方业务结果摘要（如订单响应与 other_effects，
        D14 5.3 步骤 7"保存订单响应"的审计载体），落库前脱敏后并入事件载荷。
        """
        if result_status not in RESULT_STATUSES:
            raise ValueError(
                f"invalid result_status {result_status!r}; "
                f"must be one of {sorted(RESULT_STATUSES)}"
            )
        prior = self._attempts.get(attempt_id)
        if prior is None:
            raise AuditFailureError(None, f"attempt {attempt_id!r} 不存在")
        try:
            saved = self._attempts.record_result(
                attempt_id,
                result_status=result_status,
                error_code=error_code,
                requires_operator_action=requires_operator_action,
                result_summary=result_summary,
            )
            event_payload = {
                "attempt_no": prior.attempt_no,
                "error_code": error_code,
                "requires_operator_action": requires_operator_action,
            }
            if structured_payload:
                event_payload.update(redact_mapping(structured_payload))
            self.record_event(
                event_type=EVENT_RESULT,
                trace_id=trace_id,
                actor_account=actor_account,
                actor_role=actor_role,
                task_id=prior.task_id,
                operation_id=prior.operation_id,
                duty_code=duty_code,
                policy_key=policy_key,
                policy_version=policy_version,
                policy_hash=policy_hash,
                target_type=prior.target_type,
                target_id=prior.target_id,
                request_summary=(
                    f"result {result_status} for attempt {attempt_id}"
                    f" (operation {prior.operation_id})"
                ),
                structured_payload=event_payload,
                result_code=result_status,
            )
        except Exception as exc:
            logger.error(
                "result_record_failed",
                attempt_id=attempt_id,
                error=type(exc).__name__,
            )
            detail = exc.message if isinstance(exc, AuditFailureError) else str(exc)
            raise AuditFailureError(prior.operation_id, f"result 记录失败：{detail}") from exc
        logger.info(
            "result_recorded",
            attempt_id=attempt_id,
            result_status=result_status,
            error_code=error_code,
        )
        return saved

    # ---- 只读查询（追查 L1179 / 审计样例） ----

    def events(
        self,
        *,
        task_id: str | None = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEventRecord]:
        """按追查链过滤审计事件（operation_id/trace_id 为 7.4 关联键）。"""
        return self._audit.list_by(
            task_id=task_id,
            operation_id=operation_id,
            trace_id=trace_id,
            event_type=event_type,
            limit=limit,
        )

    def list_events(
        self,
        *,
        limit: int,
        offset: int,
        task_id: str | None = None,
        operation_id: str | None = None,
        event_type: str | None = None,
    ) -> tuple[list[AuditEventRecord], int]:
        """业务审计分页（2.7.3 GET /api/v1/audit；created_at 倒序，
        11.1 append-only 只读——调用方只读输出，无写路径）。"""
        return self._audit.list_page(
            limit=limit,
            offset=offset,
            task_id=task_id,
            operation_id=operation_id,
            event_type=event_type,
        )

    def attempts(self, operation_id: str) -> list[TaskAttemptRecord]:
        """该 operation_id 的全部实际调用记录（attempt_no 升序）。"""
        return sorted(
            self._attempts.list_by_operation(operation_id),
            key=lambda r: (r.attempt_no, r.created_at or datetime.min.replace(tzinfo=timezone.utc)),
        )

    def get_attempt(self, attempt_id: str) -> TaskAttemptRecord | None:
        return self._attempts.get(attempt_id)

    # ---- 对话导出（2026-09-01 导出功能批次；单条事件，不建 attempt 对） ----

    def record_export(
        self,
        *,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        entity: str,
        filter: Mapping[str, Any] | None = None,
        row_count: int,
        file_id: str,
        columns: Sequence[str] | None = None,
        sensitive_fields: Sequence[str] | None = None,
    ) -> AuditEventRecord:
        """记录一次导出完成（EVENT_EXPORT 单条事件）。

        导出为只读操作，不建 task_attempts 行（11.2 attempt/result 语义
        只覆盖写调用）；target_id=file_id 供下载/清理追查。filter 经
        redact_mapping 脱敏后落入 structured_payload。审计失败抛
        AuditFailureError——ExcelExporter 捕获后删已落盘文件再抛（10.4）。

        [2026-09-15] structured_payload 新增两个**可为空**的 additive 键：
        - ``columns``：本次实际导出的列名（= 落盘 meta.json 的 columns 来源）；
        - ``sensitive_fields``：其中**被显式点名导出**的敏感字段名列表
          （默认导出已排除敏感字段，故未点名时为空/None）。仅**列名**，
          不含字段值；由 run_export_data 复用 explicit_sensitive 传入。
        旧记录无这两个键，读取方按可选键处理，不因缺键而崩。
        """
        return self.record_event(
            event_type=EVENT_EXPORT,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            target_type="export",
            target_id=file_id,
            request_summary=f"export {entity} (rows={row_count})",
            structured_payload={
                "entity": entity,
                "filter": dict(filter) if filter else None,
                "row_count": row_count,
                "columns": list(columns) if columns else None,
                "sensitive_fields": list(sensitive_fields) if sensitive_fields else None,
            },
            source="chat",
        )

    # ---- RAG 长期记忆增删（2026-09-22 F1；单条事件，不建 attempt 对） ----

    def record_rag_memory(
        self,
        *,
        operation: str,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        memory_id: int | None = None,
        memory_ids: Sequence[int] | None = None,
        affected: int | None = None,
        kind: str | None = None,
        subject: str | None = None,
    ) -> AuditEventRecord:
        """记录一次长期记忆 remember/forget（EVENT_RAG_MEMORY 单条事件）。

        F1（2026-09-22）：三个 RAG 工具是只读工具路径（is_write=False，不建
        task、无确认卡、payload hash 链路不动），但 ``rag_remember`` 跨系统
        新增记忆、``rag_forget`` 软删都**改变存储状态**——与纯读的
        memory_lookup 不同类。用户拍板：**不上确认卡**（否则"记住我的偏好"
        会被反复打断），但必须补一条 agent 侧审计，回答「**谁**（actor_account）
        **何时**（created_at，仓储层落库时间）对**哪条记忆**（target_id/
        memory_ids）做了**什么操作**（operation）」。

        字段落法：``target_type="rag_memory"``；``target_id`` 单条时 = memory_id
        的字符串（batch forget 为 None，逐条见 structured_payload.memory_ids）；
        ``duty_code="MEM"``（与三工具注册的 duty_code 一致）。**只落事实摘要**
        （operation/memory_id(s)/affected/kind/subject），不落记忆正文
        （content 可能含会话隐私，且正文不属审计范围）。审计失败抛
        AuditFailureError（10.4：不假报成功）。
        """
        if operation not in ("remember", "forget"):
            raise ValueError(f"invalid rag memory operation {operation!r}")
        single_id = memory_id
        if single_id is None and memory_ids and len(memory_ids) == 1:
            single_id = memory_ids[0]
        return self.record_event(
            event_type=EVENT_RAG_MEMORY,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            duty_code="MEM",
            target_type="rag_memory",
            target_id=str(single_id) if single_id is not None else None,
            request_summary=(
                f"rag_memory {operation} "
                f"(memory_id={single_id}, affected={affected})"
            ),
            structured_payload={
                "operation": operation,
                "memory_id": memory_id,
                "memory_ids": list(memory_ids) if memory_ids else None,
                "affected": affected,
                "kind": kind,
                "subject": subject,
            },
            source="agent",
        )

    def record_rag_recall(
        self,
        *,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        query_chars: int,
        top_k: int | None = None,
        kind: str | None = None,
        since: str | None = None,
        min_score: float | None = None,
        hit_count: int,
        memory_ids: Sequence[int] | None = None,
        truncated: bool = False,
    ) -> AuditEventRecord:
        """记录一次长期记忆召回（EVENT_RAG_RECALL 单条事件）。

        2026-09-23：``rag_recall`` 是**纯读**（不改变存储状态、无副作用可回滚），
        与 ``rag_remember``/``rag_forget``（EVENT_RAG_MEMORY，改变存储状态）语义
        不同类，故**独立事件类型与方法**，不把 record_rag_memory 的 operation
        白名单扩成 ("remember","forget","recall")。补这条是为了让「这一轮为什么
        这么答」在 agent 侧可追：记忆是被召回但没记、还是压根没调，事后能分出来。

        **不落 query 正文**（与 record_rag_memory「只落事实摘要、不落记忆正文」
        同口径，content 可能含会话隐私；query 是用户会话内容，同理）：只落
        ``query_chars``（长度），不落原文。**本方法签名不得新增任何承载 query
        正文的形参**（唯一含 query 的形参是 ``query_chars``，只有长度）。回答
        「**谁**（actor_account）**何时**（created_at，仓储层落库时间）带了
        **什么条件**（top_k/kind/since/min_score）查了一次，**命中几何**
        （hit_count/memory_ids）」。

        **hit_count 与 memory_ids 的口径（2026-09-23 修复自相矛盾）**：
        ``hit_count`` 是服务端**裁剪前**的命中总数；``memory_ids`` 是命中集的
        **前缀子集**——当包装层 ``_bounded_result`` 因尺寸超预算从命中列表尾部
        丢弃过条目时，它少于 ``hit_count``（按服务端命中顺序取前若干条）。
        ``truncated``（来自信封的 ``truncated`` 标志）即为这处差异留说明：读者
        见 ``hit_count=20`` 而 ``memory_ids`` 只有 5 个时，``truncated=True``
        表示「命中 20、只记了前 5」，从而与「只命中 5 条」（truncated=False，
        二者相等）区分开。**不得**只在 ``memory_ids`` 里记部分 id 却无说明。

        字段落法：``target_type="rag_memory"``；``duty_code="MEM"``（与三工具
        注册的 duty_code 一致）；``target_id=None``（召回不针对单条记忆，命中集
        见 structured_payload.memory_ids）。审计失败抛 AuditFailureError——纯读
        路径无状态可回滚，调用方按下方约定处理（见
        ``rag_memory._record_recall_audit``：读工具审计失败不得让召回失败）。
        """
        return self.record_event(
            event_type=EVENT_RAG_RECALL,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            duty_code="MEM",
            target_type="rag_memory",
            target_id=None,
            request_summary=(
                f"rag_recall (top_k={top_k}, kind={kind}, min_score={min_score}, "
                f"hits={hit_count}, query_chars={query_chars}, "
                f"truncated={truncated})"
            ),
            structured_payload={
                "top_k": top_k,
                "kind": kind,
                "since": since,
                "min_score": min_score,
                "hit_count": hit_count,
                "memory_ids": list(memory_ids) if memory_ids else None,
                "query_chars": query_chars,
                "truncated": truncated,
            },
            source="agent",
        )

    def record_rag_search_documents(
        self,
        *,
        trace_id: str,
        actor_account: str,
        actor_role: str,
        query_chars: int,
        top_k: int | None = None,
        hit_count: int,
        chunk_ids: Sequence[int] | None = None,
        document_ids: Sequence[int] | None = None,
        truncated: bool = False,
    ) -> AuditEventRecord:
        """记录一次文档检索（EVENT_RAG_SEARCH 单条事件）。

        ``rag_search_documents`` 是**纯读**（不改变存储状态、无副作用可回滚），
        与 ``rag_recall`` 同类，故与它同一套口径：补这条是为了让「这一轮为什么
        这么答」可追——文档片段是被检索到但没引用、还是压根没调，事后能分出来。
        与 ``record_rag_recall`` 分开（不扩它的签名）是因为命中的对象不是同一类：
        召回命中的是 ``memory_id``，检索命中的是 ``chunk_id`` / ``document_id``
        （两条线在服务层零交叉）。

        **不落 query 正文**（与 record_rag_recall / record_rag_memory「只落事实
        摘要、不落正文」同口径：query 是用户会话内容）：只落 ``query_chars``
        （长度）。**本方法签名不得新增任何承载 query 正文的形参**（唯一含 query
        的形参是 ``query_chars``，只有长度）。回答「**谁**（actor_account）
        **何时**（created_at，仓储层落库时间）带了**什么条件**（top_k）查了一次，
        **命中几何**（hit_count / chunk_ids / document_ids）」。

        **hit_count 与 ids 的口径**：``hit_count`` 是服务端**裁剪前**的命中总数；
        ``chunk_ids`` / ``document_ids`` 是命中集的**前缀子集**——当包装层
        ``_bounded_result`` 因尺寸超预算从命中列表尾部丢弃过条目时，它们少于
        ``hit_count``。``truncated``（来自信封的 ``truncated`` 标志）即为这处差异
        留说明：读者见 ``hit_count=20`` 而 ids 只有 5 个时，``truncated=True``
        表示「命中 20、只记了前 5」，与「只命中 5 条」（truncated=False，二者相等）
        区分开。

        字段落法：``target_type="rag_document"``；``duty_code="MEM"``（与 RAG 工具
        注册的 duty_code 一致）；``target_id=None``（检索不针对单条，命中集见
        structured_payload 的 chunk_ids / document_ids）。审计失败抛
        AuditFailureError——纯读路径无状态可回滚，调用方按
        ``rag_memory._record_search_audit`` 的约定处理（读工具审计失败不得让
        检索失败）。
        """
        return self.record_event(
            event_type=EVENT_RAG_SEARCH,
            trace_id=trace_id,
            actor_account=actor_account,
            actor_role=actor_role,
            duty_code="MEM",
            target_type="rag_document",
            target_id=None,
            request_summary=(
                f"rag_search_documents (top_k={top_k}, hits={hit_count}, "
                f"query_chars={query_chars}, truncated={truncated})"
            ),
            structured_payload={
                "top_k": top_k,
                "hit_count": hit_count,
                "chunk_ids": list(chunk_ids) if chunk_ids else None,
                "document_ids": list(document_ids) if document_ids else None,
                "query_chars": query_chars,
                "truncated": truncated,
            },
            source="agent",
        )

    # ---- 传输层重试留痕（2026-08-29 补，11.2 规则 3） ----

    def record_mcp_retries(
        self,
        *,
        retries: Sequence[Mapping[str, Any]],
        trace_id: str,
        actor_account: str,
        actor_role: str,
        task_id: str | None = None,
        operation_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        duty_code: str | None = None,
        policy_key: str | None = None,
        policy_version: str | None = None,
        policy_hash: str | None = None,
    ) -> list[AuditEventRecord]:
        """为传输层瞬时重试补记审计事件（每次重试一条）。

        背景：2026-08-27 起瞬时故障重试下沉到 MCP 协议层
        （mcp/client.py::_call_with_retries），业务层只看到最终结果，
        因此这些尝试**不会**各自产生 task_attempts 行。

        为何仍须留痕（11.2 规则 3 可追溯性）：
        - 连接失败在语义上属"结果未知"（13.1）——请求可能已到达服务端并
          执行，只是响应丢失。对写操作而言，这意味着**该次写的结果一度
          不可知**，是可审计性证据，不是可丢弃的传输噪声；
        - 此前该类痕迹只存在于结构化日志环形缓冲
          （observability/log_capture，有界 500 条、进程内存），负载高时
          会被淘汰，无法作为审计依据。

        分层约定（不改动 task_attempts 语义）：
        - task_attempts 一行 = 业务层一次逻辑调用（传输重试不计）；
        - 重试细节进**持久化审计事件流**（event_type=mcp_retry），与
          attempt/result 事件同链（task_id/operation_id/trace_id 一致）。
        调用方只应对**写工具**调用本方法（读工具重试无副作用，落审计会
        淹没真正的"结果未知"证据）。

        写入失败抛 AuditFailureError（10.4：不假报成功）。
        """
        events: list[AuditEventRecord] = []
        for retry in retries:
            attempt_no = retry.get("attempt_no")
            events.append(
                self.record_event(
                    event_type=EVENT_MCP_RETRY,
                    trace_id=trace_id,
                    actor_account=actor_account,
                    actor_role=actor_role,
                    task_id=task_id,
                    operation_id=operation_id,
                    duty_code=duty_code,
                    policy_key=policy_key,
                    policy_version=policy_version,
                    policy_hash=policy_hash,
                    target_type=target_type,
                    target_id=target_id,
                    request_summary=(
                        f"mcp transport retry #{attempt_no} "
                        f"(operation {operation_id})"
                    ),
                    structured_payload=dict(retry),
                    result_code=str(retry.get("error_code"))
                    if retry.get("error_code") is not None
                    else None,
                    source="system",
                )
            )
        return events


def _new_audit_id() -> str:
    return f"aud-{uuid.uuid4().hex[:16]}"


def _new_attempt_id() -> str:
    return f"atp-{uuid.uuid4().hex[:16]}"
