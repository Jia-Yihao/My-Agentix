import { API_BASE_URL, RETRIEVAL_API_PREFIX, withApiBase } from './config'

/**
 * 后端流式会话地址（SSE，由调用方读取 Response.body）。
 *
 * 原值 '/chat/stream' 有两处错误：
 * 1. Vite 代理只转发 /api、/uploads、/health，/chat 会打到 Vite 自身 → 404；
 * 2. 漏掉 /api/v1/retrieval 前缀，且 retrieval 聊天路由要求 Bearer JWT
 *    （见 agentix/retrieval/api/routers/chat.py）。
 * 正确地址：{API_BASE_URL}/api/v1/retrieval/chat/stream
 */
export const CHAT_STREAM_URL = withApiBase('/chat/stream')

export { API_BASE_URL, RETRIEVAL_API_PREFIX }
