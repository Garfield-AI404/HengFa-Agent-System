/**
 * 开发者面板页（规格 2.7.2 /developer；10.5：developer 只允许健康检查、
 * 部署与脱敏技术日志接口——不含任何业务查询/审计入口，L81/L31/L106）。
 *
 * - 一期已知限制（顶部静态文本；14.5-6，口径与部署运维手册
 *   docs/runbooks/DEPLOYMENT.md §10 一致）；
 * - 系统健康：GET /api/v1/developer/health（依赖状态，无业务数据、无凭据，12.4）；
 * - 事件流落库：复用同一 health 查询的 `stream_events` 字段（只读展示 sse
 *   事件落库 sink 的计数，含丢弃/写失败/未映射；字段缺失时按未启用处理）；
 * - 任务时间线（可观测性第 1 步）：读取已落库 SSE 事件的结构量，见
 *   components/StreamTimeline.tsx；纯读取，不新增落库字段、不扩大数据暴露面；
 * - 日志环：GET /api/v1/developer/logs（最新在前；10.6 长度限制 + 11.2 规则 4
 *   敏感字段；11.4 不保存完整对话/模型思考）；
 * - 外部字符串（日志事件、payload）由 React 默认 HTML 转义 + 截断展示（10.6 L1193）；
 * - 加载失败经 ErrorNotice 页内提示；各区块刷新按钮重新拉取。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { ApiError } from '../api/client'
import {
  DEV_LOG_LEVELS,
  getDeveloperHealthApi,
  getDeveloperLogsApi,
} from '../api/developer.api'
import type { DevLogLevel } from '../api/developer.api'
import { ErrorNotice, EmptyState } from '../components/ErrorNotice'
import { PanelBlock } from '../components/PanelBlock'
import { StreamTimelinePanel } from '../components/StreamTimeline'
import { formatDateTime, truncateExternal } from '../utils/format'

const DEPENDENCY_LABEL: Record<string, string> = {
  erp_mcp: 'ERP MCP',
  agent_database: 'Agent 数据库',
  checkpoint_database: 'Checkpoint 数据库',
  session: '会话密钥',
}

const DEPENDENCY_STATUS_TEXT: Record<string, string> = {
  configured: '已配置',
  missing_credentials: '缺少凭据',
  not_configured: '未配置',
  invalid: '配置无效',
}

const LOG_LEVEL_TEXT: Record<string, string> = {
  DEBUG: '调试',
  INFO: '信息',
  WARNING: '警告',
  ERROR: '错误',
  CRITICAL: '严重',
}

/**
 * 事件流降级原因的中文映射（后端 StreamEventSinkStatus.degraded_reason）。
 * 固定映射表，**绝不把后端字符串直接插进 JSX**——未知值回退为通用文案，
 * 与 DEPENDENCY_STATUS_TEXT 同款处理。
 */
const STREAM_DEGRADED_TEXT: Record<string, string> = {
  database_not_configured: '数据库未配置',
  database_unavailable: '数据库不可用',
}

/**
 * 写失败异常类名的中文映射（后端 last_error_kind，只存 `type(exc).__name__`）。
 *
 * **白名单**：只映射此处已知的类名，值全部是本文件里的静态字面量——
 * 绝不用后端返回的字符串本身。立这条边界是为了不让后端字符串成为
 * 一条无审查的渲染/注入面；认不出的类名一律回退 FALLBACK（见 lastErrorKindText）。
 */
const LAST_ERROR_KIND_TEXT: Record<string, string> = {
  IntegrityError: 'IntegrityError（完整性约束冲突，常见于投影与表结构不符/未迁移）',
  OperationalError: 'OperationalError（数据库连接或操作故障）',
  InterfaceError: 'InterfaceError（数据库接口故障）',
  DataError: 'DataError（数据取值异常）',
  ProgrammingError: 'ProgrammingError（SQL 或投影与表结构不符）',
  DatabaseError: 'DatabaseError（数据库一般错误）',
}

/** 未在白名单内的异常类名 → 中性文案，绝不回显原字符串。 */
const LAST_ERROR_KIND_FALLBACK = '其它错误（详情见服务日志）'

function lastErrorKindText(kind: string): string {
  return LAST_ERROR_KIND_TEXT[kind] ?? LAST_ERROR_KIND_FALLBACK
}

function dependencyName(name: string): string {
  return DEPENDENCY_LABEL[name] ?? name
}

export function DeveloperPage() {
  const [logLevel, setLogLevel] = useState<DevLogLevel | null>(null)
  const [expandedLog, setExpandedLog] = useState<number | null>(null)

  // 系统健康（60s 自动轮询 + 手动刷新）
  const health = useQuery({
    queryKey: ['developer', 'health'],
    queryFn: () => getDeveloperHealthApi(),
    refetchInterval: 60_000,
  })
  // 日志环（最新在前；limit 50 与后端默认一致）
  const logs = useQuery({
    queryKey: ['developer', 'logs', logLevel],
    queryFn: () => getDeveloperLogsApi({ limit: 50, level: logLevel }),
  })
  const healthError = health.error instanceof ApiError ? health.error : null
  const logsError = logs.error instanceof ApiError ? logs.error : null
  // 事件流落库状态：老后端不返回该字段时为 undefined → 统一收敛为 null。
  // 部署顺序倒置（新前端 + 老后端）时缺此兜底会整页白屏。
  const streamEvents = health.data?.stream_events ?? null
  // 超限丢弃计数（后端 P1-4 起返回）：老后端 undefined → 兜底为 0。
  // 不兜底会渲染成 undefined，并让 undefined > 0（false）之外的 NaN 隐患进入标红判断。
  const oversizeDropped = streamEvents?.oversize_dropped ?? 0
  // 最近写失败异常类名（后端本批贯通）：老后端 undefined、后端无失败时 null → 统一收敛为 null。
  // 空串也按无值处理（不显示该行）。值只经白名单映射渲染，见 LAST_ERROR_KIND_TEXT。
  const lastErrorKind = streamEvents?.last_error_kind ?? null

  return (
    <div className="page">
      <h1 className="page__title">开发者面板</h1>
      <p className="panel__muted">
        developer 账号仅提供健康检查与脱敏技术日志，不含任何业务查询或审计入口（10.5）。
      </p>

      <PanelBlock title="一期已知限制">
        <p className="panel__muted">
          以下限制已在界面标明（14.5-6），口径与部署运维手册
          docs/runbooks/DEPLOYMENT.md §10 一致：
        </p>
        <ul className="known-limits__list">
          <li>共享 manager / developer 凭据（12.1 无用户表，凭据由运维发放）</li>
          <li>考勤创建非原子（R1 逐条写入，部分成功按恢复语义处理）</li>
          <li>ERP 无取消状态（R2 订单创建 / R5 分析边界，取消类请求不支持）</li>
        </ul>
      </PanelBlock>

      <PanelBlock
        title="系统健康"
        adornment={
          <button
            type="button"
            className="icon-button"
            onClick={() => void health.refetch()}
            aria-label="刷新系统健康"
            title="刷新"
          >
            <RefreshCw aria-hidden="true" size={14} />
          </button>
        }
      >
        <ErrorNotice error={healthError} />
        {health.isLoading ? <p className="panel__muted">加载中…</p> : null}
        {health.data ? (
          <>
            <p className="kv-note">
              状态：{health.data.status} · 应用：{health.data.app} · 版本：
              {health.data.version} · 日志级别：{health.data.log_level}
            </p>
            <div className="table-scroll">
              <table className="data-table data-table--compact">
                <thead>
                  <tr>
                    <th scope="col">依赖</th>
                    <th scope="col">状态</th>
                    <th scope="col">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {health.data.dependencies.map((dependency) => (
                    <tr key={dependency.name}>
                      <th scope="row">{dependencyName(dependency.name)}</th>
                      <td>
                        <span
                          className={`dependency-status dependency-status--${dependency.status}`}
                        >
                          <span className="dependency-status__dot" aria-hidden="true" />
                          {DEPENDENCY_STATUS_TEXT[dependency.status] ?? dependency.status}
                        </span>
                      </td>
                      <td className="data-table__wrap">{dependency.detail ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </PanelBlock>

      <PanelBlock title="事件流落库">
        {health.data ? (
          streamEvents == null ? (
            <p className="panel__muted">未启用（数据库未配置或未接线）。</p>
          ) : (
            <>
              <p
                className={
                  streamEvents.dropped > 0 ||
                  streamEvents.write_failures > 0 ||
                  streamEvents.unmapped_events > 0 ||
                  // 超限丢弃同样属「有数据没落库」，与上面三项同等纳入标红。
                  oversizeDropped > 0
                    ? // 计数非零时标红加粗：用 .kv-note 自己的元素级修饰符。
                      // 别改成 .dependency-status--invalid——那是 .dependency-status 块的
                      // BEM 修饰符（颜色碰巧一致，但不带 font-weight:600），挂到 .kv-note 属块错配。
                      'kv-note kv-note--error'
                    : 'kv-note'
                }
              >
                已写入 {streamEvents.written} · 队列积压 {streamEvents.queued} · 丢弃{' '}
                {streamEvents.dropped} · 写失败 {streamEvents.write_failures} · 未映射事件{' '}
                {streamEvents.unmapped_events} · 超限丢弃 {oversizeDropped} · 已清理{' '}
                {streamEvents.pruned_rows} 行
              </p>
              {streamEvents.degraded_reason ? (
                <p className="panel__muted">
                  降级原因：
                  {STREAM_DEGRADED_TEXT[streamEvents.degraded_reason] ?? '未知'}
                </p>
              ) : null}
              {lastErrorKind ? (
                <p className="panel__muted">最近写失败异常：{lastErrorKindText(lastErrorKind)}</p>
              ) : null}
            </>
          )
        ) : null}
      </PanelBlock>

      <StreamTimelinePanel />

      <PanelBlock
        title="日志环"
        adornment={
          <button
            type="button"
            className="icon-button"
            onClick={() => void logs.refetch()}
            aria-label="刷新技术日志"
            title="刷新"
          >
            <RefreshCw aria-hidden="true" size={14} />
          </button>
        }
      >
        <p className="panel__muted">
          日志经服务端脱敏输出（敏感键与凭据形态已替换，10.6/11.2 规则 4）；
          不保存完整对话或模型思考（11.4）。外部字符串在网页输出截断展示（10.6 L1193）。
        </p>
        <div className="log-filter" role="group" aria-label="日志级别过滤">
          <button
            type="button"
            className={`btn ${logLevel === null ? 'btn--primary' : ''}`}
            onClick={() => setLogLevel(null)}
          >
            全部
          </button>
          {DEV_LOG_LEVELS.map((level) => (
            <button
              key={level}
              type="button"
              className={`btn ${logLevel === level ? 'btn--primary' : ''}`}
              onClick={() => setLogLevel(logLevel === level ? null : level)}
            >
              {LOG_LEVEL_TEXT[level] ?? level}
            </button>
          ))}
        </div>
        <ErrorNotice error={logsError} />
        {logs.isLoading ? <p className="panel__muted">加载中…</p> : null}
        {logs.data && logs.data.items.length === 0 ? (
          <EmptyState>暂无技术日志。</EmptyState>
        ) : null}
        {logs.data && logs.data.items.length > 0 ? (
          <ol className="log-list">
            {logs.data.items.map((entry, index) => {
              const hasPayload =
                entry.payload !== undefined && Object.keys(entry.payload).length > 0
              const open = expandedLog === index
              return (
                <li key={index} className="log-entry">
                  <p className="log-entry__head">
                    <span className="log-entry__time">{formatDateTime(entry.timestamp)}</span>
                    <span
                      className={`log-entry__level log-entry__level--${(entry.level ?? '').toLowerCase()}`}
                    >
                      {entry.level ?? '—'}
                    </span>
                    <span className="log-entry__event">
                      {truncateExternal(entry.event ?? '', 300)}
                    </span>
                    {entry.logger ? (
                      <span className="log-entry__logger">{entry.logger}</span>
                    ) : null}
                    {hasPayload ? (
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={open ? '收起日志明细' : '展开日志明细'}
                        aria-expanded={open}
                        onClick={() => setExpandedLog(open ? null : index)}
                      >
                        {open ? '−' : '+'}
                      </button>
                    ) : null}
                  </p>
                  {open && hasPayload ? (
                    <pre className="log-entry__payload">
                      {truncateExternal(JSON.stringify(entry.payload, null, 2), 2000)}
                    </pre>
                  ) : null}
                </li>
              )
            })}
          </ol>
        ) : null}
        {logs.data && logs.data.total > logs.data.items.length ? (
          <p className="panel__muted">
            共 {logs.data.total} 条，仅展示最新 {logs.data.items.length} 条。
          </p>
        ) : null}
      </PanelBlock>
    </div>
  )
}
