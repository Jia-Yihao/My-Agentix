import { describe, expect, it } from 'vitest'

import { readChatSse, type ChatStreamPayload } from './chatStream'

/** 构造带分块读取的假 Response。 */
function fakeResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  let i = 0
  return {
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

describe('knowledge 模块 SSE 读取（readChatSse）', () => {
  it('按 \\n\\n 切帧并派发 data 载荷', async () => {
    const payloads: ChatStreamPayload[] = []
    await readChatSse(
      fakeResponse([
        'data: {"event":"answer_chunk","content":"你"}\n\n',
        'data: {"event":"answer_chunk","content":"好"}\n\n',
      ]),
      { onPayload: (p) => void payloads.push(p) },
    )
    expect(payloads.map((p) => p.content)).toEqual(['你', '好'])
  })

  it('支持 \\r\\n\\r\\n 帧边界', async () => {
    const payloads: ChatStreamPayload[] = []
    await readChatSse(
      fakeResponse(['data: {"event":"meta","conversation_id":"c1"}\r\n\r\n']),
      { onPayload: (p) => void payloads.push(p) },
    )
    expect(payloads).toHaveLength(1)
    expect(payloads[0].conversation_id).toBe('c1')
  })

  it('结束时派发残留帧（服务端不补尾空行）', async () => {
    const payloads: ChatStreamPayload[] = []
    await readChatSse(fakeResponse(['data: {"event":"answer_chunk","content":"tail"}']), {
      onPayload: (p) => void payloads.push(p),
    })
    expect(payloads).toHaveLength(1)
    expect(payloads[0].content).toBe('tail')
  })

  it('坏 JSON 帧跳过不打断流', async () => {
    const payloads: ChatStreamPayload[] = []
    await readChatSse(
      fakeResponse([
        'data: {broken\n\n',
        'data: {"event":"answer_chunk","content":"ok"}\n\n',
      ]),
      { onPayload: (p) => void payloads.push(p) },
    )
    expect(payloads.map((p) => p.content)).toEqual(['ok'])
  })

  it('onPayload 抛错时错误向外传播（截断检测依赖该语义）', async () => {
    await expect(
      readChatSse(fakeResponse(['data: {"event":"error","content":"x"}\n\n']), {
        onPayload: () => {
          throw new Error('boom')
        },
      }),
    ).rejects.toThrow('boom')
  })

  it('批次 B2：event 缺失回退 type 并填充；冲突时 event 优先', async () => {
    const payloads: ChatStreamPayload[] = []
    await readChatSse(
      fakeResponse([
        'data: {"type":"progress","stage":"loading"}\n\n',
        'data: {"event":"meta","type":"meta"}\n\n',
        'data: {"event":"a","type":"b"}\n\n',
      ]),
      { onPayload: (p) => void payloads.push(p) },
    )

    expect(payloads[0]?.event).toBe('progress')
    expect(payloads[1]?.event).toBe('meta')
    expect(payloads[2]?.event).toBe('a')
    expect(payloads[2]?.type).toBe('b')
  })
})
