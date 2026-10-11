/**
 * X3 过程面板：答案解释 API 客户端。
 *
 * GET /api/v1/retrieval/answers/{answer_id}/explanation —— 聚合
 * 问题/证据/引用/模型等已持久化事实（X2 v1，零副作用只读端点）。
 */
import { http } from './http'

import type { AnswerExplanation } from './chatTypes'

export async function fetchAnswerExplanation(answerId: string): Promise<AnswerExplanation> {
  const resp = await http.get<{ data: AnswerExplanation }>(
    `/answers/${encodeURIComponent(answerId)}/explanation`,
  )
  return resp.data.data
}
