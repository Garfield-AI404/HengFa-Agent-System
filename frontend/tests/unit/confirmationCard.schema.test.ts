/**
 * confirmationCardSchema 透传测试（2026-09-12 多明细卡）。
 *
 * 背景：确认卡新增两个纯展示级、后端条件输出（缺省不落 JSON）的字段
 * `row_header_label`（矩阵首列表头）与 `row_quantifier`（折叠汇总量词）。
 *
 * ⚠️ zod 的 `z.object()` **默认剥离未声明的键**——若 schema 里漏声明这两个字段，
 * 它们会在 parse 阶段被静默丢弃，组件永远读到 undefined、只能走回退值（「明细」/
 * 「条」），后端传入的「员工」/「人」到不了 UI，且不报任何错。本测试即为防该坑复发
 * 的钉子：断言解析后**保留**这两个字段。
 */
import { describe, expect, it } from 'vitest'
import { confirmationCardSchema } from '../../src/api/schemas'

const baseCard = {
  operation_type: '创建订单',
  target: '批量创建 2 条',
  fields_to_write: {
    '1. 客户 铭泰 / M1': { 操作: '新建', 字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 } },
  },
  auto_filled: [],
  expected_impact: '在 ERP 写入 2 条订单',
  limitations: [],
  payload_hash: 'hash-1',
}

describe('confirmationCardSchema 展示字段透传', () => {
  it('保留 row_header_label / row_quantifier（防 zod 剥离坑复发）', () => {
    const parsed = confirmationCardSchema.parse({
      ...baseCard,
      row_header_label: '明细',
      row_quantifier: '条',
    })

    expect(parsed.row_header_label).toBe('明细')
    expect(parsed.row_quantifier).toBe('条')
  })

  it('旧卡不带这两个键：解析通过且字段为 undefined（回退值由组件负责）', () => {
    const parsed = confirmationCardSchema.parse(baseCard)

    expect(parsed.row_header_label).toBeUndefined()
    expect(parsed.row_quantifier).toBeUndefined()
  })

  it('保留 source_quote / source_note（防 zod 剥离坑复发）', () => {
    const parsed = confirmationCardSchema.parse({
      ...baseCard,
      source_quote: '把数量改成 5 个',
      source_note: '分类由模型按你的说法判定；若与你的说法不符，请在确认前修改',
    })

    expect(parsed.source_quote).toBe('把数量改成 5 个')
    expect(parsed.source_note).toBe('分类由模型按你的说法判定；若与你的说法不符，请在确认前修改')
  })

  it('旧卡不带 source_quote / source_note：解析通过且字段为 undefined', () => {
    const parsed = confirmationCardSchema.parse(baseCard)

    expect(parsed.source_quote).toBeUndefined()
    expect(parsed.source_note).toBeUndefined()
  })
})
