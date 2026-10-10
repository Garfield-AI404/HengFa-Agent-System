/**
 * ChatMessageList 渲染分支测试（2026-08-29 新增）。
 *
 * 断言的是**现有**渲染规则。原四种助手气泡形态（等待动画 / 流式增量文本 /
 * 终态 markdown / 用户纯文本）自 2026-09-13 收敛为三种：只要 streaming 就只
 * 显示生成中动效（整轮持续、气泡内无文本），终态才把整段 content 交给
 * markdown 渲染。若将来调整「流式期间不出现文本」「终态才走 markdown」这类
 * 取舍，这里的用例会先失败——正是目的。
 */
import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ChatMessageList } from '../../../src/pages/chat/ChatMessageList'
import type { ChatMessage } from '../../../src/pages/chat/types'

function renderList(messages: ChatMessage[]) {
  const scrollRef = createRef<HTMLDivElement>()
  const { container } = render(<ChatMessageList messages={messages} scrollRef={scrollRef} />)
  return container
}

const msg = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: `m-${Math.random()}`,
  role: 'assistant',
  content: '',
  ...over,
})

describe('ChatMessageList', () => {
  it('空消息列表：不渲染任何气泡', () => {
    const container = renderList([])
    expect(container.querySelectorAll('.chat-message')).toHaveLength(0)
  })

  it('用户消息：头像为「你」，内容按纯文本渲染', () => {
    renderList([msg({ role: 'user', content: '对账 8 月订单' })])
    expect(screen.getByText('你')).toBeInTheDocument()
    expect(screen.getByText('对账 8 月订单')).toBeInTheDocument()
  })

  it('助手流式且无内容：渲染等待动画而非空气泡', () => {
    const container = renderList([msg({ content: '', streaming: true })])
    expect(container.querySelector('.chat-message__typing')).not.toBeNull()
    expect(screen.getByLabelText('恒发 Agent 正在思考')).toBeInTheDocument()
  })

  it('[2026-09-13] 助手流式（有增量）：整轮只渲染生成中动效，不渲染任何文本', () => {
    const container = renderList([msg({ content: '正在核对**', streaming: true })])
    // 动效在、可访问名在（整轮持续，与 content 无关）
    expect(container.querySelector('.chat-message__typing')).not.toBeNull()
    expect(screen.getByLabelText('恒发 Agent 正在思考')).toBeInTheDocument()
    // 流式期间气泡内不出现文本，也不走 markdown 容器
    const markdown = container.querySelector('.chat-message__content--markdown')
    expect(markdown).toBeNull()
    const p = container.querySelector('.chat-message__content')
    expect(p?.tagName).toBe('P')
    expect(p).toHaveTextContent('')
  })

  it('助手落定：走 markdown 容器', () => {
    const container = renderList([msg({ content: '**加粗**已落定', streaming: false })])
    expect(container.querySelector('.chat-message__content--markdown')).not.toBeNull()
    expect(container.querySelector('strong')).not.toBeNull()
  })

  it('note 小字：有则渲染，无则不渲染', () => {
    const withNote = renderList([msg({ content: 'x', note: '已取消生成' })])
    expect(withNote.querySelector('.chat-message__note')?.textContent).toBe('已取消生成')

    const withoutNote = renderList([msg({ content: 'x' })])
    expect(withoutNote.querySelector('.chat-message__note')).toBeNull()
  })

  it('带 taskId：显示任务号', () => {
    renderList([msg({ content: 'x', taskId: 'task-42' })])
    expect(screen.getByText('（任务 task-42）')).toBeInTheDocument()
  })

  it('消息区为 aria-live=polite（流式内容可被读屏播报）', () => {
    const container = renderList([])
    expect(container.querySelector('.chat-page__messages')).toHaveAttribute(
      'aria-live',
      'polite',
    )
  })

  it('[2026-09-09 布局重构] toolbar：渲染于消息区顶部（历史视图返回条），缺省不渲染', () => {
    const { container } = render(
      <ChatMessageList
        messages={[]}
        scrollRef={createRef<HTMLDivElement>()}
        toolbar={
          <div className="chat-page__history-top">
            <button type="button">返回当前对话</button>
          </div>
        }
      />,
    )
    const list = container.querySelector('.chat-page__messages')
    expect(list?.firstElementChild).toHaveClass('chat-page__history-top')
    expect(screen.getByRole('button', { name: '返回当前对话' })).toBeInTheDocument()

    const without = renderList([])
    expect(without.querySelector('.chat-page__history-top')).toBeNull()
  })

  it('[2026-09-10 卡片入流] footer：渲染于滚动容器**内部**、消息之后；缺省不渲染', () => {
    const { container } = render(
      <ChatMessageList
        messages={[msg({ role: 'user', content: '创建订单' })]}
        scrollRef={createRef<HTMLDivElement>()}
        footer={
          <div className="chat-inline-card" aria-live="off">
            <button type="button">确认</button>
          </div>
        }
      />,
    )
    const list = container.querySelector('.chat-page__messages')
    const card = container.querySelector('.chat-inline-card')
    // 卡片在滚动容器内部（旧实现为容器之后的兄弟节点，被挤到输入框上方）
    expect(list?.contains(card)).toBe(true)
    // 且在消息行之后（footer 槽位于 messages 之后）
    const children = Array.from(list?.children ?? [])
    expect(children.indexOf(card as Element)).toBeGreaterThan(
      children.findIndex((el) => el.classList.contains('chat-message')),
    )
    // 卡内可交互按钮随之进入消息区
    expect(screen.getByRole('button', { name: '确认' })).toBeInTheDocument()

    const without = renderList([])
    expect(without.querySelector('.chat-inline-card')).toBeNull()
  })
})
