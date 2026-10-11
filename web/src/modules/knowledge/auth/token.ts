/**
 * JWT access_token 存取 —— 原实现已提升到共享层 `web/src/api/token.ts`
 * （批次 B3：chat → knowledge 反向依赖解耦）。本文件保留 re-export，
 * knowledge 存量引用（auth/session、api/auth、api/http、composables）
 * 零改动；新代码请直接从 `api/token` 导入。
 */

export { getToken, setToken, clearToken } from '../../../api/token'
