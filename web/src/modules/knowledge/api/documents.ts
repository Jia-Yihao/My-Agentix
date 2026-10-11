import type { AxiosProgressEvent, AxiosResponse } from 'axios'
import { ref } from 'vue'
import { calcHttpUploadPercent } from '../utils/kbUploadProgress'
import { http } from './http'

function formDataUploadBytes(formData: FormData): number {
  let sum = 0
  for (const value of formData.values()) {
    if (value instanceof File) sum += value.size
  }
  return sum
}

export type DocumentSummary = {
  id: string
  title: string
  status: string
  created_at: string
  updated_at?: string
  folder_id?: string | null
  /** 服务端归一化目录（如 `资料/2023`）；与 folder_id 对应的路径缓存 */
  folder_path?: string | null
  has_thumbnail?: boolean
}

/**
 * 缩略图对象 URL 缓存（B1 修复）：后端缩略图路由要求 Bearer 鉴权
 * （`GET /documents/{id}/thumbnail` → get_current_user），而 `<img src>`
 * 无法携带 Authorization 头、`AUTH_COOKIE_SESSION_FALLBACK` 默认关——
 * 直接把接口 URL 给 img 必 401。改为经 axios（自动带鉴权头）拉 blob，
 * `URL.createObjectURL` 生成 blob: 地址给 img；同步取值 + 异步填充
 * （渲染期读取响应式 map 注册依赖，URL 就绪后自动重渲染）。
 */
const thumbnailUrls = ref<Record<string, string>>({})
const thumbnailInflight = new Set<string>()

/** 取缩略图 src：未就绪时返回空串（组件回退文件图标），拉取失败静默（同回退）。 */
export function getDocumentThumbnailUrl(docId: string): string {
  if (!thumbnailUrls.value[docId] && !thumbnailInflight.has(docId)) {
    thumbnailInflight.add(docId)
    http
      .get(`/documents/${docId}/thumbnail`, { responseType: 'blob' })
      .then((res) => {
        thumbnailUrls.value = {
          ...thumbnailUrls.value,
          [docId]: URL.createObjectURL(res.data as Blob),
        }
      })
      .catch(() => {
        // 401/404/无缩略图：静默失败，组件按空串回退文件图标（与原 @error 语义一致）
      })
      .finally(() => {
        thumbnailInflight.delete(docId)
      })
  }
  return thumbnailUrls.value[docId] ?? ''
}

/** 回收不再展示的缩略图 object URL（列表刷新/组件卸载时调用，防内存泄漏）。 */
export function releaseDocumentThumbnails(keepIds?: ReadonlySet<string>): void {
  for (const [docId, url] of Object.entries(thumbnailUrls.value)) {
    if (keepIds?.has(docId)) continue
    URL.revokeObjectURL(url)
    const next = { ...thumbnailUrls.value }
    delete next[docId]
    thumbnailUrls.value = next
  }
}

/**
 * PDF 阅读器 blob URL（与缩略图同款 B1 问题）：`GET /documents/{id}/file`
 * 要求 Bearer 鉴权，iframe src 带不了 → 默认配置下阅读器整页 401。
 * 经 axios 拉全量 blob（复用 downloadDocumentBlob 鉴权通道）→ objectURL。
 * 阅读器同一时刻只展示一篇，缓存保持单条：换文档即回收旧 URL。
 */
const readerBlobUrl = ref<{ docId: string; url: string } | null>(null)
const readerInflight = new Set<string>()

export function getDocumentReaderUrl(docId: string): string {
  if (readerBlobUrl.value?.docId === docId) return readerBlobUrl.value.url
  if (!readerInflight.has(docId)) {
    readerInflight.add(docId)
    if (readerBlobUrl.value) {
      // 换文档：先回收上一本的 object URL（大 PDF 不可久留内存）
      URL.revokeObjectURL(readerBlobUrl.value.url)
      readerBlobUrl.value = null
    }
    downloadDocumentBlob(docId)
      .then((res) => {
        readerBlobUrl.value = { docId, url: URL.createObjectURL(res.data as Blob) }
      })
      .catch(() => {
        // 拉取失败：返回空串 → 阅读器空白帧（不弹登录错误页）
      })
      .finally(() => {
        readerInflight.delete(docId)
      })
  }
  return readerBlobUrl.value?.docId === docId ? readerBlobUrl.value.url : ''
}

export function releaseDocumentReaderUrl(): void {
  if (readerBlobUrl.value) {
    URL.revokeObjectURL(readerBlobUrl.value.url)
    readerBlobUrl.value = null
  }
}

/**
 * 拉取知识库全部文档（B2 修复）：后端分页默认 page_size=20 且上限 100，
 * 原实现不传分页参数 → 超 20 个文档静默截断且无翻页入口。现按上限
 * 100/页循环拉全（以返回的 total 为停止条件，上限 100 页防御异常信封）。
 */
export async function listKbDocuments(kbId: string): Promise<DocumentSummary[]> {
  const pageSize = 100
  const all: DocumentSummary[] = []
  for (let page = 1; page <= 100; page++) {
    const res = await http.get(`/knowledge-bases/${kbId}/documents`, {
      params: { page, page_size: pageSize },
    })
    const items = (res.data?.data?.items || []) as DocumentSummary[]
    all.push(...items)
    const total = Number(res.data?.data?.total ?? all.length)
    if (items.length < pageSize || all.length >= total) break
  }
  return all
}

export async function getDocument(docId: string) {
  return await http.get<{ data?: DocumentSummary }>(`/documents/${docId}`)
}

export async function renameDocument(docId: string, title: string) {
  return await http.patch(`/documents/${docId}`, { title })
}

export async function deleteDocument(docId: string) {
  return await http.delete(`/documents/${docId}`)
}

export async function downloadDocumentBlob(docId: string) {
  return await http.get(`/documents/${docId}/file`, { responseType: 'blob' })
}

export async function uploadDocument(
  formData: FormData,
  opts: {
    params: Record<string, string>
    onUploadProgress?: (pct: number) => void
    signal?: AbortSignal
  },
) {
  const fileSize = formDataUploadBytes(formData)
  const onUploadProgress = opts.onUploadProgress
    ? (evt: AxiosProgressEvent) => {
        opts.onUploadProgress?.(calcHttpUploadPercent(evt, fileSize))
      }
    : undefined

  return await http.post<unknown>('/documents', formData, {
    params: opts.params,
    onUploadProgress,
    signal: opts.signal,
  })
}

/** 批量上传响应载荷（R4.4 类型收敛：显式化 batch 响应结构） */
export type BatchPayload = {
  job_id?: string
  items?: Array<{ document_id?: string }>
}

/** 批量上传 PDF（multipart：多个 files 字段 + 可选 relative_paths 表单 JSON 数组） */
export async function uploadDocumentsBatch(
  kbId: string,
  formData: FormData,
  opts?: { params?: Record<string, string>; onUploadProgress?: (pct: number) => void },
) {
  const fileSize = formDataUploadBytes(formData)
  const onUploadProgress = opts?.onUploadProgress
    ? (evt: AxiosProgressEvent) => {
        opts?.onUploadProgress?.(calcHttpUploadPercent(evt, fileSize))
      }
    : undefined

  return await http.post<{ data?: BatchPayload }>(`/knowledge-bases/${kbId}/documents/batch`, formData, {
    params: opts?.params,
    onUploadProgress,
  })
}

export type ChatPostEnvelope = {
  data?: {
    answer?: string
    citations?: Array<unknown>
    conversation_id?: string
    /** 后端非流式回答携带的答案 id（过程面板用） */
    answer_id?: string
  }
}

export async function postChat(body: {
  kb_id: string
  question: string
  conversation_id?: string
  document_id?: string
  folder_id?: string
  chat_mode?: 'fast' | 'deep'
}): Promise<AxiosResponse<ChatPostEnvelope>> {
  return await http.post('/chat', body)
}
