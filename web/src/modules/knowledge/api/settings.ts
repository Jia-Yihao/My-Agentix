/**
 * RAG Settings API — 模型配置
 */
import { http } from './http'

export type RagConfig = {
  llm_provider: string
  llm_base_url: string
  llm_model: string
  embedding_base_url: string
  embedding_model: string
  chunk_strategy: string
  chunk_size: number
  chunk_overlap: number
  search_k: number
}

export async function fetchKbSettings(kbId: string): Promise<RagConfig> {
  const { data } = await http.get(`/settings/${kbId}`)
  return data.data
}

export async function updateKbSettings(kbId: string, config: Partial<RagConfig>): Promise<RagConfig> {
  const { data } = await http.put(`/settings/${kbId}`, config)
  return data.data
}
