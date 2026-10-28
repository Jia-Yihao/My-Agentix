<template>
  <div ref="listRef" class="chat-message-list" :class="variantClass">
    <div
      v-for="pad in topSpacers"
      :key="pad.kind"
      class="virtual-spacer virtual-spacer--top"
      aria-hidden="true"
      :style="{ height: `${pad.height}px` }"
    ></div>
    <!-- 批次 C1（audit §2 C1）：>200 条折叠中段——保留首 50 + 尾 150，
         中间插入可点击的折叠条；展开后保持全量渲染（新消息不再收起）。 -->
    <template v-for="(msg, idx) in listForRender" :key="msg.id">
      <div
        v-if="idx === foldBarIndex && hiddenCount > 0"
        class="message-fold-bar"
        role="button"
        tabindex="0"
        @click="foldExpanded = true"
        @keydown.enter="foldExpanded = true"
      >
        已折叠中间 {{ hiddenCount }} 条较早消息，点击展开
      </div>
      <div class="modal-message" :class="msg.role">
      <ThinkingSection
        v-if="msg.role === 'assistant'"
        :text="msg.thinking"
      />
      <!-- W6 B5：工具活动卡片（原实现写进 thinking 污染思考区）——
           流式实时显示最新活动行，终帧折叠为历史 -->
      <div
        v-if="msg.role === 'assistant' && (msg.toolActivity || msg.toolHistory?.length)"
        class="tool-card"
      >
        <div v-if="msg.toolActivity" class="tool-running">
          <span class="tool-spinner" aria-hidden="true"></span>
          <span class="tool-running-label">{{ msg.toolActivity.label }}</span>
        </div>
        <details v-if="msg.toolHistory?.length" class="tool-history">
          <summary class="tool-history-summary">工具活动（{{ msg.toolHistory.length }}）</summary>
          <div v-for="(h, i) in msg.toolHistory" :key="i" class="tool-history-item">{{ h }}</div>
        </details>
      </div>
      <!-- W6 B4：三态步骤器（准备会话 → 检索知识库 → 生成回答）——
           progress 不再被首个 token 清除，生成期间与正文并存（紧凑态） -->
      <StreamStepper
        v-if="msg.role === 'assistant' && msg.progress"
        :progress="msg.progress"
        :compact="!!msg.content"
      />
      <ChatThinkingPlaceholder
        v-else-if="isAssistantAwaitingContent(msg, pendingAssistantId)"
      />
      <!-- W6 A3：助手消息 markdown 富文本（marked + dompurify 消毒，
           绝不裸 v-html；流式行尾渲染光标 B6）；用户消息保持纯文本 -->
      <div
        v-else-if="msg.role === 'assistant'"
        class="modal-message-text modal-message-text--md"
        v-html="assistantHtml(msg)"
      ></div>
      <div v-else class="modal-message-text">{{ msg.content }}</div>
      <!-- W6 B8：耗时芯片（W0 终帧注入 duration_ms / ttft_ms） -->
      <div v-if="msg.role === 'assistant' && msg.timing" class="timing-chip">
        <span v-if="msg.timing.durationMs != null">耗时 {{ formatSeconds(msg.timing.durationMs) }}</span>
        <span v-if="msg.timing.ttftMs != null" class="timing-chip-sep">·</span>
        <span v-if="msg.timing.ttftMs != null">首字 {{ formatSeconds(msg.timing.ttftMs) }}</span>
      </div>
      <!-- W6 B11：消息操作（复制 / 重新生成） -->
      <div v-if="msg.content" class="msg-actions">
        <button type="button" class="msg-action-btn" @click="copyMessage(msg)">复制</button>
        <button
          v-if="canRegenerate(msg)"
          type="button"
          class="msg-action-btn"
          @click="emit('regenerate')"
        >重新生成</button>
        <!-- P2-1 满意度反馈（feedbackActive = prop 或探活发现；默认 false = 不渲染；
             后端 flag-off 404 时静默失败，不打断对话） -->
        <template v-if="feedbackActive && sessionId && msg.role === 'assistant'">
          <button
            type="button"
            class="msg-action-btn feedback-btn"
            :class="{ 'is-active': feedbackState[msg.id] === 'up' }"
            :disabled="feedbackState[msg.id] != null"
            aria-label="赞同这条回答"
            title="有帮助"
            @click="sendFeedback(msg, 'up')"
          >👍</button>
          <button
            type="button"
            class="msg-action-btn feedback-btn"
            :class="{ 'is-active': feedbackState[msg.id] === 'down' }"
            :disabled="feedbackState[msg.id] != null"
            aria-label="不满意这条回答"
            title="没帮助"
            @click="sendFeedback(msg, 'down')"
          >👎</button>
        </template>
      </div>
      <div v-if="msg.role === 'assistant' && msg.citations?.length" class="modal-citations">
        <div class="source-title">引用来源</div>
        <div
          v-for="c in msg.citations"
          :key="`${msg.id}-${c.chunk_id}`"
          class="source-item"
          tabindex="0"
          @mouseenter="openPreview(c, $event)"
          @mouseleave="scheduleHidePreview"
          @focusin="openPreview(c, $event)"
          @focusout="scheduleHidePreview"
        >
          <div class="source-icon">
            <IconFile />
          </div>
          <div>
            <div>
              {{ c.file_name || c.source || '文档片段' }} <span v-if="c.page_no">· 第{{ c.page_no }}页</span>
              <span
                v-if="statusOf(c)"
                class="source-status"
                :class="`is-${statusOf(c)!.tone}`"
              >{{ statusOf(c)!.label }}</span>
            </div>
            <div class="source-snippet">{{ c.snippet }}</div>
          </div>
        </div>
      </div>
      <!-- 执行轨迹时间线：客户端视角的事件序列（Trace UI v1）
           W6 B9：流式期间受控展开 + 高亮最新步骤；终帧释放受控（属性移除
           = 收起），用户可再手动展开 -->
      <details
        v-if="msg.role === 'assistant' && msg.trace?.length"
        class="trace-panel"
        :open="traceOpenAttr(msg)"
        @toggle="onTraceToggle($event, msg)"
      >
        <summary class="explain-summary">执行轨迹</summary>
        <div class="trace-body">
          <div
            v-for="(step, i) in msg.trace"
            :key="i"
            class="trace-step"
            :class="[`is-${step.kind}`, { 'is-latest': i === msg.trace!.length - 1 && isStreamingMsg(msg) }]"
          >
            <span class="trace-kind">{{ TRACE_KIND_LABELS[step.kind] }}</span>
            <span class="trace-label">{{ step.label }}</span>
            <span v-if="traceGapLabel(msg.trace!, i)" class="trace-gap">{{ traceGapLabel(msg.trace!, i) }}</span>
          </div>
        </div>
      </details>
      <!-- X3 过程面板：有 answer_id 的回答可展开查看"这条答案怎么来的" -->
      <details
        v-if="msg.role === 'assistant' && msg.answer_id"
        class="explain-panel"
        @toggle="onExplainToggle($event, msg.answer_id)"
      >
        <summary class="explain-summary">过程</summary>
        <div v-if="explainState[msg.answer_id]?.status === 'loading'" class="explain-body">
          加载中…
        </div>
        <div
          v-else-if="explainState[msg.answer_id]?.status === 'error'"
          class="explain-body explain-error"
        >
          过程信息加载失败
        </div>
        <div
          v-else-if="explainState[msg.answer_id]?.status === 'loaded'"
          class="explain-body"
        >
          <div class="explain-row"><span class="explain-key">模型</span>{{ viewOf(msg.answer_id).model_name }}</div>
          <div v-if="viewOf(msg.answer_id).query" class="explain-row">
            <span class="explain-key">检索词</span>{{ viewOf(msg.answer_id).query }}（命中
            {{ viewOf(msg.answer_id).hit_count }}/{{ viewOf(msg.answer_id).top_k }}）
          </div>
          <div v-if="viewOf(msg.answer_id).no_retrieval" class="explain-row">
            本次回答未走检索
          </div>
          <div v-for="e in viewOf(msg.answer_id).evidence" :key="e.chunk_id" class="explain-evidence">
            <div>
              {{ e.source }}
              <span v-if="e.score != null">· 相关度 {{ e.score.toFixed(2) }}</span>
              <span
                v-if="statusLabel(e.effective_status)"
                class="source-status"
                :class="`is-${statusLabel(e.effective_status)!.tone}`"
              >{{ statusLabel(e.effective_status)!.label }}</span>
            </div>
            <div class="source-snippet">{{ e.content_preview }}</div>
          </div>
          <div v-if="viewOf(msg.answer_id).not_recorded.length" class="explain-notrecorded">
            未记录：{{ viewOf(msg.answer_id).not_recorded.join('、') }}
          </div>
        </div>
        </details>
      </div>
    </template>
    <div
      v-for="pad in bottomSpacers"
      :key="pad.kind"
      class="virtual-spacer virtual-spacer--bottom"
      aria-hidden="true"
      :style="{ height: `${pad.height}px` }"
    ></div>
    <CitationPreviewCard
      :citation="citationPreview?.citation ?? null"
      :anchor="citationPreview?.anchor ?? null"
      @pointer-enter="cancelHidePreview"
      @pointer-leave="scheduleHidePreview"
      @dismiss="hidePreview"
    />
  </div>
</template>

<script setup lang="ts">
/**
 * 消息列表渲染组件（页面态 / 弹窗态共用）。
 *
 * T14 虚拟滚动：`virtual` prop **默认 false** ⇒ 零行为变化——模板里虚拟分支的两块
 * 占位节点不渲染、`listForRender` 就是 `displayMessages` 本身（同一引用，全量渲染）、
 * 折叠条下标恒为 `FOLD_HEAD_KEEP`，因此 flag-off 的 DOM 与基线逐位一致
 * （`AGENTS.md` 硬性纪律 1 的前端对应物）。只有装配方显式传 `virtual` 才走窗口渲染。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'

import ChatThinkingPlaceholder from './ChatThinkingPlaceholder.vue'
import CitationPreviewCard from './CitationPreviewCard.vue'
import IconFile from './icons/IconFile.vue'
import StreamStepper from './StreamStepper.vue'
import ThinkingSection from './ThinkingSection.vue'
import { isAssistantAwaitingContent } from '../utils/chatPending'
import { probeFeedbackEnabled, submitFeedback } from '../../../api/feedback'
import { effectiveStatusView } from '../utils/effectiveStatus'
import { buildExplainView, type ExplainView } from '../utils/explainSummary'
import { memoRenderMarkdown } from '../utils/markdown'
import { TRACE_KIND_LABELS, traceGapLabel, type TraceStep } from '../utils/traceTimeline'
import { fetchAnswerExplanation } from '../api/explanations'
import type { ChatCitation } from '../api/chatTypes'
import type {
  StreamProgress,
  StreamTiming,
  StreamToolActivity,
} from '../api/chatStreamHandlers'
import { useToast } from '../composables/useToast'
import {
  DEFAULT_OVERSCAN,
  DEFAULT_ROW_HEIGHT,
  FALLBACK_VIEWPORT_HEIGHT,
  computeVirtualWindow,
} from '../composables/useVirtualWindow'

export type { ChatCitation } from '../api/chatTypes'

export type ChatModalMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  thinking?: string
  citations?: ChatCitation[]
  progress?: StreamProgress | null
  answer_id?: string | null
  /** 请求追踪 id（meta 帧携带；反馈 trace_id 归因用，B3） */
  trace_id?: string | null
  /** 执行轨迹时间线（流式过程中实时填充） */
  trace?: TraceStep[]
  /** W6 B5 工具活动卡片 */
  toolActivity?: StreamToolActivity | null
  toolHistory?: string[]
  /** W6 B8 终帧耗时 */
  timing?: StreamTiming | null
}

const props = withDefaults(
  defineProps<{
    messages: readonly ChatModalMessage[]
    pendingAssistantId?: string | null
    variant?: 'modal' | 'page'
    /** P2-1：手动覆盖开关（true 恒渲染并跳过探活；不传时挂载自动探活发现） */
    feedbackEnabled?: boolean
    /** P2-1：反馈归属的会话 ID（未传入时不渲染反馈按钮） */
    sessionId?: string
    /**
     * T14：虚拟滚动开关。**默认 false**——关闭时模板与渲染结果与基线逐位一致
     * （`AGENTS.md` 硬性纪律 1 的前端对应物：默认关的生产链路必须与基线等价）；
     * 打开时只渲染滚动窗口内的消息，窗口外用两个高度占位 div 撑起滚动条。
     */
    virtual?: boolean
  }>(),
  { variant: 'modal', feedbackEnabled: false, sessionId: '', virtual: false },
)

const emit = defineEmits<{
  /** W6 B11：重新生成（父级接 useConversationChat.regenerate） */
  (e: 'regenerate'): void
}>()

const { toast } = useToast()

// ---------------------------------------------------------------------------
// P2-1 满意度反馈：👍/👎 → POST /api/v1/feedback（静默失败）
// ---------------------------------------------------------------------------

/** 每条消息的反馈状态：已提交/提交中的 rating（null = 未交互） */
const feedbackState = reactive<Record<string, 'up' | 'down' | undefined>>({})

/**
 * 生效开关 = prop（装配方手动覆盖，恒信）或挂载时探活自动发现。
 * prop 为 true 时跳过探活（不浪费一次 GET）；
 * prop 未传时探活：flag-on → 按钮出现，flag-off/网络异常 → fail-open 不渲染。
 */
const feedbackProbed = ref(false)
const feedbackActive = computed(() => props.feedbackEnabled || feedbackProbed.value)

onMounted(() => {
  if (props.feedbackEnabled) return
  void probeFeedbackEnabled().then((ok) => {
    feedbackProbed.value = ok
  })
})

async function sendFeedback(msg: ChatModalMessage, rating: 'up' | 'down'): Promise<void> {
  // 已反馈过 / 提交中：不重复提交（一票一消息）
  if (feedbackState[msg.id] != null) return
  if (!props.sessionId) return
  feedbackState[msg.id] = rating
  // B3：带上请求追踪 id（meta 帧 request_id）供后端反馈回流评测的归因列
  const ok = await submitFeedback({
    sessionId: props.sessionId,
    messageId: msg.id,
    traceId: msg.trace_id || undefined,
    rating,
  })
  if (!ok) {
    // 静默回滚：后端 flag-off（404）或网络异常——不当错误骚扰用户
    feedbackState[msg.id] = undefined
  }
}

// ---------------------------------------------------------------------------
// W6 A3/B6：助手 markdown 富文本 + 流式光标
// ---------------------------------------------------------------------------

function isStreamingMsg(msg: ChatModalMessage): boolean {
  return !!props.pendingAssistantId && msg.id === props.pendingAssistantId
}

function assistantHtml(msg: ChatModalMessage): string {
  const html = memoRenderMarkdown(msg.content)
  // 光标只拼在 sanitize 之后的受信常量 span 上（不进消毒器）
  if (isStreamingMsg(msg) && msg.content) {
    return `${html}<span class="stream-caret" aria-hidden="true"></span>`
  }
  return html
}

// ---------------------------------------------------------------------------
// W6 B8：耗时芯片格式化（毫秒 → X.XXs / Xms）
// ---------------------------------------------------------------------------

function formatSeconds(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}

// ---------------------------------------------------------------------------
// W6 B11：复制 / 重新生成
// ---------------------------------------------------------------------------

const lastAssistantId = computed(() => {
  for (let i = props.messages.length - 1; i >= 0; i--) {
    if (props.messages[i].role === 'assistant') return props.messages[i].id
  }
  return null
})

function canRegenerate(msg: ChatModalMessage): boolean {
  // 仅最后一条助手消息可重生成，且流式期间不显示（停止后可点）
  return (
    msg.role === 'assistant' &&
    msg.id === lastAssistantId.value &&
    !props.pendingAssistantId &&
    !!msg.content
  )
}

async function copyMessage(msg: ChatModalMessage): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(msg.content)
      toast('已复制', { type: 'success' })
      return
    }
    throw new Error('clipboard unavailable')
  } catch {
    // 降级：textarea + execCommand（老浏览器 / 非安全上下文）
    try {
      const ta = document.createElement('textarea')
      ta.value = msg.content
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
      toast('已复制', { type: 'success' })
    } catch {
      toast('复制失败，请手动选择文本', { type: 'error' })
    }
  }
}

// ---------------------------------------------------------------------------
// W6 B9：执行轨迹受控展开（流式期间展开 + 高亮最新；完成后释放）
// ---------------------------------------------------------------------------

/** 流式期间用户手动收起的意图（新一轮流开始时复位） */
const traceUserClosed = ref(false)

watch(
  () => props.pendingAssistantId,
  (next, prev) => {
    if (next && next !== prev) traceUserClosed.value = false
  },
)

function traceOpenAttr(msg: ChatModalMessage): boolean | undefined {
  if (!isStreamingMsg(msg)) return undefined // 释放受控 → 原生 toggle，且移除属性=收起
  return !traceUserClosed.value
}

function onTraceToggle(event: Event, msg: ChatModalMessage): void {
  if (!isStreamingMsg(msg)) return
  traceUserClosed.value = !(event.target as HTMLDetailsElement).open
}

/** 鼠标离开锚点后延迟隐藏，留出指针移入卡片的时间 */
const HIDE_DELAY = 120

type CitationPreview = { citation: ChatCitation; anchor: DOMRect }

// 用 shallowRef：DOMRect 不需要深层响应式，整体替换即可
const citationPreview = shallowRef<CitationPreview | null>(null)

/** R2-2：来源 chip 的效力状态徽标视图（缺失/未知 → null 不渲染） */
function statusOf(c: ChatCitation) {
  return effectiveStatusView(c.effective_status)
}

// ---------------------------------------------------------------------------
// X3 过程面板：懒加载 Explanation API（展开时才请求，失败显示占位）
// ---------------------------------------------------------------------------

type ExplainEntry =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'loaded'; view: ExplainView }

const explainState = reactive<Record<string, ExplainEntry>>({})

function viewOf(answerId: string): ExplainView {
  const entry = explainState[answerId]
  return entry?.status === 'loaded'
    ? entry.view
    : {
        model_name: '',
        question: '',
        query: null,
        hit_count: null,
        top_k: null,
        evidence: [],
        no_retrieval: false,
        citation_count: 0,
        not_recorded: [],
      }
}

function statusLabel(effectiveStatus: string | null) {
  return effectiveStatusView(effectiveStatus)
}

async function onExplainToggle(event: Event, answerId: string): Promise<void> {
  const open = (event.target as HTMLDetailsElement).open
  if (!open || explainState[answerId]) return
  explainState[answerId] = { status: 'loading' }
  try {
    const payload = await fetchAnswerExplanation(answerId)
    explainState[answerId] = { status: 'loaded', view: buildExplainView(payload) }
  } catch {
    explainState[answerId] = { status: 'error' }
  }
}
let hideTimer: ReturnType<typeof setTimeout> | undefined

function cancelHidePreview(): void {
  if (hideTimer !== undefined) {
    clearTimeout(hideTimer)
    hideTimer = undefined
  }
}

function scheduleHidePreview(): void {
  cancelHidePreview()
  hideTimer = setTimeout(() => {
    hideTimer = undefined
    citationPreview.value = null
  }, HIDE_DELAY)
}

function hidePreview(): void {
  cancelHidePreview()
  citationPreview.value = null
}

function openPreview(citation: ChatCitation, event: Event): void {
  const el = event.currentTarget as HTMLElement | null
  if (!el) return
  cancelHidePreview()
  citationPreview.value = { citation, anchor: el.getBoundingClientRect() }
}

onBeforeUnmount(cancelHidePreview)

const variantClass = computed(() =>
  props.variant === 'page' ? 'chat-message-list--page' : '',
)

// ---------------------------------------------------------------------------
// 批次 C1：长会话窗口化（>200 条折叠中段，保留首 50 + 尾 150）
// ---------------------------------------------------------------------------

const FOLD_THRESHOLD = 200
const FOLD_HEAD_KEEP = 50
const FOLD_TAIL_KEEP = 150

/** 展开后保持全量（新消息到达不重新折叠） */
const foldExpanded = ref(false)

const hiddenCount = computed(() => {
  if (foldExpanded.value || props.messages.length <= FOLD_THRESHOLD) return 0
  return props.messages.length - FOLD_HEAD_KEEP - FOLD_TAIL_KEEP
})

const displayMessages = computed(() => {
  const all = props.messages
  if (foldExpanded.value || all.length <= FOLD_THRESHOLD) return all
  return [...all.slice(0, FOLD_HEAD_KEEP), ...all.slice(-FOLD_TAIL_KEEP)]
})

// ---------------------------------------------------------------------------
// T14：虚拟滚动（`virtual` 默认关）
// ---------------------------------------------------------------------------

/** 根节点引用：既是"自身即滚动容器"时的滚动源，也是向上找滚动祖先的起点 */
const listRef = ref<HTMLElement | null>(null)

/** 滚动源当前 scrollTop（px）。virtual=false 时不采集，也不影响任何渲染。 */
const scrollTopPx = ref(0)

/** 滚动源可视高度（px）。首帧/纯 DOM 环境量到 0 时回落 FALLBACK_VIEWPORT_HEIGHT。 */
const viewportPx = ref(FALLBACK_VIEWPORT_HEIGHT)

/**
 * 当前窗口。virtual=false 时固定为「全量 + 两个 0 占位」，
 * 不看 scrollTop ⇒ 关闭态没有任何按滚动的重算。
 */
const virtualWindow = computed(() => {
  const total = displayMessages.value.length
  if (!props.virtual) return { start: 0, end: total, topPad: 0, bottomPad: 0 }
  return computeVirtualWindow({
    total,
    scrollTop: scrollTopPx.value,
    viewportHeight: viewportPx.value,
    rowHeight: DEFAULT_ROW_HEIGHT,
    overscan: DEFAULT_OVERSCAN,
  })
})

/**
 * 真正参与 v-for 的消息：virtual=false ⇒ 原样返回 `displayMessages`（同一引用，
 * 全量渲染，与基线逐位等价）；窗口恰好覆盖全量时也原样返回，避免无意义的切片。
 */
const listForRender = computed(() => {
  const src = displayMessages.value
  const win = virtualWindow.value
  if (win.start === 0 && win.end === src.length) return src
  return src.slice(win.start, win.end)
})

/**
 * 折叠条在**渲染列表内**的相对下标。
 *
 * 非虚拟模式恒为 `FOLD_HEAD_KEEP`（= 基线里 `idx === FOLD_HEAD_KEEP` 的判定，逐位等价）；
 * 虚拟模式下折算成窗口内下标，窗口没覆盖到折叠位置时返回 -1（永不匹配任何 idx）——
 * 折叠条随窗口滚进滚出，而不是被钉在窗口顶部。
 */
const foldBarIndex = computed(() => {
  if (!props.virtual) return FOLD_HEAD_KEEP
  const win = virtualWindow.value
  if (FOLD_HEAD_KEEP < win.start || FOLD_HEAD_KEEP >= win.end) return -1
  return FOLD_HEAD_KEEP - win.start
})

/**
 * 顶部 / 底部占位的高度清单。
 *
 * 用「0 或 1 个元素的数组 + v-for」而不是「v-if 包一个 div」：`v-if` 为 false 时
 * Vue 仍会插入 `<!--v-if-->` 占位注释节点，virtual=false 的 DOM 就不再与基线逐位一致；
 * 空数组的 v-for 一个节点都不产生。
 */
const topSpacers = computed<Array<{ kind: string; height: number }>>(() =>
  props.virtual ? [{ kind: 'top', height: virtualWindow.value.topPad }] : [],
)

const bottomSpacers = computed<Array<{ kind: string; height: number }>>(() =>
  props.virtual ? [{ kind: 'bottom', height: virtualWindow.value.bottomPad }] : [],
)

/** 手动绑定的滚动监听元素（最近的可滚动祖先；与根元素相同时不重复绑定） */
let boundRootEl: HTMLElement | null = null
let boundScrollEl: HTMLElement | null = null

function refreshMetrics(el: HTMLElement | null): void {
  const src = el ?? boundScrollEl ?? listRef.value
  if (!src) return
  scrollTopPx.value = src.scrollTop
  // clientHeight 为 0 说明还没布局（首帧 / 纯 DOM 环境）：回落默认视口高度，
  // 否则窗口只剩 overscan 条，表现为"列表几乎空白"。
  viewportPx.value = src.clientHeight > 0 ? src.clientHeight : FALLBACK_VIEWPORT_HEIGHT
}

/**
 * 真正的滚动容器通常是装配方的祖先（页面态的 `.chat-content`、弹窗态的
 * `.chat-modal-body`），本组件自身不滚动。逐级向上找第一个 `overflow-y` 为
 * auto/scroll 的祖先；找不到就退回自身（装配方式即"自身是滚动容器"）。
 */
function resolveScrollSource(): HTMLElement | null {
  const root = listRef.value
  if (!root) return null
  let node: HTMLElement | null = root.parentElement
  while (node) {
    const overflowY = getComputedStyle(node).overflowY
    if (overflowY === 'auto' || overflowY === 'scroll') return node
    node = node.parentElement
  }
  return root
}

function onScroll(event: Event): void {
  refreshMetrics((event.currentTarget as HTMLElement | null) ?? null)
}

function unbindScrollSource(): void {
  if (boundRootEl) {
    boundRootEl.removeEventListener('scroll', onScroll)
    boundRootEl = null
  }
  if (boundScrollEl) {
    boundScrollEl.removeEventListener('scroll', onScroll)
    boundScrollEl = null
  }
}

/** 只在 virtual=true 时绑定监听：关闭态零监听、零重算（flag-off 等价性） */
function bindScrollSource(): void {
  unbindScrollSource()
  if (!props.virtual) return
  const root = listRef.value
  if (!root) return
  root.addEventListener('scroll', onScroll, { passive: true })
  boundRootEl = root
  const source = resolveScrollSource()
  if (source && source !== root) {
    source.addEventListener('scroll', onScroll, { passive: true })
    boundScrollEl = source
  }
  refreshMetrics(source ?? root)
}

onMounted(bindScrollSource)

watch(
  () => props.virtual,
  () => {
    void nextTick(bindScrollSource)
  },
)

// 消息增删会改变滚动源的内容高度与可视高度（首帧量到 0），布局稳定后补量一次。
// virtual=false 直接跳过：关闭态连"补量"这步都不做（flag-off 零额外工作）。
watch(
  () => displayMessages.value.length,
  () => {
    if (!props.virtual) return
    void nextTick(() => refreshMetrics(null))
  },
)

onBeforeUnmount(unbindScrollSource)
</script>

<style scoped>
/* W6 B5：工具活动卡片（不再污染思考区） */
.tool-card {
  display: inline-block;
  max-width: 76%;
  margin-bottom: 6px;
  padding: 8px 12px;
  background: #f7f7f7;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  font-size: 13px;
  text-align: left;
  min-width: 200px;
}

.tool-running {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #4b5563;
}

.tool-spinner {
  flex: none;
  width: 12px;
  height: 12px;
  border: 2px solid #d1d5db;
  border-top-color: var(--dp-primary, #1677ff);
  border-radius: 50%;
  animation: tool-spin 0.8s linear infinite;
}

@keyframes tool-spin {
  to {
    transform: rotate(360deg);
  }
}

.tool-history {
  font-size: 12px;
}

.tool-running + .tool-history {
  margin-top: 6px;
}

.tool-history-summary {
  cursor: pointer;
  color: #6b7280;
  user-select: none;
}

.tool-history-item {
  padding: 2px 0 0 8px;
  color: #6b7280;
}

/* W6 A3：助手 markdown 文本（复用气泡外观，内文元素排版） */
.modal-message-text--md :deep(p) {
  margin: 0 0 8px;
}

.modal-message-text--md :deep(p:last-child) {
  margin-bottom: 0;
}

.modal-message-text--md :deep(ul),
.modal-message-text--md :deep(ol) {
  margin: 6px 0;
  padding-left: 20px;
}

.modal-message-text--md :deep(pre) {
  margin: 8px 0;
  padding: 8px 10px;
  background: #f0f0f0;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 13px;
}

.modal-message-text--md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}

.modal-message-text--md :deep(a) {
  color: var(--dp-primary, #1677ff);
}

/* W6 B6：流式光标 */
.stream-caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--dp-primary, #1677ff);
  animation: caret-blink 1s steps(1) infinite;
}

@keyframes caret-blink {
  50% {
    opacity: 0;
  }
}

/* W6 B8：耗时芯片 */
.timing-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  background: #f5f5f5;
  border: 1px solid #e5e7eb;
  font-size: 12px;
  color: #6b7280;
  font-variant-numeric: tabular-nums;
}

.timing-chip-sep {
  color: #d1d5db;
}

/* W6 B11：消息操作（hover 显现） */
.msg-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
  opacity: 0;
  transition: opacity 0.15s ease;
}

.modal-message:hover .msg-actions,
.modal-message:focus-within .msg-actions {
  opacity: 1;
}

.msg-action-btn {
  padding: 2px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  color: #6b7280;
  cursor: pointer;
}

.msg-action-btn:hover {
  color: var(--dp-primary, #1677ff);
  border-color: var(--dp-primary, #1677ff);
}

.modal-message {
  margin-bottom: 14px;
}

/* 批次 C1：中段折叠条 */
.message-fold-bar {
  display: block;
  width: 100%;
  margin-bottom: 14px;
  padding: 6px 10px;
  font-size: 12px;
  color: #6b7280;
  text-align: center;
  background: #fafafa;
  border: 1px dashed #d9d9d9;
  border-radius: 8px;
  cursor: pointer;
  user-select: none;
}

.message-fold-bar:hover,
.message-fold-bar:focus-visible {
  color: var(--dp-primary, #2563eb);
  border-color: var(--dp-primary, #2563eb);
  outline: none;
}

.modal-message.user {
  text-align: right;
}

.modal-message-text {
  display: inline-block;
  max-width: 76%;
  text-align: left;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f5f5f5;
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.modal-message.user .modal-message-text {
  background: var(--dp-primary-bg);
}

.modal-citations {
  margin-top: 8px;
  text-align: left;
}

.source-title {
  font-size: 12px;
  color: #999;
  margin-bottom: 8px;
}

.source-item {
  display: flex;
  gap: 8px;
  padding: 8px;
  background: #f9f9f9;
  border-radius: 6px;
  margin-bottom: 8px;
  transition: background 0.15s ease;
}

.source-item:hover {
  background: #f0f0f0;
}

.source-item:focus-visible {
  outline: 2px solid #1677ff;
  outline-offset: 1px;
}

.source-icon {
  flex-shrink: 0;
  color: #666;
}

.source-snippet {
  font-size: 12px;
  color: #999;
}

/* R2-2 效力状态徽标（与 CitationPreviewCard 同语义） */
.source-status {
  margin-left: 4px;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 500;
  line-height: 15px;
  vertical-align: 1px;
}

.source-status.is-ok {
  background: #e6f7ee;
  color: #0f8a4d;
}

.source-status.is-warn {
  background: #fdf6e7;
  color: #b7791f;
}

.source-status.is-danger {
  background: #fdecec;
  color: #c0392b;
}

/* 主聊天页（Home / History）样式 */
.chat-message-list--page .modal-message {
  margin-bottom: 16px;
}

/* X3 过程面板：跟随引用来源的紧凑风格 */
.explain-panel {
  margin-top: 8px;
  font-size: 12px;
}

.explain-summary {
  cursor: pointer;
  color: #6b7280;
  user-select: none;
}

.explain-summary:hover {
  color: var(--dp-primary, #2563eb);
}

.explain-body {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fafafa;
}

.explain-row {
  margin-bottom: 4px;
}

.explain-key {
  display: inline-block;
  margin-right: 6px;
  color: #6b7280;
}

.explain-evidence {
  margin-bottom: 6px;
  padding-left: 8px;
  border-left: 2px solid #e5e7eb;
}

.explain-notrecorded {
  color: #9ca3af;
}

.explain-error {
  color: #c0392b;
}

/* 执行轨迹时间线（Trace UI v1）：与过程面板同风格 */
.trace-panel {
  margin-top: 8px;
  font-size: 12px;
}

.trace-body {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fafafa;
}

.trace-step {
  display: flex;
  gap: 8px;
  padding: 2px 0;
  align-items: baseline;
}

.trace-kind {
  flex: none;
  color: #6b7280;
}

.trace-step.is-tool .trace-label {
  color: var(--dp-primary, #2563eb);
}

.trace-step.is-error .trace-label {
  color: #c0392b;
}

.trace-step.is-done .trace-label {
  color: #15803d;
}

.trace-label {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.trace-gap {
  flex: none;
  color: #9ca3af;
  font-variant-numeric: tabular-nums;
}

/* W6 B9：流式期间最新轨迹步骤高亮 */
.trace-step.is-latest {
  background: var(--dp-primary-bg, #eef6ff);
  border-radius: 4px;
  padding: 2px 6px;
  margin: 0 -6px;
}


.chat-message-list--page .modal-message.user .modal-message-text {
  background: var(--dp-primary);
  color: #fff;
}

.chat-message-list--page .modal-message-text {
  max-width: min(720px, 70vw);
}
</style>
