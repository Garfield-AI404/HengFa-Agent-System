/**
 * streamTimeline 纯函数单测（可观测性第 1 步 · 方案 §7.3 用例 18-23）。
 *
 * 折叠算法是本步前端唯一有真算法的部分——**不该靠 render 测**，故单独覆盖。
 * 用例 19 是「断裂可见」的回归锁：中间插入异类行必须把一次折叠劈成两条 run。
 */
import { describe, expect, it } from 'vitest'
import { foldChunkRuns } from '../../src/utils/streamTimeline'
import type { StreamEventItemOut } from '../../src/api/developer.api'

/** 造一条分片行；`over` 用来覆盖 stream_id / record_kind / event_type 等。 */
function chunk(
  seq: number,
  chars: number,
  over: Partial<StreamEventItemOut> = {},
): StreamEventItemOut {
  const streamId = over.stream_id ?? 's-a'
  return {
    event_id: `${streamId}-e${String(seq).padStart(4, '0')}`,
    stream_id: streamId,
    sequence: seq,
    event_type: 'agent_reply_chunk',
    record_kind: 'pushed',
    task_id: null,
    payload: { chunk_chars: chars },
    created_at: '2026-09-18T02:11:07.421337+00:00',
    ...over,
  }
}

function tool(seq: number): StreamEventItemOut {
  return {
    event_id: `s-a-e${String(seq).padStart(4, '0')}`,
    stream_id: 's-a',
    sequence: seq,
    event_type: 'tool_running',
    record_kind: 'pushed',
    task_id: null,
    payload: { tool_name: 'erp_query' },
    created_at: '2026-09-18T02:11:07.421337+00:00',
  }
}

describe('foldChunkRuns', () => {
  // 用例 18：相邻同类 → 1 条 run，totalChars/seqFirst/Last/min/maxChars 精确
  it('18 相邻同类分片折成 1 条 run，统计精确', () => {
    const rows = foldChunkRuns([chunk(1, 10), chunk(2, 12)])

    expect(rows).toHaveLength(1)
    const run = rows[0]
    expect(run.kind).toBe('chunk_run')
    if (run.kind !== 'chunk_run') throw new Error('unreachable')
    expect(run.rows).toHaveLength(2)
    expect(run.totalChars).toBe(22)
    expect(run.seqFirst).toBe(1)
    expect(run.seqLast).toBe(2)
    expect(run.minChars).toBe(10)
    expect(run.maxChars).toBe(12)
  })

  // 用例 19：中间插入 tool_running → 折成 2 条 run（断裂可见的回归锁）
  it('19 中间插入 tool_running → 折成 2 条 run，异类行不被吞', () => {
    const rows = foldChunkRuns([chunk(1, 5), chunk(2, 5), tool(3), chunk(4, 5), chunk(5, 5)])

    expect(rows.map((row) => row.kind)).toEqual(['chunk_run', 'event', 'chunk_run'])
    const [first, middle, second] = rows
    if (first.kind !== 'chunk_run' || second.kind !== 'chunk_run') throw new Error('unreachable')
    expect(first.seqLast).toBe(2)
    expect(second.seqFirst).toBe(4)
    if (middle.kind !== 'event') throw new Error('unreachable')
    expect(middle.event.event_id).toBe('s-a-e0003')
  })

  // 用例 20：不同 stream_id 不合并；record_kind !== 'pushed' 不折
  it('20 不同 stream_id 不合并', () => {
    const rows = foldChunkRuns([chunk(1, 5), chunk(2, 5, { stream_id: 's-b' })])

    expect(rows.map((row) => row.kind)).toEqual(['event', 'event'])
  })

  it('20 record_kind 非 pushed → 不折（unmapped 也是真实事件，但不可折叠）', () => {
    const rows = foldChunkRuns([chunk(1, 5), chunk(2, 5, { record_kind: 'suppressed' })])

    expect(rows.map((row) => row.kind)).toEqual(['event', 'event'])
  })

  // 用例 21：长度 1 的游程不折
  it('21 单个分片（游程长度 1）不折，退回普通事件行', () => {
    const rows = foldChunkRuns([chunk(1, 7), tool(2)])

    expect(rows.map((row) => row.kind)).toEqual(['event', 'event'])
    const head = rows[0]
    if (head.kind !== 'event') throw new Error('unreachable')
    expect(head.event.event_id).toBe('s-a-e0001')
  })

  // 用例 22：missing 纯算术；同时用两组数据把公式本身锁死
  it('22 missing = seqLast - seqFirst + 1 - rows.length', () => {
    // 方案 §7.3 的示例数组写作 seq 12,13,16，但按其自身公式应为 2 而非 1
    // （16-12+1=5，减去 3 行 = 2）。此处两组都断言，把公式逐值锁住。
    const oneGap = foldChunkRuns([chunk(12, 1), chunk(13, 1), chunk(15, 1)])
    if (oneGap[0].kind !== 'chunk_run') throw new Error('unreachable')
    expect(oneGap[0].missing).toBe(1)

    const twoGaps = foldChunkRuns([chunk(12, 1), chunk(13, 1), chunk(16, 1)])
    if (twoGaps[0].kind !== 'chunk_run') throw new Error('unreachable')
    expect(twoGaps[0].seqFirst).toBe(12)
    expect(twoGaps[0].seqLast).toBe(16)
    expect(twoGaps[0].missing).toBe(2)
  })

  // 用例 23：空数组 → []
  it('23 空数组 → []', () => {
    expect(foldChunkRuns([])).toEqual([])
  })
})
