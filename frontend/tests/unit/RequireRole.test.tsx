import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { AuthContext } from '../../src/auth/AuthProvider'
import type { AuthContextValue } from '../../src/auth/AuthProvider'
import { RequireRole } from '../../src/auth/RequireRole'

function authValue(overrides: Partial<AuthContextValue>): AuthContextValue {
  return {
    user: null,
    status: 'loading',
    login: vi.fn(async () => {
      throw new Error('测试中不调用 login')
    }),
    logout: vi.fn(async () => {}),
    ...overrides,
  }
}

function renderGuarded(initialPath: string, value: AuthContextValue) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuthContext.Provider value={value}>
        <Routes>
          <Route path="/login" element={<div>登录页</div>} />
          <Route path="/forbidden" element={<div>403 页面</div>} />
          <Route
            path="/chat"
            element={
              <RequireRole roles={['manager']}>
                <div>manager 内容</div>
              </RequireRole>
            }
          />
          <Route
            path="/developer"
            element={
              <RequireRole roles={['developer']}>
                <div>developer 内容</div>
              </RequireRole>
            }
          />
        </Routes>
      </AuthContext.Provider>
    </MemoryRouter>,
  )
}

const manager = { account: 'manager', role: 'manager' as const }
const developer = { account: 'developer', role: 'developer' as const }

describe('RequireRole 角色守卫', () => {
  it('loading 状态显示验证提示，不跳转', () => {
    renderGuarded('/chat', authValue({ status: 'loading', user: null }))

    expect(screen.getByRole('status')).toHaveTextContent('正在验证登录状态')
    expect(screen.queryByText('登录页')).not.toBeInTheDocument()
  })

  it('未登录访问受保护路由 → 重定向登录页', () => {
    renderGuarded('/chat', authValue({ status: 'anonymous', user: null }))

    expect(screen.getByText('登录页')).toBeInTheDocument()
  })

  it('manager 访问 manager 路由 → 放行', () => {
    renderGuarded('/chat', authValue({ status: 'authenticated', user: manager }))

    expect(screen.getByText('manager 内容')).toBeInTheDocument()
  })

  it('developer 访问 manager 路由 → 403', () => {
    renderGuarded('/chat', authValue({ status: 'authenticated', user: developer }))

    expect(screen.getByText('403 页面')).toBeInTheDocument()
  })

  it('developer 访问 developer 路由 → 放行', () => {
    renderGuarded(
      '/developer',
      authValue({ status: 'authenticated', user: developer }),
    )

    expect(screen.getByText('developer 内容')).toBeInTheDocument()
  })

  it('manager 访问 developer 路由 → 403', () => {
    renderGuarded(
      '/developer',
      authValue({ status: 'authenticated', user: manager }),
    )

    expect(screen.getByText('403 页面')).toBeInTheDocument()
  })
})
