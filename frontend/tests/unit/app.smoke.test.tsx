import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { App } from '../../src/app/App'

// 冒烟：整棵应用树（QueryClient + Auth + Router）可渲染。
// 会话未登录（/auth/me 返回 401）→ 应呈现登录页外壳。
describe('App 外壳冒烟测试', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('未登录时渲染登录页', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            error_code: 'SESSION_EXPIRED',
            message: '登录已失效',
            details: null,
            trace_id: 'smoke',
          }),
          { status: 401, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    render(<App />)

    // [D26 文案变更] 品牌 h1 由「恒发内勤 Agent」简化为「恒发 Agent」
    // （src/app/layouts/AuthLayout.tsx，UI-restyle-design.md 4.1 锁定 h1 文案）。
    // 断言跟随生产文案，而不是反向要求生产回退。
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('恒发 Agent')
    expect(screen.getByRole('main')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument()
  })
})
