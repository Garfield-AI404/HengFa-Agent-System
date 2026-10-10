/**
 * ChatComposer 交互测试（2026-08-29 新增）。
 *
 * 覆盖输入区被搬到独立组件前**无法单独测试**的键盘语义：
 * - Enter 短按（<450ms）发送、长按（≥450ms）换行；
 * - Shift+Enter 与中文输入法组词回车不拦截（走浏览器默认行为）；
 * - 禁用态与「暂停」方块钮（canStop 时替换发送钮）的出现条件。
 *
 * 这些规则在 ChatPage 内联时期只能靠整页 e2e 覆盖，现在可以逐条驱动。
 */
import { createRef } from 'react'
import type { RefObject } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChatComposer } from '../../../src/pages/chat/ChatComposer'

interface SetupOverrides {
  content?: string
  file?: File | null
  busy?: boolean
  submitting?: boolean
  canStop?: boolean
}

function setup(overrides: SetupOverrides = {}) {
  const inputRef: RefObject<HTMLTextAreaElement | null> = createRef<HTMLTextAreaElement>()
  const props = {
    content: '',
    onContentChange: vi.fn(),
    file: null as File | null,
    onFileChange: vi.fn(),
    busy: false,
    submitting: false,
    onSubmit: vi.fn(),
    inputRef,
    canStop: false,
    onStop: vi.fn(),
    ...overrides,
  }
  const { container } = render(<ChatComposer {...props} />)
  return { ...props, container, textarea: screen.getByLabelText('输入指令') }
}

const xlsx = () => new File(['dummy'], 'orders.xlsx', { type: 'application/vnd.ms-excel' })

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatComposer 禁用态', () => {
  it('busy=true：输入框、附件、发送按钮全部禁用', () => {
    const { container, textarea } = setup({ busy: true, content: '可以发' })
    expect(textarea).toBeDisabled()
    expect(container.querySelector('input[type="file"]')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('内容为空且无附件：发送按钮禁用（防空提交）', () => {
    const { textarea } = setup({ content: '   ' })
    expect(textarea).toBeEnabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('有附件即可发送（内容为空时附件是唯一载荷）', () => {
    setup({ content: '', file: xlsx() })
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('有内容即可发送', () => {
    setup({ content: '对账 8 月' })
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })
})

describe('ChatComposer 提交', () => {
  it('点击发送按钮触发 onSubmit', () => {
    const { onSubmit } = setup({ content: '对账 8 月' })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('Enter 短按（keyup 早于 450ms）触发发送', () => {
    const { onSubmit, onContentChange, textarea } = setup({ content: '对账 8 月' })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    fireEvent.keyUp(textarea, { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onContentChange).not.toHaveBeenCalled()
  })

  it('Enter 长按（≥450ms）插入换行，随后 keyup 不发送', () => {
    vi.useFakeTimers()
    const { onSubmit, onContentChange, textarea } = setup({ content: 'abc' })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    act(() => {
      vi.advanceTimersByTime(500)
    })
    fireEvent.keyUp(textarea, { key: 'Enter' })
    expect(onContentChange).toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('Shift+Enter 不拦截：不发送也不插入换行（走浏览器默认换行）', () => {
    const { onSubmit, onContentChange, textarea } = setup({ content: 'abc' })
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true })
    fireEvent.keyUp(textarea, { key: 'Enter', shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onContentChange).not.toHaveBeenCalled()
  })

  it('中文输入法组词回车（isComposing）不拦截', () => {
    const { onSubmit, textarea } = setup({ content: 'ni' })
    fireEvent.keyDown(textarea, { key: 'Enter', isComposing: true })
    fireEvent.keyUp(textarea, { key: 'Enter', isComposing: true })
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('ChatComposer 附件与状态', () => {
  // [2026-09-11 上传链路收口] accept 与后端白名单（仅 .xlsx）对齐：选择器过滤
  // 只列 .xlsx，不再出现 .xls/.csv（下游 openpyxl 读不了）。
  it('文件选择框 accept 仅 .xlsx（与后端白名单一致）', () => {
    const { container } = setup()
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(input.getAttribute('accept')).toBe('.xlsx')
  })

  it('选择附件：回调传出 File 对象', () => {
    const { onFileChange, container } = setup()
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = xlsx()
    fireEvent.change(input, { target: { files: [file] } })
    expect(onFileChange).toHaveBeenCalledWith(file)
  })

  it('已选附件：显示文件名与移除按钮，点击移除传出 null', () => {
    const { onFileChange } = setup({ file: xlsx() })
    expect(screen.getByText('orders.xlsx')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '移除附件 orders.xlsx' }))
    expect(onFileChange).toHaveBeenCalledWith(null)
  })

  // [15.14] 输入区状态字已删；发送位在 canStop 时换成「暂停」方块钮
  it('canStop=false：发送钮在位，无「暂停」钮', () => {
    setup({ canStop: false })
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '暂停（停止当前处理）' })).toBeNull()
  })

  it('canStop=true：发送位替换为「暂停」钮，可点击并触发 onStop（无 disabled）', () => {
    const { onStop } = setup({ canStop: true, content: '可以发' })
    const stopButton = screen.getByRole('button', { name: '暂停（停止当前处理）' })
    expect(stopButton).toBeEnabled()
    expect(screen.queryByRole('button', { name: '发送消息' })).toBeNull()
    fireEvent.click(stopButton)
    expect(onStop).toHaveBeenCalledTimes(1)
  })
})
