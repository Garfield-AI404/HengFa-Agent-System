/**
 * 主页面无 console 错误 / 未捕获异常（集成问题的兜底用例）。
 *
 * 价值：登录 → 布局 → 对话页 → 会话列表这一整条集成链路里任何一处渲染异常
 * （组件抛错、React key 警告、接口 401 导致的错误提示）都会在这里暴露。
 *
 * 范围克制：只做「登录后看首页」，**不发消息、不点任何确认按钮**——
 * 发消息会触发大模型调用与写工具链路（含 ERP 写入），不在本批用例范围内。
 *
 * 选择器依据：frontend/src/app/layouts/ManagerLayout.tsx（manager 的历史会话
 * 区块 .app-layout__threads-title / .app-layout__threads-error，
 * 数据到位或失败后才会渲染，可作为「页面已稳定」的信号）。
 */
import { expect, test } from '@playwright/test'
import { loginThroughUi, requireBackendAndCredentials } from './helpers'
import type { Credentials } from './helpers'

/**
 * 需要容忍的浏览器噪声：资源加载失败由浏览器自身以 console error 形式打印
 * （如 favicon / 已断开的连接），与前端代码质量无关，单独过滤。
 * 真正的 JS 异常走 pageerror 通道，一律不放过。
 */
const IGNORED_CONSOLE_ERROR = /Failed to load resource|net::ERR_|favicon/i

test.describe('主页面健康度', () => {
  test('登录后主页面渲染完成且无未捕获 JS 错误', async ({ page }, testInfo) => {
    const manager: Credentials = await requireBackendAndCredentials(testInfo, 'manager')

    const problems: string[] = []
    // 监听必须早于首次导航：登录页自身的错误也要纳入统计
    page.on('console', (message) => {
      if (message.type() !== 'error') return
      const text = message.text()
      if (IGNORED_CONSOLE_ERROR.test(text)) return
      problems.push(`console.error: ${text}`)
    })
    page.on('pageerror', (error) => {
      problems.push(`未捕获异常: ${error.message}`)
    })

    await loginThroughUi(page, manager)
    await page.waitForURL('**/chat')
    await expect(page.locator('#chat-input')).toBeVisible()

    // 等布局的会话列表请求落地（加载完成或报错均会渲染该区块），
    // 再留一小段时间捕获接口返回后触发的渲染错误。
    await expect(
      page.locator('.app-layout__threads-title, .app-layout__threads-error').first(),
    ).toBeVisible()
    await page.waitForTimeout(1000)

    expect(problems, `页面存在 console 错误或未捕获异常：\n${problems.join('\n')}`).toEqual([])
  })
})
