/**
 * 分页控件（QueryPage items/total/limit/offset 语义，D17）。
 * 原生按钮：键盘可达、禁用态明确。
 */
interface PaginationProps {
  total: number
  limit: number
  offset: number
  onChange: (offset: number) => void
}

export function Pagination({ total, limit, offset, onChange }: PaginationProps) {
  if (total <= 0) return null
  const start = offset + 1
  const end = Math.min(offset + limit, total)
  const hasPrev = offset > 0
  const hasNext = end < total

  return (
    <nav className="pagination" aria-label="分页">
      <span className="pagination__info">
        第 {start}–{end} 条，共 {total} 条
      </span>
      <div className="pagination__actions">
        <button
          type="button"
          className="btn"
          disabled={!hasPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          上一页
        </button>
        <button
          type="button"
          className="btn"
          disabled={!hasNext}
          onClick={() => onChange(offset + limit)}
        >
          下一页
        </button>
      </div>
    </nav>
  )
}
