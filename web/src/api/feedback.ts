/**
 * 满意度反馈上报（P2-1；共享 API 层，与 token.ts 同层 fetch 封装模式）。
 *
 * 后端：POST /api/v1/feedback（`FEEDBACK_ENABLED=false` 时路由不存在 → 404）。
 * flag 默认关：前端组件优先由 `feedbackEnabled` prop 控制是否渲染 👍/👎；
 * 未传 prop 时组件挂载自动调 `probeFeedbackEnabled()` 探活发现；
 * 即使误触发，404 静默失败（不弹错误、不重试）——反馈是旁路数据，
 * 绝不打断对话主流程（fail-open 纪律）。
 */
import { API_BASE_URL, apiKeyHeader } from './config'
import { getToken } from './token'

export type FeedbackRating = 'up' | 'down'

export interface FeedbackInput {
  sessionId: string
  rating: FeedbackRating
  messageId?: string | null
  traceId?: string | null
  reason?: string
}

/** 探活成功缓存（模块级）：后端 flag 是进程级配置，一次成功即可长期信任；
 * 失败（404/网络异常）不缓存——后端中途开 flag 后，下次组件挂载重探即可恢复。 */
let probedFeedbackEnabled = false

/** 组装与 submitFeedback 同款的鉴权头（apiKey + Bearer token，有才带）。 */
function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { ...apiKeyHeader() }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/**
 * 探活反馈端点：GET /api/v1/feedback/status。
 * flag-on → 200 {"enabled": true}；flag-off → 404；网络异常 → catch。
 * 任一非 200 一律 fail-open 返回 false（反馈按钮不渲染 = 与基线等价）。
 */
export async function probeFeedbackEnabled(): Promise<boolean> {
  if (probedFeedbackEnabled) return true
  try {
    const resp = await fetch(`${API_BASE_URL}/api/v1/feedback/status`, {
      headers: authHeaders(),
    })
    probedFeedbackEnabled = resp.ok
  } catch {
    // 网络异常：fail-open（不缓存，下次挂载重探）
    return false
  }
  return probedFeedbackEnabled
}

/**
 * 提交一条消息反馈。
 * @returns true = 已入队（2xx）；false = 未送达（404/网络等，调用方静默处理）。
 */
export async function submitFeedback(input: FeedbackInput): Promise<boolean> {
  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...authHeaders(),
    }

    const resp = await fetch(`${API_BASE_URL}/api/v1/feedback`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        session_id: input.sessionId,
        rating: input.rating,
        message_id: input.messageId ?? undefined,
        trace_id: input.traceId ?? undefined,
        reason: input.reason || undefined,
      }),
    })
    return resp.ok
  } catch {
    // 网络异常同样静默：反馈失败不影响聊天
    return false
  }
}
