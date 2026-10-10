/**
 * 写入计划确认面板（Phase 3a 契约：card 载荷含 operation_type/target/
 * fields_to_write/auto_filled/expected_impact/limitations + payload_hash）。
 *
 * [2026-09-10 确认卡改版] 纯展示层调整，不改任何事件/接口字段、不发送 payload_hash：
 * - 不再渲染 operation_type（口径：操作类型对用户无意义，后端字段保留）；
 * - target 为空串（钣金/塑粉创建卡）时不渲染「目标」行；
 * - fields_to_write 全网格边框（样式作用域限定在 .confirmation-panel，见 global.css）；
 * - 考勤卡改为紧凑竖向表（员工 | 操作 | 请假（分钟） | 加班（分钟）），去掉嵌套子表；
 *   「操作=保持」的无变化员工（不论位置）全局合并为一行汇总（<details>/<summary>，键盘可达）；
 * - 拒绝按钮文案「拒绝确认」→「拒绝」（样式/图标/aria-label/title 一并对齐）。
 *
 * [2026-09-12 多明细卡泛化] 订单/收支确认卡改为「一张卡多条明细」，形状与考勤卡
 * 同构（外层键 = 业务标识，值 = 含「操作」键的 dict），因此复用 planMode 矩阵渲染，
 * 不新增布局分支。改动仅为：
 * - 矩阵分支的单元格由 displayExternal 改为 NestedValue——「字段和值」是嵌套 dict，
 *   直接用 displayExternal 会渲染成 JSON 串/`[object Object]`（静默不可读）；
 *   折叠组内的值同样改用 NestedValue（考勤值全为标量，逐字符输出不变）；
 * - 首列表头与折叠量词由写死的「员工」「人」改为读卡面字段 row_header_label /
 *   row_quantifier（缺省回退「明细」「条」）。考勤卡由后端显式传入「员工」「人」，
 *   零回归不再依赖巧合。
 *
 * ⚠️ 保留下方 `planMode === false` 的旧渲染分支（「字段 | 新值」两列表）：它**不是
 * 死代码**，而是**旧形状卡的兼容路径**——上线瞬间可能仍有在飞的确认卡（checkpoint
 * 里的 pending_confirmation 不做迁移），其 fields_to_write 是扁平标量映射，走不了
 * 矩阵分支；单条订单/收支卡也按旧形状产出（口径：1 条走旧样式，≥2 条走新样式）。
 * 删掉它会让这类卡整卡无法渲染。后人不要删。
 *
 * - 由父组件驱动（消息流内联卡片）：onConfirm(approved, payload_hash) 委托
 *   POST /chat/confirm（图恢复续传），确认/拒绝均不直接调任务状态机端点
 *   （旧 /tasks/:id/confirm 路径不恢复图执行，仅保留给旧前端）；
 * - 确认（approved=True）与拒绝（approved=False → 任务 cancelled，8.1）
 *   均为显式命令按钮 + Lucide 图标可访问名称；提交期间禁用防重复（2.7.4）；
 * - 拒绝按钮为危险操作（明确命令按钮）；
 * - 提交失败时卡片保持挂起并就地展示错误（供用户重试）；
 * - re_requested=true（hash 失效重挂）：卡顶提示原确认已失效需重新确认；
 * - 外部字符串由 React 默认 HTML 转义 + truncateExternal 截断（10.6 L1193）。
 *
 * [2026-10-05 卡面原话/来源提示] 新增两个纯展示级、后端条件输出的键（旧卡不带，
 * 缺省时不渲染任何节点、DOM 零变化）：
 * - `source_quote`（整卡级，最近一条用户原话全文）→ 字段表上方小字「原话：…」；
 * - `source_note`（仅收支卡，分类来源说明）→ 字段表下方提示行。
 * 二者都不进 payload_hash；长文本靠 overflow-wrap/pre-wrap 换行不撑破卡面。
 */
import { useState } from 'react'
import { ClipboardCheck, ListChecks, RotateCcw, XCircle } from 'lucide-react'
import { ApiError, toApiError } from '../api/client'
import type { PendingConfirmation } from '../api/types'
import { ErrorNotice } from './ErrorNotice'
import { displayExternal, truncateExternal } from '../utils/format'

interface ConfirmationPanelProps {
  confirmation: PendingConfirmation
  /** 决策提交（Phase 3a：POST /chat/confirm）；reject 时 approved=false。
   *  抛错由面板就地展示（卡片保持挂起）；成功由父组件乐观清卡。 */
  onConfirm: (approved: boolean, payloadHash: string) => Promise<void>
}

type ActionState = 'idle' | 'confirming' | 'cancelling'

/**
 * 计划动作字典里的动作键与「无变化」值。
 *
 * 「保持」字面量来源：src/administrative_agent/domains/orders.py:139 的
 * ACTION_VALUE_LABELS["unchanged"]（后端 localize_confirmation_fields 已把
 * created/updated/unchanged 中文化为 新建/更新/保持）。前端只做展示匹配，
 * 不参与业务判断。
 */
const ACTION_FIELD = '操作'
const ACTION_UNCHANGED = '保持'

/** 值是否为计划动作字典：dict 且含「操作」键（考勤卡形状）。否则返回 null。 */
function asActionDict(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  return ACTION_FIELD in record ? record : null
}

/**
 * 嵌套对象（非计划动作字典）逐行展开为键值对子表，其余走 displayExternal。
 *
 * 后端 jsonRecord 放行 unknown、前端不做类型收窄：直接 String(value) 会输出
 * `[object Object]`（静默不可读）。此处仅兜底非考勤形状的对象值；考勤卡
 * （操作字典）改由上层紧凑表渲染，不再走本分支。
 */
function NestedValue({ value }: { value: unknown }) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return <>{displayExternal(value)}</>
  }
  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) return <>—</>
  return (
    <table className="data-table data-table--compact">
      <tbody>
        {entries.map(([key, nested]) => (
          <tr key={key}>
            <th scope="row">{key}</th>
            <td>{displayExternal(nested)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

type RowGroup =
  | { kind: 'normal'; field: string; value: unknown }
  | { kind: 'action'; field: string; record: Record<string, unknown> }
  | { kind: 'folded'; rows: Array<{ field: string; record: Record<string, unknown> }> }

export function ConfirmationPanel({ confirmation, onConfirm }: ConfirmationPanelProps) {
  const [action, setAction] = useState<ActionState>('idle')
  const [error, setError] = useState<ApiError | null>(null)

  const busy = action === 'confirming' || action === 'cancelling'
  const card = confirmation.card
  // hash 以事件/历史载荷为准（re_requested 重挂时为新 hash，旧确认已失效）
  const payloadHash = confirmation.payload_hash

  const submit = async (approved: boolean): Promise<void> => {
    if (busy) return // 2.7.4 防重复提交
    setAction(approved ? 'confirming' : 'cancelling')
    setError(null)
    try {
      await onConfirm(approved, payloadHash)
      // 成功：父组件（ChatPage）乐观清卡并订阅续传事件（tool_running →
      // tool_result → agent_reply），此处不置完成态
    } catch (err) {
      setAction('idle')
      setError(toApiError(err))
    }
  }

  // 目标为空串（钣金/塑粉创建卡，契约 1）时不渲染「目标」行。
  const target = typeof card.target === 'string' ? card.target.trim() : ''
  const showTarget = target !== ''

  // [2026-10-05 卡面原话/来源提示] 纯展示级、后端条件输出（旧卡不带这两个键）；
  // 不进 payload_hash。取到空串一律视为未提供——两者都没有时不渲染任何节点，
  // 旧卡 DOM 零变化。
  const sourceQuote = typeof card.source_quote === 'string' ? card.source_quote.trim() : ''
  const sourceNote = typeof card.source_note === 'string' ? card.source_note.trim() : ''

  const entries = Object.entries(card.fields_to_write)
  // 计划动作字典的列键（按首次出现顺序）——存在则走考勤紧凑表布局。
  const actionKeys: string[] = []
  for (const [, value] of entries) {
    const record = asActionDict(value)
    if (record) {
      for (const key of Object.keys(record)) {
        if (!actionKeys.includes(key)) actionKeys.push(key)
      }
    }
  }
  const planMode = actionKeys.length > 0

  // 分组：所有「操作=保持」行**全局合并**为唯一一个折叠组（N = 全部保持行数，
  // 与位置无关；展开后按原始顺序列出）。变化行/普通行保持原序在前，折叠汇总
  // 行置于表格末尾。
  const groups: RowGroup[] = []
  const unchangedRows: Array<{ field: string; record: Record<string, unknown> }> = []
  for (const [field, value] of entries) {
    const record = asActionDict(value)
    if (record && record[ACTION_FIELD] === ACTION_UNCHANGED) {
      unchangedRows.push({ field, record })
    } else if (record) {
      groups.push({ kind: 'action', field, record })
    } else {
      groups.push({ kind: 'normal', field, value })
    }
  }
  if (unchangedRows.length > 0) {
    groups.push({ kind: 'folded', rows: unchangedRows })
  }

  return (
    <div className="confirmation-panel">
      <ErrorNotice error={error} />
      {confirmation.re_requested ? (
        <p className="confirmation-panel__invalidated" role="alert">
          <RotateCcw aria-hidden="true" size={14} />
          确认内容已发生变化，原确认已失效，请重新确认。
        </p>
      ) : null}
      {showTarget ? (
        <dl className="confirmation-panel__meta">
          <div>
            <dt>目标</dt>
            <dd>{truncateExternal(card.target)}</dd>
          </div>
        </dl>
      ) : null}

      <h3 className="confirmation-panel__section">
        <ListChecks aria-hidden="true" size={14} /> 将要写入的字段和值
      </h3>
      {sourceQuote !== '' ? (
        <p className="confirmation-panel__source">原话：{sourceQuote}</p>
      ) : null}
      {planMode ? (
        <table className="data-table data-table--compact confirmation-panel__plan">
          <thead>
            <tr>
              <th scope="col">{card.row_header_label ?? '明细'}</th>
              {actionKeys.map((key) => (
                <th key={key} scope="col">
                  {key}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map((group, index) => {
              if (group.kind === 'normal') {
                return (
                  <tr key={`${group.field}-${index}`}>
                    <th scope="row">{group.field}</th>
                    <td colSpan={actionKeys.length}>
                      <NestedValue value={group.value} />
                    </td>
                  </tr>
                )
              }
              if (group.kind === 'action') {
                return (
                  <tr key={`${group.field}-${index}`}>
                    <th scope="row">{group.field}</th>
                    {actionKeys.map((key) => (
                      <td key={key}>
                        <NestedValue value={group.record[key]} />
                      </td>
                    ))}
                  </tr>
                )
              }
              // 无变化员工默认折叠：一行汇总，点开显示全部明细（键盘可达）。
              return (
                <tr key={`folded-${index}`} className="confirmation-panel__fold">
                  <td colSpan={1 + actionKeys.length}>
                    <details>
                      <summary className="confirmation-panel__fold-summary">
                        没有变化的 {group.rows.length} {card.row_quantifier ?? '条'}（点击展开）
                      </summary>
                      <ul className="confirmation-panel__fold-list">
                        {group.rows.map((row) => (
                          <li key={row.field} className="confirmation-panel__fold-item">
                            <span className="confirmation-panel__fold-name">{row.field}</span>
                            {actionKeys.map((key) => (
                              <span key={key}>
                                {key}：<NestedValue value={row.record[key]} />
                              </span>
                            ))}
                          </li>
                        ))}
                      </ul>
                    </details>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        <table className="data-table data-table--compact">
          <thead>
            <tr>
              <th scope="col">字段</th>
              <th scope="col">新值</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(card.fields_to_write).map(([field, value]) => (
              <tr key={field}>
                <th scope="row">{field}</th>
                <td>
                  <NestedValue value={value} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {sourceNote !== '' ? (
        <p className="confirmation-panel__source">{sourceNote}</p>
      ) : null}

      <div className="confirmation-panel__actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => void submit(true)}
          disabled={busy}
          aria-label="确认并执行该写入计划"
        >
          <ClipboardCheck aria-hidden="true" size={16} />
          {action === 'confirming' ? '确认中…' : '确认'}
        </button>
        <button
          type="button"
          className="btn btn--danger"
          onClick={() => void submit(false)}
          disabled={busy}
          aria-label="拒绝并取消任务（危险操作）"
          title="拒绝并取消任务（危险操作）"
        >
          <XCircle aria-hidden="true" size={16} />
          {action === 'cancelling' ? '取消中…' : '拒绝'}
        </button>
      </div>
    </div>
  )
}
