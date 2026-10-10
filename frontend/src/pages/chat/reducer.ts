/**
 * 对话状态 reducer（2026-08-29 从 ChatPage.tsx 的 applyEvent 抽出）。
 *
 * ## 为什么抽出
 * 原实现是 ChatPage 内的 213 行 switch，通过闭包调用三个 useState setter
 * （消息/挂起卡/工具条）。要测其中任何一个分支，都必须渲染整棵 ChatPage
 * 并驱动一条真实的 SSE 流。抽成纯函数后，每个事件分支可以直接喂事件断言
 * 结果。
 *
 * ## 与旧实现的行为对照（S4 接入时逐条核对过）
 *
 * | 分支 | 旧实现 | 本 reducer | 等价性 |
 * |---|---|---|---|
 * | agent_reply(next_action=call_tool) | 移除末尾流式气泡 | 同 | 等价 |
 * | agent_reply(终局) | 末尾流式气泡整段替换并落定；无流式气泡且 reply_text 非空则 push | 同上；reply_text 为空时删除流式气泡（有挂起兜底则渲染兜底） | 有意偏离（见下） |
 * | agent_reply_start | 落定全部流式气泡 + push 空气泡 | 同 | 等价 |
 * | agent_reply_chunk | **rAF 合并后整段追加** | **不写入 content**（仅确保流式气泡存在） | 有意偏离（见下） |
 * | agent_error | 落定全部流式气泡 + push 错误文案 | 终局码 dropTrailingStreaming + push；有兜底时兜底与终局原因合并进同一气泡；history_truncated 非终局不产气泡 | 有意偏离（见下） |
 * | confirmation_requested | card/task_id/tool_name 齐全才挂卡 | 同 | 等价 |
 * | tool_result | 清卡；ok=false 时立即 push 失败/拒绝文案 | 清卡；ok=false 记 ``turnFallback`` 挂起，由本轮终局事件裁决 | 有意偏离（见下） |
 * | task_failed | 落定 + push 失败文案 | dropTrailingStreaming 丢弃空流式气泡 + push 失败文案；有兜底时与兜底合并进同一气泡 | 有意偏离（见下，2026-09-13 P1） |
 * | task_cancelled | 落定 + push 取消文案 | 同 task_failed（dropTrailingStreaming + push 取消文案；有兜底时合并） | 有意偏离（见下，2026-09-13 P1） |
 *
 * ## [2026-09-10 单气泡收敛] 上表「有意偏离」的说明
 * 用户实测「某些场景前端显示两条 agent 回复」，根因是终局模型回复与代码
 * 文案并存。修订后每条已结束的 assistant 回合恰有一条气泡：
 * - ``agent_error``：终局码到达时 dropTrailingStreaming（移除 agent_reply_
 *   start 开出的空气泡/半截增量）再 push 代码文案，避免空白行/两条并存。
 *   **有挂起兜底时把兜底文案与终局原因（AGENT_ERROR_TEXT / EXECUTED + 工具名
 *   + detail）用换行合并进同一气泡**，兜底不再吞掉可操作指引（如
 *   steps_exhausted 的「请发送新消息继续」）。``history_truncated`` 属
 *   **非终局**（后端随后仍会生成模型回复），不产生任何可见气泡。
 * - ``tool_result``(ok=false)：不再无条件 push 代码文案，改为记
 *   ``turnFallback`` 挂起态，由本轮终局事件裁决（模型回复→丢弃；终局错误
 *   →渲染/合并）。
 * - ``agent_reply``(终局且 reply_text 为空)：删除流式气泡（旧实现会落定为
 *   空白 assistant 行），有挂起兜底则渲染兜底，确保不留空白、不丢文案。
 * - ``task_failed`` / ``task_cancelled``：与「合并」同口径——兜底存在时与
 *   自身文案换行拼接（去重：相同或自身文案为空则不重复），不静默丢弃。
 *
 * ## [2026-09-13 整轮动效] 增量为何不再写进气泡
 * 用户口径：助手气泡从发消息起、到最终答复到达为止**一直**显示生成中动效，
 * 整轮期间气泡内不出现任何文本，终局一次性整段渲染 markdown。为此增量内容
 * 不再累积进 content——`agent_reply_chunk` 只确保末尾存在一个流式气泡
 * （兼容缺 `agent_reply_start` 的历史重放/迟到 chunk），不动 content。
 *
 * 连带影响：原「ref 缓冲 + requestAnimationFrame 合并高频 chunk 成每帧一次
 * 渲染」的调度层（ChatPage 的 handleStreamEvent / append_reply）已无意义
 * ——既然整轮不渲染文本，逐 chunk 与合并后的**可见内容**相同，故一并移除。
 *
 * [2026-09-13 P2 修复] 但「内容不产生差异」不等于「不产生渲染」：chunk 分支
 * 若仍构造新 state 对象，useReducer 的 ``Object.is`` bailout 失效，每个 chunk
 * 都会重渲染整棵 ChatPage（token 粒度可达数百上千条）。因此末尾已有流式气泡
 * 时**直接返回原 state 引用**——高频 chunk 在无状态变化时不触发任何渲染，与
 * 原 rAF 合并层的收益等价（且不必再维护调度队列）。
 *
 * ## [2026-09-28 确认卡回执一致性] prepare 失败撤销乐观文案
 * 写工具 ``spec.prepare`` 失败（无卡）时，模型在同一回合决策轮已说出的
 * 「请点击确认卡完成写入」当时无从知道会失败（提示词改不动该形态）——前端
 * 拿到 ``agent_error``（``PREPARE_FAILURE_AGENT_ERROR_CODES``）后：
 * - 错误气泡文案经 ``AGENT_ERROR_TEXT.preparation_error`` 显式点明「本次未生成
 *   确认卡」；
 * - 本回合内（最后一条 user 之后）以已落定正文形态残留的「请点击确认卡」乐观
 *   文案由 ``revokeOptimisticCardPromise`` 就地追加可见作废说明（不删除原文）。
 * 只影响 prepare 失败路径；正常出卡 / 确认成功 / 确认拒绝 / task_failed /
 * task_cancelled 路径逐字不变（各有既有用例钉着）。
 *
 * [2026-09-28 非终局补正] ``preparation_error`` 同属**非终局**（后端图继续
 * 到 model 生成一条解释性 ``agent_reply``，见 constants 的
 * NON_TERMINAL_AGENT_ERROR_CODES）——本 reducer 仍照常渲染错误气泡（它不在
 * SILENT_NON_TERMINAL_AGENT_ERROR_CODES 内），随后模型回复到达时另起一条：
 * 错误气泡与模型回复**两条并存**、互不覆盖。
 *
 * ## 关于 nextMessageId 的纯度
 * 本模块调用 `nextMessageId()`（模块级自增计数器），严格说不是纯函数。
 * 这是既有设计（id 需在测试中可预期，且 randomUUID 依赖安全上下文）。
 * 测试断言应比对 content / role / streaming，不要断言具体 id 值。
 */
import type { PendingConfirmation, StreamEventOut } from '../../api/types'
import {
  AGENT_ERROR_EXECUTED_TEXT,
  AGENT_ERROR_TEXT,
  CARD_NOT_GENERATED_NOTICE,
  CARD_PROMISE_PATTERN,
  GENERATION_CANCELLED_TEXT,
  PREPARE_FAILURE_AGENT_ERROR_CODES,
  SILENT_NON_TERMINAL_AGENT_ERROR_CODES,
  STREAM_ABORTED_ERROR_TEXT,
  TOOL_RESULT_REJECTED_TEXT,
  toolResultFailedText,
} from './constants'
import type { ChatMessage, ExportCardInfo, ReconcileReport } from './types'
import { nextMessageId } from './types'

/**
 * [2026-09-10 单气泡收敛] 本轮「代码兜底文案」挂起态。
 *
 * tool_result(ok=false)（拒绝/执行失败）到达时记下，**不立即插气泡**——此刻
 * 无法预知本轮最终以模型回复还是终局错误收尾。裁决发生在本轮的终局事件上：
 * - 终局 ``agent_reply``（模型回复）→ 丢弃兜底，只留模型回复；
 * - 终局 ``agent_error``（模型不可用/步数耗尽/图外安全网）→ 渲染兜底，
 *   只留代码回复（且优先于 ``tool_executed=true`` 的"任务已执行完成"文案）。
 */
export interface TurnFallback {
  text: string
  taskId?: string
}

/** 对话状态（原 ChatPage 的三个 useState 收敛为一个整体）。 */
export interface ChatState {
  messages: ChatMessage[]
  pendingConfirmation: PendingConfirmation | null
  /** [2026-08-31 批次 4] 对账差异报告摘要（SSE reconcile_report 事件）。
   *  清理时机：新事件覆盖；写执行完成（tool_result ok）；下一条用户消息
   *  发送（add_user_message）；切换会话（clear_messages）；reset。 */
  reconcileReport: ReconcileReport | null
  /** [2026-09-01 导出功能批次] 导出完成摘要（SSE export_ready 事件）。
   *  清理时机：新导出覆盖；写执行完成（tool_result ok）；下一条用户消息
   *  发送（add_user_message）；切换会话（clear_messages）；reset。 */
  exportCard: ExportCardInfo | null
  /** [2026-09-10 单气泡收敛] 本轮代码兜底文案挂起态（见 TurnFallback）。
   *  清理时机：本轮终局事件（agent_reply 丢弃 / agent_error 渲染并清空）；
   *  写执行成功（tool_result ok）；新一轮（add_user_message/reset/
   *  restore/clear_messages）。 */
  turnFallback: TurnFallback | null
}

export const initialChatState: ChatState = {
  messages: [],
  pendingConfirmation: null,
  reconcileReport: null,
  exportCard: null,
  turnFallback: null,
}

/**
 * 动作分两类：
 * - `event`：后端流事件，按 event_type 分派；
 * - 编排层指令（新建对话、切换会话、历史重放、乐观气泡增删），原来由
 *   ChatPage 直接调用三个 useState setter（消息/挂起卡/工具条）完成。
 *
 * [2026-09-13 整轮动效] 原 `append_reply`（调度层 rAF 合并后投递的整段增量）
 * 随 rAF 调度层一并移除：增量不再影响渲染，chunk 事件直接进 reducer 即可。
 *
 * 编排指令刻意做成细粒度而非一个 `replace_all`：这样每个指令的副作用
 * 范围与旧实现的 setState 调用一一对应，便于核对等价性。
 */
export type ChatAction =
  | { type: 'event'; event: StreamEventOut }
  /** 新建对话：消息/卡全部清空。 */
  | { type: 'reset' }
  /** 历史重放：整体替换消息与挂起卡。 */
  | { type: 'restore'; messages: ChatMessage[]; pendingConfirmation: PendingConfirmation | null }
  /** 发送时的乐观用户气泡。 */
  | { type: 'add_user_message'; message: ChatMessage }
  /** 发送失败时按 id 精确移除乐观气泡。 */
  | { type: 'remove_message'; id: string }
  /** 切换会话但仍是同一线程：只清消息，不清草稿（旧实现同）。 */
  | { type: 'clear_messages' }
  | { type: 'clear_card' }
  /**
   * [2026-09-13 流异常收尾] 客户端侧流终止（useTaskStream 的 status 进入
   * done/error）时由 ChatPage 的落点补发。两类客户端路径下 reducer **收不到**
   * 任何终局流事件（不发这些事件就停止投递）：
   * - 用户点「暂停」→ stop() 置 stoppedRef 并 abort，无 task_cancelled；
   * - 非 2xx 响应 → fail() 置 error 并停止重连，无终局事件。
   * 二者都会让 ``agent_reply_start`` 开出的流式气泡永久转圈，故在此收尾。
   * reason 仅供选文案：'cancelled'（暂停/会话过期）/'error'（请求或流故障）。
   */
  | { type: 'stream_aborted'; reason: 'cancelled' | 'error' }

/** 落定所有流式气泡（终态分支的公共前奏）。 */
function settleStreaming(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) => (m.streaming ? { ...m, streaming: false } : m))
}

/**
 * [2026-09-10 单气泡收敛] 移除末尾的流式气泡。
 *
 * 终局错误分支用它取代 ``settleStreaming``：``agent_reply_start`` 开出的
 * 空气泡（等待动画）与带部分增量文本的流式气泡**一律**让位于代码文案——
 * 目标行为是"终局错误只留代码回复"，若只落定（streaming=false）则空气泡
 * 会以空白 assistant 行残留、增量气泡会与代码文案并存（用户看到两条）。
 * 只影响末尾流式气泡，不触碰更早轮次的已落定消息。
 */
function dropTrailingStreaming(messages: ChatMessage[]): ChatMessage[] {
  const last = messages[messages.length - 1]
  return last?.role === 'assistant' && last.streaming ? messages.slice(0, -1) : messages
}

/**
 * [2026-09-28 确认卡回执一致性] prepare 失败 → 撤销本回合内的「请点击确认卡」
 * 乐观文案（不静默删除）。
 *
 * 背景（考勤轮 C1 实测）：ERP 有同名员工时 `spec.prepare` 抛错，图内
 * ``pending_confirmation`` 不设（**无卡**），但模型在**同一回合的决策轮**
 * （工具尚未运行）已说出「写入计划如下……请点击确认卡完成写入」。该句当时
 * 无从知道后面会失败，提示词改不动这个形态；前端必须在拿到 prepare 失败事件
 * 后把它纠正/撤销。
 *
 * 行为：把**最后一条 user 消息之后**（本轮，绝不回溯改写更早历史）、正文承诺
 * 了「点击确认卡」（``CARD_PROMISE_PATTERN``）的助手消息**就地**追加可见作废
 * 说明（``CARD_NOT_GENERATED_NOTICE``）——保留原文（用户已看到的不得凭空消失）
 * 并显式说明卡片未生成。已带该说明的（幂等）不重复追加。无此形态时原样返回
 * 同一数组引用（不触发无谓重渲染）。
 *
 * 注：``agent_reply(next_action=call_tool)`` 的过渡文案本就被 reducer 丢弃
 * （见 agent_reply 分支），故正常路径下本轮没有可撤销的乐观消息，本函数是
 * no-op；它兜住的是「乐观文案以已落定正文形态留在本轮可见区」的形态（例如
 * 历史重放/未来展示口径变化）。
 */
function revokeOptimisticCardPromise(messages: ChatMessage[]): ChatMessage[] {
  let lastUserIndex = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') {
      lastUserIndex = i
      break
    }
  }
  // [2026-09-28 复审 P3] 无 user 消息时 lastUserIndex 保持 -1，而下方边界
  // ``i <= lastUserIndex`` 对任何 i>=0 都不成立 ⇒ 会把**整段历史**纳入改写
  // 范围，与本函数 docstring（「最后一条 user 消息之后，绝不回溯改写更早
  // 历史」）矛盾。无本轮起点即无可撤销的本轮正文，原样返回同一引用。
  if (lastUserIndex < 0) return messages
  let changed = false
  const next = messages.map((m, i) => {
    if (i <= lastUserIndex || m.role !== 'assistant') return m
    if (m.content.includes(CARD_NOT_GENERATED_NOTICE)) return m // 幂等
    if (!CARD_PROMISE_PATTERN.test(m.content)) return m
    changed = true
    return { ...m, content: `${m.content}\n\n${CARD_NOT_GENERATED_NOTICE}` }
  })
  return changed ? next : messages
}

/**
 * [2026-09-10 单气泡收敛] 把「代码兜底文案」与「终局文案」合并为同一条气泡
 * 正文（换行拼接）。二者相同、或终局文案为空时不重复拼接（只留兜底）。
 */
function mergeTurnText(fallback: string | undefined, terminal: string): string {
  if (!fallback) return terminal
  if (!terminal || terminal === fallback) return fallback
  return `${fallback}\n${terminal}`
}

/**
 * [2026-09-13 整轮动效] 确保末尾存在一个流式（生成中）气泡；已有则原样返回。
 *
 * 取代原 `appendToStreaming`：增量内容不再写入 content（整轮只显示动效），
 * 此函数只负责「气泡在场」——正常路径由 agent_reply_start 开出，此处兜底
 * 缺 start 的历史重放/迟到 chunk 场景。content 始终为空，终局 agent_reply
 * 整段填入。
 */
function ensureStreamingBubble(messages: ChatMessage[]): ChatMessage[] {
  const last = messages[messages.length - 1]
  if (last?.role === 'assistant' && last.streaming) return messages
  return [...messages, { id: nextMessageId(), role: 'assistant', content: '', streaming: true }]
}

/** 防御性提取分类计数（summary_counts 为 dict[classification, int]）。 */
function toCountMap(value: unknown): Record<string, number> {
  const counts: Record<string, number> = {}
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return counts
  for (const [key, count] of Object.entries(value as Record<string, unknown>)) {
    if (typeof count === 'number') counts[key] = count
  }
  return counts
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  // ---- 编排层指令（与流事件无关，故置于事件分派之前）----
  switch (action.type) {
    case 'reset':
      return initialChatState
    case 'restore':
      return {
        ...state,
        messages: action.messages,
        pendingConfirmation: action.pendingConfirmation,
        turnFallback: null,
      }
    case 'add_user_message':
      // 下一条用户消息发送：上一轮的对账报告卡/导出卡不再展示（事件在
      // 下一轮完成时会重新覆盖）；上一轮未裁决的代码兜底一并作废
      return {
        ...state,
        messages: [...state.messages, action.message],
        reconcileReport: null,
        exportCard: null,
        turnFallback: null,
      }
    case 'remove_message':
      return { ...state, messages: state.messages.filter((m) => m.id !== action.id) }
    case 'clear_messages':
      return {
        ...state,
        messages: [],
        reconcileReport: null,
        exportCard: null,
        turnFallback: null,
      }
    case 'clear_card':
      return { ...state, pendingConfirmation: null }
    case 'stream_aborted': {
      // [2026-09-13 流异常收尾] 仅当末尾确有**未落定**的流式气泡时才收尾：
      // 丢弃该空气泡（整轮动效下其 content 恒为空，落定会留空白行）并补一条
      // 可见短文案——比静默删除（这一轮毫无反馈）体验更佳。
      //
      // 幂等性（本修复最大风险点）：正常终局（agent_reply/agent_error/
      // task_failed/task_cancelled）已把气泡落定（streaming=false）或删除，
      // 而 ChatPage 对**所有** done/error 落点都会补发本指令——此时末尾不存在
      // streaming 气泡，直接原样返回 state，绝不动已落定的最终答复。
      const last = state.messages[state.messages.length - 1]
      if (!(last?.role === 'assistant' && last.streaming)) return state
      const text =
        action.reason === 'error' ? STREAM_ABORTED_ERROR_TEXT : GENERATION_CANCELLED_TEXT
      return {
        ...state,
        messages: [
          ...dropTrailingStreaming(state.messages),
          {
            id: nextMessageId(),
            role: 'assistant',
            // 与 task_failed/task_cancelled 同口径：有挂起兜底时换行合并，
            // 不静默丢弃（如 tool_result 失败后随即点暂停）。
            content: mergeTurnText(state.turnFallback?.text, text),
            taskId: state.turnFallback?.taskId,
          },
        ],
        turnFallback: null,
      }
    }
    default:
      break
  }

  const event = action.event
  const payload = event.payload as Record<string, unknown>
  const taskId = event.task_id ?? undefined

  switch (event.event_type) {
    case 'agent_reply': {
      // [2026-08-25 loop 不展示] call_tool 过渡轮：不落定对话气泡——
      // 移除最后一条流式气泡（agent_reply_start 开出的生成中气泡整个丢弃）。
      // needs_input 追问与 reply 终局照常展示（前端只看到 agent 最终回答）。
      if (payload.next_action === 'call_tool') {
        const last = state.messages[state.messages.length - 1]
        const messages =
          last?.role === 'assistant' && last.streaming
            ? state.messages.slice(0, -1)
            : state.messages
        return { ...state, messages }
      }
      // 终局轮回复（含 needs_input 提问——回答=对话内下一条消息）。
      // 流式路径：最后一条为 streaming 气泡 → 整段终态替换增量文本并落定；
      // 兼容路径（历史重放/无流式事件）：维持 push 整段逻辑
      const replyText = typeof payload.reply_text === 'string' ? payload.reply_text : ''
      const last = state.messages[state.messages.length - 1]
      // 下方 if/else 各分支均已赋值 messages（无分支直接读取初值），故不设初值，
      // 仅作类型声明（否则 eslint no-useless-assignment 报无用的赋值）。
      let messages: ChatMessage[]
      if (replyText) {
        if (last?.role === 'assistant' && last.streaming) {
          messages = [
            ...state.messages.slice(0, -1),
            { ...last, content: replyText, streaming: false },
          ]
        } else {
          messages = [
            ...state.messages,
            { id: nextMessageId(), role: 'assistant', content: replyText, taskId },
          ]
        }
      } else {
        // [2026-09-10 单气泡收敛] 终局 reply_text 为空：删除 agent_reply_start
        // 开出的流式气泡——旧实现会把它落定为 content:'' 的 assistant 行，
        // 渲染成空白气泡。若有挂起的代码兜底（tool_result 拒绝/失败），渲染
        // 兜底而不是清掉后什么都不留（至少让用户看到"已拒绝/执行失败"）。
        messages = dropTrailingStreaming(state.messages)
        if (state.turnFallback) {
          messages = [
            ...messages,
            {
              id: nextMessageId(),
              role: 'assistant',
              content: state.turnFallback.text,
              taskId: state.turnFallback.taskId ?? taskId,
            },
          ]
        }
      }
      const pendingConfirmation =
        taskId && state.pendingConfirmation?.task_id === taskId ? null : state.pendingConfirmation
      // [2026-09-10 单气泡收敛] 终局模型回复到达 → 本轮代码兜底已被裁决
      // （非空回复丢弃兜底；空回复已渲染兜底），统一清空挂起态。
      return { ...state, messages, pendingConfirmation, turnFallback: null }
    }

    case 'agent_reply_start':
      // 每轮模型生成开始：先落定所有既有流式气泡（中间轮/新轮不串字），
      // 再开一个空流式气泡（[2026-09-13] content 整轮保持空 → 全程只显示
      // 生成中动效，终局 agent_reply 才整段填入并落定）
      return {
        ...state,
        messages: [
          ...settleStreaming(state.messages),
          { id: nextMessageId(), role: 'assistant', content: '', streaming: true },
        ],
      }

    case 'agent_reply_chunk': {
      // [2026-09-13 整轮动效] 回复文本增量：**不再写入气泡 content**——整轮
      // 期间只显示生成中动效，终局 agent_reply 整段渲染。此处仅确保流式气泡
      // 存在（兜底缺 start 事件的历史重放/迟到 chunk 场景），空 chunk 原样返回。
      const chunk = typeof payload.chunk === 'string' ? payload.chunk : ''
      if (!chunk) return state
      // [2026-09-13 P2] 末尾已有流式气泡（正常路径）→ 无任何状态变化，直接返回
      // **原 state 引用**：useReducer 的 Object.is bailout 生效，高频 chunk
      // 不再逐条触发整页重渲染。仅缺 start 的兜底路径才构造新 state。
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'assistant' && last.streaming) return state
      return { ...state, messages: ensureStreamingBubble(state.messages) }
    }

    case 'agent_error': {
      const code = typeof payload.error === 'string' ? payload.error : 'UNKNOWN'
      // [2026-09-10 单气泡收敛] **不产可见气泡**的非终局 agent_error
      // （SILENT_NON_TERMINAL_AGENT_ERROR_CODES，当前仅 history_truncated）：
      // 后端发完该事件后同一模型轮仍会生成终局 agent_reply —— 这里既不插
      // 代码气泡（否则模型回复到达后成两条），也不落定/删除既有气泡
      // （无可见副作用），原样返回。
      // [2026-09-28] preparation_error 虽**也是**非终局（见
      // NON_TERMINAL_AGENT_ERROR_CODES），但**不在**此表：它必须渲染错误
      // 气泡（携 detail 与「本次未生成确认卡」），随后模型回复到达时**另起**
      // 一条气泡（两条并存、互不覆盖）——本分支下落到下面正常渲染。
      if (SILENT_NON_TERMINAL_AGENT_ERROR_CODES.has(code)) return state
      const toolName = typeof payload.tool_name === 'string' ? payload.tool_name : null
      // [15.14] 后端 preparation_error 等事件带 detail（str(exc)，如分类归属
      // 校验失败附可用清单）——非空时拼进正文（换行追加），用户能看到
      // 具体失败原因，而不是只见通用文案
      const detail = typeof payload.detail === 'string' && payload.detail ? payload.detail : null
      // [2026-09-10 单气泡收敛] 终局错误分支统一 dropTrailingStreaming：
      // agent_reply_start 开出的空气泡/半截增量让位于代码文案，只留一条。
      const baseMessages = dropTrailingStreaming(state.messages)
      // [2026-09-28 确认卡回执一致性] prepare 失败（无卡）→ 撤销本回合内已落定
      // 的「请点击确认卡」乐观文案（就地加可见作废说明，不删除）；错误气泡
      // 文案本身已在 AGENT_ERROR_TEXT.preparation_error 点明「本次未生成确认卡」。
      // 只在本类码上生效——正常出卡 / 确认成功 / 确认拒绝 / task_failed /
      // task_cancelled 是别的路径，一律不受影响。
      const correctedMessages = PREPARE_FAILURE_AGENT_ERROR_CODES.has(code)
        ? revokeOptimisticCardPromise(baseMessages)
        : baseMessages
      // [2026-08-28 文案区分][2026-09-09 steps_exhausted 同构] agent_error
      // 且 payload.tool_executed=true（本回合写工具已执行落 ERP）→ 用
      // "任务已执行完成"覆盖文案（任务已生效但结果总结未生成，提示继续询问），
      // 否则走通用 AGENT_ERROR_TEXT 文案
      const executedText =
        payload.tool_executed === true ? AGENT_ERROR_EXECUTED_TEXT[code] : undefined
      const text = executedText ?? (AGENT_ERROR_TEXT[code] ?? `处理失败（${code}）。`)
      const labeled = toolName ? `${text}（工具 ${toolName}）` : text
      const reasonText = detail ? `${labeled}\n${detail}` : labeled
      // [2026-09-10 单气泡收敛] 本轮若挂起过代码兜底（tool_result 拒绝/失败）
      // → 与终局原因**合并进同一气泡**（换行拼接，见 mergeTurnText）：既不
      // 丢失兜底（"已拒绝/执行失败"），也不吞掉终局可操作指引（如
      // steps_exhausted 的"请发送新消息继续"）。taskId 优先沿用兜底携带的。
      return {
        ...state,
        messages: [
          ...correctedMessages,
          {
            id: nextMessageId(),
            role: 'assistant',
            content: mergeTurnText(state.turnFallback?.text, reasonText),
            taskId: state.turnFallback?.taskId ?? taskId,
          },
        ],
        turnFallback: null,
      }
    }

    case 'confirmation_requested': {
      // 内联确认卡（挂起；非终态——流保持轮询，确认后事件续传）。
      // hash 失效重挂：re_requested=true，卡内容以新 payload_hash 为准
      const card = payload.card as PendingConfirmation['card'] | undefined
      const payloadHash = typeof payload.payload_hash === 'string' ? payload.payload_hash : ''
      const toolName = typeof payload.tool_name === 'string' ? payload.tool_name : ''
      const cardTaskId = typeof payload.task_id === 'string' ? payload.task_id : ''
      const pendingConfirmation =
        card && cardTaskId && toolName
          ? {
              task_id: cardTaskId,
              tool_name: toolName,
              card,
              payload_hash: payloadHash,
              re_requested: payload.re_requested === true,
            }
          : state.pendingConfirmation
      return { ...state, pendingConfirmation }
    }

    case 'tool_result': {
      // 确认后执行结果：清挂起卡。成功时 agent_reply 续上（模型总结）；
      // [2026-09-10 单气泡收敛] 失败/拒绝**不再立即插代码气泡**，只记
      // turnFallback 挂起态——本轮最终以模型回复收尾则丢弃（只留模型回复），
      // 以终局错误收尾则渲染（只留代码回复）。理由：此事件到达时前端无法
      // 预知本轮最终会以模型回复还是错误收尾。
      const ok = payload.ok === true
      const status = typeof payload.status === 'string' ? payload.status : null
      const pendingConfirmation =
        taskId && state.pendingConfirmation?.task_id === taskId ? null : state.pendingConfirmation
      const cleared: ChatState = { ...state, pendingConfirmation }
      if (ok) {
        // 对账写执行完成：报告卡使命结束；导出卡同清（新一轮写执行后旧导出不再展示）
        return { ...cleared, reconcileReport: null, exportCard: null, turnFallback: null }
      }
      const toolName = typeof payload.tool_name === 'string' ? payload.tool_name : ''
      const text = status === 'cancelled' ? TOOL_RESULT_REJECTED_TEXT : toolResultFailedText(toolName)
      // 防御性移除末尾流式气泡：确认轮到达时通常已无（call_tool 过渡轮已移除），
      // 若有（异常序列）也不得残留——它会让位于后续模型回复或代码兜底。
      return {
        ...cleared,
        messages: dropTrailingStreaming(state.messages),
        turnFallback: { text, taskId },
      }
    }

    case 'reconcile_report': {
      // [2026-08-31 批次 4] 对账分析完成（reconcile_analyze）：报告摘要卡。
      // payload 键名以 tools/reconciliation.py ctx.sink("reconcile_report", …)
      // 实际发射为准：{report_id, summary_counts, row_count, subject_match_status}；
      // 新事件覆盖旧报告（一次对话只展示最新一份）。
      const reportId = typeof payload.report_id === 'string' ? payload.report_id : ''
      if (!reportId) return state
      return {
        ...state,
        reconcileReport: {
          reportId,
          summaryCounts: toCountMap(payload.summary_counts),
          rowCount: typeof payload.row_count === 'number' ? payload.row_count : 0,
          subjectMatchStatus:
            typeof payload.subject_match_status === 'string' ? payload.subject_match_status : '',
        },
      }
    }

    case 'export_ready': {
      // [2026-09-01 导出功能批次] 导出完成（export_data 成功落盘）：导出
      // 卡片（下载链接走 GET /chat/export/{file_id}）。payload 键名以
      // agent/tools/export.py ctx.sink("export_ready", …) 实际发射为准：
      // {file_id, filename, entity, row_count, total, expires_at}；
      // 新导出覆盖旧卡片（一次对话只展示最新一份）。
      const fileId = typeof payload.file_id === 'string' ? payload.file_id : ''
      if (!fileId) return state
      return {
        ...state,
        exportCard: {
          fileId,
          filename: typeof payload.filename === 'string' ? payload.filename : `${fileId}.xlsx`,
          entity: typeof payload.entity === 'string' ? payload.entity : '',
          rowCount: typeof payload.row_count === 'number' ? payload.row_count : 0,
          total: typeof payload.total === 'number' ? payload.total : 0,
          expiresAt: typeof payload.expires_at === 'string' ? payload.expires_at : '',
        },
      }
    }

    case 'task_failed': {
      const code =
        typeof payload.final_error_code === 'string' ? payload.final_error_code : 'UNKNOWN'
      const summary =
        typeof payload.final_error_summary === 'string' ? payload.final_error_summary : null
      const ownText = summary ? `${summary}（${code}）` : `任务执行失败（${code}）。`
      return {
        ...state,
        // [2026-09-13 P1] 用 dropTrailingStreaming 取代 settleStreaming：整轮
        // 动效下流式气泡 content 恒为空，只落定会留下一条空白 assistant 行；
        // 丢弃它与 agent_error/tool_result 口径一致，每条结束回合恰一条气泡。
        messages: [
          ...dropTrailingStreaming(state.messages),
          {
            id: nextMessageId(),
            role: 'assistant',
            // [2026-09-10 单气泡收敛] 选口径 (i)：与 agent_error 同口径，有
            // 挂起兜底时合并进同一气泡（换行拼接、去重），不静默丢弃。
            content: mergeTurnText(state.turnFallback?.text, ownText),
            taskId: state.turnFallback?.taskId ?? taskId,
          },
        ],
        turnFallback: null,
      }
    }

    case 'task_cancelled': {
      const ownText =
        typeof payload.reason === 'string' && payload.reason !== 'generation_cancelled'
          ? `任务已取消（${String(payload.reason)}）。`
          : GENERATION_CANCELLED_TEXT
      return {
        ...state,
        // [2026-09-13 P1] 同 task_failed：dropTrailingStreaming 丢弃空流式气泡，
        // 不留空白 assistant 行（此前 settleStreaming 必产两条、首条为空）。
        messages: [
          ...dropTrailingStreaming(state.messages),
          {
            id: nextMessageId(),
            role: 'assistant',
            // [2026-09-10 单气泡收敛] 同 task_failed：口径 (i)，有兜底时合并，
            // 不静默丢弃。
            content: mergeTurnText(state.turnFallback?.text, ownText),
            taskId: state.turnFallback?.taskId ?? taskId,
          },
        ],
        turnFallback: null,
      }
    }

    default:
      // agent_started 不驱动界面；SESSION_EXPIRED 由 useTaskStream 处理
      // （停止 + 全局未授权回调）；agent_reasoning_chunk 思考增量不展示
      return state
  }
}
