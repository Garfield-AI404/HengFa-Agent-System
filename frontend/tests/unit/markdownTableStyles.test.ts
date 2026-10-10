/**
 * 聊天 markdown 表格样式契约测试（[2026-09-13] 五表连排补样式）。
 *
 * 为什么读 CSS 源文件而不是断言 computedStyle：vitest 的 jsdom 环境不会加载
 * 全局样式表（CSS import 被 stub），断言计算样式没有意义。这里退一步，对
 * src/styles/global.css 的**源码**做契约断言——它正是部署产物的真源，规则一旦
 * 被改回「无间距 / nowrap / 裸选择器」，用例即失败。
 *
 * 覆盖四件事（对应任务验收）：
 *  1. 表格有纵向间距（表与表、表与前后块不再贴死）；
 *  2. 表头有底色 + 字重；
 *  3. 单元格有边框 + 内边距；
 *  4. 单元格显式 white-space: normal（覆盖继承自 .chat-message__content 的
 *     pre-wrap），且规则全部限定在 .chat-message__content--markdown 作用域内。
 */
/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// 直接读样式真源文本。不用 `?raw` 导入——vitest 配置 `css: false` 下 Vite
// 的 ?raw 会返回空串；也不用 import.meta.url（本环境非 file: scheme）。
// tsconfig.app.json 的 types 只含 vite/client，故本文件顶部显式引用 node 类型。
const css = readFileSync(resolve(process.cwd(), 'src/styles/global.css'), 'utf8')

/** 取某个选择器片段对应的第一条规则块（{ 到 } 之间的声明文本）。 */
function ruleBlock(selectorFragment: string): string {
  const start = css.indexOf(selectorFragment)
  if (start < 0) throw new Error(`未找到选择器：${selectorFragment}`)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  if (open < 0 || close < 0) throw new Error(`规则块不完整：${selectorFragment}`)
  return css.slice(open + 1, close)
}

const SCOPE = '.chat-message__content--markdown'

describe('聊天 markdown 表格样式契约', () => {
  it('表格规则限定在聊天 markdown 作用域内（不写裸 table 选择器）', () => {
    expect(css).toContain(`${SCOPE} table {`)
    // 全仓不得出现以 table/th/td/thead/tbody/tfoot/tr 开头的裸选择器
    const bare = /(^|\n)[ \t]*(table|thead|tbody|tfoot|tr|th|td)\b[^{]*\{/
    expect(css.match(bare)).toBeNull()
  })

  it('表格有纵向间距，表与表不再贴死（{2026-09-13} 对密度归零的有意例外）', () => {
    const block = ruleBlock(`${SCOPE} table {`)
    const margin = /margin:\s*([^;]+);/.exec(block)?.[1]?.trim()
    expect(margin).toBeDefined()
    // 纵向不能为 0（这正是五张表连排糊成一片的根因）
    expect(margin?.startsWith('0 ')).toBe(false)
    expect(margin).not.toBe('0')
  })

  it('表头有底色 + 字重，能与数据行区分', () => {
    const block = ruleBlock(`${SCOPE} th {`)
    expect(block).toMatch(/background-color:\s*var\(--ds-/)
    expect(block).toMatch(/font-weight:\s*[5-9]00/)
  })

  it('单元格有边框 + 内边距（数字有骨架、不糊）', () => {
    const block = ruleBlock(`${SCOPE} td {`)
    expect(block).toMatch(/border:\s*1px solid/)
    expect(block).toMatch(/padding:\s*\d/)
  })

  it('单元格显式 white-space: normal，覆盖容器继承的 pre-wrap', () => {
    const block = ruleBlock(`${SCOPE} td {`)
    expect(block).toMatch(/white-space:\s*normal/)
    expect(block).not.toMatch(/white-space:\s*nowrap/)
    // 容器本身仍保留 pre-wrap（不动既有折行语义）
    expect(ruleBlock('.chat-message__content {')).toMatch(
      /white-space:\s*pre-wrap/,
    )
  })
})
