/**
 * DeveloperPage 单测（本页此前无单测）。
 *
 * 重点覆盖新增的「事件流落库」块（复用 health 查询的顶层 `stream_events` 字段）：
 * - (a) `stream_events: null`（接线未启用/未接线）→ 渲染「未启用」；
 * - (b) `dropped: 3` → 汇总行里「丢弃」与计数 3 同现（把计数与标签绑死，
 *       防止「别的数字凑巧是 3」把断言救活——本仓踩过同字词救活的假锁）；
 * - (c) `dependencies` 仍渲染恰好 4 行（回归保护：新块不得挤掉原有内容）。
 *
 * developer.api 以 vi.mock 替换，避免真实网络；react-query 用独立 QueryClient
 * （retry 关）包一层，与全局实例隔离。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DeveloperHealthOut, StreamEventSinkStatusOut } from '../../src/api/developer.api'

// vi.mock 工厂被提升到模块顶部，故用 vi.hoisted 承载可变桩。
// ⚠️ 工厂**枚举**了 developer.api 的全部导出：新增导出（本批的 stream-events 两个
// 函数）必须同步加进来，否则页面运行时 TypeError（且报错信息会误导人）。
const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  logs: vi.fn(),
  threads: vi.fn(),
  detail: vi.fn(),
}))

vi.mock('../../src/api/developer.api', () => ({
  getDeveloperHealthApi: mocks.health,
  getDeveloperLogsApi: mocks.logs,
  getStreamEventThreadsApi: mocks.threads,
  getStreamEventThreadDetailApi: mocks.detail,
  DEV_LOG_LEVELS: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
}))

// 必须在 vi.mock 之后 import，拿到的才是桩实现。
import { DeveloperPage } from '../../src/pages/DeveloperPage'

const DEPENDENCIES = [
  { name: 'erp_mcp', status: 'configured', detail: null },
  { name: 'agent_database', status: 'configured', detail: null },
  { name: 'checkpoint_database', status: 'configured', detail: null },
  { name: 'session', status: 'configured', detail: null },
]

const BASE_HEALTH: DeveloperHealthOut = {
  status: 'ok',
  app: 'administrative-agent',
  version: '0.1.0',
  log_level: 'INFO',
  dependencies: DEPENDENCIES,
}

const HEALTHY_STREAM: StreamEventSinkStatusOut = {
  enabled: true,
  queued: 0,
  written: 1234,
  dropped: 0,
  write_failures: 0,
  unmapped_events: 0,
  oversize_dropped: 0,
  pruned_rows: 0,
  last_write_age_seconds: 1.2,
  degraded_reason: null,
}

function renderPage(health: DeveloperHealthOut) {
  mocks.health.mockResolvedValue(health)
  mocks.logs.mockResolvedValue({ items: [], total: 0, limit: 50 })
  // 任务时间线面板（可观测性第 1 步）同样挂在页面上：给空桩，避免它去碰真实网络。
  mocks.threads.mockResolvedValue({
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
    untracked_events: 0,
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DeveloperPage />
    </QueryClientProvider>,
  )
}

describe('DeveloperPage 开发者面板', () => {
  it('(a) stream_events 为 null → 事件流块渲染「未启用」', async () => {
    renderPage({ ...BASE_HEALTH, stream_events: null })

    expect(await screen.findByText(/未启用/)).toHaveTextContent('未启用（数据库未配置或未接线）。')
  })

  it('(a2) 老后端缺字段（undefined）→ 同样按未启用处理，不白屏', async () => {
    const legacy = { ...BASE_HEALTH } as DeveloperHealthOut
    delete (legacy as { stream_events?: unknown }).stream_events
    renderPage(legacy)

    // 页面主体（依赖表）仍应正常渲染，且事件流块显示未启用
    expect(await screen.findByText('ERP MCP')).toBeInTheDocument()
    expect(screen.getByText(/未启用/)).toHaveTextContent('未启用（数据库未配置或未接线）。')
  })

  it('(b) dropped=3 → 汇总行同时含「丢弃」与计数 3', async () => {
    renderPage({
      ...BASE_HEALTH,
      stream_events: { ...HEALTHY_STREAM, dropped: 3 },
    })

    // 正则把标签与数值绑死：dropped 显示错误（如恒 0）时必红。
    const row = await screen.findByText(/丢弃\s*3\b/)
    expect(row).toHaveTextContent('丢弃 3')
    // 其余计数保持原值，确认是这一项而非别处凑出的 3
    expect(row).toHaveTextContent('写失败 0')
    expect(row).toHaveTextContent('未映射事件 0')
    // 计数非零 → 该汇总行须带 .kv-note--error（标红加粗）。
    // 断言落在 className 字符串本身：删掉三元、恒返回 'kv-note' 时必红。
    expect(row.className).toContain('kv-note--error')
  })

  it('(b2) 全部计数为 0 → 汇总行不带 .kv-note--error（反向对照，防「恒标红」）', async () => {
    renderPage({ ...BASE_HEALTH, stream_events: HEALTHY_STREAM })

    const row = await screen.findByText(/已写入\s*1234\b/)
    expect(row).toHaveTextContent('丢弃 0')
    // oversize_dropped=0 → 显示「超限丢弃 0」且不标红（正常路径）。
    expect(row).toHaveTextContent('超限丢弃 0')
    expect(row.className).not.toContain('kv-note--error')
  })

  it('(d1) oversize_dropped=2 而其它计数全 0 → 必须标红', async () => {
    renderPage({
      ...BASE_HEALTH,
      stream_events: { ...HEALTHY_STREAM, oversize_dropped: 2 },
    })

    const row = await screen.findByText(/超限丢弃\s*2\b/)
    // 数值层：该行确实显示 2（排除别处凑出的 2 救活断言）
    expect(row).toHaveTextContent('超限丢弃 2')
    expect(row).toHaveTextContent('丢弃 0')
    // 类名层：新增计数丢数据必须纳入标红条件，否则本用例变红。
    expect(row.className).toContain('kv-note--error')
  })

  it('(d2) 老后端缺 oversize_dropped 字段 → 不报错，按 0 显示且不标红', async () => {
    const legacyStream = { ...HEALTHY_STREAM } as Partial<StreamEventSinkStatusOut>
    delete legacyStream.oversize_dropped
    renderPage({
      ...BASE_HEALTH,
      stream_events: legacyStream as StreamEventSinkStatusOut,
    })

    const row = await screen.findByText(/已写入\s*1234\b/)
    // 兜底为 0，而非渲染成 undefined
    expect(row).toHaveTextContent('超限丢弃 0')
    expect(row).not.toHaveTextContent('undefined')
    expect(row.className).not.toContain('kv-note--error')
  })

  it('(e1) last_error_kind=IntegrityError + write_failures=1 → 显示类名且标红', async () => {
    renderPage({
      ...BASE_HEALTH,
      stream_events: { ...HEALTHY_STREAM, last_error_kind: 'IntegrityError', write_failures: 1 },
    })

    // 白名单映射文案里含 IntegrityError
    expect(await screen.findByText(/最近写失败异常/)).toHaveTextContent('IntegrityError')
    // write_failures>0 → 汇总行标红
    const row = screen.getByText(/已写入\s*1234\b/)
    expect(row.className).toContain('kv-note--error')
  })

  it('(e2) 未在白名单的类名 → 只显示兜底文案，绝不回显原字符串', async () => {
    const { container } = renderPage({
      ...BASE_HEALTH,
      stream_events: { ...HEALTHY_STREAM, last_error_kind: 'WeirdError', write_failures: 1 },
    })

    const line = await screen.findByText(/最近写失败异常/)
    expect(line).toHaveTextContent('其它错误（详情见服务日志）')
    // 关键：整页都不得出现后端传入的原始字符串
    expect(container.textContent).not.toContain('WeirdError')
  })

  it('(e3) 老后端缺 last_error_kind → 不报错、该行不显示、无 undefined', async () => {
    const legacyStream = { ...HEALTHY_STREAM } as Partial<StreamEventSinkStatusOut>
    delete legacyStream.last_error_kind
    const { container } = renderPage({
      ...BASE_HEALTH,
      stream_events: legacyStream as StreamEventSinkStatusOut,
    })

    expect(await screen.findByText(/已写入\s*1234\b/)).toBeInTheDocument()
    expect(screen.queryByText(/最近写失败异常/)).toBeNull()
    expect(container.textContent).not.toContain('undefined')
  })

  it('(e4) last_error_kind=null + write_failures=0 → 不显示该行、不标红', async () => {
    renderPage({
      ...BASE_HEALTH,
      stream_events: { ...HEALTHY_STREAM, last_error_kind: null, write_failures: 0 },
    })

    const row = await screen.findByText(/已写入\s*1234\b/)
    expect(screen.queryByText(/最近写失败异常/)).toBeNull()
    expect(row.className).not.toContain('kv-note--error')
  })

  it('(c) dependencies 仍渲染恰好 4 行（回归保护）', async () => {
    renderPage({ ...BASE_HEALTH, stream_events: HEALTHY_STREAM })

    // 等依赖表出现
    expect(await screen.findByText('ERP MCP')).toBeInTheDocument()

    // 作用域限定到「系统健康」面板：任务时间线面板也用 table.data-table，
    // 全局选择器会让两个模块的 fixture 互相牵制——改其中一个的行数就红另一个，
    // 且报错信息会指向错的文件。这里只断言健康面板自己的表。
    const healthPanel = screen.getByText('系统健康').closest('section')
    if (!healthPanel) throw new Error('unreachable: 系统健康面板未渲染')
    const rows = healthPanel.querySelectorAll('table.data-table tbody tr')
    expect(rows).toHaveLength(4)
    expect(screen.getByText('Agent 数据库')).toBeInTheDocument()
  })
})
