import { ref } from 'vue'

import { getToken } from '../auth/token'
import { CHAT_STREAM_URL } from '../api/chat'
import { apiKeyHeader } from '../api/config'
import { readChatSse, type ChatStreamPayload } from '../api/chatStream'
import {
  applyChatStreamToAssistant,
  finalizeAssistantStream,
} from '../api/chatStreamHandlers'
import type { ChatCitation, StreamableMessage } from '../api/chatTypes'
import { applyFetchUnauthorized } from '../auth/session'
import { isAbortError } from '../utils/errors'
import { appendTraceEvent } from '../utils/traceTimeline'
import { isBufferablePayload, useStreamBuffer } from './useStreamBuffer'

export type { StreamableMessage } from '../api/chatTypes'

export type StreamChatResult = 'ok' | 'unauthorized' | 'failed'

export type StreamChatOptions = {
  body: Record<string, unknown>
  signal?: AbortSignal
  assistantMsgId: string
  getAssistantMsg: () => StreamableMessage | undefined
  onUnauthorized?: () => void
  /** 首个服务端帧到达（用于区分「已应答后失败」与「未触达业务」——后者才允许重试/兜底） */
  onFirstPayload?: () => void
  onMeta?: (payload: ChatStreamPayload) => void
  onError?: (message: string, payload: ChatStreamPayload) => void | 'handled'
  onChunk?: () => void
  onFinished?: (payload: ChatStreamPayload) => void
  /** false 时在 HTTP/SSE 错误处 throw，由调用方 catch */
  inlineError?: boolean
}

/**
 * HTTP 错误体 → 用户可读文案（接口审查批次 2026-09-28 对齐 chat 模块
 * `extractChatError` 的形状覆盖）：原实现只读 `user_hint || message`，
 * FastAPI 422 的 `{detail:[{msg}]}` 恒落兜底"请求失败"，超长提问（>5000）
 * 这类可预期错误对用户完全不可诊断。
 */
export async function parseHttpError(res: Response): Promise<string> {
  const fallback = '请求失败'
  try {
    const data = await res.json()
    if (data && typeof data === 'object') {
      const pick = (obj: unknown, key: string): string | undefined => {
        if (!obj || typeof obj !== 'object') return undefined
        const v = (obj as Record<string, unknown>)[key]
        return typeof v === 'string' && v.trim() ? v : undefined
      }
      const hint = pick(data, 'user_hint') ?? pick(data, 'message')
      if (hint) return hint
      const detail = (data as Record<string, unknown>).detail
      if (typeof detail === 'string' && detail.trim()) return detail
      if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        const msg = pick(detail, 'message') ?? pick(detail, 'msg')
        if (msg) return msg
      }
      // 422 校验错误：detail 为数组
      if (Array.isArray(detail) && detail.length > 0) {
        const msg = pick(detail[0], 'msg') ?? pick(detail[0], 'message')
        if (msg) return `参数校验失败：${msg}`
      }
      const plain = pick(data, 'error')
      if (plain) return plain
    }
  } catch {
    /* 响应体非 JSON → 用兜底文案 */
  }
  return fallback
}

/** 低层 SSE 聊天流：供 composable 与 modal retry 复用。 */
export async function streamChatResponse(options: StreamChatOptions): Promise<StreamChatResult> {
  const {
    body,
    signal,
    getAssistantMsg,
    onUnauthorized,
    onFirstPayload,
    onMeta,
    onError,
    onChunk,
    onFinished,
    inlineError = true,
  } = options

  // retrieval 聊天路由挂 get_current_user，必须带 Bearer JWT（原先缺失 → 401）
  const token = getToken()
  const res = await fetch(CHAT_STREAM_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...apiKeyHeader(),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    credentials: 'include',
    signal,
    body: JSON.stringify(body),
  })

  if (res.status === 401) {
    applyFetchUnauthorized()
    onUnauthorized?.()
    return 'unauthorized'
  }

  if (!res.ok || !res.body) {
    const hint = await parseHttpError(res)
    if (inlineError) {
      const aiMsg = getAssistantMsg()
      if (aiMsg) aiMsg.content = `提示：${hint}`
      return 'failed'
    }
    throw new Error(hint)
  }

  let done = false
  let firstPayload = false

  // W6 A1：token/progress 类帧进 50ms 批渲染缓冲（渲染 ~20/s 而非每 token）；
  // flush 与终态/控制帧在**同一同步块**内保序（缓冲先落、终态后处理）。
  let flushedContent = false
  const buffer = useStreamBuffer({
    apply: (payload) => {
      const aiMsg = getAssistantMsg()
      if (aiMsg) {
        applyChatStreamToAssistant(payload, aiMsg)
        if (payload.content) flushedContent = true
      }
    },
    onFlush: () => {
      if (flushedContent) {
        flushedContent = false
        onChunk?.()
      }
    },
  })

  try {
    await readChatSse(res, {
      onPayload: async (data: ChatStreamPayload) => {
        if (done) return
        if (!firstPayload) {
          firstPayload = true
          onFirstPayload?.()
        }

        // 执行轨迹：任意事件折叠进助手消息的时间线（客户端视角）
        const traceMsg = getAssistantMsg()
        if (traceMsg) {
          traceMsg.trace = appendTraceEvent(traceMsg.trace ?? [], data, Date.now())
        }

        // 任意事件只要带 conversation_id 就通知（meta / answer_chunk.finished 均可能携带）
        if (data.conversation_id) {
          onMeta?.(data)
        }

        if (data.event === 'meta') {
          // B3：request_id 落到助手消息上（反馈 trace_id 归因列的数据源）
          const metaMsg = getAssistantMsg()
          if (metaMsg && data.request_id) metaMsg.trace_id = String(data.request_id)
          return
        }

        if (data.event === 'error' || data.code) {
          buffer.flush() // 错误覆盖正文前先落缓冲（部分回答不丢）
          const message = String(data.content || '处理失败')
          if (onError?.(message, data) === 'handled') {
            done = true
            return
          }
          if (inlineError) {
            const aiMsg = getAssistantMsg()
            if (aiMsg) aiMsg.content = `提示：${message}`
            done = true
            return
          }
          throw new Error(message)
        }

        if (data.content || data.event === 'progress') {
          if (isBufferablePayload(data)) {
            buffer.push(data)
          } else {
            buffer.flush()
            const aiMsg = getAssistantMsg()
            if (aiMsg) {
              applyChatStreamToAssistant(data, aiMsg)
              onChunk?.()
            }
          }
        }

        if (data.finished) {
          buffer.flush() // 收口前先落全部缓冲 token（顺序保真）
          const aiMsg = getAssistantMsg()
          if (aiMsg && data.citations) {
            aiMsg.citations = data.citations as ChatCitation[]
          }
          // X3 过程面板：捕获答案 id（有值时消息下方渲染"过程"折叠面板）
          if (aiMsg && data.answer_id) {
            aiMsg.answer_id = String(data.answer_id)
          }
          // 提前终止原因上屏（否则用户会误认为模型已答完）
          if (aiMsg && data.terminate_reason) {
            const note = `（已按策略提前终止：${data.terminate_reason}）`
            if (!aiMsg.content.includes(note)) {
              aiMsg.content = `${aiMsg.content}\n\n${note}`
            }
          }
          // W6 B4/B5/B8：stepper 收起、工具卡片折叠、耗时芯片落位
          if (aiMsg) {
            finalizeAssistantStream(data, aiMsg)
          }
          onFinished?.(data)
          done = true
        }
      },
    })
  } finally {
    // abort / 读取异常路径也落缓冲（用户停止后保留部分回答）
    buffer.flush()
  }

  if (!done) {
    // 流结束但没有终态帧（error / finished 均会置 done）：连接被中途截断。
    // 原实现恒返回 'ok' → 半截回答被当完成，modal 还会走「重试/非流式兜底」
    // 造成同一问题二次执行。现诚实标注并返回 'failed'（正文不覆盖、只追加）。
    const aiMsg = getAssistantMsg()
    if (aiMsg) {
      aiMsg.content = aiMsg.content
        ? `${aiMsg.content}\n\n（连接中断，以上为已生成的部分回答）`
        : '提示：连接中断，未收到完整回答'
      // 截断时无终态帧：手动收口 stepper / 工具卡片（不冒充完成态 timing）
      aiMsg.progress = null
      aiMsg.toolActivity = null
    }
    return 'failed'
  }
  return 'ok'
}

export function useChatStream() {
  const isStreaming = ref(false)
  const streamingAssistantId = ref<string | null>(null)
  let streamCtrl: AbortController | null = null

  const abort = () => {
    if (streamCtrl) {
      streamCtrl.abort()
      streamCtrl = null
    }
  }

  async function sendChatStream(options: Omit<StreamChatOptions, 'signal'>): Promise<StreamChatResult> {
    abort()
    const ctrl = new AbortController()
    streamCtrl = ctrl
    isStreaming.value = true
    streamingAssistantId.value = options.assistantMsgId

    try {
      return await streamChatResponse({
        ...options,
        signal: ctrl.signal,
      })
    } catch (e) {
      if (isAbortError(e)) return 'failed'
      if (options.inlineError !== false) {
        const aiMsg = options.getAssistantMsg()
        if (aiMsg) {
          aiMsg.content = e instanceof Error ? `提示：${e.message}` : '提示：请求失败，请稍后重试'
        }
        return 'failed'
      }
      throw e
    } finally {
      // 若本请求已被新的 sendChatStream 接管（streamCtrl 是别的 controller），
      // 不能清理全局状态——否则会把新请求的 isStreaming/streamCtrl 清掉，
      // 导致新流无法 abort、进度指示提前消失。
      // 注意：不能在这里 return（finally 的 return 会吞掉 try 的返回值）。
      const superseded = streamCtrl !== null && streamCtrl !== ctrl
      if (!superseded) {
        isStreaming.value = false
        streamingAssistantId.value = null
        if (streamCtrl === ctrl) streamCtrl = null
      }
    }
  }

  return {
    isStreaming,
    streamingAssistantId,
    sendChatStream,
    abort,
  }
}
