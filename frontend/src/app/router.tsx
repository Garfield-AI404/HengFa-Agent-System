/**
 * 路由注册与角色守卫（规格 2.7.2 页面和角色表）。
 *
 * - /login、/forbidden 公开；
 * - D28：布局不再整体包 RequireRole（避免 developer 被拦在布局外），
 *   守卫下沉到页面级——manager 与 developer 共用 ManagerLayout 布局壳：
 *   - /chat      RequireRole roles={['manager']}（developer 访问 → /forbidden）
 *   - /developer RequireRole roles={['developer']}（manager 访问 → /forbidden）
 * - 根路径按角色跳转：manager → /chat，developer → /developer。
 * - 守卫仅为 UX，后端每个 API 独立鉴权（2.7.2）。
 */
import { createBrowserRouter, Navigate } from 'react-router'
import { RequireRole } from '../auth/RequireRole'
import { useAuth } from '../auth/AuthProvider'
import { ForbiddenPage } from '../pages/ForbiddenPage'
import { LoginPage } from '../pages/LoginPage'
import { ChatPage } from '../pages/ChatPage'
import { DeveloperPage } from '../pages/DeveloperPage'
import { AuthLayout } from './layouts/AuthLayout'
import { ManagerLayout } from './layouts/ManagerLayout'

/** 根路径按角色跳转（登录态判断；未登录回落 /chat 由 RequireRole 转登录页）。 */
function HomeRedirect() {
  const { user, status } = useAuth()
  if (status === 'loading') {
    return (
      <div className="guard-loading" role="status" aria-live="polite">
        正在验证登录状态…
      </div>
    )
  }
  return <Navigate to={user?.role === 'developer' ? '/developer' : '/chat'} replace />
}

export const appRouter = createBrowserRouter([
  {
    path: '/login',
    element: <AuthLayout />,
    children: [{ index: true, element: <LoginPage /> }],
  },
  {
    path: '/forbidden',
    element: <ForbiddenPage />,
  },
  {
    element: <ManagerLayout />,
    children: [
      {
        path: '/chat',
        element: (
          <RequireRole roles={['manager']}>
            <ChatPage />
          </RequireRole>
        ),
      },
      {
        path: '/developer',
        element: (
          <RequireRole roles={['developer']}>
            <DeveloperPage />
          </RequireRole>
        ),
      },
    ],
  },
  {
    path: '/',
    element: <HomeRedirect />,
  },
  {
    path: '*',
    element: <Navigate to="/" replace />,
  },
])
