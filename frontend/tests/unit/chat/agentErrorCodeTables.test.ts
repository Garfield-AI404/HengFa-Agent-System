/**
 * [2026-09-28 复审补] agent_error 码表之间的**纯表不变量**断言。
 *
 * 这里不驱动 reducer，只钉住「表与表的关系」——复审发现此前**没有任何测试
 * import `SILENT_NON_TERMINAL_AGENT_ERROR_CODES`**，也没断言它与
 * `NON_TERMINAL_AGENT_ERROR_CODES` 的子集关系。危险组合确实存在：
 *
 *   若将来有人把某码加进 `SILENT_*` 却忘了加进 `NON_TERMINAL_*`，则该码到
 *   达时 reducer 走 `return state`（**一个气泡都不产**），同时
 *   `useTaskStream` 因不在 `NON_TERMINAL_*` 而判**终局**（停轮询）
 *   ⇒ **用户得到一个完全空白的回合**，既无错误文案也无模型回复，
 *      且没有任何红灯。
 *
 * 故此处把 `SILENT_* ⊆ NON_TERMINAL_*` 钉成硬不变量。
 */
import { describe, expect, it } from 'vitest'
import {
  NON_TERMINAL_AGENT_ERROR_CODES,
  SILENT_NON_TERMINAL_AGENT_ERROR_CODES,
} from '../../../src/pages/chat/constants'

describe('agent_error 码表不变量', () => {
  it('SILENT_NON_TERMINAL_AGENT_ERROR_CODES ⊆ NON_TERMINAL_AGENT_ERROR_CODES', () => {
    // 逐成员断言（而非仅比 size）：某人只往 SILENT 里加、忘往 NON_TERMINAL 加
    // 这张「完全空白回合」的码时，本行必须红。
    for (const c of SILENT_NON_TERMINAL_AGENT_ERROR_CODES) {
      expect(NON_TERMINAL_AGENT_ERROR_CODES.has(c)).toBe(true)
    }
  })
})
