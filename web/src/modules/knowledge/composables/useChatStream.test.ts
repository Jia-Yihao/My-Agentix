import { describe, expect, it } from 'vitest'

import { parseHttpError } from './useChatStream'

function resWith(body: unknown): Response {
  return {
    json: async () => body,
  } as unknown as Response
}

describe('parseHttpError（HTTP 错误体形状覆盖，对齐 chat 模块 extractChatError）', () => {
  it('AppError 形状优先取 user_hint', async () => {
    const hint = await parseHttpError(
      resWith({ request_id: 'r1', error_code: 'RANGE', message: '范围错误', user_hint: '请选择知识库' }),
    )
    expect(hint).toBe('请选择知识库')
  })

  it('FastAPI 422 detail 数组给出可诊断文案（不再恒为"请求失败"）', async () => {
    const hint = await parseHttpError(
      resWith({ detail: [{ msg: 'Input should be less than or equal to 5000' }] }),
    )
    expect(hint).toContain('参数校验失败')
    expect(hint).toContain('5000')
  })

  it('detail 为字符串时直接透出', async () => {
    const hint = await parseHttpError(resWith({ detail: 'Session not found' }))
    expect(hint).toBe('Session not found')
  })

  it('detail 为对象时取 message/msg', async () => {
    const hint = await parseHttpError(resWith({ detail: { message: '致命错误', error_code: 'X' } }))
    expect(hint).toBe('致命错误')
  })

  it('中间件 {error, message} 形状', async () => {
    const hint = await parseHttpError(resWith({ error: 'Invalid API key' }))
    expect(hint).toBe('Invalid API key')
  })

  it('响应体非 JSON 时兜底', async () => {
    const bad = {
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response
    expect(await parseHttpError(bad)).toBe('请求失败')
  })
})
