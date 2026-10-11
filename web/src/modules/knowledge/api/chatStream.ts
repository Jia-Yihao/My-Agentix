import { readSseData } from '../../../api/sse'

/** progress.detail.stages：R-P1-5 检索四阶段（rewrite/sparse/rrf/rerank 等）观测项 */
export type ProgressStageItem = {
  stage?: string
  ms?: number
  hits?: number
  skipped?: string | number
}

export type ChatStreamPayload = {
  event?: string
  /** 旧服务端双写期遗留词（A5/R5b 归一后新帧只发 type；读取仍容错两词） */
  type?: string
  content?: string
  finished?: boolean
  conversation_id?: string
  code?: string
  citations?: unknown
  /** finished 事件携带：答案 id（X3 过程面板懒加载 Explanation API 用） */
  answer_id?: string
  /** progress event: current processing stage */
  stage?: string
  message?: string
  detail?: { step?: number; total?: number; stages?: ProgressStageItem[] }
  /** meta 帧：请求对账 id（排障/与后端日志对齐） */
  request_id?: string
  /** meta 帧：用户提问消息 id */
  question_message_id?: string
  /** answer_chunk.finished：落库后的答案消息 id */
  message_id?: string
  /** finished 帧：agent 按策略提前终止的原因（预算/轮次上限等） */
  terminate_reason?: string
  /** finished 帧（W0 注入）：端到端耗时（毫秒） */
  duration_ms?: number
  /** finished 帧（W0 注入）：首字延迟（毫秒） */
  ttft_ms?: number
  /** finished 帧（W0 注入，flag 开）：分段耗时 */
  breakdown_ms?: { stream_ms?: number; ttft_ms?: number; retrieval_ms?: number }
}

type StreamCallbacks = {
  onPayload: (p: ChatStreamPayload) => void | Promise<void>
}

/**
 * 读取后端 SSE 的 chat stream。
 * - 字节→帧→data 行的机械 framing 收口到共享层（A5/R5c，api/sse.ts）；
 * - 自动容错 JSON.parse（坏包直接跳过）；
 * - 词汇归一：type-only 帧回填 event（A5 后服务端只发 type，本域消费方
 *   统一读 event，见 chatStreamHandlers / traceTimeline）；
 * - 不在这里做业务判断（meta/error/finished 的语义由调用方决定）。
 */
export async function readChatSse(
  res: Response,
  cb: StreamCallbacks,
): Promise<void> {
  await readSseData(res, (dataLine) => {
    let payload: ChatStreamPayload | null = null
    try {
      payload = JSON.parse(dataLine) as ChatStreamPayload
    } catch {
      return
    }
    // 词汇归一：event 缺失（A5 归一后新帧）回退 type；冲突时 event 优先
    if (payload && payload.event === undefined && payload.type !== undefined) {
      payload.event = payload.type
    }
    return cb.onPayload(payload)
  })
}
