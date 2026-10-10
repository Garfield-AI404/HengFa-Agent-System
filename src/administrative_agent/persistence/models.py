"""Agent 自有表 ORM 模型（规格 12.1 六张表）。

表清单（表名与 12.1 一致）：
- tasks                  ：8.1/8.2 任务记录
- task_attempts          ：11.2 写操作 attempt/result 规则
- review_records         ：7.4 R6 管理者复核记录
- audit_events           ：11.1 业务审计事件（append-only，模型层无 update/delete 路径）
- excel_mapping_versions ：6.3 Excel 映射版本
- excel_mapping_columns  ：6.3 列级映射

设计约束：
- 只管理 Agent 自有表，不建 ERP 镜像表（2.6 规则 5 / 12.1）。
- 结构化字段统一 JSONB（PostgreSQL 16，规格 0.3）。
- ID 风格参考 4.2/7.4 契约：task-…、op-…、trace-…；attempt 用 atp-…、
  审计用 aud-…、复核用 rev-…（ID 值由运行时生成，属 D6/D8）。
- attempt/审计不得存密码、API Key、完整原始文件（11.2 规则 4、12.4）。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """统一声明式基类；Alembic env.py 以 Base.metadata 为 target_metadata。"""


class TaskStatus(str, enum.Enum):
    """8.1 任务八态。状态迁移由确定性代码执行（D6），模型不能直接置 succeeded。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptResultStatus(str, enum.Enum):
    """11.2 规则 2：每次实际调用的 result 状态（不含 pending——result 未写入即未返回）。"""

    SUCCESS = "success"
    REPLAYED = "replayed"
    FAILED = "failed"
    CONFLICT = "conflict"


class ReviewDecision(str, enum.Enum):
    """7.4：复核决定。未复核时 decision 为 NULL。"""

    APPROVED = "approved"
    REJECTED = "rejected"


class MappingStatus(str, enum.Enum):
    """6.3：映射版本状态。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


# ---------------------------------------------------------------------------
# tasks（8.1/8.2）
# ---------------------------------------------------------------------------


class Task(Base):
    """可恢复业务请求的主记录。"""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_user_input', "
            "'waiting_confirmation', 'waiting_review', 'succeeded', "
            "'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        Index("ix_tasks_operation_id", "operation_id"),
        Index("ix_tasks_thread_id", "thread_id"),
        Index("ix_tasks_actor_account", "actor_account"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_created_at", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # task-…
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)  # LangGraph thread
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)  # op-… 稳定标识
    actor_account: Mapped[str] = mapped_column(String(128), nullable=False)  # 真实账号（12.5）
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)  # manager/developer
    duty_code: Mapped[str] = mapped_column(String(16), nullable=False)  # R1-R6
    original_user_message: Mapped[str] = mapped_column(Text, nullable=False)  # 审计最小文本
    structured_intent: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 结构化意图
    current_step: Mapped[str] = mapped_column(String(128), nullable=False, default="created")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TaskStatus.PENDING.value
    )
    pending_questions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    execution_plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confirmed_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 13.1
    final_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# task_attempts（11.2）
# ---------------------------------------------------------------------------


class TaskAttempt(Base):
    """每次实际 ERP 写调用的 attempt/result 记录（11.2 五条规则）。

    - attempt 部分（调用前）：target、field_diffs、idempotency_key / expected_updated_at
    - result 部分（调用后）：result_status、error_code、requires_operator_action
    - 每次实际调用独立一行（attempt_no 递增），网络重试不覆盖成一条（11.2 规则 3）
    """

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("operation_id", "attempt_no", name="uq_task_attempts_operation_no"),
        CheckConstraint(
            "result_status IS NULL OR result_status IN ('success', 'replayed', 'failed', 'conflict')",
            name="ck_task_attempts_result_status",
        ),
        Index("ix_task_attempts_task_id", "task_id"),
        Index("ix_task_attempts_operation_id", "operation_id"),
        Index("ix_task_attempts_result_status", "result_status"),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # atp-…
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 稳定（4.4）
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)  # 第几次实际调用
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)  # 目标实体类型
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 创建前可能无 ID
    field_diffs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 字段差异
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_updated_at: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # 原样存储 datetime string（mcp-api 7.1）
    result_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)  # structuredContent.code
    requires_operator_action: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(  # attempt 记录时间（调用前）
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    result_at: Mapped[datetime | None] = mapped_column(  # result 记录时间（调用后）
        DateTime(timezone=True), nullable=True
    )


# ---------------------------------------------------------------------------
# review_records（7.4）
# ---------------------------------------------------------------------------


class ReviewRecord(Base):
    """R6 管理者复核记录（7.4 全部字段；7.5 共享 manager 账号限制）。"""

    __tablename__ = "review_records"
    __table_args__ = (
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'rejected')",
            name="ck_review_records_decision",
        ),
        Index("ix_review_records_task_id", "task_id"),
        Index("ix_review_records_requested_at", "requested_at"),
    )

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # rev-…
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewed_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewed_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_after_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# audit_events（11.1）
# ---------------------------------------------------------------------------

#: append-only：模型层不提供 update/delete 路径；数据库触发器在首个 Alembic
#: revision 中创建（BEFORE UPDATE OR DELETE RAISE EXCEPTION），
#: 与 11.1"审计是只追加记录，普通应用账号不能 update 或 delete"一致。
AUDIT_EVENTS_APPEND_ONLY = True


class AuditEvent(Base):
    """业务审计事件（11.1 全部字段，只追加）。

    event_type 至少覆盖 11.2 的 attempt/result 与 11.3 的复核事件
    （发起、批准、拒绝、失效、重新复核），完整事件枚举由 D8 定义。
    不得写入密码、API Key、完整原始文件（11.2 规则 4）。
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_task_id", "task_id"),
        Index("ix_audit_events_operation_id", "operation_id"),
        Index("ix_audit_events_trace_id", "trace_id"),
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # aud-…
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="RESTRICT"), nullable=True
    )
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)  # trace-…
    actor_account: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)  # attempt/result/…
    duty_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    policy_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    before_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # O-1（RV-04 决策 c）：DB 层 server_default 由 D8 迁移 92d64bb42eca 补充，
    # 裸 SQL INSERT（绕过 ORM）不再被 NOT NULL 拒；Python default 保留兜底。
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="agent", server_default=text("'agent'")
    )
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ---------------------------------------------------------------------------
# excel_mapping_versions / excel_mapping_columns（6.3）
# ---------------------------------------------------------------------------


class ExcelMappingVersion(Base):
    """Excel 映射版本（6.3）。Agent 无新增/修改/发布/删除映射的工具，
    由人工管理页面维护（D18 提供页面）。"""

    __tablename__ = "excel_mapping_versions"
    __table_args__ = (
        UniqueConstraint("template_key", "version", name="uq_excel_mapping_versions_key_ver"),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_excel_mapping_versions_status",
        ),
        # 6.3 规则 2：同一 template_key 同一时刻只有一个 published 版本
        Index(
            "uq_excel_mapping_versions_published",
            "template_key",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        Index("ix_excel_mapping_versions_template_key", "template_key"),
        Index("ix_excel_mapping_versions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    template_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # 发布新版本不覆盖历史
    sheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    header_row: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MappingStatus.DRAFT.value
    )
    source_file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExcelMappingColumn(Base):
    """列级映射（6.3）。随版本发布快照保存，不随版本退役修改。"""

    __tablename__ = "excel_mapping_columns"
    __table_args__ = (
        UniqueConstraint("version_id", "excel_column", name="uq_excel_mapping_columns_ver_col"),
        Index("ix_excel_mapping_columns_version_id", "version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("excel_mapping_versions.id", ondelete="CASCADE"), nullable=False
    )
    excel_column: Mapped[str] = mapped_column(String(256), nullable=False)  # Excel 列名
    target_field: Mapped[str] = mapped_column(String(128), nullable=False)  # 目标标准字段
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)  # string/int/date/enum/…
    conversion_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enum_mapping: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# chat_archive（2026-08-24 定稿：会话记忆存档）
# ---------------------------------------------------------------------------


class ChatArchive(Base):
    """会话记忆存档（2026-08-24 定稿：压缩前的原始会话全量存储，memory_lookup 查询源）。"""

    __tablename__ = "chat_archive"
    __table_args__ = (
        UniqueConstraint("thread_id", "segment_no", name="uq_chat_archive_thread_segment"),
        Index("ix_chat_archive_thread_id", "thread_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    segment_no: Mapped[int] = mapped_column(Integer, nullable=False)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{role, content}] 文本形态（非消息对象）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# tool_query_archive（2026-08-28 两级数据访问：工具查询原始数据存档）
# ---------------------------------------------------------------------------


class ToolQueryArchive(Base):
    """工具查询存档（2026-08-28 两级数据访问方案：查询结果原始全量明细
    落库，memory_lookup 按 query_id 查回；[2026-10-06 §3.8] 扩展为
    读工具调用参数存档——只读工具在「正常返回 + 执行抛异常 + 参数校验
    失败」三条路径各落一行，不落行的分支见下方枚举）。

    - 两级数据访问（原语义）：查询类工具成功路径在结果写对话历史前写入，
      明细隔轮驱逐后（或深分析需要原始噪音字段时）经 memory_lookup(query_id)
      查回本表原始全量（含噪音字段）；
    - [2026-10-06 §3.8 读工具调用参数落审计] **只读工具
      （``spec.is_write=False``）在正常返回（成功 / 0 行 / ok=False）+
      执行抛异常 + 参数校验失败三条路径各落一行**——在原有「成功 + 非空
      items + 存档标识」之外，还包含「成功但返回 0 行」「失败（ok=False）」
      的调用，使「模型到底查了什么 filter/fields/时间窗、成败如何」可回放
      （此前三条通道都拿不到：SSE tool_running 只有工具名、chat_stream_events
      按设计不落模型输入、checkpoint 只记写动作）。**不落行的分支**（勿读成
      「任何只读调用都落一行」）：未知工具（无 ToolSpec）、``NotImplementedError``
      （工具未接线，直接返回错误 ToolMessage）、写工具、archive 未接线。新增两列：
      * ``arguments``：本次调用**模型传的入参**（``args_schema`` 校验后的
        pydantic 模型 → ``model_dump(mode="json")``）；历史行 / 缺参为 NULL；
      * ``ok``：本次调用成败（True=成功、False=失败）；历史行为 NULL。
      两列均可空 → 历史行不受影响（NULL）。**注意**：挂给模型的 ``query_id``
      （可回查标识）**不因本扩展而放宽**——仍仅在「成功 + 非空 items +
      存档标识」成立时挂（见 tools_node），故模型可见行为零变化；
      `arguments`/`ok` 仅供取证回放，不进模型上下文。
    - 11.4 合规：仅会话内可查（查询按 thread_id + query_id）、随会话删除
      清理（routes_chat.delete_thread → ArchiveStore.delete_thread 覆盖
      本表）；不属审计记录，不设 append-only。
    """

    __tablename__ = "tool_query_archive"
    __table_args__ = (
        UniqueConstraint("thread_id", "query_id", name="uq_tool_query_archive_thread_query"),
        Index("uq_tool_query_archive_query_id", "query_id", unique=True),  # query_id 全局唯一（tool-call-… UUID）
        Index("ix_tool_query_archive_thread_id", "thread_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    query_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 工具调用标识（tool-call-…）
    entity: Mapped[str] = mapped_column(String(64), nullable=False)  # 实体名/信封标记或工具名（metal_orders、catalog/erp_calc 等；[2026-09-09] 查询类工具外带 items 的结果同样存档，裁剪按标记原样返回；[2026-10-06 §3.8] 无 entity 的读工具（memory_lookup/export_data/render_* 等）以工具名作标记）
    count: Mapped[int] = mapped_column(Integer, nullable=False)  # 明细条数（无 items 的读工具为 0）
    items: Mapped[list] = mapped_column(JSONB, nullable=False)  # 原始全量明细（不裁剪）；无 items 的读工具为 []
    # [2026-10-06 §3.8] 模型本次入参（JSON 形态）；历史行为 NULL。
    arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # [2026-10-06 §3.8] 本次调用成败；历史行为 NULL。
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    queried_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# chat_threads（2026-08-24：对话会话索引）
# ---------------------------------------------------------------------------


class ChatThread(Base):
    """对话会话索引（2026-08-24：checkpoint 只有 thread_id 粒度、无
    「账号 → 线程列表」索引——本表补齐会话列表/恢复/删除的账目层）。

    - thread_id 主键：与 LangGraph checkpoint 的 thread_id 一一对应；
    - account：归属账号（会话列表只返回当前账号；删除时校验归属，
      account 不匹配一律 404，不泄露存在性）；
    - title：会话标题 = 首条用户消息截断 40 字符（upsert 只首插不覆盖）；
    - updated_at：每次提交消息更新（列表按 updated_at DESC 排序）。
    """

    __tablename__ = "chat_threads"
    __table_args__ = (
        Index("ix_chat_threads_account", "account"),
    )

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# chat_attachments / reconcile_reports（2026-08-31 对话对账批次 1）
# ---------------------------------------------------------------------------


class ChatAttachment(Base):
    """对话附件元数据（2026-08-31 对话对账：上传 Excel 的临时附件登记）。

    - attachment_id：att-<uuid16>（运行时生成）；
    - sha256：文件指纹（去重与审计定位）；
    - expires_at：attachment_ttl_seconds 后过期（启动时 + 每次上传时
      清过期）；内容落 attachment_dir 临时目录，本表只存元数据——
      6.5/11.2 规则 4：不保存完整原始文件。
    """

    __tablename__ = "chat_attachments"
    __table_args__ = (
        Index("ix_chat_attachments_thread_id", "thread_id"),
        Index("ix_chat_attachments_expires_at", "expires_at"),
    )

    attachment_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # att-…
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ReconcileReport(Base):
    """对账分析报告（2026-08-31 对话对账：Excel 对账结果跨轮确认载体）。

    - status：analyzed → confirmed → executed（合法迁移由
      domains.reconcile_reports.ReconcileReportStore 校验，DB 层
      CheckConstraint 兜底）；
    - plan_hash：confirm 后回填（写入计划 hash，与 7.4 复核机制联动）；
    - *_json 列：JSON 文本（报告载荷：summary/conversion_errors/entries/
      rows/limitations）；expires_at 后过期清理（TTL 24h 跨轮确认窗口）。
    """

    __tablename__ = "reconcile_reports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('analyzed', 'confirmed', 'executed')",
            name="ck_reconcile_reports_status",
        ),
        Index("ix_reconcile_reports_thread_id", "thread_id"),
        Index("ix_reconcile_reports_file_id", "file_id"),
        Index("ix_reconcile_reports_expires_at", "expires_at"),
    )

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # rpt-…
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 附件 id（chat_attachments）
    template_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_name: Mapped[str] = mapped_column(String(256), nullable=False)
    period_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    conversion_errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    entries_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rows_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    data_completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    limitations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="analyzed")
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ---------------------------------------------------------------------------
# chat_stream_events（2026-09-19 可观测性第 0 步：SSE 事件流结构性元数据落库）
# ---------------------------------------------------------------------------


class ChatStreamEventRow(Base):
    """SSE 事件流结构性元数据（A 档：只落结构，绝不落模型输入输出原文）。

    事故现场易失：``web/chat_streams.py`` 的 ``StreamStore`` 是纯内存
    （重启即丢），而决策轮的模型输入输出只在事件流里留痕。本表把事件流
    按 A 档粒度持久化，容器重建后仍可回答「昨天那次失败对话在第几步、
    调了什么工具、确认卡长什么样、为什么被抑制」。

    - ``payload`` 是经 ``web/stream_projection.py`` **投影后**的结构
      （键白名单 + 降维），不是原始 payload；
    - ``record_kind``：``pushed``（已推给浏览器）/``suppressed``
      （被展示门控丢弃的中间轮 agent_reply 影子行）/``stream_created``
      （流创建行）/``unmapped``（未声明事件类型，payload 空）；
    - **主键用业务键 ``event_id`` 而非 ``Identity()``**：① 事件 ID 天然
      唯一；② 批量写重试靠 ``ON CONFLICT DO NOTHING`` 幂等；③ 避开
      SQLite 测试垫片坑——``tests/api/conftest.py`` 只垫了 ``JSONB→JSON``
      与 ``BigInteger→INTEGER``，没有 ``autoincrement`` 方言选项，用
      ``Identity`` 会让 ``create_all`` 建出的表在 INSERT 时因
      ``id NOT NULL`` 失败；
    - **不落 ``account``**：身份数据，且 ``stream → thread → account``
      可由 ``chat_threads`` 推出，少一列少一条暴露面；
    - **``event_type`` 不设枚举 CheckConstraint**：本仓半年加了 8 个事件
      类型，DB 枚举会让每次加类型都变成一次迁移；枚举性由投影白名单在
      代码层 fail-closed 保证（真正的第二道锁是迁移里的 payload 约束）。

    ⚠️ **ORM 与迁移的漂移点（勿删）**：迁移
    ``alembic/versions/*_create_chat_stream_events_table.py`` 额外含三条
    **PG-only** 约束（``jsonb_typeof(payload) = 'object'``、
    ``pg_column_size(payload) <= 4096``、两个 chunk 事件的 ``payload``
    **只允许 ``chunk_chars`` 这一个数字键**）。它们**刻意不写进本文件的 ``__table_args__``**——
    ``jsonb_typeof``/``pg_column_size`` 是 PG 函数，写进 ORM 会让
    ``tests/api/conftest.py`` 的 SQLite ``create_all`` 报
    ``no such function``，炸掉整个 API 测试套件。代价是
    ``alembic --autogenerate`` 会误报这两条约束差异，**本仓约定手写迁移**。

    ``payload`` 的 ``server_default`` 用跨方言的 ``'{}'``（而非 PG 专属的
    ``'{}'::jsonb``，后者会让 SQLite ``create_all`` 报
    ``unrecognized token ':'``）；迁移里写显式 ``'{}'::jsonb``。
    """

    __tablename__ = "chat_stream_events"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="ck_chat_stream_events_sequence"),
        CheckConstraint(
            "record_kind IN ('pushed', 'suppressed', 'stream_created', 'unmapped')",
            name="ck_chat_stream_events_record_kind",
        ),
        Index("ix_chat_stream_events_stream_seq", "stream_id", "sequence"),
        Index("ix_chat_stream_events_thread_created", "thread_id", "created_at"),
        Index("ix_chat_stream_events_task_created", "task_id", "created_at"),
        Index("ix_chat_stream_events_created_at", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    record_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pushed", server_default=text("'pushed'")
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
