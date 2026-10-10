/**
 * 登录页（规格 2.7.2 / 2.7.3 / 12.5）。
 *
 * - 表单错误按 error_code 分支展示（2.7.3：展示 message，分支只读 error_code）。
 * - 提交期间禁用按钮，防重复提交（2.7.4）。
 * - 不再展示共享账号说明（12.1 无用户表，共享账号凭据由运维发放）。
 * - 支持"记住账号"（localStorage 仅保存账号名，绝不保存密码；登录成功且勾选
 *   时写入，未勾选清除；损坏/无存档回退空值，并顺带清除旧版本遗留的密码存档）。
 */
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { ApiError, toApiError } from '../api/client'
import { CLIENT_ERROR_CODES } from '../auth/auth.types'
import type { Role } from '../auth/auth.types'
import { useAuth } from '../auth/AuthProvider'

/** error_code → 面向用户的提示；未知错误码回退展示后端 message。 */
const HINT_BY_ERROR_CODE: Record<string, string> = {
  INVALID_CREDENTIALS: '账号或密码不正确。',
  SESSION_EXPIRED: '会话已过期，请重新登录。',
  [CLIENT_ERROR_CODES.NETWORK_ERROR]: '无法连接服务器，请检查网络后重试。',
  // RV-06 决策项 a / RV-38 非阻塞 3（D26 落地）：网关 limit_req 标准 429
  // （nginx 非 JSON 体 → client.ts 映射 RATE_LIMITED）：限速提示 + 等待引导
  RATE_LIMITED: '登录尝试过于频繁，已触发限速保护。请等待 30 秒以上再重试；持续超限请联系运维。',
}

/** localStorage key（与 ChatPage 的 THREAD_STORAGE_KEY 同风格：hengfa.<域>.<名>）。 */
const REMEMBER_KEY = 'hengfa.login.remember'

/**
 * 读取记住的账号名。仅存账号、不存密码（密码交给浏览器密码管理器）。
 * 兼容清理：旧版本曾以 base64 混淆存过 {account, password}，此处一律视为
 * 无效存档并返回 null，由登录成功路径 removeItem 覆写清理。
 */
function readRememberedAccount(): string | null {
  try {
    const raw = localStorage.getItem(REMEMBER_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { account?: unknown; password?: unknown }
    // 旧格式含 password 字段 → 拒绝回填，避免继续沿用明文密码存档
    if (typeof parsed.account !== 'string' || 'password' in parsed) return null
    return parsed.account
  } catch {
    return null
  }
}

/**
 * 登录后的默认首页：按角色跳转——manager → /chat，developer → /developer
 * （D28：developer 专属面板页，不再被 RequireRole 拦到 /forbidden）。
 * 有守卫来源路径时优先回跳（仍受 RequireRole 角色守卫约束）。
 */
function resolveHome(fromPath: string | undefined, role: Role | undefined): string {
  if (fromPath) return fromPath
  return role === 'developer' ? '/developer' : '/chat'
}

export function LoginPage() {
  const { user, status, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from

  // 记住账号：初始值来自 localStorage 存档（有存档 → 回填账号 + 默认勾选，密码留空）
  const [remembered] = useState(readRememberedAccount)
  const [account, setAccount] = useState(remembered ?? '')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(remembered !== null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  // 已登录（含刷新后会话仍有效）→ 直接进入工作台
  useEffect(() => {
    if (status !== 'authenticated' || !user) return
    navigate(resolveHome(from, user.role), { replace: true })
  }, [status, user, from, navigate])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submitting) return // 2.7.4 防重复提交
    if (!account.trim() || !password) return // 必填校验（HTML5 校验之外的代码兜底）

    setSubmitting(true)
    setError(null)
    try {
      const currentUser = await login(account.trim(), password)
      // 仅登录成功才写存档（失败不写）：勾选 → 保存账号名（不含密码），未勾选 → 清除。
      // 两种分支都会覆写/清掉旧版本可能遗留的 base64 密码存档。
      if (remember) {
        try {
          localStorage.setItem(REMEMBER_KEY, JSON.stringify({ account: account.trim() }))
        } catch {
          // 配额满 / 隐私模式 → 存档失败不影响登录流程
        }
      } else {
        try {
          localStorage.removeItem(REMEMBER_KEY)
        } catch {
          // 同上，忽略
        }
      }
      navigate(resolveHome(from, currentUser.role), { replace: true })
    } catch (err) {
      // [2026-09-11 上传链路收口] 统一归一：TypeError（fetch 网络不可达）仍归
      // NETWORK_ERROR（HINT_BY_ERROR_CODE 显示「无法连接服务器」），不再把所有
      // 非 ApiError 一律当网络错误。
      setError(toApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const hint = error ? (HINT_BY_ERROR_CODE[error.errorCode] ?? error.message) : null

  return (
    <main className="login-page">
      <p className="login-page__intro">登录后进入工作台。</p>
      <form className="login-form" onSubmit={handleSubmit} noValidate>
        <div className="login-form__field">
          <label htmlFor="login-account">账号</label>
          <input
            id="login-account"
            name="account"
            type="text"
            autoComplete="username"
            value={account}
            onChange={(event) => setAccount(event.target.value)}
            required
            aria-required="true"
            aria-invalid={error?.errorCode === 'INVALID_CREDENTIALS'}
          />
        </div>
        <div className="login-form__field">
          <label htmlFor="login-password">密码</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            aria-required="true"
            aria-invalid={error?.errorCode === 'INVALID_CREDENTIALS'}
          />
        </div>
        <label className="login-form__remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
          />
          记住账号
        </label>
        {error && (
          <p className="login-form__error" role="alert">
            {hint}
            {error.traceId ? (
              <span className="login-form__trace">（trace_id: {error.traceId}）</span>
            ) : null}
          </p>
        )}
        <button type="submit" className="login-form__submit" disabled={submitting}>
          {submitting ? '登录中…' : '登录'}
        </button>
      </form>
    </main>
  )
}
