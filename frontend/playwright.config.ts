import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright e2e 基础配置（入库，所有环境共用）。
 *
 * 设计约束与取舍：
 * - baseURL 固定 127.0.0.1:5173（与 vite.config.ts 的 server.host/port 一致）。
 *   前端 dev server 已配置 /api 同源代理到后端 127.0.0.1:18000，
 *   因此用例全程同源，不需要处理 CORS 与跨域 cookie。
 * - 后端**不**放进 webServer：后端依赖 Postgres / ERP MCP、启动慢且各环境差异大，
 *   交给人工启动更可控；用例侧通过 e2e/helpers/env.ts 的 isBackendAlive()
 *   前置探活，后端不在时明确 skip 而不是报一堆费解的失败。
 *   启动方式见 e2e/README.md。
 * - projects 必须有至少一项：playwright.local.config.ts（gitignored）通过
 *   baseConfig.projects[0].use 派生 Edge 覆盖配置。
 * - 凭据绝不写在本文件：账号密码一律由环境变量注入（见 e2e/helpers/env.ts）。
 */

/** 前端 dev server 地址（vite；与 vite.config.ts 保持一致）。 */
const FRONTEND_URL = 'http://127.0.0.1:5173'

export default defineConfig({
  // 用例目录：只放 e2e 规格，与 Vitest 单测（tests/）完全分离，
  // 避免 vitest.config.ts 的 include 误收 e2e 用例。
  testDir: './e2e',

  // 单个用例超时：是一条交互链路（登录 + 跳转 + 渲染）的上界，
  // 不覆盖大模型推理——本批用例全部不触发模型调用。
  timeout: 30_000,
  // 断言超时：网络往返（登录 / /auth/me）通常 < 1s，7s 足以吸收冷启动抖动。
  expect: { timeout: 7_000 },

  // CI 上禁止 .only、给一次重试；本地保持零重试，失败就要看见失败。
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // 用例之间互相独立（各自登录、不共享会话），可安全并行。
  fullyParallel: true,

  reporter: [
    ['list'],
    // HTML 报告落到 playwright-report/（已 gitignore），open: 'never' 避免跑完弹窗。
    ['html', { open: 'never' }],
  ],

  use: {
    baseURL: FRONTEND_URL,
    // 失败时留证；trace 只在重试时开，避免本地跑得慢。
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
    video: 'off',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // 自动拉起前端 dev server（vite 冷启动 < 2s，适合交给 Playwright 管）。
  // reuseExistingServer：本机常驻一个 vite 实例，避免 strictPort 抢占冲突。
  webServer: {
    command: 'npx vite --port 5173',
    url: FRONTEND_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    // stdout 不打出来：vite 的启动横幅会淹没测试结果。
    stdout: 'ignore',
  },
})
