import { describe, expect, it } from 'vitest'

import { buildExplainView } from './explainSummary'
import type { AnswerExplanation } from '../api/chatTypes'

function basePayload(): AnswerExplanation {
  return {
    answer: {
      id: 'a1',
      conversation_id: 'c1',
      question_message_id: 'm1',
      version: 1,
      is_current: true,
      created_at: '2026-09-21T00:00:00',
    },
    question: { message_id: 'm1', content: '劳动合同违约金上限是多少？' },
    model: { name: 'qwen3-max', prompt_version: { status: 'not_recorded' } },
    evidence: { status: 'no_retrieval' },
    citations: [],
    validation: {
      route_decision: { status: 'not_recorded' },
      citation_verifier: { status: 'not_recorded' },
      grounding: { status: 'not_recorded' },
      prompt_version: { status: 'not_recorded' },
    },
    notes: 'X2 v1',
  }
}

describe('X3 过程面板视图映射（buildExplainView）', () => {
  it('no_retrieval：无检索证据，not_recorded 块转为可读说明', () => {
    const view = buildExplainView(basePayload())
    expect(view.no_retrieval).toBe(true)
    expect(view.evidence).toEqual([])
    expect(view.model_name).toBe('qwen3-max')
    expect(view.question).toBe('劳动合同违约金上限是多少？')
    expect(view.not_recorded).toEqual(['路由理由', '引用校验判定', '证据锚定评分', 'Prompt 版本'])
  })

  it('recorded：证据块映射出相关度/来源/效力状态', () => {
    const payload = basePayload()
    payload.evidence = {
      status: 'recorded',
      retrieval_run: {
        query_text: '劳动合同 违约金 上限',
        top_k: 5,
        hit_count: 2,
        chunks: [
          {
            chunk_id: 'chunk-1',
            score: 0.91,
            source: '劳动合同法',
            effective_status: 'in_force',
            content_preview: '第二十五条……',
          },
          {
            chunk_id: null,
            score: null,
            source: null,
            effective_status: null,
            content_preview: '相关节选……',
          },
        ],
      },
    }
    payload.citations = [{ chunk_id: 'chunk-1', snippet: '第二十五条……' }]

    const view = buildExplainView(payload)
    expect(view.no_retrieval).toBe(false)
    expect(view.query).toBe('劳动合同 违约金 上限')
    expect(view.hit_count).toBe(2)
    expect(view.top_k).toBe(5)
    expect(view.citation_count).toBe(1)
    expect(view.evidence).toHaveLength(2)
    expect(view.evidence[0]).toEqual({
      chunk_id: 'chunk-1',
      score: 0.91,
      source: '劳动合同法',
      effective_status: 'in_force',
      content_preview: '第二十五条……',
    })
    // 缺失字段回退：chunk_id 空串、source 未知来源
    expect(view.evidence[1].chunk_id).toBe('')
    expect(view.evidence[1].source).toBe('未知来源')
  })
})
