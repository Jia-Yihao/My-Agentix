import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatStreamPayload } from '../api/chatStream'
import { STREAM_BUFFER_WINDOW_MS, isBufferablePayload, useStreamBuffer } from './useStreamBuffer'

describe('isBufferablePayload（W6 A1 可缓冲帧判定）', () => {
  it('token / 思考 / 工具 / 进度帧可缓冲', () => {
    expect(isBufferablePayload({ event: 'answer_chunk', content: '你' })).toBe(true)
    expect(isBufferablePayload({ event: 'thinking_chunk', content: '想' })).toBe(true)
    expect(isBufferablePayload({ event: 'tool_running', content: '检索中' })).toBe(true)
    expect(isBufferablePayload({ event: 'progress', stage: 'retrieval' })).toBe(true)
  })

  it('answer_chunk 空 content 不进缓冲（控制语义帧直通）', () => {
    expect(isBufferablePayload({ event: 'answer_chunk', content: '' })).toBe(false)
    expect(isBufferablePayload({ event: 'answer_chunk' })).toBe(false)
  })

  it('终态帧（finished/error）不进缓冲', () => {
    expect(isBufferablePayload({ event: 'finished', duration_ms: 1200 })).toBe(false)
    expect(isBufferablePayload({ event: 'error', message: 'x' })).toBe(false)
  })
})

describe('useStreamBuffer（50ms 批渲染缓冲）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('默认窗口 50ms；窗口内多帧一次 flush 按序应用', () => {
    expect(STREAM_BUFFER_WINDOW_MS).toBe(50)
    const applied: string[] = []
    const onFlush = vi.fn()
    const buf = useStreamBuffer({
      apply: (p: ChatStreamPayload) => applied.push(p.content ?? p.event ?? ''),
      onFlush,
    })

    buf.push({ event: 'answer_chunk', content: 'a' })
    buf.push({ event: 'answer_chunk', content: 'b' })
    buf.push({ event: 'progress', stage: 'retrieval' })
    expect(applied).toHaveLength(0)

    vi.advanceTimersByTime(50)
    expect(applied).toEqual(['a', 'b', 'progress'])
    expect(onFlush).toHaveBeenCalledTimes(1)
  })

  it('顺序恒等：逐帧直推与批应用结果一致', () => {
    const batch: string[] = []
    const buf = useStreamBuffer({ apply: (p) => batch.push(p.content ?? '') })
    for (const c of ['1', '2', '3']) buf.push({ event: 'answer_chunk', content: c })
    vi.advanceTimersByTime(STREAM_BUFFER_WINDOW_MS)
    expect(batch.join('')).toBe('123')
  })

  it('窗口滚动：窗口内新帧不重置定时器（首帧起算）', () => {
    const applied: string[] = []
    const buf = useStreamBuffer({ apply: (p) => applied.push(p.content ?? '') })
    buf.push({ event: 'answer_chunk', content: 'a' })
    vi.advanceTimersByTime(30)
    buf.push({ event: 'answer_chunk', content: 'b' })
    vi.advanceTimersByTime(20)
    expect(applied).toEqual(['a', 'b'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('flush 立即落盘且清空队列（二次 flush 无重复）', () => {
    const applied: string[] = []
    const buf = useStreamBuffer({ apply: (p) => applied.push(p.content ?? '') })
    buf.push({ event: 'answer_chunk', content: 'x' })
    buf.flush()
    expect(applied).toEqual(['x'])
    buf.flush()
    expect(applied).toEqual(['x'])
  })

  it('pushNow：先落缓冲再应用终帧（先缓冲后终态，顺序保真）', () => {
    const applied: string[] = []
    const buf = useStreamBuffer({
      apply: (p) => applied.push(p.content ?? p.event ?? ''),
    })
    buf.push({ event: 'answer_chunk', content: 'tok' })
    buf.pushNow({ event: 'finished', duration_ms: 900 })
    expect(applied).toEqual(['tok', 'finished'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('dispose 兜底 flush（abort 也不丢部分回答）', () => {
    const applied: string[] = []
    const buf = useStreamBuffer({ apply: (p) => applied.push(p.content ?? '') })
    buf.push({ event: 'answer_chunk', content: 'partial' })
    buf.dispose()
    expect(applied).toEqual(['partial'])
  })

  it('onFlush 一窗最多一次（渲染后处理节流）', () => {
    const onFlush = vi.fn()
    const buf = useStreamBuffer({ apply: () => {}, onFlush })
    buf.push({ event: 'answer_chunk', content: 'a' })
    buf.push({ event: 'answer_chunk', content: 'b' })
    vi.advanceTimersByTime(50)
    expect(onFlush).toHaveBeenCalledTimes(1)
    buf.push({ event: 'answer_chunk', content: 'c' })
    vi.advanceTimersByTime(50)
    expect(onFlush).toHaveBeenCalledTimes(2)
  })
})
