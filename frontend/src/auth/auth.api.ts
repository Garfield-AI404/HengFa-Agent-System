/**
 * 认证 API（规格 2.7.3 契约）。
 *
 * POST /api/v1/auth/login   登录并建立会话（公共）
 * POST /api/v1/auth/logout  注销（已登录）
 * GET  /api/v1/auth/me      当前账号和角色（已登录）
 */
import { apiFetch } from '../api/client'
import type { CurrentUser, LoginRequest } from './auth.types'

export function loginApi(account: string, password: string): Promise<CurrentUser> {
  const body: LoginRequest = { account, password }
  return apiFetch<CurrentUser>('/auth/login', { method: 'POST', jsonBody: body })
}

export function logoutApi(): Promise<void> {
  return apiFetch<void>('/auth/logout', { method: 'POST' })
}

export function meApi(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>('/auth/me')
}
