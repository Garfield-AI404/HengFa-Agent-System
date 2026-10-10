/**
 * manager / developer 工作台布局壳（规格 2.7.2 / 2.7.5；UI-restyle-design.md 4.2.1）。
 * D28：布局不再整体包角色守卫，由页面级 RequireRole 兜底；业务导航按角色
 * 过滤（manager → 对话；developer → 开发者面板），developer 无对话区
 * （不渲染新建对话/历史会话，避免误入 /chat 与 403）。
 *
 * - 桌面端：264px 侧栏（品牌行 + 新建对话 + 业务导航 + 历史会话 + 底部用户区）
 *   + 主工作区；顶栏桌面隐藏（CSS 控制）；窄屏：顶部栏（菜单按钮 + 品牌）
 *   + 抽屉导航。
 * - 新建对话经 navigate('/chat', { state: { newChat: Date.now() } }) 触发清空；
 *   历史会话（2026-08-24）经 navigate('/chat', { state: { openThread } }) 打开
 *   指定会话、两段确认后 DELETE 删除（归属/活动保护在后端）。
 * - 导航为 UX 便利；后端每个 API 仍独立鉴权（2.7.2）。
 * - drawer 开关是 React 局部状态（弹窗/展开项，2.7.4）。
 */
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'
import {
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Trash,
  Wrench,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useAuth } from '../../auth/AuthProvider'
import {
  CHAT_THREADS_CHANGED_EVENT,
  THREADS_CHANGED_CHANNEL,
  THREAD_STORAGE_KEY,
  deleteThreadApi,
  listThreadsApi,
  notifyThreadsChanged,
} from '../../api/chat.api'
import type { ThreadInfo } from '../../api/chat.api'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

/** 角色显示名映射（左下角用户区展示用；账号名 account 保持后端原值）。 */
const ROLE_DISPLAY_LABELS: Record<string, string> = {
  manager: '经理',
}

/** 侧栏收起态 localStorage key（风格：hengfa.<域>.<名>，与现有 key 不冲突）。 */
const SIDEBAR_COLLAPSED_KEY = 'hengfa.ui.sidebarCollapsed'

/** 业务导航（D28 按角色过滤：developer → 开发者面板；manager 无业务导航）。 */
function navItemsFor(role: string | undefined): NavItem[] {
  return role === 'developer'
    ? [{ to: '/developer', label: '开发者面板', icon: Wrench }]
    : []
}

/** 相对时间（updated_at → 「刚刚 / N 分钟前 / N 小时前 / N 天前 / M-D」；
 *  展示用简单格式化，不参与业务判断）。 */
function relativeTime(value: string): string {
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return '—'
  const minutes = Math.floor((Date.now() - time.getTime()) / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days} 天前`
  return `${time.getMonth() + 1}-${time.getDate()}`
}

interface ThreadsListProps {
  /** 会话列表（null = 加载中）。 */
  threads: ThreadInfo[] | null
  /** 列表加载失败提示（重试入口）。 */
  error: string | null
  onRetry: () => void
  onOpen: (threadId: string) => void
  /** 删除会话（后端完成归属校验与活动保护；失败抛出由本组件展示）。 */
  onDelete: (threadId: string) => Promise<void>
  /** 移动端抽屉：点击会话项后关闭抽屉。 */
  onNavigate?: () => void
}

/** 历史会话区块（桌面侧栏与移动抽屉共用同一数据与删除交互）。 */
function ThreadsList({ threads, error, onRetry, onOpen, onDelete, onNavigate }: ThreadsListProps) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null) // 两段确认中的会话
  const [deleteError, setDeleteError] = useState<string | null>(null)

  if (threads === null && !error) return null // 加载中：不渲染区块（避免闪跳）

  return (
    <div className="app-layout__threads">
      <p className="app-layout__threads-title">历史会话</p>
      {error ? (
        <p className="app-layout__threads-error">
          {error}
          <button type="button" className="app-layout__threads-retry" onClick={onRetry}>
            重新加载
          </button>
        </p>
      ) : null}
      {threads !== null && threads.length === 0 ? (
        <p className="app-layout__threads-empty">暂无历史会话</p>
      ) : null}
      {threads !== null && threads.length > 0 ? (
        <ul className="app-layout__threads-list">
          {threads.map((thread) => {
            const confirming = confirmingId === thread.thread_id
            return (
              <li key={thread.thread_id} className="app-layout__thread-item">
                <button
                  type="button"
                  className="app-layout__thread-open"
                  title={thread.title}
                  onClick={() => {
                    setConfirmingId(null)
                    setDeleteError(null)
                    onOpen(thread.thread_id)
                    onNavigate?.()
                  }}
                >
                  <span className="app-layout__thread-title">{thread.title}</span>
                  <span className="app-layout__thread-time">
                    {relativeTime(thread.updated_at)}
                  </span>
                </button>
                {confirming ? (
                  // 两段确认（无弹窗）：先变「确认删除？」，再点确认才调删除
                  <span className="app-layout__thread-confirm" role="group" aria-label="确认删除会话">
                    <span className="app-layout__thread-confirm-text">确认删除？</span>
                    <button
                      type="button"
                      className="app-layout__thread-confirm-yes"
                      onClick={() => {
                        void onDelete(thread.thread_id)
                          .then(() => {
                            setConfirmingId(null)
                            setDeleteError(null)
                          })
                          .catch(() => {
                            setConfirmingId(null)
                            setDeleteError('删除失败，请稍后重试。')
                          })
                      }}
                    >
                      删除
                    </button>
                    <button
                      type="button"
                      className="app-layout__thread-confirm-no"
                      onClick={() => setConfirmingId(null)}
                    >
                      取消
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="app-layout__thread-delete"
                    aria-label={`删除会话：${thread.title}`}
                    onClick={() => setConfirmingId(thread.thread_id)}
                  >
                    <Trash aria-hidden="true" size={14} />
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      ) : null}
      {deleteError ? <p className="app-layout__threads-error">{deleteError}</p> : null}
    </div>
  )
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth()
  const items = navItemsFor(user?.role)
  if (items.length === 0) return null
  return (
    <nav aria-label="业务导航">
      <ul className="app-layout__nav">
        {items.map(({ to, label, icon: Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              onClick={onNavigate}
              className={({ isActive }) =>
                isActive
                  ? 'app-layout__nav-link app-layout__nav-link--active'
                  : 'app-layout__nav-link'
              }
            >
              <Icon aria-hidden="true" size={18} />
              <span>{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}

/** 底部用户区（桌面侧栏底部 / 移动端抽屉底部；退出登录文字与 aria 保留）。 */
function UserArea({
  account,
  role,
  onLogout,
}: {
  account: string
  role: string
  onLogout: () => void
}) {
  return (
    <div className="app-layout__user">
      <span className="app-layout__user-avatar" aria-hidden="true">
        {account ? account.charAt(0).toUpperCase() : 'U'}
      </span>
      <div className="app-layout__user-info">
        <span className="app-layout__user-account">
          {account}（{ROLE_DISPLAY_LABELS[role] ?? role}）
        </span>
        <button type="button" className="app-layout__logout" onClick={onLogout}>
          <LogOut aria-hidden="true" size={16} />
          退出登录
        </button>
      </div>
    </div>
  )
}

export function ManagerLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [drawerOpen, setDrawerOpen] = useState(false)
  // 侧栏收起态（桌面端）：localStorage 持久化，隐私模式等异常场景降级为本次会话有效
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
    } catch {
      return false
    }
  })

  const toggleSidebar = (): void => {
    setSidebarCollapsed((prev) => {
      const next = !prev
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? '1' : '0')
      } catch {
        /* 隐私模式等场景：仅本次会话生效 */
      }
      return next
    })
  }
  // 历史会话数据在布局层持有（桌面侧栏与移动抽屉共用同一份列表）
  const [threads, setThreads] = useState<ThreadInfo[] | null>(null)
  const [threadsError, setThreadsError] = useState<string | null>(null)

  const loadThreads = (): void => {
    if (user?.role !== 'manager') return // developer 无历史会话区，不拉取（避免 403）
    listThreadsApi()
      .then((data) => {
        setThreads(data.threads)
        setThreadsError(null)
      })
      .catch(() => {
        setThreadsError('历史会话加载失败')
      })
  }

  // 挂载时加载（登录态下；布局只对已登录用户渲染）。依赖 user?.role：
  // 整页加载时布局先于鉴权挂载（user 为 null），鉴权完成后 effect 重跑，
  // 历史会话列表才会请求（修复刷新后列表永不加载的竞态，2026-08-28）
  useEffect(() => {
    loadThreads()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.role])

  // 会话数据变化（发消息成功）→ 重新拉取历史列表，保证新会话/活跃时间即时可见
  useEffect(() => {
    const handler = () => loadThreads()
    window.addEventListener(CHAT_THREADS_CHANGED_EVENT, handler)
    return () => window.removeEventListener(CHAT_THREADS_CHANGED_EVENT, handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // [2026-08-28 跨页同步] BroadcastChannel 监听：其他标签页/窗口发消息/删除
  // 会话后广播到此 → 本页历史会话列表实时刷新（BroadcastChannel 不向发送方
  // 自身投递，与本页 window 事件监听互补，无重复刷新）
  useEffect(() => {
    const channel = new BroadcastChannel(THREADS_CHANGED_CHANNEL)
    channel.onmessage = () => loadThreads()
    return () => channel.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // [2026-08-28 跨页同步兜底] 页面从后台切回前台时刷新历史列表：覆盖广播
  // 丢失（页面休眠期间被回收/网络中断）与休眠期间其他页面的变更
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') loadThreads()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleOpenThread = (threadId: string): void => {
    localStorage.setItem(THREAD_STORAGE_KEY, threadId)
    // 已在 /chat 时 useNavigate 会触发 location.state 变化——ChatPage 的
    // openThread effect 处理切换；不在 chat 页则跳转后由挂载路径接管
    navigate('/chat', { state: { openThread: threadId } })
  }

  /** 删除会话：成功从列表移除；删除的正是当前会话 → 清 localStorage 并
   *  走新建对话重置（即使用户不在 chat 页也无害——ChatPage 只在新建
   *  标记变化时响应）。 */
  const handleDeleteThread = async (threadId: string): Promise<void> => {
    await deleteThreadApi(threadId) // 失败（归属 404/活动 409/500）抛出
    setThreads((prev) => (prev ?? []).filter((t) => t.thread_id !== threadId))
    // [2026-08-28 跨页同步] 删除也走统一通知：其他标签页/窗口同步移除该会话
    notifyThreadsChanged()
    if (localStorage.getItem(THREAD_STORAGE_KEY) === threadId) {
      localStorage.removeItem(THREAD_STORAGE_KEY)
      navigate('/chat', { state: { newChat: Date.now() } })
    }
  }

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-layout">
      <header className="app-layout__topbar">
        <button
          type="button"
          className="app-layout__menu-toggle"
          aria-label="打开导航菜单"
          onClick={() => setDrawerOpen(true)}
        >
          <Menu aria-hidden="true" />
        </button>
        <span className="app-layout__brand">恒发 Agent</span>
      </header>

      <div className="app-layout__body">
        <aside
          className={
            sidebarCollapsed
              ? 'app-layout__sidebar app-layout__sidebar--collapsed'
              : 'app-layout__sidebar'
          }
        >
          <div className="app-layout__brand-row">
            <span className="app-layout__brand-logo" aria-hidden="true">
              恒
            </span>
            <span className="app-layout__brand">恒发 Agent</span>
            <button
              type="button"
              className="app-layout__sidebar-toggle"
              aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
              onClick={toggleSidebar}
            >
              {sidebarCollapsed ? (
                <PanelLeftOpen aria-hidden="true" size={16} />
              ) : (
                <PanelLeftClose aria-hidden="true" size={16} />
              )}
            </button>
          </div>
          {user?.role === 'manager' ? (
            <>
              <button
                type="button"
                className="app-layout__new-chat"
                onClick={() => navigate('/chat', { state: { newChat: Date.now() } })}
              >
                <Plus aria-hidden="true" size={16} />
                新建对话
              </button>
            </>
          ) : null}
          <NavList />
          {user?.role === 'manager' ? (
            <ThreadsList
              threads={threads}
              error={threadsError}
              onRetry={loadThreads}
              onOpen={handleOpenThread}
              onDelete={handleDeleteThread}
            />
          ) : null}
          <div className="app-layout__sidebar-divider" />
          <UserArea
            account={user?.account ?? ''}
            role={user?.role ?? ''}
            onLogout={() => void handleLogout()}
          />
        </aside>
        <main className="app-layout__main">
          <Outlet />
        </main>
      </div>

      {drawerOpen && (
        <div className="app-layout__drawer-backdrop" onClick={() => setDrawerOpen(false)}>
          <div
            className="app-layout__drawer"
            role="dialog"
            aria-modal="true"
            aria-label="导航菜单"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="app-layout__drawer-header">
              <span className="app-layout__brand">恒发 Agent</span>
              <button
                type="button"
                className="app-layout__menu-close"
                aria-label="关闭导航菜单"
                onClick={() => setDrawerOpen(false)}
              >
                <X aria-hidden="true" />
              </button>
            </div>
            <NavList onNavigate={() => setDrawerOpen(false)} />
            {user?.role === 'manager' ? (
              <ThreadsList
                threads={threads}
                error={threadsError}
                onRetry={loadThreads}
                onOpen={handleOpenThread}
                onDelete={handleDeleteThread}
                onNavigate={() => setDrawerOpen(false)}
              />
            ) : null}
            <div className="app-layout__sidebar-divider" />
            <UserArea
              account={user?.account ?? ''}
              role={user?.role ?? ''}
              onLogout={() => void handleLogout()}
            />
          </div>
        </div>
      )}
    </div>
  )
}
