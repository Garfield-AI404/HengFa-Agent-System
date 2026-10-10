/**
 * 面板块（.panel 容器 + 标题行 + 内容区；复用全局 .panel 系列样式）。
 *
 * 原为 DeveloperPage.tsx 的模块私有组件，可观测性第 1 步把它原样外移并导出，
 * 供 DeveloperPage 与 StreamTimelinePanel 共用——**className 与 DOM 结构逐字符不变**
 * （DeveloperPage.test.tsx 的 className 断言不受影响）。
 */
import type { ReactNode } from 'react'

export function PanelBlock({
  title,
  adornment,
  children,
}: {
  title: string
  adornment?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="panel">
      <div className="panel__header">
        <h2 className="panel__title">{title}</h2>
        {adornment}
      </div>
      <div className="panel__body">{children}</div>
    </section>
  )
}
