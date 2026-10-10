/**
 * 对话与 SSE API（规格 2.7.3 chat 端点；2.7.4 L530 SSE 事件契约；Phase 3a
 * 对话式 harness：thread_id 为会话线程，确认走 /chat/confirm）。
 *
 * POST /api/v1/chat/attachments         附件上传（multipart：file + thread_id）
 *                                       → {attachment_id, filename, content_type,
 *                                       size_bytes, sha256, expires_at}
 * POST /api/v1/chat/messages             提交消息 → {thread_id, stream_id, task_id}
 * GET  /api/v1/chat/history?thread_id=   对话历史重放 + 挂起确认卡 + stream_id
 * GET  /api/v1/chat/streams/:streamId    SSE 事件流（event_id/event_type/task_id/sequence/payload）
 * POST /api/v1/chat/streams/:streamId/cancel  取消当前生成（不等于取消已执行写操作）
 * POST /api/v1/chat/confirm              写操作确认恢复（{thread_id, approved, payload_hash}）
 * GET  /api/v1/chat/reports/:reportId    对账报告详情（?thread_id=；归属不匹配 404）
 * GET  /api/v1/chat/threads              会话索引列表（当前账号，updated_at DESC）
 * DELETE /api/v1/chat/threads/:threadId  删除会话（checkpoint/存档/索引；归属与活动保护）
 *
 * 响应经 api/schemas.ts Zod 运行时校验（规则 10 L448）。
 *
 * [2026-08-31 对话对账批次 4] 附件走两段式：先 POST /chat/attachments 拿
 * attachment_id，再 POST /chat/messages JSON {message, thread_id,
 * attachment_ids}（后端 messages 为 JSON 契约，无 file 字段）。
 */
import { API_BASE_URL, ApiError, apiFetch } from './client'
import {
  chatAttachmentResponseSchema,
  chatConfirmResponseSchema,
  chatHistorySchema,
  chatMessageResponseSchema,
  chatMessageSchema,
  parseWith,
  reconcileReportDetailSchema,
} from './schemas'
import type {
  ChatAttachmentOut,
  ChatConfirmRequest,
  ChatConfirmResponse,
  ChatHistoryOut,
  ChatMessageResponse,
  ReconcileReportDetail,
} from './types'

/**
 * 当前会话 thread_id 的 localStorage 持久化 key（ChatPage / ManagerLayout 共用，
 * 从 chat.api 统一导出避免循环依赖）。
 */
export const THREAD_STORAGE_KEY = 'hengfa.chat.thread'

/** 会话数据变化事件（ChatPage 发消息成功后发射，ManagerLayout 监听刷新历史列表）。 */
export const CHAT_THREADS_CHANGED_EVENT = 'chat:threads-changed'

/** 跨标签页/窗口会话变化广播频道（与 THREAD_STORAGE_KEY 同命名风格，2026-08-28）。 */
export const THREADS_CHANGED_CHANNEL = 'hengfa:threads-changed'

/**
 * 仅带附件、不打字时的兜底消息文案（2026-09-10 缺陷修复）。
 *
 * 后端 ChatMessageRequest.message 必填（Field(min_length=1)），而界面上
 * 「只挂 Excel 附件、不打字直接发送」是被允许的（发送键 disabled 仅当
 * 无内容且无附件）。此前该场景直接把空串交给 chatMessageSchema 校验 →
 * ZodError（早于附件上传）→ 归为 NETWORK_ERROR 且上传请求根本没发。
 * 正确修法是补文案而非放宽校验：用本常量作为 message 发往后端。
 *
 * ChatPage 乐观气泡与 sendChatMessage 共用本常量，保证用户看到的气泡
 * 内容 == 模型收到/后端记录的内容，且两处文案不会漂移。
 */
export const ATTACHMENT_ONLY_MESSAGE = '（上传 Excel 文件）'

/**
 * 附件大小上限（10 MiB）——与后端 attachment_max_bytes（config.py 默认
 * 10 * 1024 * 1024，.env 的 ATTACHMENT_MAX_BYTES）对齐；**改动需同步两处**。
 * 用于前端在选择文件的当下做体积预校验，避免大文件走一趟网络才被 413 拒。
 */
export const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024

/**
 * 上限的整数 MiB 展示值（与 ATTACHMENT_MAX_BYTES 同源，供错误文案引用，
 * 避免在多处写死「10 MB」造成漂移）。
 */
export const ATTACHMENT_MAX_MB = Math.round(ATTACHMENT_MAX_BYTES / (1024 * 1024))

/** 会话数据变化统一通知：同页 window 事件 + 跨标签页 BroadcastChannel
 *  （BroadcastChannel 不向发送方自身投递，两者互补，2026-08-28）。 */
let threadsChannel: BroadcastChannel | null = null
export function notifyThreadsChanged(): void {
  window.dispatchEvent(new CustomEvent(CHAT_THREADS_CHANGED_EVENT))
  threadsChannel ??= new BroadcastChannel(THREADS_CHANGED_CHANNEL)
  threadsChannel.postMessage('changed')
}

/** 会话索引行（GET /chat/threads 列表项；后端 ThreadOut 形态）。 */
export interface ThreadInfo {
  thread_id: string
  title: string
  created_at: string
  updated_at: string
}

/** 会话索引列表（GET /chat/threads）。 */
export interface ThreadListOut {
  threads: ThreadInfo[]
}

/** 会话列表（2026-08-24 对话会话管理；当前账号，updated_at DESC）。
 *  线程列表是轻量展示数据，运行时校验就地做（字段类型守卫），不新增
 *  schemas.ts 条目。 */
export function listThreadsApi(): Promise<ThreadListOut> {
  return apiFetch<unknown>('/chat/threads').then((data) => {
    const items = (data as { threads?: unknown } | null)?.threads
    if (!Array.isArray(items)) {
      // [2026-09-11 上传链路收口] 这是响应契约违例，不是网络故障。此前抛
      // TypeError，而 toApiError 把 TypeError 等同于网络失败（fetch 网络不可达
      // 也抛 TypeError）——是个定时炸弹（调用方一旦接 toApiError 即误报网络故障）。
      // 改抛 ApiError VALIDATION_ERROR，与同文件 parseWith 的既有做法一致。
      throw new ApiError(0, {
        error_code: 'VALIDATION_ERROR',
        message: 'GET /chat/threads: threads 字段缺失或非数组',
      })
    }
    const threads: ThreadInfo[] = []
    for (const item of items) {
      const record = item as Record<string, unknown>
      if (
        typeof record.thread_id === 'string' &&
        typeof record.title === 'string' &&
        typeof record.created_at === 'string' &&
        typeof record.updated_at === 'string'
      ) {
        threads.push({
          thread_id: record.thread_id,
          title: record.title,
          created_at: record.created_at,
          updated_at: record.updated_at,
        })
      }
    }
    return { threads }
  })
}

/** 删除会话（破坏性操作：后端做归属校验与活动任务保护；成功 204 无响应体）。 */
export function deleteThreadApi(threadId: string): Promise<void> {
  return apiFetch<void>(`/chat/threads/${encodeURIComponent(threadId)}`, {
    method: 'DELETE',
  })
}

/**
 * 提交消息（Phase 3a；[2026-08-31 批次 4] 带附件改两段式）。
 * @param threadId 会话线程；无附件时缺省由服务端生成 thread-{account}-{uuid8}
 *                 并返回，调用方（ChatPage）据此持久化。
 *
 * 带附件（file 非空）→ 两段式：
 *   1. POST /chat/attachments（FormData：file + thread_id）→ attachment_id；
 *   2. POST /chat/messages JSON {message, thread_id, attachment_ids:[id]}。
 * 附件上传失败（如 503 附件功能未启用/400 校验拒绝）→ 抛 ApiError，不发消息，
 * 由调用方提示用户重试。
 *
 * 首次提交且无 thread_id 时：后端 /chat/attachments 的 thread_id 必填
 * （Form(min_length=1)），而 /chat/messages 缺省会由服务端另生成新线程——
 * 前端先占一个客户端 thread_id（thread-client-<uuid8>）传两处，保证附件
 * 归属与消息线程一致；ChatMessageResponse 原样返回该 id，调用方照常持久化。
 */
export function sendChatMessage(
  content: string,
  file?: File | null,
  threadId?: string | null,
): Promise<ChatMessageResponse> {
  // 空内容仅在「带附件」时兜底为 ATTACHMENT_ONLY_MESSAGE（后端 message
  // 必填，见常量注释）；无附件时空内容仍走 chatMessageSchema 的
  // 「请输入内容」校验（不因本修复意外放宽）。10.6 长度限制照常生效。
  const message = chatMessageSchema.parse(
    content.trim() || (file ? ATTACHMENT_ONLY_MESSAGE : content),
  )
  if (file) {
    const thread = threadId ?? `thread-client-${Math.random().toString(16).slice(2, 10)}`
    return uploadChatAttachment(file, thread).then((attachment) =>
      apiFetch<unknown>('/chat/messages', {
        method: 'POST',
        jsonBody: { message, thread_id: thread, attachment_ids: [attachment.attachment_id] },
      }).then((data) => parseWith(chatMessageResponseSchema, data, 'POST /chat/messages')),
    )
  }
  return apiFetch<unknown>('/chat/messages', {
    method: 'POST',
    jsonBody: { message, ...(threadId ? { thread_id: threadId } : {}) },
  }).then((data) => parseWith(chatMessageResponseSchema, data, 'POST /chat/messages'))
}

/** 上传对账附件（2026-08-31 批次 4；写 API：manager + CSRF，multipart 表单）。 */
export function uploadChatAttachment(file: File, threadId: string): Promise<ChatAttachmentOut> {
  const form = new FormData()
  form.append('file', file)
  form.append('thread_id', threadId)
  return apiFetch<unknown>('/chat/attachments', { method: 'POST', body: form }).then((data) =>
    parseWith(chatAttachmentResponseSchema, data, 'POST /chat/attachments'),
  )
}

/** 对账报告详情（2026-08-31 批次 4；thread_id 归属校验，不匹配 404）。 */
export function fetchReconcileReportApi(
  reportId: string,
  threadId: string,
): Promise<ReconcileReportDetail> {
  const query = new URLSearchParams({ thread_id: threadId })
  return apiFetch<unknown>(
    `/chat/reports/${encodeURIComponent(reportId)}?${query.toString()}`,
  ).then((data) => parseWith(reconcileReportDetailSchema, data, 'GET /chat/reports/{report_id}'))
}

/** 对话历史重放（Phase 3a；页面刷新/重连恢复路径）。 */
export function fetchChatHistoryApi(threadId: string): Promise<ChatHistoryOut> {
  const query = new URLSearchParams({ thread_id: threadId })
  return apiFetch<unknown>(`/chat/history?${query.toString()}`).then((data) =>
    parseWith(chatHistorySchema, data, 'GET /chat/history'),
  )
}

/** 写操作确认恢复（Phase 3a；manager + CSRF；返回续传事件流 stream_id）。 */
export function confirmChatApi(body: ChatConfirmRequest): Promise<ChatConfirmResponse> {
  return apiFetch<unknown>('/chat/confirm', {
    method: 'POST',
    jsonBody: body,
  }).then((data) => parseWith(chatConfirmResponseSchema, data, 'POST /chat/confirm'))
}

/** 取消当前生成（不取消已执行写操作；后端独立处理）。 */
export function cancelStreamApi(streamId: string): Promise<void> {
  return apiFetch<void>(`/chat/streams/${encodeURIComponent(streamId)}/cancel`, {
    method: 'POST',
  })
}

/**
 * 导出文件下载 URL（2026-09-01 导出功能批次）。
 *
 * GET /api/v1/chat/export/{file_id}：manager 门禁 + Cookie 会话，直接
 * <a href> 打开即下载（同源 /api/v1 路径，浏览器自动携带 Cookie；
 * 跨源部署时由 VITE_API_BASE_URL 指向同源反代）。不做 fetch 下载——
 * 大文件流式落盘浏览器下载框，经 fetch 反而要手工接管 blob。
 */
export function exportDownloadUrl(fileId: string): string {
  return `${API_BASE_URL}/chat/export/${encodeURIComponent(fileId)}`
}
