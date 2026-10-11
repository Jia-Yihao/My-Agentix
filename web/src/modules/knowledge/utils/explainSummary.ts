/**
 * X3 过程面板：把 AnswerExplanation 载荷映射为紧凑视图模型（纯函数，可单测）。
 *
 * 设计约束：not_recorded 块不编造——映射为「未记录」占位行；
 * 证据块缺省（no_retrieval）→ 显示"本次回答未走检索"。
 */
import type { AnswerExplanation } from '../api/chatTypes'

export type ExplainEvidenceRow = {
  chunk_id: string
  score: number | null
  source: string
  effective_status: string | null
  content_preview: string
}

export type ExplainView = {
  model_name: string
  question: string
  query: string | null
  hit_count: number | null
  top_k: number | null
  evidence: ExplainEvidenceRow[]
  no_retrieval: boolean
  citation_count: number
  not_recorded: string[]
}

/** not_recorded 块 → 用户可读的"未记录"说明 */
const NOT_RECORDED_LABELS: Record<string, string> = {
  route_decision: '路由理由',
  citation_verifier: '引用校验判定',
  grounding: '证据锚定评分',
  prompt_version: 'Prompt 版本',
}

export function buildExplainView(payload: AnswerExplanation): ExplainView {
  const evidenceBlock = payload.evidence
  const recorded = evidenceBlock.status === 'recorded'
  const run = recorded ? evidenceBlock.retrieval_run : null

  return {
    model_name: payload.model?.name || '未知模型',
    question: payload.question?.content || '',
    query: run?.query_text ?? null,
    hit_count: run?.hit_count ?? null,
    top_k: run?.top_k ?? null,
    evidence: recorded
      ? (run?.chunks ?? []).map((c) => ({
          chunk_id: c.chunk_id || '',
          score: c.score,
          source: c.source || '未知来源',
          effective_status: c.effective_status,
          content_preview: c.content_preview,
        }))
      : [],
    no_retrieval: evidenceBlock.status === 'no_retrieval',
    citation_count: payload.citations?.length ?? 0,
    not_recorded: Object.entries(payload.validation ?? {})
      .filter(([, v]) => v?.status === 'not_recorded')
      .map(([k]) => NOT_RECORDED_LABELS[k] || k),
  }
}
