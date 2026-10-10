/**
 * 对话消息列表（2026-08-29 从 ChatPage.tsx 抽出，纯展示组件）。
 *
 * 抽出目的：消息气泡的四种渲染分支（等待动画 / 流式增量 / 终态 markdown /
 * 用户纯文本）此前内联在 ChatPage 的 800 行 return 里，无法单独测试。
 * 抽出后可对渲染分支直接断言，不必驱动整条 SSE。
 *
 * [2026-09-09 布局重构] 行的 JSX 已进一步抽到 ChatMessageRow（本轮滚动区与
 * 历史视图共用同一渲染）；本组件 props 保持 messages + scrollRef 不变，
 * 另加可选 toolbar 渲染在滚动容器内顶部——历史视图的「返回当前对话」条
 * （见 ChatPage 的 showingHistory 分支），本轮视图不传。
 *
 * [2026-09-10 卡片入流] 新增可选 footer 槽，与 toolbar 对称：渲染在滚动容器
 * 内**消息之后**的底部。本轮的三张卡（确认卡 / 对账报告卡 / 导出卡）由此进入
 * `.chat-page__messages` 流内——此前它们是滚动容器**之后的兄弟节点**，被 flex
 * 列布局挤到输入框正上方，与助手回复行不同宽、不顶格；入流后与助手消息行同为
 * 容器的直接 flex 子项（默认 align-items:stretch → 全宽顶格），且随消息区一同
 * 滚动。历史视图不传 footer（卡不进历史视图；ChatPage 侧门禁）。
 *
 * 本组件无内部状态、无副作用；滚动容器 ref 由父级持有（滚动时机是编排
 * 职责，见 ChatPage 的 lastMessageKey effect）。
 */
import type { ReactNode, RefObject } from 'react'
import { ChatMessageRow } from './ChatMessageRow'
import type { ChatMessage } from './types'

interface ChatMessageListProps {
  messages: ChatMessage[]
  /** 消息区滚动容器：父级据此在流式增量时滚底。 */
  scrollRef: RefObject<HTMLDivElement | null>
  /** [2026-09-09 布局重构] 滚动容器内顶部的附加条（历史视图的返回条）。 */
  toolbar?: ReactNode
  /** [2026-09-10 卡片入流] 滚动容器内底部的附加槽（本轮三张卡：确认 /
   *  对账报告 / 导出）。与 toolbar 对称，随消息区一同滚动；缺省不渲染。 */
  footer?: ReactNode
}

export function ChatMessageList({
  messages,
  scrollRef,
  toolbar,
  footer,
}: ChatMessageListProps): React.JSX.Element {
  return (
    <div ref={scrollRef} className="chat-page__messages" aria-live="polite">
      {toolbar}
      {messages.map((message) => (
        <ChatMessageRow key={message.id} message={message} />
      ))}
      {footer}
    </div>
  )
}
