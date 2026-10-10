/**
 * developer API（规格 2.7.3 L520-521；10.5 fail closed：developer 只允许
 * 健康检查、部署和脱敏技术日志接口，页面不含任何业务查询/审计入口）。
 *
 * GET /api/v1/developer/health  健康与依赖状态（无业务数据、无凭据，12.4）
 * GET /api/v1/developer/logs    脱敏技术日志（最新在前；10.6 长度限制 +
 *   11.2 规则 4 敏感字段；11.4 不保存完整对话/模型思考）
 * GET /api/v1/developer/stream-events/threads        已落库 SSE 事件的会话聚合
 * GET /api/v1/developer/stream-events/threads/{id}   单会话完整事件时间线
 *
 * 响应形状以后端 web/schemas.py（DeveloperHealthResponse / DeveloperLogsResponse
 * / DependencyStatus / LogEntryOut / ThreadSummary / ThreadTimeline …）为准，
 * 简单 interface 声明（不经 zod 运行时校验）。
 *
 * ⚠️ 字段可选性纪律（与 health 的既有规矩**刻意不同**）：
 * `stream_events` / `oversize_dropped` 标可选，是为了「新前端 + 老后端」的灰度
 * （老后端不返回该字段）。**下面四个 stream-events 接口是全新端点**，老后端只会
 * 404（由 ErrorNotice 兜底），不存在「字段部分缺失」的中间态。故其字段一律
 * **标必需、不标可选**——标可选会给每个数字加 `?? 0` 兜底，反而把真 bug（字段名
 * 打错 / 后端漏序列化）掩成 0。
 */
import { apiFetch } from './client'

/** 单个依赖状态（后端 DependencyStatus：name/status/detail，无凭据，12.4）。 */
export interface DependencyStatusOut {
  name: string // erp_mcp / agent_database / checkpoint_database / session
  status: string // configured / missing_credentials / not_configured / invalid
  detail: string | null
}

/**
 * SSE 事件流落库 sink 状态（后端 StreamEventSinkStatus）。
 *
 * 字段命名避雷（沿用后端约定）：本仓脱敏按**子串**匹配（password/pwd/secret/
 * api_key/token/authorization/credential/密码/密钥），此处键名全干净，勿引入
 * 含上述子串的名字（如 dropped_tokens），否则会被抹成 `***`。
 */
export interface StreamEventSinkStatusOut {
  enabled: boolean
  queued: number // 当前队列积压
  written: number // 累计成功写入行数
  dropped: number // 队列满丢弃
  write_failures: number // DB 写失败批次数
  unmapped_events: number // 未在投影白名单内的事件类型计数
  pruned_rows: number // 累计清理行数
  // 单行 payload 超限被预检剔除的行数（只丢这一行，不拖累整批）。
  // 可选：后端 P1-4 起返回，但回滚/灰度会出现「新前端 + 老后端」→ undefined，
  // 展示侧必须 `?? 0` 兜底（方案风险 7），否则会渲染成 undefined 或让 NaN 进标红判断。
  oversize_dropped?: number
  last_write_age_seconds: number | null
  // database_not_configured | database_unavailable | null
  degraded_reason: string | null
  // 最近一次写失败时捕获的异常类名（后端 type(exc).__name__，只存类名、不含 str(exc)）。
  // 可选：后端本批贯通，回滚/灰度会出现「新前端 + 老后端」→ undefined。
  // 展示侧**只经白名单映射**渲染，绝不把该字符串直接插进 JSX（见 DeveloperPage 的
  // LAST_ERROR_KIND_TEXT），认不出的类名回退中性文案。
  last_error_kind?: string | null
}

/**
 * GET /api/v1/developer/health 响应（后端 DeveloperHealthResponse）。
 *
 * `stream_events` 为后续新增的顶层字段：老后端不返回时它是 `undefined`，
 * `null` 表示「压根没接线」（与「接线了但队列为 0」区分）。展示侧必须用
 * 可选链兜底，避免部署顺序倒置（新前端 + 老后端）时白屏。
 */
export interface DeveloperHealthOut {
  status: string
  app: string
  version: string
  log_level: string
  dependencies: DependencyStatusOut[]
  stream_events?: StreamEventSinkStatusOut | null
}

/** 单条脱敏技术日志（后端 LogEntryOut）。 */
export interface DeveloperLogEntryOut {
  timestamp: string | null
  level: string | null
  event: string | null
  logger: string | null
  payload: Record<string, unknown>
}

/** GET /api/v1/developer/logs 响应（后端 DeveloperLogsResponse）。 */
export interface DeveloperLogsOut {
  items: DeveloperLogEntryOut[]
  total: number
  limit: number
}

/** 日志级别过滤值（后端 VALID_LOG_LEVELS；非法值 422）。 */
export const DEV_LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
export type DevLogLevel = (typeof DEV_LOG_LEVELS)[number]

export interface DeveloperLogsParams {
  limit?: number
  level?: DevLogLevel | null
}

export function getDeveloperHealthApi(): Promise<DeveloperHealthOut> {
  return apiFetch<DeveloperHealthOut>('/developer/health')
}

export function getDeveloperLogsApi(params: DeveloperLogsParams = {}): Promise<DeveloperLogsOut> {
  const query = new URLSearchParams()
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.level) query.set('level', params.level)
  const qs = query.toString()
  return apiFetch<DeveloperLogsOut>(`/developer/logs${qs ? `?${qs}` : ''}`)
}

// ---------------------------------------------------------------------------
// 任务时间线（可观测性第 1 步）：读取已落库的 SSE 事件结构量。
// 口径：只展示结构性元数据（事件类型 / 长度 / 枚举 / 键名），payload 已是 A 档
// 投影结果，**原文根本不存在于表中**；不关联会话归属（无 account / title）。
// ---------------------------------------------------------------------------

/** 会话列表单项（后端 ThreadSummary）。字段全必需，理由见文件头注释。 */
export interface StreamEventThreadSummaryOut {
  thread_id: string
  /** 保留期内全部行数（含 suppressed / stream_created）。 */
  event_count: number
  /** COUNT(DISTINCT stream_id)：这次对话有几轮。 */
  stream_count: number
  /** 分片行数——让 event_count 可解释。 */
  chunk_count: number
  /** 显式闭集 ('agent_error' | 'task_failed' | 'memory_compression_failed') 的计数。 */
  error_count: number
  suppressed_count: number
  /** 投影白名单漂移信号（record_kind='unmapped'）。 */
  unmapped_count: number
  first_seen: string
  last_seen: string
}

/** GET /api/v1/developer/stream-events/threads 响应。 */
export interface StreamEventThreadsOut {
  items: StreamEventThreadSummaryOut[]
  total: number
  limit: number
  offset: number
  /** thread_id IS NULL 的孤儿行数（0 = 无）；顶层而非 item，否则会静默消失。 */
  untracked_events: number
}

/**
 * 单条事件行的 payload 值域（后端显式标量联合，不用 Any）：
 * 投影只放行标量，故这里也**不写 `unknown`**——将来后端把嵌套结构塞进来会
 * 变成一次类型错误（显式改动），而不是静默通过。
 */
export type StreamEventPayloadValue = string | number | boolean | string[] | null

/** 时间线单条事件行（后端 StreamEventItem）。 */
export interface StreamEventItemOut {
  event_id: string
  stream_id: string
  /** 流内序号（真实流事件从 1 起连续；影子行 / 建流行固定 0）。 */
  sequence: number
  event_type: string
  record_kind: string // pushed | suppressed | stream_created | unmapped
  task_id: string | null
  payload: Record<string, StreamEventPayloadValue>
  created_at: string
}

/** GET /api/v1/developer/stream-events/threads/{thread_id} 响应（后端 ThreadTimeline）。 */
export interface StreamEventThreadDetailOut {
  thread_id: string
  items: StreamEventItemOut[]
  /** 该会话全部行数（与 items 同口径 → 分页一致）。 */
  total: number
  limit: number
  offset: number
  /** Σ每流 MAX(sequence)：真实流事件「应有」条数。 */
  expected_events: number
  /** max(0, expected − 实际真实事件条数)：写入端到底丢没丢数据的持久事实。 */
  missing_events: number
}

export interface StreamEventThreadsParams {
  limit?: number
  offset?: number
}

export interface StreamEventThreadDetailParams {
  limit?: number
  offset?: number
}

function streamEventQuery(params: StreamEventThreadsParams): string {
  const query = new URLSearchParams()
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  const qs = query.toString()
  return qs ? `?${qs}` : ''
}

export function getStreamEventThreadsApi(
  params: StreamEventThreadsParams = {},
): Promise<StreamEventThreadsOut> {
  return apiFetch<StreamEventThreadsOut>(`/developer/stream-events/threads${streamEventQuery(params)}`)
}

export function getStreamEventThreadDetailApi(
  threadId: string,
  params: StreamEventThreadDetailParams = {},
): Promise<StreamEventThreadDetailOut> {
  // thread_id 进的是 path 段（可能是任意 opaque 串），必须 encodeURIComponent。
  return apiFetch<StreamEventThreadDetailOut>(
    `/developer/stream-events/threads/${encodeURIComponent(threadId)}${streamEventQuery(params)}`,
  )
}
