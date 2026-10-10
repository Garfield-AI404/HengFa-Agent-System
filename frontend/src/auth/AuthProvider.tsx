/**
 * 登录态管理（规格 2.7.4 状态规则）。
 *
 * - 挂载时经 /auth/me 恢复登录态；页面刷新从服务端会话恢复，不依赖浏览器内存。
 * - 收到 401（SESSION_EXPIRED）时清理 Query cache 并置为匿名；
 *   守卫组件随即把页面重定向到 /login（2.7.4"清理 Query cache 并跳转登录"）。
 * - 不引入 Redux；登录态是跨页面客户端状态，此处以 Context 承载。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { setUnauthorizedHandler } from '../api/client'
import { queryClient } from '../app/queryClient'
import { loginApi, logoutApi, meApi } from './auth.api'
import type { CurrentUser } from './auth.types'

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous'

export interface AuthContextValue {
  user: CurrentUser | null
  status: AuthStatus
  login: (account: string, password: string) => Promise<CurrentUser>
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [status, setStatus] = useState<AuthStatus>('loading')

  // 页面刷新后从服务端会话恢复登录态（2.7.4：不能只依赖浏览器内存）
  useEffect(() => {
    let cancelled = false
    meApi()
      .then((currentUser) => {
        if (cancelled) return
        setUser(currentUser)
        setStatus('authenticated')
      })
      .catch(() => {
        // 401 / 网络失败均视为匿名；登录页按需引导登录
        if (cancelled) return
        setUser(null)
        setStatus('anonymous')
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 全局 401（SESSION_EXPIRED）：清理 Query cache 并回到匿名态（2.7.4）
  useEffect(() => {
    const handleUnauthorized = (): void => {
      queryClient.clear()
      setUser(null)
      setStatus('anonymous')
    }
    setUnauthorizedHandler(handleUnauthorized)
    return () => {
      setUnauthorizedHandler(null)
    }
  }, [])

  const login = useCallback(async (account: string, password: string) => {
    const currentUser = await loginApi(account, password)
    setUser(currentUser)
    setStatus('authenticated')
    return currentUser
  }, [])

  const logout = useCallback(async () => {
    try {
      await logoutApi()
    } catch {
      // 注销接口失败也本地退出（best effort）；服务端会话由其后端逻辑兜底
    }
    queryClient.clear()
    setUser(null)
    setStatus('anonymous')
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, status, login, logout }),
    [user, status, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth 必须在 AuthProvider 内使用')
  }
  return context
}
