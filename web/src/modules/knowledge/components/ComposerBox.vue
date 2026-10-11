<template>
  <div class="composer-root">
    <div class="composer-shell" :class="shellVariantClass">
      <div class="composer-main-row">
        <!-- maxlength 与后端 settings.QUERY_MAX_LENGTH（默认 5000）对齐：
             超长提交会被 422 拒绝，用户只会看到"请求失败" -->
        <textarea
          ref="textareaEl"
          class="composer-textarea"
          :value="modelValue"
          rows="1"
          :placeholder="placeholder"
          maxlength="5000"
          @input="onInput"
          @keydown="onKeydown"
          @focus="$emit('focus')"
          @click="$emit('focus')"
        />
      </div>

      <div v-if="showErrorBanner && errorText" class="composer-error-banner">
        {{ errorText }}
      </div>

      <div class="composer-footer">
        <div class="composer-left">
          <button
            v-if="enableKbPicker"
            class="composer-pill knowledge-toggle"
            :class="{ 'is-enabled': knowledgeEnabled }"
            type="button"
            role="switch"
            :aria-checked="knowledgeEnabled"
            :disabled="knowledgeToggleDisabled"
            aria-label="知识库检索"
            :title="knowledgeEnabled ? '知识库检索已开启，点击关闭' : '知识库检索已关闭，点击开启'"
            @click.stop="emit('update:knowledgeEnabled', !knowledgeEnabled)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M4 5.5C4 4.67 7.58 4 12 4s8 .67 8 1.5S16.42 7 12 7 4 6.33 4 5.5Z" stroke="currentColor" stroke-width="1.7"/>
              <path d="M4 5.5v13C4 19.33 7.58 20 12 20s8-.67 8-1.5v-13M4 12c0 .83 3.58 1.5 8 1.5s8-.67 8-1.5" stroke="currentColor" stroke-width="1.7"/>
            </svg>
            <span>知识库</span>
            <span class="knowledge-switch-dot" aria-hidden="true"></span>
          </button>
          <button
            v-if="enableKbPicker && knowledgeEnabled"
            class="composer-pill kb-select-button"
            type="button"
            :disabled="kbOptions.length === 0"
            :aria-expanded="showKbDropdown"
            @click.stop="openKbDropdown"
          >
            {{ selectedKbName || '选择知识库' }}
            <span aria-hidden="true">⌄</span>
          </button>
          <input
            v-if="enableAttachment"
            ref="attachmentInputEl"
            class="attachment-input"
            type="file"
            :accept="attachmentAccept"
            aria-label="选择要上传的文件"
            @change="onAttachmentChange"
          />
          <button
            v-if="enableAttachment"
            class="composer-pill attachment-button"
            type="button"
            :disabled="uploading || (enableKbPicker && knowledgeEnabled && !selectedKbId)"
            :aria-label="uploading ? `正在上传 ${uploadProgress}%` : attachmentLabel"
            :title="uploading ? `正在上传 ${uploadProgress}%` : attachmentLabel"
            @click.stop="openAttachmentPicker"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="M21.4 11.1 12.9 19.6a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"
                fill="none"
                stroke="currentColor"
                stroke-linecap="round"
                stroke-linejoin="round"
                stroke-width="1.8"
              />
            </svg>
            <span>{{ uploading ? `上传中 ${uploadProgress}%` : attachmentLabel }}</span>
          </button>
          <button
            v-if="knowledgeEnabled || !enableKbPicker"
            class="composer-pill"
            type="button"
            :class="{ 'is-deep': chatMode === 'deep' }"
            :title="chatMode === 'deep' ? '深度模式：展示思考过程' : '快速模式'"
            @click.stop="toggleChatMode"
          >
            {{ chatModeLabel }}
          </button>
        </div>
        <div class="composer-right">
          <!-- W6 B7：流式期间发送键变「停止」（接 useConversationChat.stopGeneration） -->
          <button
            v-if="streaming"
            class="composer-send-btn is-stop"
            type="button"
            aria-label="停止生成"
            title="停止生成"
            @click="emit('stop')"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <rect x="1" y="1" width="10" height="10" rx="2" fill="currentColor" />
            </svg>
          </button>
          <button
            v-else
            class="composer-send-btn"
            type="button"
            aria-label="发送消息"
            title="发送消息"
            :disabled="disabledSend"
            @click="$emit('send')"
          >
            <IconSend />
          </button>
        </div>
      </div>
    </div>

    <div v-if="enableKbPicker && knowledgeEnabled && showKbDropdown" ref="kbDropdownEl" class="kb-dropdown">
      <div v-if="kbOptions.length === 0" class="kb-dropdown-empty">暂无知识库，请先创建知识库</div>
      <div
        v-for="kb in kbOptions"
        :key="kb.id"
        class="kb-dropdown-item"
        :class="{ active: kb.id === selectedKbId }"
        @click="selectKb(kb)"
      >
        <div class="kb-item-title">{{ kb.type }}</div>
        <div class="kb-item-name">{{ kb.name }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useChatMode } from '../composables/useChatMode'
import IconSend from './icons/IconSend.vue'

const { chatMode, chatModeLabel, toggleChatMode } = useChatMode()

type KbOption = { id: string; name: string; type: string }

const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
    disabledSend?: boolean
    kbOptions?: KbOption[]
    selectedKbId?: string | null
    errorText?: string
    showErrorBanner?: boolean
    shellVariant?: 'card' | 'bare'
    enableKbPicker?: boolean
    enableAttachment?: boolean
    uploading?: boolean
    uploadProgress?: number
    attachmentLabel?: string
    attachmentAccept?: string
    /** W6 B7：流式中——发送键切换为「停止」 */
    streaming?: boolean
    knowledgeEnabled?: boolean
    knowledgeToggleDisabled?: boolean
  }>(),
  {
    kbOptions: () => [],
    enableKbPicker: true,
    streaming: false,
    enableAttachment: false,
    uploading: false,
    uploadProgress: 0,
    attachmentLabel: '上传文件',
    attachmentAccept: '.pdf,application/pdf',
    knowledgeEnabled: false,
    knowledgeToggleDisabled: false,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  (e: 'send'): void
  (e: 'stop'): void
  (e: 'selectKb', kb: KbOption): void
  (e: 'update:knowledgeEnabled', enabled: boolean): void
  (e: 'attachment', file: File): void
  (e: 'clipboard'): void
  (e: 'focus'): void
}>()

const textareaEl = ref<HTMLTextAreaElement | null>(null)
const attachmentInputEl = ref<HTMLInputElement | null>(null)
const kbDropdownEl = ref<HTMLElement | null>(null)
const showKbDropdown = ref(false)

const shellVariantClass = computed(() => (props.shellVariant === 'bare' ? 'is-bare' : 'is-card'))
const selectedKbName = computed(
  () => props.kbOptions.find((kb) => kb.id === props.selectedKbId)?.name ?? '',
)

const autoResize = async () => {
  await nextTick()
  const el = textareaEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

const openKbDropdown = () => {
  if (!props.knowledgeEnabled || props.kbOptions.length === 0) return
  showKbDropdown.value = !showKbDropdown.value
}

const openAttachmentPicker = () => {
  if (!props.uploading) attachmentInputEl.value?.click()
}

const onAttachmentChange = (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('attachment', file)
  input.value = ''
}

const selectKb = (kb: KbOption) => {
  emit('selectKb', kb)
  showKbDropdown.value = false
  void autoResize()
}

const onInput = (e: Event) => {
  const v = (e.target as HTMLTextAreaElement).value
  emit('update:modelValue', v)
  void autoResize()
}

const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Enter') {
    e.preventDefault()
    emit('send')
  }
}

const handleClickOutside = (e: MouseEvent) => {
  const target = e.target as HTMLElement | null
  if (!target) return
  if (kbDropdownEl.value && !kbDropdownEl.value.contains(target)) {
    showKbDropdown.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  void autoResize()
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
})

watch(
  () => props.modelValue,
  () => {
    void autoResize()
  }
)

watch(
  () => props.knowledgeEnabled,
  (enabled) => {
    if (!enabled) showKbDropdown.value = false
  },
)
</script>

<style scoped>
.composer-root{
  position: relative;
  width: 100%;
}

.composer-shell{
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.composer-shell.is-card{
  background: #fff;
  border-radius: 18px;
  padding: 12px 14px 10px;
  border: 1px solid #e6e6e6;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
}

.composer-shell.is-bare{
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 0;
  border-radius: 0;
}

.composer-main-row{
  display: flex;
  align-items: stretch;
}

.composer-textarea{
  flex: 1;
  border: none;
  background: transparent;
  outline: none;
  color: #6b7280;
  font-size: 14px;
  resize: none;
  line-height: 22px;
  padding: 2px 6px;
  min-height: 22px;
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.composer-textarea::placeholder{
  color: #94a3b8;
}

.composer-footer{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
  min-height: 32px;
  padding: 0 6px;
  box-sizing: border-box;
}

.composer-left{
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}

.attachment-input{
  display: none;
}

.attachment-button{
  gap: 6px;
}

.attachment-button:disabled{
  cursor: wait;
  opacity: 0.75;
}

.composer-pill{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  border: none;
  background: #f7f7f7;
  font-size: 12px;
  line-height: 1;
  color: #6b7280;
  cursor: pointer;
  box-sizing: border-box;
}

.composer-pill:hover{
  background: #efefef;
}

.composer-pill.is-deep{
  background: var(--dp-primary-bg);
  color: var(--dp-primary);
}

.knowledge-toggle {
  gap: 6px;
  border: 1px solid rgba(15, 23, 42, 0.12);
  background: #fff;
  color: #6b7280;
}

.knowledge-toggle.is-enabled {
  border-color: rgba(79, 91, 213, 0.38);
  background: #eef0fc;
  color: #3c46b1;
}

.knowledge-switch-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c4c7d0;
}

.knowledge-toggle.is-enabled .knowledge-switch-dot {
  background: #4f5bd5;
}

.kb-select-button {
  max-width: 210px;
  gap: 7px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #3c46b1;
}

.kb-dropdown-empty {
  padding: 10px;
  color: #6b7280;
  font-size: 12px;
}

.kb-select-button:disabled {
  cursor: not-allowed;
  color: #9ca3af;
}

.composer-right{
  display: flex;
  align-items: center;
  gap: 8px;
}

.composer-icon-btn{
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: none;
  background: transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #6b7280;
}

.composer-send-btn{
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: var(--dp-primary-solid);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.composer-send-btn:disabled{
  background: #ccc;
  cursor: not-allowed;
}

/* W6 B7：停止态（区别于发送主色） */
.composer-send-btn.is-stop{
  background: #c0392b;
}

.composer-send-btn.is-stop:hover{
  background: #a93226;
}

.composer-error-banner{
  margin: 2px 6px 0;
  padding: 8px 10px;
  border-radius: 10px;
  background: #fff1f0;
  border: 1px solid #ffccc7;
  color: #a8071a;
  font-size: 12px;
  line-height: 18px;
}

.kb-dropdown{
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  padding: 8px;
  min-width: 220px;
  z-index: 110;
}

.kb-dropdown-item{
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
}

.kb-dropdown-item:hover{
  background: #f5f5f5;
}

.kb-dropdown-item.active{
  background: #eef0fc;
}

.kb-item-title{
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 2px;
}

.kb-item-name{
  font-size: 14px;
  color: #6b7280;
}

@media (max-width: 560px) {
  .composer-left {
    gap: 5px;
    flex-wrap: wrap;
  }

  .kb-select-button {
    max-width: 130px;
  }
}
</style>
