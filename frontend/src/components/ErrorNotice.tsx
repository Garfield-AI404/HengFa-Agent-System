/**
 * 错误提示（2.7.3：展示 message，分支逻辑只读 error_code）。
 * 已知错误码给确定性提示；未知错误码回退展示后端 message + trace_id。
 */
import type { ReactNode } from 'react'
import { ApiError } from '../api/client'
import { ERROR_CODES } from '../api/types'
import { ATTACHMENT_MAX_MB } from '../api/chat.api'

// VALIDATION_ERROR 有意不在此表登记：让它走下方 `HINTS[code] ?? error.message`
// 回退路径，透出 zod/后端给的具体 message（如「内容不能超过 10000 字」），而非
// 被笼统文案覆盖掉信息量。UNKNOWN_ERROR 则相反——它是兜底码，常来自网关非 JSON
// 错误响应（无 message 可透出，否则会渲染英文原始异常），故在上表登记固定中文提示。
const HINTS: Record<string, string> = {
  [ERROR_CODES.NETWORK_ERROR]: '无法连接服务器，请检查网络后重试。',
  [ERROR_CODES.SESSION_EXPIRED]: '会话已过期，请重新登录。',
  [ERROR_CODES.FORBIDDEN]: '当前账号无权限执行该操作。',
  [ERROR_CODES.CSRF_VALIDATION_FAILED]: '跨站请求校验失败，请刷新页面后重试。',
  // D26（RV-39 信息级）：优雅停机门禁 503，文案与后端 routes_chat.py 一致
  [ERROR_CODES.SERVICE_UNAVAILABLE]: '系统正在优雅停机，暂不接受新写任务，请稍后重试。',
  // Phase 3a：同一会话线程有活动图循环（submit/confirm 并发）→ 409 防重复提交
  [ERROR_CODES.THREAD_BUSY]: '上一条消息处理中，请稍候再试。',
  // Phase 2：/chat/confirm 时线程无挂起确认卡（无 interrupt 可恢复）
  [ERROR_CODES.CONFIRM_NOT_PENDING]: '该任务当前没有待确认内容，请刷新后重试。',
  // [2026-09-11 上传链路收口] 413 通用语义（网关 client_max_body_size 或后端 413）；
  // 上限从 ATTACHMENT_MAX_MB 取，与后端 attachment_max_bytes / 前端预校验同源。
  // 前端体积预校验亦复用此码，故此处文案同时服务两者（ChatPage 不再自带文案）。
  [ERROR_CODES.PAYLOAD_TOO_LARGE]: `文件过大，附件不能超过 ${ATTACHMENT_MAX_MB} MB，请压缩或拆分后重试。`,
  // [2026-09-11] 兜底码如实但不误导：不再谎称网络问题，也不把英文原始异常
  // （如 `SyntaxError: Unexpected end of JSON input`）直接渲染到中文界面；
  // trace_id 仍照常展示供排查。
  [ERROR_CODES.UNKNOWN_ERROR]: '系统处理异常，请稍后重试。',
}

interface ErrorNoticeProps {
  error: ApiError | null
  /** 额外展示 details 中的 errors 清单（后端部分错误码携带该明细）。 */
  showDetails?: boolean
}

export function ErrorNotice({ error, showDetails = false }: ErrorNoticeProps) {
  if (!error) return null
  const hint = HINTS[error.errorCode] ?? error.message
  const detailErrors =
    showDetails &&
    error.details &&
    typeof error.details === 'object' &&
    'errors' in error.details &&
    Array.isArray((error.details as { errors?: unknown[] }).errors)
      ? ((error.details as { errors: string[] }).errors)
      : null

  return (
    <div className="error-notice" role="alert">
      <p className="error-notice__message">{hint}</p>
      {detailErrors && detailErrors.length > 0 ? (
        <ul className="error-notice__details">
          {detailErrors.map((item, index) => (
            <li key={index}>{String(item)}</li>
          ))}
        </ul>
      ) : null}
      {error.traceId ? (
        <p className="error-notice__trace">trace_id: {error.traceId}</p>
      ) : null}
    </div>
  )
}

/** 空状态（无数据时给出确定性文案，不把"未查到"说成"从未存在"，RV-25 口径）。 */
export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>
}
