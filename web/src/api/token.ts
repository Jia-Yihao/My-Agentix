/**
 * JWT access_token 存取（共享 API 层；批次 B3 / R4.3 单一 session 贯通两模块）。
 *
 * 原实现位于 `modules/knowledge/auth/token.ts`，chat 模块从 knowledge
 * **反向 import**（模块边界倒挂）。现提升到本共享层（与 `api/config.ts`
 * 同层），knowledge/chat 都从这里取 token 注入 Authorization: Bearer；
 * knowledge 原路径保留 re-export，存量引用零改动。
 *
 * 独立于 session 状态与登录接口，避免 session ↔ auth 的循环 import。
 */

const TOKEN_STORAGE_KEY = 'agentix:access_token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // localStorage 不可用（隐私模式等）：token 仅存活于当前页面内存态
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // 同上，忽略存储异常
  }
}
