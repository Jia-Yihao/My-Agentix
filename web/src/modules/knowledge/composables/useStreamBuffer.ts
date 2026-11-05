import type { ChatStreamPayload } from '../api/chatStream'

/**
 * 流式批渲染缓冲。
 *
 * SSE 每帧同步 mutate 深层响应式状态 ⇒ 每 token 触发一次完整重渲染
 * （高峰 ~50–80 次/s）。本缓冲把 token/progress 类帧聚到 **50ms 窗口**，
 * 到期一次性按序应用 —— 渲染次数降到 ~20/s；终帧（finished/error）与
 * abort 立即 flush（部分回答不丢、终态语义不延迟）。
 *
 * 关键契约：
 * - **顺序恒等**：flush 按入队原序同步调用 `apply`（与逐帧应用的结果一致）；
 * - 终帧 flush 在终帧处理**之前**（先落缓冲 token，再做 finished 收口）；
 * - `push` 不触发渲染，`flush` 在同一同步块内应用全部缓冲（Vue 合并为一次渲染）。
 */

export const STREAM_BUFFER_WINDOW_MS = 50

/** 可缓冲帧：token / 思考 / 工具活动 / 进度（终态与控制帧不进缓冲） */
export function isBufferablePayload(p: ChatStreamPayload): boolean {
  const ev = p.event
  if (ev === 'progress' || ev === 'thinking_chunk' || ev === 'tool_running') return true
  if (ev === 'answer_chunk' || ev === undefined || ev === '') {
    return Boolean(p.content)
  }
  return false
}

export type StreamBufferOptions = {
  /** 窗口时长（毫秒），默认 50 */
  windowMs?: number
  /** 缓冲到期/强制 flush 时按序应用的回调（同一同步块内执行） */
  apply: (payload: ChatStreamPayload) => void
  /** 每次 flush 后通知（渲染后处理：吸底/节流等），一窗最多一次 */
  onFlush?: () => void
}

export function useStreamBuffer(options: StreamBufferOptions) {
  const windowMs = options.windowMs ?? STREAM_BUFFER_WINDOW_MS
  let queue: ChatStreamPayload[] = []
  let timer: ReturnType<typeof setTimeout> | null = null

  const flush = () => {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    if (queue.length === 0) return
    const batch = queue
    queue = []
    for (const payload of batch) {
      options.apply(payload)
    }
    options.onFlush?.()
  }

  const schedule = () => {
    if (timer !== null) return
    timer = setTimeout(() => {
      timer = null
      flush()
    }, windowMs)
  }

  const push = (payload: ChatStreamPayload) => {
    queue.push(payload)
    schedule()
  }

  /** 入队并立即 flush（终帧前的强制落盘——先缓冲后终态，顺序保真） */
  const pushNow = (payload: ChatStreamPayload) => {
    flush()
    options.apply(payload)
    options.onFlush?.()
  }

  const dispose = () => {
    flush()
  }

  return { push, pushNow, flush, dispose }
}
