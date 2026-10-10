/**
 * StreamTimelinePanel 单测（可观测性第 1 步 · 方案 §7.4 用例 24-32）。
 *
 * 整页渲染 DeveloperPage（而非只渲染面板），目的是让用例 31 能验证
 * 「新块请求失败时 health / logs 两块仍在」——面板不是孤岛。
 *
 * developer.api 以 vi.mock 替换（工厂必须**枚举全部导出**，新增导出忘加会
 * 导致页面运行时 TypeError）；react-query 用独立 QueryClient（retry 关）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../../src/api/client'
import type {
  DeveloperHealthOut,
  StreamEventItemOut,
  StreamEventThreadDetailOut,
  StreamEventThreadsOut,
} from '../../src/api/developer.api'

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

const THREAD_A = '6f1c2d3e4a5b6c7d8e9f0001'
const THREAD_B = 'abcdef01234567890000ffff'

const BASE_HEALTH: DeveloperHealthOut = {
  status: 'ok',
  app: 'administrative-agent',
  version: '0.1.0',
  log_level: 'INFO',
  dependencies: [
    { name: 'erp_mcp', status: 'configured', detail: null },
    { name: 'agent_database', status: 'configured', detail: null },
    { name: 'checkpoint_database', status: 'configured', detail: null },
    { name: 'session', status: 'configured', detail: null },
  ],
}

const EMPTY_THREADS: StreamEventThreadsOut = {
  items: [],
  total: 0,
  limit: 50,
  offset: 0,
  untracked_events: 0,
}

const TWO_THREADS: StreamEventThreadsOut = {
  items: [
    {
      thread_id: THREAD_A,
      event_count: 812,
      stream_count: 3,
      chunk_count: 795,
      error_count: 0,
      suppressed_count: 12,
      unmapped_count: 0,
      first_seen: '2026-09-18T02:11:07+00:00',
      last_seen: '2026-09-18T02:14:52+00:00',
    },
    {
      thread_id: THREAD_B,
      event_count: 30,
      stream_count: 1,
      chunk_count: 0,
      error_count: 0,
      suppressed_count: 0,
      unmapped_count: 0,
      first_seen: '2026-09-17T10:00:00+00:00',
      last_seen: '2026-09-17T10:00:05+00:00',
    },
  ],
  total: 2,
  limit: 50,
  offset: 0,
  untracked_events: 0,
}

const CREATED_AT = '2026-09-18T02:11:07.421337+00:00'

function chunk(seq: number, chars: number): StreamEventItemOut {
  return {
    event_id: `s-a-e${String(seq).padStart(4, '0')}`,
    stream_id: 's-a',
    sequence: seq,
    event_type: 'agent_reply_chunk',
    record_kind: 'pushed',
    task_id: null,
    payload: { chunk_chars: chars },
    created_at: CREATED_AT,
  }
}

function detailOf(
  items: StreamEventItemOut[],
  over: Partial<StreamEventThreadDetailOut> = {},
): StreamEventThreadDetailOut {
  return {
    thread_id: THREAD_A,
    items,
    total: items.length,
    limit: 500,
    offset: 0,
    expected_events: items.length,
    missing_events: 0,
    ...over,
  }
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DeveloperPage />
    </QueryClientProvider>,
  )
}

/** 时间线面板容器（`任务时间线` 标题所在的 .panel）。 */
function timelinePanel(): HTMLElement {
  const panel = screen.getByRole('heading', { name: '任务时间线' }).closest('.panel')
  if (!panel) throw new Error('未找到任务时间线面板')
  return panel as HTMLElement
}

/** 点开第一条会话（「查看」按钮按列表顺序取第一个）。 */
async function openFirstThread() {
  const buttons = await screen.findAllByRole('button', { name: '查看' })
  fireEvent.click(buttons[0])
  await screen.findByRole('button', { name: '← 返回会话列表' })
}

beforeEach(() => {
  mocks.health.mockResolvedValue(BASE_HEALTH)
  mocks.logs.mockResolvedValue({ items: [], total: 0, limit: 50 })
  mocks.threads.mockResolvedValue(EMPTY_THREADS)
  mocks.detail.mockResolvedValue(detailOf([]))
})

describe('StreamTimelinePanel 任务时间线', () => {
  // 用例 24：列表精确行数 + 每行 thread_id 前 8 位与事件数都在
  it('24 会话列表渲染精确行数，含 thread_id 前 8 位与事件数', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    renderPage()

    await screen.findByText('6f1c2d3e')

    const rows = timelinePanel().querySelectorAll('table.data-table tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('6f1c2d3e')
    expect(rows[0].textContent).toContain('812（其中分片 795）')
    expect(rows[1].textContent).toContain('abcdef01')
    expect(rows[1].textContent).toContain('30（其中分片 0）')
  })

  // 用例 25：点「查看」→ detail 被调用且参数正确；出现返回按钮
  it('25 点「查看」→ detail 以 (threadId, {limit, offset}) 调用并进入时间线', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(detailOf([chunk(1, 5), chunk(2, 5)]))
    renderPage()

    await openFirstThread()

    expect(mocks.detail).toHaveBeenCalledWith(THREAD_A, { limit: 500, offset: 0 })
    expect(screen.getByRole('button', { name: '← 返回会话列表' })).toBeInTheDocument()
  })

  // 用例 26：折叠行文案与造的数据严格绑定
  it('26 折叠行显示「分片 3」「共 41 字」', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    // 10 + 12 + 19 = 41；3 与 41 不用于其它展示字段（total 另用别的数）。
    mocks.detail.mockResolvedValue(detailOf([chunk(1, 10), chunk(2, 12), chunk(3, 19)]))
    renderPage()

    await openFirstThread()

    const runRow = await screen.findByText(/分片 3 · 共 41 字/)
    expect(runRow).toHaveTextContent('单段 10–19 字')
    expect(runRow).toHaveTextContent('seq 1–3')
  })

  // 用例 27：展开折叠行 → 原始 event_id 出现；返回后展开态复位
  it('27 展开折叠行出现原始 event_id，返回后展开态复位', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(detailOf([chunk(1, 10), chunk(2, 12), chunk(3, 19)]))
    const { container } = renderPage()

    await openFirstThread()
    await screen.findByText(/分片 3 · 共 41 字/)
    expect(container.textContent).not.toContain('s-a-e0001')

    fireEvent.click(screen.getByRole('button', { name: '展开分片' }))
    expect(container.textContent).toContain('s-a-e0001')

    // 返回列表 → 再次进入 → 展开态必须已复位（防「展开态挂到别的行上」）
    fireEvent.click(screen.getByRole('button', { name: '← 返回会话列表' }))
    await openFirstThread()
    expect(container.textContent).not.toContain('s-a-e0001')
  })

  // 用例 28：游程内部缺段 → 红字「缺 N 段」
  it('28 游程内部缺段显示「缺 2 段」并带 error 类', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    // seq 1,2,5 → 5-1+1-3 = 缺 2 段
    mocks.detail.mockResolvedValue(detailOf([chunk(1, 10), chunk(2, 10), chunk(5, 10)]))
    renderPage()

    await openFirstThread()

    const badge = await screen.findByText('缺 2 段')
    expect(badge.classList.contains('log-entry__count--error')).toBe(true)
  })

  // 用例 29：会话级缺失 → 顶部汇总标红
  it('29 missing_events>0 → 顶部汇总出现且带 kv-note--error', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(
      detailOf([chunk(1, 5)], { expected_events: 4, missing_events: 3 }),
    )
    renderPage()

    await openFirstThread()

    const summary = await screen.findByText(/缺失 3 条/)
    expect(summary.className).toContain('kv-note--error')
  })

  // 用例 30：口径锁（前端侧）
  it('30 未知事件类型渲染 FALLBACK，原串不上屏', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(
      detailOf([
        {
          event_id: 's-a-e0009',
          stream_id: 's-a',
          sequence: 9,
          event_type: 'mystery_event',
          record_kind: 'pushed',
          task_id: null,
          payload: { evil_text: 'SECRET_原文' },
          created_at: CREATED_AT,
        },
      ]),
    )
    const { container } = renderPage()

    await openFirstThread()

    expect(await screen.findByText('未知事件类型')).toBeInTheDocument()
    expect(container.textContent).not.toContain('mystery_event')
  })

  // 用例 30 的另一半：payload 键经映射表 → **未知键回退显示键名本身（漂移可见）**
  //
  // ⚠️ 这里测的是**正向**行为，刻意与上面的未知事件类型相反：
  // - 一个 payload 键**只可能因为它在 `PROJECTION` 白名单里才存在于表中**
  //   （写入侧投影 + DB 三条 CHECK 两道锁），所以「前端映射表不认识的键」
  //   **仍是白名单内的键**，只是还没中文标签——显示键名零风险，且是「投影白名单
  //   漂移」的唯一可见信号（风险清单第 5 条要保的东西）。
  // - **事件类型不在任何白名单里**，原串只是后端符号，故走通用 FALLBACK 不上屏。
  // 若将来有人把未命中键改成「整条跳过」，本用例立刻红。
  it('30 payload 未知键回退显示键名本身（漂移可见，不隐藏）', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(
      detailOf([
        {
          event_id: 's-a-e0009',
          stream_id: 's-a',
          sequence: 9,
          event_type: 'agent_error',
          record_kind: 'pushed',
          task_id: null,
          // long_text 用来验证「值走 displayExternal 的截断路径」（默认 500 字符上限）
          payload: { evil_text: 'SECRET_原文', ok: false, long_text: 'x'.repeat(600) },
          created_at: CREATED_AT,
        },
      ]),
    )
    const { container } = renderPage()

    await openFirstThread()
    fireEvent.click(await screen.findByRole('button', { name: '展开事件明细' }))

    // 正向锁 1：映射表没有的键，**键名本身必须上屏**（漂移可见）
    expect(container.textContent).toContain('evil_text')
    // 正向锁 2：该键的值也照常渲染（键与值同一条 entry）
    expect(await screen.findByText(/evil_text：SECRET_原文/)).toBeInTheDocument()
    // 正向锁 3：值经 displayExternal 截断路径（500 字符 + 省略号），不是原样直出
    expect(screen.getByText(/long_text：/).textContent).toBe(
      `long_text：${'x'.repeat(500)}…`,
    )
    // 布尔必须转「是 / 否」，绝不能渲染成 `false` / `1`
    expect(screen.getByText(/结果：否/)).toBeInTheDocument()
  })

  // 用例 30 补充：抑制原因枚举措辞（suppressed_reason）四个值全部映射为中文。
  // 该键不在 PROJECTION 表里（抑制路径单独注入），值域是闭集 SUPPRESSED_REASONS；
  // `unknown` 正是「投影侧收到集合外的值」的收敛点，必须可见。
  it('30c suppressed_reason 四个枚举值全部映射为中文，原串不上屏', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    const reasons = [
      'interrupted',
      'final_step:model_failed',
      'final_step:steps_exhausted',
      'unknown',
    ]
    mocks.detail.mockResolvedValue(
      detailOf(
        reasons.map((reason, index) => ({
          event_id: `s-a-x000${index + 1}`,
          stream_id: 's-a',
          sequence: 0,
          event_type: 'tool_result',
          record_kind: 'suppressed',
          task_id: null,
          payload: { suppressed_reason: reason },
          created_at: CREATED_AT,
        })),
      ),
    )
    const { container } = renderPage()

    await openFirstThread()
    const buttons = await screen.findAllByRole('button', { name: '展开事件明细' })
    const expected = [
      '抑制原因：被中断',
      '抑制原因：末轮模型失败',
      '抑制原因：末轮步数耗尽',
      '抑制原因：未知',
    ]
    expect(buttons).toHaveLength(4)
    for (let index = 0; index < expected.length; index += 1) {
      fireEvent.click(buttons[index])
      expect(await screen.findByText(expected[index])).toBeInTheDocument()
      // 展开这一行时，枚举原串（代码侧常量）不得出现在页面上
      expect(container.textContent).not.toContain(reasons[index])
    }
  })

  // 可观测性第 2 步新增三个事件类型（compression_decision / decision_input_summary /
  // args_input_summary）——EVENT_TYPE_TEXT 是白名单映射，不补标签就会全部显示成
  // 「未知事件类型」＝等于没交付。本用例只**新增**，不动用例 30 的守护期望。
  it('33 三个新事件类型渲染中文标签，不出现「未知事件类型」', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(
      detailOf([
        {
          event_id: 's-a-n0001',
          stream_id: 's-a',
          sequence: 1,
          event_type: 'compression_decision',
          record_kind: 'pushed',
          task_id: null,
          payload: {},
          created_at: CREATED_AT,
        },
        {
          event_id: 's-a-n0002',
          stream_id: 's-a',
          sequence: 2,
          event_type: 'decision_input_summary',
          record_kind: 'pushed',
          task_id: null,
          payload: {},
          created_at: CREATED_AT,
        },
        {
          event_id: 's-a-n0003',
          stream_id: 's-a',
          sequence: 3,
          event_type: 'args_input_summary',
          record_kind: 'pushed',
          task_id: null,
          payload: {},
          created_at: CREATED_AT,
        },
      ]),
    )
    const { container } = renderPage()

    await openFirstThread()

    expect(await screen.findByText('压缩判定')).toBeInTheDocument()
    expect(screen.getByText('决策轮输入摘要')).toBeInTheDocument()
    expect(screen.getByText('参数轮输入摘要')).toBeInTheDocument()
    // 三个类型都命中映射表 → 无一行走 FALLBACK；原串（后端符号）不上屏
    expect(container.textContent).not.toContain('未知事件类型')
    expect(container.textContent).not.toContain('compression_decision')
    expect(container.textContent).not.toContain('decision_input_summary')
    expect(container.textContent).not.toContain('args_input_summary')
  })

  // 用例 34：新 payload 键补了中文标签（不补也不违规，补上更好读）。
  it('34 新增 payload 键渲染中文名（压缩判定 + 决策轮输入摘要）', async () => {
    mocks.threads.mockResolvedValue(TWO_THREADS)
    mocks.detail.mockResolvedValue(
      detailOf([
        {
          event_id: 's-a-p0001',
          stream_id: 's-a',
          sequence: 1,
          event_type: 'compression_decision',
          record_kind: 'pushed',
          task_id: null,
          payload: {
            decision: 'compressed',
            occupancy_pct: 87,
            messages_count: 12,
            segment_messages: 6,
          },
          created_at: CREATED_AT,
        },
        {
          event_id: 's-a-p0002',
          stream_id: 's-a',
          sequence: 2,
          event_type: 'decision_input_summary',
          record_kind: 'pushed',
          task_id: null,
          payload: {
            history_raw: 30,
            history_sent: 24,
            evicted_details: 3,
            summaries_count: 2,
            active_skill: 'finance_order',
          },
          created_at: CREATED_AT,
        },
      ]),
    )
    const { container } = renderPage()

    await openFirstThread()
    const buttons = await screen.findAllByRole('button', { name: '展开事件明细' })
    expect(buttons).toHaveLength(2)
    // 单一展开（同一时刻只开一行）→ 逐行点开、断言、再点下一行。
    fireEvent.click(buttons[0])
    expect(screen.getByText('判定结果：compressed')).toBeInTheDocument()
    expect(screen.getByText('占用百分比：87')).toBeInTheDocument()
    expect(screen.getByText('消息条数：12')).toBeInTheDocument()
    expect(screen.getByText('待压段消息数：6')).toBeInTheDocument()

    fireEvent.click(buttons[1])
    expect(screen.getByText('历史原始条数：30')).toBeInTheDocument()
    expect(screen.getByText('历史发送条数：24')).toBeInTheDocument()
    expect(screen.getByText('驱逐明细条数：3')).toBeInTheDocument()
    expect(screen.getByText('摘要条数：2')).toBeInTheDocument()
    expect(screen.getByText('激活流程：finance_order')).toBeInTheDocument()
    // 键名本身不应再上屏（已命中映射表）
    expect(container.textContent).not.toContain('occupancy_pct')
    expect(container.textContent).not.toContain('evicted_details')
  })

  // 用例 31：列表请求失败 → 新块给出 ErrorNotice，且既有两块仍在
  it('31 时间线请求失败不挤掉 health / logs 两块', async () => {
    mocks.threads.mockRejectedValue(new ApiError(500, { error_code: 'UNKNOWN_ERROR' }))
    const { container } = renderPage()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '系统健康' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '日志环' })).toBeInTheDocument()
    // 既有依赖表仍渲染 4 行
    expect(container.querySelectorAll('table.data-table tbody tr')).toHaveLength(4)
  })

  // 用例 32：孤儿行显式计数
  it('32 untracked_events>0 → 出现「N 条事件未关联会话」', async () => {
    mocks.threads.mockResolvedValue({ ...TWO_THREADS, untracked_events: 5 })
    renderPage()

    expect(await screen.findByText('5 条事件未关联会话')).toBeInTheDocument()
  })
})
