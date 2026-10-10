/**
 * 对话输入区（2026-08-29 从 ChatPage.tsx 抽出，自持有回车长按判定的内部状态）。
 *
 * 抽出目的：输入区的键盘交互（Enter 短按发送 / 长按 ≥450ms 换行 / 中文输入法
 * 组词回车不拦截）此前依赖 ChatPage 里的 4 个 ref 与 doSubmit 闭包，无法单独
 * 驱动测试。抽出后三个判定 ref 成为组件内部状态，测试可直接构造按键事件。
 *
 * 输入区 auto-grow（2026-09-02 15.19）：一行起步、内容变多（含长按 Enter
 * 插入的换行）框随内容长高，最高约视口 50%——超出护栏的内容在框内滚动仍
 * 可见（布局护栏，非行数截断）。见组件内 auto-grow effect 与 global.css
 * .chat-composer__field textarea 的同名兜底。
 *
 * 边界：本组件只负责「输入区长什么样、按键怎么解释」；「何时禁用」「何时
 * 聚焦」「提交后做什么」属编排职责，由父级通过 props 决定。
 */
import { useEffect, useRef } from 'react'
import type { FormEvent, KeyboardEvent, RefObject } from 'react'
import { Paperclip, Send, Square, X } from 'lucide-react'

interface ChatComposerProps {
  content: string
  onContentChange: (value: string) => void
  file: File | null
  onFileChange: (file: File | null) => void
  /** 提交中 / 流订阅中 / 有挂起确认卡 → 输入区禁用、发送钮禁用；canStop 时
   *  该位置换成可点的「暂停」方块钮（无 disabled——工作态按钮可点击）。 */
  busy: boolean
  submitting: boolean
  /** 表单提交（点击发送按钮 / 输入框内短按 Enter）。 */
  onSubmit: () => void
  inputRef: RefObject<HTMLTextAreaElement | null>
  /** agent 工作全程（connecting/streaming/reconnecting，排除挂起确认卡 suspended
   *  与 done/error）时用「暂停」方块钮替换发送钮；点击调用 onStop。 */
  canStop: boolean
  onStop: () => void
}

export function ChatComposer({
  content,
  onContentChange,
  file,
  onFileChange,
  busy,
  submitting,
  onSubmit,
  inputRef,
  canStop,
  onStop,
}: ChatComposerProps): React.JSX.Element {
  const enterPressStartRef = useRef(0) // Enter 按下时刻（长按判定）
  const enterTimerRef = useRef<number | null>(null) // 长按判定 timer
  const enterLongPressRef = useRef(false) // 本次按键是否已长按插入换行

  // 组件卸载清理：回车长按 timer（避免 timer 到点后操作已卸载组件）
  useEffect(() => {
    return () => {
      if (enterTimerRef.current !== null) window.clearTimeout(enterTimerRef.current)
    }
  }, [])

  // —— 输入框 auto-grow（2026-09-02 15.19）——
  // 视口护栏上限：输入框最高约视口 50%（innerHeight × 0.5，向下取整）。防
  // 内容无限撑高顶出发送钮/聊天区；超出护栏的内容在框内滚动仍可见——这是
  // 布局护栏不是行数截断（用户选「不限行数」），与 global.css 的
  // max-height: 50dvh 兜底一致，双保险。
  const MAX_COMPOSER_VH = 0.5

  // 依赖 [content, inputRef]：重算由 content 变化驱动（输入 / 长按 Enter 换行
  // 走 onContentChange → content）；inputRef 是父级传入的 useRef 结果（React
  // 保证 ref 对象跨渲染恒同），列入仅因 exhaustive-deps 无法自证传入 ref
  // 稳定，不影响重算时机。
  // 重算逻辑：先复位 'auto' 再读 scrollHeight——否则高度只增不减，内容删空后
  // 无法回落一行；再与护栏上限取 min 写 px（向下取整）。scrollHeight === 0
  // （jsdom 等无布局环境）不写高度，保持 'auto' 兜底。另挂 window resize
  // 重算（移动端地址栏/虚拟键盘收展后视口变化防高度失真），effect 清理
  // （含卸载）时移除监听。
  useEffect(() => {
    const regrow = (): void => {
      const el = inputRef.current
      if (!el) return
      el.style.height = 'auto'
      if (el.scrollHeight === 0) return
      const cap = Math.floor(window.innerHeight * MAX_COMPOSER_VH)
      el.style.height = `${Math.min(el.scrollHeight, cap)}px`
    }
    regrow()
    window.addEventListener('resize', regrow)
    return () => window.removeEventListener('resize', regrow)
  }, [content, inputRef])

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    onSubmit()
  }

  // 回车键长按换行 / 短按发送：keydown 拦截 Enter 并启动 450ms 判定 timer——
  // timer 到点（长按）在光标处插入换行；keyup 早于 timer（短按）触发发送。
  // Shift+Enter / 中文输入法组词回车不拦截（走浏览器默认行为）。
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== 'Enter' || event.shiftKey) return
    // 中文输入法组词中按 Enter 是提交候选词，不拦截（isComposing / keyCode 229）
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    event.preventDefault() // 拦截默认换行，由长按判定决定插入换行还是发送
    if (event.repeat) return // 按住自动重复：不重设 timer、不重复插入
    enterPressStartRef.current = Date.now()
    enterLongPressRef.current = false
    if (enterTimerRef.current !== null) window.clearTimeout(enterTimerRef.current)
    enterTimerRef.current = window.setTimeout(() => {
      // 长按到点（≥450ms 未松开）：在光标处插入换行
      enterTimerRef.current = null
      enterLongPressRef.current = true
      const el = inputRef.current
      if (!el) return
      const start = el.selectionStart ?? el.value.length
      const end = el.selectionEnd ?? el.value.length
      // 读当前 DOM 值拼接（timer 闭包内的 content 可能过期）
      const next = el.value.slice(0, start) + '\n' + el.value.slice(end)
      onContentChange(next)
      // 光标移到插入点之后（onContentChange 提交后下一帧恢复选中位置）
      requestAnimationFrame(() => {
        const pos = start + 1
        inputRef.current?.setSelectionRange(pos, pos)
      })
    }, 450)
  }

  const handleKeyUp = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== 'Enter' || event.shiftKey) return
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    if (enterTimerRef.current !== null) {
      window.clearTimeout(enterTimerRef.current)
      enterTimerRef.current = null
    }
    if (enterLongPressRef.current) return // 已长按插入换行，不触发发送
    onSubmit() // 短按发送（父级 doSubmit 内部 guard：busy/空内容自动忽略）
  }

  return (
    <form className="chat-composer" onSubmit={handleSubmit}>
      <div className="chat-composer__field">
        <label className="sr-only" htmlFor="chat-input">
          输入指令
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          value={content}
          onChange={(event) => onContentChange(event.target.value)}
          onKeyDown={handleKeyDown}
          onKeyUp={handleKeyUp}
          rows={1}
          disabled={busy}
          placeholder="例如：对账 2026-08 客户 xx 的订单差异"
        />
        <div className="chat-composer__row">
          <label className="btn chat-composer__file chat-composer__attach" aria-label="上传附件">
            <Paperclip aria-hidden="true" size={16} />
            <input
              type="file"
              accept=".xlsx"
              onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
              disabled={busy}
              className="sr-only"
            />
          </label>
          {file ? (
            <span className="chat-composer__filename">
              {file.name}
              <button
                type="button"
                className="icon-button"
                onClick={() => onFileChange(null)}
                aria-label={`移除附件 ${file.name}`}
                disabled={busy}
              >
                <X aria-hidden="true" size={14} />
              </button>
            </span>
          ) : null}
          <span className="chat-composer__spacer" />
          {canStop ? (
            // [15.14] 流处理中：发送位换成「暂停」方块钮（同规格圆钮，
            // 无 disabled——工作态按钮可点击，点按停止当前生成）
            <button
              type="button"
              className="btn btn--primary chat-composer__send"
              onClick={onStop}
              aria-label="暂停（停止当前处理）"
              aria-busy={submitting}
            >
              <Square aria-hidden="true" size={18} />
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn--primary chat-composer__send"
              disabled={busy || (!content.trim() && !file)}
              aria-label="发送消息"
              aria-busy={submitting}
            >
              <Send aria-hidden="true" size={18} />
            </button>
          )}
        </div>
      </div>
    </form>
  )
}
