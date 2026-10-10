/**
 * ExportCard 实体中文名映射测试（2026-09-12 读白名单扩到 14 实体后补）。
 *
 * 钉住两件事：
 * - 后端只读查询白名单 QUERYABLE_ENTITIES 的全部 14 个实体在导出卡上都能
 *   取到中文标签（标签照抄 ERP mcp/registry.py ENTITY_REGISTRY[*]["label"]，
 *   改动若漏登记某实体此用例会红）；
 * - 未知实体回退为原始实体名（不空白、不报错、不拟中文）。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ExportCard } from '../../src/components/ExportCard'
import type { ExportCardInfo } from '../../src/pages/chat/types'

/** 14 个可查询实体 → 中文标签（与 ExportCard ENTITY_LABELS 同源期望）。 */
const EXPECTED_LABELS: Record<string, string> = {
  metal_orders: '钣金订单',
  powder_orders: '塑粉订单',
  customers: '客户',
  suppliers: '供货商',
  employees: '员工',
  transactions: '收支记录',
  transaction_categories: '收支分类',
  customer_contacts: '客户联系人',
  attendance: '考勤记录',
  ar_payments: '应收收款',
  ap_payments: '应付付款',
  salary_payments: '工资发放',
  powder_inventory: '塑粉库存色号',
  powder_consumption: '塑粉消耗记录',
}

function info(entity: string, over: Partial<ExportCardInfo> = {}): ExportCardInfo {
  return {
    fileId: 'f-1',
    filename: '导出结果.xlsx',
    entity,
    rowCount: 3,
    total: 3,
    expiresAt: '2026-09-11T00:00:00Z',
    ...over,
  }
}

describe('ExportCard 实体中文名', () => {
  it('覆盖后端白名单全部 14 个实体（无一漏登记）', () => {
    expect(Object.keys(EXPECTED_LABELS)).toHaveLength(14)
  })

  for (const [entity, label] of Object.entries(EXPECTED_LABELS)) {
    it(`${entity} → 「${label}」（不显示英文原名）`, () => {
      const { container } = render(<ExportCard exportInfo={info(entity)} />)
      expect(container.textContent).toContain(label)
      expect(container.textContent).not.toContain(entity)
    })
  }

  it('未知实体回退为原始实体名（不空白、不报错）', () => {
    const { container } = render(<ExportCard exportInfo={info('unknown_entity')} />)
    expect(container.textContent).toContain('unknown_entity')
  })

  it('exportInfo 为 null 时不渲染', () => {
    const { container } = render(<ExportCard exportInfo={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
