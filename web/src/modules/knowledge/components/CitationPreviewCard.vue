<template>
  <Teleport to="body">
    <Transition name="citation-preview">
      <div
        v-if="visible"
        ref="cardRef"
        class="citation-preview-card"
        role="tooltip"
        :style="{ top: `${position.top}px`, left: `${position.left}px` }"
        @mouseenter="emit('pointer-enter')"
        @mouseleave="emit('pointer-leave')"
      >
        <div class="citation-preview-head">
          <span class="citation-preview-icon"><IconFile :size="14" /></span>
          <span
            v-if="citation?.ref"
            class="citation-preview-ref"
            :title="citation.cited ? '回答中已引用该片段' : '本次召回的片段标号'"
          >[{{ citation.ref }}]</span>
          <span class="citation-preview-source">{{ displayName }}</span>
          <span
            v-if="statusView"
            class="citation-preview-status"
            :class="`is-${statusView.tone}`"
          >{{ statusView.label }}</span>
          <span v-if="citation?.file_ext" class="citation-preview-ext">{{ citation.file_ext }}</span>
          <span v-if="citation?.page_no" class="citation-preview-page">第 {{ citation.page_no }} 页</span>
        </div>
        <div v-if="metaLine" class="citation-preview-meta">{{ metaLine }}</div>
        <div v-if="citation?.document_id" class="citation-preview-doc">
          文档 ID：{{ citation.document_id }}
        </div>
        <div v-if="citation?.replaced_by" class="citation-preview-replaced">
          该条文已被「{{ citation.replaced_by }}」替代，请以现行文本为准
        </div>
        <div class="citation-preview-snippet">{{ citation?.snippet }}</div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import IconFile from './icons/IconFile.vue'
import type { ChatCitation } from '../api/chatTypes'
import { effectiveStatusView } from '../utils/effectiveStatus'

/** 卡片与视口边缘的最小留白 */
const MARGIN = 12

const props = defineProps<{
  /** 当前需要预览的引用；为空表示不展示 */
  citation?: ChatCitation | null
  /** 触发元素的位置矩形（getBoundingClientRect 结果） */
  anchor?: DOMRect | null
}>()

const emit = defineEmits<{
  (e: 'pointer-enter'): void
  (e: 'pointer-leave'): void
  (e: 'dismiss'): void
}>()

const cardRef = ref<HTMLElement | null>(null)
const position = ref({ top: 0, left: 0 })

const visible = computed(() => Boolean(props.citation && props.anchor))

/** 文件名优先，其次检索来源，最后兜底文案 */
const displayName = computed(
  () => props.citation?.file_name || props.citation?.source || '文档片段',
)

/** R2-2：效力状态徽标视图；缺失/未知状态返回 null（不渲染） */
const statusView = computed(() => effectiveStatusView(props.citation?.effective_status))

/** 目录 + 大小；都取不到时整行为空、不渲染（不出现空行或 undefined） */
const metaLine = computed(() => {
  const citation = props.citation
  if (!citation) return ''
  return [citation.folder_path, formatFileSize(citation.file_size)]
    .filter(Boolean)
    .join(' · ')
})

/** 字节数转可读大小；非数字/负数返回空串（调用方据此不渲染） */
function formatFileSize(bytes?: number | null): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(1)} ${units[unit]}`
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

/**
 * 计算卡片位置：卡片用 position: fixed 且 Teleport 到 body，
 * 因此坐标直接基于视口，不受任何祖先 overflow / transform 影响。
 * 采用「悬浮叠加」策略：卡片直接覆盖在锚点（引用来源卡片）之上，
 * 左上角对齐锚点，而不是偏移到锚点下方/上方，避免错位遮挡其他内容。
 * 卡片若在内容密集或贴近视口边缘时超出，则向视口内侧夹紧。
 */
function updatePosition(): void {
  const anchor = props.anchor
  if (!anchor) return

  // 首帧元素尚未挂载，尺寸按锚点尺寸估算，nextTick 量到真实尺寸后会再算一次
  const el = cardRef.value
  const cardWidth = el?.offsetWidth ?? anchor.width
  const cardHeight = el?.offsetHeight ?? anchor.height
  const viewportWidth = window.innerWidth
  const viewportHeight = window.innerHeight

  // 悬浮叠加：左上角对齐锚点，再夹紧到视口内，保证卡片始终完整可见
  const left = clamp(anchor.left, MARGIN, viewportWidth - cardWidth - MARGIN)
  const top = clamp(anchor.top, MARGIN, viewportHeight - cardHeight - MARGIN)

  position.value = { top, left }
}

function attachListeners(): void {
  window.addEventListener('resize', updatePosition)
  // capture 模式：内部滚动容器的滚动事件不会冒泡到 window，必须捕获阶段监听
  window.addEventListener('scroll', updatePosition, true)
  window.addEventListener('keydown', handleKeydown)
}

function detachListeners(): void {
  window.removeEventListener('resize', updatePosition)
  window.removeEventListener('scroll', updatePosition, true)
  window.removeEventListener('keydown', handleKeydown)
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('dismiss')
}

watch(
  () => [props.citation, props.anchor] as const,
  async ([citation, anchor]) => {
    if (!citation || !anchor) {
      detachListeners()
      return
    }
    // 先按锚点给一个估计位置，避免首帧闪到左上角；挂载后再量尺寸二次定位
    updatePosition()
    await nextTick()
    updatePosition()
    attachListeners()
  },
  { immediate: true },
)

onBeforeUnmount(detachListeners)
</script>

<style scoped>
/* Teleport 到 body，position: fixed 的坐标基准是视口，不会被 overflow 容器裁剪 */
.citation-preview-card {
  position: fixed;
  z-index: 9999;
  box-sizing: border-box;
  /* 减去左右各 12px 的 MARGIN，窄视口下也不会超出视口 */
  max-width: min(360px, calc(100vw - 24px));
  max-height: 40vh;
  overflow-y: auto;
  padding: 10px 12px;
  background: #fff;
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.14);
  font-size: 12px;
  line-height: 1.6;
  color: #333;
  text-align: left;
  /* 必须可接收指针事件，否则指针从锚点移向卡片时卡片会立刻消失 */
  pointer-events: auto;
}

.citation-preview-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  color: #333;
}

.citation-preview-icon {
  display: inline-flex;
  flex-shrink: 0;
  color: #666;
}

.citation-preview-source {
  overflow-wrap: anywhere;
}

.citation-preview-page {
  flex-shrink: 0;
  font-weight: 400;
  color: #999;
}

.citation-preview-ext {
  flex-shrink: 0;
  padding: 0 4px;
  border-radius: 3px;
  background: #f0f0f0;
  font-size: 10px;
  font-weight: 400;
  line-height: 16px;
  color: #666;
  text-transform: uppercase;
}

/* R2-2 效力状态徽标：ok=绿 / warn=黄 / danger=红（跌红涨绿为行情惯例，此处按风险语义） */
.citation-preview-status {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 500;
  line-height: 16px;
}

.citation-preview-status.is-ok {
  background: #e6f7ee;
  color: #0f8a4d;
}

.citation-preview-status.is-warn {
  background: #fdf6e7;
  color: #b7791f;
}

.citation-preview-status.is-danger {
  background: #fdecec;
  color: #c0392b;
}

/* 召回溯源：片段标号（与答案里的 [S1] 对应） */
.citation-preview-ref {
  flex-shrink: 0;
  padding: 0 5px;
  border-radius: 3px;
  background: #eef2ff;
  color: #4f46e5;
  font-size: 10px;
  font-weight: 600;
  line-height: 16px;
}

.citation-preview-replaced {
  margin-top: 4px;
  font-size: 11px;
  color: #c0392b;
  overflow-wrap: anywhere;
}

.citation-preview-meta {
  margin-top: 4px;
  color: #999;
  overflow-wrap: anywhere;
}

.citation-preview-doc {
  margin-top: 4px;
  color: #999;
  overflow-wrap: anywhere;
}

.citation-preview-snippet {
  margin-top: 6px;
  color: #666;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.citation-preview-enter-active,
.citation-preview-leave-active {
  transition: opacity 0.14s ease, transform 0.14s ease;
}

.citation-preview-enter-from,
.citation-preview-leave-to {
  opacity: 0;
  transform: scale(0.97);
}
</style>
