<template>
  <AppShell @toggle-sidebar="toggleSidebar">
    <template #nav-left>
      <span class="workspace-title">法律知识工作台</span>
    </template>
    <div class="main-container">
      <ChatSidebar
        variant="home"
        :collapsed="sidebarCollapsed"
        :conversations="filteredConversations"
        :active-conversation-id="knowledgeEnabled ? currentConversation?.id ?? null : null"
        v-model:search-text="historySearchText"
        :show-search="showHistorySearch"
        search-placeholder="搜索对话历史..."
        @new-chat="startNewChat"
        @select="selectConversationFromSidebar"
        @delete="handleDeleteConversation"
        @toggle-search="toggleHistorySearch"
        @go-kb="goKb"
        @toggle-sidebar="toggleSidebar"
      />

      <ChatPanel
        v-if="chatMessages.length > 0 || (knowledgeEnabled && currentConversation)"
        ref="chatPanelRef"
        :title="chatTitle"
        :messages="chatMessages"
        :pending-assistant-id="pendingAssistantId"
        @regenerate="onRegenerate"
      >
        <template #composer>
          <ComposerBox
            v-model="inputValue"
            :knowledge-enabled="knowledgeEnabled"
            :knowledge-toggle-disabled="isStreaming || isAgentStreaming"
            :enable-attachment="true"
            :kb-options="kbOptions"
            :selected-kb-id="selectedKb?.id ?? null"
            :uploading="documentUploading"
            :upload-progress="documentUploadProgress"
            :attachment-label="attachmentLabel"
            :attachment-accept="attachmentAccept"
            :disabled-send="!inputValue.trim() || isStreaming || isAgentStreaming || (knowledgeEnabled && !selectedKb)"
            :streaming="isStreaming || isAgentStreaming"
            :error-text="errorText"
            :show-error-banner="!!errorText"
            :placeholder="composerPlaceholder"
            @send="sendFromHome"
            @stop="onStopGeneration"
            @select-kb="selectKb"
            @attachment="uploadToCurrentKb"
            @update:knowledge-enabled="knowledgeEnabled = $event"
          />
        </template>
      </ChatPanel>

      <div v-else class="content-area">
        <HomeWelcomeHero
          :knowledge-enabled="knowledgeEnabled"
          :selected-kb-name="knowledgeEnabled ? selectedKb?.name : undefined"
          @select-prompt="inputValue = $event"
        >
          <ComposerBox
            v-model="inputValue"
            :knowledge-enabled="knowledgeEnabled"
            :knowledge-toggle-disabled="isStreaming || isAgentStreaming"
            :enable-attachment="true"
            :kb-options="kbOptions"
            :selected-kb-id="selectedKb?.id ?? null"
            :uploading="documentUploading"
            :upload-progress="documentUploadProgress"
            :attachment-label="attachmentLabel"
            :attachment-accept="attachmentAccept"
            :disabled-send="!inputValue.trim() || isStreaming || isAgentStreaming || (knowledgeEnabled && !selectedKb)"
            :streaming="isStreaming || isAgentStreaming"
            :error-text="errorText"
            :show-error-banner="!!errorText"
            :placeholder="composerPlaceholder"
            @send="sendFromHome"
            @stop="onStopGeneration"
            @select-kb="selectKb"
            @attachment="uploadToCurrentKb"
            @update:knowledge-enabled="knowledgeEnabled = $event"
          />
        </HomeWelcomeHero>
      </div>
    </div>
  </AppShell>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import axios from 'axios'

import { API_BASE_URL, apiKeyHeader } from '../../../api/config'
import { getToken } from '../../../api/token'
import { streamChat } from '../../chat/api/chatStream'
import { scopeDisplayLabel } from '../api/chatScope'
import { uploadDocument } from '../api/documents'
import AppShell from '../components/AppShell.vue'
import ChatPanel from '../components/chat/ChatPanel.vue'
import ChatSidebar from '../components/chat/ChatSidebar.vue'
import HomeWelcomeHero from '../components/chat/HomeWelcomeHero.vue'
import ComposerBox from '../components/ComposerBox.vue'
import { useAppNavigation } from '../composables/useAppNavigation'
import { type KbOption, useConversationChat } from '../composables/useConversationChat'
import { useToast } from '../composables/useToast'
import type { ChatMessage } from '../types/conversation'
import type { ChatCitation } from '../api/chatTypes'
import type { ChatModalMessage } from '../components/ChatMessageList.vue'

const { goKb } = useAppNavigation()
const { toast } = useToast()

const sidebarCollapsed = ref(false)
const knowledgeEnabled = ref(true)
const isAgentStreaming = ref(false)
const documentUploading = ref(false)
const documentUploadProgress = ref(0)
const agentAttachmentPath = ref('')
const agentAttachmentName = ref('')
const inputValue = ref('')
const agentMessages = ref<ChatMessage[]>([])
const showHistorySearch = ref(false)
const historySearchText = ref('')
const chatPanelRef = ref<InstanceType<typeof ChatPanel> | null>(null)
const agentAbortController = ref<AbortController | null>(null)
const agentSessionStorageKey = 'agentix:chat:session_id'
const agentSessionId = ref(readAgentSessionId())

const {
  currentConversation,
  activeScope,
  messages,
  errorText,
  kbOptions,
  selectedKb,
  isStreaming,
  streamingAssistantId,
  displayMessages,
  chatInputPlaceholder,
  chatKbId,
  loadKnowledgeBases,
  loadConversations,
  selectConversation,
  handleDeleteConversation,
  sendMessage,
  newChat,
  filterConversations,
  abortChatStream,
  stopGeneration,
  regenerate,
} = useConversationChat({
  mode: 'home',
  onScroll: () => chatPanelRef.value?.scrollToBottom(),
  onChunkScroll: () => chatPanelRef.value?.smartScroll(),
  onFileScopeNavigate: goKb,
})

const filteredConversations = computed(() => filterConversations(historySearchText))
const pendingAssistantId = computed(() =>
  knowledgeEnabled.value ? streamingAssistantId.value : agentStreamingMessageId.value,
)
const agentStreamingMessageId = ref<string | null>(null)
const agentDisplayMessages = computed((): ChatModalMessage[] =>
  agentMessages.value.map((message) => ({
    id: message.id,
    role: message.role,
    content: message.content,
    thinking: message.thinking,
    citations: message.citations,
    progress: message.progress,
    answer_id: message.answer_id,
    trace_id: message.trace_id,
    trace: message.trace,
    toolActivity: message.toolActivity,
    toolHistory: message.toolHistory,
    timing: message.timing,
  })),
)
const chatMessages = computed(() =>
  knowledgeEnabled.value ? displayMessages.value : agentDisplayMessages.value,
)
const chatTitle = computed(() => {
  if (!knowledgeEnabled.value) return agentMessages.value.length ? '智能助手' : ''
  return currentConversation.value
    ? `${scopeDisplayLabel(activeScope.value)} · ${currentConversation.value.title}`
    : '新建会话'
})
const composerPlaceholder = computed(() =>
  knowledgeEnabled.value ? chatInputPlaceholder.value : '向智能助手提问',
)
const attachmentAccept = computed(() =>
  knowledgeEnabled.value
    ? '.pdf,application/pdf'
    : '.txt,.md,.json,.csv,.log,.xlsx,.xls,.pdf,.doc,.docx',
)
const attachmentLabel = computed(() => {
  if (knowledgeEnabled.value) return '上传 PDF 到知识库'
  return agentAttachmentName.value ? `已附加：${agentAttachmentName.value}` : '上传助手附件'
})

const toggleHistorySearch = () => {
  showHistorySearch.value = !showHistorySearch.value
  if (!showHistorySearch.value) {
    historySearchText.value = ''
  }
}

const toggleSidebar = () => {
  sidebarCollapsed.value = !sidebarCollapsed.value
}

const selectKb = (kb: KbOption) => {
  selectedKb.value = kb
}

const selectConversationFromSidebar = async (item: Parameters<typeof selectConversation>[0]) => {
  knowledgeEnabled.value = true
  await selectConversation(item)
}

const sendFromHome = async () => {
  const q = inputValue.value.trim()
  if (!q) return
  if (knowledgeEnabled.value && !selectedKb.value) {
    toast('请先选择或创建一个知识库，或关闭知识库开关继续对话。', { type: 'error' })
    return
  }
  const usedKnowledge = knowledgeEnabled.value
  const sent = usedKnowledge
    ? await sendMessage(q, { kbId: selectedKb.value?.id })
    : await sendAgentMessage(q)
  if (sent) {
    inputValue.value = ''
  }
}

/** W6 B7：用户主动停止——保留部分回答 + toast 反馈 */
const onStopGeneration = () => {
  if (isAgentStreaming.value) {
    agentAbortController.value?.abort()
    isAgentStreaming.value = false
    agentAbortController.value = null
    toast('已停止生成')
    return
  }
  if (stopGeneration()) toast('已停止生成')
}

/** W6 B11：重新生成（末轮问题重发） */
const onRegenerate = async () => {
  if (knowledgeEnabled.value) {
    await regenerate()
    return
  }
  const lastQuestion = [...agentMessages.value].reverse().find((message) => message.role === 'user')
  if (lastQuestion) await sendAgentMessage(lastQuestion.content)
}

const uploadToCurrentKb = async (file: File) => {
  if (!knowledgeEnabled.value) {
    await uploadAgentAttachment(file)
    return
  }
  const kbId = selectedKb.value?.id
  if (!kbId) {
    toast('请先创建并选择一个知识库，再上传文件。', { type: 'error' })
    return
  }
  if (documentUploading.value) return

  const formData = new FormData()
  formData.append('file', file)
  documentUploading.value = true
  documentUploadProgress.value = 0
  try {
    await uploadDocument(formData, {
      params: { kb_id: kbId },
      onUploadProgress: (progress) => {
        documentUploadProgress.value = progress
      },
    })
    toast('文件已上传并提交索引，处理完成后即可在知识库问答中检索。', { type: 'success' })
  } catch (error: unknown) {
    const detail = axios.isAxiosError(error)
      ? (error.response?.data as { detail?: unknown } | undefined)?.detail
      : error instanceof Error
        ? error.message
        : undefined
    toast(
      typeof detail === 'string' && detail.trim()
        ? `文件上传失败：${detail}`
        : '文件上传失败，请检查文件格式和网络后重试。',
      { type: 'error' },
    )
  } finally {
    documentUploading.value = false
    documentUploadProgress.value = 0
  }
}

const uploadAgentAttachment = async (file: File) => {
  if (documentUploading.value) return
  const formData = new FormData()
  formData.append('file', file)
  documentUploading.value = true
  documentUploadProgress.value = 0
  try {
    const token = getToken()
    const { data } = await axios.post<{
      success?: boolean
      file_path?: string
      original_name?: string
      detail?: string
    }>(`${API_BASE_URL}/api/v1/upload/file`, formData, {
      headers: { ...apiKeyHeader(), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      withCredentials: true,
      onUploadProgress: (event) => {
        if (event.total) {
          documentUploadProgress.value = Math.min(100, Math.round((event.loaded / event.total) * 100))
        }
      },
    })
    if (!data.success || !data.file_path) {
      throw new Error(data.detail || '服务端未返回附件路径')
    }
    agentAttachmentPath.value = data.file_path
    agentAttachmentName.value = data.original_name || file.name
    toast(`已附加文件：${agentAttachmentName.value}`, { type: 'success' })
  } catch (error: unknown) {
    const detail = axios.isAxiosError(error)
      ? (error.response?.data as { detail?: unknown } | undefined)?.detail
      : error instanceof Error
        ? error.message
        : undefined
    toast(
      typeof detail === 'string' && detail.trim()
        ? `附件上传失败：${detail}`
        : '附件上传失败，请检查文件格式和网络后重试。',
      { type: 'error' },
    )
  } finally {
    documentUploading.value = false
    documentUploadProgress.value = 0
  }
}

function readAgentSessionId(): string {
  try {
    return localStorage.getItem(agentSessionStorageKey) || ''
  } catch {
    return ''
  }
}

function persistAgentSessionId(sessionId: string): void {
  agentSessionId.value = sessionId
  try {
    if (sessionId) localStorage.setItem(agentSessionStorageKey, sessionId)
    else localStorage.removeItem(agentSessionStorageKey)
  } catch {
    // localStorage 不可用时，本次会话仍可继续使用内存中的 id。
  }
}

function mapAgentSource(source: unknown, index: number): ChatCitation {
  if (typeof source === 'string') {
    return { chunk_id: `agent-source-${index}`, snippet: source, source }
  }
  const item = source && typeof source === 'object' ? source as Record<string, unknown> : {}
  return {
    chunk_id: String(item.chunk_id ?? item.id ?? `agent-source-${index}`),
    document_id: item.document_id ? String(item.document_id) : undefined,
    page_no: typeof item.page_no === 'number' ? item.page_no : undefined,
    snippet: String(item.content_snippet ?? item.snippet ?? item.content ?? item.source ?? ''),
    source: String(item.source ?? item.title ?? item.law ?? '智能助手来源'),
  }
}

async function sendAgentMessage(question: string): Promise<boolean> {
  if (isAgentStreaming.value || isStreaming.value) return false
  const now = new Date().toISOString()
  const userMessage: ChatMessage = {
    id: `agent-u-${Date.now()}`,
    role: 'user',
    content: question,
    created_at: now,
  }
  const assistantMessage: ChatMessage = {
    id: `agent-a-${Date.now()}`,
    role: 'assistant',
    content: '',
    created_at: now,
  }
  agentMessages.value.push(userMessage, assistantMessage)
  // 响应性关键：后续流式回调改的是 push 进数组后的**代理对象**，而非本地原始
  // 对象——改原始对象不经过 proxy set，不触发依赖，DOM 永远停在空气泡
  // （实测 bug：Agent 流结束后回答不渲染，组件状态里内容却是全的）。
  // 与 useConversationChat 的 getAssistantMsg() 从响应式数组取代理同款范式。
  const assistantView = agentMessages.value[agentMessages.value.length - 1]!
  agentStreamingMessageId.value = assistantView.id
  isAgentStreaming.value = true
  errorText.value = ''
  const controller = new AbortController()
  agentAbortController.value = controller
  let failedMessage = ''
  try {
    await streamChat(
      {
        message: question,
        sessionId: agentSessionId.value || undefined,
        filePath: agentAttachmentPath.value || undefined,
        ingestIncremental: agentAttachmentPath.value ? true : undefined,
        signal: controller.signal,
      },
      {
        onSession: persistAgentSessionId,
        onMessage: (token) => {
          assistantView.content += token
          chatPanelRef.value?.smartScroll()
        },
        onMetadata: (chunk) => {
          const route = chunk.route ?? chunk.metadata?.route
          if (route) assistantView.trace = [{
            kind: 'stage',
            label: `智能路由：${String(route)}`,
            at: Date.now(),
          }]
        },
        onToolStart: (tool) => {
          assistantView.toolActivity = { label: `正在使用 ${tool || '工具'}` }
        },
        onToolEnd: (tool) => {
          assistantView.toolActivity = null
          assistantView.toolHistory = [...(assistantView.toolHistory ?? []), `已完成 ${tool || '工具'}`]
        },
        onDone: (sources) => {
          assistantView.citations = sources.map(mapAgentSource)
          assistantView.toolActivity = null
        },
        onError: (message) => {
          failedMessage = message
        },
      },
    )
    if (failedMessage) {
      assistantView.content = assistantView.content || `提示：${failedMessage}`
      errorText.value = failedMessage
      return false
    }
    agentAttachmentPath.value = ''
    agentAttachmentName.value = ''
    return true
  } catch (error: unknown) {
    if (controller.signal.aborted) return false
    const message = error instanceof Error ? error.message : '助手请求失败，请稍后重试'
    assistantView.content = assistantView.content || `提示：${message}`
    errorText.value = message
    return false
  } finally {
    isAgentStreaming.value = false
    agentAbortController.value = null
    agentStreamingMessageId.value = null
  }
}

const startNewChat = () => {
  if (isAgentStreaming.value) agentAbortController.value?.abort()
  persistAgentSessionId('')
  agentMessages.value = []
  agentAttachmentPath.value = ''
  agentAttachmentName.value = ''
  newChat()
}

onMounted(() => {
  void loadKnowledgeBases().then(() => {
    if (!selectedKb.value && kbOptions.value.length > 0) {
      selectedKb.value = kbOptions.value[0]
    }
    void loadConversations()
  })
  if (agentSessionId.value) {
    const token = getToken()
    void axios.get(`${API_BASE_URL}/api/v1/chat/history/${encodeURIComponent(agentSessionId.value)}`, {
      params: { limit: 100 },
      headers: { ...apiKeyHeader(), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      withCredentials: true,
    }).then(({ data }) => {
      if (agentMessages.value.length) return
      const history = Array.isArray(data) ? data : []
      agentMessages.value = history.map((item: {
        id?: number
        message_type?: string
        content?: string
        created_at?: string
        message_metadata?: Record<string, unknown> | null
      }) => ({
        id: `agent-history-${item.id ?? Date.now()}`,
        role: item.message_type === 'user_query' ? 'user' : 'assistant',
        content: item.content ?? '',
        created_at: item.created_at ?? new Date().toISOString(),
        citations: Array.isArray(item.message_metadata?.sources)
          ? (item.message_metadata.sources as unknown[]).map(mapAgentSource)
          : [],
      }))
    }).catch(() => {
      persistAgentSessionId('')
    })
  }
})

onUnmounted(() => {
  agentAbortController.value?.abort()
  abortChatStream()
})
</script>

<style scoped>
.main-container {
  display: flex;
  flex: 1;
  min-height: 0;
  background: #f6f6f4;
}

.content-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.workspace-title {
  color: #6b7280;
  font-size: 14px;
  font-weight: 550;
}
</style>
