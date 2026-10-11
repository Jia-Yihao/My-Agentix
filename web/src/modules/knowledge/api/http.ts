import axios from 'axios'

import { clearSession } from '../auth/session'
import { getToken } from '../auth/token'
import { API_BASE_URL, RETRIEVAL_API_PREFIX, API_KEY } from './config'

export const http = axios.create({
  baseURL: `${API_BASE_URL}${RETRIEVAL_API_PREFIX}` || undefined,
  withCredentials: true,
})

// R4.3：单一 JWT 会话——所有 axios 请求统一注入 Authorization: Bearer；
// 共享 API key（REQUIRE_API_KEY=true 时的 x-api-key 门禁）同步注入。
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  if (API_KEY) config.headers.set('x-api-key', API_KEY)
  return config
})

http.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status !== 401) return Promise.reject(err)

    const url = String(err.config?.url ?? '')

    if (url.includes('/auth/login') || url.includes('/auth/register')) {
      return Promise.reject(err)
    }

    if (url.includes('/users/me')) {
      clearSession()
      return Promise.reject(err)
    }

    clearSession()
    window.dispatchEvent(new CustomEvent('tracerag:unauthorized'))
    return Promise.reject(err)
  },
)
