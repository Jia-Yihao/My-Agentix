import { describe, expect, it } from 'vitest'

import { extractSseDataLines, readSseData } from './sse'

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

describe('共享 SSE framing（api/sse.ts，A5/R5c）', () => {
  it('按 \\n\\n 切帧并逐条派发 data 载荷（跨 chunk 拼接）', async () => {
    const lines: string[] = []
    await readSseData(
      fakeResponse([
        'data: {"type":"message"',
        ',"content":"hi"}\n\ndata: {"type":"done"}\n\n',
      ]),
      (l) => void lines.push(l),
    )
    expect(lines).toEqual(['{"type":"message","content":"hi"}', '{"type":"done"}'])
  })

  it('支持 \\r\\n\\r\\n 帧边界与残留尾帧（无尾空行）', async () => {
    const lines: string[] = []
    await readSseData(
      fakeResponse(['data: a\r\n\r\ndata: tail']),
      (l) => void lines.push(l),
    )
    expect(lines).toEqual(['a', 'tail'])
  })

  it('提取块内全部 data 行并滤掉空载荷与 [DONE]', () => {
    const block = [
      ': comment line',
      'data:',
      'data: [DONE]',
      'data: {"type":"message"}',
      'data:{"type":"done"}',
    ].join('\n')
    expect(extractSseDataLines(block)).toEqual([
      '{"type":"message"}',
      '{"type":"done"}',
    ])
  })

  it('缓冲超限：先 cancel 再抛 overflow（不静默吞）', async () => {
    const big = 'x'.repeat(2048)
    await expect(
      readSseData(fakeResponse([big]), () => undefined, { maxBuffer: 1024 }),
    ).rejects.toThrow('stream buffer overflow')
  })

  it('onData 抛错向外传播（截断检测依赖该语义）', async () => {
    await expect(
      readSseData(fakeResponse(['data: x\n\n']), () => {
        throw new Error('boom')
      }),
    ).rejects.toThrow('boom')
  })
})
