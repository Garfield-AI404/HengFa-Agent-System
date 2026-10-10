/**
 * 单条对话消息行（2026-09-09 布局重构从 ChatMessageList 的内联 map 抽出）。
 *
 * 抽出目的：[2026-09-09 前端] 布局改为「最新一问常驻置顶 + 本轮优先（历史
 * 收拢）」后，消息行会被两处共用渲染——本轮滚动区（ChatMessageList）与
 * 历史视图（同一 ChatMessageList 容器内渲染 history 切片）。把行的渲染分支
 * （user 纯文本 / assistant 生成中动效 / assistant 终态 markdown）抽成独立
 * 组件，两处复用同一份 JSX 且可单独测试。
 *
 * [2026-09-13 整轮动效] 助手气泡分支收敛为两条：**只要 streaming 就只显示
 * 生成中动效**（整轮持续，与 content 无关），终态（streaming=false）才把整段
 * content 交给 markdown 渲染。此前「等待动画 → 流式 pre-wrap 逐字 → 落定
 * markdown」的三段式已废弃——按用户口径，整轮期间气泡内不出现任何文本。
 *
 * 本组件无内部状态、无副作用；key 由使用方（父级 map）负责。
 */
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMessage } from './types'

export function ChatMessageRow({ message }: { message: ChatMessage }): React.JSX.Element {
  return (
    <div className={`chat-message chat-message--${message.role}`}>
      <div className="chat-message__head">
        <span
          className="chat-message__avatar"
          aria-label={message.role === 'user' ? '用户' : '恒发 Agent'}
        >
          {message.role === 'user' ? '你' : '恒'}
        </span>
        {message.taskId ? (
          <span className="chat-message__task">（任务 {message.taskId}）</span>
        ) : null}
      </div>
      {message.role === 'assistant' ? (
        message.streaming ? (
          // [2026-09-13 整轮动效] 生成中：整轮只显示三点跳动动效，气泡内
          // 不出现任何文本（增量不再写入 content）。终态事件（agent_reply /
          // agent_error / task_failed / task_cancelled）落定 streaming=false
          // 后才整段渲染。
          <p className="chat-message__content">
            <span className="chat-message__typing" aria-label="恒发 Agent 正在思考" />
          </p>
        ) : (
          <div className="chat-message__content chat-message__content--markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )
      ) : (
        <p className="chat-message__content">{message.content}</p>
      )}
      {message.note ? <p className="chat-message__note">{message.note}</p> : null}
    </div>
  )
}
