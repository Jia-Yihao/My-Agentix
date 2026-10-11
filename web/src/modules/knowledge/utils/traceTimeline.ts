import type { ChatStreamPayload } from '../api/chatStream'

/** 执行轨迹步骤（客户端视角：事件序列 + 到达时刻，间隔在渲染时计算） */
export type TraceStep = {
  kind: 'stage' | 'tool' | 'answer' | 'done' | 'error'
  label: string
  at: number
}

/**
 * 把一条 SSE 载荷折叠进执行轨迹（纯函数：返回同一数组引用便于原地响应式更新）。
 *
 * 折叠规则（去抖：相邻同 kind+label 的事件只记一次——tool_running 会随
 * 状态更新重复触发，逐 token 的 answer_chunk 只在首次入列）：
 * - `meta`                → stage「会话与检索就绪」
 * - `progress`            → stage（label = 阶段文案，相邻同文案去重）
 * - `tool_running`        → tool（label = 后端工具运行标签）
 * - `answer_chunk` 首次   → answer「生成回答」
 * - `finished`            → done「回答完成」
 * - `error` / code        → error
 */
export function appendTraceEvent(
  steps: TraceStep[],
  data: ChatStreamPayload,
  now: number,
): TraceStep[] {
  const push = (kind: TraceStep['kind'], label: string): void => {
    const last = steps[steps.length - 1]
    if (last && last.kind === kind && last.label === label) return
    steps.push({ kind, label, at: now })
  }

  if (data.event === 'meta') {
    push('stage', '会话与检索就绪')
    return steps
  }
  if (data.event === 'progress') {
    push('stage', String(data.message || data.stage || '处理中'))
    return steps
  }
  if (data.event === 'tool_running') {
    push('tool', String(data.content || '工具执行'))
    return steps
  }
  if (data.event === 'error' || data.code) {
    push('error', String(data.content || '处理失败'))
    return steps
  }
  if (data.event === 'answer_chunk' && data.content) {
    const started = steps.some((s) => s.kind === 'answer')
    if (!started) push('answer', '生成回答')
    return steps
  }
  if (data.finished) {
    push('done', '回答完成')
  }
  return steps
}

/** 步骤间间隔的展示文本（首步无间隔）。 */
export function traceGapLabel(steps: TraceStep[], index: number): string {
  if (index <= 0) return ''
  const gap = steps[index].at - steps[index - 1].at
  if (gap < 1000) return `${gap}ms`
  return `${(gap / 1000).toFixed(1)}s`
}

export const TRACE_KIND_LABELS: Record<TraceStep['kind'], string> = {
  stage: '阶段',
  tool: '工具',
  answer: '生成',
  done: '完成',
  error: '异常',
}
