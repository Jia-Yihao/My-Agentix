/**
 * P1-4 前端最小流式接入（已由 ChatWidget 接线为主路径）。
 *
 * 后端 SSE 协议（每个事件一帧 `data: <ChatStreamChunk json>\n\n`）：
 *   - {"type":"metadata","metadata":{"status":"processing"}}
 *   - {"type":"metadata","metadata":{"route":"kb","logic":"..."},"route":"kb"}
 *   - {"type":"tool_start","metadata":{"tool":"milvus_search"}}
 *   - {"type":"message","content":"<token>"}      // 逐 token，追加到气泡
 *   - {"type":"tool_end","metadata":{"tool":"milvus_search"}}
 *   - {"type":"done","metadata":{"sources":[...]}}
 *   - {"type":"error","content":"处理请求时出错，请稍后重试。"}
 *
 * 终态语义（接口审查批次 2026-09-28）：后端契约是 **error 之后必补 done**
 * （chat_service 的「恰好一次 done」兜底，被 test_p14 钉死）。解析器因此
 * 引入 terminated 标志——error/done 任一到达后丢弃后续帧，否则 ChatWidget
 * 的 outcome 会被迟到的 done 从 `error` 翻转成 `delivered`（错误分支成死
 * 代码、报错轮次仍被消费附件）。
 */

/** 与后端 `ChatStreamChunk` 对齐的流式分片模型。 */
export interface ChatStreamChunk {
  type: "message" | "metadata" | "tool_start" | "tool_end" | "done" | "error";
  /** 旧服务端双写期遗留词（A5/R5b 归一后新帧只发 type；分发仍容错旧帧） */
  event?: string;
  content?: string | null;
  metadata?: Record<string, unknown> | null;
  session_id?: string | null;
  route?: string | null;
}

/** 流式消费回调（全部可选，按需实现）。 */
export interface ChatStreamHandlers {
  /** 收到路由/状态等元信息帧。 */
  onMetadata?: (chunk: ChatStreamChunk) => void;
  /** 收到一个 LLM token，调用方应“追加”而非替换。 */
  onMessage?: (token: string, chunk: ChatStreamChunk) => void;
  /** 工具（检索）开始。 */
  onToolStart?: (tool: string, chunk: ChatStreamChunk) => void;
  /** 工具（检索）结束。 */
  onToolEnd?: (tool: string, chunk: ChatStreamChunk) => void;
  /** 流正常结束，携带引用来源。 */
  onDone?: (sources: unknown[], chunk: ChatStreamChunk) => void;
  /** 服务端返回的友好错误事件。 */
  onError?: (message: string, chunk: ChatStreamChunk) => void;
  /** 拿到会话 id（首次会话时后端会新建）。 */
  onSession?: (sessionId: string) => void;
}

export interface StreamChatOptions {
  message: string;
  sessionId?: string;
  userId?: string;
  imagePath?: string;
  filePath?: string;
  /** 覆盖 Excel 增量入库开关（后端默认取服务端配置）。 */
  ingestIncremental?: boolean;
  signal?: AbortSignal;
  apiBase?: string;
}

import { getToken as getAccessToken, clearToken } from '../../../api/token';
import { API_BASE_URL, apiKeyHeader } from '../../../api/config';
import { readSseData } from '../../../api/sse';

/** 解析器终态（error/done 任一到达后置位，后续帧全部丢弃）。 */
interface ParserState {
  terminated: boolean;
}

/**
 * 以流式方式调用后端 `/api/v1/chat/stream`，逐 token 回调。
 *
 * @returns 终态 Promise：`done` 或 `error` 事件到达（或流结束）后 resolve；
 *          网络/解析层异常 reject。
 */
export async function streamChat(
  options: StreamChatOptions,
  handlers: ChatStreamHandlers = {}
): Promise<void> {
  // 共享层统一归一化 baseUrl（原裸读 env：尾斜杠配置会拼出 //api/v1/...）
  const base = options.apiBase ?? API_BASE_URL;
  const payload: Record<string, unknown> = {
    message: options.message,
    session_id: options.sessionId || undefined,
    user_id: options.userId || "default_user",
    stream: true
  };
  if (options.imagePath) payload.image_path = options.imagePath;
  if (options.filePath) {
    payload.file_path = options.filePath;
    if (options.ingestIncremental !== undefined) {
      payload.ingest_incremental = options.ingestIncremental;
    }
  }

  const token = getAccessToken();
  const response = await fetch(`${base}/api/v1/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...apiKeyHeader(),
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(payload),
    // 与 knowledge 域同口径：跨源部署时带上 cookie（Vite proxy 下无副作用）
    credentials: "include",
    signal: options.signal
  });

  if (!response.ok) {
    // B5：401 与 knowledge 域对齐——清 token + 广播全局未授权（App.vue 路由收口）。
    // V1_REQUIRE_JWT 默认关时不会走到这里；开启后 chat 域不再静默失败。
    if (response.status === 401) {
      clearToken();
      window.dispatchEvent(new CustomEvent("tracerag:unauthorized"));
    }
    throw new Error(`stream request failed: HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("stream response has no body");
  }

  const parserState: ParserState = { terminated: false };

  // A5/R5c：字节→帧→data 行的机械 framing 收口到共享层（api/sse.ts）；
  // JSON 容错与业务分发（含终态 terminated）仍在本模块。
  await readSseData(response, (dataLine) => {
    let chunk: ChatStreamChunk;
    try {
      chunk = JSON.parse(dataLine) as ChatStreamChunk;
    } catch {
      // 非法 JSON 帧直接跳过，不打断整条流
      return;
    }
    dispatchChunk(chunk, handlers, parserState);
  });
}

/** 将业务分片路由到对应回调。 */
function dispatchChunk(chunk: ChatStreamChunk, handlers: ChatStreamHandlers, state: ParserState): void {
  // 终态之后的帧一律丢弃（error 后必补 done 是后端契约，见文件头注释）
  if (state.terminated) return;

  if (chunk.session_id && handlers.onSession) {
    handlers.onSession(chunk.session_id);
  }

  // 词汇容错（A5/R5b 后服务端只发 type；event 仅为旧服务端/双写期帧保留）：
  // 分发优先读 event，回退 type
  switch (chunk.event ?? chunk.type) {
    case "metadata":
      handlers.onMetadata?.(chunk);
      break;
    case "message":
      if (chunk.content) handlers.onMessage?.(chunk.content, chunk);
      break;
    case "tool_start":
      handlers.onToolStart?.(String(chunk.metadata?.tool ?? ""), chunk);
      break;
    case "tool_end":
      handlers.onToolEnd?.(String(chunk.metadata?.tool ?? ""), chunk);
      break;
    case "done": {
      state.terminated = true;
      const raw = chunk.metadata?.sources;
      handlers.onDone?.(Array.isArray(raw) ? raw : [], chunk);
      break;
    }
    case "error":
      state.terminated = true;
      handlers.onError?.(chunk.content || "处理请求时出错，请稍后重试。", chunk);
      break;
    default:
      break;
  }
}
