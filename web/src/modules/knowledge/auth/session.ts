import { ref } from 'vue'

import type { UserMe } from '../api/auth'
import * as authApi from '../api/auth'
import { clearToken } from './token'

export const currentUser = ref<UserMe | null>(null)
export const authReady = ref(false)

export { getToken, setToken } from './token'

export async function bootAuth(): Promise<void> {
  authReady.value = false
  try {
    currentUser.value = await authApi.fetchMe()
  } catch {
    currentUser.value = null
  } finally {
    authReady.value = true
  }
}

export function clearSession(): void {
  currentUser.value = null
  clearToken()
}

/** fetch 等非 axios 请求的 401（与 http 拦截器行为对齐） */
export function applyFetchUnauthorized(): void {
  clearSession()
  window.dispatchEvent(new CustomEvent('tracerag:unauthorized'))
}
