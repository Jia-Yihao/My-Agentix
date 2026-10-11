import { describe, expect, it } from 'vitest'

import { appendTraceEvent, traceGapLabel, type TraceStep } from './traceTimeline'

describe('appendTraceEvent', () => {
  it('按事件序列折叠：meta → tool_running → 首个 answer_chunk → finished', () => {
    const steps: TraceStep[] = []
    let t = 1000

    appendTraceEvent(steps, { event: 'meta' }, (t += 10))
    appendTraceEvent(steps, { event: 'tool_running', content: '图谱查询中…' }, (t += 120))
    appendTraceEvent(steps, { event: 'answer_chunk', content: '根据' }, (t += 300))
    appendTraceEvent(steps, { event: 'answer_chunk', content: '劳动法…' }, (t += 30))
    appendTraceEvent(steps, { finished: true, answer_id: 'a1' }, (t += 500))

    expect(steps.map((s) => `${s.kind}:${s.label}`)).toEqual([
      'stage:会话与检索就绪',
      'tool:图谱查询中…',
      'answer:生成回答',
      'done:回答完成',
    ])
    expect(steps[3].at).toBe(t)
  })

  it('去抖：相邻同类 tool_running 事件只记一次', () => {
    const steps: TraceStep[] = []
    appendTraceEvent(steps, { event: 'tool_running', content: '图谱查询中…' }, 1)
    appendTraceEvent(steps, { event: 'tool_running', content: '图谱查询中…' }, 2)
    appendTraceEvent(steps, { event: 'tool_running', content: '向量检索中…' }, 3)
    expect(steps).toHaveLength(2)
  })

  it('error 事件入列且保留原始消息', () => {
    const steps: TraceStep[] = []
    appendTraceEvent(steps, { event: 'error', content: '索引未就绪' }, 5)
    expect(steps).toEqual([{ kind: 'error', label: '索引未就绪', at: 5 }])
  })

  it('逐 token answer_chunk 不重复入列（去抖）', () => {
    const steps: TraceStep[] = []
    appendTraceEvent(steps, { event: 'answer_chunk', content: 'a' }, 1)
    appendTraceEvent(steps, { event: 'answer_chunk', content: 'b' }, 2)
    appendTraceEvent(steps, { event: 'answer_chunk', content: 'c' }, 3)
    expect(steps).toHaveLength(1)
  })

  it('progress 事件入轨迹（stage 文案，相邻同文案去抖）', () => {
    const steps: TraceStep[] = []
    appendTraceEvent(
      steps,
      { event: 'progress', stage: 'loading', message: '加载文档' },
      1,
    )
    appendTraceEvent(
      steps,
      { event: 'progress', stage: 'loading', message: '加载文档' },
      2,
    )
    appendTraceEvent(
      steps,
      { event: 'progress', stage: 'retrieval', message: '检索知识库' },
      3,
    )
    // stage 无 message 时回退 stage 键 → "处理中" 兜底
    appendTraceEvent(steps, { event: 'progress', stage: '' }, 4)
    expect(steps.map((s) => `${s.kind}:${s.label}`)).toEqual([
      'stage:加载文档',
      'stage:检索知识库',
      'stage:处理中',
    ])
  })
})

describe('traceGapLabel', () => {
  it('首步无间隔；毫秒与秒两档', () => {
    const steps: TraceStep[] = [
      { kind: 'stage', label: 'x', at: 0 },
      { kind: 'tool', label: 'y', at: 230 },
      { kind: 'done', label: 'z', at: 230 + 1500 },
    ]
    expect(traceGapLabel(steps, 0)).toBe('')
    expect(traceGapLabel(steps, 1)).toBe('230ms')
    expect(traceGapLabel(steps, 2)).toBe('1.5s')
  })
})
