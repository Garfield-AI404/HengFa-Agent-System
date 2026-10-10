/**
 * SSE 事件流（规格 2.7.4 L530-534；Phase 3a 对话式 harness 事件契约）。
 *
 * - 事件必须携带 event_id/event_type/task_id/sequence/payload（Zod 运行时校验，
 *   规则 10 L448；event_id 格式 {stream_id}-e{seq:04d}；task_id 可空）；
 * - 断线重连携带 Last-Event-ID 头；重复 sequence 必须丢弃；
 * - 终态事件为 agent_reply / agent_error / task_failed / task_cancelled
 *   （无 done 事件，流正常关闭即结束）；收到终态后停止并进入 done；
 *   **call_tool 过渡轮除外**：后端在决策为 call_tool 的过渡轮也会发射
 *   agent_reply，其 payload.next_action === "call_tool" 且**不是终态**（图
 *   继续执行工具，随后还有终局 agent_reply）——仅终局 reply/needs_input
 *   的 agent_reply 为终态（2026-08-26 修复：此前误判终态导致提前停轮询，
 *   真回复/侧栏刷新事件全部错过）；
 * - **挂起等待确认**：confirmation_requested 非终态——收到后进入
 *   suspended 状态，空连启发式停止（挂起期间流保持轮询，退避封顶），
 *   确认（POST /chat/confirm）追加的事件经 Last-Event-ID 续传到达；
 * - GET /streams 为一次性响应，连接关闭不代表流结束——流关闭后持续
 *   退避重连直到终态/错误/手动停止（图事件经续传到达）；
 * - [2026-08-28] 流正常关闭后的轮询续连是正常机制（模型思考空窗期反复
 *   发生）：静默重连、状态保持 streaming，不打扰用户；reconnecting 仅
 *   表示真实网络/读取故障（fetch 网络错误、无 response.body、read 异常）；
 * - 收到 SESSION_EXPIRED（L534）→ 清理 Query cache 并跳转登录（全局回调）；
 * - 401 → 全局未授权回调；stop() 调用 POST /streams/:id/cancel。
 *
 * `epoch`：同一 stream_id 上的重新订阅信号（确认恢复后立即续传用）——
 * epoch 变化会重新连接，但保留 Last-Event-ID/sequence（续传而非全量重放，
 * 避免事件重复应用）；stream_id 变化则全量重放。
 *
 * 以 fetch + ReadableStream 实现（原生 EventSource 不便注入/断言 Last-Event-ID，
 * 也不便 Vitest mock；Cookie 会话走 fetch 默认凭据）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, notifyUnauthorized } from '../api/client'
import { streamEventSchema } from '../api/schemas'
import { cancelStreamApi } from '../api/chat.api'
import { NON_TERMINAL_AGENT_ERROR_CODES } from '../pages/chat/constants'
import type { StreamEventOut } from '../api/types'

export type StreamStatus =
  | 'idle'
  | 'connecting'
  | 'streaming'
  | 'reconnecting'
  | 'done'
  | 'error'

// 与 api/client.ts 保持一致：未配置时回退同源相对路径 /api/v1（vite proxy →
// 127.0.0.1:18000，生产 nginx 同源反代）。不要写死 127.0.0.1:8000。
const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1'

const MAX_RECONNECT_BACKOFF_MS = 8000
/**
 * 去重键保留窗口（2026-08-29）：超过后按 FIFO 淘汰最旧的键。
 * 断线重连只需覆盖「上次投递之后」的事件，单轮会话的事件量远小于此值；
 * 设上限是为了让长期会话的内存占用有界。
 */
const SEEN_EVENT_WINDOW = 1000
/** 终态事件类型集合（Phase 3a：agent_reply/agent_error 为图终局事件；
 *  task_failed/task_cancelled 保留；无 done 事件，流正常关闭即结束）。
 *  注意两类非终态排除（TERMINAL 分支判断处）：
 *  - agent_reply：call_tool 过渡轮（next_action === "call_tool"）非终态；
 *  - agent_error：history_truncated 等非终局码（NON_TERMINAL_AGENT_ERROR_CODES，
 *    见 pages/chat/constants）——后端发完仍会生成终局回复，不得提前停止轮询。 */
const TERMINAL_EVENT_TYPES = new Set([
  'agent_reply',
  'agent_error',
  'task_failed',
  'task_cancelled',
])

/** 纯函数：解析一段 SSE 文本块为一个事件（可独立单测；Zod 校验五要素）。 */
export function parseSseEvent(block: string): StreamEventOut | null {
  const idLine: string[] = []
  const eventLine: string[] = []
  const dataLines: string[] = []
  for (const raw of block.split(/\r?\n/)) {
    const line = raw.trimEnd()
    if (line.startsWith('id:')) idLine.push(line.slice(3).trim())
    else if (line.startsWith('event:')) eventLine.push(line.slice(6).trim())
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null

  let data: unknown
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    return null
  }
  // 规则 10：运行时响应校验（五要素），校验失败视为损坏事件丢弃
  const parsed = streamEventSchema.safeParse(data)
  if (!parsed.success) return null
  const { event_id, event_type, task_id, sequence, payload } = parsed.data

  return {
    event_id: idLine[0] ?? event_id,
    event_type: eventLine[0] ?? event_type,
    task_id,
    sequence,
    payload,
  }
}

/**
 * 单条事件投递回调。
 *
 * [2026-08-29 重构] 此前 hook 把事件累积进 `events` 数组并暴露给消费方，
 * 消费方每次渲染都要全量遍历数组做前缀过滤 + 去重——每来一个事件 O(N)，
 * 整轮流式 O(N²)，且数组只增不减（长对话持续吃内存）。
 *
 * SSE 事件本质是「流」而不是「状态」：正确做法是 hook 内部完成去重后，
 * 每个事件只投递一次，消费方无需持有全量数组。
 */
export type StreamEventSink = (event: StreamEventOut) => void

export interface UseTaskStreamResult {
  status: StreamStatus
  error: ApiError | null
  /** 挂起等待确认：收到 confirmation_requested 且未恢复（空连启发式停止）。 */
  suspended: boolean
  /** 取消当前生成并关闭连接（幂等）。 */
  stop: () => void
}

function toApiErrorBody(body: unknown): Record<string, unknown> {
  return typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {}
}

export function useTaskStream(
  streamId: string | null,
  epoch = 0,
  onEvent?: StreamEventSink,
): UseTaskStreamResult {
  const [status, setStatus] = useState<StreamStatus>('idle')
  const [error, setError] = useState<ApiError | null>(null)
  const [suspended, setSuspended] = useState(false)

  // 事件回调存 ref：调用方无需保证引用稳定（回调身份变化不会触发重连）。
  const onEventRef = useRef<StreamEventSink | undefined>(onEvent)
  onEventRef.current = onEvent

  /** 已投递事件的去重键集合（跨重连保留；按流切换重置）。 */
  const seenEventKeysRef = useRef<Set<string>>(new Set())
  /** 去重键的 FIFO 队列：配合下方窗口上限做淘汰，避免长期会话无界增长。 */
  const seenEventKeyQueueRef = useRef<string[]>([])

  const abortRef = useRef<AbortController | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const stoppedRef = useRef(true)
  const lastEventIdRef = useRef<string | null>(null)
  const lastSequenceRef = useRef(-1)
  const backoffRef = useRef(500)
  const receivedAnyRef = useRef(false) // 本次连接是否收到新事件（空连停止判据）
  const suspendedRef = useRef(false) // 跨连接保持：挂起期间空连不得判定流结束
  const streamIdRef = useRef<string | null>(null)
  const prevStreamIdRef = useRef<string | null>(null)
  streamIdRef.current = streamId

  // [2026-08-29 react-hooks 修复] 订阅键（streamId@epoch）变化 → 渲染期间
  // 同步重置订阅状态（React 官方「adjusting state when props change」模式）。
  // 原先这些 setState 在订阅 effect 体内同步执行，触发
  // react-hooks/set-state-in-effect（级联渲染风险）；移到渲染期后 React
  // 会以重置后的状态提交，语义不变。ref 重置仍留在订阅 effect 开头
  // （effect 阶段写 ref 不受渲染纯度约束）。
  // [2026-08-29] events 数组已移除（改为 onEvent 直投），此处不再需要重置。
  const subKey = `${streamId ?? ''}@${epoch}`
  const [prevSubKey, setPrevSubKey] = useState(subKey)
  if (prevSubKey !== subKey) {
    setPrevSubKey(subKey)
    setError(null)
    setSuspended(false)
    if (streamId === null) setStatus('idle')
  }

  const cleanup = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  useEffect(() => {
    // [2026-08-29 react-hooks 修复] error/suspended/status 的重置已
    // 移到上方渲染期块（subKey 变化必伴随本 effect 重跑，语义一致）；
    // 这里只重置 ref 并建立连接。
    backoffRef.current = 500
    receivedAnyRef.current = false
    suspendedRef.current = false
    stoppedRef.current = streamId === null
    if (streamId === null) {
      return cleanup // status 已在渲染期置 idle；cleanup 关闭旧连接
    }

    // stream_id 变化 → 全量重放（Last-Event-ID 重置）；
    // 仅 epoch 变化（同一流重新订阅）→ 保留 Last-Event-ID/sequence 续传，
    // 避免已应用事件被重放导致重复渲染。
    if (prevStreamIdRef.current !== streamId) {
      lastEventIdRef.current = null
      lastSequenceRef.current = -1
      prevStreamIdRef.current = streamId
      // 新流 = 全新的事件空间：清空去重集合（不同流的 event_id 不会重复，
      // 但 sequence 从 0 重新开始，且集合需随流切换回收内存）
      seenEventKeysRef.current.clear()
      seenEventKeyQueueRef.current.length = 0
    }

    const scheduleReconnect = (silent = false): void => {
      if (stoppedRef.current) return
      // [2026-08-28] 正常轮询续连（流正常关闭后等待新事件）静默进行：
      // 不置 reconnecting，状态栏不显示「连接中断，重连中…」；仅真实
      // 故障（网络错误/读取中断）才置 reconnecting 提示用户
      if (!silent) setStatus('reconnecting')
      cleanup()
      const delay = backoffRef.current
      backoffRef.current = Math.min(backoffRef.current * 2, MAX_RECONNECT_BACKOFF_MS)
      timerRef.current = setTimeout(() => {
        void connect()
      }, delay)
    }

    const fail = (apiError: ApiError): void => {
      stoppedRef.current = true
      setStatus('error')
      setError(apiError)
    }

    const connect = async (): Promise<void> => {
      if (stoppedRef.current) return
      // [2026-08-29 react-hooks 修复] connecting 状态在 connect 顶部设置：
      // 原先在 effect 体内同步 setState，同属 set-state-in-effect；移入
      // async 函数体后与原时序一致（首个 await 之前同步执行）
      setStatus('connecting')
      const controller = new AbortController()
      abortRef.current = controller
      setError(null)
      receivedAnyRef.current = false

      const headers: Record<string, string> = { accept: 'text/event-stream' }
      if (lastEventIdRef.current) {
        // 2.7.4 L531：断线重连携带 Last-Event-ID
        headers['last-event-id'] = lastEventIdRef.current
      }

      let response: Response
      try {
        response = await fetch(
          `${API_BASE_URL}/chat/streams/${encodeURIComponent(streamId)}`,
          { headers, credentials: 'include', signal: controller.signal },
        )
      } catch {
        // 网络错误（AbortError 且已停止时静默）
        if (!stoppedRef.current && !controller.signal.aborted) scheduleReconnect()
        return
      }

      if (response.status === 401) {
        // 2.7.4 L534：SESSION_EXPIRED → 全局未授权回调（清缓存 + 匿名 + 跳登录）
        stoppedRef.current = true
        setStatus('error')
        try {
          const body: unknown = await response.json()
          setError(new ApiError(401, toApiErrorBody(body)))
        } catch {
          setError(new ApiError(401, {}))
        }
        notifyUnauthorized()
        return
      }
      if (!response.ok) {
        try {
          const body: unknown = await response.json()
          fail(new ApiError(response.status, toApiErrorBody(body)))
        } catch {
          fail(new ApiError(response.status, {}))
        }
        return
      }
      if (!response.body) {
        scheduleReconnect()
        return
      }

      setStatus('streaming')
      backoffRef.current = 500
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          const text = decoder.decode(value, { stream: true })
          buffer += text
          for (;;) {
            // 边界在每轮迭代开头重新扫描：重复 sequence 的 continue 不得跳过扫描
            const boundary = buffer.indexOf('\n\n')
            if (boundary === -1) break
            const block = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            const event = parseSseEvent(block)
            if (event) {
              const seq = event.sequence
              if (!Number.isNaN(seq)) {
                if (seq <= lastSequenceRef.current) continue // 重复 sequence 丢弃
                lastSequenceRef.current = seq
              }
              receivedAnyRef.current = true
              if (event.event_id) lastEventIdRef.current = event.event_id

              // [2026-08-29 重构] 去重后直投：替代「累积进 events 数组、由
              // 消费方全量遍历 diff」的旧路径。
              // - sequence 层：跨重连保留 lastSequenceRef，重放事件在此丢弃；
              // - event_id 层：兜住 sequence 缺失/不单调的事件，窗口上限
              //   SEEN_EVENT_WINDOW 条，FIFO 淘汰，避免长期会话无界增长。
              const dedupKey = event.event_id ?? (Number.isNaN(seq) ? null : `seq:${seq}`)
              if (dedupKey !== null) {
                if (seenEventKeysRef.current.has(dedupKey)) continue // 已投递过
                seenEventKeysRef.current.add(dedupKey)
                seenEventKeyQueueRef.current.push(dedupKey)
                if (seenEventKeyQueueRef.current.length > SEEN_EVENT_WINDOW) {
                  const evicted = seenEventKeyQueueRef.current.shift()
                  if (evicted !== undefined) seenEventKeysRef.current.delete(evicted)
                }
              }

              if (event.event_type === 'SESSION_EXPIRED') {
                // 2.7.4 L534：会话过期 → 停止并跳登录（不再重连）
                stoppedRef.current = true
                setStatus('done')
                notifyUnauthorized()
                return
              }
              if (event.event_type === 'confirmation_requested') {
                // 挂起等待确认：非终态；空连启发式停止——流保持轮询，
                // 确认（POST /chat/confirm）追加的事件经续传到达
                suspendedRef.current = true
                setSuspended(true)
              }
              // [2026-08-29 重构] 每条事件只投递一次（此前是 setEvents
              // 追加进全量数组，由消费方每次渲染全量遍历取增量）
              onEventRef.current?.(event)
              // call_tool 过渡轮（payload.next_action === "call_tool"）的
              // agent_reply 不是终态：图继续执行工具，终局 agent_reply 才
              // 结束流（2026-08-26 修复：此前误判终态导致提前停轮询，
              // 真回复/侧栏刷新事件全部错过）
              const isCallToolTransition =
                event.event_type === 'agent_reply' &&
                typeof event.payload === 'object' &&
                event.payload !== null &&
                (event.payload as Record<string, unknown>).next_action === 'call_tool'
              // [2026-09-10 单气泡收敛] 非终局 agent_error（如 history_truncated）：
              // 后端发完该事件后图仍继续、模型仍会生成终局 agent_reply —— 不得
              // 据此判定流结束（否则真回复被丢弃、本回合变空白）。按 last-event-id
              // 续传等待终局事件（agent_reply 或终局 agent_error）。
              const isNonTerminalAgentError =
                event.event_type === 'agent_error' &&
                typeof event.payload === 'object' &&
                event.payload !== null &&
                NON_TERMINAL_AGENT_ERROR_CODES.has(
                  (event.payload as Record<string, unknown>).error as string,
                )
              if (
                TERMINAL_EVENT_TYPES.has(event.event_type) &&
                !isCallToolTransition &&
                !isNonTerminalAgentError
              ) {
                stoppedRef.current = true
                suspendedRef.current = false
                setSuspended(false)
                setStatus('done')
                return
              }
            }
          }
        }
        // 流正常关闭：GET /streams 是一次性响应（读当前全部事件即关闭），
        // 连接关闭不代表流结束——后台图仍在跑，事件还会追加（模型思考
        // 空窗期可能 15-30s 无产出）。因此流关闭后总是退避重连，直到收到
        // 终态事件（TERMINAL 分支）、HTTP 错误/404（fail）或手动
        // stop()/卸载（stoppedRef 守卫）。挂起等待确认时同样保持轮询
        // （确认事件经续传到达）。
        // [2026-08-28] 正常关闭后的续连为正常轮询机制（非真实断线）：
        // 静默重连（scheduleReconnect(true)），状态保持 streaming——停止
        // 按钮/生成中状态不被打断，状态栏不显示「连接中断」；真实故障
        // 路径（fetch 网络错误、无 response.body、read 异常）才置
        // reconnecting。
        if (!stoppedRef.current) {
          scheduleReconnect(true)
        }
      } catch {
        if (!stoppedRef.current && !controller.signal.aborted) scheduleReconnect()
      }
    }

    void connect()
    return cleanup
  }, [streamId, epoch, cleanup])

  // 卸载兜底：组件卸载时停止一切重连与请求
  useEffect(() => {
    return () => {
      stoppedRef.current = true
      cleanup()
    }
  }, [cleanup])

  const stop = useCallback(() => {
    if (stoppedRef.current) return
    stoppedRef.current = true
    suspendedRef.current = false
    setSuspended(false)
    cleanup()
    const currentStreamId = streamIdRef.current
    if (currentStreamId) {
      // 取消当前生成（best effort；服务端仍独立处理）
      void cancelStreamApi(currentStreamId).catch(() => undefined)
    }
    setStatus('done')
  }, [cleanup])

  return { status, error, suspended, stop }
}
