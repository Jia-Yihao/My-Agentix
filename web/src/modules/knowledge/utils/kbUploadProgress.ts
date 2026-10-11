import { parseIndexJobProgress } from '../api/indexJobs'

/** 浏览列表仅展示已可检索的文档 */
export function isReadyDocument(doc: { status: string }): boolean {
  return doc.status === 'ready'
}

/** 进页后需纳入进度面板的文档 */
export function isPendingUploadDocument(doc: { status: string }): boolean {
  return doc.status === 'draft' || doc.status === 'indexing' || doc.status === 'failed'
}

export const INDEX_FAILURE_MESSAGE = '处理失败'

export type RestoreDocInput = {
  id: string
  title: string
  status: string
  folder_path?: string | null
}

export type RestoreJobInput = {
  id: string
  status?: string
  progress?: number
  error_message?: string | null
} | null

export type RestoreTaskDraft = {
  id: string
  fileName: string
  uploadProgress: number
  indexProgress: number
  status: 'indexing' | 'failed'
  documentId: string
  jobId?: string
  errorMessage?: string
}

function clampPercent(n: number): number {
  return Math.max(0, Math.min(100, Math.round(n)))
}

/** 从文档 + 可选 index job 生成恢复用进度项 */
export function buildRestoreTaskFromDocument(
  doc: RestoreDocInput,
  job: RestoreJobInput,
): RestoreTaskDraft {
  const fileName = displayFileName(doc)
  const base = {
    id: `restore_${doc.id}`,
    fileName,
    uploadProgress: 100,
    documentId: doc.id,
  }

  if (doc.status === 'failed') {
    return {
      ...base,
      jobId: job?.id,
      indexProgress: 0,
      status: 'failed',
      errorMessage: INDEX_FAILURE_MESSAGE,
    }
  }

  const jobStatus = job?.status
  const progress = parseIndexJobProgress(job?.progress)

  // Job 失败但文档仍在处理：以文档为准，继续轮询
  if (jobStatus === 'failed' && (doc.status === 'indexing' || doc.status === 'draft')) {
    return {
      ...base,
      jobId: job?.id,
      indexProgress: progress,
      status: 'indexing',
    }
  }

  if (jobStatus === 'failed') {
    return {
      ...base,
      jobId: job?.id,
      indexProgress: progress,
      status: 'failed',
      errorMessage: INDEX_FAILURE_MESSAGE,
    }
  }

  return {
    ...base,
    jobId: job?.id,
    indexProgress: progress,
    status: 'indexing',
  }
}

export function displayFileName(doc: Pick<RestoreDocInput, 'title' | 'folder_path'>): string {
  const fp = (doc.folder_path ?? '').trim()
  if (fp) return doc.title
  const t = (doc.title || '').replace(/\\/g, '/')
  if (t.includes('/')) return t.split('/').pop() || doc.title
  return doc.title
}

/** 合并恢复任务，同 documentId 以 incoming 为准 */
export function mergeRestoreUploadTasks<T extends { documentId?: string }>(
  existing: T[],
  incoming: T[],
): T[] {
  const incomingDocIds = new Set(
    incoming.map((t) => t.documentId).filter((id): id is string => !!id),
  )
  const kept = existing.filter((t) => !t.documentId || !incomingDocIds.has(t.documentId))
  return [...kept, ...incoming]
}

export type UploadProgressTaskView = {
  status: 'uploading' | 'indexing' | 'succeeded' | 'failed'
  uploadProgress: number
  indexProgress: number
  jobId?: string
  documentId?: string
}

/** 是否仍处于 HTTP 上传阶段（未拿到 job 且字节未传完） */
export function isHttpUploadPhase(task: UploadProgressTaskView): boolean {
  return (
    task.status === 'uploading' &&
    task.uploadProgress < 100 &&
    task.indexProgress <= 0 &&
    !task.jobId &&
    !task.documentId
  )
}

export function uploadTaskStatusLabel(task: UploadProgressTaskView): string {
  if (task.status === 'failed') return INDEX_FAILURE_MESSAGE
  if (task.status === 'succeeded') return '完成'
  if (isHttpUploadPhase(task)) return '上传中'
  return '处理中'
}

export function canCancelUploadTask(status: 'uploading' | 'indexing' | 'succeeded' | 'failed'): boolean {
  return status === 'uploading' || status === 'indexing' || status === 'failed'
}

/** 「停留过久」提示文案；出现该提示即表示后端已可把此任务判为僵尸，重试接口会接受 */
export const INDEX_STALL_HINT = '索引任务长时间未完成，可点击重试或稍后查看'

/**
 * 是否可对该任务发起重试。
 * 后端 retry 只接受「已失败」或「已判定僵尸」的任务，且复用原 job。
 * 没有 jobId 但有 documentId 的任务（如恢复时拉 job 失败）也允许：点击重试时会按文档
 * 补拉一次 latest job，拉到再真正发起重试。
 * 处理中的任务不主动暴露重试入口（避免用户点到一个必然 400 的按钮），
 * 仅在出现「停留过久」提示时才允许。
 */
export function canRetryUploadTask(task: {
  status: string
  jobId?: string
  documentId?: string
  errorMessage?: string
}): boolean {
  if (!task.jobId && !task.documentId) return false
  if (task.status === 'failed') return true
  return task.status === 'indexing' && task.errorMessage === INDEX_STALL_HINT
}

/** 上传字节阶段用 uploadProgress；一旦进入索引（有 job/索引进度）用 indexProgress */
export function displayTaskPercent(task: UploadProgressTaskView): number {
  if (isHttpUploadPhase(task)) {
    return clampPercent(task.uploadProgress)
  }
  const raw = clampPercent(task.indexProgress)
  if (task.status === 'failed' && raw >= 100) return 99
  return raw
}

/** axios 上传进度：total 缺失时用 file.size 估算 */
export function calcHttpUploadPercent(
  evt: { loaded?: number; total?: number },
  fileSize: number,
): number {
  const loaded = evt.loaded ?? 0
  const total =
    evt.total && evt.total > 0 ? evt.total : fileSize > 0 ? fileSize : 0
  if (total <= 0) return loaded > 0 ? 99 : 0
  return Math.min(100, Math.round((loaded / total) * 100))
}
