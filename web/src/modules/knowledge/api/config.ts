/**
 * knowledge 模块 API 配置。
 *
 * 基础三件套（normalizeBaseUrl / API_KEY / apiKeyHeader / API_BASE_URL）已上移
 * 至共享层 `web/src/api/config.ts`（接口审查批次 2026-09-28），此处保留原路径
 * re-export——存量 `../api/config` 引用零改动。
 *
 * 本文件保留 retrieval 域专属部分（前缀注入 / withApiBase）。
 */
export { API_KEY, API_BASE_URL, apiKeyHeader, normalizeBaseUrl } from '../../../api/config'

import { API_BASE_URL } from '../../../api/config'

/**
 * R4.x 路径适配：融合后检索域 API 统一挂载在 /api/v1/retrieval 前缀下（R1.10）。
 * knowledge 模块内调用处一律写「域内路径」（如 /auth/login、/knowledge-bases），
 * 前缀由此处统一注入——调用处不再出现 /api/v1 字面量。
 */
export const RETRIEVAL_API_PREFIX = '/api/v1/retrieval'

export function withApiBase(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  // 兼容直传完整 /api/v1/... 的旧调用：剥掉旧前缀，避免与 RETRIEVAL_API_PREFIX 叠加
  // 产出 /api/v1/retrieval/api/v1/... 这类双前缀 URL。
  let p = path
  if (p.startsWith(`${RETRIEVAL_API_PREFIX}/`)) p = p.slice(RETRIEVAL_API_PREFIX.length)
  else if (p.startsWith('/api/v1/')) p = p.slice('/api/v1'.length)
  else if (p === '/api/v1' || p === '/api/v1/') p = ''
  if (!p || p === '/') return `${API_BASE_URL}${RETRIEVAL_API_PREFIX}`
  const suffix = p.startsWith('/') ? p : `/${p}`
  return `${API_BASE_URL}${RETRIEVAL_API_PREFIX}${suffix}`
}
