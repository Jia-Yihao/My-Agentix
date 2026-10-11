<template>
  <div class="chat-entry" :class="{ 'is-embedded': props.embedded }">
    <transition name="fade">
      <button
        v-if="!isOpen"
        class="floating-button"
        type="button"
        @click="openChat"
      >
        <span class="icon">💬</span>
        <span class="label">与 My Agentix 对话</span>
      </button>
    </transition>

    <transition name="pop">
      <div v-if="isOpen" class="chat-panel">
        <header class="chat-header">
          <div class="header-content">
            <div class="title-row">
              <div class="title">My Agentix 智能助手</div>
              <div class="session-chip">
                {{ sessionId ? "会话ID：" + sessionId : "新会话" }}
              </div>
            </div>
              <div class="subtitle">
                <span class="status-dot"></span>
                在线 · 多源知识库
              </div>
          </div>
          <button v-if="!props.embedded" class="close-btn" type="button" @click="closeChat">×</button>
        </header>

        <section ref="messageContainer" class="chat-body">
          <div class="suggestion-bar">
            <button
              v-for="(item, idx) in quickSuggestions"
              :key="idx"
              type="button"
              class="suggestion-chip"
              @click="sendSuggestion(item.prompt)"
            >
              <span class="chip-icon">⚡</span>
              <span>{{ item.label }}</span>
            </button>
          </div>
          <div v-if="messages.length === 0" class="empty-hint">
            <p>你好！我可以帮你查询知识库与图谱，或分析上传的文件。</p>
            <ul>
              <li>直接输入问题，例如“帮我总结上传文档的要点”。</li>
              <li>上传文件后问我“请分析这份文档”。</li>
            </ul>
            <p class="disclaimer">⚠️ 回答由 AI 基于已收录的知识生成，仅供参考，不构成专业意见。</p>
          </div>

          <div
            v-for="(message, index) in messages"
            :key="index"
            :class="['message', message.role]"
          >
            <div class="bubble">
              <p v-if="message.activity" class="activity">{{ message.activity }}</p>
              <p class="content" v-html="linkify(message.content)"></p>
              <div v-if="message.sources?.length" class="sources">
                <span class="sources-title">来源引用</span>
                <ul>
                  <li
                    v-for="(source, sIdx) in message.sources"
                    :key="sIdx"
                    class="citation-card"
                  >
                    <template v-if="typeof source === 'string'">
                      <div class="citation-head">
                        <span class="badge-law">来源</span>
                      </div>
                      <p class="citation-snippet">{{ source }}</p>
                    </template>
                    <template v-else>
                      <div class="citation-head">
                        <span class="badge-law">{{ source.law || source.type || "来源" }}</span>
                        <span v-if="source.article" class="badge-article">{{ source.article }}</span>
                      </div>
                      <p v-if="source.content_snippet" class="citation-snippet">{{ source.content_snippet }}</p>
                      <span v-if="source.source" class="citation-source">{{ source.source }}</span>
                      <span v-else class="citation-source">{{ formatSource(source) }}</span>
                    </template>
                  </li>
                </ul>
              </div>
              <div v-if="message.route" class="route">
                路由：{{ message.route }}
                <span v-if="message.routeLogic">（{{ message.routeLogic }}）</span>
              </div>
            </div>
          </div>

          <div v-if="isTyping && !hasStreamingBubble" class="message assistant">
            <div class="bubble typing">
              <span class="dot"></span>
              <span class="dot"></span>
              <span class="dot"></span>
            </div>
          </div>
        </section>

        <footer class="chat-footer">
          <div class="upload-row">
            <label class="upload-button">
              <input type="file" class="hidden" @change="onFileSelected" />
              <span>📎 上传文件</span>
            </label>
            <div v-if="uploadStatus" class="upload-status">
              {{ uploadStatus }}
            </div>
            <button
              v-if="sessionId"
              type="button"
              class="reset-session"
              @click="resetSession"
            >
              开启新会话
            </button>
          </div>
          <form class="input-row" @submit.prevent="sendMessage">
            <textarea
              v-model="userInput"
              :placeholder="placeholder"
              :disabled="isTyping"
              maxlength="5000"
              rows="2"
              @keydown.enter.exact.prevent="sendMessage"
            />
            <button class="send-button" type="submit" :disabled="isTyping || !userInput.trim()">
              发送
            </button>
          </form>
        </footer>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import axios from "axios";
import { getToken as getAccessToken, clearToken } from "../api/token";
import { API_BASE_URL as API_BASE, apiKeyHeader } from "../api/config";
import { decorateDegraded, emptyResponseMessage, extractChatError } from "../modules/chat/api/chatContract";
import { streamChat } from "../modules/chat/api/chatStream";

const props = withDefaults(defineProps<{ embedded?: boolean }>(), {
  embedded: false,
});

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  route?: string | null;
  routeLogic?: string | null;
  sources?: Array<Record<string, unknown> | string>;
  /** 流式期间的活动提示（思考/工具调用），收到正文后清空 */
  activity?: string | null;
  /** 该气泡是进行中的流式回答（隐藏底部打点占位） */
  streaming?: boolean;
}

// 共享层归一化 baseUrl（原裸读 env：尾斜杠配置会拼出 //api/v1/ 双斜杠）

// ── 会话持久化 + 历史恢复（sessions/history 契约接线，审查批次 2026-09-27）──
// 后端三条契约：POST /chat 返回 session_id（自动建会话）、GET /chat/history/{id}
// 取回历史、DELETE /chat/session/{id} 清空服务端消息。原先前端零调用 →
// 刷新即丢历史、旧会话成孤儿。
const SESSION_STORAGE_KEY = "agentix:chat:session_id";

function readStoredSessionId(): string {
  try {
    return localStorage.getItem(SESSION_STORAGE_KEY) || "";
  } catch {
    return ""; // 隐私模式 / 存储禁用
  }
}

function persistSessionId(id: string): void {
  try {
    if (id) localStorage.setItem(SESSION_STORAGE_KEY, id);
    else localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    /* 存储不可用时忽略——退化为无持久化 */
  }
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { ...apiKeyHeader() };
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

const defaultState = () => ({
  isOpen: false,
  isTyping: false,
  sessionId: readStoredSessionId(),
  userInput: "",
  messages: [] as ChatMessage[],
  uploadStatus: "",
  attachedFilePath: "",
  attachedFileName: ""
});

const state = reactive(defaultState());

const messageContainer = ref<HTMLElement | null>(null);

const isOpen = computed(() => props.embedded || state.isOpen);
const isTyping = computed(() => state.isTyping);
const sessionId = computed(() => state.sessionId);
const userInput = computed({
  get: () => state.userInput,
  set: (val: string) => {
    state.userInput = val;
  }
});
const messages = computed(() => state.messages);
const uploadStatus = computed(() => state.uploadStatus);
/** 存在进行中的流式气泡时，隐藏底部打点占位（流式活动提示已上屏）。 */
const hasStreamingBubble = computed(() => state.messages.some((m) => m.streaming === true));

const placeholder = computed(() => {
  if (state.isTyping) return "正在生成回复...";
  if (state.attachedFileName) return `文件已上传：${state.attachedFileName}，请输入问题`;
  return "请输入您的问题，例如“帮我总结上传文档的要点”";
});

const quickSuggestions = [
  {
    label: "知识检索",
    prompt: "帮我查询知识库中与此相关的内容。"
  },
  {
    label: "文档分析",
    prompt: "我上传了一份文档，请总结其中的要点。"
  },
  {
    label: "上传文件后如何分析？",
    prompt: "我已经上传了一份文档，请帮我总结其中的要点。"
  },
  {
    label: "知识来源",
    prompt: "知识库里的来源有哪些？"
  }
] as const;

function openChat() {
  state.isOpen = true;
}

function closeChat() {
  state.isOpen = false;
}

defineExpose({
  openChat,
  closeChat,
  resetSession,
});
function resetSession() {
  const prevId = state.sessionId;
  state.sessionId = "";
  persistSessionId("");
  state.messages = [];
  state.uploadStatus = "";
  state.attachedFileName = "";
  state.attachedFilePath = "";
  if (prevId) {
    // 尽力清空服务端消息（fire-and-forget：失败不影响本地重开新会话）
    axios
      .delete(`${API_BASE}/api/v1/chat/session/${encodeURIComponent(prevId)}`, {
        headers: authHeaders()
      })
      .catch((e) => console.warn("clear remote session failed", e));
  }
}

/** 把 GET /chat/history/{id} 的消息映射为本地 ChatMessage 并回填。 */
async function restoreHistory(sessionId: string): Promise<void> {
  try {
    const { data } = await axios.get(
      `${API_BASE}/api/v1/chat/history/${encodeURIComponent(sessionId)}`,
      { params: { limit: 100 }, headers: authHeaders() }
    );
    if (!Array.isArray(data) || data.length === 0) return;
    const restored: ChatMessage[] = data
      .map((m: {
        message_type?: string;
        content?: string;
        message_metadata?: Record<string, unknown> | null;
      }) => {
        const meta = m.message_metadata || {};
        const restoredSources = meta.sources;
        return {
          role: m.message_type === "user_query" ? ("user" as const) : ("assistant" as const),
          content: String(m.content ?? ""),
          route: (meta.route as string | undefined) || null,
          routeLogic: null,
          // 后端 save_message 起持久化 metadata.sources（C6）；老数据无此键 → []
          sources: Array.isArray(restoredSources)
            ? (restoredSources as Array<Record<string, unknown> | string>)
            : []
        };
      })
      .filter((m: ChatMessage) => m.content);
    if (restored.length > 0) state.messages = restored;
  } catch (e) {
    const status = axios.isAxiosError(e) ? e.response?.status : undefined;
    if (status === 404) {
      // 服务端已无此会话 → 丢弃本地存的 id，下条消息重新开
      state.sessionId = "";
      persistSessionId("");
    }
    console.warn("restore chat history failed", e);
  }
}

async function onFileSelected(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  state.uploadStatus = "正在上传...";
  try {
    const formData = new FormData();
    formData.append("file", file);
    const authHeaders: Record<string, string> = {
      ...apiKeyHeader()
    };
    const token = getAccessToken();
    if (token) authHeaders.Authorization = `Bearer ${token}`;
    const { data } = await axios.post(`${API_BASE}/api/v1/upload/file`, formData, {
      headers: authHeaders
    });
    if (data?.success) {
      state.attachedFilePath = data.file_path;
      state.attachedFileName = data.original_name || file.name;
      state.uploadStatus = `已上传：${state.attachedFileName}`;
    } else {
      throw new Error(data?.detail || "上传失败");
    }
  } catch (error: unknown) {
    console.error("File upload failed", error);
    state.uploadStatus = "上传失败，请重试";
  } finally {
    // reset input to allow re-upload same file
    input.value = "";
  }
}

function sendSuggestion(prompt: string) {
  if (state.isTyping) return;
  state.userInput = prompt;
  void sendMessage();
}

/** 流式发送结果：delivered=拿到回答；error=服务端已应答但报错（不重试）；failed=未触达业务（可回退非流式）。 */
type SendOutcome = "delivered" | "error" | "failed";

/**
 * 流式主路径（Phase C1 接线）：POST /api/v1/chat/stream，逐 token 上屏，
 * 工具/路由元信息进气泡。任何网络/HTTP 层失败返回 "failed" 由调用方
 * 回退既有非流式契约（含降级 status/error_code 语义）。
 */
async function sendViaStream(message: string): Promise<SendOutcome> {
  const assistant: ChatMessage = {
    role: "assistant",
    content: "",
    activity: "正在思考…",
    streaming: true
  };
  state.messages.push(assistant);
  let outcome: SendOutcome = "failed";
  // 只要收到任意服务端帧（2xx 后首个 SSE 帧），即视为「业务已应答」——
  // 此后任何网络层失败都不得回退非流式（会二次执行同一问题）。
  let serverResponded = false;

  try {
    await streamChat(
      {
        message,
        sessionId: state.sessionId || undefined,
        filePath: state.attachedFilePath || undefined,
        ingestIncremental: state.attachedFilePath ? true : undefined
      },
      {
        onSession: (id) => {
          serverResponded = true;
          state.sessionId = id;
          persistSessionId(id);
        },
        onMetadata: (chunk) => {
          serverResponded = true;
          const meta = (chunk.metadata ?? {}) as Record<string, unknown>;
          if (meta.route) {
            assistant.route = String(meta.route);
            assistant.routeLogic = meta.logic ? String(meta.logic) : null;
          }
          if (meta.status === "processing" && !assistant.content) {
            assistant.activity = "正在思考…";
          }
        },
        onToolStart: (tool) => {
          serverResponded = true;
          assistant.activity = tool ? `正在调用 ${tool}…` : "正在检索…";
        },
        onToolEnd: () => {
          assistant.activity = assistant.content ? null : "正在整理回答…";
        },
        onMessage: (token) => {
          serverResponded = true;
          assistant.content += token;
          assistant.activity = null;
        },
        onDone: (sources) => {
          // 解析器已保证 error 后不再派发 done；此处兜底防 outcome 被翻转
          if (outcome === "error") return;
          assistant.sources = sources as Array<Record<string, unknown> | string>;
          if (!assistant.content) assistant.content = emptyResponseMessage(undefined);
          assistant.activity = null;
          outcome = "delivered";
        },
        onError: (msg) => {
          serverResponded = true;
          assistant.content = `提示：${msg}`;
          assistant.activity = null;
          outcome = "error";
        }
      }
    );
    // 流正常关闭但没有终态帧：有正文视为已交付（标记为部分回答），否则回退。
    // outcome 的实际写入在回调里——用读函数取值，避免 TS 按初始化值误收窄。
    const readOutcome = (): SendOutcome => outcome;
    if (readOutcome() === "failed" && assistant.content) {
      outcome = "delivered";
      if (!assistant.sources) assistant.sources = [];
      // 没收到 done 终态帧 = 正文不完整，诚实标注而不是静默当完整答案
      assistant.content = `${assistant.content}\n\n（连接提前关闭，以上为已生成的部分回答）`;
    }
    const settled = readOutcome();
    if (settled === "error") {
      // 服务端已应答但报错 → 不回退（回退会二次执行同一问题）
      return "error";
    }
    if (settled !== "delivered") {
      removeMessage(assistant);
      return "failed";
    }
    return "delivered";
  } catch (e) {
    if (serverResponded) {
      // 已收到服务端帧后的网络失败：回退非流式 = 同一问题二次执行
      // （POST /chat 再存一条 user 消息 + ingest_incremental 副作用重放）。
      console.warn("stream interrupted mid-flight (no non-stream fallback)", e);
      if (!assistant.content) {
        assistant.content = "提示：连接中断，未能收到完整回答。";
      } else {
        assistant.content = `${assistant.content}\n\n（连接中断，以上为已生成的部分回答）`;
      }
      assistant.activity = null;
      return "error";
    }
    // 尚未触达业务（请求未发出/HTTP 层失败）：摘掉空气泡，交回非流式路径
    console.warn("stream chat failed, fallback to non-stream", e);
    removeMessage(assistant);
    return "failed";
  } finally {
    assistant.streaming = false;
    assistant.activity = null;
  }
}

function removeMessage(target: ChatMessage): void {
  const idx = state.messages.indexOf(target);
  if (idx !== -1) state.messages.splice(idx, 1);
}

/** 非流式回退路径（既有契约：status/error_code/trace_id 降级语义）。 */
async function sendViaNonStream(message: string): Promise<boolean> {
  try {
    const payload: Record<string, unknown> = {
      message,
      session_id: state.sessionId || undefined,
      stream: false
    };

    if (state.attachedFilePath) {
      payload.file_path = state.attachedFilePath;
      payload.ingest_incremental = true;
    }

    const token = getAccessToken();
    const { data } = await axios.post(`${API_BASE}/api/v1/chat`, payload, {
      headers: {
        ...apiKeyHeader(),
        ...(token ? { Authorization: `Bearer ${token}` } : {})
      }
    });

    if (data?.message) {
      const assistantMessage: ChatMessage = {
        role: "assistant",
        // status/error_code 契约消费：degraded 等非 ok 状态追加可见提示，
        // 降级兜底不再被当成正常答案（审查批次 2026-09-27）
        content: decorateDegraded(String(data.message), {
          status: data.status,
          errorCode: data.error_code,
          traceId: data.trace_id
        }),
        route: data.route || null,
        routeLogic: data.route_logic || null,
        sources: data.sources || []
      };
      state.messages.push(assistantMessage);
    } else {
      state.messages.push({
        role: "assistant",
        content: emptyResponseMessage(data?.error_code)
      });
    }

    if (data?.session_id) {
      state.sessionId = data.session_id;
      persistSessionId(data.session_id);
    }
    return true;
  } catch (error: unknown) {
    // B5：401 与 knowledge 域对齐——清 token + 广播全局未授权（App.vue 路由收口）
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 401) {
      clearToken();
      window.dispatchEvent(new CustomEvent("tracerag:unauthorized"));
    }
    console.error("Chat request failed", error);
    state.messages.push({
      role: "assistant",
      // 透出真实失败原因（detail.error_code / user_hint / 中间件 message…），
      // 不再一律固定文案
      content: extractChatError(error)
    });
    return false;
  }
}

async function sendMessage() {
  const message = state.userInput.trim();
  if (!message || state.isTyping) {
    return;
  }

  const userMessage: ChatMessage = { role: "user", content: message };
  state.messages.push(userMessage);
  state.userInput = "";
  state.isTyping = true;

  try {
    const outcome = await sendViaStream(message);
    if (outcome === "failed") {
      const delivered = await sendViaNonStream(message);
      if (!delivered) return; // 失败路径保留附件以便重试（原契约语义）
    } else if (outcome === "error") {
      return; // 服务端已应答但报错：保留附件，不二次执行
    }
    // 一次性消费文件（delivered）
    state.attachedFilePath = "";
    state.attachedFileName = "";
    state.uploadStatus = "";
  } finally {
    state.isTyping = false;
  }
}

function linkify(text: string): string {
  if (!text) return "";
  // First escape HTML to prevent XSS
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
  // Then linkify URLs (safe because we already escaped HTML entities)
  return escaped.replace(
    /(https?:\/\/[^\s]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
}

function formatSource(source: Record<string, unknown> | string): string {
  // KB 问答路径的 sources 是纯字符串列表（collect_sources 返回 list[str]）
  if (typeof source === "string") return source;
  const docId = source.document_id || source.source || source.id || source.name;
  const name = source.name || source.title || "";
  if (name && docId && name !== docId) {
    return `${name} (${docId})`;
  }
  return String(docId || JSON.stringify(source));
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && state.isOpen) {
    closeChat();
  }
}

// 消息数变化（新气泡）与正文增长（流式 token 追加）都触发贴底滚动
watch(
  [
    () => state.messages.length,
    () => state.messages.reduce((n, m) => n + m.content.length, 0)
  ],
  async () => {
    await nextTick();
    if (messageContainer.value) {
      messageContainer.value.scrollTop = messageContainer.value.scrollHeight;
    }
  }
);

onMounted(() => {
  window.addEventListener("keydown", handleKeydown);
  // 刷新后按持久化的 session_id 取回历史（契约：GET /chat/history/{id}）
  if (state.sessionId) void restoreHistory(state.sessionId);
});

onUnmounted(() => {
  window.removeEventListener("keydown", handleKeydown);
});
</script>

<style scoped lang="scss">
.chat-entry {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 1000;
  font-size: 14px;
  color: #1f2937;
}

.chat-entry.is-embedded {
  position: relative;
  inset: auto;
  z-index: auto;
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 0;
}

.chat-entry.is-embedded .chat-panel {
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.floating-button {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 999px;
  border: none;
  background: linear-gradient(135deg, #f97316, #ef4444);
  color: #fff;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 15px 30px rgba(249, 115, 22, 0.35);
  transition: transform 0.2s ease, box-shadow 0.2s ease;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 20px 34px rgba(239, 68, 68, 0.4);
  }

  .icon {
    font-size: 18px;
  }
}

.chat-panel {
  width: clamp(320px, 30vw, 420px);
  height: clamp(460px, 50vh, 640px);
  display: flex;
  flex-direction: column;
  border-radius: 20px;
  background: #fffdfb;
  box-shadow: 0 28px 60px rgba(15, 23, 42, 0.18);
  overflow: hidden;
  border: 1px solid rgba(248, 187, 125, 0.35);
}

.chat-header {
  padding: 18px 20px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  background: linear-gradient(135deg, #f97316, #ef4444);
  color: #fff;
  box-shadow: inset 0 -1px 0 rgba(255, 255, 255, 0.2);

  .header-content {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .title {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  .subtitle {
    font-size: 12px;
    opacity: 0.85;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .session-chip {
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.16);
    font-size: 11px;
    font-weight: 500;
    backdrop-filter: blur(4px);
    max-width: 220px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #fef3c7;
    box-shadow: 0 0 0 4px rgba(254, 243, 199, 0.25);
  }

  .close-btn {
    border: none;
    background: transparent;
    color: inherit;
    font-size: 24px;
    cursor: pointer;
    line-height: 1;
    transition: transform 0.2s ease, opacity 0.2s ease;

    &:hover {
      opacity: 0.8;
      transform: scale(1.05);
    }
  }
}

.chat-body {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  background: linear-gradient(180deg, rgba(255, 244, 230, 0.6), rgba(255, 251, 247, 0.95));
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.suggestion-bar {
  position: sticky;
  top: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-bottom: 12px;
  margin-bottom: 4px;
  background: linear-gradient(180deg, rgba(255, 251, 247, 0.92), rgba(255, 251, 247, 0));
  z-index: 1;
}

.suggestion-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid rgba(249, 115, 22, 0.45);
  background: rgba(255, 247, 237, 0.85);
  color: #9a3412;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover {
    background: linear-gradient(135deg, rgba(249, 115, 22, 0.15), rgba(239, 68, 68, 0.15));
    transform: translateY(-1px);
    box-shadow: 0 4px 10px rgba(239, 68, 68, 0.2);
  }

  .chip-icon {
    font-size: 14px;
  }
}

.empty-hint {
  padding: 18px;
  border-radius: 12px;
  background: rgba(254, 215, 170, 0.5);
  border: 1px dashed rgba(249, 115, 22, 0.4);
  color: #7c2d12;
  font-size: 13px;

  ul {
    padding-left: 18px;
    margin: 10px 0 0;
  }
}

.message {
  display: flex;
  align-items: flex-start;

  &.user {
    justify-content: flex-end;

    .bubble {
      background: linear-gradient(135deg, #f97316, #f97316 40%, #ef4444);
      color: #fff;
      border-bottom-right-radius: 4px;
      box-shadow: 0 12px 24px rgba(239, 68, 68, 0.25);
    }
  }

  &.assistant {
    .bubble {
      background: rgba(255, 255, 255, 0.92);
      color: #1f2937;
      border-bottom-left-radius: 4px;
      border: 1px solid rgba(248, 187, 125, 0.4);
      backdrop-filter: blur(4px);
    }
  }
}

.bubble {
  max-width: 80%;
  padding: 12px 14px;
  border-radius: 14px;
  line-height: 1.5;
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
  font-size: 13px;

  .activity {
    margin: 0 0 6px;
    font-size: 12px;
    color: #8a93a6;
  }

  a {
    color: #f97316;
    text-decoration: none;

    &:hover {
      text-decoration: underline;
    }
  }
}

.sources {
  margin-top: 10px;
  padding: 12px;
  border-radius: 12px;
  background: rgba(219, 234, 254, 0.4);
  border: 1px dashed rgba(37, 99, 235, 0.4);

  .sources-title {
    font-size: 12px;
    font-weight: 600;
    color: #1d4ed8;
  }

  ul {
    margin: 6px 0 0;
    padding-left: 0;
    list-style: none;
    font-size: 12px;
    color: #1f2937;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
}

.citation-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.75);
  border: 1px solid rgba(37, 99, 235, 0.25);

  .citation-head {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }

  .badge-law {
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(37, 99, 235, 0.12);
    color: #1d4ed8;
    font-size: 11px;
    font-weight: 600;
  }

  .badge-article {
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(220, 38, 38, 0.1);
    color: #b91c1c;
    font-size: 11px;
    font-weight: 600;
  }

  .citation-snippet {
    margin: 2px 0 0;
    font-size: 12px;
    color: #334155;
    line-height: 1.5;
  }

  .citation-source {
    font-size: 11px;
    color: #64748b;
  }
}

.disclaimer {
  margin: 10px 0 0;
  font-size: 11px;
  color: #b45309;
  line-height: 1.5;
}

.route {
  margin-top: 8px;
  font-size: 11px;
  color: #b45309;
}

.typing {
  display: inline-flex;
  align-items: center;
  gap: 4px;

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #f97316;
    animation: pulse 1.2s infinite ease-in-out;

    &:nth-child(2) {
      animation-delay: 0.2s;
    }
    &:nth-child(3) {
      animation-delay: 0.4s;
    }
  }
}

@keyframes pulse {
  0%, 80%, 100% {
    opacity: 0.4;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.chat-footer {
  padding: 12px 16px 16px;
  background: #fff;
  border-top: 1px solid rgba(248, 187, 125, 0.35);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.upload-row {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;

  .upload-button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(254, 215, 170, 0.4);
    color: #c2410c;
    font-weight: 600;
    cursor: pointer;

    .hidden {
      display: none;
    }
  }

  .upload-status {
    color: #92400e;
  }

  .reset-session {
    margin-left: auto;
    border: none;
    background: transparent;
    color: #fb923c;
    cursor: pointer;
    font-size: 12px;

    &:hover {
      color: #ea580c;
    }
  }
}

.input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;

  textarea {
    flex: 1;
    resize: none;
    border: 1px solid rgba(148, 163, 184, 0.4);
    border-radius: 12px;
    padding: 10px 12px;
    font-family: inherit;
    font-size: 13px;
    outline: none;

    &:focus {
      border-color: #f97316;
      box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.18);
    }
  }

  .send-button {
    border: none;
    border-radius: 12px;
    padding: 10px 16px;
    background: linear-gradient(135deg, #f97316, #ef4444);
    color: #fff;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.2s ease;

    &:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.pop-enter-active {
  animation: pop 0.24s ease forwards;
}
.pop-leave-active {
  animation: pop 0.2s ease reverse forwards;
}

@keyframes pop {
  0% {
    opacity: 0;
    transform: translateY(20px) scale(0.96);
  }
  100% {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}
</style>
