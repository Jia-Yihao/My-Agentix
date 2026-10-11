/**
 * v1 chat 契约消费工具（接口审查批次 2026-09-27）。
 *
 * 后端 `ChatResponse` 产出 `status`（ok|degraded）与 `error_code`（P0-3 ErrorCode），
 * ChatWidget 原先完全不消费——降级兜底被当成正常答案展示。4xx/5xx 则有三种错误体形状：
 *   1. FastAPI `HTTPException`：`{ detail: string | { error_code, message, ... } }`
 *   2. AppError 全局处理器：`{ request_id, error_code, message, user_hint }`
 *   3. `APIKeyMiddleware`：`{ error, message }`
 */

export const FALLBACK_CHAT_ERROR = "请求出错了，请稍后重试。";
export const EMPTY_RESPONSE_HINT = "抱歉，未能获取到有效的响应。";

/** ErrorCode → 用户可读文案（未收录的码原样展示）。 */
const ERROR_CODE_LABELS: Record<string, string> = {
  llm_timeout: "模型响应超时",
  llm_rate_limit: "模型限流",
  llm_unavailable: "模型服务暂不可用",
  tool_not_found: "工具不存在",
  tool_exec_failed: "工具执行失败",
  retrieval_failed: "检索失败",
  graph_unavailable: "图服务不可用",
  context_overflow: "上下文超长",
  max_steps_exceeded: "执行步数超限",
  agent_timeout: "执行超时",
  token_budget_exceeded: "token 预算超限",
  no_evidence: "未找到足够证据",
  out_of_scope: "超出能力范围"
};

export function friendlyErrorCode(code?: string | null): string {
  if (!code) return "";
  return ERROR_CODE_LABELS[code] ?? code;
}

function pickStr(obj: unknown, key: string): string | undefined {
  if (!obj || typeof obj !== "object") return undefined;
  const v = (obj as Record<string, unknown>)[key];
  return typeof v === "string" && v.trim() ? v : undefined;
}

/** 从 axios 错误里提取一句用户可读的失败原因（永远返回非空字符串）。 */
export function extractChatError(error: unknown): string {
  const err = error as { response?: { data?: unknown }; message?: string };
  const data = err?.response?.data;

  if (data && typeof data === "object") {
    // AppError 全局处理器：user_hint 面向用户，message 兜底
    const hint = pickStr(data, "user_hint") ?? pickStr(data, "message");
    if (hint) return hint;

    const detail = (data as Record<string, unknown>).detail;
    // FastAPI HTTPException detail 为字符串
    if (typeof detail === "string" && detail.trim()) return detail;
    // HTTPException detail 为对象（chat_service 致命错误形状）
    if (detail && typeof detail === "object") {
      const msg = pickStr(detail, "message") ?? pickStr(detail, "msg");
      const code = pickStr(detail, "error_code");
      if (msg) return code ? `${msg}（${friendlyErrorCode(code)}）` : msg;
      if (code) return friendlyErrorCode(code);
    }
    // 422 校验错误：detail 为数组
    if (Array.isArray(detail) && detail.length > 0) {
      const msg = pickStr(detail[0], "msg");
      if (msg) return `参数校验失败：${msg}`;
    }
    // APIKeyMiddleware 形状 { error, message }
    const plain = pickStr(data, "message") ?? pickStr(data, "error");
    if (plain) return plain;
  }

  // 网络层错误（无响应体）
  if (typeof err?.message === "string" && err.message.trim()) return err.message;
  return FALLBACK_CHAT_ERROR;
}

export interface DegradedOpts {
  status?: string | null;
  errorCode?: string | null;
  traceId?: string | null;
}

/** 非 ok 状态在正文尾部追加降级/异常提示（ok 或缺省原样返回）。 */
export function decorateDegraded(content: string, opts: DegradedOpts): string {
  if (!opts.status || opts.status === "ok") return content;
  const label = opts.status === "degraded" ? "回答已降级" : `响应状态：${opts.status}`;
  const code = opts.errorCode ? `（${friendlyErrorCode(opts.errorCode)}）` : "";
  const trace = opts.traceId ? `，追踪 ID：${opts.traceId}` : "";
  return `${content}\n\n⚠️ ${label}${code}${trace}`;
}

/** `message` 为空时的兜底文案（带得上 error_code 就带上）。 */
export function emptyResponseMessage(errorCode?: string | null): string {
  const code = errorCode ? `（${friendlyErrorCode(errorCode)}）` : "";
  return `${EMPTY_RESPONSE_HINT}${code}`;
}
