import { QueryClient } from '@tanstack/react-query'

/**
 * 全局 QueryClient（规格 2.7.4）。
 * 服务端状态由 TanStack Query 管理；SESSION_EXPIRED 时 AuthProvider 调用
 * queryClient.clear() 清理缓存，避免继续展示敏感旧数据。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
})
