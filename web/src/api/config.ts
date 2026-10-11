/**
 * 共享 API 配置层（接口审查批次 2026-09-28）。
 *
 * 此前 `web/src/api/` 是空目录：baseUrl 归一化 / x-api-key 只存在于
 * `modules/knowledge/api/config.ts`，chat 模块于是出现两类问题：
 * 1. 自己裸读 `import.meta.env.VITE_API_BASE_URL`（不归一化 → 尾斜杠配置
 *    拼出 `//api/v1/...` 双斜杠，部分代理 404，同环境下一模块能用一模块不能用）；
 * 2. token/apiKey 跨模块从 knowledge 反向 import（模块边界倒挂）。
 * 现统一收敛到本文件，两个模块都从共享层取（knowledge 原路径保留 re-export，
 * 存量引用零改动）。
 */

export function normalizeBaseUrl(raw: string | undefined | null): string {
  const s = (raw ?? '').trim()
  if (!s) return ''
  return s.endsWith('/') ? s.slice(0, -1) : s
}

/**
 * 可选共享 API key（对应后端 `API_KEY` + `x-api-key` 头；`REQUIRE_API_KEY=true`
 * 时所有请求必须携带，否则 401）。
 * ⚠️ `VITE_*` 变量会打进前端产物——只能当「门禁钥匙」，不是保密凭证；
 * 真正的机密仍由 JWT 承载。
 */
export const API_KEY: string = String(import.meta.env?.VITE_API_KEY ?? '').trim()

/** 附加 `x-api-key` 头（未配置时返回空对象，调用处行为零变化）。 */
export function apiKeyHeader(): Record<string, string> {
  return API_KEY ? { 'x-api-key': API_KEY } : {}
}

/**
 * 可选：当本地开发不走 Vite proxy 时，提供直连后端的 baseUrl。
 * - 不配置时：默认使用相对路径（例如 /api/v1/...），交给 Vite proxy 处理。
 * - 配置时：会把相对路径拼成绝对地址（例如 http://127.0.0.1:8001/api/v1/...）。
 * 统一经 `normalizeBaseUrl` 归一化（去尾斜杠），杜绝 `//` 双斜杠拼接。
 */
export const API_BASE_URL = normalizeBaseUrl(import.meta.env?.VITE_API_BASE_URL)
