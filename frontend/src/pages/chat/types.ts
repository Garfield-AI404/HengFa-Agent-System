/**
 * 对话页领域类型（2026-08-29 从 ChatPage.tsx 抽出）。
 *
 * 抽出目的：让消息列表、输入区、事件 reducer 三处共用同一份形状定义，
 * 并使 reducer 成为可独立单测的纯函数（不必渲染整棵 ChatPage）。
 */
export interface ChatMessage {
  /**
   * 消息唯一标识（2026-08-29 修复）。此前列表渲染用数组下标作 key，
   * 而本列表存在删除（发送失败移除乐观气泡）与插入（流式气泡落定）操作，
   * 下标 key 会让 React 错误复用 DOM——表现为消息错位、打字机动画串到
   * 错误气泡。改用稳定 id 后 React 能正确匹配每一条消息。
   */
  id: string
  role: 'user' | 'assistant'
  content: string
  taskId?: string
  /** 消息下方的小字提示（如取消/失败补充说明）。 */
  note?: string
  /** [2026-08-24 流式改造] 生成中标记：[2026-09-13 整轮动效] true = 该气泡
   *  处于生成中（整轮只显示生成中动效、content 保持空；agent_reply 终态
   *  事件整段填入 content 并落定为 false）。 */
  streaming?: boolean
}

/**
 * 消息 id 生成器：模块级自增计数器。
 * 用计数器而非 crypto.randomUUID()，是为了 id 在测试里可预期（randomUUID
 * 还需安全上下文，http 直连局域网调试时会不可用）。模块级保证跨组件
 * 重挂载也不会重复。
 */
let messageSeq = 0
export const nextMessageId = (): string => `msg-${++messageSeq}`

/**
 * [2026-08-31 批次 4] 对账差异报告摘要（SSE reconcile_report 事件 payload 的
 * 已提取形态；payload 键名以后端 tools/reconciliation.py ctx.sink 实发为准：
 * report_id / summary_counts / row_count / subject_match_status）。
 * 明细（entries/diffs 等）由 ReconcileReportCard 点击「查看明细」时按
 * reportId 另拉 GET /chat/reports/{report_id}。
 */
export interface ReconcileReport {
  reportId: string
  /** 分类 → 计数（分类值域见后端 reconciliation_rules.py 常量：
   *  matched/erp_missing/field_diff/ambiguous/customer_unmatched/key_incomplete）。 */
  summaryCounts: Record<string, number>
  rowCount: number
  /** 主体（客户/供货商）匹配状态：matched / ambiguous / not_found。 */
  subjectMatchStatus: string
}

/**
 * [2026-09-01 导出功能批次] 导出完成摘要（SSE export_ready 事件 payload 的
 * 已提取形态；payload 键名以后端 agent/tools/export.py ctx.sink 实发为准：
 * file_id / filename / entity / row_count / total / expires_at）。
 * 下载走 GET /chat/export/{file_id}（manager 门禁，Cookie 会话）。
 */
export interface ExportCardInfo {
  fileId: string
  /** 下载文件名（如 metal_orders_导出_20260901.xlsx）。 */
  filename: string
  /** 实体名（∈ 后端只读白名单 QUERYABLE_ENTITIES，共 14 个；中文名映射见
   *  components/ExportCard.tsx ENTITY_LABELS）。 */
  entity: string
  /** 实际写入 Excel 的行数。 */
  rowCount: number
  /** ERP 全量总数（可能大于 rowCount——导出按上限截断会拒绝而非截断）。 */
  total: number
  /** 过期时刻（ISO；文件保留 1 小时）。 */
  expiresAt: string
}
