/**
 * 应用根（规格 2.6：全局 Provider 与错误边界）。
 *
 * - QueryClientProvider：TanStack Query 管理服务端状态（2.7.4）。
 * - AuthProvider：登录态与会话失效处理（2.7.4）。
 * - RouterProvider：路由 + 角色守卫（2.7.2）。
 */
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router'
import { AuthProvider } from '../auth/AuthProvider'
import { queryClient } from './queryClient'
import { appRouter } from './router'

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={appRouter} />
      </AuthProvider>
    </QueryClientProvider>
  )
}
