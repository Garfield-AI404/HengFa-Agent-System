/**
 * 登录布局壳：居中面板承载登录页（无业务逻辑）。
 * 品牌行：圆形 logo 占位 + h1（UI-restyle-design.md 4.1；h1 文本不可变）。
 */
import { Outlet } from 'react-router'

export function AuthLayout() {
  return (
    <div className="auth-layout">
      <div className="auth-layout__panel">
        <div className="auth-layout__brand-row">
          <span className="auth-layout__brand-logo" aria-hidden="true">
            恒
          </span>
          <h1 className="auth-layout__brand">恒发 Agent</h1>
        </div>
        <Outlet />
      </div>
    </div>
  )
}
