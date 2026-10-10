/**
 * useTaskStream 单元测试（2.7.4 L530-534；D22 变更项①：真实后端事件语义）：
 * - parseSseEvent 纯函数：事件块解析（id:/event:/data: 行、非法 JSON、
 *   缺五要素校验失败丢弃——规则 10 Zod 运行时校验）；
 * - 连接：accept 头 + credentials；事件收集与终态（task_failed/task_cancelled）终止；
 * - 真实后端无 done 事件：终态事件到达或空流关闭即进入 done（不无限重连）；
 * - 重复 sequence 必须丢弃；
 * - 断线重连：携带 Last-Event-ID、指数退避；
 * - SESSION_EXPIRED：HTTP 401 与流内事件均触发 notifyUnauthorized，不重连；
 * - stop()：调用 cancelStreamApi 并关闭连接。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { parseSseEvent, useTaskStream } from '../../src/hooks/useTaskStream'
import type { StreamEventOut } from '../../src/api/types'

vi.mock('../../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/client')>()
  return { ...actual, notifyUnauthorized: vi.fn() }
})
vi.mock('../../src/api/chat.api', () => ({
  cancelStreamApi: vi.fn(async () => {}),
}))

import { notifyUnauthorized } from '../../src/api/client'
import { cancelStreamApi } from '../../src/api/chat.api'

const mockedNotify = vi.mocked(notifyUnauthorized)
const mockedCancel = vi.mocked(cancelStreamApi)

function sseResponse(blocks: string[], closeStream: boolean): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream({
    start(controller) {
      for (const block of blocks) {
        controller.enqueue(encoder.encode(block + '\n\n'))
      }
      if (closeStream) controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

function eventBlock(eventId: string, eventType: string, sequence: number, payload: unknown): string {
  return `id: ${eventId}\nevent: ${eventType}\ndata: ${JSON.stringify({
    event_id: eventId,
    event_type: eventType,
    task_id: 'task-1',
    sequence,
    payload,
  })}`
}

const fetchCalls = (): RequestInit[] =>
  vi.mocked(fetch).mock.calls.map((call) => call[1] as RequestInit)

/**
 * 渲染 hook 并收集全部投递事件。
 *
 * [2026-08-29 重构适配] hook 不再暴露 `events` 数组（SSE 是「流」而非
 * 「状态」，改为 onEvent 逐个投递，消费方无需持有全量数组）。测试侧用
 * 收集器重建数组即可——被测行为（去重、续传、终态）不受影响。
 * 注意：收集器在 renderHook 之前创建，闭包捕获同一个数组引用，
 * renderHook 重渲染不会丢失已收集的事件。
 */
function renderStream(streamId: string | null, epoch = 0) {
  const events: StreamEventOut[] = []
  const rendered = renderHook(() =>
    useTaskStream(streamId, epoch, (event) => {
      events.push(event)
    }),
  )
  return { ...rendered, events }
}

afterEach(() => {
  // 顺序：先清理（此时 fetch 仍是 stub），再还原全局
  mockedNotify.mockClear()
  mockedCancel.mockClear()
  vi.mocked(fetch).mockClear?.()
  vi.unstubAllGlobals()
})

describe('parseSseEvent 纯函数', () => {
  it('解析完整事件块（id/event/data 行）', () => {
    const block = [
      'id: stream-1-e0009',
      'event: task_intent',
      `data: ${JSON.stringify({ event_id: 'stream-1-e0009', event_type: 'task_intent', task_id: 't1', sequence: 3, payload: { policy_key: 'R2.order.create' } })}`,
    ].join('\n')

    const event = parseSseEvent(block)
    expect(event).toEqual({
      event_id: 'stream-1-e0009',
      event_type: 'task_intent',
      task_id: 't1',
      sequence: 3,
      payload: { policy_key: 'R2.order.create' },
    })
  })

  it('无 data 行 → null', () => {
    expect(parseSseEvent('id: x\nevent: y')).toBeNull()
  })

  it('data 非合法 JSON → null', () => {
    expect(parseSseEvent('data: {not json')).toBeNull()
  })

  it('data 缺五要素（如无 payload）→ null（Zod 运行时校验，规则 10）', () => {
    const block = `id: e1\nevent: task_created\ndata: ${JSON.stringify({ sequence: 1, task_id: 't' })}`
    expect(parseSseEvent(block)).toBeNull()
  })

  it('data 缺 event_type/sequence → null（校验失败即丢弃，fail closed）', () => {
    const block = `id: e2\nevent: task_created\ndata: ${JSON.stringify({ event_id: 'e2', task_id: 't', payload: {} })}`
    expect(parseSseEvent(block)).toBeNull()
  })

  it('多行 data 合并为单个 JSON；event 行优先于 data 内 event_type', () => {
    const block = [
      'id: e2',
      'event: task_queued',
      'data: {"event_type":"task_created","event_id":"e2","task_id":"t","sequence":2,',
      'data: "payload":{"status":"pending"}}',
    ].join('\n')
    const event = parseSseEvent(block)
    expect(event?.event_type).toBe('task_queued')
    expect(event?.payload).toEqual({ status: 'pending' })
    expect(event?.sequence).toBe(2)
  })
})

describe('useTaskStream 连接与事件', () => {
  it('连接后收集事件；task_failed 终态 → done（真实后端无 done 事件）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [
            eventBlock('stream-1-e0001', 'task_created', 1, { thread_id: 'th', operation_id: 'op', status: 'pending', is_write: true }),
            eventBlock('stream-1-e0002', 'task_intent', 2, { policy_key: 'R2.order.create' }),
            eventBlock('stream-1-e0003', 'model_stub', 3, { step: 'intent_to_policy', policy_key: 'R2.order.create', note: '模型调用点' }),
            eventBlock('stream-1-e0004', 'task_failed', 4, {
              final_error_code: 'MODEL_PIPELINE_NOT_CONNECTED',
              final_error_summary: '模型/执行器链路未连接',
              status: 'failed',
            }),
          ],
          true,
        ),
      ),
    )

    const { result, events } = renderStream('stream-1')

    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events.map((e) => e.event_type)).toEqual([
      'task_created',
      'task_intent',
      'model_stub',
      'task_failed',
    ])
    expect(result.current.error).toBeNull()
    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(String(url)).toContain('/chat/streams/stream-1')
    expect(init?.headers).toMatchObject({ accept: 'text/event-stream' })
    expect(init?.credentials).toBe('include')
  })

  it('task_cancelled 终态 → done', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [
            eventBlock('s-e0001', 'task_created', 1, { status: 'running', is_write: true }),
            eventBlock('s-e0002', 'task_cancelled', 2, { reason: 'generation_cancelled' }),
          ],
          true,
        ),
      ),
    )

    const { result, events } = renderStream('stream-cancel')
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events.at(-1)?.event_type).toBe('task_cancelled')
  })

  it('流内 SESSION_EXPIRED 事件 → notifyUnauthorized + done，不再重连', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [
            eventBlock('s-e0001', 'task_created', 1, { status: 'running', is_write: true }),
            eventBlock('s-e0002', 'SESSION_EXPIRED', 2, { reason: 'session_expired' }),
          ],
          true,
        ),
      ),
    )

    const { result } = renderHook(() => useTaskStream('stream-expired'))
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(mockedNotify).toHaveBeenCalledTimes(1)
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(fetchCalls()).toHaveLength(1) // 不得重连
  })

  it('task_waiting_input 非终态：流关闭后静默续连轮询，状态保持 streaming', async () => {
    // [2026-08-28 行为变更] 旧实现有一条「流正常关闭且本次连接未收到新事件
    // → 视为流已结束、停止重连」的启发式，本用例即断言该启发式。
    // 该启发式已被**有意移除**：GET /streams 是一次性响应，连接关闭不代表
    // 流结束（模型思考空窗期可能 15-30s 无产出），挂起等待用户确认时更是
    // 必然长时间无事件。现在流关闭后总是静默退避续连，直到终态事件 /
    // HTTP 错误 / 手动 stop——故此处断言新契约，而不是「停止重连」。
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          // 首次：收到 task_waiting_input（非终态）后流关闭 → 静默退避续连
          return sseResponse(
            [
              eventBlock('s-e0001', 'task_created', 1, { status: 'running', is_write: true }),
              eventBlock('s-e0002', 'task_intent', 2, { policy_key: 'R6.reconcile' }),
              eventBlock('s-e0003', 'task_waiting_input', 3, { pending_questions: ['请确认订单类型'] }),
            ],
            true,
          )
        }
        // 后续：用户尚未确认，服务端无新事件（Last-Event-ID 续传空结果）
        // → 关闭 → 继续退避轮询，等待确认后追加的事件
        return sseResponse([], true)
      }),
    )

    const { result, events } = renderStream('stream-waiting')
    await waitFor(() => expect(events).toHaveLength(3))
    await waitFor(() => expect(fetchCalls().length).toBeGreaterThanOrEqual(2))
    // 第二次请求携带 Last-Event-ID（L531）
    expect(fetchCalls()[1]?.headers).toMatchObject({ 'last-event-id': 's-e0003' })
    // 关键：waiting_input 非终态 → 不进入 done，保持 streaming 持续轮询
    expect(result.current.status).toBe('streaming')
    // 轮询确实在继续（每次成功连接后退避重置为 500ms，4s 内应有第 3 次请求）
    await waitFor(() => expect(fetchCalls().length).toBeGreaterThanOrEqual(3), {
      timeout: 4000,
    })
    // 续连为静默轮询：不置 reconnecting（该状态只表示真实网络/读取故障）
    expect(result.current.status).not.toBe('reconnecting')
  })

  it('[2026-09-10] agent_error(history_truncated) 非终态：不停止轮询，续传等待模型回复', async () => {
    // model_node 在历史截断时发 history_truncated，随后同轮仍会生成终局
    // agent_reply —— 不得据此判定流结束（否则真回复被丢弃、本回合空白）。
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          return sseResponse(
            [
              eventBlock('s-e0001', 'agent_reply_start', 1, {}),
              eventBlock('s-e0002', 'agent_error', 2, { error: 'history_truncated' }),
            ],
            true,
          )
        }
        // 后续续连：模型终局回复到达
        return sseResponse(
          [eventBlock('s-e0003', 'agent_reply', 3, { reply_text: '这是本轮回答。' })],
          true,
        )
      }),
    )

    const { result, events } = renderStream('stream-trunc')
    // 收到 history_truncated 后不得进入 done，续连后收到终局 agent_reply 才 done
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events.map((e) => e.event_type)).toEqual([
      'agent_reply_start',
      'agent_error',
      'agent_reply',
    ])
    expect(fetchCalls().length).toBeGreaterThanOrEqual(2)
  })

  it('[2026-09-10] agent_error(model_unavailable) 终态：停止轮询并置 done', async () => {
    // 除 history_truncated 外的 agent_error 码均为终局：收到即停止，不得续连
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [
            eventBlock('s-e0001', 'agent_reply_start', 1, {}),
            eventBlock('s-e0002', 'agent_error', 2, { error: 'model_unavailable' }),
          ],
          true,
        ),
      ),
    )

    const { result, events } = renderStream('stream-model-down')
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events.at(-1)?.event_type).toBe('agent_error')
    expect(fetchCalls()).toHaveLength(1) // 不得续连
  })

  it('[2026-09-10] agent_error(chat_run_failed) 图外安全网：终态停止轮询', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse([eventBlock('s-e0001', 'agent_error', 1, { error: 'chat_run_failed' })], true),
      ),
    )

    const { result } = renderStream('stream-run-failed')
    await waitFor(() => expect(result.current.status).toBe('done'))
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(fetchCalls()).toHaveLength(1) // 不得续连
  })

  it('[2026-09-10] history_truncated 之后模型失败（终局 agent_error）：流仍能停止', async () => {
    // 非终局 history_truncated 不停止轮询；随后终局 agent_error 到达必须收敛，
    // 不得悬停/无限轮询
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          return sseResponse(
            [
              eventBlock('s-e0001', 'agent_reply_start', 1, {}),
              eventBlock('s-e0002', 'agent_error', 2, { error: 'history_truncated' }),
            ],
            true,
          )
        }
        return sseResponse(
          [eventBlock('s-e0003', 'agent_error', 3, { error: 'model_unavailable' })],
          true,
        )
      }),
    )

    const { result, events } = renderStream('stream-trunc-then-fail')
    await waitFor(() => expect(result.current.status).toBe('done'), { timeout: 4000 })
    expect(events.map((e) => e.event_type)).toEqual([
      'agent_reply_start',
      'agent_error',
      'agent_error',
    ])
    // 终态后不再续连
    const callsAfterDone = fetchCalls().length
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(fetchCalls()).toHaveLength(callsAfterDone)
  })

  it('[2026-09-28] agent_error(preparation_error) 非终局：不停止轮询，续传等模型解释回复', async () => {
    // prepare 失败（无卡）后图继续到 model 生成解释性 agent_reply —— 不得据此
    // 判定流结束（否则模型那条解释回复被丢弃）。
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          return sseResponse(
            [
              eventBlock('s-e0001', 'agent_reply_start', 1, {}),
              eventBlock('s-e0002', 'agent_error', 2, { error: 'preparation_error', detail: '同名员工' }),
            ],
            true,
          )
        }
        return sseResponse(
          [eventBlock('s-e0003', 'agent_reply', 3, { reply_text: '未生成确认卡。' })],
          true,
        )
      }),
    )

    const { result, events } = renderStream('stream-prep')
    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events.map((e) => e.event_type)).toEqual([
      'agent_reply_start',
      'agent_error',
      'agent_reply',
    ])
    expect(fetchCalls().length).toBeGreaterThanOrEqual(2)
  })

  it('[2026-09-28] preparation_error 之后模型失败（终局 agent_error）：流仍能收敛', async () => {
    // 非终局不无限等：后续终局 agent_error 到达必须停止轮询
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          return sseResponse(
            [
              eventBlock('s-e0001', 'agent_reply_start', 1, {}),
              eventBlock('s-e0002', 'agent_error', 2, { error: 'preparation_error' }),
            ],
            true,
          )
        }
        return sseResponse(
          [eventBlock('s-e0003', 'agent_error', 3, { error: 'model_unavailable' })],
          true,
        )
      }),
    )

    const { result, events } = renderStream('stream-prep-then-fail')
    await waitFor(() => expect(result.current.status).toBe('done'), { timeout: 4000 })
    expect(events.map((e) => e.event_type)).toEqual(['agent_reply_start', 'agent_error', 'agent_error'])
    const callsAfterDone = fetchCalls().length
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(fetchCalls()).toHaveLength(callsAfterDone)
  })

  it('重复 sequence 的事件必须丢弃', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [
            eventBlock('s-e0001', 'task_created', 1, { status: 'pending', is_write: true }),
            eventBlock('s-e0001b', 'task_created', 1, { status: 'pending', is_write: true }),
            eventBlock('s-e0002', 'task_intent', 2, { policy_key: 'R3' }),
            eventBlock('s-e0003', 'task_failed', 3, { final_error_code: 'X', status: 'failed' }),
          ],
          true,
        ),
      ),
    )

    const { result, events } = renderStream('stream-dup')

    await waitFor(() => expect(result.current.status).toBe('done'))
    expect(events).toHaveLength(3) // created/intent/failed，e0001b 被丢弃
    expect(events[0].event_id).toBe('s-e0001')
  })

  it('同一 chunk 内重复 sequence 后的事件不得被破坏（回归：continue 跳过边界重扫）', async () => {
    // 真实网络下多个事件块常在同一 chunk 到达。此前 continue 会跳过
    // boundary 重新扫描，导致下一个块用过期边界被切开而丢失。
    const encoder = new TextEncoder()
    const oneChunk =
      [
        eventBlock('s-e0001', 'task_created', 1, { status: 'running', is_write: true }),
        eventBlock('s-e0001b', 'task_created', 1, { status: 'running', is_write: true }),
        eventBlock('s-e0002', 'task_intent', 2, { policy_key: 'R2.order.create' }),
        eventBlock('s-e0003', 'task_waiting_input', 3, { pending_questions: ['请确认订单类型：钣金还是塑粉？'] }),
      ].join('\n\n') + '\n\n'
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(encoder.encode(oneChunk))
                controller.close()
              },
            }),
            { status: 200, headers: { 'content-type': 'text/event-stream' } },
          ),
      ),
    )

    const { events } = renderStream('stream-dup-single-chunk')

    // 末位是 task_waiting_input（非终态），流关闭后会静默续连 → 不等待 done，
    // 改为等待三个有效事件投递完成（本用例关注点是 chunk 边界解析，不是终态）
    await waitFor(() => expect(events).toHaveLength(3))
    expect(events.map((e) => e.event_type)).toEqual([
      'task_created',
      'task_intent',
      'task_waiting_input',
    ])
    // 重复之后的 task_waiting_input 必须完整保留（payload 未被边界切开）
    expect(events[2].payload).toEqual({
      pending_questions: ['请确认订单类型：钣金还是塑粉？'],
    })
  })

  it('断线后重连：携带 Last-Event-ID，退避后恢复', async () => {
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        call += 1
        if (call === 1) {
          // 首次连接：收到事件后流被服务端关闭（无 done 事件）→ 触发退避重连
          return sseResponse([eventBlock('s-e0005', 'task_created', 5, { status: 'running', is_write: true })], true)
        }
        // 第二次连接：续传剩余事件至终态
        return sseResponse(
          [
            eventBlock('s-e0006', 'task_intent', 6, { policy_key: 'R4' }),
            eventBlock('s-e0007', 'task_failed', 7, { final_error_code: 'MODEL_PIPELINE_NOT_CONNECTED', status: 'failed' }),
          ],
          true,
        )
      }),
    )

    const { result, events } = renderStream('stream-reconnect')

    await waitFor(() => expect(result.current.status).toBe('done'), { timeout: 4000 })
    expect(fetchCalls().length).toBeGreaterThanOrEqual(2)
    // Last-Event-ID 头必须携带最后收到的 event_id（2.7.4 L531）
    const reconnectInit = fetchCalls()[1]
    expect(reconnectInit?.headers).toMatchObject({ 'last-event-id': 's-e0005' })
    expect(events.map((e) => e.event_id)).toEqual(['s-e0005', 's-e0006', 's-e0007'])
  })

  it('401（SESSION_EXPIRED）→ 触发全局未授权回调，不重连', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({ error_code: 'SESSION_EXPIRED', message: '登录已失效' }),
          { status: 401, headers: { 'content-type': 'application/json' } },
        ),
      ),
    )

    const { result } = renderHook(() => useTaskStream('stream-401'))

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.error?.errorCode).toBe('SESSION_EXPIRED')
    expect(mockedNotify).toHaveBeenCalledTimes(1)
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(fetchCalls()).toHaveLength(1) // 401 后不得重连
  })

  it('stop()：调用 cancelStreamApi 并置 done', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sseResponse(
          [eventBlock('s-e0001', 'task_intent', 1, { policy_key: 'R2' })],
          false,
        ),
      ),
    )

    const { result } = renderHook(() => useTaskStream('stream-stop'))
    await waitFor(() => expect(result.current.status).toBe('streaming'))

    act(() => {
      result.current.stop()
    })
    expect(result.current.status).toBe('done')
    expect(mockedCancel).toHaveBeenCalledWith('stream-stop')
  })

  it('streamId 为 null 时不发起任何请求', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useTaskStream(null))
    expect(result.current.status).toBe('idle')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
