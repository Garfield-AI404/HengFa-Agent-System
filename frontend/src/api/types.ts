/**
 * D22/D23 API 契约类型（手写补充；契约纪律 2.7.3/2.7.7：只能来自后端 OpenAPI
 * 或经 Review 的手写补充，D4 起沿用 auth.types.ts 先例）。
 *
 * 来源（三层）：
 * - 2.7.3 API 契约表（L499-522）与 D22/D23 任务书端点清单；
 * - 后端已建模块 web/schemas.py 与 web/routes_*.py 的实际形状（D23 联调窗口
 *   对齐：chat/tasks/audit 端点 e7a798e、developer 端点 D23）；
 * - 运行时校验：src/api/schemas.ts（规则 10 L448：Zod 校验运行时响应，
 *   TypeScript 类型不能代替运行时校验）。
 */

// ---------------------------------------------------------------------------
// 通用分页（D17 QueryPage 语义：items/total/limit/offset，全后端统一）
// ---------------------------------------------------------------------------

export interface QueryPage<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// ---------------------------------------------------------------------------
// 统一错误契约（2.7.3：前端展示 message，分支逻辑只读 error_code）
// ---------------------------------------------------------------------------

/** 稳定错误码全集（后端 web/schemas.py ErrorCode + 前端独有码：NETWORK_ERROR /
 *  PAYLOAD_TOO_LARGE / UNKNOWN_ERROR / THREAD_MISSING，后端不返回这几个）。 */
export const ERROR_CODES = {
  INVALID_CREDENTIALS: 'INVALID_CREDENTIALS',
  UNAUTHENTICATED: 'UNAUTHENTICATED',
  SESSION_EXPIRED: 'SESSION_EXPIRED',
  FORBIDDEN: 'FORBIDDEN',
  CSRF_VALIDATION_FAILED: 'CSRF_VALIDATION_FAILED',
  CREDENTIALS_NOT_CONFIGURED: 'CREDENTIALS_NOT_CONFIGURED',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  NETWORK_ERROR: 'NETWORK_ERROR',
  // [2026-09-11 上传链路收口] 413 通用语义：nginx client_max_body_size 网关拦截
  // （返回 HTML 错误页，非 JSON）与未来后端 413 都归此码，文案按「文件过大」写；
  // 前端体积预校验也复用此码（同语义：文件过大），从而与其它 VALIDATION_ERROR
  // 来源（zod/后端 422/parseWith）区分开，避免附件操作误清无关错误。
  PAYLOAD_TOO_LARGE: 'PAYLOAD_TOO_LARGE',
  // 客户端兜底码：非 ApiError/ZodError/TypeError 的未知异常（如代码缺陷、
  // HTTP 200 但响应体非 JSON 的 SyntaxError）。如实但不误导——不谎称网络问题，
  // 也不把英文原始异常直接抛给中文界面，message 由 ErrorNotice 的 HINT 覆盖。
  UNKNOWN_ERROR: 'UNKNOWN_ERROR',
  // 前端前置条件缺失（非网络故障）：如无 threadId 无法拉对账明细。前端独有，
  // 后端不返回。**不设 HINT**，让具体中文 message 经
  // ErrorNotice 的 `HINTS[code] ?? error.message` 透出。
  THREAD_MISSING: 'THREAD_MISSING',
  // [2026-09-11 上传链路收口] 附件扩展名不在白名单（前端独有，后端不返回）。
  // 与后端 domains/attachments.py ALLOWED_EXTENSIONS（仅 .xlsx）对齐：accept
  // 只是选择器过滤，用户仍可在「所有文件」里强选——选择当下即拦，避免走一趟
  // 网络才被后端 400 拒。**不设 HINT**，让具体中文 message 经 ErrorNotice 的
  // `HINTS[code] ?? error.message` 透出，并与其它 VALIDATION_ERROR 来源区分开
  // （附件操作不误清无关错误）。
  UNSUPPORTED_ATTACHMENT_TYPE: 'UNSUPPORTED_ATTACHMENT_TYPE',
  // 映射/复核/任务列表错误码已随 D27/D28 页面简化全量删除（ErrorNotice 不再
  // 引用；后端 /api/v1/mappings、/reviews、/tasks 仍返回这些码，未知码由
  // ErrorNotice 回退展示后端 message）
  // D25/D26：优雅停机门禁（routes_chat.py 503，15.3 步骤 7）
  SERVICE_UNAVAILABLE: 'SERVICE_UNAVAILABLE',
  // RV-06 决策项 a / RV-38 非阻塞 3：网关 limit_req 标准 429（nginx 返回非 JSON 体）
  RATE_LIMITED: 'RATE_LIMITED',
  // Phase 3a：同一会话线程有活动图循环时 submit/confirm → 409（防重复提交，
  // routes_chat.py _enter_thread）
  THREAD_BUSY: 'THREAD_BUSY',
  // Phase 2：/chat/confirm 时线程无挂起确认卡 → 409（无 interrupt 可恢复）
  CONFIRM_NOT_PENDING: 'CONFIRM_NOT_PENDING',
} as const

export type ErrorCodeValue = (typeof ERROR_CODES)[keyof typeof ERROR_CODES] | string

// ---------------------------------------------------------------------------
// 轻确认卡片（Phase 3a 契约：domains/orders.py ConfirmationCard.to_dict()）
// ---------------------------------------------------------------------------

/** 7.3 六要素 + payload_hash；字段名以新契约为准——fields_to_write/
 *  auto_filled/expected_impact（字符串）/limitations。 */
export interface ConfirmationCard {
  operation_type: string
  target: string
  /** 将要写入的字段与值（有序 field → value 映射）。 */
  fields_to_write: Record<string, unknown>
  /** 自动补全字段及来源（要素 4）。 */
  auto_filled: Array<{
    field: string
    value: unknown
    source: string
    previous_value?: unknown
  }>
  /** 预计影响（要素 5；新契约为单条字符串）。 */
  expected_impact: string
  /** 无法由 ERP 预先确定的限制（要素 6）。 */
  limitations: string[]
  payload_hash: string
  /** [2026-09-12 多明细卡] 矩阵首列表头（行头列名）。缺省回退「明细」；
   *  考勤卡后端显式传「员工」。纯展示级，不进 payload_hash。 */
  row_header_label?: string
  /** [2026-09-12 多明细卡] 「操作=保持」折叠汇总的量词。缺省回退「条」；
   *  考勤卡后端显式传「人」。纯展示级，不进 payload_hash。 */
  row_quantifier?: string
  /** [2026-10-05 卡面原话对照] 最近一条用户原话全文（整卡级，非逐行）。
   *  纯展示级，后端条件输出（缺省不落 JSON 键，旧卡不带该键），不进 payload_hash。 */
  source_quote?: string
  /** [2026-10-05 卡面来源提示] 仅收支卡出现的提示（如「分类由模型按你的说法
   *  判定；若与你的说法不符，请在确认前修改」）。纯展示级，后端条件输出
   *  （缺省不落 JSON 键），不进 payload_hash。 */
  source_note?: string
}

/** 挂起确认状态（SSE confirmation_requested 事件 payload /
 *  GET /chat/history pending_confirmation；确认卡不含执行上下文 execution）。 */
export interface PendingConfirmation {
  confirm_type?: string
  task_id: string
  tool_name: string
  card: ConfirmationCard
  payload_hash: string
  /** hash 失效重挂：复用 confirmation_requested 事件并带 re_requested=true。 */
  re_requested?: boolean
}

// ---------------------------------------------------------------------------
// 对话与 SSE（2.7.3 chat 3 端点；2.7.4 L530-534）
// ---------------------------------------------------------------------------

export interface ChatMessageResponse {
  thread_id: string
  stream_id: string
  /** 纯对话轮次为 null；本轮写工具已建写任务（挂起确认）时有值。 */
  task_id: string | null
}

/** Phase 3a 事件类型（routes_chat.py / chat_streams.py；会话循环图）。
 *  旧事件 task_created/task_intent/model_stub/task_queued/task_waiting_input/
 *  task_reply 已废弃不发射（常量注释保留于后端）；task_failed/task_cancelled
 *  保留。 */
export const STREAM_EVENT_TYPES = [
  'agent_started',
  'agent_reply',
  'agent_error',
  'tool_running',
  'tool_result',
  'confirmation_requested',
  'task_failed',
  'task_cancelled',
  // [2026-08-31 对话对账批次 4] reconcile_analyze 完成时经 harness sink 发射，
  // payload = {report_id, summary_counts, row_count, subject_match_status}
  'reconcile_report',
  // [2026-09-01 导出功能批次] export_data 完成时经 harness sink 发射，
  // payload = {file_id, filename, entity, row_count, total, expires_at}
  'export_ready',
  'SESSION_EXPIRED',
] as const

export type StreamEventType = (typeof STREAM_EVENT_TYPES)[number] | string

/** agent_error 的 error 取值（模型不可用/工具级校验/停机/步数上限等）。 */
export type AgentErrorCode =
  | 'model_unavailable'
  | 'unknown_tool'
  | 'invalid_arguments'
  | 'not_implemented'
  | 'shutting_down'
  | 'preparation_error'
  | 'steps_exhausted'
  | 'history_truncated'
  | 'task_state_changed'
  | string

/** 事件 payload（宽松对象，防御性提取）。 */
export type StreamEventPayload = Record<string, unknown>

export interface StreamEventOut {
  event_id: string
  event_type: StreamEventType
  /** 纯对话事件为 null（Phase 3a 可空化；chat_streams.ChatStreamEvent）。 */
  task_id: string | null
  sequence: number
  payload: StreamEventPayload
}

// ---------------------------------------------------------------------------
// 对话历史与确认（Phase 3a：GET /chat/history、POST /chat/confirm）
// ---------------------------------------------------------------------------

/** 历史消息（checkpoint 重放；role ∈ human/ai/tool，content 为字符串）。 */
export interface ChatHistoryMessage {
  role: string
  content: string
}

export interface ChatHistoryOut {
  messages: ChatHistoryMessage[]
  pending_confirmation: PendingConfirmation | null
  /** 线程最近事件流；进程重启丢流 → null（只重放历史不订阅）。 */
  stream_id: string | null
}

export interface ChatConfirmRequest {
  thread_id: string
  approved: boolean
  payload_hash: string
}

export interface ChatConfirmResponse {
  /** 恢复执行的事件流 stream_id（SSE 重放新事件；复用或新建）。 */
  stream_id: string
}

// ---------------------------------------------------------------------------
// 对话对账（2026-08-31 批次 4：附件上传 / 对账报告详情）
// ---------------------------------------------------------------------------

/** 附件上传结果（POST /chat/attachments 成功响应）。 */
export interface ChatAttachmentOut {
  attachment_id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  expires_at: string | null
}

/** 报告详情字段差异（FieldDiff.to_dict() 形态）。 */
export interface ReconcileFieldDiff {
  field: string
  excel_value: string | null
  erp_value: string | null
  kind?: string
}

/** 报告详情单条差异条目（DiffEntry.to_dict() 形态）。 */
export interface ReconcileReportEntry {
  classification: string
  row_number: number
  source_file_hash?: string
  mapping_version?: number
  match_key?: Record<string, unknown> | null
  matched_order_id?: number | null
  diffs?: ReconcileFieldDiff[]
  write_entry?: string | null
  message?: string
}

/** 行级转换错误（RowError 形态）。 */
export interface ReconcileConversionError {
  row_number: number
  excel_column: string
  target_field: string
  reason: string
}

/** 对账报告详情（GET /chat/reports/{report_id}；ReconcileReportOut 形态）。
 *  subject_match/summary 为混合值 dict，组件侧防御性提取。 */
export interface ReconcileReportDetail {
  report_id: string
  thread_id: string
  entity: string
  subject_name: string
  subject_match: Record<string, unknown>
  period_note: string
  row_count: number
  summary: Record<string, unknown>
  conversion_errors: ReconcileConversionError[]
  limitations?: string[]
  data_completeness: string
  status: string
  created_at?: string | null
  expires_at?: string | null
  entries?: ReconcileReportEntry[]
}

// ---------------------------------------------------------------------------
// 业务审计（11.1；persistence/models.py AuditEvent）
// ---------------------------------------------------------------------------

export interface AuditOut {
  audit_id: string
  task_id: string | null
  operation_id: string | null
  trace_id: string
  actor_account: string
  actor_role: string
  event_type: string
  duty_code: string | null
  policy_key: string | null
  policy_version: string | null
  policy_hash: string | null
  target_type: string | null
  target_id: string | null
  request_summary: string | null
  structured_payload: Record<string, unknown> | null
  before_snapshot: Record<string, unknown> | null
  after_snapshot: Record<string, unknown> | null
  source: string
  result_code: string | null
  created_at: string
}

