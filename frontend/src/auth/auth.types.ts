/**
 * 认证相关类型（手写补充，规格 2.7.3 契约）。
 *
 * 契约纪律：src/api/types.ts 只能来自后端 OpenAPI 或经 Review 的手写补充。
 * D4 阶段后端 OpenAPI 未联调，此处为按规格 2.7.3 契约的手写形状；
 * D22 后端 OpenAPI 就绪后，以 scripts/generate-api-types.mjs 生成结果为准，
 * 差异纳入同一 Review 对齐。
 */

export type Role = 'manager' | 'developer'

export interface LoginRequest {
  account: string
  password: string
}

/**
 * GET /api/v1/auth/me 与登录响应的当前账号形状。
 * D22 D4 认证契约对齐：后端 web/schemas.py AuthUserResponse 为
 * {account, role, shared_manager_note}（D4 手写的 username 字段已修正）。
 */
export interface CurrentUser {
  account: string
  role: Role
  /** 12.5：共享 manager 账号提示（仅 manager 响应携带）。 */
  shared_manager_note?: string | null
}

/** 后端错误包络（规格 2.7.3：所有写 API 返回稳定 error_code、message、details、trace_id）。 */
export interface ApiErrorBody {
  error_code?: string
  message?: string
  details?: unknown
  trace_id?: string
}

/** 客户端错误码（后端不可达等场景；与后端错误码并列，前端统一只按 errorCode 分支）。 */
export const CLIENT_ERROR_CODES = {
  NETWORK_ERROR: 'NETWORK_ERROR',
} as const
