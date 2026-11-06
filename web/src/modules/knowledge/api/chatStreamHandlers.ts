import type { ChatStreamPayload, ProgressStageItem } from './chatStream'

export type StreamProgress = {
  stage: string
  message: string
  step?: number
  total?: number
  /** R-P1-5 检索阶段观测（rewrite/dense/sparse/fusion/rerank + ms/hits） */
  stages?: ProgressStageItem[]
}

export type StreamToolActivity = {
  /** 冻结工具文案（deep 模式每工具名只播一次） */
  label: string
}

export type StreamTiming = {
  durationMs?: number
  ttftMs?: number
}

export type StreamableAssistantMessage = {
  content: string
  thinking?: string
  /** Current processing stage info */
  progress?: StreamProgress | null
  /** 工具活动卡片（W6 B5）：不再污染思考区；finished 后折叠为历史 */
  toolActivity?: StreamToolActivity | null
  toolHistory?: string[]
  /** 终帧耗时（W6 B8，W0 注入 duration_ms/ttft_ms） */
  timing?: StreamTiming | null
}

// 批次 C3（audit §1 C3）：原 PROGRESS_STAGE_LABELS 死导出已删除——全仓无引用，
// stage 文案由后端 progress.message 直接渲染。

/** 将 SSE 载荷写入助手消息（区分思考过程与正文 / progress / 工具活动）。 */
export function applyChatStreamToAssistant(
  data: ChatStreamPayload,
  msg: StreamableAssistantMessage,
): void {
  const ev = data.event
  const chunk = data.content ?? ''
  if (!chunk && ev !== 'progress') return

  // progress event: show processing stage（W6 B4：detail.stages 透传，
  // 检索四阶段子步骤 + ms 徽标由 StreamStepper 消费）
  if (ev === 'progress') {
    msg.progress = {
      stage: data.stage || '',
      message: data.message || '',
      step: data.detail?.step,
      total: data.detail?.total,
      stages: data.detail?.stages,
    }
    return
  }

  if (ev === 'thinking_chunk') {
    msg.thinking = (msg.thinking ?? '') + String(chunk)
    return
  }

  // W6 B5：工具活动独立成卡片（原实现写进 thinking → 工具文案污染思考区；
  // 同一工具名后端已去重，活动行取最新一条即可）
  if (ev === 'tool_running') {
    msg.toolActivity = { label: String(chunk) }
    return
  }

  if (ev === 'answer_chunk' || ev === undefined || ev === '') {
    msg.content += String(chunk)
    // W6 B4 修复「首 token 即消失」：progress 不在正文出现时清空——
    // 生成阶段（llm_generate）的 stepper 需与正文并存保持到终帧。
  }
}

/** 终帧收口：stepper / 工具卡片收起，耗时芯片落位（W6 B4/B5/B8）。 */
export function finalizeAssistantStream(
  data: ChatStreamPayload,
  msg: StreamableAssistantMessage,
): void {
  msg.progress = null
  if (msg.toolActivity) {
    const label = msg.toolActivity.label
    const history = msg.toolHistory ?? []
    if (label && !history.includes(label)) history.push(label)
    msg.toolHistory = history
    msg.toolActivity = null
  }
  if (data.duration_ms != null || data.ttft_ms != null) {
    msg.timing = {
      durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : undefined,
      ttftMs: typeof data.ttft_ms === 'number' ? data.ttft_ms : undefined,
    }
  }
}
