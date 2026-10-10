/**
 * 认证全链路集成测试：真实 AuthProvider + 真实布局 + 守卫 + 全局 QueryClient，
 * 后端以全局 fetch mock 模拟（D4 后端联调前；e2e 层另有 Playwright route mock）。
 *
 * 覆盖（2.7.4）：
 * - 未登录 → 受保护路由重定向登录；
 * - 已登录（会话有效）→ 直接进入工作台；
 * - 会话过期（401/SESSION_EXPIRED）→ 清理 Query cache 并跳转登录；
 * - 注销 → 回到登录态且缓存被清理。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { QueryClientProvider } from '@tanstack/react-query'
import { apiFetch } from '../../src/api/client'
import { AuthProvider } from '../../src/auth/AuthProvider'
import { RequireRole } from '../../src/auth/RequireRole'
import type { CurrentUser } from '../../src/auth/auth.types'
import { queryClient } from '../../src/app/queryClient'
import { ManagerLayout } from '../../src/app/layouts/ManagerLayout'

const ERROR = (errorCode: string, message: string) => ({
  error_code: errorCode,
  message,
  details: null,
  trace_id: `it-${errorCode}`,
})

/** 会话用户形状跟随 D22 对齐后的后端 AuthUserResponse：{account, role}。 */
interface FakeSession {
  user: CurrentUser | null
}

function installFakeBackend(session: FakeSession) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { 'content-type': 'application/json' },
        })

      if (url.endsWith('/auth/me')) {
        return session.user
          ? json(200, session.user)
          : json(401, ERROR('SESSION_EXPIRED', '登录已失效'))
      }
      if (url.endsWith('/auth/login')) {
        const body = JSON.parse(String(init?.body ?? '{}')) as {
          account: string
          password: string
        }
        if (body.password === 'secret') {
          session.user = {
            account: body.account,
            role: body.account === 'developer' ? 'developer' : 'manager',
          }
          return json(200, session.user)
        }
        return json(401, ERROR('INVALID_CREDENTIALS', '账号或密码不正确'))
      }
      if (url.endsWith('/auth/logout')) {
        session.user = null
        return new Response(null, { status: 204 })
      }
      return json(404, ERROR('NOT_FOUND', 'not found'))
    },
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderApp(initialPath: string) {
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/login" element={<div>登录页</div>} />
            <Route path="/forbidden" element={<div>403 页面</div>} />
            <Route
              path="/chat"
              element={
                <RequireRole roles={['manager']}>
                  <ManagerLayout />
                </RequireRole>
              }
            >
              <Route index element={<div>对话工作台</div>} />
            </Route>
            <Route
              path="/developer"
              element={
                // [D26 架构变更] DeveloperLayout 已删除：manager 与 developer
                // 现在共用 ManagerLayout 布局壳，/developer 的页面体由
                // DeveloperPage 承载（守卫下沉到页面级，见 app/router.tsx）。
                // 本文件测的是「认证链路」而非开发者面板内容，故此处用占位页
                // 只验证守卫放行/拦截，避免把 developer.api 的数据依赖拖进来。
                <RequireRole roles={['developer']}>
                  <div>开发者控制台</div>
                </RequireRole>
              }
            />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe('认证全链路（AuthProvider + 守卫 + Query cache）', () => {
  beforeEach(() => {
    queryClient.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('未登录访问受保护路由 → 登录页', async () => {
    installFakeBackend({ user: null })
    renderApp('/chat')

    expect(await screen.findByText('登录页')).toBeInTheDocument()
  })

  it('已登录（会话有效）→ 直接进入工作台，不出现登录页', async () => {
    installFakeBackend({ user: { account: 'manager', role: 'manager' } })
    renderApp('/chat')

    expect(await screen.findByText('对话工作台')).toBeInTheDocument()
    expect(screen.queryByText('登录页')).not.toBeInTheDocument()
  })

  it('developer 会话访问 /developer → 守卫放行；manager 会话 → 403 页', async () => {
    installFakeBackend({ user: { account: 'developer', role: 'developer' } })
    renderApp('/developer')
    expect(await screen.findByText('开发者控制台')).toBeInTheDocument()

    cleanup()
    installFakeBackend({ user: { account: 'manager', role: 'manager' } })
    renderApp('/developer')
    expect(await screen.findByText('403 页面')).toBeInTheDocument()
  })

  it('会话过期：401 触发后清理 Query cache 并跳转登录', async () => {
    const session: FakeSession = {
      user: { account: 'manager', role: 'manager' },
    }
    installFakeBackend(session)
    queryClient.setQueryData(['seed'], { sensitive: 'should-be-cleared' })
    renderApp('/chat')
    expect(await screen.findByText('对话工作台')).toBeInTheDocument()

    // 服务端会话失效：下一次 API 调用返回 401
    session.user = null
    await expect(apiFetch('/auth/me')).rejects.toMatchObject({
      errorCode: 'SESSION_EXPIRED',
    })

    // 2.7.4：清理 Query cache，避免继续展示敏感旧数据
    await waitFor(() => {
      expect(queryClient.getQueryData(['seed'])).toBeUndefined()
    })
    // 守卫重定向到登录页
    expect(await screen.findByText('登录页')).toBeInTheDocument()
  })

  it('注销：退出登录按钮 → 回登录页且缓存被清理', async () => {
    installFakeBackend({ user: { account: 'manager', role: 'manager' } })
    queryClient.setQueryData(['seed'], { sensitive: 'should-be-cleared' })
    renderApp('/chat')
    expect(await screen.findByText('对话工作台')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '退出登录' }))

    expect(await screen.findByText('登录页')).toBeInTheDocument()
    await waitFor(() => {
      expect(queryClient.getQueryData(['seed'])).toBeUndefined()
    })
  })
})
