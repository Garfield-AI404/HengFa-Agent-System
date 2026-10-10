/**
 * 任务时间线面板（可观测性第 1 步 S4）。
 *
 * 用户原话「我能看到模型整个调用流程的过程吗」——本面板把已落库的 SSE 事件骨架
 * 读出来：**最近会话列表 → 点进去 → 该次对话的完整事件流水**。
 *
 * 口径（不扩大数据暴露面）：
 * - 只读 /api/v1/developer/stream-events/*；这些端点只查 `chat_stream_events`
 *   一张表、不 JOIN 任何表，故**没有 account / title**（这是设计，不是 bug）；
 * - payload 是服务端 A 档投影结果，**原文根本不存在于表中**；
 * - 面板顶部固定一条口径文案，免得开发者看到没有标题当成 bug 报回来。
 *
 * 分片折叠见 utils/streamTimeline.ts：折叠只是展示层，断裂 / 内部空缺分别由
 * 游程断裂、`缺 N 段` 如实呈现，页边界由底部分页控件（`1-500 / 1234`）给出；
 * 关掉「展开全部分片」按钮即完全逐行流水。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { ApiError } from '../api/client'
import {
  getStreamEventThreadDetailApi,
  getStreamEventThreadsApi,
} from '../api/developer.api'
import type {
  StreamEventItemOut,
  StreamEventPayloadValue,
  StreamEventThreadSummaryOut,
} from '../api/developer.api'
import { EmptyState, ErrorNotice } from './ErrorNotice'
import { Pagination } from './Pagination'
import { PanelBlock } from './PanelBlock'
import { displayExternal, formatDateTime, truncateExternal } from '../utils/format'
import { foldChunkRuns } from '../utils/streamTimeline'
import type { TimelineChunkRunRow } from '../utils/streamTimeline'

const LIST_LIMIT = 50
const DETAIL_LIMIT = 500

/**
 * 事件类型 → 中文。**全部 `?? FALLBACK`**：认不出的类型回退通用文案，
 * 绝不把后端字符串直接插进 JSX（与 DeveloperPage 的 LAST_ERROR_KIND_TEXT 同款边界）。
 */
const EVENT_TYPE_TEXT: Record<string, string> = {
  agent_started: '对话开始',
  tool_running: '工具调用中',
  tool_result: '工具返回',
  confirmation_requested: '请求确认',
  // 后端真实事件类型是 `re_requested`（PROJECTION 第 17 类）；写成
  // `confirmation_re_requested` 会永远命中不到、该类型只能走 FALLBACK。
  re_requested: '再次请求确认',
  agent_reply: '回复完成',
  agent_reply_start: '回复开始',
  agent_reply_chunk: '回复分片',
  agent_reasoning_chunk: '思考分片',
  agent_error: '错误',
  memory_compressed: '记忆压缩',
  memory_compression_failed: '记忆压缩失败',
  // 可观测性第 2 步新增埋点：压缩节点每次进入发一条判定台账（含成功轮），
  // 决策轮 / 参数轮各发一条输入摘要。两轮段结构不同故拆两个事件类型，
  // 不合并成 stage 单事件（否则某键对某一轮恒无意义）。
  compression_decision: '压缩判定',
  decision_input_summary: '决策轮输入摘要',
  args_input_summary: '参数轮输入摘要',
  reconcile_report: '对账报告',
  export_ready: '导出就绪',
  task_cancelled: '任务取消',
  SESSION_EXPIRED: '会话过期',
  task_failed: '任务失败',
}

/** 未在映射表内的事件类型 / 记录类别 → 中性文案，绝不回显原字符串。 */
const EVENT_TYPE_FALLBACK = '未知事件类型'
const RECORD_KIND_FALLBACK = '未知记录类别'

/**
 * 未知**事件类型**走通用 FALLBACK（不显示原串），而未知 **payload 键**回退显示键名
 * ——两者**刻意不同**，理由在映射表下方：**事件类型不在任何白名单里**（后端新增
 * 事件常量时，前端字典必然落后，原串只是一串后端符号）；而**键一定在白名单里**
 * （见 PAYLOAD_KEY_TEXT 注释），所以键名本身是可信标签，显示它零风险、且是漂移信号。
 */

/** record_kind → 中文（后端四值闭集）。 */
const RECORD_KIND_TEXT: Record<string, string> = {
  pushed: '已推送',
  suppressed: '已抑制',
  stream_created: '流创建',
  unmapped: '未映射',
}

/**
 * payload 键 → 中文。**未命中时回退显示键名本身（`?? key`），绝不隐藏。**
 *
 * 为什么显示键名是安全的（2026-09-19 口径裁定）：
 * 一个 payload 键**只可能因为它在 `PROJECTION` 白名单里才存在于表中**——写入侧
 * 投影白名单 + DB 三条 CHECK 是两道锁，白名单外的键**根本进不了库**。所以
 * 「前端映射表不认识的键」**仍然是白名单内的键**：它只是还没有中文标签，
 * **不是可疑数据**。隐藏它换不来任何安全，反而会连带藏掉「投影白名单漂移」
 * 这个信号（正是风险清单第 5 条要保的东西）。真正危险的是「**投影**不认识的键」，
 * 而那种键在数据层就被拦掉了。
 *
 * ⚠️ 因此不要在这里加「映射表未命中即整条跳过」的逻辑：未知键 → 显示键名
 * 是**被测用例（30）钉死的行为**，改成跳过会立刻红。
 */
const PAYLOAD_KEY_TEXT: Record<string, string> = {
  // tool_name / task_id 是 payload 里最高频的两个键（tool_running / tool_result /
  // confirmation_requested / re_requested 都带），故置顶。
  tool_name: '工具名',
  task_id: '任务号',
  thread_id: '会话 ID',
  stage: '阶段',
  ok: '结果',
  payload_hash: '载荷哈希',
  re_requested: '是否重新请求',
  card_keys: '卡片键',
  reply_chars: '回复字数',
  chunk_chars: '分片字数',
  error: '错误',
  tool_executed: '已执行工具',
  has_message: '是否含消息',
  segment_no: '分段号',
  tier: '层级',
  summary_chars: '摘要字数',
  archived_messages: '归档消息数',
  index_chars: '索引字数',
  report_id: '报告 ID',
  row_count: '行数',
  subject_match_status: '主体匹配状态',
  file_id: '文件 ID',
  entity: '实体',
  total: '总数',
  expires_at: '过期时间',
  filename_chars: '文件名字数',
  reason: '原因',
  // ⚠️ 该键**不在 PROJECTION 表里**：它是抑制（影子）路径单独注入的
  // （后端 stream_projection.py:326），只出现在 record_kind='suppressed' 的行上。
  suppressed_reason: '抑制原因',
  // 可观测性第 2 步新增的 payload 键（compression_decision / *_input_summary）。
  // 不补标签也不违规（未知键回退显示键名，是有意的漂移信号），补上更好读。
  decision: '判定结果',
  occupancy_pct: '占用百分比',
  messages_count: '消息条数',
  segment_messages: '待压段消息数',
  history_raw: '历史原始条数',
  history_sent: '历史发送条数',
  evicted_details: '驱逐明细条数',
  summaries_count: '摘要条数',
  active_skill: '激活流程',
}

/**
 * 抑制原因（`suppressed_reason`）的枚举措辞 → 中文。
 *
 * 值域是**闭集** `SUPPRESSED_REASONS`（后端 stream_projection.py:62-68），
 * 集合外的值在写入侧就被收敛成 `unknown`，故前端正常只会见到这四个。
 * `unknown` 恰恰是最该被看见的那个——它意味着投影侧收到了集合外的值。
 * 认不出的值回退通用文案（与事件类型同款 `?? FALLBACK`），绝不回显原字符串。
 */
const SUPPRESSED_REASON_TEXT: Record<string, string> = {
  interrupted: '被中断',
  'final_step:model_failed': '末轮模型失败',
  'final_step:steps_exhausted': '末轮步数耗尽',
  unknown: '未知',
}
const SUPPRESSED_REASON_FALLBACK = '未知抑制原因'

/** 错误事件类型闭集（与后端 ERROR_EVENT_TYPES 同集合；只影响行色，不影响计数）。 */
const ERROR_EVENT_TYPES = ['agent_error', 'task_failed', 'memory_compression_failed']
/** 取消 / 过期不是故障，但值得用警告色区分（绝不并入 error_count 语义）。 */
const WARN_EVENT_TYPES = ['task_cancelled', 'SESSION_EXPIRED']

function eventLevelClass(eventType: string): string {
  if (ERROR_EVENT_TYPES.includes(eventType)) return 'error'
  if (WARN_EVENT_TYPES.includes(eventType)) return 'warning'
  return 'info'
}

/** payload 值 → 展示文本：布尔转「是 / 否」（`true` 绝不能渲染成 `1`），列表拼接。 */
function payloadValueText(value: StreamEventPayloadValue): string {
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.map((item) => displayExternal(item, 64)).join('、')
  return displayExternal(value)
}

/**
 * 带键上下文的取值文案：`suppressed_reason` 是枚举措辞，走专属映射表；
 * 其余键一律走通用的 displayExternal 路径。
 */
function payloadEntryValueText(key: string, value: StreamEventPayloadValue): string {
  if (key === 'suppressed_reason' && typeof value === 'string') {
    return SUPPRESSED_REASON_TEXT[value] ?? SUPPRESSED_REASON_FALLBACK
  }
  return payloadValueText(value)
}

/**
 * 行摘要：`tool_name` / `task_id` **有意识地直出**（不经过映射表），沿用
 * DeveloperPage 的 `log-entry__event` 先例，只做 `truncateExternal(x, 64)`。
 * 理由：二者是代码侧常量（工具名 / 任务 ID），会随工具集漂移，映射表只会造成
 * 无谓的「未知」；这是本文件**唯一有意为之的例外**，不是漏网。
 */
function eventSummary(item: StreamEventItemOut): string {
  const parts: string[] = []
  const toolName = item.payload?.tool_name
  if (typeof toolName === 'string' && toolName) {
    parts.push(truncateExternal(toolName, 64))
  }
  const taskId =
    item.task_id ??
    (typeof item.payload?.task_id === 'string' ? item.payload.task_id : null)
  if (taskId) {
    parts.push(`任务 ${truncateExternal(taskId, 64)}`)
  }
  return parts.join(' · ')
}

/**
 * **普通行渲染函数**——逐行流水与「折叠展开后的原始行」共用同一个函数，
 * 避免两种视图漂移（本步最容易写歪的地方）。key 用 `event_id`，
 * **绝不用数组 index**：index 做 key 会让「展开态串到别的行上」测不出来。
 */
function renderEventRow(
  item: StreamEventItemOut,
  expandedKey: string | null,
  onToggle: (key: string) => void,
): ReactNode {
  const open = expandedKey === item.event_id
  const payloadEntries = Object.entries(item.payload ?? {})
  const hasPayload = payloadEntries.length > 0
  const summary = eventSummary(item)
  return (
    <li key={item.event_id} className="log-entry">
      <p className="log-entry__head">
        <span className="log-entry__time">{formatDateTime(item.created_at)}</span>
        <span
          className={`log-entry__level log-entry__level--${eventLevelClass(item.event_type)}`}
        >
          {EVENT_TYPE_TEXT[item.event_type] ?? EVENT_TYPE_FALLBACK}
        </span>
        <span className="log-entry__event">
          {`seq ${item.sequence}`}
          {summary ? ` · ${summary}` : ''}
        </span>
        <span className="log-entry__logger">
          {`${RECORD_KIND_TEXT[item.record_kind] ?? RECORD_KIND_FALLBACK} · ${truncateExternal(item.event_id, 64)}`}
        </span>
        {hasPayload ? (
          <button
            type="button"
            className="icon-button"
            aria-label={open ? '收起事件明细' : '展开事件明细'}
            aria-expanded={open}
            onClick={() => onToggle(item.event_id)}
          >
            {open ? '−' : '+'}
          </button>
        ) : null}
      </p>
      {open && hasPayload ? (
        <div className="log-entry__payload">
          {payloadEntries.map(([key, value]) => (
            <p key={key}>{`${PAYLOAD_KEY_TEXT[key] ?? key}：${payloadEntryValueText(key, value)}`}</p>
          ))}
        </div>
      ) : null}
    </li>
  )
}

/** 折叠行：一段相邻同类分片游程。 */
function renderChunkRunRow(
  run: TimelineChunkRunRow,
  expandedKey: string | null,
  onToggle: (key: string) => void,
): ReactNode {
  // 游程身份 = 首行 event_id（稳定且唯一），既做 React key 也做展开态键。
  const key = run.rows[0].event_id
  const open = expandedKey === key
  const label = EVENT_TYPE_TEXT[run.eventType] ?? EVENT_TYPE_FALLBACK
  return (
    <li key={key} className="log-entry log-entry--chunk-run">
      <p className="log-entry__head">
        <span className="log-entry__time">{formatDateTime(run.rows[0].created_at)}</span>
        <span className="log-entry__event">
          {`${label} · 分片 ${run.rows.length} · 共 ${run.totalChars} 字 · 单段 ${run.minChars}–${run.maxChars} 字 · seq ${run.seqFirst}–${run.seqLast}`}
        </span>
        {run.missing > 0 ? (
          <span className="log-entry__count--error">{`缺 ${run.missing} 段`}</span>
        ) : null}
        <button
          type="button"
          className="icon-button"
          aria-label={open ? '收起分片' : '展开分片'}
          aria-expanded={open}
          onClick={() => onToggle(key)}
        >
          {open ? '−' : '+'}
        </button>
      </p>
      {open ? (
        <ol className="log-list log-list--nested">
          {run.rows.map((item) => renderEventRow(item, expandedKey, onToggle))}
        </ol>
      ) : null}
    </li>
  )
}

/**
 * 状态列：只有 `error_count > 0` 才标红（取消 / 过期不算故障）。
 * `unmapped_count > 0`（投影白名单与事件常量漂移）用琥珀色提示——它不是故障，
 * 但必须**看得见**，否则后端新增事件类型时前端会静默漏掉。
 */
function threadStatus(status: StreamEventThreadSummaryOut): ReactNode {
  if (status.error_count > 0) {
    return (
      <span className="dependency-status dependency-status--invalid">
        <span className="dependency-status__dot" aria-hidden="true" />
        {`错误 ${status.error_count}`}
      </span>
    )
  }
  if (status.unmapped_count > 0) {
    return (
      <span className="dependency-status dependency-status--not_configured">
        <span className="dependency-status__dot" aria-hidden="true" />
        {`未映射 ${status.unmapped_count}`}
      </span>
    )
  }
  return (
    <span className="dependency-status dependency-status--configured">
      <span className="dependency-status__dot" aria-hidden="true" />
      正常
    </span>
  )
}

export function StreamTimelinePanel() {
  // 单一展开（同 DeveloperPage 的 expandedLog 写法）：列表态与时间线态共用。
  const [expanded, setExpanded] = useState<string | null>(null)
  const [selectedThread, setSelectedThread] = useState<string | null>(null)
  const [listOffset, setListOffset] = useState(0)
  const [detailOffset, setDetailOffset] = useState(0)
  const [foldChunks, setFoldChunks] = useState(true)

  // 会话列表：60s 轮询（与 health 同节奏）。
  const threads = useQuery({
    queryKey: ['developer', 'stream-threads', listOffset],
    queryFn: () => getStreamEventThreadsApi({ limit: LIST_LIMIT, offset: listOffset }),
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  })
  // 时间线：**不轮询**（排查时画面要稳定），给手动刷新按钮。
  const detail = useQuery({
    queryKey: ['developer', 'stream-thread', selectedThread, detailOffset],
    queryFn: () => {
      if (!selectedThread) throw new Error('未选择会话')
      return getStreamEventThreadDetailApi(selectedThread, {
        limit: DETAIL_LIMIT,
        offset: detailOffset,
      })
    },
    enabled: selectedThread !== null,
    placeholderData: keepPreviousData,
  })

  const threadsError = threads.error instanceof ApiError ? threads.error : null
  const detailError = detail.error instanceof ApiError ? detail.error : null

  function openThread(threadId: string): void {
    setSelectedThread(threadId)
    setDetailOffset(0)
    setExpanded(null)
  }

  function backToList(): void {
    setSelectedThread(null)
    setDetailOffset(0)
    setExpanded(null)
  }

  function jumpToLatest(): void {
    const total = detail.data?.total ?? 0
    if (total <= 0) return
    setDetailOffset(Math.max(0, Math.floor((total - 1) / DETAIL_LIMIT) * DETAIL_LIMIT))
    setExpanded(null)
  }

  function refresh(): void {
    if (selectedThread) {
      void detail.refetch()
    } else {
      void threads.refetch()
    }
  }

  const rows = detail.data
    ? foldChunks
      ? foldChunkRuns(detail.data.items)
      : detail.data.items.map((event) => ({ kind: 'event' as const, event }))
    : []

  return (
    <PanelBlock
      title="任务时间线"
      adornment={
        <button
          type="button"
          className="icon-button"
          onClick={refresh}
          aria-label="刷新任务时间线"
          title="刷新"
        >
          <RefreshCw aria-hidden="true" size={14} />
        </button>
      }
    >
      <p className="panel__muted">
        只展示结构性元数据（事件类型/长度/枚举/键名），不含模型输入输出原文；
        不关联会话归属（无 account/title）。保留 14 天。
      </p>

      {selectedThread === null ? (
        <>
          <ErrorNotice error={threadsError} />
          {threads.isLoading && !threads.data ? <p className="panel__muted">加载中…</p> : null}
          {threads.data && threads.data.untracked_events > 0 ? (
            // 孤儿行（thread_id IS NULL）点不进去，不显式计数就会静默消失。
            <p className="kv-note kv-note--error">
              {`${threads.data.untracked_events} 条事件未关联会话`}
            </p>
          ) : null}
          {threads.data && threads.data.items.length === 0 ? (
            <EmptyState>保留期内暂无已落库的会话事件。</EmptyState>
          ) : null}
          {threads.data && threads.data.items.length > 0 ? (
            <div className="table-scroll">
              <table className="data-table data-table--compact">
                <thead>
                  <tr>
                    <th scope="col">会话</th>
                    <th scope="col">最近活动</th>
                    <th scope="col">事件数</th>
                    <th scope="col">轮数</th>
                    <th scope="col">状态</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {threads.data.items.map((item) => (
                    <tr key={item.thread_id}>
                      <th scope="row">
                        {/* thread_id 用 title 属性给完整值（可选中文本；jsdom 无
                            navigator.clipboard，故刻意不做复制按钮）。 */}
                        <span title={item.thread_id}>{item.thread_id.slice(0, 8)}</span>
                      </th>
                      <td>{formatDateTime(item.last_seen)}</td>
                      <td>{`${item.event_count}（其中分片 ${item.chunk_count}）`}</td>
                      <td>{item.stream_count}</td>
                      <td>{threadStatus(item)}</td>
                      <td>
                        {/* onClick 挂在按钮而非 <tr>：键盘可达。 */}
                        <button
                          type="button"
                          className="btn"
                          onClick={() => openThread(item.thread_id)}
                        >
                          查看
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {threads.data ? (
            <Pagination
              total={threads.data.total}
              limit={threads.data.limit}
              offset={threads.data.offset}
              onChange={(offset) => {
                setListOffset(offset)
                setExpanded(null)
              }}
            />
          ) : null}
        </>
      ) : (
        <>
          <ErrorNotice error={detailError} />
          <div className="timeline-toolbar">
            <button type="button" className="btn" onClick={backToList}>
              ← 返回会话列表
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setFoldChunks((value) => !value)
                setExpanded(null)
              }}
            >
              {foldChunks ? '展开全部分片' : '折叠全部分片'}
            </button>
            <button type="button" className="btn" onClick={jumpToLatest}>
              跳到最新
            </button>
          </div>
          <p className="panel__muted">{`会话 ${selectedThread}`}</p>
          {detail.isLoading && !detail.data ? <p className="panel__muted">加载中…</p> : null}
          {detail.data ? (
            <>
              <p
                className={
                  detail.data.missing_events > 0 ? 'kv-note kv-note--error' : 'kv-note'
                }
              >
                {`本会话共 ${detail.data.total} 行 · 应有 ${detail.data.expected_events} 条真实事件 · 缺失 ${detail.data.missing_events} 条`}
              </p>
              {detail.data.items.length === 0 ? (
                <EmptyState>该会话在保留期内没有已落库事件。</EmptyState>
              ) : (
                <ol className="log-list">
                  {rows.map((row) =>
                    row.kind === 'chunk_run'
                      ? renderChunkRunRow(row, expanded, setExpanded)
                      : renderEventRow(row.event, expanded, setExpanded),
                  )}
                </ol>
              )}
              <Pagination
                total={detail.data.total}
                limit={detail.data.limit}
                offset={detail.data.offset}
                onChange={(offset) => {
                  setDetailOffset(offset)
                  setExpanded(null)
                }}
              />
            </>
          ) : null}
        </>
      )}
    </PanelBlock>
  )
}
