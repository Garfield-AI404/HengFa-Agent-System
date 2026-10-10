/**
 * 对话页（规格 2.7.2 /chat；Phase 3a 对话式 harness：模型拥有循环）。
 *
 * - thread_id 生命周期：localStorage 持久化（hengfa.chat.thread）；新对话 =
 *   新 thread_id 并清 localStorage；首次提交无 thread_id 时服务端生成并返回，
 *   前端据此持久化（后续提交显式携带）；
 * - 历史重放：挂载时（有 thread_id）fetch /chat/history → 渲染 checkpoint
 *   重放消息 + 挂起确认卡；stream_id 非 null 且有挂起卡 → 订阅该流等待确认
 *   后事件续传（进程重启丢流 → 只重放历史不订阅）；
 * - 事件映射（Phase 3a）：agent_started 忽略；agent_reply 终态助手消息；
 *   agent_error 按 error 值给文案；tool_running 轻提示；confirmation_requested
 *   → 消息流内联确认卡；tool_result 清卡/失败提示；task_cancelled / task_failed
 *   保留映射；
 * - [2026-08-31 批次 4] reconcile_report（reconcile_analyze 完成）→ 对账差异
 *   报告卡（摘要 + 查看明细，明细按 reportId 另拉 GET /chat/reports）；
 * - [2026-08-25 loop 不展示] call_tool 过渡轮的 agent_reply 事件带
 *   next_action=call_tool：前端移除该轮流式气泡，不展示过渡文案（只看 agent
 *   最终回答；needs_input 追问与 reply 终局照常落定）；
 * - 确认走 POST /chat/confirm（{thread_id, approved, payload_hash}）→ 乐观
 *   清卡 → 同一流续传 tool_running → tool_result → agent_reply（re_requested
 *   事件重新挂卡并提示确认已失效）；
 * - 409 THREAD_BUSY → 「上一条消息处理中」（后端防重复提交兜底）；
 * - 消息区为全宽流式文本（无气泡/边框卡片），按时间顺序滚动；输入区水平
 *   居中并固定于页面底部（消息区内部滚动）；
 * - [2026-09-09 布局重构] 「最新一问常驻置顶 + 本轮优先（历史收拢）」：
 *   窗口顶部常驻「当前问题」条（messages 中最后一条 role=user 的消息，随
 *   新问题自动替换）；其下滚动区只渲染该问题之后的回答与内联卡；更早历史
 *   默认收起，经「查看更早对话（N 轮）」入口在历史视图阅读（同一滚动容器，
 *   顶部返回条）。新一轮发送 / reset / 切换会话强制回本轮；挂起确认卡期间
 *   禁止切历史视图（详见派生切分与滚动 effect 注释）；
 * - 发送期间禁用按钮防重复（2.7.4）；危险操作为明确命令按钮（2.7.5）；
 * - 外部字符串（消息、问题）由 React 默认 HTML 转义 + 长度限制（10.6 L1193）；
 * - D26：503 优雅停机门禁提示 + 重试引导（RV-39 信息级落地）；
 * - [2026-08-24 流式改造] 对话形式：乐观用户气泡（发送即显示，失败移除）+
 *   生成中动效（agent_reply_start 起，整轮持续）+ 终局整段渲染（agent_reply
 *   落定替换）；textarea 回车长按（≥450ms）换行/短按发送。
 * - [2026-09-13 整轮动效] 助手气泡整轮只显示生成中动效、不显示文本：增量
 *   （agent_reply_chunk）不再写入气泡，终局事件才把整段回复交给 markdown
 *   渲染。原 rAF 增量合并调度层随之移除（见 handleStreamEvent 注释）。
 * - [2026-08-28] 模型思考内容（agent_reasoning_chunk）前端不展示；
 *   2026-09-01 起工具执行指示条一并移除，仅保留生成中动效作为
 *   「模型现在在做什么」的显示。
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { useLocation } from 'react-router'
import { ApiError, toApiError } from '../api/client'
import {
  ATTACHMENT_MAX_BYTES,
  ATTACHMENT_ONLY_MESSAGE,
  THREAD_STORAGE_KEY,
  confirmChatApi,
  fetchChatHistoryApi,
  notifyThreadsChanged,
  sendChatMessage,
} from '../api/chat.api'
import { useTaskStream } from '../hooks/useTaskStream'
import type { StreamEventOut } from '../api/types'
import { ERROR_CODES } from '../api/types'
import { ErrorNotice } from '../components/ErrorNotice'
import { ConfirmationPanel } from '../components/ConfirmationPanel'
import { ExportCard } from '../components/ExportCard'
import { ReconcileReportCard } from '../components/ReconcileReportCard'

import { chatReducer, initialChatState } from './chat/reducer'
import type { ChatMessage } from './chat/types'
import { nextMessageId } from './chat/types'
import { ChatComposer } from './chat/ChatComposer'
import { ChatMessageList } from './chat/ChatMessageList'

export function ChatPage() {
  const location = useLocation()
  const [content, setContent] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false) // 同一事件循环内的重复点击拦截（2.7.4）
  const [sendError, setSendError] = useState<ApiError | null>(null)
  const [historyError, setHistoryError] = useState<ApiError | null>(null)

  // thread_id 生命周期：localStorage 持久化；新对话 = 新 thread_id
  const [threadId, setThreadId] = useState<string | null>(() =>
    localStorage.getItem(THREAD_STORAGE_KEY),
  )
  const [streamId, setStreamId] = useState<string | null>(null)
  const [subscribeEpoch, setSubscribeEpoch] = useState(0) // 同流重新订阅（确认恢复后立即续传）

  // [2026-09-09 布局重构] 历史视图状态：null = 本轮视图（默认）；非 null =
  // 正在查看「该问题 id 之前」的更早历史。存问题 id 而非布尔：新一轮发送 /
  // reset / 切换会话（restore）后最后一条 user 消息的 id 变化 → showingHistory
  // 派生条件破坏 → 自动回本轮，无需额外 effect 同步（见下方派生注释）。
  const [historyQuestionId, setHistoryQuestionId] = useState<string | null>(null)

  // [2026-08-29 拆分] 消息 / 挂起卡 / 工具条三个 state 收敛为一个 reducer：
  // 事件映射是纯函数（chat/reducer.ts），可脱离 SSE 与整页渲染单独测试。
  // 下面所有 chat.xxx 读、dispatch(...) 写，均对应原三个 useState setter
  // 的一对一替换（2026-09-01 起工具条 state 已随指示条移除）。
  const [chat, dispatch] = useReducer(chatReducer, initialChatState)

  const messagesRef = useRef<HTMLDivElement | null>(null) // 消息区滚动容器（底部固定输入区）
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null) // 新建对话后聚焦输入框
  const handledNewChatRef = useRef(0) // 已处理的新建对话时间戳（防历史回退重复触发）
  const handledOpenThreadRef = useRef<string | null>(null) // 已处理的打开会话 thread_id（防重复触发）
  const focusAfterResetRef = useRef(false) // 待聚焦标记（输入框解除禁用后一次性聚焦）
  const restoredThreadsRef = useRef<Set<string>>(new Set()) // 已重放历史的 thread_id

  /**
   * [2026-09-13 整轮动效] SSE 事件入口：useTaskStream 去重后的事件直接投递
   * 给 reducer。
   *
   * 原「ref 缓冲 + requestAnimationFrame 合并高频 chunk 成每帧一次渲染」的
   * 调度层已随「整轮不渲染文本」（增量不再写入助手气泡，见 chat/reducer.ts
   * 顶部说明）一并移除。**但内容不产生差异 ≠ 不产生渲染**：为守住「高频
   * chunk 不逐条触发整页重渲染」，改由 reducer 的 chunk 分支在末尾已有流式
   * 气泡时返回原 state 引用（useReducer 的 Object.is bailout 生效），与原
   * rAF 合并层收益等价。本回调仅依赖 useReducer 的稳定 dispatch，引用稳定，
   * 可直接传给 useTaskStream，无需再经 ref 中转。
   */
  const handleStreamEvent = useCallback((event: StreamEventOut): void => {
    dispatch({ type: 'event', event })
  }, [])

  // 回车键长按换行 / 短按发送（textarea）：长按（≥450ms 未松开）在光标处
  // 插入换行；短按触发发送。Shift+Enter 走浏览器默认换行。
  const stream = useTaskStream(streamId, subscribeEpoch, handleStreamEvent)

  /** 新建对话：新 thread_id + 清空消息/卡/流（localStorage 持久化 key 同步清）。
   *  [2026-08-29 react-hooks 修复] useCallback 稳定引用：newChat effect 的
   *  exhaustive-deps 依赖项（内部依赖 useState setter、dispatch 与各 ref，
   *  均为稳定引用）。 */
  const resetConversation = useCallback((): void => {
    localStorage.removeItem(THREAD_STORAGE_KEY)
    setThreadId(null)
    dispatch({ type: 'reset' })
    setContent('')
    setFile(null)
    setSendError(null)
    setHistoryError(null)
    setHistoryQuestionId(null) // [2026-09-09 布局重构] 新建对话：视图回本轮（防残留历史视图）
    handledOpenThreadRef.current = null // 打开会话标记同步清（新建后允许再打开同一会话）
    setSubscribeEpoch((prev) => prev + 1)
    setStreamId(null)
  }, [])

  // 新建对话（侧栏「新建对话」→ navigate('/chat', { state: { newChat: Date.now() } })）
  useEffect(() => {
    const stamp = (location.state as { newChat?: number } | null)?.newChat
    if (typeof stamp === 'number' && stamp !== handledNewChatRef.current) {
      handledNewChatRef.current = stamp
      focusAfterResetRef.current = true
      resetConversation()
    }
    // [2026-08-29 react-hooks 修复] resetConversation 已 useCallback 稳定，
    // 列入依赖不引发重复执行（体内另有 stamp 去重守卫）
  }, [location.state, resetConversation])

  // 打开指定会话（侧栏「历史会话」→ navigate('/chat', { state: { openThread } }）；
  // 与 newChat 同来时 openThread 后处理（优先）：新建重置先跑、再切到指定会话）。
  // 只在 thread_id 变化时做完整重置（切线程要清空消息/卡/流，让历史重放
  // effect 因 threadId 变化自动 fetch 填充）；打开的就是当前线程时保持现状
  // （不空屏——历史重放 effect 依赖未变不会重新拉取）。
  useEffect(() => {
    const openThread = (location.state as { openThread?: string } | null)?.openThread
    if (typeof openThread !== 'string' || openThread === handledOpenThreadRef.current) return
    handledOpenThreadRef.current = openThread
    const isSameThread = openThread === threadId
    // [2026-08-29 react-hooks 豁免] 有意为之：本 effect 是对路由导航事件
    // （侧栏 navigate 传入 openThread）的命令式响应——一次性重置会话状态
    // 并写入 localStorage（外部系统）。渲染期调整 state 不适用于这种多
    // 状态 + 外部写入场景，react.dev「响应事件」建议事件驱动，此处事件
    // 源在侧栏组件，经 location.state 传达，effect 是标准落点。
    /* eslint-disable react-hooks/set-state-in-effect */
    if (streamId !== null) setStreamId(null) // 丢弃旧流
    setThreadId(openThread)
    localStorage.setItem(THREAD_STORAGE_KEY, openThread)
    dispatch({ type: 'clear_card' })
    setSendError(null)
    setHistoryError(null)
    restoredThreadsRef.current.delete(openThread) // 已重放过也允许重新拉取
    if (!isSameThread) {
      dispatch({ type: 'clear_messages' }) // 历史重放 effect 因 threadId 变化自动 fetch 填充
      setContent('') // 草稿属于旧线程，不带到新线程（防误发到错误会话）
      setFile(null)
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [location.state, threadId, streamId])

  // 历史重放（挂载/thread 变化时；有活动流或已重放过则跳过）：
  // 渲染 checkpoint 消息 + 挂起确认卡；stream_id 非 null 且有挂起卡 → 订阅
  // 该流等待确认后事件续传（Phase 3a 契约 4）。
  useEffect(() => {
    if (!threadId || streamId !== null || restoredThreadsRef.current.has(threadId)) return
    let cancelled = false
    fetchChatHistoryApi(threadId)
      .then((history) => {
        if (cancelled) return
        restoredThreadsRef.current.add(threadId)
        // 消息重放（human→user；ai→assistant；tool 为内部 JSON 不展示）
        const restored: ChatMessage[] = []
        for (const item of history.messages) {
          if (item.role === 'human') {
            restored.push({ id: nextMessageId(), role: 'user', content: item.content })
          } else if (item.role === 'ai') {
            restored.push({ id: nextMessageId(), role: 'assistant', content: item.content })
          }
        }
        dispatch({
          type: 'restore',
          messages: restored,
          pendingConfirmation: history.pending_confirmation,
        })
        setHistoryError(null)
        if (history.pending_confirmation && history.stream_id) {
          // 挂起卡存在且流可订阅 → 订阅等待确认后事件续传（全量重放：
          // confirmation_requested 幂等 upsert，不会重复渲染）
          // 注：事件去重由 useTaskStream 在流切换时重置，此处无需清理
          setStreamId(history.stream_id)
        }
      })
      .catch((err) => {
        if (cancelled) return
        setHistoryError(toApiError(err))
      })
    return () => {
      cancelled = true
    }
  }, [threadId, streamId])

  // 流结束（终态事件/完成）→ 解除输入禁用，允许下一轮（对话式多轮）
  useEffect(() => {
    if (
      (stream.status === 'done' || stream.status === 'error') &&
      streamId !== null &&
      !stream.suspended
    ) {
      // [2026-08-29 react-hooks 豁免] 有意为之：stream.status 是 SSE 外部
      // 系统（useTaskStream 连接循环）的终态信号，此处是「流结束事件」的
      // 落点——补发流终止收尾 + 清 streamId 解除输入禁用 + 广播侧栏刷新
      // （notifyThreadsChanged 是 window/BroadcastChannel 副作用，绝不能进
      // 渲染期）。
      /* eslint-disable react-hooks/set-state-in-effect */
      // [2026-09-13 P0 修复] 两条**客户端侧**流终止路径下 reducer 收不到任何
      // 终局流事件——用户点「暂停」→ stop() abort 后不再投递；非 2xx 响应 →
      // fail() 停止重连同样不投递——于是 agent_reply_start 开出的流式气泡会
      // 永久转圈。此处补发 stream_aborted 让 reducer 收尾（丢弃空气泡 + 补可见
      // 文案）。**幂等**：正常终局路径的气泡早已由终局事件落定（streaming=
      // false）或删除，reducer 对「末尾无 streaming 气泡」直接原样返回，绝不动
      // 已落定的最终答复。reason 只决定文案：error 终态用请求失败文案，done
      // 终态（暂停 / 会话过期）用取消文案。
      dispatch({ type: 'stream_aborted', reason: stream.status === 'error' ? 'error' : 'cancelled' })
      setStreamId(null)
      // 图已完成（drain 成功路径已 upsert 会话索引）→ 通知侧栏刷新历史列表；
      // POST 时派发太早（索引未写入），统一由终态派发（error 终态刷新后不变，无害）
      // [2026-08-28 跨页同步] 统一走 notifyThreadsChanged：同页 window 事件 +
      // BroadcastChannel 广播，其他标签页/窗口的历史会话列表也实时刷新
      notifyThreadsChanged()
      /* eslint-enable react-hooks/set-state-in-effect */
    }
  }, [stream.status, stream.suspended, streamId])

  /** 提交核心（表单提交与 503 重试共用；内容在失败时保留于 state）。 */
  const doSubmit = async (): Promise<void> => {
    if (submittingRef.current || streamId !== null || chat.pendingConfirmation !== null) return
    if (!content.trim() && !file) return
    const userContent = content.trim() || ATTACHMENT_ONLY_MESSAGE
    submittingRef.current = true
    setSubmitting(true)
    setSendError(null)
    // 乐观渲染：发送即显示用户气泡（不等后端响应；失败时按 id 精确移除）
    // [2026-08-29 修复] 此前失败时按「role + content + taskId 全等」匹配移除，
    // 会误删等待期间由其他路径产生的同内容消息；改用本次气泡的 id 精确匹配。
    const optimisticId = nextMessageId()
    dispatch({
      type: 'add_user_message',
      message: { id: optimisticId, role: 'user', content: userContent },
    })
    try {
      const result = await sendChatMessage(content.trim(), file, threadId)
      // thread_id 持久化：首次提交由服务端生成（thread-{account}-{uuid8}）
      if (!threadId && result.thread_id) {
        setThreadId(result.thread_id)
        localStorage.setItem(THREAD_STORAGE_KEY, result.thread_id)
      }
      // 会话内新建立（或已持有）的线程视为已恢复：防止流结束、streamId
      // 清空后历史重放效应再次触发，用 checkpoint 快照覆盖内存中的对话
      restoredThreadsRef.current.add(result.thread_id)
      setContent('')
      setFile(null)
      setStreamId(result.stream_id)
    } catch (err) {
      // 发送失败：按 id 移除本次的乐观用户气泡（不误删任何其他消息）
      dispatch({ type: 'remove_message', id: optimisticId })
      // [2026-09-09 review 修复] 失败回滚后显式回本轮：历史视图内发送时
      // question 已换新 id（自动回本轮），remove_message 使 question 回落到
      // 历史视图记录的问题 id → showingHistory 派生复活、视图翻回历史，
      // 而 sendError 的 ErrorNotice / 503 重试块只在本轮视图渲染 → 错误被藏。
      setHistoryQuestionId(null)
      setSendError(toApiError(err))
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  /**
   * [2026-09-11 上传链路收口] 附件选择即校验扩展名与体积：任一不过时**不保留该
   * 文件**并立刻提示（避免用户以为已选上、打完字点发送才走一趟网络被拒）。
   * 扩展名白名单与后端 domains/attachments.py ALLOWED_EXTENSIONS（仅 .xlsx）
   * 同源；体积上限常量与后端 attachment_max_bytes 同源（chat.api.ts）。提示复用
   * sendError 通道（ErrorNotice）。
   *
   * 为何需要扩展名预校验：file input 的 accept 只是选择器过滤，用户在文件对话框
   * 选「所有文件」仍可强选任意扩展名；页面无拖拽上传入口，故无 dragover/drop
   * 绕过路径，但「所有文件」这一条足以绕过 accept，故补轻量预校验。
   *
   * 未能接受（扩展名/体积不过）时不改 file，选择合法文件（含移除）时清除上一条
   * 附件校验提示；仅在当前错误正是本函数设置的附件错误码时清除，避免顺手抹掉
   * 503 重试、提交被拒等其它提示。
   */
  const handleFileChange = useCallback((next: File | null): void => {
    if (next && !next.name.toLowerCase().endsWith('.xlsx')) {
      // 与后端 fail-closed 白名单一致（仅 .xlsx）：下游 openpyxl 读不了
      // .xls/.csv，前端选择器与预校验同步收窄，避免「选得上、传得上、工具层
      // 才报不可读」的割裂。用前端独有码 UNSUPPORTED_ATTACHMENT_TYPE：语义准确，
      // 且不与 VALIDATION_ERROR（zod/422/parseWith）混用——文案经 ErrorNotice
      // 的 `HINTS[code] ?? error.message` 透出。
      setSendError(
        new ApiError(0, {
          error_code: ERROR_CODES.UNSUPPORTED_ATTACHMENT_TYPE,
          message: '仅支持 .xlsx 格式的 Excel 文件，请重新选择。',
        }),
      )
      return
    }
    if (next && next.size > ATTACHMENT_MAX_BYTES) {
      // 不保留这个超限文件（也不改 file：此前已挂的合法附件必须原样保留，
      // 否则用户误点一个超限文件会静默丢掉旧附件、空发一条消息）。
      // 用 PAYLOAD_TOO_LARGE 而非 VALIDATION_ERROR：与 413 同码、语义准确，
      // 且不与「提交 zod 校验/后端 422/parseWith 契约」共用的 VALIDATION_ERROR
      // 混淆——文案由 ErrorNotice 的 HINTS 提供，此处不再重复拼写。
      setSendError(new ApiError(0, { error_code: ERROR_CODES.PAYLOAD_TOO_LARGE }))
      return
    }
    setFile(next)
    // 选合法文件（含移除）时只清本附件校验的提示，无关错误（如提交被拒）原样保留。
    setSendError((prev) =>
      prev?.errorCode === ERROR_CODES.PAYLOAD_TOO_LARGE ||
      prev?.errorCode === ERROR_CODES.UNSUPPORTED_ATTACHMENT_TYPE
        ? null
        : prev,
    )
  }, [])

  /** 内联确认卡决策：POST /chat/confirm → 乐观清卡 → 同流续传新事件。 */
  const handleConfirm = async (approved: boolean, payloadHash: string): Promise<void> => {
    if (!threadId || !chat.pendingConfirmation) return
    const result = await confirmChatApi({ thread_id: threadId, approved, payload_hash: payloadHash })
    dispatch({ type: 'clear_card' }) // 乐观清卡：tool_result/agent_reply 自然续上；re_requested 重新挂卡
    // [2026-09-09 review 修复] 清卡即显式回本轮：挂卡前用户若进过历史
    // （historyQuestionId=当前问题 id），clear_card 使 pendingConfirmation
    // 变 null 后 showingHistory 派生会复活、把视图翻回历史顶——随后
    // tool_result/agent_reply 的执行反馈（成功/失败/拒绝）追加在本轮却
    // 不可见，写操作结果被藏、输入框还禁用，用户可能误判未执行。
    // 确认与拒绝（含 re_requested 重挂）都应留在本轮视图看续传。
    setHistoryQuestionId(null)
    if (result.stream_id !== streamId) {
      setStreamId(result.stream_id) // 流切换时 useTaskStream 自动重置去重集合
    }
    setSubscribeEpoch((prev) => prev + 1) // 同流重新订阅（保留 Last-Event-ID 续传）
  }

  const stopStreaming = (): void => {
    stream.stop()
  }

  const busy = submitting || streamId !== null || chat.pendingConfirmation !== null

  // 新建对话后聚焦输入框：等 streamId 清空（textarea 解除禁用）后执行一次
  useEffect(() => {
    if (focusAfterResetRef.current && !busy) {
      focusAfterResetRef.current = false
      chatInputRef.current?.focus()
    }
  }, [busy, location.state])

  // [2026-09-09 布局重构] 「最新一问常驻置顶 + 本轮优先（历史收拢）」的
  // 消息切分（纯派生，不改 chat.messages 数据结构）：
  //   - lastUserIndex = 最后一条 role=user 的消息下标（倒序找）；
  //   - question      = 该消息 → 渲染在窗口顶部「当前问题」条；
  //   - current       = slice(lastUserIndex+1) → 本轮滚动区（回答/卡片）；
  //   - history       = slice(0, lastUserIndex) → 默认收起，入口切入。
  // 防御：messages 无 user（空会话 / 异常重放）→ 不渲染问题条，全部消息按
  // 本轮渲染（slice(lastUserIndex+1) 退化为 slice(0)）。
  const lastUserIndex = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      if (chat.messages[i].role === 'user') return i
    }
    return -1
  }, [chat.messages])
  const question = lastUserIndex >= 0 ? chat.messages[lastUserIndex] : null
  const currentMessages = useMemo(
    () => (lastUserIndex >= 0 ? chat.messages.slice(lastUserIndex + 1) : chat.messages),
    [chat.messages, lastUserIndex],
  )
  const historyMessages = useMemo(
    () => (lastUserIndex > 0 ? chat.messages.slice(0, lastUserIndex) : []),
    [chat.messages, lastUserIndex],
  )
  // 「查看更早对话（N 轮）」：N = 更早历史里的 user 消息条数（一轮 ≈ 一问）
  const historyUserCount = useMemo(
    () => historyMessages.reduce((n, m) => (m.role === 'user' ? n + 1 : n), 0),
    [historyMessages],
  )
  // 历史视图生效条件（渲染期派生而非独立 state，规格「新一轮发送强制切回
  // 本轮」无需 effect）：① 用户点过入口并记下当时的问题 id ② 当前问题 id
  // 未变（新一轮发送 / reset / clear_messages / restore → 问题 id 变化 →
  // 自动回本轮）③ 无挂起确认卡（确认卡必须在当前轮可见，防确认上下文被
  // 历史阅读打断）④ 确有更早历史。任一条破坏即回本轮视图。
  // [2026-09-09 review 修复] 局限：清卡（handleConfirm 的 clear_card）与
  // 发送失败回滚（doSubmit 的 remove_message 使 question 回落历史 id）两条
  // 路径上 question id 不变，派生条件会在上下文变化后的时刻**复活**、翻回
  // 历史视图把本轮反馈藏掉——由 handleConfirm / doSubmit 两处显式
  // setHistoryQuestionId(null) 兜底；id 维度安全（nextMessageId 不重复）之外
  // 不再依赖「不会复活」假设。切换视图时的滚动定位见下方两个 effect。
  const showingHistory =
    historyQuestionId !== null &&
    question !== null &&
    historyQuestionId === question.id &&
    chat.pendingConfirmation === null &&
    historyUserCount > 0

  // [2026-08-26 修复] 正文流式也滚底：此前只在生成开始与工具执行变化时
  // 滚底，模型思考结束进入正文输出阶段（agent_reply_chunk 流式增量）时
  // 页面停在思考位不跟随。取最后一条消息的 role/streaming/content 长度/
  // 任务号组合为 key：仅最新消息变化（正文流式增长、定稿）时滚底；历史
  // 消息变化（如确认卡状态）不滚。
  const lastMessageKey = useMemo(() => {
    const last = chat.messages[chat.messages.length - 1]
    return last ? `${last.role}:${last.streaming}:${last.content.length}:${last.taskId ?? ''}` : 'empty'
  }, [chat.messages])

  // [2026-08-26 修复] 依赖加入 lastMessageKey：正文输出阶段（agent_reply_chunk
  // 流式增量更新最后一条消息 content）也跟随滚底，不再停在思考位
  // [2026-09-01] 依赖补卡片状态：export_ready/reconcile_report 事件只设卡片，
  // 消息区收缩时不滚底会把用户消息裁出视口；卡片出现即滚
  // （2026-09-01 指示条移除：原「工具指示条出现/消失滚底」动机已不存在）
  // [2026-09-09 布局重构] 自动滚底只在**本轮视图**生效：历史视图用户在读
  // 历史，current 流式增量 / 卡片出现不得把它拉走（不自动滚）；showingHistory
  // 依赖保证离开历史视图时立即滚回本轮底部。
  useEffect(() => {
    if (showingHistory) return
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight })
  }, [
    lastMessageKey,
    chat.pendingConfirmation,
    chat.exportCard,
    chat.reconcileReport,
    showingHistory,
  ])

  // [2026-09-09 布局重构] 进入历史视图：一次滚到顶（从头读起；离开视图的
  // 滚底由上方 effect 的 showingHistory 依赖接管，不在本 effect 重复执行）。
  useEffect(() => {
    if (showingHistory) messagesRef.current?.scrollTo({ top: 0 })
  }, [showingHistory])

  return (
    <div className="chat-page">
      <section className="chat-page__main" aria-label="对话">
        {/* [2026-09-09 布局重构] 当前问题条：messages 中最后一条 user 消息
            （最新一问）常驻置顶，随新问题自动替换；无 user 消息（空会话 /
            首问前 / 异常重放）不渲染。强调样式与流内普通 user 行区分。 */}
        {question ? (
          <div className="chat-page__question" role="region" aria-label="当前问题">
            <span className="chat-page__question__label">当前问题</span>
            <p className="chat-page__question__text">{question.content}</p>
          </div>
        ) : null}

        {/* [2026-09-09 布局重构] 滚动容器**恒为同一个 DOM**（ChatMessageList
            常驻渲染，不随视图切换卸载重建，滚动位置语义稳定）：本轮视图
            渲染该问题之后的回答，历史视图渲染更早历史 + 顶部「返回当前
            对话」条。容器内容由 showingHistory 派生切换；滚动定位见滚动
            effect 注释（自动滚底仅本轮生效、切历史滚顶、回本轮滚底）。 */}
        <ChatMessageList
          messages={showingHistory ? historyMessages : currentMessages}
          scrollRef={messagesRef}
          toolbar={
            showingHistory ? (
              <div className="chat-page__history-top">
                <button
                  type="button"
                  className="btn"
                  onClick={() => setHistoryQuestionId(null)}
                >
                  返回当前对话
                </button>
              </div>
            ) : undefined
          }
          /* [2026-09-10 卡片入流] 三张卡经 footer 槽渲染在消息区滚动容器**内部**、
             消息之后（此前是容器之后的兄弟节点，被 flex 列挤到输入框正上方、
             与助手回复行不同宽不顶格）。仍只在本轮视图渲染；历史视图不传 footer。
             `aria-live="off"`：把卡片子树排除出容器继承的 polite 播报——卡内是
             大块可交互内容（六要素表格 + 确认/拒绝按钮），整体自动播报且任何内部
             状态变化（按钮「确认中…」、就地错误、展开/折叠）都会反复触发，过于
             啰嗦。卡仍在 DOM 序中紧随消息、按钮带可访问名称、表格有 th scope，
             读屏可正常导航；ConfirmationPanel 内 role="alert" 的「确认已失效」
             提示为元素自身隐式 live，不受祖先 off 影响，关键变更仍会播报。 */
          footer={
            showingHistory ? undefined : (
              <>
                {/* 挂起确认卡：写入计划六要素 + 确认/拒绝 */}
                {chat.pendingConfirmation ? (
                  <div className="chat-inline-card" aria-live="off">
                    <ConfirmationPanel
                      confirmation={chat.pendingConfirmation}
                      onConfirm={(approved, payloadHash) => handleConfirm(approved, payloadHash)}
                    />
                  </div>
                ) : null}

                {/* [2026-08-31 批次 4] 对账差异报告卡：摘要 + 查看明细
                    （SSE reconcile_report 事件驱动，明细按需拉 GET /chat/reports） */}
                {chat.reconcileReport ? (
                  <div className="chat-inline-card" aria-live="off">
                    <ReconcileReportCard report={chat.reconcileReport} threadId={threadId ?? ''} />
                  </div>
                ) : null}

                {/* [2026-09-01 导出功能批次] 导出完成卡（SSE export_ready 事件
                    驱动；下载走 GET /chat/export/{file_id}） */}
                {chat.exportCard ? (
                  <div className="chat-inline-card" aria-live="off">
                    <ExportCard exportInfo={chat.exportCard} />
                  </div>
                ) : null}
              </>
            )
          }
        />

        {/* 本轮视图的错误提示与 503 重试入口：仍在滚动容器之外（错误/重试
            不需随消息滚动）；历史视图期间数据仍在 chat 中，返回即恢复。 */}
        {!showingHistory ? (
          <>
            <ErrorNotice error={historyError} />
            <ErrorNotice error={sendError} />
            {sendError?.errorCode === ERROR_CODES.SERVICE_UNAVAILABLE ? (
              <div className="chat-composer__retry" role="alert">
                <p className="panel__muted">
                  系统正在优雅停机，暂不接受新写任务；你的输入已保留，系统恢复后可直接重试。
                </p>
                <button type="button" className="btn" onClick={() => void doSubmit()}>
                  重试发送
                </button>
              </div>
            ) : null}
            <ErrorNotice error={stream.error} />
          </>
        ) : null}

        {/* [2026-09-09 布局重构] 历史收拢入口：更早历史默认收起（本轮视图时
            显示，位于滚动区与输入区之间）。N = 更早问答轮数；挂起确认卡期间
            禁用并提示——先处理卡，防确认上下文被历史阅读打断。 */}
        {!showingHistory && historyUserCount > 0 ? (
          <div className="chat-page__history-toggle">
            <button
              type="button"
              className="btn"
              disabled={chat.pendingConfirmation !== null}
              title={chat.pendingConfirmation !== null ? '请先处理确认卡' : undefined}
              onClick={() => {
                if (question) setHistoryQuestionId(question.id)
              }}
            >
              查看更早对话（{historyUserCount} 轮）
            </button>
          </div>
        ) : null}

        <ChatComposer
          content={content}
          onContentChange={setContent}
          file={file}
          onFileChange={handleFileChange}
          busy={busy}
          submitting={submitting}
          onSubmit={() => {
            void doSubmit()
          }}
          inputRef={chatInputRef}
          // [15.15 常亮] agent 工作全程（connecting/streaming/reconnecting）暂停键
          // 恒显示可点（stop 幂等：abort + 后端 cancel best effort）；挂起确认卡
          // （suspended）期不显示——用户在操作卡片，非工作态；done/error 不显示。
          // POST 返回后首帧 status 可能仍为 idle（订阅 effect 未跑，setStreamId
          // 后一帧才置 connecting），新表达式不排除 idle——该瞬态帧本就该显
          // 暂停键，全程无缝；勿按此注释补 status !== 'idle' 排除（会引入一帧闪）。
          canStop={
            streamId !== null &&
            !stream.suspended &&
            stream.status !== 'done' &&
            stream.status !== 'error'
          }
          onStop={stopStreaming}
        />
      </section>
    </div>
  )
}
