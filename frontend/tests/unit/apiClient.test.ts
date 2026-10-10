/**
 * toApiError 异常归一化测试（2026-09-11 上传链路收口）。
 *
 * 缺陷背景：此前 5 处调用点各自写「非 ApiError 一律归 NETWORK_ERROR」，把
 * zod 校验失败/代码缺陷谎报成「无法连接服务器」（2026-09-10 附件上传事故）。
 * 本文件钉住分流规则：ApiError 原样、ZodError→VALIDATION_ERROR（透出真实
 * message）、TypeError→NETWORK_ERROR（真网络失败保护）、其它→UNKNOWN_ERROR。
 */
import { describe, expect, it } from 'vitest'
import { ApiError, toApiError } from '../../src/api/client'
import { chatMessageSchema } from '../../src/api/schemas'
import { ERROR_CODES } from '../../src/api/types'

/** 用真实 schema 触发一个 ZodError（保证与线上校验收敛路径一致）。 */
function zodErrorFrom(input: string): unknown {
  try {
    chatMessageSchema.parse(input)
  } catch (err) {
    return err
  }
  throw new Error('期望 parse 失败，但没有抛错')
}

describe('toApiError 异常归一化', () => {
  it('ApiError 原样返回（后端契约错误不被改写）', () => {
    const original = new ApiError(403, { error_code: 'FORBIDDEN', message: '无权限' })
    expect(toApiError(original)).toBe(original)
  })

  it('ZodError → VALIDATION_ERROR 且透出首条 issue（内容不能超过 10000 字）', () => {
    const err = toApiError(zodErrorFrom('x'.repeat(10001)))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.errorCode).toBe(ERROR_CODES.VALIDATION_ERROR)
    expect(err.message).toBe('内容不能超过 10000 字')
  })

  it('ZodError（min 校验）→ VALIDATION_ERROR 且透出「请输入内容」', () => {
    const err = toApiError(zodErrorFrom(''))
    expect(err.errorCode).toBe(ERROR_CODES.VALIDATION_ERROR)
    expect(err.message).toBe('请输入内容')
  })

  it('TypeError → NETWORK_ERROR（fetch 网络不可达仍提示网络问题）', () => {
    const err = toApiError(new TypeError('fetch failed'))
    expect(err.errorCode).toBe(ERROR_CODES.NETWORK_ERROR)
  })

  it('其它异常 → UNKNOWN_ERROR（不再谎称网络问题）', () => {
    const err = toApiError(new Error('代码缺陷'))
    expect(err.errorCode).toBe('UNKNOWN_ERROR')
  })
})

describe('ApiError 构造：413 → PAYLOAD_TOO_LARGE', () => {
  it('非 JSON 413 错误体（网关 HTML 页，json 解析失败）→ PAYLOAD_TOO_LARGE', () => {
    const err = new ApiError(413, {})
    expect(err.errorCode).toBe(ERROR_CODES.PAYLOAD_TOO_LARGE)
    expect(err.status).toBe(413)
  })

  it('携带后端 error_code 的 413 保留后端码（不覆盖契约错误）', () => {
    const err = new ApiError(413, { error_code: 'CUSTOM_413' })
    expect(err.errorCode).toBe('CUSTOM_413')
  })
})
