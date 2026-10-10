/**
 * 登录链路 e2e（不依赖大模型、不写 ERP）。
 *
 * 覆盖：登录成功跳转、错误密码提示、未登录访问受保护页面被重定向。
 * 所有选择器取自源码：
 * - frontend/src/pages/LoginPage.tsx（#login-account / #login-password /
 *   .login-form__submit / .login-form__error，错误文案按 error_code 映射）
 * - frontend/src/app/router.tsx（/chat 由 RequireRole(['manager']) 守卫）
 * - frontend/src/auth/RequireRole.tsx（匿名 → /login 并携带来源路径）
 * - frontend/src/pages/chat/ChatComposer.tsx（#chat-input 为对话页标志元素）
 */
import { expect, test } from '@playwright/test'
import { loginThroughUi, requireBackend, requireBackendAndCredentials } from './helpers'
import type { Credentials } from './helpers'

test.describe('登录链路', () => {
  test('正确凭据 → 登录成功并跳转到对话页', async ({ page }, testInfo) => {
    const manager: Credentials = await requireBackendAndCredentials(testInfo, 'manager')

    await loginThroughUi(page, manager)

    // manager 角色的落地页是 /chat（LoginPage.resolveHome）
    await page.waitForURL('**/chat')
    await expect(page.locator('#chat-input')).toBeVisible()
    // 会话已建立：刷新后仍应留在对话页（AuthProvider 经 /auth/me 恢复登录态）
    await page.reload()
    await expect(page.locator('#chat-input')).toBeVisible()
  })

  test('错误密码 → 展示错误提示且不跳转', async ({ page }, testInfo) => {
    const manager: Credentials = await requireBackendAndCredentials(testInfo, 'manager')

    // 由真实密码派生一个必然错误的值：测试文件本身不含任何密码字面量
    await loginThroughUi(page, { account: manager.account, password: `${manager.password}__e2e_invalid__` })

    // INVALID_CREDENTIALS 的提示文案见 LoginPage.HINT_BY_ERROR_CODE
    const error = page.locator('.login-form__error')
    await expect(error).toBeVisible()
    await expect(error).toContainText('账号或密码不正确')
    // 仍停留在登录页，未进入对话页
    await expect(page).toHaveURL(/\/login/)
    await expect(page.locator('#chat-input')).toHaveCount(0)
  })
})

test.describe('未登录保护', () => {
  test('未登录直接访问 /chat → 被重定向到登录页', async ({ page }, testInfo) => {
    await requireBackend(testInfo)

    await page.goto('/chat')

    // RequireRole：匿名 → Navigate('/login', { state: { from: '/chat' } })
    await page.waitForURL('**/login')
    await expect(page.locator('#login-account')).toBeVisible()
    await expect(page.locator('#chat-input')).toHaveCount(0)
  })
})
