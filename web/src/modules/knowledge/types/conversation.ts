export type ConversationItem = {
  id: string
  title: string
  updated_at: string
  kb_id?: string
  scope_type?: 'kb' | 'folder' | 'file'
  scope_id?: string | null
}

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  citations?: import('../api/chatTypes').ChatCitation[]
  created_at: string
  /** 流式期间的处理进度（历史接口不回放该字段） */
  progress?: import('../api/chatTypes').StreamableMessage['progress']
  /** 过程面板答案 id（历史接口 MessageData 会回放） */
  answer_id?: string | null
  /** 请求追踪 id（meta 帧携带；反馈 trace_id 归因用，历史回放可能缺失） */
  trace_id?: string | null
  /** 执行轨迹时间线（流式过程实时填充） */
  trace?: import('../utils/traceTimeline').TraceStep[]
  /** 工具活动卡片（W6 B5）：流式实时活动行 / 终帧折叠历史 */
  toolActivity?: import('../api/chatStreamHandlers').StreamToolActivity | null
  toolHistory?: string[]
  /** 终帧耗时芯片（W6 B8，W0 注入 duration_ms/ttft_ms） */
  timing?: import('../api/chatStreamHandlers').StreamTiming | null
}
