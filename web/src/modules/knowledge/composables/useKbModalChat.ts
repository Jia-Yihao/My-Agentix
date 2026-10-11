import { computed, onScopeDispose, ref } from 'vue'
import { streamChatResponse } from './useChatStream'
import { useChatMode } from './useChatMode'
import { postChat } from '../api/documents'
import {
  deleteConversation,
  getConversation,
  listKbConversations,
  renameConversation,
  type ConversationSummary as ApiConversationSummary,
} from '../api/conversations'
import {
  buildChatScopeBody,
  chatScopeCacheKey,
  listConversationsQueryParams,
  scopeFromConversation,
  type ChatScopePayload,
} from '../api/chatScope'
import { getApiErrorHint, isAbortError } from '../utils/errors'
import { openConversationSync } from '../utils/conversationSync'
import { useToast } from './useToast'
import type { ChatCitation } from '../api/chatTypes'

export type KbModalChatTarget = { id: string }

/**
 * 与 ChatCitation 保持同一契约（PDF 阅读器侧聊天复用 ChatMessageList 渲染引用）。
 * 原先此处重复声明了 5 个字段，导致新增的文件级元信息
 * （file_name / file_size / folder_path / file_ext / document_title）
 * 在这一侧取不到类型。改为别名，避免再次漂移。
 */
export type KbModalCitation = ChatCitation

export type KbModalMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  citations?: KbModalCitation[]
  /** 过程面板答案 id（历史回放 / 非流式兜底均可能携带） */
  answer_id?: string | null
  /** 请求追踪 id（meta 帧携带；反馈 trace_id 归因用，历史回放可能缺失） */
  trace_id?: string | null
}

export type KbConversationSummary = Pick<ApiConversationSummary, 'id' | 'title' | 'updated_at'>

function formatHistoryDate(dateStr: string) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

export function useKbModalChat(opts: {
  ensureSelectedKb: () => Promise<KbModalChatTarget | null>
  getChatScope?: () => ChatScopePayload
  onScopeRestored?: (scope: ChatScopePayload) => void
}) {
  const { chatMode } = useChatMode()
  const showChatModal = ref(false)
  const modalMessages = ref<KbModalMessage[]>([])
  const modalInput = ref('')
  const modalIsStreaming = ref(false)
  const modalStreamingAssistantId = ref<string | null>(null)
  let modalStreamCtrl: AbortController | null = null
  const modalConversationId = ref<string | null>(null)
  const modalConversations = ref<KbConversationSummary[]>([])
  const showModalHistoryPanel = ref(false)
  const modalExpanded = ref(false)
  /** kbId + 对话范围；切换 KB 或 folder/file 时置空以触发重新加载 */
  const modalLoadedScopeKey = ref<string | null>(null)
  const openConversationMenuId = ref<string | null>(null)
  /** 续聊时会话锁定范围；新会话用 getChatScope() */
  const lockedScope = ref<ChatScopePayload | null>(null)
  /** 防止异步加载历史覆盖用户正在进行的对话 */
  let conversationLoadToken = 0

  // 批次 B4：重命名/删除是**本组合式函数**发起的会话变更（useConversationChat
  // 侧无重命名入口）——经同一通道广播，其它标签页的会话列表实时合并。
const { toast } = useToast()
  const conversationSync = openConversationSync(() => {
    // 本组合式函数只发送不接收：弹层历史在打开/变更后都会重新拉取
  })

  const assignConversationId = (
    conversationId: string,
    scope: ChatScopePayload,
  ) => {
    modalConversationId.value = conversationId
    if (!lockedScope.value) {
      lockedScope.value = scope
    }
  }

  const disposeStream = () => {
    if (modalStreamCtrl) {
      modalStreamCtrl.abort()
      modalStreamCtrl = null
    }
  }

  onScopeDispose(disposeStream)
  onScopeDispose(() => conversationSync.close())

  const modalConversationGroups = computed(() => {
    const sorted = [...modalConversations.value].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
    const groups = new Map<string, KbConversationSummary[]>()
    for (const c of sorted) {
      const label = formatHistoryDate(c.updated_at)
      if (!groups.has(label)) groups.set(label, [])
      groups.get(label)!.push(c)
    }
    return Array.from(groups.entries()).map(([label, items]) => ({ label, items }))
  })

  const currentScope = (): ChatScopePayload => opts.getChatScope?.() ?? { scope_type: 'kb' }

  const buildModalScopeKey = (kbId: string) => {
    const scope = lockedScope.value ?? currentScope()
    return `${kbId}|${chatScopeCacheKey(scope)}`
  }

  const markKbSessionsStale = () => {
    conversationLoadToken += 1
    modalLoadedScopeKey.value = null
    modalConversationId.value = null
    modalMessages.value = []
    lockedScope.value = null
  }

  /** 续聊用会话锁定范围；新会话用当前浏览位置 */
  const effectiveScope = computed((): ChatScopePayload => {
    if (lockedScope.value) return lockedScope.value
    return opts.getChatScope?.() ?? { scope_type: 'kb' }
  })

  const loadModalConversations = async (kbId: string) => {
    const scopeParams = listConversationsQueryParams(effectiveScope.value)
    const res = await listKbConversations(kbId, {
      page: 1,
      page_size: 50,
      ...scopeParams,
    })
    const items = (res.data?.data?.items || []) as KbConversationSummary[]
    modalConversations.value = items
    return items
  }

  const applyConversationScope = (
    data: {
      scope_type?: string
      scope_id?: string | null
    },
    options?: { syncBrowseUi?: boolean },
  ) => {
    const scope = scopeFromConversation(data)
    lockedScope.value = scope
    if (options?.syncBrowseUi !== false) {
      opts.onScopeRestored?.(scope)
    }
  }

  const loadModalConversationMessages = async (
    conversationId: string,
    options?: { syncBrowseUi?: boolean },
  ) => {
    const loadToken = conversationLoadToken
    const res = await getConversation(conversationId)
    if (loadToken !== conversationLoadToken) return
    const data = res.data?.data
    if (data) applyConversationScope(data, options)
    const items = data?.messages || []
    modalMessages.value = items.map(
      (m: {
        id: string
        role: 'user' | 'assistant'
        content: string
        citations?: KbModalCitation[]
        answer_id?: string | null
        trace_id?: string | null
      }) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        citations: m.citations?.length ? m.citations : [],
        // 历史回放保留过程面板 id（否则刷新后"过程"面板消失）
        answer_id: m.answer_id ?? null,
        // B3：历史回放补 trace_id（后端若有则带，无则 null）
        trace_id: m.trace_id ?? null,
      }),
    )
  }

  const resolveRequestScope = (): ChatScopePayload => {
    if (lockedScope.value) return lockedScope.value
    return opts.getChatScope?.() ?? { scope_type: 'kb' }
  }

  const loadLatestConversationForReader = async () => {
    const kb = await opts.ensureSelectedKb()
    if (!kb) return
    const key = buildModalScopeKey(kb.id)
    const loadToken = conversationLoadToken
    try {
      const items = await loadModalConversations(kb.id)
      if (loadToken !== conversationLoadToken) return
      // 用户已在当前范围发起新提问时，不要用「最近会话」覆盖进行中的对话
      if (modalIsStreaming.value || modalMessages.value.length > 0) {
        modalLoadedScopeKey.value = key
        // 本地已有消息但尚未绑定会话 id 时，尽量挂上最近一条以便续聊
        if (!modalConversationId.value) {
          if (items[0]?.id) {
            assignConversationId(items[0].id, resolveRequestScope())
          }
        }
        return
      }
      modalLoadedScopeKey.value = key
      if (items[0]?.id) {
        modalConversationId.value = items[0].id
        await loadModalConversationMessages(items[0].id)
      } else {
        modalConversationId.value = null
        modalMessages.value = []
        lockedScope.value = null
      }
    } catch {
      if (loadToken !== conversationLoadToken) return
      modalLoadedScopeKey.value = key
      if (!modalIsStreaming.value && modalMessages.value.length === 0) {
        modalConversationId.value = null
        modalMessages.value = []
        lockedScope.value = null
      }
    }
  }

  const openModalConversation = async (conversationId: string) => {
    modalConversationId.value = conversationId
    await loadModalConversationMessages(conversationId)
  }

  /** PDF 阅读器侧边栏：打开指定会话并锁定范围 */
  const openReaderWithConversation = async (conversationId: string) => {
    await openModalConversation(conversationId)
    const kb = await opts.ensureSelectedKb()
    if (kb) modalLoadedScopeKey.value = buildModalScopeKey(kb.id)
  }

  const openChatModalFromInput = async () => {
    showChatModal.value = true
    const kb = await opts.ensureSelectedKb()
    if (!kb) return
    const key = buildModalScopeKey(kb.id)
    if (
      modalLoadedScopeKey.value !== key ||
      (!modalConversationId.value && modalMessages.value.length === 0)
    ) {
      await loadLatestConversationForReader()
    }
  }

  const newModalConversation = () => {
    modalConversationId.value = null
    modalMessages.value = []
    lockedScope.value = null
    openConversationMenuId.value = null
  }

  const toggleModalHistory = () => {
    showModalHistoryPanel.value = !showModalHistoryPanel.value
  }

  const toggleModalExpand = () => {
    modalExpanded.value = !modalExpanded.value
  }

  const toggleConversationMenu = (conversationId: string) => {
    openConversationMenuId.value =
      openConversationMenuId.value === conversationId ? null : conversationId
  }

  const renameModalConversation = async (c: {
    id: string
    title?: string | null
    updated_at: string
  }) => {
    const nextTitle = prompt('请输入新的会话名称', c.title || '新会话')?.trim()
    openConversationMenuId.value = null
    if (!nextTitle) return
    try {
      await renameConversation(c.id, nextTitle)
      // 批次 B4：广播重命名，其它标签页列表实时合并标题
      conversationSync.post({ type: 'rename', id: c.id, title: nextTitle })
      const kb = await opts.ensureSelectedKb()
      if (kb) await loadModalConversations(kb.id)
    } catch (error) {
      console.error('Rename conversation failed:', error)
      toast('重命名失败', { type: 'error' })
    }
  }

  const deleteModalConversation = async (conversationId: string) => {
    if (!confirm('确定删除该会话吗？')) return
    openConversationMenuId.value = null
    try {
      await deleteConversation(conversationId)
      // 批次 B4：广播删除，其它标签页的会话列表实时移除
      conversationSync.post({ type: 'delete', id: conversationId })
      if (modalConversationId.value === conversationId) {
        modalConversationId.value = null
        modalMessages.value = []
        lockedScope.value = null
      }
      const kb = await opts.ensureSelectedKb()
      if (kb) {
        const items = await loadModalConversations(kb.id)
        if (!modalConversationId.value && items[0]?.id) {
          modalConversationId.value = items[0].id
          await loadModalConversationMessages(items[0].id)
        }
      }
    } catch (error) {
      console.error('Delete conversation failed:', error)
      toast('删除会话失败', { type: 'error' })
    }
  }

  const askInModal = async (rawQuestion: string) => {
    if (modalIsStreaming.value) return

    const kb = await opts.ensureSelectedKb()
    if (!kb) {
      toast('个人知识库初始化失败，请检查后端服务', { type: 'error' })
      return
    }
    const userId = `m-u-${Date.now()}`
    const aiId = `m-a-${Date.now()}`
    modalMessages.value.push(
      { id: userId, role: 'user', content: rawQuestion },
      { id: aiId, role: 'assistant', content: '' },
    )
    modalIsStreaming.value = true
    modalStreamingAssistantId.value = aiId
    const scope = resolveRequestScope()
    const scopeBody = buildChatScopeBody(scope)
    const conversationIdForRequest = modalConversationId.value ?? undefined
    try {
      disposeStream()
      modalStreamCtrl = new AbortController()
      let streamOk = false
      // 是否已收到服务端帧（meta/任意事件）。只有「未触达业务」的失败才允许
      // 非流式兜底——原实现在流中途截断时用同一 conversation_id 再问一遍，
      // 服务端落第二条 user 消息 + 前端 += 拼接（残段 + 二次全文），审查 M2。
      let serverResponded = false
      try {
        const streamResult = await streamChatResponse({
          body: {
            kb_id: kb.id,
            question: rawQuestion,
            conversation_id: modalConversationId.value || conversationIdForRequest,
            ...scopeBody,
            chat_mode: chatMode.value,
          },
          signal: modalStreamCtrl.signal,
          assistantMsgId: aiId,
          inlineError: false,
          getAssistantMsg: () => modalMessages.value.find((m) => m.id === aiId),
          onFirstPayload: () => {
            serverResponded = true
          },
          onMeta: (data) => {
            if (data.conversation_id) {
              assignConversationId(data.conversation_id, scope)
            }
          },
          onFinished: (data) => {
            streamOk = true
            if (data.conversation_id) {
              assignConversationId(data.conversation_id, scope)
            }
          },
        })
        if (streamResult === 'unauthorized') {
          const msg = modalMessages.value.find((m) => m.id === aiId)
          if (msg) msg.content = '登录已失效，请重新登录'
          modalIsStreaming.value = false
          return
        }
        streamOk = streamResult === 'ok'
      } catch (streamErr) {
        if (isAbortError(streamErr)) throw streamErr
        if (serverResponded) {
          // 服务端已应答后的失败：不重试不兜底（同一问题二次执行），上屏错误
          throw streamErr
        }
        // 未触达业务（HTTP/网络层）→ 走下方非流式兜底（审查 M2 修复：原二连
        // attempt 循环对已应答的流重复提问，死代码 getApiErrorHint 分支永假）
        console.warn('stream failed before server responded, fallback to postChat', streamErr)
      }

      let canRefresh = streamOk
      if (!streamOk && !serverResponded) {
        const res = await postChat({
          kb_id: kb.id,
          question: rawQuestion,
          conversation_id: modalConversationId.value || conversationIdForRequest,
          ...scopeBody,
          chat_mode: chatMode.value,
        })
        const msg = modalMessages.value.find((m) => m.id === aiId)
        if (msg) {
          msg.content = res.data?.data?.answer || '暂无回答'
          msg.citations = (res.data?.data?.citations as KbModalCitation[] | undefined) ?? []
          // 非流式兜底回答同样携带 answer_id（可展开"过程"面板）
          msg.answer_id = res.data?.data?.answer_id ?? null
        }
        if (res.data?.data?.conversation_id) {
          assignConversationId(res.data.data.conversation_id, scope)
        }
        canRefresh = true
      } else if (streamOk && !modalConversationId.value) {
        const items = await loadModalConversations(kb.id)
        if (items[0]?.id) {
          assignConversationId(items[0].id, scope)
        }
      }
      // 流成功后的列表/消息刷新：失败只记日志——原实现与发送同在一个 try，
      // 刷新一失败就落进 catch 把**刚流式完成的正确答案**覆盖成错误文案（M2c）
      if (canRefresh) {
        try {
          await loadModalConversations(kb.id)
          if (modalConversationId.value) {
            await loadModalConversationMessages(modalConversationId.value, { syncBrowseUi: false })
          }
        } catch (refreshErr) {
          console.error('refresh after ask failed (answer preserved):', refreshErr)
        }
      }
    } catch (error) {
      if (isAbortError(error)) return
      console.error('Ask in modal failed:', error)
      const msg = modalMessages.value.find((m) => m.id === aiId)
      if (msg) {
        msg.content = `提示：${getApiErrorHint(error, '请求失败，请稍后重试')}`
      }
    } finally {
      modalIsStreaming.value = false
      modalStreamingAssistantId.value = null
      modalStreamCtrl = null
    }
  }

  const sendModalQuestion = async () => {
    const q = modalInput.value.trim()
    if (!q || modalIsStreaming.value) return
    modalInput.value = ''
    await askInModal(q)
  }

  /** W6 B7：用户主动停止 modal 流式（保留部分回答；finally 归位 streaming 态） */
  const stopModalGeneration = () => {
    if (!modalIsStreaming.value) return false
    disposeStream()
    return true
  }

  /** W6 B11：重新生成——重发 modal 末轮用户问题 */
  const regenerateModal = async () => {
    if (modalIsStreaming.value) return
    const lastUser = [...modalMessages.value].reverse().find((m) => m.role === 'user')
    if (!lastUser?.content) return
    await askInModal(lastUser.content)
  }

  const closeChatModal = () => {
    showChatModal.value = false
    showModalHistoryPanel.value = false
    modalExpanded.value = false
    openConversationMenuId.value = null
    disposeStream()
  }

  return {
    showChatModal,
    modalMessages,
    modalInput,
    modalIsStreaming,
    modalStreamingAssistantId,
    modalConversationId,
    modalConversations,
    showModalHistoryPanel,
    modalExpanded,
    modalLoadedScopeKey,
    openConversationMenuId,
    effectiveScope,
    modalConversationGroups,
    markKbSessionsStale,
    loadLatestConversationForReader,
    openModalConversation,
    openReaderWithConversation,
    openChatModalFromInput,
    newModalConversation,
    toggleModalHistory,
    toggleModalExpand,
    toggleConversationMenu,
    renameModalConversation,
    deleteModalConversation,
    askInModal,
    sendModalQuestion,
    stopModalGeneration,
    regenerateModal,
    closeChatModal,
  }
}
