/**
 * ChatPage 编排层测试（2026-08-29 重写）。
 *
 * ## 为什么重写
 * 原文件 import 了已随 D26 删除的 `src/api/tasks.api`，收集阶段即失败，
 * 是死测试。原测试还依赖 D25 的同步 chat 契约（`{task_id, stream_id}`）与
 * 已移除的规则化意图分类（`intent` 字段），两者都已不存在。
 *
 * ## 重写后的职责边界
 * 事件 → 状态的映射已抽为纯函数（tests/unit/chat/reducer.test.ts），
 * 消息气泡与输入区的渲染/交互也已各自抽出组件并单测。因此本文件**不再
 * mock 整条 SSE 去验证事件映射**，只测 ChatPage 独有的编排职责：
 *
 * - 提交 → 乐观气泡 → 订阅流 → 终态后输入框恢复；
 * - 发送失败（含优雅停机 503）→ 乐观气泡回滚 + 重试入口；
 * - 打开会话 → checkpoint 历史重放（tool 角色不展示）；
 * - 新建对话 → 清空消息与 thread 持久化；
 * - 流订阅 / 挂起确认卡期间的禁用门禁。
 *
 * ## 两个容易踩空的测试环境事实
 * 1. `rerender` 必须传入**新建**的 element：React 对引用相同的 element 会
 *    直接跳过重渲染，effect 不重跑，用「改 mock 状态 → rerender 驱动终态」
 *    的写法会静默失效。
 * 2. jsdom 不实现 `Element.prototype.scrollTo`，已在 tests/setup.ts 补空实现。
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../src/api/client'
import { ATTACHMENT_MAX_BYTES } from '../../src/api/chat.api'
import { chatMessageSchema } from '../../src/api/schemas'
import { ERROR_CODES } from '../../src/api/types'
import type {
  ChatHistoryOut,
  ChatMessageResponse,
  ConfirmationCard,
  PendingConfirmation,
  StreamEventOut,
} from '../../src/api/types'
import type { StreamEventSink, StreamStatus } from '../../src/hooks/useTaskStream'
import { ChatPage } from '../../src/pages/ChatPage'

const h = vi.hoisted(() => ({
  stream: { status: 'idle' as StreamStatus, error: null as ApiError | null, suspended: false },
  stop: vi.fn(),
  sink: null as StreamEventSink | null,
  seen: { streamId: null as string | null, epoch: 0 },
  send: vi.fn(),
  history: vi.fn(),
  confirm: vi.fn(),
  notify: vi.fn(),
}))

vi.mock('../../src/hooks/useTaskStream', () => ({
  useTaskStream: (streamId: string | null, epoch = 0, onEvent?: StreamEventSink) => {
    h.seen.streamId = streamId
    h.seen.epoch = epoch
    h.sink = onEvent ?? null
    return { status: h.stream.status, error: h.stream.error, suspended: h.stream.suspended, stop: h.stop }
  },
}))

vi.mock('../../src/api/chat.api', async (importOriginal) => {
  // [2026-09-11 消除漂移] 常量（THREAD_STORAGE_KEY / ATTACHMENT_ONLY_MESSAGE /
  // ATTACHMENT_MAX_* / exportDownloadUrl 等）一律取自真实模块，只覆盖需要 mock
  // 的函数。此前把 ATTACHMENT_ONLY_MESSAGE 写成字面量副本，真值改文案时 mock
  // 不跟着变、断言静默测一个早已不存在的字符串——importActual 从根上消除漂移。
  const actual = await importOriginal<typeof import('../../src/api/chat.api')>()
  return {
    ...actual,
    sendChatMessage: h.send,
    fetchChatHistoryApi: h.history,
    confirmChatApi: h.confirm,
    notifyThreadsChanged: h.notify,
    cancelStreamApi: vi.fn(async () => undefined),
    listThreadsApi: vi.fn(async () => ({ threads: [] })),
    deleteThreadApi: vi.fn(async () => undefined),
  }
})

const CHAT_ROUTE = '/chat'

/**
 * 渲染整页。返回的 `rerenderPage` 每次**新建** element——见文件头第 1 条
 * 环境说明，复用同一个 element 对象会触发 React 的引用相等跳过优化，
 * effect 不重跑，用 rerender 驱动终态的用例会假失败。
 */
function renderChatPage(state: Record<string, unknown> | null = null) {
  const build = () => (
    <MemoryRouter initialEntries={[{ pathname: CHAT_ROUTE, state }]}>
      <ChatPage />
    </MemoryRouter>
  )
  const utils = render(build())
  return { ...utils, rerenderPage: () => utils.rerender(build()) }
}

const okSend = (over: Partial<ChatMessageResponse> = {}): ChatMessageResponse => ({
  thread_id: 'th-1',
  stream_id: 'st-1',
  task_id: null,
  ...over,
})

const emptyHistory = (over: Partial<ChatHistoryOut> = {}): ChatHistoryOut => ({
  messages: [],
  pending_confirmation: null,
  stream_id: null,
  ...over,
})

/** 确认卡六要素（ConfirmationPanel 全部读取，缺字段会在截断时抛错）。 */
const testCard = (over: Partial<ConfirmationCard> = {}): ConfirmationCard => ({
  operation_type: '创建钣金订单',
  target: '客户 A / 2026-08',
  fields_to_write: { customer: '客户 A', amount: 1200 },
  auto_filled: [],
  expected_impact: '新增 1 条订单记录',
  limitations: [],
  payload_hash: 'hash-1',
  ...over,
})

const testPending = (over: Partial<PendingConfirmation> = {}): PendingConfirmation => ({
  task_id: 'T-1',
  tool_name: 'order_create',
  card: testCard(),
  payload_hash: 'hash-1',
  ...over,
})

function ev(
  eventType: string,
  payload: Record<string, unknown> = {},
  taskId: string | null = 'T-1',
): StreamEventOut {
  return {
    event_id: `e-${eventType}`,
    event_type: eventType as StreamEventOut['event_type'],
    task_id: taskId,
    sequence: 1,
    payload,
  }
}

async function submit(text: string): Promise<void> {
  fireEvent.change(screen.getByLabelText('输入指令'), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(h.send).toHaveBeenCalled())
}

/**
 * [2026-09-09 布局重构] 完整跑完一轮问答并解锁输入框：提交（乐观 user 消息
 * push）→ 注入终态 agent_reply（assistant 消息落定）→ 流置 done + rerender
 * （解除 streamId 门禁）。供多轮 / 历史收拢视图用例复用。
 */
async function completeRound(
  rerenderPage: () => void,
  text: string,
  reply?: string,
): Promise<void> {
  await submit(text)
  if (reply) {
    act(() => {
      h.sink?.(ev('agent_reply', { reply_text: reply }, 'T-1'))
    })
  }
  h.stream.status = 'done'
  act(() => {
    rerenderPage()
  })
  await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
}

/**
 * 消息气泡的文本集合。不用 getByText：textarea 的 value 在 jsdom 里同样算
 * text content，「查询某条消息是否存在」会同时命中输入框，导致误判。
 */
function bubbleTexts(): string[] {
  return Array.from(document.querySelectorAll('.chat-message__content')).map(
    (el) => el.textContent ?? '',
  )
}

beforeEach(() => {
  localStorage.clear()
  h.stream.status = 'idle'
  h.stream.error = null
  h.stream.suspended = false
  h.seen.streamId = null
  h.sink = null
  h.send.mockReset()
  h.history.mockReset()
  h.confirm.mockReset()
  h.notify.mockReset()
  h.stop.mockClear()
  h.send.mockResolvedValue(okSend())
  h.history.mockResolvedValue(emptyHistory())
})

afterEach(() => {
  localStorage.clear()
})

describe('ChatPage 提交与流订阅', () => {
  it('提交成功：当前问题条立即置顶显示（乐观渲染），输入框清空，并订阅 stream_id', async () => {
    renderChatPage()
    // [2026-09-09 布局重构] 乐观渲染的落点从「消息流 user 气泡」变为顶部
    // 「当前问题」条（最新一问常驻置顶）：发送即同步可见，不等后端响应。
    // 模型回答未到前本轮滚动区为空，用户内容不在消息行重复渲染。
    fireEvent.change(screen.getByLabelText('输入指令'), { target: { value: '对账 8 月订单' } })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByLabelText('当前问题')).toHaveTextContent('对账 8 月订单')
    expect(bubbleTexts()).toHaveLength(0)

    await waitFor(() => expect(h.seen.streamId).toBe('st-1'))
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toHaveValue(''))
  })

  it('首次提交：服务端生成的 thread_id 写入 localStorage', async () => {
    h.send.mockResolvedValue(okSend({ thread_id: 'thread-manager-abc12345' }))
    renderChatPage()
    await submit('你好')
    await waitFor(() =>
      expect(localStorage.getItem('hengfa.chat.thread')).toBe('thread-manager-abc12345'),
    )
  })

  it('提交携带已持久化的 thread_id（续接同一会话）', async () => {
    localStorage.setItem('hengfa.chat.thread', 'th-existing')
    renderChatPage()
    await submit('继续')
    await waitFor(() => expect(h.send).toHaveBeenCalledWith('继续', null, 'th-existing'))
  })

  it('流订阅期间输入区禁用；流结束（done）后恢复', async () => {
    h.stream.status = 'streaming'
    const { rerenderPage } = renderChatPage()
    await submit('生成中')

    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeDisabled())

    h.stream.status = 'done'
    act(() => {
      rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
  })

  it('流订阅中发送位换成「暂停」钮，点击调用 stream.stop()', async () => {
    h.stream.status = 'streaming'
    renderChatPage()
    await submit('生成中')

    const stopBtn = await screen.findByRole('button', { name: '暂停（停止当前处理）' })
    fireEvent.click(stopBtn)
    expect(h.stop).toHaveBeenCalledTimes(1)
  })

  // [2026-09-13 P0 修复] 客户端侧流终止（暂停 / 非 2xx）此前不投递终局事件，
  // agent_reply_start 开出的流式气泡会永久转圈。落点补发 stream_aborted 后必须
  // 停动效且有可见文案，且不留空白行。
  it('[2026-09-13 P0] 点暂停至终态：气泡停动效、显示取消文案（不再永久转圈）', async () => {
    h.stream.status = 'streaming'
    const { rerenderPage } = renderChatPage()
    await submit('生成中')
    // 模型生成开始：agent_reply_start 开出流式气泡（整轮只显示动效，无文本）
    act(() => {
      h.sink?.(ev('agent_reply_start'))
    })
    expect(document.querySelector('.chat-message__typing')).not.toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: '暂停（停止当前处理）' }))
    // 真实 hook 的 stop() 置 status='done'；mock 手动推进驱动落点
    h.stream.status = 'done'
    act(() => {
      rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
    // 动效消失 + 可见取消文案（无空白气泡行）
    expect(document.querySelector('.chat-message__typing')).toBeNull()
    expect(bubbleTexts()).toEqual(['已取消当前生成。'])
  })

  it('[2026-09-13 P0] 非 2xx 流错误终态：气泡停动效、显示请求失败文案', async () => {
    h.stream.status = 'streaming'
    const { rerenderPage } = renderChatPage()
    await submit('生成中')
    act(() => {
      h.sink?.(ev('agent_reply_start'))
    })
    expect(document.querySelector('.chat-message__typing')).not.toBeNull()

    h.stream.status = 'error'
    act(() => {
      rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
    expect(document.querySelector('.chat-message__typing')).toBeNull()
    expect(bubbleTexts()).toEqual(['对话处理失败，请稍后重试。'])
  })

  it('[2026-09-13 P0] 正常终局后（气泡已落定）的收尾无害：不改写最终答复', async () => {
    h.stream.status = 'streaming'
    const { rerenderPage } = renderChatPage()
    await submit('正常问题')
    act(() => {
      h.sink?.(ev('agent_reply_start'))
      h.sink?.(ev('agent_reply', { reply_text: '这是最终答复。' }, 'T-1'))
    })
    h.stream.status = 'done'
    act(() => {
      rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
    // 最终答复原样保留，未被 stream_aborted 误删/覆盖
    expect(bubbleTexts()).toEqual(['这是最终答复。'])
  })
})

describe('ChatPage 发送失败', () => {
  it('通用失败：移除乐观气泡，未订阅任何流', async () => {
    h.send.mockRejectedValue(new ApiError(500, { error_code: 'INTERNAL_ERROR' }))
    renderChatPage()
    await submit('会失败的消息')

    // [2026-09-09 布局重构] 失败回滚同时作用于当前问题条（问题条随消息
    // 移除消失，不残留"没发出"的问题）
    await waitFor(() => expect(screen.queryByLabelText('当前问题')).toBeNull())
    expect(bubbleTexts()).not.toContain('会失败的消息')
    expect(h.seen.streamId).toBeNull()
    // 输入内容保留，用户可直接改后重发
    expect(screen.getByLabelText('输入指令')).toHaveValue('会失败的消息')
  })

  it('优雅停机 503：给出重试入口且保留用户输入', async () => {
    h.send.mockRejectedValue(new ApiError(503, { error_code: ERROR_CODES.SERVICE_UNAVAILABLE }))
    renderChatPage()
    await submit('停机时发送')

    // 不按 role=alert 定位：ErrorNotice 与重试区块都是 alert，会多个匹配。
    // 从「重试发送」按钮反查所属区块，锁定的就是 503 专属引导。
    const retry = await screen.findByRole('button', { name: '重试发送' })
    expect(retry.closest('[role="alert"]')).toHaveTextContent('系统正在优雅停机，暂不接受新写任务')
    expect(screen.getByLabelText('输入指令')).toHaveValue('停机时发送')
  })

  it('空内容与无附件时不发起提交', () => {
    renderChatPage()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(h.send).not.toHaveBeenCalled()
  })

  it('仅挂附件不打字：允许发送，乐观气泡用兜底文案（2026-09-10 缺陷回归）', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'order.xlsx', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))

    // 乐观气泡（当前问题条）与发给后端的 message 同为兜底文案；空内容仍透传
    // 给 sendChatMessage（兜底在 api 层完成，UI 只看得到结果）
    expect(screen.getByLabelText('当前问题')).toHaveTextContent('（上传 Excel 文件）')
    await waitFor(() => expect(h.send).toHaveBeenCalledWith('', file, null))
  })

  it('提交异常为 ZodError：归为 VALIDATION_ERROR 并透出真实原因（不再谎报网络错误，2026-09-11）', async () => {
    // 复现 2026-09-10 事故同类路径：schema 校验失败此前被归一为 NETWORK_ERROR，
    // 界面谎报「无法连接服务器」。修复后应归 VALIDATION_ERROR 并透出 zod message。
    let zodError: unknown
    try {
      chatMessageSchema.parse('') // min(1) → '请输入内容'
    } catch (err) {
      zodError = err
    }
    h.send.mockRejectedValueOnce(zodError)
    renderChatPage()
    await submit('触发校验失败')

    expect(await screen.findByText('请输入内容')).toBeInTheDocument()
    expect(screen.queryByText('无法连接服务器，请检查网络后重试。')).toBeNull()
  })

  it('选择超限附件：当场拒绝并提示、不保留该文件（2026-09-11 体积预校验）', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const big = new File(['x'], 'big.xlsx')
    Object.defineProperty(big, 'size', { value: ATTACHMENT_MAX_BYTES + 1 })
    fireEvent.change(fileInput, { target: { files: [big] } })

    // 选择当下即提示；文件未保留（不显示文件名）；无内容无附件 → 发送禁用
    expect(await screen.findByText(/文件过大/)).toBeInTheDocument()
    expect(screen.queryByText('big.xlsx')).toBeNull()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('超限提示后改选合法附件：旧提示清除、文件名展示（2026-09-11）', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const big = new File(['x'], 'big.xlsx')
    Object.defineProperty(big, 'size', { value: ATTACHMENT_MAX_BYTES + 1 })
    fireEvent.change(fileInput, { target: { files: [big] } })
    expect(await screen.findByText(/文件过大/)).toBeInTheDocument()

    const ok = new File(['x'], 'order.xlsx', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [ok] } })

    await waitFor(() => expect(screen.queryByText(/文件过大/)).toBeNull())
    expect(screen.getByText('order.xlsx')).toBeInTheDocument()
  })

  it('超限选择：保留此前已挂的合法附件，不清空（2026-09-11 静默丢附件修复）', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const ok = new File(['x'], 'order.xlsx', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [ok] } })
    expect(screen.getByText('order.xlsx')).toBeInTheDocument()

    // 误点一个超限文件：提示出现，但**旧附件仍在**（不静默丢数据）
    const big = new File(['x'], 'big.xlsx')
    Object.defineProperty(big, 'size', { value: ATTACHMENT_MAX_BYTES + 1 })
    fireEvent.change(fileInput, { target: { files: [big] } })

    expect(await screen.findByText(/文件过大/)).toBeInTheDocument()
    expect(screen.getByText('order.xlsx')).toBeInTheDocument()
    expect(screen.queryByText('big.xlsx')).toBeNull()
    // 有合法附件 → 仍可发送
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('附件操作不误清无关的 VALIDATION_ERROR（2026-09-11 清除条件收窄）', async () => {
    // 先制造一个与附件无关的 zod 校验失败（提交被拒）
    let zodError: unknown
    try {
      chatMessageSchema.parse('')
    } catch (err) {
      zodError = err
    }
    h.send.mockRejectedValueOnce(zodError)
    renderChatPage()
    await submit('触发校验失败')
    expect(await screen.findByText('请输入内容')).toBeInTheDocument()

    // 随手选一个合法附件：不得把提交校验错误静默清掉
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const ok = new File(['x'], 'order.xlsx', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [ok] } })

    expect(screen.getByText('请输入内容')).toBeInTheDocument()
  })

  // [2026-09-11 上传链路收口] accept 只是选择器过滤，用户可在「所有文件」里强选
  // 任意扩展名——选择当下即拦，与后端 ALLOWED_EXTENSIONS（仅 .xlsx）一致。
  it('选择非 .xlsx 附件（如 .csv）：当场拒绝并提示、不保留该文件', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const csv = new File(['a,b'], 'orders.csv', { type: 'text/csv' })
    fireEvent.change(fileInput, { target: { files: [csv] } })

    expect(await screen.findByText(/仅支持 \.xlsx/)).toBeInTheDocument()
    expect(screen.queryByText('orders.csv')).toBeNull()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('扩展名不符被拦后改选 .xlsx：提示清除、文件名展示（2026-09-11）', async () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const xls = new File(['x'], 'legacy.xls', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [xls] } })
    expect(await screen.findByText(/仅支持 \.xlsx/)).toBeInTheDocument()

    const ok = new File(['x'], 'order.xlsx', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [ok] } })

    await waitFor(() => expect(screen.queryByText(/仅支持 \.xlsx/)).toBeNull())
    expect(screen.getByText('order.xlsx')).toBeInTheDocument()
  })

  it('大写 .XLSX 扩展名按小写匹配放行（与后端口径一致）', () => {
    renderChatPage()
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const upper = new File(['x'], 'ORDER.XLSX', { type: 'application/vnd.ms-excel' })
    fireEvent.change(fileInput, { target: { files: [upper] } })

    expect(screen.getByText('ORDER.XLSX')).toBeInTheDocument()
  })
})

describe('ChatPage 历史重放与会话切换', () => {
  it('打开会话：重放 human/ai 消息，tool 角色不展示', async () => {
    h.history.mockResolvedValue(
      emptyHistory({
        messages: [
          { role: 'human', content: '创建订单' },
          { role: 'ai', content: '已创建完成' },
          { role: 'tool', content: '{"ok":true,"order_id":"SO-1"}' },
        ],
      }),
    )
    renderChatPage({ openThread: 'th-9' })

    expect(await screen.findByText('创建订单')).toBeInTheDocument()
    expect(screen.getByText('已创建完成')).toBeInTheDocument()
    expect(screen.queryByText('{"ok":true,"order_id":"SO-1"}')).toBeNull()
    expect(h.history).toHaveBeenCalledWith('th-9')
  })

  it('重放带挂起确认卡时展示确认卡', async () => {
    h.history.mockResolvedValue(emptyHistory({ pending_confirmation: testPending() }))
    renderChatPage({ openThread: 'th-9' })

    // [2026-09-10 确认卡改版] operation_type 不再渲染，改用「目标」验证卡片可见
    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())
  })

  it('新建对话：清空消息并移除 thread 持久化', async () => {
    localStorage.setItem('hengfa.chat.thread', 'th-old')
    h.history.mockResolvedValue(
      emptyHistory({ messages: [{ role: 'human', content: '旧会话消息' }] }),
    )
    renderChatPage({ newChat: Date.now() })

    await waitFor(() => expect(localStorage.getItem('hengfa.chat.thread')).toBeNull())
    // 挂载瞬间 threadId 取自 localStorage，历史 effect 会带着旧 thread_id 发
    // 出一次请求；但 newChat effect 在其之前执行、抢先把 threadId 置 null，
    // 使该请求进入下一次 effect 时被 cancelled 丢弃。这是既有竞态处理：
    // 请求会发出，但结果不会回流污染新会话。
    await waitFor(() => expect(bubbleTexts()).not.toContain('旧会话消息'))
  })

  it('同一 thread 只重放一次（线程内后续渲染不再拉取）', async () => {
    renderChatPage({ openThread: 'th-9' })
    await waitFor(() => expect(h.history).toHaveBeenCalledTimes(1))
    await submit('新消息')
    await waitFor(() => expect(h.seen.streamId).toBe('st-1'))
    expect(h.history).toHaveBeenCalledTimes(1)
  })
})

describe('ChatPage 问题条与历史收拢视图（2026-09-09 布局重构）', () => {
  it('问题条：首问前不渲染；最新一问常驻、随新问题自动替换', async () => {
    const { rerenderPage } = renderChatPage()
    expect(screen.queryByLabelText('当前问题')).toBeNull()

    await completeRound(rerenderPage, '第一问', '回答一')
    expect(screen.getByLabelText('当前问题')).toHaveTextContent('第一问')
    // 仅一轮时无更早历史：入口不显示（N=0）
    expect(screen.queryByRole('button', { name: /查看更早对话/ })).toBeNull()

    await completeRound(rerenderPage, '第二问', '回答二')
    const question = screen.getByLabelText('当前问题')
    expect(question).toHaveTextContent('第二问')
    expect(question).not.toHaveTextContent('第一问')
  })

  it('历史收拢：入口按更早轮数显示；历史视图渲染更早问答 + 返回条，返回后本轮恢复', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    await completeRound(rerenderPage, '第二问', '回答二')

    // 本轮优先：滚动区只渲染本轮回答（最新一问不在消息行，在顶部问题条）
    expect(screen.getByLabelText('当前问题')).toHaveTextContent('第二问')
    expect(bubbleTexts()).toEqual(['回答二'])

    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    // 历史视图：同一滚动容器渲染更早问答 + 顶部返回条；本轮内容不进本视图
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()
    expect(screen.getByText('第一问')).toBeInTheDocument()
    expect(screen.getByText('回答一')).toBeInTheDocument()
    expect(screen.queryByText('回答二')).toBeNull()

    // 返回本轮：本轮回答恢复、历史入口重现
    fireEvent.click(screen.getByRole('button', { name: '返回当前对话' }))
    expect(bubbleTexts()).toEqual(['回答二'])
    expect(screen.getByRole('button', { name: '查看更早对话（1 轮）' })).toBeInTheDocument()
  })

  it('历史视图中的新一轮发送：强制切回本轮视图', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    await completeRound(rerenderPage, '第二问', '回答二')
    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()

    // 不返回，直接在历史视图发起新一轮 → 最新一问变化强制回本轮
    await submit('第三问')
    await waitFor(() => expect(screen.getByLabelText('当前问题')).toHaveTextContent('第三问'))
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()
    // 更早轮数随消息增长为 2
    expect(screen.getByRole('button', { name: '查看更早对话（2 轮）' })).toBeInTheDocument()
    // 本轮回答未到：滚动区为空
    expect(bubbleTexts()).toHaveLength(0)
  })

  it('历史视图期间的本轮流式回复：不进入历史视图，返回后可见', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    // 第二轮提交后**保持流中**（busy 只禁输入区，不禁历史入口）即切历史
    await submit('第二问')
    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()

    // 终态回复到达：历史视图不受本轮增量打扰（数据仍在，返回即恢复）
    act(() => {
      h.sink?.(ev('agent_reply', { reply_text: '回答二' }, 'T-1'))
    })
    expect(screen.queryByText('回答二')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '返回当前对话' }))
    expect(bubbleTexts()).toEqual(['回答二'])
  })

  it('历史重放：默认本轮视图（最新问答在前，旧历史收起）', async () => {
    h.history.mockResolvedValue(
      emptyHistory({
        messages: [
          { role: 'human', content: '旧问题' },
          { role: 'ai', content: '旧回答' },
          { role: 'human', content: '新问题' },
          { role: 'ai', content: '新回答' },
        ],
      }),
    )
    renderChatPage({ openThread: 'th-9' })

    await waitFor(() => expect(screen.getByLabelText('当前问题')).toHaveTextContent('新问题'))
    // 本轮优先：滚动区只有最新一轮回答；旧一轮收起、无返回条（默认本轮视图）
    expect(bubbleTexts()).toEqual(['新回答'])
    expect(screen.queryByText('旧回答')).toBeNull()
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByText('旧问题')).toBeInTheDocument()
    expect(screen.getByText('旧回答')).toBeInTheDocument()
  })

  it('确认卡处理后：即使挂卡前进过历史，仍停留本轮并可见执行反馈（review H1）', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    await completeRound(rerenderPage, '创建订单')
    // 挂卡前先进历史视图（historyQuestionId 记录 = 当前问题 id）
    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()

    // 流内确认卡到达 → 自动回本轮（卡片必须可见）
    act(() => {
      h.sink?.(
        ev('confirmation_requested', {
          tool_name: 'order_create',
          task_id: 'T-1',
          payload_hash: 'hash-1',
          card: testCard(),
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()

    // 点确认：乐观清卡后**不得**因派生复活翻回历史视图（review H1 回归点）
    h.confirm.mockResolvedValue({ stream_id: 'st-1' })
    fireEvent.click(screen.getByRole('button', { name: '确认并执行该写入计划' }))
    await waitFor(() => expect(screen.queryByText('客户 A / 2026-08')).toBeNull())
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()
    expect(screen.getByRole('button', { name: '查看更早对话（1 轮）' })).toBeInTheDocument()

    // 执行续传反馈落在本轮可见（tool_result 成功 → agent_reply 终态）
    act(() => {
      h.sink?.(ev('tool_result', { ok: true, status: 'ok' }, 'T-1'))
      h.sink?.(ev('agent_reply', { reply_text: '订单已创建完成' }, 'T-1'))
    })
    expect(bubbleTexts()).toContain('订单已创建完成')
  })

  it('历史视图内发送失败：停留本轮、错误可见、问题条回落上一问（review M1）', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    await completeRound(rerenderPage, '第二问', '回答二')
    // 进历史视图（historyQuestionId 记录 = 第二问）
    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()

    // 历史视图内发送第三问 → POST 失败（乐观气泡回滚，question 回落第二问，
    // 与 historyQuestionId 相同——不得翻回历史把错误藏掉）
    h.send.mockRejectedValueOnce(new ApiError(500, { error_code: 'INTERNAL_ERROR' }))
    await submit('第三问')

    await waitFor(() =>
      expect(screen.getByLabelText('当前问题')).not.toHaveTextContent('第三问'),
    )
    expect(screen.getByLabelText('当前问题')).toHaveTextContent('第二问')
    // 停留本轮视图：无返回条、本轮回答可见、错误提示可见（hint 回落
    // ApiError.message = '请求失败'）
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()
    expect(await screen.findByText('请求失败')).toBeInTheDocument()
    expect(bubbleTexts()).toEqual(['回答二'])
  })
})

describe('ChatPage 禁用门禁', () => {
  /** 提交 → 订阅成功 → 推流到终态（解除 streamId 门禁），便于单独测确认卡门禁。 */
  async function submitAndFinish(text = '创建订单') {
    const page = renderChatPage()
    await submit(text)
    await waitFor(() => expect(h.seen.streamId).toBe('st-1'))
    h.stream.status = 'done'
    act(() => {
      page.rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
    return page
  }

  it('挂起确认卡期间输入区禁用（防确认前再次提交）', async () => {
    await submitAndFinish()

    act(() => {
      h.sink?.(
        ev('confirmation_requested', {
          tool_name: 'order_create',
          task_id: 'T-1',
          payload_hash: 'hash-1',
          card: testCard(),
        }),
      )
    })

    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())
    // 此时 streamId 已为 null，禁用**只可能**来自挂起确认卡门禁
    expect(h.seen.streamId).toBeNull()
    expect(screen.getByLabelText('输入指令')).toBeDisabled()
  })

  it('确认后清卡，流终态后输入区重新可用', async () => {
    h.confirm.mockResolvedValue({ stream_id: 'st-1' })
    const page = await submitAndFinish()

    act(() => {
      h.sink?.(
        ev('confirmation_requested', {
          tool_name: 'order_create',
          task_id: 'T-1',
          payload_hash: 'hash-1',
          card: testCard(),
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: '确认并执行该写入计划' }))
    await waitFor(() =>
      expect(h.confirm).toHaveBeenCalledWith({
        thread_id: 'th-1',
        approved: true,
        payload_hash: 'hash-1',
      }),
    )
    // 乐观清卡
    await waitFor(() => expect(screen.queryByText('客户 A / 2026-08')).toBeNull())
    // 确认后同流续传（tool_running → tool_result → agent_reply），期间仍禁用；
    // 推到终态后才恢复——这是「确认即解锁」与「流结束才解锁」的分界。
    h.stream.status = 'done'
    act(() => {
      page.rerenderPage()
    })
    await waitFor(() => expect(screen.getByLabelText('输入指令')).toBeEnabled())
  })

  it('挂起确认卡期间：历史入口禁用并提示先处理确认卡', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问')
    await completeRound(rerenderPage, '创建订单', '好的')

    act(() => {
      h.sink?.(
        ev('confirmation_requested', {
          tool_name: 'order_create',
          task_id: 'T-1',
          payload_hash: 'hash-1',
          card: testCard(),
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())

    // 历史入口（1 轮更早）挂起卡期间禁用 + title 提示；仍在本轮视图（无返回条），
    // 确认卡可见不被历史阅读打断
    const toggle = screen.getByRole('button', { name: '查看更早对话（1 轮）' })
    expect(toggle).toBeDisabled()
    expect(toggle).toHaveAttribute('title', '请先处理确认卡')
    expect(screen.queryByRole('button', { name: '返回当前对话' })).toBeNull()
  })
})

describe('ChatPage 卡片入流（2026-09-10 布局修复）', () => {
  /** 断言卡片是消息区滚动容器的**直接子项**（全宽顶格的对齐前提）。 */
  function assertCardInsideMessages() {
    const list = document.querySelector('.chat-page__messages')
    const card = document.querySelector('.chat-inline-card')
    expect(card).not.toBeNull()
    expect(list?.contains(card as Node)).toBe(true)
    expect(card?.parentElement).toBe(list)
  }

  it('挂起确认卡渲染在滚动容器内部且排除出 aria-live 播报区', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '创建订单', '好的')

    act(() => {
      h.sink?.(
        ev('confirmation_requested', {
          tool_name: 'order_create',
          task_id: 'T-1',
          payload_hash: 'hash-1',
          card: testCard(),
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('客户 A / 2026-08')).toBeInTheDocument())

    assertCardInsideMessages()
    // 卡内可交互按钮随卡进入消息区
    expect(screen.getByRole('button', { name: '确认并执行该写入计划' })).toBeInTheDocument()
    // 卡子树排除出容器继承的 polite 播报（避免整卡自动播报与内部状态反复播报）
    expect(document.querySelector('.chat-inline-card')).toHaveAttribute('aria-live', 'off')
  })

  it('导出完成卡同样渲染在滚动容器内部', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '导出订单', '已导出')

    act(() => {
      h.sink?.(
        ev('export_ready', {
          file_id: 'f-1',
          filename: '订单.xlsx',
          entity: 'metal_orders',
          row_count: 3,
          total: 3,
          expires_at: '2026-09-11T00:00:00Z',
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('订单.xlsx')).toBeInTheDocument())

    assertCardInsideMessages()
  })

  it('历史视图不渲染卡片（footer 未传入）；返回本轮即恢复', async () => {
    const { rerenderPage } = renderChatPage()
    await completeRound(rerenderPage, '第一问', '回答一')
    await completeRound(rerenderPage, '第二问', '回答二')

    act(() => {
      h.sink?.(
        ev('export_ready', {
          file_id: 'f-1',
          filename: '订单.xlsx',
          entity: 'metal_orders',
          row_count: 3,
          total: 3,
          expires_at: '',
        }),
      )
    })
    await waitFor(() => expect(screen.getByText('订单.xlsx')).toBeInTheDocument())
    assertCardInsideMessages()

    // 进历史视图：卡片整体不渲染（不论容器内外）
    fireEvent.click(screen.getByRole('button', { name: '查看更早对话（1 轮）' }))
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()
    expect(screen.queryByText('订单.xlsx')).toBeNull()
    expect(document.querySelector('.chat-inline-card')).toBeNull()

    // 返回本轮：卡片恢复且仍在容器内
    fireEvent.click(screen.getByRole('button', { name: '返回当前对话' }))
    expect(screen.getByText('订单.xlsx')).toBeInTheDocument()
    assertCardInsideMessages()
  })
})
