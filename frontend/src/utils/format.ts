/**
 * 展示格式化工具（只做展示，不参与业务判断）。
 *
 * 注入防护（10.6 L1193）：外部字符串（Excel 单元格、订单 notes、客户备注、
 * ERP 返回自由文本）在网页输出必须做长度限制与 HTML 转义——React 文本节点
 * 默认转义（无需 dangerouslySetInnerHTML），本模块提供显式转义与截断工具
 * 供任何需要字符串拼接/属性注入的位置使用，并有组件测试覆盖（D23 ③）。
 */

/** 外部自由文本显示上限（10.6 L1193；与 api/schemas.ts EXTERNAL_TEXT_MAX 一致）。 */
export const EXTERNAL_DISPLAY_MAX = 500

/** 显式 HTML 转义（供非 React 文本节点场景；React 渲染默认已转义）。 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 外部字符串长度截断（10.6 L1193：日志与网页输出的长度限制）。 */
export function truncateExternal(value: string, max = EXTERNAL_DISPLAY_MAX): string {
  if (value.length <= max) return value
  return `${value.slice(0, max)}…`
}

/** 外部值展示：null/undefined → 占位符；长文本截断；对象 JSON 序列化后截断。 */
export function displayExternal(value: unknown, max = EXTERNAL_DISPLAY_MAX): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return truncateExternal(JSON.stringify(value), max)
  return truncateExternal(String(value), max)
}

/** ISO 时间 → 本地 "YYYY-MM-DD HH:mm"；空值返回占位符。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 数值序列化（含 undefined 处理；非外部字符串场景）。 */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
