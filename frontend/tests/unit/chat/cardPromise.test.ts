/**
 * [2026-09-28 复审 P2] CARD_PROMISE_PATTERN 真值表。
 *
 * 复审指出旧模式（`点击…{0,6}确认卡|确认卡…{0,4}(完成|核对|写入)`）比注释
 * 声称的宽：第二支的 0~4 字空隙允许 0 字，于是「陈述既成事实」被当成承诺；
 * 第一支也命中否定句。收紧后「陈述句 / 否定句」全部不再命中，而真正的祈使
 * 式承诺仍命中。本文件把那张表钉成用例（命中/不命中各若干条）。
 *
 * 另钉一条**不变量**：``CARD_NOT_GENERATED_NOTICE`` 自身必须仍被模式命中
 * （reducer 的幂等虽靠字面量 ``includes`` 先判——reducer.ts:228——此条把
 * 该不变量也固定在模式层，防未来重构把顺序调成「先判模式」时静默破功）。
 */
import { describe, expect, it } from 'vitest'
import { CARD_NOT_GENERATED_NOTICE, CARD_PROMISE_PATTERN } from '../../../src/pages/chat/constants'

/** 命中：确属祈使式承诺 / 引导用户点击确认卡。 */
const SHOULD_MATCH = [
  '请点击确认卡完成写入。',
  '请点击确认卡核对后写入。',
  '请点击确认卡。',
  '点击确认卡完成写入。',
  '写入计划如下：\n- 员工：杨峰\n\n请点击确认卡完成写入。',
] as const

/** 不命中：陈述既成事实 / 否定 / 劝阻（复审实测曾全部误命中）。 */
const SHOULD_NOT_MATCH = [
  '好的，已通过确认卡写入。',
  '本次已由确认卡写入 ERP。',
  '确认卡核对无误，系统已执行。',
  '上一步的确认卡完成了写入。',
  '未点击确认卡。',
  '请勿点击确认卡完成写入。',
] as const

describe('CARD_PROMISE_PATTERN', () => {
  for (const s of SHOULD_MATCH) {
    it(`命中：${JSON.stringify(s)}`, () => {
      expect(CARD_PROMISE_PATTERN.test(s)).toBe(true)
    })
  }

  for (const s of SHOULD_NOT_MATCH) {
    it(`不命中：${JSON.stringify(s)}`, () => {
      expect(CARD_PROMISE_PATTERN.test(s)).toBe(false)
    })
  }

  it('不变量：CARD_NOT_GENERATED_NOTICE 自身仍被模式命中', () => {
    expect(CARD_PROMISE_PATTERN.test(CARD_NOT_GENERATED_NOTICE)).toBe(true)
  })
})
