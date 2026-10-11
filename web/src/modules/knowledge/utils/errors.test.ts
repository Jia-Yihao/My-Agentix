import { describe, expect, it } from 'vitest'
import {
  ErrorCode,
  getApiErrorPayload,
  getApiErrorHint,
  isAbortError,
} from './errors'

describe('ErrorCode', () => {
  it('contains all expected error codes', () => {
    expect(ErrorCode.VALIDATION_ERROR).toBe('VALIDATION_ERROR')
    expect(ErrorCode.UNAUTHORIZED).toBe('UNAUTHORIZED')
    expect(ErrorCode.FORBIDDEN).toBe('FORBIDDEN')
    expect(ErrorCode.KB_NOT_FOUND).toBe('KB_NOT_FOUND')
    expect(ErrorCode.INDEX_NOT_READY).toBe('INDEX_NOT_READY')
    expect(ErrorCode.INTERNAL_ERROR).toBe('INTERNAL_ERROR')
  })
})

describe('getApiErrorPayload', () => {
  it('returns undefined for null/undefined', () => {
    expect(getApiErrorPayload(null)).toBeUndefined()
    expect(getApiErrorPayload(undefined)).toBeUndefined()
  })

  it('returns undefined for non-object', () => {
    expect(getApiErrorPayload('string')).toBeUndefined()
    expect(getApiErrorPayload(123)).toBeUndefined()
  })

  it('extracts error_code from response.data', () => {
    const err = {
      response: {
        data: {
          error_code: 'VALIDATION_ERROR',
          message: 'Invalid input',
        },
      },
    }
    const payload = getApiErrorPayload(err)
    expect(payload).toEqual({
      error_code: 'VALIDATION_ERROR',
      message: 'Invalid input',
    })
  })

  it('extracts from nested detail', () => {
    const err = {
      response: {
        data: {
          detail: {
            error_code: 'KB_NOT_FOUND',
            message: 'Knowledge base not found',
          },
        },
      },
    }
    const payload = getApiErrorPayload(err)
    expect(payload).toEqual({
      error_code: 'KB_NOT_FOUND',
      message: 'Knowledge base not found',
    })
  })

  it('returns undefined when no error_code', () => {
    const err = {
      response: {
        data: {
          message: 'Some error',
        },
      },
    }
    expect(getApiErrorPayload(err)).toBeUndefined()
  })
})

describe('getApiErrorHint', () => {
  it('returns user_hint when available', () => {
    const err = {
      response: {
        data: {
          error_code: 'VALIDATION_ERROR',
          user_hint: '请检查输入格式',
          message: 'Invalid input',
        },
      },
    }
    expect(getApiErrorHint(err)).toBe('请检查输入格式')
  })

  it('falls back to message', () => {
    const err = {
      response: {
        data: {
          error_code: 'INTERNAL_ERROR',
          message: '服务器内部错误',
        },
      },
    }
    expect(getApiErrorHint(err)).toBe('服务器内部错误')
  })

  it('falls back to Error.message', () => {
    const err = new Error('Network error')
    expect(getApiErrorHint(err)).toBe('Network error')
  })

  it('falls back to default hint', () => {
    expect(getApiErrorHint(null)).toBe('请求失败')
    expect(getApiErrorHint('unknown')).toBe('请求失败')
    expect(getApiErrorHint('unknown', '自定义默认')).toBe('自定义默认')
  })
})

describe('isAbortError', () => {
  it('returns true for AbortError', () => {
    expect(isAbortError({ name: 'AbortError' })).toBe(true)
  })

  it('returns false for other errors', () => {
    expect(isAbortError({ name: 'TypeError' })).toBe(false)
    expect(isAbortError(null)).toBe(false)
    expect(isAbortError('string')).toBe(false)
  })
})
