/**
 * e2e 公共助手：环境门控（后端探活 + 凭据注入）与登录动作。
 *
 * 安全约定（重要）：
 * - 本目录所有文件都会入库，**绝不允许出现任何账号 / 密码字面量**。
 *   凭据一律从环境变量读取（E2E_MANAGER_* / E2E_DEVELOPER_*），缺失时
 *   明确 skip 并说明原因，**不回退到默认账号去撞登录接口**——否则会污染
 *   后端的失败登录计数，且失败信息具有误导性。
 * - 注入方式见同目录 README.md（从 .env 导出到环境变量即可）。
 */
import type { Page, TestInfo } from '@playwright/test'

export interface Credentials {
  account: string
  password: string
}

export type E2ERole = 'manager' | 'developer'

/** 角色 → 环境变量名（值来自 .env，运行前由 shell 导出）。 */
const ENV_KEYS_BY_ROLE: Record<E2ERole, { account: string; password: string }> = {
  manager: { account: 'E2E_MANAGER_ACCOUNT', password: 'E2E_MANAGER_PASSWORD' },
  developer: { account: 'E2E_DEVELOPER_ACCOUNT', password: 'E2E_DEVELOPER_PASSWORD' },
}

/**
 * 后端地址（探活 + 写链路门禁的 developer 探针用）。默认与 vite.config.ts 的
 * proxy target 一致；若改过代理目标，用 E2E_BACKEND_URL 覆盖，避免探活与真实
 * 链路指向不同后端。
 */
export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? 'http://127.0.0.1:18000'
const HEALTH_URL = `${BACKEND_URL}/api/v1/health`

/** 读取指定角色的凭据；任一变量缺失或为空 → null（调用方据此 skip）。 */
export function credentialsFor(role: E2ERole): Credentials | null {
  const keys = ENV_KEYS_BY_ROLE[role]
  const account = process.env[keys.account]?.trim()
  const password = process.env[keys.password]
  if (!account || !password) return null
  return { account, password }
}

/** 缺失的环境变量名（用于 skip 文案，让人一眼知道该补哪个变量）。 */
export function missingCredentialKeys(role: E2ERole): string[] {
  const keys = ENV_KEYS_BY_ROLE[role]
  const missing: string[] = []
  if (!process.env[keys.account]?.trim()) missing.push(keys.account)
  if (!process.env[keys.password]) missing.push(keys.password)
  return missing
}

/**
 * 后端探活：GET /api/v1/health 返回 2xx 视为可用。
 * 后端不交给 Playwright 的 webServer 托管（依赖 Postgres / ERP MCP、启动慢、
 * 环境差异大），改由人工启动 + 用例前置探活，未就绪时 skip 而非误报失败。
 */
export async function isBackendAlive(): Promise<boolean> {
  try {
    const response = await fetch(HEALTH_URL, { signal: AbortSignal.timeout(3000) })
    return response.ok
  } catch {
    // 连接被拒 / 超时 / DNS 失败：统一视为后端未就绪
    return false
  }
}

/**
 * 只需后端在线的用例（如未登录重定向）用这个。
 * testInfo.skip() 会终止当前用例并标记为 skipped，原因会打在报告里。
 */
export async function requireBackend(testInfo: TestInfo): Promise<void> {
  if (!(await isBackendAlive())) {
    skip(testInfo, `后端未就绪（${HEALTH_URL} 无响应）。启动方式见 e2e/README.md`)
    throw new Error('unreachable: skip 已终止用例')
  }
}

/** 需要登录的用例用这个：先探活后端，再取指定角色凭据。 */
export async function requireBackendAndCredentials(
  testInfo: TestInfo,
  role: E2ERole,
): Promise<Credentials> {
  await requireBackend(testInfo)

  const credentials = credentialsFor(role)
  if (!credentials) {
    skip(
      testInfo,
      `缺少 e2e 凭据环境变量：${missingCredentialKeys(role).join(' / ')}。` +
        '注入方式见 e2e/README.md（严禁硬编码进测试文件）',
    )
    throw new Error('unreachable: skip 已终止用例')
  }
  return credentials
}

// ---------------------------------------------------------------------------
// 写链路门禁（[2026-09-12] 本地环境写工具 e2e）
// ---------------------------------------------------------------------------
/**
 * 写链路门禁的契约（见同目录 README「写工具链路 e2e」章节）。
 *
 * 背景：本目录其余用例一律「不发消息、不点确认」（会触发 ERP 写入）。写工具
 * 链路的 e2e 只在**完全本地、零生产接触**的环境下才允许运行（本地 SQLite ERP +
 * 本地 agent 后端）。为此加一道**双重门禁**：
 *
 *   1. `E2E_ALLOW_WRITE=1`——显式开关，缺省不设。语义上等价于「我知道这是本地
 *      环境，允许点确认卡触发写」。
 *   2. `E2E_LOCAL_ERP_URL`——本地 ERP 基地址（如 http://127.0.0.1:18001），用于
 *      核对落库。**必须是回环地址**。
 *
 * **两者缺一即 skip**（不执行任何写入）。而当开关打开、URL 却不是回环地址时，
 * 不是 skip 而是**直接 fail**——这是防止「有人误把门禁指向生产」的硬闸：
 * 宁可用例失败，也绝不让确认卡里的写请求落到回环之外的 ERP。
 *
 * 核对落库另需 `E2E_LOCAL_ERP_API_KEY`（本地 Agent 的 X-API-Key，取值方式见
 * README）；缺失时同样 skip（无法核对，宁可跳过也不盲信 UI 文案）。
 */
export interface LocalErpGate {
  /** 已归一化的 ERP 基地址（无尾斜杠），仅回环。 */
  baseUrl: string
  /** 本地 ERP 查询用 X-API-Key（Agent 身份，仅 erp:query 读路径）。 */
  apiKey: string
}

const WRITE_GATE_FLAG = 'E2E_ALLOW_WRITE'
const LOCAL_ERP_URL_KEY = 'E2E_LOCAL_ERP_URL'
const LOCAL_ERP_API_KEY_KEY = 'E2E_LOCAL_ERP_API_KEY'

/** URL.hostname 对 IPv6 回环返回带方括号的 '[::1]'，一并纳入白名单。 */
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

/**
 * 解析并强制校验本地 ERP 地址必须是回环地址。
 * 非回环 → **抛错（fail）**，绝不降级为 skip：这是写链路唯一的生产隔离闸。
 */
export function parseAndAssertLoopback(rawUrl: string): URL {
  let url: URL
  try {
    url = new URL(rawUrl)
  } catch {
    throw new Error(`E2E_LOCAL_ERP_URL 不是合法 URL：${rawUrl}`)
  }
  if (!isLoopbackHost(url.hostname)) {
    throw new Error(
      `拒绝执行写链路 e2e：E2E_LOCAL_ERP_URL 指向非回环地址（host=${url.hostname}）。` +
        '该用例只允许打向 127.0.0.1 / localhost 上的本地 ERP，绝不触达生产。',
    )
  }
  return url
}

/**
 * 写链路门禁总入口。**必须在用例第一行调用**，让门禁未开时的 skip 早于
 * 任何后端探活 / 登录 / 发消息，从而保证「门禁未开 = 不触达任何写入」。
 *
 * 顺序刻意如此（安全优先）：
 *   ① 开关未开 → skip；
 *   ② URL 缺失 → skip；
 *   ③ URL 非回环 → **fail**（即便 API Key 缺失也先在此拦下）；
 *   ④ API Key 缺失 → skip。
 */
export function requireWriteGate(testInfo: TestInfo): LocalErpGate {
  if ((process.env[WRITE_GATE_FLAG] ?? '').trim() !== '1') {
    skip(
      testInfo,
      `写链路门禁未开：未设置 ${WRITE_GATE_FLAG}=1，本用例不点击确认、不执行任何写入`,
    )
    throw new Error('unreachable: skip 已终止用例')
  }

  const rawUrl = (process.env[LOCAL_ERP_URL_KEY] ?? '').trim()
  if (!rawUrl) {
    skip(
      testInfo,
      `写链路门禁已开但缺少 ${LOCAL_ERP_URL_KEY}（须指向本地 ERP，如 http://127.0.0.1:18001），` +
        '无法核对落库，跳过',
    )
    throw new Error('unreachable: skip 已终止用例')
  }

  // 非回环地址直接 fail（不是 skip）：见函数头「硬闸」说明。
  const url = parseAndAssertLoopback(rawUrl)

  const apiKey = (process.env[LOCAL_ERP_API_KEY_KEY] ?? '').trim()
  if (!apiKey) {
    skip(
      testInfo,
      `写链路门禁已开但缺少 ${LOCAL_ERP_API_KEY_KEY}（本地 ERP 查询用 X-API-Key），无法核对落库，跳过`,
    )
    throw new Error('unreachable: skip 已终止用例')
  }

  return { baseUrl: url.origin, apiKey }
}

/** 本地 ERP 钣金订单按型号子串查询的结果（仅取核对所需字段）。 */
export interface LocalErpMetalQuery {
  total: number
  models: string[]
}

/**
 * 直连本地 ERP REST 核对钣金订单：`GET /api/warehouse/metal?model=<子串>`。
 *
 * 认证用 Agent 的 X-API-Key（get_current_user 优先 X-API-Key；include_deleted
 * 缺省 false，任意登录角色可读）。此路径与 agent 后端写订单的落库表同源，
 * 是对「UI 文案」的独立交叉验证。
 */
export async function queryLocalErpMetalByModel(
  gate: LocalErpGate,
  modelSubstring: string,
): Promise<LocalErpMetalQuery> {
  const url =
    `${gate.baseUrl}/api/warehouse/metal` +
    `?model=${encodeURIComponent(modelSubstring)}&page_size=50`
  const response = await fetch(url, { headers: { 'X-API-Key': gate.apiKey } })
  if (!response.ok) {
    throw new Error(`本地 ERP 查询失败：HTTP ${response.status}（${url}）`)
  }
  const body = (await response.json()) as {
    code?: number
    message?: string
    data?: { total?: number; items?: Array<{ model?: string }> }
  }
  if (body.code !== 0 || !body.data) {
    throw new Error(`本地 ERP 查询返回错误：${body.message ?? '未知错误'}`)
  }
  return {
    total: body.data.total ?? 0,
    models: (body.data.items ?? []).map((item) => item.model ?? ''),
  }
}

/**
 * 轮询等待本地 ERP 中出现至少 `expected` 条匹配记录（写入经 agent 后端异步
 * 落库，UI 回复与 ERP 落库之间存在秒级时差，故轮询而非一次性查询）。
 * 超时返回最后一次查询结果，由调用方断言。
 */
export async function waitForLocalErpMetalCount(
  gate: LocalErpGate,
  modelSubstring: string,
  expected: number,
  timeoutMs = 20_000,
): Promise<LocalErpMetalQuery> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const result = await queryLocalErpMetalByModel(gate, modelSubstring)
    if (result.total >= expected || Date.now() > deadline) return result
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
}

// ---------------------------------------------------------------------------
// 第 3 道断言：agent 后端**实际写入的 ERP** 也必须回环
// ---------------------------------------------------------------------------
/**
 * 为什么需要单独这一道（[2026-09-12] 补洞）：
 *
 * 前两道门禁（`E2E_ALLOW_WRITE`、`E2E_LOCAL_ERP_URL` 回环）校验的是**核对用的
 * 地址**——即用例去查询落库的那个 ERP。但真正执行写入的不是用例，而是**前端经
 * agent 后端**打到它配置的 `ERP_MCP_URL`。若有人把 `E2E_LOCAL_ERP_URL` 设成回环、
 * 而 agent 后端其实连的是生产 ERP，确认卡里的写请求会落到生产，前两道闸拦不住。
 *
 * 因此第 3 道断言穿透到「写请求真正的落点」：用 developer 账号登录取
 * `GET /api/v1/developer/health`（src/administrative_agent/web/routes_developer.py），
 * 从 `dependencies` 取出 `erp_mcp` 那项的 `detail`（形如 `host=127.0.0.1`，仅 host、
 * 不含 key/凭据），**断言该 host 是回环地址**。非回环或取不到 → **fail（不是 skip）**，
 * 把配置问题暴露出来，绝不带着「可能打生产」的不确定性去点确认。
 */
export interface BackendErpProbe {
  /** erp_mcp 依赖状态：configured / missing_credentials / not_configured / invalid。 */
  status: string
  /** 后端实际配置的 ERP MCP 主机名（解析自 detail 的 `host=`）。 */
  host: string
  /** 原始 detail（`host=<host>`），用于报告留证。 */
  detail: string | null
}

/** host 是否为回环（含 IPv6 `::1` 与带方括号形态）。 */
export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host)
}

/**
 * 登录取 developer 身份后查询 `/developer/health`，解析出 erp_mcp 的落点。
 * 任一步失败（登录失败 / 接口非 2xx / 缺 erp_mcp 项 / 无 host）**抛错**——
 * 由调用方表现为用例 fail，绝不降级为 skip。
 */
export async function fetchBackendErpProbe(dev: Credentials): Promise<BackendErpProbe> {
  const loginResp = await fetch(`${BACKEND_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account: dev.account, password: dev.password }),
  })
  if (!loginResp.ok) {
    throw new Error(
      `写链路门禁第 3 道断言失败：developer 登录 ${BACKEND_URL}/api/v1/auth/login → HTTP ${loginResp.status}`,
    )
  }
  // 只取 name=value 片段，丢掉 HttpOnly/SameSite 等属性
  const cookie = loginResp.headers
    .getSetCookie()
    .map((part) => part.split(';')[0])
    .join('; ')
  if (!cookie) {
    throw new Error('写链路门禁第 3 道断言失败：developer 登录未返回会话 Cookie')
  }

  const healthResp = await fetch(`${BACKEND_URL}/api/v1/developer/health`, {
    headers: { cookie },
  })
  if (!healthResp.ok) {
    throw new Error(
      `写链路门禁第 3 道断言失败：GET /api/v1/developer/health → HTTP ${healthResp.status}`,
    )
  }
  const body = (await healthResp.json()) as {
    dependencies?: Array<{ name?: string; status?: string; detail?: string | null }>
  }
  const erp = (body.dependencies ?? []).find((d) => d.name === 'erp_mcp')
  if (!erp) {
    throw new Error('写链路门禁第 3 道断言失败：/developer/health 未返回 erp_mcp 依赖项')
  }
  const detail = erp.detail ?? null
  const host = detail?.match(/host=(\S+)/)?.[1] ?? ''
  if (!host) {
    throw new Error(
      `写链路门禁第 3 道断言失败：erp_mcp 依赖无 host ` +
        `(status=${erp.status ?? 'unknown'}, detail=${detail ?? 'null'})——` +
        '无法确认 agent 后端写入落点，拒绝执行（配置问题，暴露而非跳过）',
    )
  }
  if (!isLoopbackHost(host)) {
    throw new Error(
      `拒绝执行写链路 e2e：agent 后端实际配置的 ERP MCP host=${host} 非回环地址。` +
        '写请求会经 agent 后端落到该 ERP，可能触达生产——硬闸 fail。',
    )
  }
  return { status: erp.status ?? 'unknown', host, detail }
}

/**
 * 跳过当前用例并说明理由。
 * 除了 testInfo.skip（写进报告），额外打一行日志——list/line reporter 默认不展示
 * skip 描述，光看终端会以为用例「莫名没跑」，排查成本很高。
 */
function skip(testInfo: TestInfo, reason: string): void {
  console.log(`[e2e][skip] ${testInfo.titlePath.join(' › ')} → ${reason}`)
  // TestInfo.skip 签名为 skip(condition: boolean, description?: string)——此前传
  // 字符串当 condition（真值→跳过，description 实际丢失）。显式传 true + 描述，
  // 让 skip 原因真正写进报告（playwright 1.62 types 亦要求 boolean）。
  testInfo.skip(true, reason)
}

/**
 * 走真实 UI 登录（不用 API 注入 cookie）：既覆盖表单，也让会话 cookie
 * 按生产路径（HttpOnly Set-Cookie）落地。
 * 选择器取自 frontend/src/pages/LoginPage.tsx：#login-account /
 * #login-password / .login-form__submit。
 */
export async function loginThroughUi(page: Page, credentials: Credentials): Promise<void> {
  await page.goto('/login')

  // 关键等待：AuthProvider 在挂载时会发 /auth/me（React StrictMode 下是两次），
  // 未登录时返回 401 → catch 把状态置为 anonymous。若这两次请求在**登录成功之后**
  // 才落地，会把已认证状态覆盖回匿名，页面被弹回 /login（用例表现为：已跳到
  // /chat 又被踢回登录页）。这是前端既有竞态，生产代码不在本批改动范围内，
  // 因此在提交表单前等应用进入稳定态：networkidle = 上述请求全部返回 + 500ms 静默。
  await page.waitForLoadState('networkidle')

  await page.locator('#login-account').fill(credentials.account)
  await page.locator('#login-password').fill(credentials.password)
  await page.locator('.login-form__submit').click()
}
