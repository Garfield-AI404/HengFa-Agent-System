/**
 * 写工具链路 e2e：一条消息两条同类钣金订单 → 一张确认卡两行明细 → 确认 →
 * 本地 ERP 落 2 条（[2026-09-12]）。
 *
 * ⚠️ 默认跳过，且只允许打向回环地址上的本地 ERP。本用例会**点击确认卡触发 ERP
 * 写入**——门禁为**三道断言**（顺序在最前，早于任何写入）：
 *   ① 未设 `E2E_ALLOW_WRITE=1`                          → skip；
 *   ② 未设 `E2E_LOCAL_ERP_URL` / 未设 `E2E_LOCAL_ERP_API_KEY` → skip；
 *   ③ `E2E_LOCAL_ERP_URL` 非回环地址                    → **fail**；
 *   ④ agent 后端实际写入的 ERP（`/api/v1/developer/health` 的 erp_mcp host）
 *      非回环或取不到                                    → **fail**。
 * ③④ 是防误打生产的硬闸：③ 管「核对用的地址」，④ 管**写请求真正的落点**（后端
 * 配置的 ERP_MCP_URL）——后者才是关键，见 helpers.fetchBackendErpProbe 注释。
 * 前置条件与注入方式见同目录 README「写工具链路 e2e」章节。
 *
 * ## 为什么是「一张卡两条明细」而不是「两张卡」
 *
 * 真实模型实测：同一条消息里的多条同类记录会被合并成一次 `order_create`（items
 * 两条），出一张卡（两行明细）。而「两条异类 → 两张卡」结构上不可能——后端
 * route_after_tools（src/administrative_agent/agent/graph.py）在写工具产出
 * pending_confirmation 后直接路由到 confirm，**一条用户消息最多产出一张写确认卡**。
 * 故本用例断言「恰一张卡」。
 *
 * ## 选择器依据（改组件时按图索骥）
 * - frontend/src/pages/chat/ChatComposer.tsx：`#chat-input`、发送钮 aria-label「发送消息」；
 * - frontend/src/pages/ChatPage.tsx：确认卡在 `.chat-inline-card` 内由 ConfirmationPanel 渲染；
 * - frontend/src/components/ConfirmationPanel.tsx：卡根 `.confirmation-panel`，
 *   多明细矩阵表 `.confirmation-panel__plan`（首列表头 = row_header_label「明细」），
 *   外层键形如「1. 客户 昌盛五金 / MK-01」，内层「操作 / 字段和值」经 NestedValue
 *   逐行展开（**不得**渲染成 `[object Object]` 或 JSON 串——本次改造的功能性修复点）；
 * - frontend/src/pages/chat/ChatMessageRow.tsx：终态助手回复用
 *   `.chat-message__content--markdown`（流式中/等待动画不用该 class）。
 */
import { expect, test } from '@playwright/test'
import {
  credentialsFor,
  fetchBackendErpProbe,
  loginThroughUi,
  missingCredentialKeys,
  requireBackendAndCredentials,
  requireWriteGate,
  waitForLocalErpMetalCount,
} from './helpers'
import type { Credentials } from './helpers'

/**
 * 本地 dev-e2e-seed.json 预置的客户（元/kg 计价，单价 15.5）。本地栈预置数据，
 * 变更 seed 时需同步。重量单位按客户 price_unit 换算（元/kg → kg），故订单里
 * 显式给 kg。
 */
const CUSTOMER_NAME = '昌盛五金'

/** 本次运行的唯一标记：模型串含该标记，用于精确核对本次落库、避免与历史数据混淆。 */
const RUN_MARKER = `E2E${Date.now().toString(36).toUpperCase()}`
const MODEL_A = `${RUN_MARKER}A`
const MODEL_B = `${RUN_MARKER}B`

/** 确认卡「确认」按钮的可访问名称（ConfirmationPanel 的 aria-label）。 */
const CONFIRM_BUTTON = '确认并执行该写入计划'

test.describe('写工具链路（本地环境门禁）', () => {
  test('一条消息两条同类订单 → 一张卡两行 → 确认后本地 ERP 落 2 条', async ({
    page,
  }, testInfo) => {
    // 门禁最先判定：门禁未开时，本用例在任何后端探活 / 登录 / 发消息之前就以
    // skip 结束，保证「门禁未开 = 零写入、零触达」。
    const gate = requireWriteGate(testInfo)

    // 第 3 道断言（关键闸）：agent 后端**实际写入的 ERP** 必须回环。它穿透到写
    // 请求真正的落点（后端配置的 ERP_MCP_URL 主机名），而不只是核对用的地址；
    // 非回环或取不到 → fail（见 helpers.fetchBackendErpProbe）。
    const developer: Credentials | null = credentialsFor('developer')
    if (!developer) {
      throw new Error(
        '写链路门禁第 3 道断言无法执行：缺少 developer 凭据 ' +
          `${missingCredentialKeys('developer').join(' / ')}（README §2 注入）。` +
          '这是配置问题，按门禁要求 fail 而非 skip。',
      )
    }
    const erpProbe = await fetchBackendErpProbe(developer)
    console.log(
      `[e2e][gate] agent 后端 ERP 落点：status=${erpProbe.status} ` +
        `host=${erpProbe.host} detail=${erpProbe.detail}`,
    )

    // 写链路含真实模型调用 + 确认后续传，超时按链路整体放宽（不退让 base config
    // 对其它用例的 30s 约束，仅本用例放大）。
    testInfo.setTimeout(180_000)

    const manager: Credentials = await requireBackendAndCredentials(testInfo, 'manager')

    await loginThroughUi(page, manager)
    await page.waitForURL('**/chat')
    await expect(page.locator('#chat-input')).toBeVisible()

    // 一句口语包含两条同类（钣金）订单；型号带唯一标记供落库核对。
    const message =
      `帮我给客户${CUSTOMER_NAME}录两条钣金订单：` +
      `型号 ${MODEL_A}，数量 100 套，单价 15.5 元/kg，单件重量 2.5 kg，今天入库；` +
      `型号 ${MODEL_B}，数量 50 套，单价 15.5 元/kg，单件重量 2.5 kg，今天入库。`

    await page.locator('#chat-input').fill(message)
    await page.getByRole('button', { name: '发送消息' }).click()

    // ① 恰好一张确认卡（一条用户消息最多一张写确认卡）。
    const panels = page.locator('.confirmation-panel')
    await expect(panels).toHaveCount(1, { timeout: 90_000 })
    const panel = panels.first()
    await expect(panel).toBeVisible()

    // ② 两行明细：矩阵表（首列「明细」），外层键形如「1. 客户 … / MK-01」。
    const planRows = panel.locator('.confirmation-panel__plan > tbody > tr')
    await expect(planRows).toHaveCount(2)
    // 只取每行的**首列商务标识**（外层键）。注意：内层「字段和值」子表也用
    // `<th scope="row">` 渲染键名，故不能用 th[scope=row] 全收——必须限定为行内
    // 第一个，否则会把子表键名一并算进来。
    const rowHeaders: string[] = []
    for (let i = 0; i < 2; i++) {
      const text = await planRows.nth(i).locator('th[scope="row"]').first().innerText()
      rowHeaders.push(text.trim())
    }
    expect(rowHeaders[0].startsWith('1. ')).toBeTruthy()
    expect(rowHeaders[1].startsWith('2. ')).toBeTruthy()
    const headerJoined = rowHeaders.join('\n')
    expect(headerJoined).toContain(MODEL_A)
    expect(headerJoined).toContain(MODEL_B)

    // ③ 功能性修复钉死：嵌套「字段和值」必须逐行展开，不得退化成
    //    `[object Object]` 或 JSON 串（本次前端唯一功能性修复点）。
    const cardText = await panel.innerText()
    expect(cardText).not.toContain('[object Object]')
    expect(cardText).not.toContain('{')

    // ④ 确认执行：乐观清卡 → 等待终态助手回复（成功文案由模型生成，不锚定措辞；
    //    真正的成败以第 ⑤ 步本地 ERP 落库为准）。
    await panel.getByRole('button', { name: CONFIRM_BUTTON }).click()
    await expect(panels).toHaveCount(0)
    await expect(
      page.locator('.chat-message--assistant .chat-message__content--markdown').last(),
    ).toBeVisible({ timeout: 90_000 })

    // ⑤ 交叉验证：直连本地 ERP 按唯一标记查询，确认确实落了 2 条（不信 UI 文案）。
    const found = await waitForLocalErpMetalCount(gate, RUN_MARKER, 2)
    expect(found.total, `本地 ERP 未落入预期 2 条（标记 ${RUN_MARKER}）`).toBe(2)
    expect([...found.models].sort()).toEqual([MODEL_A, MODEL_B].sort())
  })
})
