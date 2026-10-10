/**
 * 运行时校验 Schema（规格规则 10 L448：前端 api/schemas.ts 必须使用 Zod 校验
 * 运行时响应；TypeScript 类型不能代替运行时校验）。
 *
 * - 外部字符串长度限制（10.6 L1193：外部字符串在日志和网页输出中做长度限制，
 *   与后端 Field 上限对齐）；
 * - 响应契约校验：SSE 事件五要素（2.7.4 L530）与各 API 响应包络——
 *   校验失败按 fail closed 处理（不渲染未经验证的数据，抛 ApiError）。
 */
import { z } from 'zod'
import { ApiError } from './client'

// ---------------------------------------------------------------------------
// 外部字符串长度限制（10.6 L1193；与后端 web/schemas.py Field 上限对齐）
// ---------------------------------------------------------------------------

export const EXTERNAL_TEXT_MAX = 2000 // 外部自由文本（Excel 单元格/notes/备注）显示上限
export const CHAT_MESSAGE_MAX = 10000 // 后端 ChatMessageRequest.message
export const LOGIN_ACCOUNT_MAX = 128 // 后端 LoginRequest.account
export const LOGIN_PASSWORD_MAX = 256 // 后端 LoginRequest.password

export const externalTextSchema = z.string().max(EXTERNAL_TEXT_MAX)
export const chatMessageSchema = z
  .string()
  .trim()
  .min(1, '请输入内容')
  .max(CHAT_MESSAGE_MAX, `内容不能超过 ${CHAT_MESSAGE_MAX} 字`)
export const loginSchema = z.object({
  account: z.string().trim().min(1, '请输入账号').max(LOGIN_ACCOUNT_MAX),
  password: z.string().min(1, '请输入密码').max(LOGIN_PASSWORD_MAX),
})

// ---------------------------------------------------------------------------
// 响应契约（规则 10：运行时校验）
// ---------------------------------------------------------------------------

const jsonRecord = z.record(z.string(), z.unknown())

/** SSE 事件五要素（2.7.4 L530；Phase 3a：task_id 可空——纯对话事件为 null）。 */
export const streamEventSchema = z.object({
  event_id: z.string(),
  event_type: z.string(),
  task_id: z.string().nullable(),
  sequence: z.number(),
  payload: jsonRecord,
})

export const authUserSchema = z.object({
  account: z.string(),
  role: z.string(),
  shared_manager_note: z.string().nullable().optional(),
})

export const chatMessageResponseSchema = z.object({
  thread_id: z.string(),
  stream_id: z.string(),
  task_id: z.string().nullable(),
})

/** 确认卡六要素 + payload_hash（Phase 3a：ConfirmationCard.to_dict() 形状）。 */
export const confirmationCardSchema = z.object({
  operation_type: z.string(),
  target: z.string(),
  fields_to_write: jsonRecord,
  auto_filled: z.array(
    z.object({
      field: z.string(),
      value: z.unknown(),
      source: z.string(),
      previous_value: z.unknown().optional(),
    }),
  ),
  expected_impact: z.string(),
  limitations: z.array(z.string()),
  payload_hash: z.string(),
  // [2026-09-12 多明细卡] 行头列名 / 折叠量词（纯展示级，后端条件输出：单条旧卡
  // 不带这两个键）。**必须在此声明**——zod 的 z.object() 默认剥离未声明的键，
  // 漏声明的可选字段会被静默丢弃、永远走组件回退值（此坑仓库已踩过一次）。
  row_header_label: z.string().optional(),
  row_quantifier: z.string().optional(),
  // [2026-10-05 卡面原话/来源提示] 纯展示级、后端条件输出（缺省时不落 JSON 键，
  // 旧卡不带该键）；不进 payload_hash。**必须在此声明**——zod 的 z.object()
  // 默认剥离未声明的键，漏声明会被静默丢弃（与 row_header_label 是同一个坑）。
  source_quote: z.string().optional(),
  source_note: z.string().optional(),
})

/** 挂起确认（SSE confirmation_requested / history pending_confirmation）。 */
export const pendingConfirmationSchema = z.object({
  confirm_type: z.string().optional(),
  task_id: z.string(),
  tool_name: z.string(),
  card: confirmationCardSchema,
  payload_hash: z.string(),
  re_requested: z.boolean().optional(),
})

/** 对话历史（GET /chat/history）。 */
export const chatHistorySchema = z.object({
  messages: z.array(z.object({ role: z.string(), content: z.string() })),
  pending_confirmation: pendingConfirmationSchema.nullable(),
  stream_id: z.string().nullable(),
})

/** 确认受理（POST /chat/confirm）。 */
export const chatConfirmResponseSchema = z.object({
  stream_id: z.string(),
})

/** 附件上传（POST /chat/attachments；routes_attachments.py 成功响应形态）。 */
export const chatAttachmentResponseSchema = z.object({
  attachment_id: z.string(),
  filename: z.string(),
  content_type: z.string(),
  size_bytes: z.number(),
  sha256: z.string(),
  expires_at: z.string().nullable(),
})

/** 对账报告单条字段差异（FieldDiff.to_dict() 形态）。 */
export const reconcileFieldDiffSchema = z.object({
  field: z.string(),
  excel_value: z.string().nullable(),
  erp_value: z.string().nullable(),
  kind: z.string().optional(),
})

/** 对账报告单条差异条目（DiffEntry.to_dict() 形态）。 */
export const reconcileEntrySchema = z.object({
  classification: z.string(),
  row_number: z.number(),
  source_file_hash: z.string().optional(),
  mapping_version: z.number().optional(),
  match_key: jsonRecord.nullable().optional(),
  matched_order_id: z.number().nullable().optional(),
  diffs: z.array(reconcileFieldDiffSchema).optional(),
  write_entry: z.string().nullable().optional(),
  message: z.string().optional(),
})

/** 行级转换错误（RowError 形态：row_number/excel_column/target_field/reason）。 */
export const reconcileConversionErrorSchema = z.object({
  row_number: z.number(),
  excel_column: z.string(),
  target_field: z.string(),
  reason: z.string(),
})

/** 对账报告详情（GET /chat/reports/{report_id}；ReconcileReportOut 形态）。
 *  subject_match/summary 为嵌套 dict（值类型混合），按 jsonRecord 宽松校验，
 *  取值由组件防御性提取（与 SSE 事件 payload 同口径）。 */
export const reconcileReportDetailSchema = z.object({
  report_id: z.string(),
  thread_id: z.string(),
  entity: z.string(),
  subject_name: z.string(),
  subject_match: jsonRecord,
  period_note: z.string(),
  row_count: z.number(),
  summary: jsonRecord,
  conversion_errors: z.array(reconcileConversionErrorSchema),
  limitations: z.array(z.string()).optional(),
  data_completeness: z.string(),
  status: z.string(),
  created_at: z.string().nullable().optional(),
  expires_at: z.string().nullable().optional(),
  entries: z.array(reconcileEntrySchema).optional(),
})

export const auditOutSchema = z.object({
  audit_id: z.string(),
  task_id: z.string().nullable(),
  operation_id: z.string().nullable(),
  trace_id: z.string(),
  actor_account: z.string(),
  actor_role: z.string(),
  event_type: z.string(),
  duty_code: z.string().nullable(),
  policy_key: z.string().nullable(),
  policy_version: z.string().nullable(),
  policy_hash: z.string().nullable(),
  target_type: z.string().nullable(),
  target_id: z.string().nullable(),
  request_summary: z.string().nullable(),
  structured_payload: jsonRecord.nullable(),
  before_snapshot: jsonRecord.nullable(),
  after_snapshot: jsonRecord.nullable(),
  source: z.string(),
  result_code: z.string().nullable(),
  created_at: z.string(),
})

export const auditListResponseSchema = z.object({
  items: z.array(auditOutSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
})

// ---------------------------------------------------------------------------
// 校验辅助：失败即 fail closed（不渲染未经验证的数据）
// ---------------------------------------------------------------------------

export function parseWith<S extends z.ZodType>(schema: S, data: unknown, path: string): z.infer<S> {
  const result = schema.safeParse(data)
  if (!result.success) {
    throw new ApiError(0, {
      error_code: 'VALIDATION_ERROR',
      message: `服务器响应不符合契约（${path}，api/schemas.ts 校验失败）。`,
    })
  }
  return result.data
}
