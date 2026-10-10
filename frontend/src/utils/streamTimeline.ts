/**
 * 任务时间线 · 分片游程折叠（纯函数，无 React）。
 *
 * 背景（可观测性第 1 步 §6.3）：模型每流一个 token 就发一条 chunk 事件，
 * 一次 800 token 的回复 = 800 行分片 + 十几个真实步骤行。列表上的「事件数 812」
 * 会被误读成「812 个步骤」，与「排查失败在第几步」正面对撞。
 *
 * 因此对「数组内相邻 + 同 stream_id + 同 event_type + 同 record_kind」的
 * **分片行游程**做纯机械折叠。**这不是语义聚合**——判据只是数组相邻，
 * 任何插进分片流的异类行（tool_running / agent_error / 换轮）都会把一次折叠
 * 劈成两条 run，断裂因此天然可见。本模块做不到跨越异类行。
 *
 * 硬规则：
 * - 只有 `record_kind === 'pushed'` 且事件类型 ∈ CHUNK_EVENT_TYPES 的行才可折叠；
 * - 游程长度 < 2 不折（否则出现「1 个分片 · 共 3 字」这种纯噪音行）；
 * - 折叠行头必须携带 seq 区间，并由 `seqLast - seqFirst + 1 - rows.length` 算出
 *   游程内部空缺段数（纯算术，不是猜测）。
 *
 * 本模块**只吃后端 /developer/stream-events/* 返回的已投影结构量**，
 * 不接触任何原文（契约见 api/developer.api.ts 与方案 §三）。
 */
import type { StreamEventItemOut } from '../api/developer.api'

/**
 * 分片事件类型（与后端 `stream_event_reader.CHUNK_EVENT_TYPES` 同集合）。
 * 后端新增分片类型时必须同步改这里（后端常量注释亦点名此处）。
 */
export const CHUNK_EVENT_TYPES: readonly string[] = [
  'agent_reply_chunk',
  'agent_reasoning_chunk',
]

/** 普通事件行（逐行流水 / 折叠展开后的原始行都由它承载）。 */
export interface TimelineEventRow {
  kind: 'event'
  event: StreamEventItemOut
}

/** 折叠行：一段相邻同类分片游程。 */
export interface TimelineChunkRunRow {
  kind: 'chunk_run'
  /** 被折进来的原始分片行（展开时逐行渲染，顺序不变）。 */
  rows: StreamEventItemOut[]
  /** 本游程 chunk_chars 之和。 */
  totalChars: number
  /** 本游程单段最小 / 最大 chunk_chars（「分布被压平」的补偿信息）。 */
  minChars: number
  maxChars: number
  /** 游程首 / 末行的 sequence（行头永远显示 `seq a–b`）。 */
  seqFirst: number
  seqLast: number
  /** 游程内部没落库的段数 = seqLast - seqFirst + 1 - rows.length（clamp 到 0）。 */
  missing: number
  streamId: string
  eventType: string
}

export type TimelineRow = TimelineEventRow | TimelineChunkRunRow

/** 分片行是否可折叠。`unmapped` / `suppressed` / 非分片类型一律不折。 */
function isFoldable(item: StreamEventItemOut): boolean {
  return item.record_kind === 'pushed' && CHUNK_EVENT_TYPES.includes(item.event_type)
}

/** 单行分片字数（payload.chunk_chars）；非有限数值按 0 计，绝不产生 NaN。 */
function chunkChars(item: StreamEventItemOut): number {
  const value = item.payload?.chunk_chars
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return 0
}

function makeChunkRun(rows: StreamEventItemOut[]): TimelineChunkRunRow {
  const chars = rows.map(chunkChars)
  const seqFirst = rows[0].sequence
  const seqLast = rows[rows.length - 1].sequence
  return {
    kind: 'chunk_run',
    rows,
    totalChars: chars.reduce((sum, value) => sum + value, 0),
    minChars: Math.min(...chars),
    maxChars: Math.max(...chars),
    seqFirst,
    seqLast,
    missing: Math.max(0, seqLast - seqFirst + 1 - rows.length),
    streamId: rows[0].stream_id,
    eventType: rows[0].event_type,
  }
}

/**
 * 把一页事件行折成「事件行 + 折叠行」序列。
 *
 * 折叠判据是数组内相邻性与 (stream_id, event_type) 相等，**不跨异类行**，
 * 也不跨页——每次折叠都到页边界为止，页边界的位置由底部分页控件给出。
 */
export function foldChunkRuns(items: StreamEventItemOut[]): TimelineRow[] {
  const rows: TimelineRow[] = []
  let index = 0
  while (index < items.length) {
    const head = items[index]
    if (isFoldable(head)) {
      let end = index + 1
      while (
        end < items.length &&
        isFoldable(items[end]) &&
        items[end].stream_id === head.stream_id &&
        items[end].event_type === head.event_type
      ) {
        end += 1
      }
      // 长度 < 2 的游程不折。
      if (end - index >= 2) {
        rows.push(makeChunkRun(items.slice(index, end)))
        index = end
        continue
      }
    }
    rows.push({ kind: 'event', event: head })
    index += 1
  }
  return rows
}
