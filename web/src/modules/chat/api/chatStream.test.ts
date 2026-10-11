import { afterEach, describe, expect, it, vi } from 'vitest'

import { streamChat, type ChatStreamHandlers } from './chatStream'

/** 构造带分块读取能力的假 Response（不触网）。 */
function fakeResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  let i = 0
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (i < chunks.length) {
              return { done: false, value: encoder.encode(chunks[i++]) }
            }
            return { done: true, value: undefined }
          },
          releaseLock() {},
          async cancel() {},
        }
      },
    },
  } as unknown as Response
}

function stubFetch(chunks: string[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => fakeResponse(chunks)),
  )
}

function collectHandlers(): ChatStreamHandlers & {
  messages: string[]
  errors: string[]
  doneCalls: number
  doneSources: unknown[][]
  sessions: string[]
} {
  const h = {
    messages: [] as string[],
    errors: [] as string[],
    doneCalls: 0,
    doneSources: [] as unknown[][],
    sessions: [] as string[],
    onMessage: (t: string) => h.messages.push(t),
    onError: (m: string) => h.errors.push(m),
    onDone: (sources: unknown[]) => {
      h.doneCalls += 1
      h.doneSources.push(sources)
    },
    onSession: (id: string) => h.sessions.push(id),
  }
  return h
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('chat 模块 SSE 解析（chatStream.streamChat）', () => {
  it('error 帧之后的 done 帧被丢弃（outcome 不得被翻转成 delivered）', async () => {
    // 后端契约：error 之后必补 done（chat_service「恰好一次 done」兜底）
    stubFetch([
      'data: {"type":"message","content":"部分","session_id":"s1"}\n\n',
      'data: {"type":"error","content":"出错了"}\n\n',
      'data: {"type":"done","metadata":{"sources":["兜底来源"]},"session_id":"s1"}\n\n',
      'data: {"type":"message","content":"不应到达"}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)

    expect(h.errors).toEqual(['出错了'])
    expect(h.doneCalls, 'error 后的 done 不得触发 onDone').toBe(0)
    expect(h.messages).toEqual(['部分'])
  })

  it('正常流：message 逐 token + done 携带 sources', async () => {
    stubFetch([
      'data: {"type":"metadata","metadata":{"status":"processing"},"session_id":"s1"}\n\n',
      'data: {"type":"message","content":"你"}\n\n',
      'data: {"type":"message","content":"好"}\n\n',
      'data: {"type":"done","metadata":{"sources":["民法典"]}}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)

    expect(h.messages).toEqual(['你', '好'])
    expect(h.doneCalls).toBe(1)
    expect(h.doneSources[0]).toEqual(['民法典'])
    expect(h.sessions).toContain('s1')
    expect(h.errors).toEqual([])
  })

  it('支持 \\r\\n\\r\\n 帧边界', async () => {
    stubFetch([
      'data: {"type":"message","content":"a"}\r\n\r\n',
      'data: {"type":"message","content":"b"}\r\n\r\n',
      'data: {"type":"done","metadata":{"sources":[]}}\r\n\r\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)
    expect(h.messages).toEqual(['a', 'b'])
    expect(h.doneCalls).toBe(1)
  })

  it('帧跨 chunk 半截到达时按 buffer 拼接后再派发', async () => {
    stubFetch([
      'data: {"type":"messa',
      'ge","content":"hi"}\n\nda',
      'ta: {"type":"done","metadata":{"sources":[]}}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)
    expect(h.messages).toEqual(['hi'])
    expect(h.doneCalls).toBe(1)
  })

  it('残留最后一帧（无尾空行）也会派发', async () => {
    stubFetch(['data: {"type":"message","content":"tail"}'])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)
    expect(h.messages).toEqual(['tail'])
  })

  it('非法 JSON 帧跳过但不打断后续流', async () => {
    stubFetch([
      'data: {broken json}\n\n',
      'data: {"type":"message","content":"ok"}\n\n',
      'data: {"type":"done","metadata":{"sources":[]}}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)
    expect(h.messages).toEqual(['ok'])
    expect(h.doneCalls).toBe(1)
  })

  it('done 之后的帧同样被丢弃（终态语义双向）', async () => {
    stubFetch([
      'data: {"type":"done","metadata":{"sources":[]}}\n\n',
      'data: {"type":"message","content":"迟到"}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)
    expect(h.doneCalls).toBe(1)
    expect(h.messages).toEqual([])
  })

  it('批次 B2：分发优先 event，旧帧（仅 type）回退 type', async () => {
    stubFetch([
      // 双写帧且人为冲突：以 event 为准
      'data: {"type":"error","event":"message","content":"由 event 决定"}\n\n',
      // 旧后端帧：仅 type → 回退到 type 分发
      'data: {"type":"message","content":"回退帧"}\n\n',
      'data: {"type":"done","metadata":{"sources":[]}}\n\n',
    ])
    const h = collectHandlers()
    await streamChat({ message: 'hi' }, h)

    expect(h.messages).toEqual(['由 event 决定', '回退帧'])
    expect(h.doneCalls).toBe(1)
    expect(h.errors).toEqual([])
  })
})
