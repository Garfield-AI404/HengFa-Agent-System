/**
 * 注入防护工具单测（10.6 L1193；D23 ③）：
 * - escapeHtml：外部字符串中的 HTML 特殊字符被转义（非 React 文本节点场景）；
 * - truncateExternal：超长外部字符串截断 + 省略号，不截断多字节字符；
 * - displayExternal：null/对象/长文本的安全展示；
 * - 注入文本（<script>/事件属性/忽略系统规则类提示）处理为纯文本数据。
 */
import { describe, expect, it } from 'vitest'
import { displayExternal, escapeHtml, truncateExternal } from '../../src/utils/format'

describe('escapeHtml（10.6 注入防护）', () => {
  it('转义 & < > " \' 五类字符', () => {
    expect(escapeHtml(`<script>alert("x")</script> & 'on'`)).toBe(
      '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#39;on&#39;',
    )
  })

  it('注入载荷转义后不含任何原始标签起始符，无法形成可执行结构', () => {
    const escaped = escapeHtml('<img src=x onerror=alert(1)>')
    expect(escaped).not.toContain('<') // 无任何未转义左尖括号
    expect(escaped).not.toContain('<img')
    expect(escaped).not.toMatch(/<\s*[a-z]/i)
  })

  it('普通文本原样返回', () => {
    expect(escapeHtml('对账 2026-08 客户差异')).toBe('对账 2026-08 客户差异')
  })
})

describe('truncateExternal（10.6 长度限制）', () => {
  it('未超长时原样返回', () => {
    expect(truncateExternal('客户备注：无')).toBe('客户备注：无')
  })

  it('超长截断并追加省略号，总长 = 上限 + 1', () => {
    const result = truncateExternal('请'.repeat(600), 500)
    expect(result).toHaveLength(501)
    expect(result.endsWith('…')).toBe(true)
  })

  it('自定义上限', () => {
    expect(truncateExternal('示例贸易有限公司', 4)).toBe('示例贸易…')
  })
})

describe('displayExternal（安全展示）', () => {
  it('null/undefined → 占位符', () => {
    expect(displayExternal(null)).toBe('—')
    expect(displayExternal(undefined)).toBe('—')
  })

  it('对象序列化且超长截断', () => {
    const big = { note: 'x'.repeat(600) }
    const result = displayExternal(big, 100)
    expect(result.endsWith('…')).toBe(true)
    expect(result.length).toBeLessThanOrEqual(101)
  })

  it('注入文本作为数据呈现不产生标记', () => {
    const payload = '<script>alert(1)</script>'
    const result = displayExternal(payload)
    expect(result).toBe(payload) // 原样数据；渲染层转义（React 文本节点）
  })
})
