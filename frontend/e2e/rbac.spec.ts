/**
 * 角色权限隔离 e2e（不依赖大模型、不写 ERP）。
 *
 * 依据：
 * - frontend/src/app/router.tsx：/chat 由 RequireRole(['manager']) 守卫，
 *   /developer 由 RequireRole(['developer']) 守卫，越权 → /forbidden。
 * - frontend/src/pages/LoginPage.tsx：登录后按角色落地（developer → /developer）。
 * - frontend/src/pages/ForbiddenPage.tsx：标题「无权限访问（403）」。
 *
 * 说明：前端守卫只是 UX，真正的权限由后端每个 API 独立校验；这里验证的是
 * 界面层的角色隔离是否符合预期。
 */
import { expect, test } from '@playwright/test'
import { loginThroughUi, requireBackendAndCredentials } from './helpers'
import type { Credentials } from './helpers'

test.describe('角色权限隔离', () => {
  test('developer 登录后落在开发者面板', async ({ page }, testInfo) => {
    const developer: Credentials = await requireBackendAndCredentials(testInfo, 'developer')

    await loginThroughUi(page, developer)

    await page.waitForURL('**/developer')
    await expect(page.getByRole('heading', { name: '开发者面板' })).toBeVisible()
    // developer 无对话区（ManagerLayout.navItemsFor 不渲染对话入口）
    await expect(page.locator('#chat-input')).toHaveCount(0)
  })

  test('developer 访问 manager 专属页 /chat → 落到 403 页', async ({ page }, testInfo) => {
    const developer: Credentials = await requireBackendAndCredentials(testInfo, 'developer')

    await loginThroughUi(page, developer)
    await page.waitForURL('**/developer')

    // 直接改地址访问受保护页面（全量加载，走 /auth/me 恢复会话 → RequireRole 拦截）
    await page.goto('/chat')

    await page.waitForURL('**/forbidden')
    const title = page.locator('.forbidden-page__title')
    await expect(title).toBeVisible()
    await expect(title).toContainText('403')
    // 403 页按角色给出返回入口（developer → /developer），不会形成死循环
    await expect(page.locator('.forbidden-page__actions')).toContainText('返回工作台')
  })
})
