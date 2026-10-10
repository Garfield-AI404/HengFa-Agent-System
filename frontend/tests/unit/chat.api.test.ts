/**
 * chat.api 提交契约测试（2026-09-10 生产缺陷回归）。
 *
 * 缺陷背景：只挂 Excel 附件、不打字直接发送时，ChatPage 传入 content=''，
 * 旧实现先做 chatMessageSchema.parse('') → ZodError（min(1) 校验失败）早于
 * 附件上传抛出 → 前端归为 NETWORK_ERROR 提示「无法连接服务器」，且
 * POST /chat/attachments 从未发出。
 *
 * 正确修法（不改 chatMessageSchema 契约本身）：带附件时空内容兜底为
 * ATTACHMENT_ONLY_MESSAGE，与 ChatPage 乐观气泡文案一致；无附件时空内容
 * 仍保留「请输入内容」校验。
 *
 * 本文件只 mock apiFetch（保留真实 client 的 ApiError，供 schemas.ts 与
 * chat.api 的运行时校验共用），从而断言真实的「先传附件、再发消息」顺序
 * 与请求体，而非断言 mock 之间的调用。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => {
  const calls: { path: string; options: Record<string, unknown> }[] = []
  return { calls }
})

vi.mock('../../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/client')>()
  return {
    ...actual,
    apiFetch: (path: string, options: Record<string, unknown> = {}) => {
      h.calls.push({ path, options })
      if (path === '/chat/attachments') {
        return Promise.resolve({
          attachment_id: 'att-1',
          filename: 'order.xlsx',
          content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          size_bytes: 10,
          sha256: 'abc',
          expires_at: null,
        })
      }
      return Promise.resolve({ thread_id: 'thread-client-1', stream_id: 'st-1', task_id: null })
    },
  }
})

import { ATTACHMENT_ONLY_MESSAGE, sendChatMessage } from '../../src/api/chat.api'

function makeFile(name = 'order.xlsx'): File {
  return new File(['x'], name, {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
}

/** 路径序列（按调用先后）。 */
const paths = (): string[] => h.calls.map((call) => call.path)

beforeEach(() => {
  h.calls.length = 0
})

describe('sendChatMessage 附件两段式', () => {
  it('仅带附件（内容为空）：先传附件再发消息，message 为兜底文案', async () => {
    const file = makeFile()
    const result = await sendChatMessage('', file, 'thread-client-1')

    // 1) 上传请求必须先发出（缺陷根因：此前 parse 抛错导致它根本没发）
    expect(paths()).toEqual(['/chat/attachments', '/chat/messages'])
    expect(result).toEqual({ thread_id: 'thread-client-1', stream_id: 'st-1', task_id: null })

    const upload = h.calls[0].options.body as FormData
    expect(upload.get('file')).toBe(file)
    expect(upload.get('thread_id')).toBe('thread-client-1')

    // 2) messages 请求体 message 必须为与乐观气泡一致的兜底文案
    const body = h.calls[1].options.jsonBody as {
      message: string
      thread_id: string
      attachment_ids: string[]
    }
    expect(body.message).toBe(ATTACHMENT_ONLY_MESSAGE)
    expect(body.thread_id).toBe('thread-client-1')
    expect(body.attachment_ids).toEqual(['att-1'])
  })

  it('附件 + 非空内容：message 保留用户输入（不套兜底文案）', async () => {
    await sendChatMessage('对账 8 月订单', makeFile(), 'thread-client-1')

    expect(paths()).toEqual(['/chat/attachments', '/chat/messages'])
    expect((h.calls[1].options.jsonBody as { message: string }).message).toBe('对账 8 月订单')
  })
})

describe('sendChatMessage 空内容校验（不因本修复放宽）', () => {
  it('无附件且内容为空：仍抛「请输入内容」，不发起任何请求', () => {
    // 校验同步抛出（sendChatMessage 非 async，parse 在函数体内直接 throw），
    // 用 toThrow 而非 rejects。
    expect(() => sendChatMessage('', null, null)).toThrow('请输入内容')
    expect(h.calls).toHaveLength(0)
  })

  it('无附件且内容为纯空白：同样拒绝，不发起任何请求', () => {
    expect(() => sendChatMessage('   ', undefined, null)).toThrow('请输入内容')
    expect(h.calls).toHaveLength(0)
  })
})
