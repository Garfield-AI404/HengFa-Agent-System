/**
 * chatReducer 单元测试（2026-08-29 新增）。
 *
 * 每个用例对应旧 ChatPage.applyEvent 的一个 switch 分支，断言的是**旧实现的
 * 实际行为**（而非理想行为）——本轮重构的硬边界是行为等价，任何一条断言
 * 失败都意味着 S4 接入后界面表现会变，必须先确认是有意变更还是回归。
 *
 * 2026-09-01 起工具执行指示条与工具条 state 已移除，相关断言与用例
 * 一并删除。
 */
import { describe, expect, it } from 'vitest'
import type { PendingConfirmation, StreamEventOut } from '../../../src/api/types'
import { chatReducer, initialChatState } from '../../../src/pages/chat/reducer'
import type { ChatState } from '../../../src/pages/chat/reducer'
import { CARD_NOT_GENERATED_NOTICE } from '../../../src/pages/chat/constants'
import type { ChatMessage } from '../../../src/pages/chat/types'

let seq = 0
function ev(
  eventType: string,
  payload: Record<string, unknown> = {},
  taskId: string | null = null,
): StreamEventOut {
  seq += 1
  return {
    event_id: `e${seq}`,
    event_type: eventType as StreamEventOut['event_type'],
    task_id: taskId,
    sequence: seq,
    payload: payload as StreamEventOut['payload'],
  }
}

const msg = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: `m${Math.random()}`,
  role: 'assistant',
  content: '',
  ...over,
})

const streaming = (content: string): ChatMessage =>
  msg({ role: 'assistant', content, streaming: true })

// 测试只关心 card 被原样透传，故用最小假对象（真实 ConfirmationCard 字段见
// api/types.ts；reducer 不读 card 内部，无需构造完整形状）
const card = { summary: '写入订单' } as unknown as PendingConfirmation['card']

const pending = (taskId: string): PendingConfirmation => ({
  task_id: taskId,
  tool_name: 'order_create',
  card,
  payload_hash: 'h1',
})

/** 只比对消息的可读部分（id 由模块级计数器生成，不可预期）。 */
function visible(state: ChatState): Array<Pick<ChatMessage, 'role' | 'content' | 'streaming'>> {
  return state.messages.map(({ role, content, streaming }) => ({ role, content, streaming }))
}

describe('chatReducer', () => {
  it('未知事件类型：状态原样返回', () => {
    const state: ChatState = { ...initialChatState, messages: [msg({ content: 'hi' })] }
    expect(chatReducer(state, { type: 'event', event: ev('agent_started') })).toBe(state)
  })

  describe('agent_reply', () => {
    it('next_action=call_tool：移除末尾流式气泡，不落定对话消息', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ content: '问题', role: 'user' }), streaming('过渡文案')],
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply', { next_action: 'call_tool', reply_text: '过渡文案' }),
      })
      expect(visible(next)).toEqual([{ role: 'user', content: '问题', streaming: undefined }])
    })

    it('next_action=call_tool 但末尾非流式气泡：不动', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ content: '已落定', streaming: false })],
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply', { next_action: 'call_tool' }),
      })
      expect(next.messages).toHaveLength(1)
    })

    it('终局：整段替换流式气泡内容并落定', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [streaming('半截增量')],
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '完整答案' }),
      })
      expect(visible(next)).toEqual([{ role: 'assistant', content: '完整答案', streaming: false }])
    })

    it('终局无流式气泡且 reply_text 非空：push 新消息', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '答案' }, 'task-1'),
      })
      expect(visible(next)).toEqual([{ role: 'assistant', content: '答案', streaming: undefined }])
      expect(next.messages[0].taskId).toBe('task-1')
    })

    it('终局无流式气泡且 reply_text 为空：不产生空消息', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '' }),
      })
      expect(next.messages).toHaveLength(0)
    })

    it('终局清理本任务的挂起卡', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [streaming('增量')],
        pendingConfirmation: pending('task-9'),
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '完成' }, 'task-9'),
      })
      expect(next.pendingConfirmation).toBeNull()
    })

    it('终局不清其他任务的挂起卡', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [streaming('增量')],
        pendingConfirmation: pending('task-9'),
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '完成' }, 'task-OTHER'),
      })
      expect(next.pendingConfirmation).not.toBeNull()
    })
  })

  describe('agent_reply_start / chunk', () => {
    it('start：落定既有流式气泡并开一个空气泡', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('上一轮')] }
      const next = chatReducer(state, { type: 'event', event: ev('agent_reply_start') })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '上一轮', streaming: false },
        { role: 'assistant', content: '', streaming: true },
      ])
    })

    it('[2026-09-13] chunk：不写入 content，气泡保持生成中（整轮只显示动效）', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('')] }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_reply_chunk', { chunk: '，世界' }),
      })
      expect(visible(next)).toEqual([{ role: 'assistant', content: '', streaming: true }])
    })

    it('[2026-09-13] chunk 无流式气泡：兜底开一条空气泡（兼容缺 start）', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_chunk', { chunk: '迟到增量' }),
      })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '', streaming: true },
      ])
    })

    it('空 chunk：状态原样返回', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('x')] }
      expect(chatReducer(state, { type: 'event', event: ev('agent_reply_chunk', { chunk: '' }) })).toBe(
        state,
      )
    })

    it('[2026-09-13 P2] 末尾已有流式气泡：chunk 返回原 state 引用（Object.is bailout，不逐条重渲染）', () => {
      const s = chatReducer(initialChatState, { type: 'event', event: ev('agent_reply_start') })
      expect(
        chatReducer(s, { type: 'event', event: ev('agent_reply_chunk', { chunk: '第一个字' }) }),
      ).toBe(s)
    })

    it('[2026-09-13 P2] 末尾无流式气泡：缺 start 兜底路径仍构造新 state（补气泡）', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_chunk', { chunk: '迟到增量' }),
      })
      expect(next).not.toBe(initialChatState)
      expect(visible(next)).toEqual([{ role: 'assistant', content: '', streaming: true }])
    })

    it('[2026-09-13] 整轮多段增量：气泡始终只有动效、无文本（content 恒为空）', () => {
      let s: ChatState = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_start'),
      })
      for (const chunk of ['正在', '核对', '数据', '**']) {
        s = chatReducer(s, { type: 'event', event: ev('agent_reply_chunk', { chunk }) })
      }
      expect(s.messages).toHaveLength(1)
      expect(visible(s)).toEqual([{ role: 'assistant', content: '', streaming: true }])
    })

    it('[2026-09-13] 整轮动效后终局 agent_reply：整段替换并落定（streaming=false）', () => {
      let s: ChatState = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_start'),
      })
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_chunk', { chunk: '半截' }) })
      s = chatReducer(s, { type: 'event', event: ev('agent_reply', { reply_text: '**完整**答案' }) })
      expect(visible(s)).toEqual([{ role: 'assistant', content: '**完整**答案', streaming: false }])
    })

    it('[2026-09-13] 整轮动效后终态错误：必须停动效并出文案（不留转圈气泡）', () => {
      let s: ChatState = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_start'),
      })
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_chunk', { chunk: '生成中' }) })
      s = chatReducer(s, { type: 'event', event: ev('agent_error', { error: 'model_unavailable' }) })
      expect(s.messages.some((m) => m.streaming)).toBe(false)
      expect(s.messages).toHaveLength(1)
      expect(s.messages[0].content).toBe('模型暂时不可用，未执行任何操作，请稍后重试。')
    })
  })

  describe('agent_error', () => {
    it('[2026-09-10] 终局错误丢弃末尾流式气泡（空气泡/半截增量让位于代码文案），只留一条', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [streaming('半截')],
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_error', { error: 'unknown_tool' }),
      })
      expect(visible(next)).toEqual([
        {
          role: 'assistant',
          content: '无法识别该操作指令，未执行任何写入，请换个说法重试。',
          streaming: undefined,
        },
      ])
    })

    it('[2026-09-10] 移除 agent_reply_start 开出的空气泡，只留代码文案', () => {
      // agent_reply_start → 空气泡 → 模型失败 agent_error
      const afterStart = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_reply_start'),
      })
      const next = chatReducer(afterStart, {
        type: 'event',
        event: ev('agent_error', { error: 'model_unavailable' }),
      })
      expect(visible(next)).toEqual([
        {
          role: 'assistant',
          content: '模型暂时不可用，未执行任何操作，请稍后重试。',
          streaming: undefined,
        },
      ])
    })

    it('未知 error 码：回退为「处理失败（code）。」', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'SOMETHING_NEW' }),
      })
      expect(next.messages[0].content).toBe('处理失败（SOMETHING_NEW）。')
    })

    it('带 tool_name：文案附带工具名', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'not_implemented', tool_name: 'erp_query' }),
      })
      expect(next.messages[0].content).toBe('该操作暂未接入执行链路。（工具 erp_query）')
    })

    it('model_unavailable + tool_executed=true：改用「任务已执行完成」文案', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'model_unavailable', tool_executed: true }),
      })
      expect(next.messages[0].content).toContain('任务已执行完成')
    })

    it('model_unavailable 但未执行工具：仍是「未执行任何操作」文案', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'model_unavailable' }),
      })
      expect(next.messages[0].content).toContain('未执行任何操作')
    })

    it('steps_exhausted + tool_executed=true：任务已执行但额度耗尽，提示发新消息询问结果', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'steps_exhausted', tool_executed: true }),
      })
      expect(next.messages[0].content).toBe(
        '任务已执行完成，但当前消息的执行额度已用完，未能生成结果总结；请发送新消息继续询问结果。',
      )
    })

    it('steps_exhausted 未执行工具：提示发送新消息继续（额度按消息重置，无需开新对话）', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'steps_exhausted' }),
      })
      expect(next.messages[0].content).toBe('已达本轮执行上限，请发送新消息继续。')
    })
  })

  describe('confirmation_requested', () => {
    it('字段齐全：挂卡', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev(
          'confirmation_requested',
          { card, tool_name: 'order_create', task_id: 'task-1', payload_hash: 'h1' },
          'task-1',
        ),
      })
      expect(next.pendingConfirmation).toMatchObject({
        task_id: 'task-1',
        tool_name: 'order_create',
        payload_hash: 'h1',
        re_requested: false,
      })
    })

    it('re_requested=true 透传', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev(
          'confirmation_requested',
          {
            card,
            tool_name: 'order_create',
            task_id: 'task-1',
            payload_hash: 'h2',
            re_requested: true,
          },
          'task-1',
        ),
      })
      expect(next.pendingConfirmation?.re_requested).toBe(true)
      expect(next.pendingConfirmation?.payload_hash).toBe('h2')
    })

    it('字段不全：不挂卡', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('confirmation_requested', { tool_name: 'order_create', task_id: 'task-1' }),
      })
      expect(next.pendingConfirmation).toBeNull()
    })
  })

  describe('tool_result', () => {
    it('ok=true：清卡，不追加消息（等 agent_reply 续上）', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ content: 'x', streaming: false })],
        pendingConfirmation: pending('task-1'),
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('tool_result', { ok: true }, 'task-1'),
      })
      expect(next.pendingConfirmation).toBeNull()
      expect(next.messages).toHaveLength(1)
      expect(next.turnFallback).toBeNull()
    })

    it('[2026-09-10] ok=false 拒绝：不立即插气泡，只挂兜底态', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('tool_result', { ok: false, status: 'cancelled' }, 'task-1'),
      })
      expect(next.messages).toHaveLength(0)
      expect(next.turnFallback).toEqual({
        text: '已拒绝确认，任务已取消（未执行任何写入）。',
        taskId: 'task-1',
      })
    })

    it('[2026-09-10] ok=false 失败：兜底文案带工具名', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev(
          'tool_result',
          { ok: false, status: 'failed', tool_name: 'order_create' },
          'task-1',
        ),
      })
      expect(next.messages).toHaveLength(0)
      expect(next.turnFallback?.text).toBe('工具 order_create 执行失败，未写入 ERP。')
    })

    it('[2026-09-10] ok=false 无工具名：通用失败兜底文案', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('tool_result', { ok: false }, 'task-1'),
      })
      expect(next.turnFallback?.text).toBe('执行失败，未写入 ERP。')
    })
  })

  describe('task_failed / task_cancelled', () => {
    it('[2026-09-13 P1] task_failed：丢弃空流式气泡，只留「摘要（错误码）」，无空白行', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('')] }
      const next = chatReducer(state, {
        type: 'event',
        event: ev(
          'task_failed',
          { final_error_code: 'ERP_DOWN', final_error_summary: 'ERP 连接失败' },
          'task-1',
        ),
      })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: 'ERP 连接失败（ERP_DOWN）', streaming: undefined },
      ])
      // 无空白 assistant 行残留
      expect(next.messages.filter((m) => m.role === 'assistant' && m.content === '')).toHaveLength(0)
    })

    it('[2026-09-13 P1] task_failed：末尾非流式（已落定）气泡不受影响，仅追加一条', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ content: '上一轮回答', streaming: false })],
      }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('task_failed', { final_error_code: 'ERP_DOWN' }, 'task-1'),
      })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '上一轮回答', streaming: false },
        { role: 'assistant', content: '任务执行失败（ERP_DOWN）。', streaming: undefined },
      ])
    })

    it('[2026-09-13 P1] task_cancelled：丢弃空流式气泡，只留取消文案，无空白行', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('')] }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('task_cancelled', { reason: 'generation_cancelled' }, 'task-1'),
      })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '已取消当前生成。', streaming: undefined },
      ])
      expect(next.messages.filter((m) => m.role === 'assistant' && m.content === '')).toHaveLength(0)
    })

    it('task_failed 无摘要：只显示错误码', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('task_failed', { final_error_code: 'ERP_DOWN' }, 'task-1'),
      })
      expect(next.messages[0].content).toBe('任务执行失败（ERP_DOWN）。')
    })

    it('task_cancelled（generation_cancelled）：通用取消文案', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('task_cancelled', { reason: 'generation_cancelled' }, 'task-1'),
      })
      expect(next.messages[0].content).toBe('已取消当前生成。')
    })

    it('task_cancelled 其他原因：文案带原因', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('task_cancelled', { reason: 'user_abort' }, 'task-1'),
      })
      expect(next.messages[0].content).toBe('任务已取消（user_abort）。')
    })
  })

  /**
   * [2026-09-10 单气泡收敛] 行为测试：六条真实事件序列（与后端发射点一一对应）
   * 下，每个已结束回合的 assistant 气泡恰好 1 条、内容来源正确、无空白残留。
   */
  describe('[2026-09-10] 单气泡收敛（确认卡 / 终局错误）', () => {
    function feed(events: StreamEventOut[]): ChatState {
      return events.reduce<ChatState>(
        (s, e) => chatReducer(s, { type: 'event', event: e }),
        { ...initialChatState, messages: [msg({ role: 'user', content: '帮我操作' })] },
      )
    }

    const assistants = (s: ChatState): ChatMessage[] =>
      s.messages.filter((m) => m.role === 'assistant')

    const emptySettled = (s: ChatState): ChatMessage[] =>
      s.messages.filter(
        (m) => m.role === 'assistant' && m.content === '' && m.streaming === false,
      )

    // 确认前的外部轮：模型决定 call_tool（过渡气泡被移除）
    const preConfirm = [
      ev('agent_reply_start'),
      ev('agent_reply_chunk', { chunk: '正在处理' }),
      ev('agent_reply', { next_action: 'call_tool', reply_text: '正在处理' }),
      ev(
        'confirmation_requested',
        { card, tool_name: 'order_create', task_id: 'task-1', payload_hash: 'h1' },
        'task-1',
      ),
    ]

    it('(a) 确认卡拒绝 → 模型正常回复：只留模型回复', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_reply_chunk', { chunk: '好的' }),
        ev('agent_reply', { reply_text: '好的，已取消本次写入。' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('好的，已取消本次写入。')
      expect(emptySettled(next)).toHaveLength(0)
      expect(next.turnFallback).toBeNull()
    })

    it('(b) 确认卡确认成功 → 模型正常回复：只留模型回复（不在同一回合出现第二条）', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: true, tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_reply_chunk', { chunk: '已完成' }),
        ev('agent_reply', { reply_text: '订单创建已完成。' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('订单创建已完成。')
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(c) 拒绝确认后模型不可用：兜底与终局原因合并进同一气泡，不吞掉可操作指引', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev(
          'agent_error',
          { error: 'model_unavailable', tool_executed: true },
          'task-1',
        ),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '已拒绝确认，任务已取消（未执行任何写入）。\n' +
          '任务已执行完成，但回复生成时模型暂时不可用，请继续询问以确认结果。',
      )
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(c1b) 拒绝确认 → steps_exhausted：合并后仍能看到「请发送新消息继续」', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_error', { error: 'steps_exhausted' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      const content = next.messages[next.messages.length - 1].content
      expect(content).toContain('已拒绝确认，任务已取消（未执行任何写入）。')
      expect(content).toContain('已达本轮执行上限，请发送新消息继续。')
    })

    it('(c2) 执行失败后模型不可用：兜底与终局原因合并进同一气泡', () => {
      const next = feed([
        ...preConfirm,
        ev(
          'tool_result',
          { ok: false, status: 'failed', tool_name: 'order_create' },
          'task-1',
        ),
        ev('agent_reply_start'),
        ev('agent_error', { error: 'model_unavailable', tool_executed: true }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '工具 order_create 执行失败，未写入 ERP。\n' +
          '任务已执行完成，但回复生成时模型暂时不可用，请继续询问以确认结果。',
      )
    })

    it('(c3) 确认成功执行后模型不可用：用「任务已执行完成」覆盖文案（未被兜底误伤）', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: true, tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_error', { error: 'model_unavailable', tool_executed: true }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toContain('任务已执行完成')
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(d) 模型不可用（无确认卡）：清除空气泡，只留代码文案', () => {
      const next = feed([
        ev('agent_reply_start'),
        ev('agent_error', { error: 'model_unavailable' }),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '模型暂时不可用，未执行任何操作，请稍后重试。',
      )
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(d2) 图外安全网 chat_run_failed：清除空气泡，只留代码文案', () => {
      const next = feed([
        ev('agent_reply_start'),
        ev('agent_error', { error: 'chat_run_failed' }),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('对话处理失败，请稍后重试。')
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(e) 步数耗尽（未执行）：只留代码文案', () => {
      const next = feed([
        ev('agent_reply_start'),
        ev('agent_reply', { next_action: 'call_tool', reply_text: '正在处理' }),
        ev('agent_error', { error: 'steps_exhausted', tool_executed: false }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '已达本轮执行上限，请发送新消息继续。',
      )
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(e2) 步数耗尽且已执行写工具：用「任务已执行完成」覆盖文案', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: true, tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_error', { error: 'steps_exhausted', tool_executed: true }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toContain('任务已执行完成')
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(f1) history_truncated 非终局：不产生任何可见气泡', () => {
      const next = feed([
        ev('agent_reply_start'),
        ev('agent_reply', { next_action: 'call_tool', reply_text: '正在处理' }),
        ev('agent_error', { error: 'history_truncated' }),
      ])
      expect(assistants(next)).toHaveLength(0)
      expect(next.turnFallback).toBeNull()
    })

    it('(f2) history_truncated 后模型正常回复：只留模型回复，不残留代码气泡', () => {
      const next = feed([
        ev('agent_reply_start'),
        ev('agent_reply', { next_action: 'call_tool', reply_text: '正在处理' }),
        ev('agent_error', { error: 'history_truncated' }),
        ev('agent_reply_start'),
        ev('agent_reply', { reply_text: '这是本轮回答。' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('这是本轮回答。')
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(f3) tool_result 兜底挂起 + history_truncated + 模型回复：仍只留模型回复', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('agent_error', { error: 'history_truncated' }),
        ev('agent_reply_start'),
        ev('agent_reply', { reply_text: '已取消。' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('已取消。')
      expect(next.turnFallback).toBeNull()
    })

    it('(g1) 终局 reply_text 为空 + 流式空气泡：删除气泡，不留空白 assistant 行', () => {
      const next = feed([ev('agent_reply_start'), ev('agent_reply', { reply_text: '' }, 'task-1')])
      expect(next.messages.filter((m) => m.role === 'assistant')).toHaveLength(0)
      expect(emptySettled(next)).toHaveLength(0)
    })

    it('(g2) 终局 reply_text 为空 + 流式气泡 + 挂起兜底：渲染兜底，只留一条非空气泡', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply_start'),
        ev('agent_reply', { reply_text: '' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '已拒绝确认，任务已取消（未执行任何写入）。',
      )
      expect(emptySettled(next)).toHaveLength(0)
      expect(next.turnFallback).toBeNull()
    })

    it('(g3) 终局 reply_text 为空且无流式气泡但有挂起兜底：仍渲染兜底', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'failed', tool_name: 'order_create' }, 'task-1'),
        ev('agent_reply', { reply_text: '' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '工具 order_create 执行失败，未写入 ERP。',
      )
    })

    it('(g4) 终局非空 reply_text 正常路径：不受空串分支影响', () => {
      const next = feed([ev('agent_reply_start'), ev('agent_reply', { reply_text: '正常回复' })])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe('正常回复')
      expect(next.messages[next.messages.length - 1].streaming).toBe(false)
    })

    it('(h1) task_failed + 挂起兜底：兜底与失败文案合并进同一气泡', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
        ev('task_failed', { final_error_code: 'ERP_DOWN', final_error_summary: 'ERP 连接失败' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '已拒绝确认，任务已取消（未执行任何写入）。\nERP 连接失败（ERP_DOWN）',
      )
      expect(next.turnFallback).toBeNull()
    })

    it('(h2) task_cancelled + 挂起兜底：兜底与取消文案合并进同一气泡', () => {
      const next = feed([
        ...preConfirm,
        ev('tool_result', { ok: false, status: 'failed', tool_name: 'order_create' }, 'task-1'),
        ev('task_cancelled', { reason: 'generation_cancelled' }, 'task-1'),
      ])
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[next.messages.length - 1].content).toBe(
        '工具 order_create 执行失败，未写入 ERP。\n已取消当前生成。',
      )
      expect(next.turnFallback).toBeNull()
    })

    it('(i1) 兜底文案与终局原因相同：不重复拼接', () => {
      const next = chatReducer(
        {
          ...initialChatState,
          messages: [msg({ role: 'user', content: 'x' })],
          turnFallback: { text: '模型暂时不可用，未执行任何操作，请稍后重试。', taskId: 'task-1' },
        },
        { type: 'event', event: ev('agent_error', { error: 'model_unavailable' }, 'task-1') },
      )
      expect(next.messages[next.messages.length - 1].content).toBe(
        '模型暂时不可用，未执行任何操作，请稍后重试。',
      )
    })
  })

  /**
   * [2026-09-10 单气泡收敛] 兜底跨轮不残留：新一轮（发送新消息 / 切换会话 /
   * 历史重放）开始时挂起兜底必须作废，不得渲染到新轮。
   */
  describe('[2026-09-10] 兜底跨轮清理', () => {
    const withFallback: ChatState = {
      ...initialChatState,
      messages: [],
      turnFallback: { text: '已拒绝确认，任务已取消（未执行任何写入）。', taskId: 'task-1' },
    }

    it('add_user_message：清空挂起兜底', () => {
      const next = chatReducer(withFallback, {
        type: 'add_user_message',
        message: msg({ role: 'user', content: '下一条' }),
      })
      expect(next.turnFallback).toBeNull()
    })

    it('clear_messages：清空挂起兜底', () => {
      const next = chatReducer(withFallback, { type: 'clear_messages' })
      expect(next.turnFallback).toBeNull()
    })

    it('restore：清空挂起兜底', () => {
      const next = chatReducer(withFallback, {
        type: 'restore',
        messages: [msg({ role: 'assistant', content: '历史' })],
        pendingConfirmation: null,
      })
      expect(next.turnFallback).toBeNull()
    })

    it('reset：回到初始态（无兜底）', () => {
      const next = chatReducer(withFallback, { type: 'reset' })
      expect(next).toBe(initialChatState)
    })

    it('跨轮序列：挂起兜底 → 新用户消息 → 终局 agent_error 不渲染旧兜底', () => {
      const next = chatReducer(
        chatReducer(withFallback, {
          type: 'add_user_message',
          message: msg({ role: 'user', content: '下一条' }),
        }),
        { type: 'event', event: ev('agent_error', { error: 'model_unavailable' }) },
      )
      expect(next.messages.at(-1)?.content).toBe('模型暂时不可用，未执行任何操作，请稍后重试。')
      expect(next.messages.at(-1)?.content).not.toContain('已拒绝确认')
    })
  })

  /**
   * [2026-09-13 P0] 客户端侧流终止收尾（stream_aborted）。
   *
   * useTaskStream 在「用户点暂停」与「非 2xx」两条路径上**不投递任何终局
   * 流事件**，ChatPage 的 done/error 落点必须补发 stream_aborted，否则
   * agent_reply_start 开出的流式气泡永久转圈。同时它对**所有** done/error
   * 都会补发，故必须对「正常终局之后到达」幂等无害。
   */
  describe('[2026-09-13 P0] stream_aborted：客户端侧流终止收尾', () => {
    it('暂停（cancelled）：丢弃空流式气泡 + 补可见取消文案，无空白行、无转圈', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [
          msg({ role: 'user', content: '帮我查一下' }),
          streaming(''),
        ],
      }
      const next = chatReducer(state, { type: 'stream_aborted', reason: 'cancelled' })
      expect(visible(next)).toEqual([
        { role: 'user', content: '帮我查一下', streaming: undefined },
        { role: 'assistant', content: '已取消当前生成。', streaming: undefined },
      ])
      expect(next.messages.some((m) => m.streaming)).toBe(false)
      expect(next.messages.filter((m) => m.role === 'assistant' && m.content === '')).toHaveLength(0)
    })

    it('非 2xx（error）：丢弃空流式气泡 + 补请求失败文案', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('')] }
      const next = chatReducer(state, { type: 'stream_aborted', reason: 'error' })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '对话处理失败，请稍后重试。', streaming: undefined },
      ])
      expect(next.messages.some((m) => m.streaming)).toBe(false)
    })

    it('幂等：正常终局（agent_reply 已落定）后到达的 stream_aborted 是无害的', () => {
      // 正常终局：agent_reply_start → 流式气泡 → 终局 agent_reply 落定
      const settled = chatReducer(
        chatReducer(initialChatState, { type: 'event', event: ev('agent_reply_start') }),
        { type: 'event', event: ev('agent_reply', { reply_text: '这是最终答复。' }) },
      )
      const next = chatReducer(settled, { type: 'stream_aborted', reason: 'cancelled' })
      // 原引用返回：既不删最终答复，也不追加收尾气泡
      expect(next).toBe(settled)
      expect(visible(next)).toEqual([
        { role: 'assistant', content: '这是最终答复。', streaming: false },
      ])
    })

    it('幂等：末尾非流式（无 streaming 气泡）时不追加收尾文案', () => {
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'assistant', content: '已落定', streaming: false })],
      }
      expect(chatReducer(state, { type: 'stream_aborted', reason: 'error' })).toBe(state)
    })

    it('幂等：空会话无流式气泡时原样返回', () => {
      expect(chatReducer(initialChatState, { type: 'stream_aborted', reason: 'cancelled' })).toBe(
        initialChatState,
      )
    })
  })

  /**
   * [2026-09-28 确认卡回执一致性] prepare 失败（无卡）时撤销本回合内「请点击
   * 确认卡」的乐观文案 + 错误气泡点明「本次未生成确认卡」。
   *
   * 事件序列取自考勤轮 C1 实测（`hengfa-eval/e2e/attendance-card-2026-09-28/
   * runs/wave2/run_C1_r1.txt`）：同名员工让 `spec.prepare` 抛错 →
   * `agent_error{error:"preparation_error"}`，此前决策轮的 call_tool 计划文案
   * 含「请点击确认卡完成写入」。
   */
  describe('[2026-09-28] 确认卡回执一致性：prepare 失败撤销乐观文案', () => {
    const prepErr = (detail = '计划中同一员工出现多条') =>
      ev('agent_error', { error: 'preparation_error', tool_name: 'attendance_plan_execute', detail })

    const assistants = (s: ChatState): ChatMessage[] =>
      s.messages.filter((m) => m.role === 'assistant')
    const lastContent = (s: ChatState): string => s.messages[s.messages.length - 1].content

    it('真实序列（call_tool 计划文案被丢弃）+ 模型解释回复：错误气泡与回复两条并存', () => {
      let s: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: '今天杨峰请假一天。' })],
      }
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_start') })
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_chunk', { chunk: '写入计划如下' }) })
      s = chatReducer(s, {
        type: 'event',
        event: ev('agent_reply', {
          next_action: 'call_tool',
          reply_text: '写入计划如下：\n- 员工：杨峰\n\n请点击确认卡完成写入。',
        }),
      })
      s = chatReducer(s, {
        type: 'event',
        event: ev('tool_running', {
          tool_name: 'attendance_plan_execute',
          stage: 'collecting_arguments',
        }),
      })
      s = chatReducer(s, { type: 'event', event: prepErr() })

      // 乐观文案由 reducer 丢弃（call_tool 轮不展示），界面不残留任何「请点击确认卡」
      expect(assistants(s).some((m) => m.content.includes('请点击确认卡'))).toBe(false)
      // preparation_error 非终局（图继续到 model）但**非静默**：错误气泡必须渲染
      // （这一步锚定 SILENT_NON_TERMINAL_AGENT_ERROR_CODES 不含该码——若误入
      // 静默表，此处 assistants 会退化为 0，用例变红）
      expect(assistants(s)).toHaveLength(1)
      expect(lastContent(s)).toContain('本次未生成确认卡')
      expect(lastContent(s)).toContain('计划中同一员工出现多条')

      // 模型随后生成的解释性回复（非终局 ⇒ 流继续）到达：**另起**一条，两条并存
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_start') })
      s = chatReducer(s, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '未生成确认卡，请补充员工姓名。' }),
      })
      expect(assistants(s)).toHaveLength(2)
      // 错误气泡未被模型回复覆盖（仍在倒数第二条），且模型回复在最后
      expect(s.messages[s.messages.length - 2].content).toContain('本次未生成确认卡')
      expect(lastContent(s)).toBe('未生成确认卡，请补充员工姓名。')
    })

    it('乐观文案以已落定正文本形态残留：就地追加可见作废说明（不删除原文）', () => {
      const plan = msg({
        role: 'assistant',
        content: '写入计划如下：\n- 员工：杨峰\n\n请点击确认卡完成写入。',
        streaming: false,
      })
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: '今天杨峰请假一天。' }), plan],
      }
      const next = chatReducer(state, { type: 'event', event: prepErr() })
      const revoked = next.messages.find((m) => m.id === plan.id)!
      expect(revoked.content).toContain('请点击确认卡') // 原文保留（不作静默删除）
      expect(revoked.content).toContain(CARD_NOT_GENERATED_NOTICE)
      expect(lastContent(next)).toContain('本次未生成确认卡')
    })

    it('幂等：同一形态再次收到 preparation_error 不重复追加作废说明', () => {
      const plan = msg({ role: 'assistant', content: '请点击确认卡核对后写入。', streaming: false })
      const base: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: 'x' }), plan],
      }
      const once = chatReducer(base, { type: 'event', event: prepErr() })
      const twice = chatReducer(once, { type: 'event', event: prepErr() })
      const text = twice.messages.find((m) => m.id === plan.id)!.content
      expect(text.split(CARD_NOT_GENERATED_NOTICE).length - 1).toBe(1)
    })

    it('只撤销本回合（最后一条 user 之后）；更早历史不被改写', () => {
      const older = msg({
        role: 'assistant',
        content: '上一轮：请点击确认卡完成写入。',
        streaming: false,
      })
      const current = msg({ role: 'assistant', content: '请点击确认卡完成写入。', streaming: false })
      const state: ChatState = {
        ...initialChatState,
        messages: [older, msg({ role: 'user', content: '第二轮' }), current],
      }
      const next = chatReducer(state, { type: 'event', event: prepErr() })
      // 更早历史一字未动
      expect(next.messages.find((m) => m.id === older.id)!.content).toBe(
        '上一轮：请点击确认卡完成写入。',
      )
      // 本回合的被就地作废
      expect(next.messages.find((m) => m.id === current.id)!.content).toContain(
        CARD_NOT_GENERATED_NOTICE,
      )
    })

    it('反向：其他 agent_error 码不撤销乐观文案，也不带「未生成确认卡」', () => {
      const current = msg({ role: 'assistant', content: '请点击确认卡完成写入。', streaming: false })
      const state: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: 'x' }), current],
      }
      const next = chatReducer(state, { type: 'event', event: ev('agent_error', { error: 'unknown_tool' }) })
      expect(next.messages.find((m) => m.id === current.id)!.content).toBe('请点击确认卡完成写入。')
      expect(lastContent(next)).not.toContain('未生成确认卡')
    })

    it('反向：正常出卡路径逐字不变', () => {
      let s: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: '帮我操作' })],
      }
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_start') })
      s = chatReducer(s, {
        type: 'event',
        event: ev('agent_reply', { next_action: 'call_tool', reply_text: '请点击确认卡完成写入。' }),
      })
      s = chatReducer(s, {
        type: 'event',
        event: ev(
          'confirmation_requested',
          { card, tool_name: 'order_create', task_id: 'task-1', payload_hash: 'h1' },
          'task-1',
        ),
      })
      expect(visible(s)).toEqual([{ role: 'user', content: '帮我操作', streaming: undefined }])
      expect(s.pendingConfirmation?.task_id).toBe('task-1')
    })

    it('反向：确认拒绝路径逐字不变', () => {
      let s: ChatState = {
        ...initialChatState,
        messages: [msg({ role: 'user', content: '帮我操作' })],
      }
      s = chatReducer(s, {
        type: 'event',
        event: ev('tool_result', { ok: false, status: 'cancelled', tool_name: 'order_create' }, 'task-1'),
      })
      s = chatReducer(s, { type: 'event', event: ev('agent_reply_start') })
      s = chatReducer(s, {
        type: 'event',
        event: ev('agent_reply', { reply_text: '好的，已取消本次写入。' }, 'task-1'),
      })
      expect(visible(s)).toEqual([
        { role: 'user', content: '帮我操作', streaming: undefined },
        { role: 'assistant', content: '好的，已取消本次写入。', streaming: false },
      ])
      expect(s.turnFallback).toBeNull()
    })

    it('反向：task_failed 路径逐字不变', () => {
      const state: ChatState = { ...initialChatState, messages: [streaming('')] }
      const next = chatReducer(state, {
        type: 'event',
        event: ev(
          'task_failed',
          { final_error_code: 'ERP_DOWN', final_error_summary: 'ERP 连接失败' },
          'task-1',
        ),
      })
      expect(visible(next)).toEqual([
        { role: 'assistant', content: 'ERP 连接失败（ERP_DOWN）', streaming: undefined },
      ])
    })

    it('[2026-09-28] 码表锚定：纳入的「无卡」写工具失败码各自触发撤销（清空码表即红）', () => {
      // 复审指出真实序列那条用例没锚在机制上（清空码表它不会红）；本用例
      // 逐码验证 PREPARE_FAILURE_AGENT_ERROR_CODES 的每个成员都会撤销。
      for (const code of ['preparation_error', 'shutting_down', 'harness_misconfigured', 'not_implemented', 'invalid_arguments']) {
        const promise = msg({
          role: 'assistant',
          content: '请点击确认卡完成写入。',
          streaming: false,
        })
        const base: ChatState = {
          ...initialChatState,
          messages: [msg({ role: 'user', content: 'x' }), promise],
        }
        const next = chatReducer(base, { type: 'event', event: ev('agent_error', { error: code }) })
        expect(next.messages.find((m) => m.id === promise.id)!.content).toContain(
          CARD_NOT_GENERATED_NOTICE,
        )
      }
    })

    it('[2026-09-28 裁定纳入] invalid_arguments 触发撤销乐观文案（破例：发射点读写共用）', () => {
      // 用户 2026-09-28 裁定：批准意图是「全部无卡码」，写工具上的
      // invalid_arguments（tools_node.py:228 非超限 / :234 超限带 detail，
      // 均位于 :246 的 if spec.is_write 之前）
      // 必然无卡，且模型同回合决策轮可能已说过「请点击确认卡」。
      const promise = msg({ role: 'assistant', content: '请点击确认卡完成写入。', streaming: false })
      const next = chatReducer(
        { ...initialChatState, messages: [msg({ role: 'user', content: 'x' }), promise] },
        { type: 'event', event: ev('agent_error', { error: 'invalid_arguments' }) },
      )
      expect(next.messages.find((m) => m.id === promise.id)!.content).toContain(
        CARD_NOT_GENERATED_NOTICE,
      )
    })

    it('[2026-09-28 复审 P3] 无 user 消息时不改写任何历史（同一引用，不回填作废说明）', () => {
      const older = msg({ role: 'assistant', content: '请点击确认卡完成写入。', streaming: false })
      const state: ChatState = { ...initialChatState, messages: [older] }
      const next = chatReducer(state, { type: 'event', event: prepErr() })
      // 无本轮起点（lastUserIndex<0）⇒ 无可撤销的本轮正文，原消息对象原样保留
      expect(next.messages[0]).toBe(older)
      expect(older.content).toBe('请点击确认卡完成写入。')
    })
  })

  /**
   * [2026-09-29] agent_error 的 detail 拼进错误气泡（超限 >20 条明细 → 明确拒绝
   * + 给出口 的载荷经此路径展示）。
   *
   * 渲染链（reducer.ts）：:441 取「非空字符串」detail（空串/非串走 null 分支）
   * → :461 以换行追加到基础文案 → :473 经 mergeTurnText 落进**同一条** assistant
   * 气泡。本组钉三态：有 / 无 / 空串，确保 detail 只追加、不新增气泡、也不拼空串。
   */
  describe('[2026-09-29] agent_error detail 拼进错误气泡', () => {
    const assistants = (s: ChatState): ChatMessage[] =>
      s.messages.filter((m) => m.role === 'assistant')

    const baseText = '指令参数无法通过校验，未执行任何写入，请检查后重试。'
    const detailText =
      '本次明细共 23 条，超过单次上限 20 条，未写入任何数据，请分批提交。'

    it('带 detail：基础文案与 detail 同现于一条气泡，且只产生一条气泡', () => {
      // 前置一个流式气泡，验证终局错误把它丢弃后仍只留 1 条（不是两条）
      const state: ChatState = { ...initialChatState, messages: [streaming('半截增量')] }
      const next = chatReducer(state, {
        type: 'event',
        event: ev('agent_error', { error: 'invalid_arguments', detail: detailText }),
      })
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[0].content).toContain(baseText)
      expect(next.messages[0].content).toContain(detailText)
      // 换行追加（基础文案在前、detail 在后）
      expect(next.messages[0].content).toBe(`${baseText}\n${detailText}`)
    })

    it('不带 detail：正文等于基础文案，无多余换行', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'invalid_arguments' }),
      })
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[0].content).toBe(baseText)
      expect(next.messages[0].content).not.toContain('\n')
    })

    it('detail 为空字符串：走 null 分支，正文等于基础文案，不拼空串', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'invalid_arguments', detail: '' }),
      })
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[0].content).toBe(baseText)
      expect(next.messages[0].content).not.toContain('\n')
    })

    it('detail 为非字符串（类型守卫）：忽略之，正文等于基础文案', () => {
      const next = chatReducer(initialChatState, {
        type: 'event',
        event: ev('agent_error', { error: 'invalid_arguments', detail: 23 }),
      })
      expect(assistants(next)).toHaveLength(1)
      expect(next.messages[0].content).toBe(baseText)
      expect(next.messages[0].content).not.toContain('23')
    })
  })
})
