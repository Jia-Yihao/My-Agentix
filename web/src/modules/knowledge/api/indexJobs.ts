import { http } from './http'

export type IndexJobSummary = {
  id: string
  kb_id?: string
  status: string
  progress: number
  error_message?: string | null
}

/** 从 API 响应体解析 IndexJob（兼容 axios 的 data 包装） */
export function unwrapIndexJobPayload(body: unknown): IndexJobSummary | null {
  if (!body || typeof body !== 'object') return null
  const root = body as Record<string, unknown>
  const data = root.data
  if (data && typeof data === 'object') {
    return data as IndexJobSummary
  }
  if (typeof root.id === 'string' && typeof root.status === 'string') {
    return root as IndexJobSummary
  }
  return null
}

export function parseIndexJobProgress(progress: unknown): number {
  if (typeof progress === 'number' && Number.isFinite(progress)) {
    return Math.max(0, Math.min(100, Math.round(progress)))
  }
  if (typeof progress === 'string' && progress.trim() !== '') {
    const n = Number(progress)
    if (Number.isFinite(n)) return Math.max(0, Math.min(100, Math.round(n)))
  }
  return 0
}

export async function getIndexJob(jobId: string) {
  return await http.get<{ data?: IndexJobSummary }>(`/index-jobs/${jobId}`)
}

export async function getDocumentIndexJob(documentId: string) {
  return await http.get<{ data?: IndexJobSummary }>(
    `/index-jobs/documents/${documentId}/index-job`,
  )
}

/**
 * 重试索引任务（复用原 job，不新建）。
 * 后端仅接受「已失败」或「已判定为僵尸（长时间无心跳）」的任务，其余情况返回校验错误。
 */
export async function retryIndexJob(jobId: string) {
  return await http.post<{ data?: IndexJobSummary }>(`/index-jobs/${jobId}/retry`)
}

export type IndexJobTimingConfig = {
  job_timeout_seconds: number
  stale_queued_seconds: number
  stale_running_seconds: number
  /** 前端「停留过久可重试」提示阈值：后端僵尸判定线 + 缓冲 */
  stall_hint_seconds: number
}

/** 索引任务时序配置（生效值）：前端据此计算「任务停滞」提示，避免写死阈值后与后端漂移 */
export async function getIndexJobTimingConfig() {
  return await http.get<{ data?: IndexJobTimingConfig }>('/index-jobs/timing-config')
}
