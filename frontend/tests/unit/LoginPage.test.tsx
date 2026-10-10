import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { ApiError } from '../../src/api/client'
import { AuthContext } from '../../src/auth/AuthProvider'
import type { AuthContextValue } from '../../src/auth/AuthProvider'
import type { CurrentUser } from '../../src/auth/auth.types'
import { LoginPage } from '../../src/pages/LoginPage'

const manager: CurrentUser = { account: 'manager', role: 'manager' }
const developer: CurrentUser = { account: 'developer', role: 'developer' }

/** 与 LoginPage 内 REMEMBER_KEY 保持一致。 */
const REMEMBER_KEY = 'hengfa.login.remember'

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  localStorage.clear()
})

function renderLoginPage(loginMock: AuthContextValue['login']) {
  const value: AuthContextValue = {
    user: null,
    status: 'anonymous',
    login: loginMock,
    logout: vi.fn(async () => {}),
  }
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthContext.Provider value={value}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/chat" element={<div>对话工作台</div>} />
          <Route path="/developer" element={<div>开发者控制台</div>} />
        </Routes>
      </AuthContext.Provider>
    </MemoryRouter>,
  )
}

async function fillAndSubmit(account: string, password: string) {
  fireEvent.change(screen.getByLabelText('账号'), { target: { value: account } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: password } })
  fireEvent.click(screen.getByRole('button', { name: '登录' }))
}

describe('LoginPage 登录页', () => {
  it('渲染表单字段与"记住账号"勾选框（共享账号提示已下线）', () => {
    renderLoginPage(vi.fn())

    expect(screen.getByLabelText('账号')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument()
    expect(screen.getByLabelText('记住账号')).toBeInTheDocument()

    // [D26 行为变更] 旧版在此还断言 role="note" 的「共享 manager / developer
    // 账号」说明块。该提示已被**有意移除**（LoginPage 头注释第 3 条：12.1 无
    // 用户表，共享账号凭据由运维发放，不在登录页暴露账号清单）。此处反向断言
    // 该块不存在，防止其在后续重构中被无意加回。
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('勾选"记住账号"→ 登录成功仅存账号名，绝不存密码（P0-1 安全回归）', async () => {
    const loginMock = vi.fn(async () => manager)
    renderLoginPage(loginMock)

    fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'manager' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'super-secret' } })
    // 必须在提交**之前**勾选：handleSubmit 读取的是提交瞬间的 remember 快照
    fireEvent.click(screen.getByLabelText('记住账号'))
    expect(screen.getByLabelText('记住账号')).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('对话工作台')).toBeInTheDocument()

    const raw = localStorage.getItem(REMEMBER_KEY)
    expect(raw).not.toBeNull()
    // 核心安全断言（P0-1）：密码明文不得出现在 localStorage 的任何位置
    expect(raw).not.toContain('super-secret')
    expect(JSON.parse(raw as string)).toEqual({ account: 'manager' })
  })

  it('未勾选"记住账号"→ 登录成功后清除既有存档（含旧版明文密码存档）', async () => {
    // 模拟旧版本遗留的明文密码存档
    localStorage.setItem(
      REMEMBER_KEY,
      JSON.stringify({ account: 'manager', password: 'leaked-plaintext' }),
    )
    const loginMock = vi.fn(async () => manager)
    renderLoginPage(loginMock)

    // 旧存档含 password → 不得回填账号（readRememberedAccount 拒绝）
    expect(screen.getByLabelText('账号')).toHaveValue('')

    // 默认未勾选（无有效存档）→ 提交后清除旧存档
    await fillAndSubmit('manager', 'secret')
    expect(await screen.findByText('对话工作台')).toBeInTheDocument()
    expect(localStorage.getItem(REMEMBER_KEY)).toBeNull()
  })

  it('有账号存档 → 回填账号名且默认勾选，密码输入框保持为空', () => {
    localStorage.setItem(REMEMBER_KEY, JSON.stringify({ account: 'manager' }))
    renderLoginPage(vi.fn())

    expect(screen.getByLabelText('账号')).toHaveValue('manager')
    expect(screen.getByLabelText('密码')).toHaveValue('')
    expect(screen.getByLabelText('记住账号')).toBeChecked()
  })

  it('429 限速（RATE_LIMITED）：限速提示与等待引导（RV-06 a / RV-38 非阻塞 3）', async () => {
    // nginx limit_req 返回标准 429（非 JSON 体 → client.ts 映射 RATE_LIMITED）
    const loginMock = vi.fn(async () => {
      throw new ApiError(429, {})
    })
    renderLoginPage(loginMock)

    await fillAndSubmit('manager', 'secret')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('登录尝试过于频繁，已触发限速保护')
    expect(alert).toHaveTextContent('请等待 30 秒以上再重试')
  })

  it('空表单不提交', () => {
    const loginMock = vi.fn()
    renderLoginPage(loginMock)

    fireEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(loginMock).not.toHaveBeenCalled()
  })

  it('manager 登录成功跳转 /chat（账号 trim 后提交）', async () => {
    const loginMock = vi.fn(async () => manager)
    renderLoginPage(loginMock)

    await fillAndSubmit('  manager  ', 'secret')

    expect(await screen.findByText('对话工作台')).toBeInTheDocument()
    expect(loginMock).toHaveBeenCalledWith('manager', 'secret')
  })

  it('developer 登录成功跳转 /developer', async () => {
    const loginMock = vi.fn(async () => developer)
    renderLoginPage(loginMock)

    await fillAndSubmit('developer', 'secret')

    expect(await screen.findByText('开发者控制台')).toBeInTheDocument()
  })

  it('INVALID_CREDENTIALS 展示分支提示且停留登录页', async () => {
    const loginMock = vi.fn(async () => {
      throw new ApiError(401, {
        error_code: 'INVALID_CREDENTIALS',
        message: '用户名或密码不正确',
        details: null,
        trace_id: 't-1',
      })
    })
    renderLoginPage(loginMock)

    await fillAndSubmit('manager', 'wrong')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('账号或密码不正确。')
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument()
  })

  it('非 ApiError（网络失败）归一到 NETWORK_ERROR 提示', async () => {
    const loginMock = vi.fn(async () => {
      throw new TypeError('fetch failed')
    })
    renderLoginPage(loginMock)

    await fillAndSubmit('manager', 'secret')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '无法连接服务器，请检查网络后重试。',
    )
  })

  it('提交期间禁用重复提交（2.7.4）', async () => {
    let resolveLogin!: (user: CurrentUser) => void
    const loginMock = vi.fn(
      () =>
        new Promise<CurrentUser>((resolve) => {
          resolveLogin = resolve
        }),
    )
    renderLoginPage(loginMock)

    await fillAndSubmit('manager', 'secret')
    fireEvent.click(screen.getByRole('button', { name: '登录中…' }))

    expect(loginMock).toHaveBeenCalledTimes(1)

    resolveLogin(manager)
    expect(await screen.findByText('对话工作台')).toBeInTheDocument()
  })
})
