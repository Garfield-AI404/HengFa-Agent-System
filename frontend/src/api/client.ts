/**
 * 统一 API 客户端（规格 2.7.1 / 2.7.3 / 2.7.4）。
 *
 * - 会话只走 HttpOnly、Secure、SameSite Cookie（credentials: 'include'），
 *   认证令牌绝不写入 localStorage（2.7.1）。
 * - CSRF 由后端 Origin/Referer 白名单承担（RV-06 变更项 3），前端不发送任何 CSRF 令牌。
 * - 非 2xx 归一化为 ApiError：分支逻辑只读取 errorCode（error_code），
 *   message 仅用于展示（2.7.3）。
 * - 收到 401 时触发全局未授权回调（AuthProvider 注册），
 *   用于 SESSION_EXPIRED 时清理 Query cache 并跳转登录（2.7.4）。
 */
import { z } from 'zod'
import type { ApiErrorBody } from '../auth/auth.types'
// [2026-09-11 上传链路收口] 错误码统一取自 ERROR_CODES（ErrorNotice 的 HINTS 键
// 也是它）——避免一侧字面量改名、另一侧静默失配（HINT 查不到键退化为 message，
// 不报错不红测试）。types.ts 无任何 import，不会成环。
import { ERROR_CODES } from './types'

// 未配置时回退同源相对路径 /api/v1：dev 由 vite proxy 转发到 127.0.0.1:18000，
// 生产由 nginx 同源反代——与 vite.config.ts / .env 现状一致。
// 注意：不要写死 127.0.0.1:8000（后端实际端口是 18000，且跨 host 会丢 Cookie）。
// 导出为 const 导出（2026-09-01 导出功能批次：chat.api.ts exportDownloadUrl
// 构建 <a href> 下载链接复用；避免复制粘贴导致两处回退逻辑漂移）。
export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1'

export class ApiError extends Error {
  readonly errorCode: string
  readonly details: unknown
  readonly traceId: string | undefined
  readonly status: number

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? '请求失败')
    this.name = 'ApiError'
    this.status = status
    this.errorCode =
      body.error_code ??
      (status === 401
        ? ERROR_CODES.SESSION_EXPIRED
        : status === 429
          ? ERROR_CODES.RATE_LIMITED // RV-06 a：网关 limit_req 标准 429（nginx 返回非 JSON 体）
          : status === 413
            ? ERROR_CODES.PAYLOAD_TOO_LARGE // [2026-09-11] 网关 client_max_body_size 413（HTML 体，json 解析失败）
            : ERROR_CODES.UNKNOWN_ERROR)
    this.details = body.details ?? null
    this.traceId = body.trace_id
  }
}

let unauthorizedHandler: (() => void) | null = null

/** 注册全局未授权（401/SESSION_EXPIRED）回调，AuthProvider 在挂载时注册。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

/** 触发全局未授权回调（供非 apiFetch 的 401 场景复用，如 SSE 流 401）。 */
export function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}

/**
 * 把任意 catch 到的异常归一为 ApiError（2026-09-11 上传链路收口）。
 *
 * 背景：此前 5 处调用点各自写 `err instanceof ApiError ? err :
 * new ApiError(0, { error_code: 'NETWORK_ERROR' })`——zod 校验失败、代码缺陷
 * 等一切非网络异常都被说成「无法连接服务器」，掩盖真实原因（2026-09-10 生产
 * 事故：附件上传前 `chatMessageSchema.parse('')` 抛 ZodError 被谎报为网络故障，
 * 请求根本没发出）。按异常类型分流，让真实原因透出到界面：
 * - ApiError → 原样返回（后端契约错误，error_code/message 语义已准确）；
 * - ZodError → VALIDATION_ERROR + 首条 issue 的 message（如「内容不能超过
 *   10000 字」），用户能直接看到被拒原因；
 * - TypeError → NETWORK_ERROR（fetch 网络不可达抛的就是 TypeError，必须保留此
 *   归类：真网络故障仍提示「无法连接服务器」——LoginPage.test 即此保护）；
 * - 其他 → UNKNOWN_ERROR 兜底，不再谎称网络问题。
 */
export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  if (err instanceof z.ZodError) {
    return new ApiError(0, {
      error_code: ERROR_CODES.VALIDATION_ERROR,
      message: err.issues[0]?.message ?? '输入内容不符合要求，请检查后重试。',
    })
  }
  if (err instanceof TypeError) {
    return new ApiError(0, { error_code: ERROR_CODES.NETWORK_ERROR })
  }
  return new ApiError(0, { error_code: ERROR_CODES.UNKNOWN_ERROR })
}

export interface ApiFetchOptions extends RequestInit {
  /** 是否发送 JSON 请求体；true 时自动设置 content-type 并序列化 body。 */
  jsonBody?: unknown
}

export async function apiFetch<T>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const { jsonBody, method = 'GET', headers, ...rest } = options

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers: {
      accept: 'application/json',
      ...(jsonBody !== undefined ? { 'content-type': 'application/json' } : {}),
      ...headers,
    },
    body: jsonBody !== undefined ? JSON.stringify(jsonBody) : undefined,
    credentials: 'include',
    ...rest,
  })

  if (response.status === 401) {
    unauthorizedHandler?.()
  }

  if (!response.ok) {
    let errorBody: ApiErrorBody = {}
    try {
      errorBody = (await response.json()) as ApiErrorBody
    } catch {
      // 非 JSON 错误体：保留空包络，由 ApiError 生成默认 errorCode
    }
    throw new ApiError(response.status, errorBody)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
