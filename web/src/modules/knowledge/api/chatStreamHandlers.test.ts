import { describe, expect, it } from 'vitest'

import { applyChatStreamToAssistant, finalizeAssistantStream } from './chatStreamHandlers'
import type { StreamableAssistantMessage } from './chatStreamHandlers'

function msg(): StreamableAssistantMessage {
  return { content: '' }
}

describe('applyChatStreamToAssistant（W6 B4/B5 帧→消息）', () => {
  it('progress：stage/message/step/total 与 detail.stages 透传', () => {
    const m = msg()
    applyChatStreamToAssistant(
      {
        event: 'progress',
        stage: 'retrieval',
        message: '检索知识库',
        detail: {
          step: 2,
          total: 3,
          stages: [{ stage: 'rewrite', ms: 12 }],
        },
      },
      m,
    )
    expect(m.progress).toEqual({
      stage: 'retrieval',
      message: '检索知识库',
      step: 2,
      total: 3,
      stages: [{ stage: 'rewrite', ms: 12 }],
    })
  })

  it('tool_running 写入工具卡（不进 thinking/正文）', () => {
    const m = msg()
    applyChatStreamToAssistant({ event: 'tool_running', content: '正在检索法条' }, m)
    expect(m.toolActivity).toEqual({ label: '正在检索法条' })
    expect(m.thinking).toBeUndefined()
    expect(m.content).toBe('')
  })

  it('answer_chunk 累加正文且不清 progress（首 token 即消失修复）', () => {
    const m = msg()
    applyChatStreamToAssistant({ event: 'progress', stage: 'llm_generate', message: '生成' }, m)
    applyChatStreamToAssistant({ event: 'answer_chunk', content: '第一' }, m)
    applyChatStreamToAssistant({ event: 'answer_chunk', content: '段' }, m)
    expect(m.content).toBe('第一段')
    expect(m.progress?.stage).toBe('llm_generate')
  })

  it('thinking_chunk 累加思考区', () => {
    const m = msg()
    applyChatStreamToAssistant({ event: 'thinking_chunk', content: '推理' }, m)
    applyChatStreamToAssistant({ event: 'thinking_chunk', content: '中' }, m)
    expect(m.thinking).toBe('推理中')
    expect(m.content).toBe('')
  })

  it('event 缺省视作正文（兼容后端省略 event）', () => {
    const m = msg()
    applyChatStreamToAssistant({ content: '直连正文' }, m)
    expect(m.content).toBe('直连正文')
  })

  it('空 content 的非 progress 帧不产生副作用', () => {
    const m = msg()
    applyChatStreamToAssistant({ event: 'answer_chunk', content: '' }, m)
    expect(m.content).toBe('')
  })
})

describe('finalizeAssistantStream（W6 B4/B5/B8 终帧收口）', () => {
  it('progress 置空（stepper 收起）', () => {
    const m = msg()
    m.progress = { stage: 'llm_generate', message: '生成' }
    finalizeAssistantStream({ event: 'finished' }, m)
    expect(m.progress).toBeNull()
  })

  it('活动工具折叠入历史（去重），工具卡清空', () => {
    const m = msg()
    m.toolActivity = { label: '检索法条' }
    m.toolHistory = ['已有工具']
    finalizeAssistantStream({ event: 'finished' }, m)
    expect(m.toolHistory).toEqual(['已有工具', '检索法条'])
    expect(m.toolActivity).toBeNull()
  })

  it('同名工具不重复入历史', () => {
    const m = msg()
    m.toolActivity = { label: '检索法条' }
    m.toolHistory = ['检索法条']
    finalizeAssistantStream({ event: 'finished' }, m)
    expect(m.toolHistory).toEqual(['检索法条'])
  })

  it('终帧 duration_ms/ttft_ms → timing 芯片', () => {
    const m = msg()
    finalizeAssistantStream({ event: 'finished', duration_ms: 3210, ttft_ms: 480 }, m)
    expect(m.timing).toEqual({ durationMs: 3210, ttftMs: 480 })
  })

  it('无 timing 字段时不动 timing', () => {
    const m = msg()
    m.timing = { durationMs: 100 }
    finalizeAssistantStream({ event: 'finished' }, m)
    expect(m.timing).toEqual({ durationMs: 100 })
  })

  it('非数字 duration 降级 undefined（脏帧不写 NaN）', () => {
    const m = msg()
    finalizeAssistantStream(
      { event: 'finished', duration_ms: 'oops' as unknown as number },
      m,
    )
    expect(m.timing?.durationMs).toBeUndefined()
  })

  it('无工具活动时也安全（幂等）', () => {
    const m = msg()
    finalizeAssistantStream({ event: 'finished' }, m)
    finalizeAssistantStream({ event: 'finished' }, m)
    expect(m.toolActivity ?? null).toBeNull()
    expect(m.progress).toBeNull()
  })
})
