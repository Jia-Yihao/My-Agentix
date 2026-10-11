<template>
  <div class="content-area">
    <div v-if="title" class="chat-header">
      <div class="chat-title">{{ title }}</div>
    </div>
    <div ref="chatContentRef" class="chat-content" @scroll="onScroll">
      <slot v-if="empty" name="empty" />
      <ChatMessageList
        v-else
        variant="page"
        :messages="messages"
        :pending-assistant-id="pendingAssistantId"
        @regenerate="emit('regenerate')"
      />
    </div>
    <!-- W6 A2：上翻阅读时不打扰——新内容到达只在贴底时自动下滚；
         离开底部出现「↓ 回到底部」胶囊 -->
    <button
      v-if="showJumpBottom"
      type="button"
      class="jump-bottom"
      aria-label="回到底部"
      @click="jumpToBottom"
    >↓ 回到底部</button>
    <div class="chat-input">
      <slot name="composer" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'

import ChatMessageList, { type ChatModalMessage } from '../ChatMessageList.vue'
import { shouldStickToBottom } from '../../utils/scrollBehavior'

const props = defineProps<{
  title?: string
  messages: ChatModalMessage[]
  pendingAssistantId?: string | null
  empty?: boolean
}>()

const emit = defineEmits<{
  (e: 'regenerate'): void
}>()

const chatContentRef = ref<HTMLElement | null>(null)
/** 贴底状态：流式新内容是否继续自动下滚（初始贴底） */
const stickToBottom = ref(true)
/** 离底后是否有新内容（胶囊显隐信号） */
const showJumpBottom = ref(false)

let rafId: number | null = null

const readState = () => {
  const el = chatContentRef.value
  if (!el) return { scrollTop: 0, scrollHeight: 0, clientHeight: 0 }
  return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
}

/** scroll 监听：rAF 节流更新贴底状态（指针滚轮高频触发不抖） */
const onScroll = () => {
  if (rafId !== null) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    const stick = shouldStickToBottom(readState())
    stickToBottom.value = stick
    showJumpBottom.value = !stick
  })
}

/** 强制滚到底（切会话 / 发送 / 点胶囊）并恢复贴底 */
const jumpToBottom = async () => {
  stickToBottom.value = true
  showJumpBottom.value = false
  await nextTick()
  scrollToBottom()
}

const scrollToBottom = () => {
  if (chatContentRef.value) {
    chatContentRef.value.scrollTop = chatContentRef.value.scrollHeight
  }
}

/** 流式增量滚动（onChunk）：仅贴底时跟随，上翻阅读不打扰 */
const smartScroll = () => {
  if (!stickToBottom.value) {
    showJumpBottom.value = true
    return
  }
  scrollToBottom()
}

defineExpose({ scrollToBottom, smartScroll })
</script>

<style scoped>
.content-area {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
  min-width: 0;
}

.chat-header {
  height: 48px;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  flex-shrink: 0;
}

.chat-title {
  font-size: 14px;
  color: #6b7280;
}

.chat-content {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: #fff;
  min-height: 0;
}

.chat-input {
  padding: 16px;
  background: #fff;
  border-top: 1px solid #e8e8e8;
  display: flex;
  justify-content: center;
  flex-shrink: 0;
}

.chat-empty {
  color: #94a3b8;
  font-size: 14px;
  text-align: center;
  padding: 48px 16px;
}

/* W6 A2：↓ 回到底部胶囊（聊天区上方居中，流式期间离开底部时浮现） */
.jump-bottom {
  position: absolute;
  left: 50%;
  bottom: calc(72px + 16px);
  transform: translateX(-50%);
  padding: 6px 14px;
  border: none;
  border-radius: 999px;
  background: rgba(30, 30, 30, 0.82);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2);
  z-index: 5;
}

.jump-bottom:hover {
  background: rgba(30, 30, 30, 0.95);
}
</style>
