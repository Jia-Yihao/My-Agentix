/**
 * 向量库管理 API
 */
import { http } from './http'

export type VectorDbDoc = {
  id: string
  title: string
  kb_id: string
  status: string
  size: number
  chunk_count: number
  category: string
  tags: string[]
  strategy_detected: string
  created_at: string
}

export type VectorDbStats = {
  total_documents: number
  total_chunks: number
  collections: number
  backend: string
}

export async function fetchVectorDbStats(): Promise<VectorDbStats> {
  const { data } = await http.get('/vector-db/stats')
  return data.data
}

export async function fetchVectorDbDocuments(params?: {
  search?: string
  category?: string
  kb_id?: string
  page?: number
  page_size?: number
}): Promise<{ data: VectorDbDoc[]; total: number; page: number }> {
  const { data } = await http.get('/vector-db/documents', { params })
  return data
}

export async function deleteVectorDbDocument(docId: string): Promise<void> {
  await http.delete(`/vector-db/documents/${docId}`)
}

/**
 * 单篇文档的分类结果。
 *
 * 形状对齐后端 `_classify_document`（backend/tracerag/api/routers/vector_db.py:51），
 * 它有两条返回分支：
 *   - 空文档：category / tags / confidence
 *   - 正常文档：category / tags / strategy_detected / chunk_count / total_chars
 * 因此后四个字段为可选。
 */
export type VectorDbClassification = {
  category: string
  tags: string[]
  strategy_detected?: string
  chunk_count?: number
  total_chars?: number
  confidence?: number
}

/** 返回按 document_id 索引的分类结果映射。 */
export async function classifyDocuments(
  docIds: string[],
): Promise<Record<string, VectorDbClassification>> {
  const { data } = await http.post('/vector-db/classify', docIds)
  return data.data
}
