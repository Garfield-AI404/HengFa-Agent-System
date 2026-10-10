"""API 请求/响应 Schema 与统一错误契约（规格 2.7.3）。

所有写 API 必须返回稳定的 error_code、message、details、trace_id。
前端展示 message，但分支逻辑只读取 error_code（2.7.3）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def new_trace_id() -> str:
    """最小 trace_id 生成（完整可观测性属 D8）。"""
    return "trace-" + uuid.uuid4().hex[:16]


class ErrorCode(str, Enum):
    """稳定错误码全集（新增枚举前先过 Review）。"""

    # 认证与会话（D4）
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    CSRF_VALIDATION_FAILED = "CSRF_VALIDATION_FAILED"
    CREDENTIALS_NOT_CONFIGURED = "CREDENTIALS_NOT_CONFIGURED"
    # 通用
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # Excel 映射管理（D18，6.3；新增枚举经 RV-27 Review 登记）
    MAPPING_NOT_FOUND = "MAPPING_NOT_FOUND"
    MAPPING_VALIDATION_FAILED = "MAPPING_VALIDATION_FAILED"
    MAPPING_INVALID_STATE = "MAPPING_INVALID_STATE"
    MAPPING_PUBLISHED_EXISTS = "MAPPING_PUBLISHED_EXISTS"
    MAPPING_PUBLISHED_NOT_FOUND = "MAPPING_PUBLISHED_NOT_FOUND"
    # R6 manager 复核（D20，7.4/2.7.3；新增枚举经 RV-30 Review 登记）
    REVIEW_NOT_FOUND = "REVIEW_NOT_FOUND"
    REVIEW_INVALID_STATE = "REVIEW_INVALID_STATE"
    REVIEW_HASH_MISMATCH = "REVIEW_HASH_MISMATCH"
    REVIEW_REJECTION_REASON_REQUIRED = "REVIEW_REJECTION_REASON_REQUIRED"
    REVIEW_ALREADY_PENDING = "REVIEW_ALREADY_PENDING"
    # D22 前端整合 chat/tasks/audit（2.7.3/2.7.4；新增枚举经 RV-33/34 Review 登记）
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_INVALID_STATE = "TASK_INVALID_STATE"
    TASK_ANSWERS_MISMATCH = "TASK_ANSWERS_MISMATCH"
    CONFIRM_HASH_MISMATCH = "CONFIRM_HASH_MISMATCH"
    STREAM_NOT_FOUND = "STREAM_NOT_FOUND"
    MODEL_PIPELINE_NOT_CONNECTED = "MODEL_PIPELINE_NOT_CONNECTED"
    # [2026-08-24 harness 改造] Phase 2 /chat/confirm（新增枚举经 Review 登记）：
    # 线程无挂起确认卡（load_thread 无 pending_confirmation）或任务不在
    # waiting_confirmation——恢复指令无挂起 interrupt 可消费，拒绝 409。
    CONFIRM_NOT_PENDING = "CONFIRM_NOT_PENDING"
    # 确认恢复执行失败（图执行/checkpoint 基础设施异常，fail closed 500；
    # 不泄露内部细节，14.4）。
    CONFIRM_RESUME_FAILED = "CONFIRM_RESUME_FAILED"
    # [2026-08-24 harness 改造] Phase 3a /chat/messages（新增枚举经 Review 登记）：
    # 同一 thread_id 已有活动图循环（进程内并发防护，防重复提交）→ 409。
    THREAD_BUSY = "THREAD_BUSY"
    # chat 图执行失败（checkpoint/图基础设施异常，fail closed 500；不泄露
    # 内部细节，14.4；与 CONFIRM_RESUME_FAILED 同口径的 submit 侧码）。
    CHAT_RUN_FAILED = "CHAT_RUN_FAILED"
    # 优雅停机（D25，15.3 步骤 7；新增枚举经 RV-38 Review 登记）
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    # 对话导出（2026-09-01 导出功能批次；下载端点错误码）
    EXPORT_FILE_NOT_FOUND = "EXPORT_FILE_NOT_FOUND"
    EXPORT_FILE_EXPIRED = "EXPORT_FILE_EXPIRED"
    EXPORT_NOT_CONFIGURED = "EXPORT_NOT_CONFIGURED"


class ErrorResponse(BaseModel):
    """统一错误响应体（2.7.3）。"""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    details: object | None = None
    trace_id: str


def make_error_response(
    error_code: str,
    message: str,
    details: object | None = None,
    trace_id: str | None = None,
) -> ErrorResponse:
    """构造统一错误响应；trace_id 缺失时自动生成。"""
    return ErrorResponse(
        error_code=error_code,
        message=message,
        details=details,
        trace_id=trace_id or new_trace_id(),
    )


# ---------------------------------------------------------------------------
# auth 请求/响应（2.7.3）
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """登录请求：账号与密码（manager/developer 独立入口）。"""

    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class AuthUserResponse(BaseModel):
    """登录成功 / me 的响应体。"""

    model_config = ConfigDict(extra="forbid")

    account: str
    role: str  # manager | developer
    shared_manager_note: str | None = None  # 12.5：共享 manager 账号提示


class MessageResponse(BaseModel):
    """通用成功响应。"""

    model_config = ConfigDict(extra="forbid")

    message: str


# ---------------------------------------------------------------------------
# Excel 映射管理（D18，6.3；2.7.3 API 契约）
# ---------------------------------------------------------------------------


class MappingColumnIn(BaseModel):
    """列级映射定义（6.3：Excel 列名/目标标准字段/类型与转换规则/必填/
    枚举映射/默认值）。"""

    model_config = ConfigDict(extra="forbid")

    excel_column: str = Field(min_length=1, max_length=256)
    target_field: str = Field(min_length=1, max_length=128)
    data_type: Literal["string", "int", "decimal", "date", "enum", "bool"]
    conversion_rule: str | None = None
    is_required: bool = False
    enum_mapping: dict[str, str] | None = None
    default_value: str | None = None


class MappingImportRequest(BaseModel):
    """导入 draft 映射（POST /api/v1/mappings/import）。

    source_columns：Excel 实际列名清单（预览/导入时人工提供），用于
    规则 4 未知列告警与规则 3 必填列比对；不提供时只做定义自洽校验。
    required_columns：模板必含列（人工声明），缺失 → 停止（规则 3）。
    """

    model_config = ConfigDict(extra="forbid")

    template_key: str = Field(min_length=1, max_length=128)
    sheet_name: str = Field(min_length=1, max_length=128)
    header_row: int = Field(default=1, ge=1)
    source_file_hash: str = Field(min_length=1, max_length=64)
    columns: list[MappingColumnIn] = Field(min_length=1)
    source_columns: list[str] | None = None
    required_columns: list[str] | None = None


class MappingColumnOut(BaseModel):
    """列级映射响应。"""

    model_config = ConfigDict(extra="forbid")

    excel_column: str
    target_field: str
    data_type: str
    conversion_rule: str | None = None
    is_required: bool = False
    enum_mapping: dict[str, str] | None = None
    default_value: str | None = None


class MappingVersionOut(BaseModel):
    """映射版本响应（6.3 版本字段 + 列级快照）。"""

    model_config = ConfigDict(extra="forbid")

    version_id: int
    template_key: str
    version: int
    sheet_name: str
    header_row: int
    status: str
    source_file_hash: str
    created_by: str
    created_at: datetime
    published_at: datetime | None = None
    columns: list[MappingColumnOut] = Field(default_factory=list)


class MappingImportResult(BaseModel):
    """导入结果（页面"预览"能力：映射表 + 未知列告警 + 缺必填列清单）。

    errors 非空 → 校验失败未产生版本（HTTP 400 MAPPING_VALIDATION_FAILED，
    details 携带本结构）；warnings 非空但 errors 空 → draft 已建立，
    前端展示未知列告警（规则 4）。
    """

    model_config = ConfigDict(extra="forbid")

    version: MappingVersionOut | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class MappingListResponse(BaseModel):
    """版本列表（D17 QueryPage 分页语义：items/total/limit/offset）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[MappingVersionOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# R6 manager 复核（D20，7.4/2.7.3；/reviews 页面后端底座，D22 前端）
# ---------------------------------------------------------------------------


class ReviewOut(BaseModel):
    """复核记录（7.4 十二字段；复核详情与队列共用）。"""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    task_id: str
    requested_by: str
    requested_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    decision: str | None = None
    reviewed_payload: dict[str, Any] | None = None
    reviewed_payload_hash: str
    changed_after_review: bool = False
    invalidated_at: datetime | None = None
    rejection_reason: str | None = None


class ReviewListResponse(BaseModel):
    """复核队列（2.7.3 GET /api/v1/reviews 分页；pending_only 语义）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewOut]
    total: int
    limit: int
    offset: int


class ReviewApproveRequest(BaseModel):
    """批准复核（2.7.3 L514：批准当前 payload_hash）。

    payload_hash 可选：调用方提供时服务端校验其与复核记录一致，不一致
    拒绝（REVIEW_HASH_MISMATCH，提示重新复核）。
    """

    model_config = ConfigDict(extra="forbid")

    payload_hash: str | None = Field(default=None, min_length=16, max_length=64)


class ReviewRejectRequest(BaseModel):
    """拒绝复核并提交原因（2.7.3；7.4 rejection_reason 必填）。"""

    model_config = ConfigDict(extra="forbid")

    rejection_reason: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# chat（D22 前端整合，2.7.3 L504-506 / 2.7.4 L530-534）
# ---------------------------------------------------------------------------


class ChatMessageRequest(BaseModel):
    """提交消息（POST /api/v1/chat/messages）。

    [2026-08-24 harness 改造] Phase 3a：thread_id 为会话线程（checkpoint
    重放粒度）；可缺省（服务端分配 thread-{account}-{uuid8} 并返回），
    Phase 3b 前端起显式传（新对话 = 新 thread_id，localStorage 持久化）。

    [2026-08-31 对话对账批次 3] attachment_ids：本轮消息引用的附件
    （POST /attachments 上传返回的 attachment_id 列表，至多 10 个）；
    服务端逐个校验存在/未过期/属于当前会话，任一不满足整条拒绝
    （fail closed），附件说明以 [附件：…] 前缀拼入模型消息。
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=10000)
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    attachment_ids: list[str] | None = Field(default=None, max_length=10)


class ChatMessageResponse(BaseModel):
    """消息受理响应（[2026-08-24 harness 改造] Phase 3a）。

    - thread_id：会话线程（缺省时服务端生成并返回，前端据此持久化）；
    - stream_id：本轮事件流（SSE 重放，2.7.4）；
    - task_id：可空——纯对话轮次无任务（None）；本轮写工具已建写任务
      （挂起确认）时有值。旧 intent 字段随意图全链路移除（Phase 3a）。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    stream_id: str
    task_id: str | None = None


class ChatHistoryMessage(BaseModel):
    """[2026-08-24 harness 改造] Phase 3a：历史消息（/chat/history 重放）。

    LangChain 消息 → 前端形态：role ∈ {human, ai, tool}，content 为消息
    正文（工具消息为 JSON 字符串；task 信息不逐条携带——前端从事件流
    已有信息补，history 层不强求每消息 task_id）。
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class ChatHistoryOut(BaseModel):
    """[2026-08-24 harness 改造] Phase 3a：对话历史（GET /api/v1/chat/history）。

    - messages：checkpoint 重放（无快照 → []）；
    - pending_confirmation：挂起确认卡（六要素 + payload_hash + task_id；
      不含执行上下文 execution——前端确认只回 payload_hash，服务端从
      checkpoint 取执行载荷）；无挂起 → null；
    - stream_id：线程最近事件流（StreamStore.latest_stream_id；进程重启
      丢流 → null，前端只重放历史不订阅）。
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatHistoryMessage] = Field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    stream_id: str | None = None


class ChatConfirmRequest(BaseModel):
    """[2026-08-24 harness 改造] Phase 2：轻确认决策（POST /api/v1/chat/confirm）。

    - thread_id：会话线程（挂起确认卡所在线程；与任务 thread_id 必须一致）；
    - approved：True 继续执行（载荷 = 已批准的卡片），False 取消（8.1）；
    - payload_hash：客户端展示卡片的哈希（7.3 变化失效依据）——与挂起
      卡片不一致时重新 interrupt（确认失效，等待重新确认）。

    命名避开旧 ConfirmRequest（tasks replies 轻确认，2.7.3 L510；同模块
    后定义者胜——chat 侧用独立类名防漂移，routes_tasks.py 零改动）。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=128)
    approved: bool
    payload_hash: str = Field(min_length=1, max_length=128)


class ChatConfirmResponse(BaseModel):
    """确认受理响应：恢复执行的事件流 stream_id（SSE 重放新事件）。

    恢复产生的图内事件（confirmation_requested 重新发射 / tool_running /
    tool_result / agent_reply / re_requested）追加到该流；前端以
    POST /api/v1/chat/streams/{stream_id} 重放（2.7.4 断线续传语义）。
    """

    model_config = ConfigDict(extra="forbid")

    stream_id: str


class ChatEventOut(BaseModel):
    """SSE 事件（2.7.4 L530：event_id/event_type/task_id/sequence/payload）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    task_id: str
    sequence: int
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# tasks（D22 前端整合，2.7.3 L507-511）
# ---------------------------------------------------------------------------


class TaskOut(BaseModel):
    """任务记录（8.2 字段；任务列表/详情共用）。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    thread_id: str
    operation_id: str
    actor_account: str
    actor_role: str
    duty_code: str
    original_user_message: str
    structured_intent: dict[str, Any] = Field(default_factory=dict)
    current_step: str
    status: str
    pending_questions: list[str] | None = None
    execution_plan: dict[str, Any] | None = None
    confirmed_payload_hash: str | None = None
    reviewed_payload_hash: str | None = None
    checkpoint_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    final_error_code: str | None = None
    final_error_summary: str | None = None


class TaskListResponse(BaseModel):
    """任务列表（2.7.3 GET /api/v1/tasks 分页；created_at 倒序）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[TaskOut]
    total: int
    limit: int
    offset: int


class TaskAttemptOut(BaseModel):
    """写操作 attempt（11.2；任务详情展示 attempt/result）。"""

    model_config = ConfigDict(extra="forbid")

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


class TaskEventOut(BaseModel):
    """任务相关审计事件（任务详情"步骤"展示；脱敏输出）。"""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    event_type: str
    task_id: str | None = None
    operation_id: str | None = None
    duty_code: str | None = None
    policy_key: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    request_summary: str | None = None
    structured_payload: dict[str, Any] | None = None
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    source: str | None = None
    result_code: str | None = None
    created_at: datetime | None = None


class TaskDetailOut(BaseModel):
    """任务详情（2.7.3 GET /api/v1/tasks/:taskId：步骤/attempt/result）。"""

    model_config = ConfigDict(extra="forbid")

    task: TaskOut
    events: list[TaskEventOut] = Field(default_factory=list)
    attempts: list[TaskAttemptOut] = Field(default_factory=list)


class RepliesRequest(BaseModel):
    """回答 pending_questions（2.7.3 L509；8.3 第 2 条显式绑定 task_id）。"""

    model_config = ConfigDict(extra="forbid")

    answers: list[str] = Field(min_length=1, max_length=50)


class ConfirmRequest(BaseModel):
    """提交轻确认（2.7.3 L510；7.3 confirmed_payload_hash 机制）。

    payload_hash 可选：提供时服务端校验与任务确认内容一致（7.3 L886：
    执行内容/目标记录/关键字段变化 → 原确认失效，必须重新确认）。
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    payload_hash: str | None = Field(default=None, min_length=16, max_length=64)


# ---------------------------------------------------------------------------
# audit（D22 前端整合，2.7.3 L516）
# ---------------------------------------------------------------------------


class AuditEventOut(BaseModel):
    """业务审计事件（11.1；脱敏输出——structured_payload/前后快照经
    audit/redaction.py 处理）。"""

    model_config = ConfigDict(extra="forbid")

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
    source: str | None = None
    result_code: str | None = None
    created_at: datetime | None = None


class AuditListResponse(BaseModel):
    """业务审计分页（2.7.3 GET /api/v1/audit；11.1 append-only 只读）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[AuditEventOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# D23 developer 页面（2.7.3 L520-521 / 10.5；新增枚举经 RV-35/36 Review 登记）
# ---------------------------------------------------------------------------


class DependencyStatus(BaseModel):
    """健康检查中的单个依赖状态（无业务数据、无凭据，12.4）。"""

    model_config = ConfigDict(extra="forbid")

    name: str  # erp_mcp / agent_database / checkpoint_database / session
    status: str  # configured / missing_credentials / not_configured / invalid
    detail: str | None = None  # 只含 host/engine 等非敏感信息


class StreamEventSinkStatus(BaseModel):
    """SSE 事件落库 sink 只读快照（可观测性第 0 步；developer 面板用）。

    **字段命名纪律**（见 ``audit/redaction.py`` 的 ``is_sensitive_key``——按
    **子串**匹配）：键名含 ``token/key/pwd/secret/credential/密码/密钥`` 即被
    脱敏打死。以下命名全干净，**绝不要**引入 ``dropped_tokens`` /
    ``credential_status`` / ``api_key_errors`` 之类。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool  # False = 数据库未配置（database_not_configured）
    queued: int  # 当前积压
    written: int  # 累计成功写入
    dropped: int  # 队列满丢弃
    write_failures: int  # DB 写失败批次数
    unmapped_events: int  # 未映射事件计数（fail-closed 可见性）
    oversize_dropped: int  # 单行 payload 超限被预检剔除的行数（不拖累整批）
    pruned_rows: int  # 累计清理行数
    last_write_age_seconds: float | None = None  # 距上次成功写入秒数
    degraded_reason: str | None = None  # database_not_configured / database_unavailable / None
    #: 最近一次写失败的异常**类名**（``IntegrityError`` / ``OperationalError`` …；
    #: 绝非 ``str(exc)``）。与 ``degraded_reason`` 互补——后者只答「降级了没」，
    #: 它答「为什么降级」，让「投影 ↔ 约束漂移 / 表未迁移」不再只能翻容器日志。
    last_error_kind: str | None = None


class DeveloperHealthResponse(BaseModel):
    """GET /api/v1/developer/health（developer 鉴权版；main.py /health
    为无鉴权运维探活，两者并存）。"""

    model_config = ConfigDict(extra="forbid")

    status: str  # ok
    app: str
    version: str
    log_level: str
    dependencies: list[DependencyStatus]
    # [2026-09-19 可观测性第 0 步] 新增**顶层字段**（不进 dependencies——
    # test_developer_api 断言依赖清单集合完全相等）。sink 未接线 → None，
    # 以区分「压根没接线」与「接线了但队列为 0」。老前端忽略未知字段。
    stream_events: StreamEventSinkStatus | None = None


class LogEntryOut(BaseModel):
    """脱敏技术日志条目（10.6 长度限制 + 11.2 规则 4 敏感字段）。"""

    model_config = ConfigDict(extra="forbid")

    timestamp: str | None = None
    level: str | None = None
    event: str | None = None
    logger: str | None = None
    payload: dict[str, Any] = {}


class DeveloperLogsResponse(BaseModel):
    """GET /api/v1/developer/logs（最新在前；11.4 不保存完整对话/思考）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[LogEntryOut]
    total: int
    limit: int


# ---------------------------------------------------------------------------
# 可观测性第 1 步：任务时间线视图（developer；只读 chat_stream_events）
#
# 口径：**不看原文**。本批只把第 0 步落库的 A 档结构量读出来——
# 不新增落库字段、不改投影白名单、零迁移，因而不扩大数据暴露面。
#
# 字段命名纪律（同 ``StreamEventSinkStatus``）：``audit/redaction.py`` 的
# ``is_sensitive_key`` 是**子串**匹配，键名含 ``token/key/pwd/secret/
# credential/密码/密钥`` 即被打成 ``***``。本批字段名逐个核过全部安全。
# ---------------------------------------------------------------------------


class StreamEventThreadSummary(BaseModel):
    """最近会话列表的一行（纯聚合；**不含会话标题/归属**）。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    #: 保留期内该会话的**全部**事件行数（含 suppressed / stream_created）
    event_count: int
    #: COUNT(DISTINCT stream_id)：这次对话产生了几条流（几轮）
    stream_count: int
    #: 分片行数（agent_reply_chunk + agent_reasoning_chunk）。**必须有**：
    #: 否则 event_count 会被误读成「812 个步骤」，而真实步骤只有几步
    chunk_count: int
    #: **显式闭集** ('agent_error','task_failed','memory_compression_failed')
    #: 的计数。绝不 ``LIKE '%error%'``，也绝不算 ``task_cancelled`` /
    #: ``SESSION_EXPIRED``——用户主动取消 / 会话过期**不是故障**
    error_count: int
    #: record_kind='suppressed' 的影子行数（被展示门控抑制但已落库）
    suppressed_count: int
    #: record_kind='unmapped' 的行数——投影白名单与事件常量**漂移**的信号
    #: （第 0 步只在进程内计数、重启即丢，落到这里才能按会话看见）
    unmapped_count: int
    #: 该会话最早 / 最晚事件时间（UTC，带 ``+00:00`` 偏移）。**裸 ``str``，
    #: 不是 ``str | None``**：``created_at`` 是 NOT NULL 列、``min/max`` 落在
    #: ``GROUP BY`` 的非空分组上 → **永不为 null**，声明成 Optional 就是在说谎
    #: （标可选会让下游用 ``?? 0`` 把真 bug 掩成 0）。
    first_seen: str
    last_seen: str


class StreamEventThreadsResponse(BaseModel):
    """GET /api/v1/developer/stream-events/threads（最近会话列表）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[StreamEventThreadSummary]
    total: int
    limit: int
    offset: int
    #: ``thread_id IS NULL`` 的孤儿行数（0 = 无）。**放顶层而非 item**：
    #: 孤儿行点不进去，不显式计数就会静默消失
    untracked_events: int


class StreamEventRowOut(BaseModel):
    """会话时间线的一行（一条事件一行，忠实呈现库里的行，不做语义聚合）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    stream_id: str
    sequence: int
    event_type: str
    record_kind: str
    task_id: str | None = None
    #: 投影后的结构量。**显式标量联合，不用 ``Any``**——这是契约声明
    #: （投影只放行标量 + ``list[str]``）；将来有人往里塞嵌套结构会
    #: fail closed（响应校验失败 → 500），而不是静默通过
    payload: dict[str, str | int | float | bool | list[str] | None]
    #: **裸 ``str``**：来源是 NOT NULL 的 ``created_at`` 列，永不为 null
    created_at: str


class StreamEventTimelineResponse(BaseModel):
    """GET /api/v1/developer/stream-events/threads/{thread_id}（事件流水）。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    items: list[StreamEventRowOut]
    #: 该会话**全部**行数（与 items 同口径 → 分页一致）
    total: int
    limit: int
    offset: int
    #: Σ每流 ``MAX(sequence)`` over ``record_kind ∈ ('pushed','unmapped')``：
    #: 真实流事件「应有」条数（**前提：每流 sequence 从 1 连续**）
    expected_events: int
    #: ``max(0, expected_events − 实际真实事件条数)``。**本步最有价值的字段**：
    #: >0 意味着 sink 的队列满丢弃 / 失败批次丢弃 / 超限丢弃**真的发生过**，
    #: 且可回溯到具体会话（clamp 到 0：宁可报 0 也不报负数）
    missing_events: int
