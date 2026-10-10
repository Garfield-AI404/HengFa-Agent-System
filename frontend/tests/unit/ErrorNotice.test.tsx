/**
 * ErrorNotice 提示分流测试（2026-09-11 上传链路收口）。
 *
 * 钉住几件事：
 * - PAYLOAD_TOO_LARGE 给出「怎么办」的文案，上限与 ATTACHMENT_MAX_MB 同源
 *   （不在组件里写死数字，避免与后端 attachment_max_bytes 漂移）；
 * - VALIDATION_ERROR 不登记笼统提示，回退展示具体 message；THREAD_MISSING 同理；
 * - UNKNOWN_ERROR 则登记固定中文提示，覆盖底层英文原始异常。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../../src/api/client'
import { ATTACHMENT_MAX_MB } from '../../src/api/chat.api'
import { ERROR_CODES } from '../../src/api/types'
import { ErrorNotice } from '../../src/components/ErrorNotice'

describe('ErrorNotice 提示分流', () => {
  it('PAYLOAD_TOO_LARGE：给出「文件过大」的处置引导 + 与常量同源的上限', () => {
    render(<ErrorNotice error={new ApiError(413, {})} />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('文件过大')
    expect(alert).toHaveTextContent(`${ATTACHMENT_MAX_MB} MB`)
    expect(alert).toHaveTextContent('请压缩或拆分后重试')
  })

  it('VALIDATION_ERROR：透出具体 message，不被笼统文案覆盖', () => {
    render(
      <ErrorNotice
        error={
          new ApiError(0, {
            error_code: ERROR_CODES.VALIDATION_ERROR,
            message: '内容不能超过 10000 字',
          })
        }
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('内容不能超过 10000 字')
  })

  it('UNKNOWN_ERROR：如实但不误导的中文提示，不渲染英文原始异常', () => {
    render(
      <ErrorNotice
        error={
          new ApiError(0, {
            error_code: 'UNKNOWN_ERROR',
            // 典型场景：HTTP 200 但响应体非 JSON → 原始英文异常不得直接上屏
            message: 'SyntaxError: Unexpected end of JSON input',
          })
        }
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('系统处理异常，请稍后重试。')
    expect(alert).not.toHaveTextContent('SyntaxError')
    // 不得再谎称网络问题（本批要消灭的）
    expect(alert).not.toHaveTextContent('无法连接服务器')
  })

  it('THREAD_MISSING：无 HINT，具体中文原因经回退路径透出', () => {
    render(
      <ErrorNotice
        error={
          new ApiError(0, {
            error_code: 'THREAD_MISSING',
            message: '缺少会话线程，无法加载对账明细。',
          })
        }
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('缺少会话线程，无法加载对账明细。')
    expect(alert).not.toHaveTextContent('无法连接服务器')
  })
})
