import { computed, getCurrentScope, onScopeDispose, ref, type Ref } from 'vue'

import { deleteConversation, getConversation, listConversationsPaged } from '../api/conversations'
import {
  buildChatScopeBody,
  scopeFromConversation,
  scopeInputPlaceholder,
  type ChatScopePayload,
} from '../api/chatScope'
import { listKnowledgeBases } from '../api/kb'
import type { ChatModalMessage } from '../components/ChatMessageList.vue'
import type { ChatMessage, ConversationItem } from '../types/conversation'
import { isAbortError } from '../utils/errors'
import { setOpenFileChat } from '../utils/openFileChat'
import {
  openConversationSync,
  type ConversationSyncEvent,
} from '../utils/conversationSync'
import { useChatMode } from './useChatMode'
import { useChatStream } from './useChatStream'

export type KbOption = {
  id: string
  name: string
  type: string
}

export type ConversationChatMode = 'home' | 'history'

export type UseConversationChatOptions = {
  mode: ConversationChatMode
  pageSize?: number
  /** 加载列表后若无选中会话，自动打开第一条 */
  autoSelectFirst?: boolean
  /** 再次点击当前会话时取消选中（仅 home） */
  toggleDeselect?: boolean
  /** 强制滚底（切会话 / 发送新消息） */
  onScroll?: () => void
  /** W6 A2：流式增量滚动——仅贴底时跟随（上翻阅读不打扰）；缺省回落 onScroll */
  onChunkScroll?: () => void
  onFileScopeNavigate?: () => void
}

export function useConversationChat(options: UseConversationChatOptions) {
  const {
    mode,
    pageSize = mode === 'home' ? 20 : 50,
    autoSelectFirst = mode === 'history',
    toggleDeselect = mode === 'home',
    onScroll,
    onChunkScroll,
    onFileScopeNavigate,
  } = options

  const { chatMode } = useChatMode()
  const { isStreaming, streamingAssistantId, sendChatStream, abort: abortChatStream } = useChatStream()

  const conversations = ref<ConversationItem[]>([])
  const currentConversation = ref<ConversationItem | null>(null)
  const messages = ref<ChatMessage[]>([])
  const activeScope = ref<ChatScopePayload>({ scope_type: 'kb' })
  const errorText = ref('')
  const kbOptions = ref<KbOption[]>([])
  const selectedKb = ref<KbOption | null>(null)
  const draftConversationId = ref<string | null>(null)

  // W6 A1：映射引用恒定——非流式消息投影对象跨帧复用（结构/成员变化才重建），
  // 流式消息字段原地同步；props 浅比较与 v-for item 引用不再每帧全量翻新。
  // 对照基线：原实现每 SSE 帧 `.map` 出一批全新对象（含全部历史消息）。
  let projected: ChatModalMessage[] = []
  let projectedSrc: ChatMessage[] = []
  const displayMessages = computed((): ChatModalMessage[] => {
    const src = messages.value
    const structurallySame =
      projected.length === src.length &&
      projectedSrc.length === src.length &&
      projectedSrc.every((m, i) => m === src[i])
    if (!structurallySame) {
      projectedSrc = [...src]
      projected = src.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        thinking: m.thinking,
        citations: m.citations,
        // 透传流式期间产生的进度/轨迹/过程面板字段（否则 Home/History 页永不显示）
        progress: m.progress,
        answer_id: m.answer_id,
        // B3：反馈 trace_id 归因列数据源透传
        trace_id: m.trace_id,
        trace: m.trace,
        toolActivity: m.toolActivity,
        toolHistory: m.toolHistory,
        timing: m.timing,
      }))
      return projected
    }
    // 同结构：字段原地同步（同 id 对象引用保持不变）
    for (let i = 0; i < src.length; i++) {
      const m = src[i]
      const p = projected[i]
      if (
        p.content !== m.content ||
        p.thinking !== m.thinking ||
        p.citations !== m.citations ||
        p.progress !== m.progress ||
        p.answer_id !== m.answer_id ||
        p.trace_id !== m.trace_id ||
        p.trace !== m.trace ||
        p.toolActivity !== m.toolActivity ||
        p.toolHistory !== m.toolHistory ||
        p.timing !== m.timing
      ) {
        p.content = m.content
        p.thinking = m.thinking
        p.citations = m.citations
        p.progress = m.progress
        p.answer_id = m.answer_id
        p.trace_id = m.trace_id
        p.trace = m.trace
        p.toolActivity = m.toolActivity
        p.toolHistory = m.toolHistory
        p.timing = m.timing
      }
    }
    return projected
  })

  const chatInputPlaceholder = computed(() => scopeInputPlaceholder(activeScope.value))
  const chatKbId = computed(
    () => currentConversation.value?.kb_id ?? selectedKb.value?.id ?? null,
  )

  const scroll = () => {
    onScroll?.()
  }

  /** 流式增量滚动：优先贴底跟随（W6 A2），未接线时回落 force scroll */
  const chunkScroll = () => {
    if (onChunkScroll) onChunkScroll()
    else onScroll?.()
  }

  // 会话切换的请求序号（接口审查批次 2026-09-28）：连点 A→B 时 A 的响应可能
  // 后到，原实现无守卫 → 消息区显示 A、标题显示 B（串台）；A 失败还会把
  // 已加载的 B 清空。与 useKbModalChat 的 conversationLoadToken 同款范式。
  let selectToken = 0

  const applyActiveScope = (data: { scope_type?: string; scope_id?: string | null }) => {
    activeScope.value = scopeFromConversation(data)
  }

  const syncSelectedKb = (kbId?: string) => {
    if (!kbId) return
    const match = kbOptions.value.find((k) => k.id === kbId)
    if (match) selectedKb.value = match
  }

  const clearSelectedConversation = () => {
    selectToken++ // 使在途的 getConversation 响应过期（防串台）
    if (isStreaming.value) abortChatStream() // 离开会话上下文即中断进行中的流
    currentConversation.value = null
    messages.value = []
    activeScope.value = { scope_type: 'kb' }
  }

  // 批次 B4（audit §2 B4）：跨标签页同步——本标签页的新建/删除广播给其它
  // 标签页（重命名在 useKbModalChat 发出，同一通道），并把收到的事件**合并**
  // 进本地列表：保留 local- 草稿与分页状态，不做整表覆盖。
  const applyRemoteConversationEvent = (event: ConversationSyncEvent) => {
    if (event.type === 'create') {
      const idx = conversations.value.findIndex((c) => c.id === event.item.id)
      if (idx >= 0) conversations.value.splice(idx, 1, event.item)
      else conversations.value = [event.item, ...conversations.value]
      return
    }
    if (event.type === 'rename') {
      conversations.value = conversations.value.map((c) =>
        c.id === event.id ? { ...c, title: event.title } : c,
      )
      if (currentConversation.value?.id === event.id) {
        currentConversation.value = { ...currentConversation.value, title: event.title }
      }
      return
    }
    conversations.value = conversations.value.filter((c) => c.id !== event.id)
    if (draftConversationId.value === event.id) draftConversationId.value = null
    if (currentConversation.value?.id === event.id) clearSelectedConversation()
  }
  const conversationSync = openConversationSync(applyRemoteConversationEvent)
  // 组合式函数可能在无活动作用域下被调用（测试/工具脚本）：仅在作用域内注册回收
  if (getCurrentScope()) onScopeDispose(() => conversationSync.close())

  const ensureDraftConversation = (): ConversationItem => {
    if (draftConversationId.value) {
      const hit = conversations.value.find((c) => c.id === draftConversationId.value) || null
      if (hit) return hit
    }
    const id = `local-${Date.now()}`
    const draft: ConversationItem = {
      id,
      title: mode === 'history' ? '新对话' : '',
      updated_at: new Date().toISOString(),
      kb_id: selectedKb.value?.id,
    }
    draftConversationId.value = id
    conversations.value = [draft, ...conversations.value]
    return draft
  }

  const redirectFileScopeIfNeeded = (
    item: ConversationItem,
    data: { kb_id?: string } | undefined,
  ): boolean => {
    const scope = activeScope.value
    if (scope.scope_type !== 'file' || !scope.document_id) return false
    const kbId = data?.kb_id || item.kb_id
    if (!kbId) return false
    setOpenFileChat({
      conversationId: item.id,
      documentId: scope.document_id,
      kbId,
    })
    onFileScopeNavigate?.()
    return true
  }

  const selectConversation = async (item: ConversationItem) => {
    if (toggleDeselect && currentConversation.value?.id === item.id) {
      clearSelectedConversation()
      return
    }

    // 切换到别的会话时先中断进行中的流：否则旧流的回调会写「当前会话」
    // （侧栏高亮跳回草稿）、isStreaming 卡死新会话输入框、token 进度静默丢失
    if (currentConversation.value?.id !== item.id && isStreaming.value) {
      abortChatStream()
    }
    const token = ++selectToken

    currentConversation.value = item
    applyActiveScope(item)
    syncSelectedKb(item.kb_id)

    if (item.id.startsWith('local-')) {
      activeScope.value = { scope_type: 'kb' }
      messages.value = []
      return
    }

    try {
      const res = await getConversation(item.id)
      // 期间已切换到别的会话/取消选中：丢弃过期响应，避免消息串台
      if (token !== selectToken) return
      const data = res.data?.data
      if (data) {
        applyActiveScope(data)
        syncSelectedKb(data.kb_id)
        if (data.kb_id && currentConversation.value) {
          currentConversation.value = { ...currentConversation.value, kb_id: data.kb_id }
        }
      }
      if (redirectFileScopeIfNeeded(item, data)) return
      messages.value = data?.messages || []
      queueMicrotask(scroll)
    } catch (e) {
      if (token !== selectToken) return
      console.error('加载会话详情失败:', e)
      messages.value = []
    }
  }

  const handleDeleteConversation = async (item: ConversationItem) => {
    if (item.id.startsWith('local-')) {
      conversations.value = conversations.value.filter((c) => c.id !== item.id)
      if (draftConversationId.value === item.id) draftConversationId.value = null
      if (currentConversation.value?.id === item.id) clearSelectedConversation()
      return
    }

    try {
      await deleteConversation(item.id)
    } catch (e) {
      console.error('删除会话失败:', e)
      errorText.value = '删除失败，请稍后重试'
      return
    }

    conversations.value = conversations.value.filter((c) => c.id !== item.id)
    if (currentConversation.value?.id === item.id) {
      clearSelectedConversation()
    }
    // 批次 B4：广播删除（local- 草稿在上方已 return——它仅存在于本标签页）
    conversationSync.post({ type: 'delete', id: item.id })
  }

  const loadKnowledgeBases = async () => {
    try {
      const items = await listKnowledgeBases()
      kbOptions.value = items.map((kb) => ({
        id: kb.id,
        name: kb.name,
        type: '个人知识库',
      }))
    } catch (e) {
      console.error('加载知识库失败:', e)
    }
  }

  const loadConversations = async () => {
    try {
      const res = await listConversationsPaged({ page: 1, page_size: pageSize })
      const serverItems: ConversationItem[] = res.data?.data?.items || []
      const drafts = conversations.value.filter((c) => c.id.startsWith('local-'))
      conversations.value = [
        ...drafts,
        ...serverItems.filter((s) => !drafts.some((d) => d.id === s.id)),
      ]
      if (autoSelectFirst && !currentConversation.value && conversations.value.length > 0) {
        await selectConversation(conversations.value[0])
      }
    } catch (e) {
      console.error('加载对话失败:', e)
    }
  }

  const resolveConversationForSend = (): ConversationItem | null => {
    if (currentConversation.value) return currentConversation.value
    // 统一就地建草稿（home/history 同款）。history 原先返回 null → 消息被推进
    // 一个被「请选择一条问答历史」空态占位**隐藏**的列表 = 静默失败。
    const draft = ensureDraftConversation()
    currentConversation.value = draft
    return draft
  }

  const applyHomeMeta = (conv: ConversationItem, conversationId: string, question: string) => {
    if (!draftConversationId.value || conv.id !== draftConversationId.value) return
    const idx = conversations.value.findIndex((c) => c.id === conv.id)
    if (idx < 0) return
    const titleLimit = 24
    const title = question.length > titleLimit ? `${question.slice(0, titleLimit)}…` : question
    const updated: ConversationItem = {
      ...conversations.value[idx],
      id: conversationId,
      title: conversations.value[idx].title || title,
      updated_at: new Date().toISOString(),
      kb_id: selectedKb.value?.id,
    }
    conversations.value.splice(idx, 1, updated)
    currentConversation.value = updated
    draftConversationId.value = null
    // 批次 B4：草稿落库为真实会话——广播给其它标签页
    conversationSync.post({ type: 'create', item: updated })
  }

  const applyHistoryMeta = (conversationId: string, question: string, kbId: string) => {
    if (currentConversation.value && !currentConversation.value.id.startsWith('local-')) return
    const titleLimit = 30
    const title = question.length > titleLimit ? `${question.slice(0, titleLimit)}…` : question
    const conv: ConversationItem = {
      id: conversationId,
      title,
      updated_at: new Date().toISOString(),
      kb_id: kbId,
    }
    conversations.value = [
      conv,
      ...conversations.value.filter(
        (c) => !c.id.startsWith('local-') && c.id !== conversationId,
      ),
    ]
    currentConversation.value = conv
    // 批次 B4：首条消息新建会话——广播给其它标签页
    conversationSync.post({ type: 'create', item: conv })
  }

  const sendMessage = async (question: string, options: { kbId?: string } = {}) => {
    const kbId = options.kbId || chatKbId.value
    const q = question.trim()
    if (!kbId || !q || isStreaming.value) return false

    errorText.value = ''
    const conv = resolveConversationForSend()
    const conversationId = conv?.id.startsWith('local-') ? undefined : conv?.id

    const userTempId = `u-${Date.now()}`
    const aiTempId = `a-${Date.now()}`
    messages.value.push(
      { id: userTempId, role: 'user', content: q, created_at: new Date().toISOString() },
      { id: aiTempId, role: 'assistant', content: '', created_at: new Date().toISOString() },
    )
    queueMicrotask(scroll)

    try {
      const result = await sendChatStream({
        body: {
          kb_id: kbId,
          question: q,
          conversation_id: conversationId,
          ...buildChatScopeBody(activeScope.value),
          chat_mode: chatMode.value,
        },
        assistantMsgId: aiTempId,
        getAssistantMsg: () => messages.value.find((m) => m.id === aiTempId),
        inlineError: mode === 'history' ? false : true,
        onUnauthorized: () => {
          messages.value = messages.value.filter((m) => m.id !== userTempId && m.id !== aiTempId)
        },
        onMeta: (payload) => {
          if (!payload.conversation_id) return
          // 已切换会话（本地临时消息被移除）：丢弃过期 meta，防高亮跳回旧会话
          if (!messages.value.some((m) => m.id === userTempId)) return
          if (mode === 'home' && conv) {
            applyHomeMeta(conv, payload.conversation_id, q)
            return
          }
          applyHistoryMeta(payload.conversation_id, q, kbId)
        },
        onChunk: chunkScroll,
        onFinished: (payload) => {
          if (!messages.value.some((m) => m.id === userTempId)) return
          if (
            mode === 'history' &&
            payload.conversation_id &&
            currentConversation.value?.id.startsWith('local-')
          ) {
            currentConversation.value = {
              ...currentConversation.value,
              id: payload.conversation_id,
            }
          }
        },
      })

      if (result === 'unauthorized') return false

      if (mode === 'history' && currentConversation.value && !currentConversation.value.id.startsWith('local-')) {
        void loadConversations()
      }
      return true
    } catch (e) {
      if (isAbortError(e)) return false
      console.error('发送失败:', e)
      const msg = e instanceof Error ? e.message : '发送失败，请稍后重试'
      // 同步到 banner（history 空态/会话校验类错误原先完全静默：错误帧不带
      // conversation_id → 会话建不出来 → 消息区被空态占位隐藏）
      errorText.value = msg
      const aiMsg = messages.value.find((m) => m.id === aiTempId)
      if (aiMsg) aiMsg.content = `提示：${msg}`
      return false
    }
  }

  const newChat = () => {
    errorText.value = ''
    if (mode === 'history') {
      // 复用已有本地草稿：连点 N 次不再留下 N 个幽灵"新对话"（审查 L3）
      const existingDraft = conversations.value.find((c) => c.id.startsWith('local-'))
      const draft: ConversationItem = existingDraft ?? {
        id: `local-${Date.now()}`,
        title: '新对话',
        updated_at: new Date().toISOString(),
        kb_id: selectedKb.value?.id || '',
      }
      if (!existingDraft) conversations.value = [draft, ...conversations.value]
      currentConversation.value = draft
      selectToken++ // 使在途的历史加载响应过期
      if (isStreaming.value) abortChatStream()
      messages.value = []
      activeScope.value = { scope_type: 'kb' }
      return
    }
    draftConversationId.value = null
    clearSelectedConversation()
  }

  /**
   * W6 B7：用户主动停止生成——中断流并保留已生成的部分回答
   * （流截断路径由 useChatStream 兜底 flush 缓冲 + 标注，不覆盖正文）。
   * 返回是否确有在途流（无流时调用方不应弹「已停止」toast）。
   */
  const stopGeneration = (): boolean => {
    if (!isStreaming.value) return false
    abortChatStream()
    return true
  }

  /**
   * W6 B11：重新生成——取**最后一条用户消息**重发（等价追问同一问题，
   * 服务端按新版本出答案；当前助手回复保留在历史中由版本机制处理）。
   */
  const regenerate = async (): Promise<boolean> => {
    if (isStreaming.value) return false
    for (let i = messages.value.length - 1; i >= 0; i--) {
      const m = messages.value[i]
      if (m.role === 'user' && m.content.trim()) {
        return sendMessage(m.content)
      }
    }
    return false
  }

  const filterConversations = (searchText: Ref<string> | string) => {
    const keyword = (typeof searchText === 'string' ? searchText : searchText.value).trim().toLowerCase()
    if (!keyword) return conversations.value
    return conversations.value.filter((c) => (c.title || '').toLowerCase().includes(keyword))
  }

  const openConversationById = async (id: string | null | undefined) => {
    if (!id) return
    const hit = conversations.value.find((c) => c.id === id) || null
    if (hit) {
      await selectConversation(hit)
      return
    }
    if (id.startsWith('local-')) return
    // 深链/路由指向非第一页的会话（loadConversations 只拉 page=1）：原实现
    // 找不到就什么都不做 → URL 说的会话与展示的会话静默不一致。直接按 id
    // 拉取并插入列表后再选中。
    try {
      const res = await getConversation(id)
      const data = res.data?.data
      if (!data) return
      const conv: ConversationItem = {
        id,
        title: data.title || '会话详情',
        updated_at: data.updated_at || new Date().toISOString(),
        kb_id: data.kb_id,
        scope_type: data.scope_type,
        scope_id: data.scope_id,
      }
      conversations.value = [conv, ...conversations.value.filter((c) => c.id !== id)]
      await selectConversation(conv)
    } catch (e) {
      console.error('打开会话失败:', e)
      errorText.value = '打开该会话失败，请稍后重试'
    }
  }

  return {
    conversations,
    currentConversation,
    messages,
    activeScope,
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
    clearSelectedConversation,
    sendMessage,
    newChat,
    filterConversations,
    openConversationById,
    abortChatStream,
    stopGeneration,
    regenerate,
  }
}
