/**
 * 导出完成卡片（2026-09-01 导出功能批次）。
 *
 * - 数据源：SSE export_ready 事件 payload（export_data 成功落盘后经
 *   ctx.sink 发射），reducer 存入 state 后经 props 传入；
 * - 下载：<a href={exportDownloadUrl(fileId)}> 直接打开 GET
 *   /chat/export/{file_id}（manager 门禁 + Cookie 会话；后端按 meta
 *   校验存在/过期，过期 404 EXPORT_FILE_EXPIRED 提示重新导出）；
 * - 实体中文名映射与后端 mcp/scopes.py QUERYABLE_ENTITIES 对齐；
 * - 单行紧凑布局：文件名 + 实体/行数摘要与下载按钮同行（复用
 *   confirmation-panel 家族类 + .export-card__name 一个新增 CSS 类，
 *   见 global.css）。
 */
import { FileDown, FileSpreadsheet } from 'lucide-react'
import { exportDownloadUrl } from '../api/chat.api'
import type { ExportCardInfo } from '../pages/chat/types'

interface ExportCardProps {
  /** 导出完成摘要（SSE export_ready 事件提取形态；null = 不渲染）。 */
  exportInfo: ExportCardInfo | null
}

/**
 * 实体中文名。键集 = 后端只读查询白名单
 * administrative_agent/mcp/scopes.py 的 QUERYABLE_ENTITIES（14 个，导出
 * 跟随读，见 domains/export_fields.py）；标签取值照抄 ERP 权威来源
 * hengfa-erp/backend/app/mcp/registry.py 的 ENTITY_REGISTRY[*]["label"]，
 * 不在此自拟。取不到时回退原始实体名（见下方 entityLabel 兜底）。
 */
const ENTITY_LABELS: Record<string, string> = {
  // registry.py:277 / 281 / 261 / 269 / 273
  metal_orders: '钣金订单',
  powder_orders: '塑粉订单',
  customers: '客户',
  suppliers: '供货商',
  employees: '员工',
  // registry.py:288 / 301
  transactions: '收支记录',
  transaction_categories: '收支分类',
  // registry.py:265 / 285 / 292 / 295 / 298 / 311 / 315
  customer_contacts: '客户联系人',
  attendance: '考勤记录',
  ar_payments: '应收收款',
  ap_payments: '应付付款',
  salary_payments: '工资发放',
  powder_inventory: '塑粉库存色号',
  powder_consumption: '塑粉消耗记录',
}

export function ExportCard({ exportInfo }: ExportCardProps) {
  if (!exportInfo) return null

  const entityLabel = ENTITY_LABELS[exportInfo.entity] ?? exportInfo.entity
  const rowText =
    exportInfo.total > exportInfo.rowCount
      ? `${exportInfo.rowCount} 行（全量 ${exportInfo.total} 行）`
      : `${exportInfo.rowCount} 行`

  return (
    <div className="confirmation-panel">
      <div className="confirmation-panel__actions">
        <span className="export-card__name">
          <FileSpreadsheet aria-hidden="true" size={16} />
          {exportInfo.filename}
          <span className="panel__muted"> · {entityLabel} · {rowText}</span>
        </span>
        <a
          className="btn btn--primary"
          href={exportDownloadUrl(exportInfo.fileId)}
          download={exportInfo.filename}
          aria-label={`下载导出文件 ${exportInfo.filename}`}
        >
          <FileDown aria-hidden="true" size={16} /> 下载 Excel
        </a>
      </div>
    </div>
  )
}
