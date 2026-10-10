/**
 * 对账差异报告卡片（2026-08-31 对话对账批次 4）。
 *
 * - 数据源两层：摘要（SSE reconcile_report 事件 payload，随流到达）由
 *   reducer 存入 state 后经 props 传入；明细（entries/conversion_errors/
 *   data_completeness 等）在用户点击「查看明细」时按 reportId 另拉
 *   GET /chat/reports/{report_id}?thread_id=…（后端归属校验，跨线程 404）。
 * - 分类值域与后端 business/reconciliation_rules.py 常量对齐：
 *   matched/erp_missing/field_diff/ambiguous/customer_unmatched/
 *   key_incomplete（「unique」不是实际值——唯一匹配的实际常量是 matched）；
 * - subject_match_status 值域（business/matching.py MatchOutcome）：
 *   matched / ambiguous / not_found；
 * - SSE payload 无 data_completeness 字段，故「数据不完整需复核」提示在
 *   明细加载后按详情返回的 data_completeness 展示（partial）；
 * - 样式复用 ConfirmationPanel 同款 class（confirmation-panel 家族 +
 *   data-table + btn），不新增 CSS。
 */
import { useState } from 'react'
import { ChevronDown, ChevronUp, ClipboardList, TriangleAlert } from 'lucide-react'
import { ApiError, toApiError } from '../api/client'
import { fetchReconcileReportApi } from '../api/chat.api'
import type {
  ReconcileConversionError,
  ReconcileReportDetail,
  ReconcileReportEntry,
} from '../api/types'
import { ErrorNotice } from './ErrorNotice'
import { displayExternal, truncateExternal } from '../utils/format'
import type { ReconcileReport } from '../pages/chat/types'

interface ReconcileReportCardProps {
  /** 报告摘要（SSE reconcile_report 事件提取形态；null = 不渲染）。 */
  report: ReconcileReport | null
  /** 当前会话线程（明细端点归属校验所需；与摘要卡同线程）。 */
  threadId: string
}

/** 差异分类中文标签（后端 reconciliation_rules.py 常量值域）。 */
const CLASSIFICATION_LABELS: Record<string, string> = {
  matched: '唯一匹配',
  erp_missing: 'ERP 缺失',
  field_diff: '字段差异',
  ambiguous: '匹配歧义',
  customer_unmatched: '主体未匹配',
  key_incomplete: '键不完整',
}

/** 展示顺序（已知分类在前，未知分类追加在后防漏展示）。 */
const CLASSIFICATION_ORDER = [
  'matched',
  'erp_missing',
  'field_diff',
  'ambiguous',
  'customer_unmatched',
  'key_incomplete',
]

/** 主体匹配状态中文标签（backend matching.py MatchOutcome.status）。 */
const SUBJECT_MATCH_LABELS: Record<string, string> = {
  matched: '主体已唯一匹配',
  ambiguous: '主体匹配歧义（多名同名候选）',
  not_found: '未找到匹配主体',
}

function diffText(diff: { field: string; excel_value: string | null; erp_value: string | null }): string {
  return `${diff.field}：Excel ${displayExternal(diff.excel_value)} → ERP ${displayExternal(diff.erp_value)}`
}

export function ReconcileReportCard({ report, threadId }: ReconcileReportCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [detail, setDetail] = useState<ReconcileReportDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  // 新一轮分析到达（reportId 变化）：渲染期同步重置展开态与明细，旧明细不挂
  // 新报告（React「adjusting state when props change」模式，与 useTaskStream
  // 的 subKey 同款；不用 effect 避免级联渲染，react-hooks/set-state-in-effect）
  const [prevReportId, setPrevReportId] = useState(report?.reportId ?? null)
  if (report !== null && prevReportId !== report.reportId) {
    setPrevReportId(report.reportId)
    setExpanded(false)
    setDetail(null)
    setError(null)
  }

  if (!report) return null

  const toggleDetail = async (): Promise<void> => {
    if (expanded) {
      setExpanded(false)
      return
    }
    setExpanded(true)
    if (detail) return // 已加载过：直接展开缓存，不重复拉取
    if (!threadId) {
      // [2026-09-11 上传链路收口] 这是前端前置条件缺失（拿不到 threadId），
      // 不是网络故障：此前归 NETWORK_ERROR → ErrorNotice 的 HINTS 覆盖 message，
      // 用户看到的是「无法连接服务器」且本中文说明成死代码。改用无 HINT 的
      // THREAD_MISSING，让具体原因经回退路径透出。
      setError(
        new ApiError(0, {
          error_code: 'THREAD_MISSING',
          message: '缺少会话线程，无法加载对账明细。',
        }),
      )
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await fetchReconcileReportApi(report.reportId, threadId)
      setDetail(data)
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setLoading(false)
    }
  }

  const knownRows = CLASSIFICATION_ORDER.filter(
    (key) => report.summaryCounts[key] !== undefined,
  ).map((key) => [key, report.summaryCounts[key]] as const)
  const unknownRows = Object.entries(report.summaryCounts)
    .filter(([key]) => !CLASSIFICATION_ORDER.includes(key))
    .sort(([a], [b]) => a.localeCompare(b))
  const summaryRows = [...knownRows, ...unknownRows]

  return (
    <div className="confirmation-panel">
      <h3 className="confirmation-panel__section">
        <ClipboardList aria-hidden="true" size={14} /> 对账差异报告
      </h3>

      <dl className="confirmation-panel__meta">
        <div>
          <dt>主体匹配</dt>
          <dd>{SUBJECT_MATCH_LABELS[report.subjectMatchStatus] ?? (report.subjectMatchStatus || '未知')}</dd>
        </div>
        <div>
          <dt>Excel 行数</dt>
          <dd>{report.rowCount}</dd>
        </div>
      </dl>

      {summaryRows.length > 0 ? (
        <>
          <h3 className="confirmation-panel__section">差异分类</h3>
          <table className="data-table data-table--compact">
            <tbody>
              {summaryRows.map(([key, count]) => (
                <tr key={key}>
                  <th scope="row">{CLASSIFICATION_LABELS[key] ?? key}</th>
                  <td>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="panel__muted">无差异条目。</p>
      )}

      {detail && detail.data_completeness === 'partial' ? (
        <p className="panel__muted" role="alert">
          <TriangleAlert aria-hidden="true" size={14} /> 数据不完整（部分行解析或匹配受限），结果需人工复核后再确认执行。
        </p>
      ) : null}

      <div className="confirmation-panel__actions">
        <button
          type="button"
          className="btn"
          onClick={() => void toggleDetail()}
          disabled={loading}
          aria-expanded={expanded}
          aria-label={expanded ? '收起对账明细' : '查看对账明细'}
        >
          {expanded ? <ChevronUp aria-hidden="true" size={16} /> : <ChevronDown aria-hidden="true" size={16} />}
          {loading ? '加载中…' : expanded ? '收起明细' : '查看明细'}
        </button>
      </div>

      <ErrorNotice error={error} />

      {expanded && detail ? (
        <div className="confirmation-panel__detail">
          <EntryTable entries={detail.entries ?? []} />
          {detail.conversion_errors.length > 0 ? (
            <ConversionErrors errors={detail.conversion_errors} />
          ) : null}
          {!detail.entries?.length && detail.conversion_errors.length === 0 ? (
            <p className="panel__muted">明细为空。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** 逐条差异明细（行号 + 分类 + 字段级前后值）。 */
function EntryTable({ entries }: { entries: ReconcileReportEntry[] }) {
  if (entries.length === 0) return null
  return (
    <table className="data-table data-table--compact">
      <thead>
        <tr>
          <th scope="col">行号</th>
          <th scope="col">分类</th>
          <th scope="col">说明</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={entry.row_number}>
            <th scope="row">{entry.row_number}</th>
            <td>{CLASSIFICATION_LABELS[entry.classification] ?? entry.classification}</td>
            <td>
              {entry.message ? <p>{truncateExternal(entry.message)}</p> : null}
              {entry.diffs && entry.diffs.length > 0 ? (
                <ul className="confirmation-panel__list">
                  {entry.diffs.map((diff) => (
                    <li key={diff.field}>{diffText(diff)}</li>
                  ))}
                </ul>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 行级转换错误（行号 + 列 + 原因；6.3 规则 5「列出行号和原因」）。 */
function ConversionErrors({ errors }: { errors: ReconcileConversionError[] }) {
  return (
    <>
      <h3 className="confirmation-panel__section">
        <TriangleAlert aria-hidden="true" size={14} /> 转换错误（{errors.length} 条）
      </h3>
      <ul className="confirmation-panel__list">
        {errors.map((err) => (
          <li key={`${err.row_number}-${err.excel_column}`}>
            第 {err.row_number} 行（{err.excel_column}
            {err.target_field ? ` → ${err.target_field}` : ''}）：{truncateExternal(err.reason)}
          </li>
        ))}
      </ul>
    </>
  )
}
