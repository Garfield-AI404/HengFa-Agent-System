/**
 * ConfirmationPanel 渲染测试（2026-08-30 新建；2026-09-10 确认卡改版同步）。
 *
 * ## 改版钉住的红线
 * - 不渲染 operation_type；target 为空串（钣金/塑粉创建卡）不渲染「目标」行；
 * - fields_to_write 全网格边框（CSS 规则的 .confirmation-panel 作用域由 DOM 层断言保障）；
 * - 考勤卡（值 = 含「操作」键的 dict）改紧凑竖表、去掉嵌套子表；
 * - 「操作 = 保持」的无变化员工默认折叠（<details>），非保持行不折叠、N 计数正确；
 * - 拒绝按钮文案「拒绝」（aria-label/title 对齐）。
 *
 * 另保留原红线：任何形态的卡都不出现 `[object Object]`（对象值不静默变占位符）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ConfirmationPanel } from '../../src/components/ConfirmationPanel'
import type { ConfirmationCard, PendingConfirmation } from '../../src/api/types'

const card = (over: Partial<ConfirmationCard> = {}): ConfirmationCard => ({
  operation_type: '记录每日考勤',
  target: '2026-08-30 考勤计划',
  fields_to_write: {},
  auto_filled: [],
  expected_impact: '在 ERP 写入当天考勤记录',
  limitations: [],
  payload_hash: 'hash-1',
  ...over,
})

const pending = (over: Partial<ConfirmationCard> = {}): PendingConfirmation => ({
  task_id: 'T-1',
  tool_name: 'attendance_plan_execute',
  card: card(over),
  payload_hash: 'hash-1',
})

function renderPanel(
  confirmation: PendingConfirmation,
  onConfirm: (approved: boolean, payloadHash: string) => Promise<void> = async () => undefined,
) {
  const { container } = render(
    <ConfirmationPanel confirmation={confirmation} onConfirm={onConfirm} />,
  )
  return { container, onConfirm }
}

/** 考勤卡样例（与后端 localize_confirmation_fields 输出的中文键/值一致）。 */
const attendanceFields = {
  张三: { 操作: '更新', '请假（分钟）': 0, '加班（分钟）': 120 },
  李四: { 操作: '新建', '请假（分钟）': 60, '加班（分钟）': 0 },
}

/** 含 3 名「保持」（无变化）员工的考勤卡样例。 */
const attendanceWithUnchanged = {
  张三: { 操作: '更新', '请假（分钟）': 0, '加班（分钟）': 120 },
  赵六: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
  钱七: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
  孙八: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
}

/** 保持行被变化行「打断」的考勤卡样例（保持/更新/保持/保持）。 */
const attendanceInterleaved = {
  赵六: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
  张三: { 操作: '更新', '请假（分钟）': 0, '加班（分钟）': 120 },
  钱七: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
  孙八: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
}

/** 全部为「保持」的考勤卡样例（无变化行）。 */
const attendanceAllUnchanged = {
  赵六: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
  钱七: { 操作: '保持', '请假（分钟）': 0, '加班（分钟）': 0 },
}

/**
 * 考勤卡挂起确认：行头「员工」/ 量词「人」由后端显式传入（2026-09-12 起
 * attendance.py 会写这两个展示字段）。考勤零回归由此显式表达，不再依赖前端
 * 写死字面量的巧合——前端只做 `?? '明细'/'条'` 回退。
 */
const attendancePending = (fields: Record<string, unknown>): PendingConfirmation =>
  pending({ row_header_label: '员工', row_quantifier: '人', fields_to_write: fields })

/** 多明细订单卡：行头「明细」/ 量词「条」，值为 { 操作, 字段和值 } 嵌套 dict。 */
const multiDetailFields = {
  '1. 客户 铭泰 / M1': { 操作: '新建', 字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 } },
  '2. 客户 示例 / M2': { 操作: '新建', 字段和值: { 客户: '示例', 型号: 'M2', 数量: 5 } },
}

const multiDetailPending = (
  over: Partial<ConfirmationCard> = {},
): PendingConfirmation =>
  pending({ row_header_label: '明细', row_quantifier: '条', fields_to_write: multiDetailFields, ...over })

describe('ConfirmationPanel 字段值渲染', () => {
  it('考勤卡（嵌套对象）：不出现 [object Object]', () => {
    const { container } = renderPanel(attendancePending(attendanceFields))

    expect(container.textContent).not.toContain('[object Object]')
  })

  it('考勤卡：员工竖向 + 右侧三列，无嵌套子表', () => {
    const { container } = renderPanel(attendancePending(attendanceFields))

    // 表头：员工 + 操作/请假/加班（4 列）
    const headers = screen.getAllByRole('columnheader').map((el) => el.textContent)
    expect(headers).toEqual(['员工', '操作', '请假（分钟）', '加班（分钟）'])
    // 员工名是行头（竖向），无变化时 2 名员工 → 2 个行头（不再有内层子表的字段行头）
    expect(screen.getAllByRole('rowheader').map((el) => el.textContent)).toEqual(['张三', '李四'])
    // 值落在数据格里
    expect(screen.getByText('120')).toBeInTheDocument()
    expect(screen.getByText('60')).toBeInTheDocument()
    // 无嵌套子表（表头只有一个 thead）
    expect(container.querySelectorAll('table table')).toHaveLength(0)
  })

  it('无变化（操作=保持）员工：默认折叠成一行汇总，N 正确', () => {
    const { container } = renderPanel(attendancePending(attendanceWithUnchanged))

    expect(screen.getByText('没有变化的 3 人（点击展开）')).toBeInTheDocument()
    const details = container.querySelector('details')
    expect(details).not.toBeNull()
    expect(details?.open).toBe(false)
    // 保持员工只有 1 名「更新」员工是行头，其余被折叠
    expect(screen.getAllByRole('rowheader').map((el) => el.textContent)).toEqual(['张三'])
  })

  it('非「保持」行不被折叠（正常展开显示）', () => {
    renderPanel(attendancePending(attendanceWithUnchanged))

    // 张三（更新）正常出现在主表行头里
    expect(screen.getByRole('rowheader', { name: '张三' })).toBeInTheDocument()
    // 保持员工不出现在主表行头里（已被折叠）
    expect(screen.queryByRole('rowheader', { name: '赵六' })).toBeNull()
  })

  it('展开折叠后可看到全部「保持」员工（按原始顺序）', () => {
    const { container } = renderPanel(attendancePending(attendanceWithUnchanged))

    const summary = screen.getByText('没有变化的 3 人（点击展开）')
    fireEvent.click(summary)

    expect(container.querySelector('details')?.open).toBe(true)
    // 展开后按 fields_to_write 的原始顺序列出
    const names = Array.from(container.querySelectorAll('.confirmation-panel__fold-name')).map(
      (el) => el.textContent,
    )
    expect(names).toEqual(['赵六', '钱七', '孙八'])
  })

  it('保持行被变化行打断：仍只有一个汇总行，N 为全部保持行数', () => {
    const { container } = renderPanel(attendancePending(attendanceInterleaved))

    // 只有 1 个汇总行（不因打断而分裂成多个）
    expect(screen.getAllByText(/没有变化的 \d+ 人（点击展开）/)).toHaveLength(1)
    expect(screen.getByText('没有变化的 3 人（点击展开）')).toBeInTheDocument()
    // 主表行头只剩变化员工张三
    expect(screen.getAllByRole('rowheader').map((el) => el.textContent)).toEqual(['张三'])
    // 汇总行位于表格末尾（变化行之后）
    const rows = container.querySelectorAll('.confirmation-panel__plan tbody tr')
    expect(rows[rows.length - 1].querySelector('details')).not.toBeNull()
  })

  it('打断场景展开后：保持行按原始顺序列出', () => {
    const { container } = renderPanel(attendancePending(attendanceInterleaved))

    fireEvent.click(screen.getByText('没有变化的 3 人（点击展开）'))

    const names = Array.from(container.querySelectorAll('.confirmation-panel__fold-name')).map(
      (el) => el.textContent,
    )
    expect(names).toEqual(['赵六', '钱七', '孙八'])
  })

  it('全部为「保持」：汇总唯一且 N 等于全部员工数，主表无行头', () => {
    const { container } = renderPanel(attendancePending(attendanceAllUnchanged))

    expect(screen.getAllByText(/没有变化的 \d+ 人（点击展开）/)).toHaveLength(1)
    expect(screen.getByText('没有变化的 2 人（点击展开）')).toBeInTheDocument()
    expect(screen.queryAllByRole('rowheader')).toHaveLength(0)
    fireEvent.click(screen.getByText('没有变化的 2 人（点击展开）'))
    const names = Array.from(container.querySelectorAll('.confirmation-panel__fold-name')).map(
      (el) => el.textContent,
    )
    expect(names).toEqual(['赵六', '钱七'])
  })

  it('非 dict / 无「操作」键的值不误折叠（一律正常展开）', () => {
    renderPanel(
      pending({
        fields_to_write: {
          张三: { 操作: '更新', '请假（分钟）': 0, '加班（分钟）': 120 },
          备注: '原样文本',
          空对象: {},
          空值: null,
        },
      }),
    )

    expect(screen.getByRole('rowheader', { name: '备注' })).toBeInTheDocument()
    expect(screen.getByText('原样文本')).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '空对象' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '空值' })).toBeInTheDocument()
    // 没有任何折叠汇总行
    expect(screen.queryByText(/没有变化的/)).toBeNull()
  })

  it('嵌套值为 null：显示占位符 —，不显示 null 字符串', () => {
    renderPanel(pending({ fields_to_write: { 张三: { 操作: '更新', '加班（分钟）': null } } }))

    const row = screen.getByRole('rowheader', { name: '张三' }).closest('tr')
    expect(row?.textContent).toContain('—')
    expect(row?.textContent).not.toContain('null')
  })

  it('旧形状订单卡（扁平标量值）：兼容回归——值原样展示', () => {
    // 旧扁平形状（fields_to_write 直接是 字段→标量 映射）→ planMode=false →
    // 走「字段 | 新值」旧渲染分支。**这是旧形状卡（含上线瞬间在飞的确认卡）
    // 的兼容钉子，不要删**；单条订单/收支卡也按此形状产出。
    renderPanel(
      pending({
        operation_type: '创建钣金订单',
        target: '客户 A',
        fields_to_write: { 客户: '铭泰', 型号: 'M1', 数量: 12 },
      }),
    )

    expect(screen.getByRole('rowheader', { name: '客户' })).toBeInTheDocument()
    expect(screen.getByText('铭泰')).toBeInTheDocument()
    expect(screen.getByText('M1')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
  })

  it('空嵌套对象与空值：显示占位符 —', () => {
    renderPanel(pending({ fields_to_write: { 空条目: {}, 空值: null } }))

    expect(screen.getAllByText('—')).toHaveLength(2)
  })

  it('旧形状塑粉创建卡（扁平标量值）：目标为空串 + 首行供货商名（普通字段行）', () => {
    // 同「旧形状订单卡」：旧扁平形状兼容钉子，planMode=false 走旧分支。
    renderPanel(
      pending({
        operation_type: '创建塑粉订单',
        target: '',
        fields_to_write: { 供货商: '宏源粉末厂', 型号: 'P-01', 数量: 5 },
      }),
    )

    // 目标行不渲染
    expect(screen.queryByText('目标')).toBeNull()
    // 首行供货商名作为普通字段行正常展示
    expect(screen.getByRole('rowheader', { name: '供货商' })).toBeInTheDocument()
    expect(screen.getByText('宏源粉末厂')).toBeInTheDocument()
    expect(screen.getByText('P-01')).toBeInTheDocument()
  })
})

describe('ConfirmationPanel 卡片头与操作', () => {
  it('不渲染操作类型（operation_type 字段保留但不展示）', () => {
    const { container } = renderPanel(
      pending({ operation_type: '创建钣金订单', target: '客户 A' }),
    )

    expect(screen.queryByText('操作类型')).toBeNull()
    expect(container.textContent).not.toContain('创建钣金订单')
  })

  it('目标非空时渲染「目标」行', () => {
    renderPanel(pending({ target: '2026-08-30 考勤计划' }))

    expect(screen.getByText('目标')).toBeInTheDocument()
    expect(screen.getByText('2026-08-30 考勤计划')).toBeInTheDocument()
  })

  it('目标为空串时不渲染「目标」行', () => {
    const { container } = renderPanel(pending({ target: '' }))

    expect(screen.queryByText('目标')).toBeNull()
    // 空 meta 区整体不渲染
    expect(container.querySelector('.confirmation-panel__meta')).toBeNull()
  })

  it('拒绝按钮文案为「拒绝」，可访问名称与 title 对齐', () => {
    renderPanel(pending())

    const reject = screen.getByRole('button', { name: '拒绝并取消任务（危险操作）' })
    expect(reject).toHaveTextContent('拒绝')
    expect(reject).toHaveAttribute('title', '拒绝并取消任务（危险操作）')
    // 「确认」文案不变
    expect(screen.getByRole('button', { name: '确认并执行该写入计划' })).toHaveTextContent('确认')
  })

  it('渲染分支不影响确认回调（确认卡契约仍成立）', async () => {
    const onConfirm = vi.fn(async () => undefined)
    renderPanel(attendancePending(attendanceFields), onConfirm)

    fireEvent.click(screen.getByRole('button', { name: '确认并执行该写入计划' }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(true, 'hash-1'))
  })

  it('渲染分支不影响拒绝回调（确认卡契约仍成立）', async () => {
    const onConfirm = vi.fn(async () => undefined)
    renderPanel(attendancePending(attendanceFields), onConfirm)

    fireEvent.click(screen.getByRole('button', { name: '拒绝并取消任务（危险操作）' }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(false, 'hash-1'))
  })
})

describe('ConfirmationPanel 多明细卡（订单/收支泛化）', () => {
  it('表头为「明细 | 操作 | 字段和值」三列', () => {
    renderPanel(multiDetailPending())

    const headers = screen.getAllByRole('columnheader').map((el) => el.textContent)
    expect(headers).toEqual(['明细', '操作', '字段和值'])
  })

  it('每行行头为业务标识，操作列显示「新建/更新」', () => {
    const { container } = renderPanel(
      multiDetailPending({
        fields_to_write: {
          '1. 客户 铭泰 / M1': {
            操作: '新建',
            字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 },
          },
          '2. 客户 示例 / M2': {
            操作: '更新',
            字段和值: { 客户: '示例', 型号: 'M2', 数量: 5 },
          },
        },
      }),
    )

    // 只取主表直接行的行头（嵌套「字段和值」子表的字段名也是 rowheader，需排除）
    const mainHeaders = Array.from(
      container.querySelectorAll('.confirmation-panel__plan > tbody > tr > th'),
    ).map((el) => el.textContent)
    expect(mainHeaders).toEqual(['1. 客户 铭泰 / M1', '2. 客户 示例 / M2'])
    const rows = container.querySelectorAll('.confirmation-panel__plan > tbody > tr')
    expect(rows[0].textContent).toContain('新建')
    expect(rows[1].textContent).toContain('更新')
  })

  it('「字段和值」渲染为逐字段小表，不出现 [object Object]', () => {
    const { container } = renderPanel(multiDetailPending())

    // 嵌套 dict 必须逐字段展开，而非 String(value) 成 [object Object] 或 JSON 串
    expect(container.textContent).not.toContain('[object Object]')
    expect(screen.getByText('铭泰')).toBeInTheDocument()
    expect(screen.getByText('M1')).toBeInTheDocument()
    expect(screen.getByText('示例')).toBeInTheDocument()
    // 每条明细的「字段和值」格内是一张子表（2 条明细 → 2 张嵌套子表）
    const nested = container.querySelectorAll(
      '.confirmation-panel__plan td table.data-table--compact',
    )
    expect(nested).toHaveLength(2)
    // 子表的字段名是行头
    expect(
      Array.from(nested[0].querySelectorAll('th')).map((el) => el.textContent),
    ).toEqual(['客户', '型号', '数量'])
  })

  it('row_quantifier=条：折叠文案为「没有变化的 N 条」', () => {
    renderPanel(
      multiDetailPending({
        fields_to_write: {
          '1. 客户 铭泰 / M1': {
            操作: '新建',
            字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 },
          },
          '2. 客户 示例 / M2': {
            操作: '保持',
            字段和值: { 客户: '示例', 型号: 'M2', 数量: 5 },
          },
        },
      }),
    )

    expect(screen.getByText('没有变化的 1 条（点击展开）')).toBeInTheDocument()
  })

  it('旧卡（未带 row_header_label/row_quantifier）：回退为「明细」/「条」', () => {
    // 不带两个展示字段的卡（旧形状 fallback 语义）——后端条件输出，字段缺失。
    renderPanel(
      pending({
        fields_to_write: {
          '1. 客户 铭泰 / M1': {
            操作: '新建',
            字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 },
          },
          '2. 客户 示例 / M2': {
            操作: '保持',
            字段和值: { 客户: '示例', 型号: 'M2', 数量: 5 },
          },
        },
      }),
    )

    const headers = screen.getAllByRole('columnheader').map((el) => el.textContent)
    expect(headers).toEqual(['明细', '操作', '字段和值'])
    expect(screen.getByText('没有变化的 1 条（点击展开）')).toBeInTheDocument()
  })

  it('混合形状：旧扁平行 + 新明细行同卡共存不崩', () => {
    const { container } = renderPanel(
      pending({
        row_header_label: '明细',
        row_quantifier: '条',
        fields_to_write: {
          备注: '原样文本',
          '1. 客户 铭泰 / M1': {
            操作: '新建',
            字段和值: { 客户: '铭泰', 型号: 'M1', 数量: 12 },
          },
          数量: 3,
        },
      }),
    )

    // 旧普通行走 normal 分支（行头 + 跨列单格），新明细行走 action 分支
    expect(screen.getByRole('rowheader', { name: '备注' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '1. 客户 铭泰 / M1' })).toBeInTheDocument()
    expect(screen.getByText('原样文本')).toBeInTheDocument()
    expect(screen.getByText('铭泰')).toBeInTheDocument()
    expect(container.textContent).not.toContain('[object Object]')
  })
})

describe('确认卡「原话 / 来源提示」（2026-10-05，纯展示级可选键）', () => {
  it('带 source_quote：渲染「原话：…」小字', () => {
    renderPanel(pending({ source_quote: '把数量改成 5 个' }))

    expect(screen.getByText('原话：把数量改成 5 个')).toBeInTheDocument()
  })

  it('带 source_note：渲染分类来源提示行', () => {
    const note = '分类由模型按你的说法判定；若与你的说法不符，请在确认前修改'
    renderPanel(pending({ source_note: note }))

    expect(screen.getByText(note)).toBeInTheDocument()
  })

  it('两个键都没有：不渲染这两行（旧卡 DOM 零变化）', () => {
    const { container } = renderPanel(pending())

    expect(screen.queryByText(/^原话：/)).toBeNull()
    expect(container.querySelector('.confirmation-panel__source')).toBeNull()
  })

  it('键为空串：同样不渲染（视为未提供）', () => {
    const { container } = renderPanel(pending({ source_quote: '', source_note: '' }))

    expect(container.querySelector('.confirmation-panel__source')).toBeNull()
  })
})

describe('确认卡全网格边框的作用域（DOM 层）', () => {
  // CSS 全网格规则以 `.confirmation-panel .data-table` 选择器限定作用域
  // （见 src/styles/global.css）。这里在 DOM 层钉住「确认卡的表格确为
  // .confirmation-panel 后代且带 .data-table 类」——选择器必然命中，
  // 而全局 .data-table 未被改动，其它页面表格不受影响。
  it('确认卡内的数据表是 .confirmation-panel 后代且带 .data-table 类', () => {
    const { container } = renderPanel(attendancePending(attendanceFields))

    const panel = container.querySelector('.confirmation-panel')
    expect(panel).not.toBeNull()
    const tables = panel?.querySelectorAll('.data-table') ?? []
    expect(tables.length).toBeGreaterThan(0)
  })
})
