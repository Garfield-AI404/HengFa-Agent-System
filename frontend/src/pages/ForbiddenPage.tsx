/**
 * 403 拒绝页（规格 2.7.2）。
 *
 * 前端守卫仅是 UX（隐藏导航不等于权限控制）；后端每个 API 仍独立鉴权。
 *
 * “返回工作台”按角色跳转（D28：developer → /developer，其余 → /chat），
 * 避免 developer 被拦到 /forbidden 后跳 /chat 又被 RequireRole 拦回的死循环。
 */
import { Link, useNavigate } from 'react-router'
import { useAuth } from '../auth/AuthProvider'

export function ForbiddenPage() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const home = user?.role === 'developer' ? '/developer' : '/chat'

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <main className="forbidden-page">
      <h1 className="forbidden-page__title">无权限访问（403）</h1>
      <p className="forbidden-page__desc">
        {user
          ? `当前账号 ${user.account}（${user.role}）无权访问该页面。`
          : '当前未登录。'}
        前端守卫仅为界面提示，后端每个 API 仍会独立校验权限。
      </p>
      <nav className="forbidden-page__actions" aria-label="可用操作">
        {user ? (
          <>
            <Link to={home} className="btn">
              返回工作台
            </Link>
            <button type="button" className="btn" onClick={() => void handleLogout()}>
              退出登录
            </button>
          </>
        ) : (
          <Link to="/login" className="btn">
            去登录
          </Link>
        )}
      </nav>
    </main>
  )
}
