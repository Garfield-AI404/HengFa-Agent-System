/**
 * 角色路由守卫（规格 2.7.2）。
 *
 * - anonymous → 重定向 /login（携带来源路径，登录后返回）。
 * - 角色不在允许列表 → 重定向 /forbidden（403）。
 * - 前端守卫只改善用户体验；后端每个 API 仍独立鉴权（2.7.2）。
 */
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'
import { useAuth } from './AuthProvider'
import type { Role } from './auth.types'

interface RequireRoleProps {
  roles: Role[]
  children: ReactNode
}

export function RequireRole({ roles, children }: RequireRoleProps) {
  const { user, status } = useAuth()
  const location = useLocation()

  if (status === 'loading') {
    return (
      <div className="guard-loading" role="status" aria-live="polite">
        正在验证登录状态…
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  if (!roles.includes(user.role)) {
    return <Navigate to="/forbidden" replace />
  }

  return <>{children}</>
}
