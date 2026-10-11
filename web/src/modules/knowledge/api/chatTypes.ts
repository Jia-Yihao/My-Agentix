import type { TraceStep } from '../utils/traceTimeline'

export type ChatCitation = {
  chunk_id: string
  document_id?: string
  page_no?: number
  snippet: string
  source?: string
  /** 文档标题（Document.title） */
  document_title?: string | null
  /** 上传时的原始文件名；取不到时后端回退文档标题 */
  file_name?: string | null
  /** 文档所在目录 */
  folder_path?: string | null
  /** 文件大小（字节） */
  file_size?: number | null
  /** 小写扩展名，不含点 */
  file_ext?: string | null
  /**
   * R2-2 法条效力状态：in_force（现行有效）/ expired（已失效）/
   * repealed（已废止）/ pending（未生效）；前端对未知值不渲染徽标。
   */
  effective_status?: string | null
  /** 被何法替代（expired/repealed 时的展示字段） */
  replaced_by?: string | null
}

export type StreamableMessage = {
  content: string
  thinking?: string
  citations?: ChatCitation[]
  /** 当前处理阶段进度信息 */
  progress?: { stage: string; message: string; step?: number; total?: number } | null
  /**
   * X3 过程面板：流式 finished 事件携带的答案 id；
   * 有值时消息下方渲染"过程"折叠面板（懒加载 Explanation API）。
   */
  answer_id?: string | null
  /**
   * 请求追踪 id（meta 帧携带）：满意度反馈 trace_id 归因列的数据源；
   * 历史回放/非流式兜底可能缺失（可选项，缺省不传反馈 trace_id）。
   */
  trace_id?: string | null
  /** 执行轨迹（客户端视角事件时间线）：流式过程中实时填充 */
  trace?: TraceStep[]
  /** W6 B5：进行中的工具（工具卡实时态，tool_running 帧驱动） */
  toolActivity?: import('../api/chatStreamHandlers').StreamToolActivity | null
  /** W6 B5：已完成工具历史（流式结束折叠入卡） */
  toolHistory?: string[]
  /** W6 B8：流式耗时指标（finished 帧 duration_ms/ttft_ms/breakdown_ms） */
  timing?: import('../api/chatStreamHandlers').StreamTiming | null
}

/** X2 Explanation API 响应（data 字段） */
export type AnswerExplanation = {
  answer: {
    id: string
    conversation_id: string
    question_message_id: string
    version: number
    is_current: boolean
    created_at: string | null
  }
  question: { message_id: string; content: string | null }
  model: { name: string; prompt_version: { status: string } }
  evidence:
    | { status: 'no_retrieval' }
    | {
        status: 'recorded'
        retrieval_run: {
          query_text: string
          top_k: number
          hit_count: number
          chunks: Array<{
            chunk_id: string | null
            score: number | null
            source: string | null
            effective_status: string | null
            content_preview: string
          }>
        }
      }
  citations: ChatCitation[]
  validation: Record<string, { status: string }>
  notes: string
}
