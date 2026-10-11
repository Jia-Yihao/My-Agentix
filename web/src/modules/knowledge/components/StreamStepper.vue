<template>
  <div class="stream-stepper" :class="{ 'is-compact': compact }">
    <div class="stepper-steps">
      <div
        v-for="(step, i) in STEPS"
        :key="step.key"
        class="stepper-step"
        :class="`is-${stateOf(i)}`"
      >
        <span class="step-dot" aria-hidden="true">
          <span v-if="stateOf(i) === 'active'" class="step-spinner" />
          <span v-else-if="stateOf(i) === 'done'" class="step-check">✓</span>
        </span>
        <span class="step-label">{{ step.label }}</span>
        <span v-if="i < STEPS.length - 1" class="step-line" aria-hidden="true" />
      </div>
    </div>

    <div v-if="currentStep" class="stepper-current">
      {{ progress?.message || currentStep.label }}
    </div>

    <!-- R-P1-5 检索四阶段子步骤：rewrite/sparse/rrf/rerank + 各阶段 ms -->
    <div v-if="!compact && subSteps.length" class="stepper-substeps">
      <span v-for="(st, i) in subSteps" :key="i" class="substep">
        {{ subStepLabel(st) }}
        <em v-if="st.ms != null" class="substep-ms">{{ formatMs(st.ms) }}</em>
      </span>
    </div>

    <div v-if="progress?.total" class="stepper-bar">
      <div class="stepper-fill" :style="{ width: percent + '%' }" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import type { StreamProgress } from '../api/chatStreamHandlers'

const props = defineProps<{
  progress: StreamProgress | null | undefined
  /** 生成期间（正文已出现）：紧凑显示，隐藏子步骤 */
  compact?: boolean
}>()

const STEPS = [
  { key: 'loading', label: '准备会话' },
  { key: 'retrieval', label: '检索知识库' },
  { key: 'llm_generate', label: '生成回答' },
] as const

const activeIndex = computed(() => {
  const stage = props.progress?.stage
  if (!stage) return -1
  return STEPS.findIndex((s) => s.key === stage)
})

const currentStep = computed(() =>
  activeIndex.value >= 0 ? STEPS[activeIndex.value] : null,
)

function stateOf(i: number): 'done' | 'active' | 'pending' {
  const cur = activeIndex.value
  if (cur < 0) return 'pending'
  if (i < cur) return 'done'
  if (i === cur) return 'active'
  return 'pending'
}

const percent = computed(() => {
  const p = props.progress
  if (!p || !p.total) return 0
  return Math.min(100, Math.round(((p.step || 0) / p.total) * 100))
})

const subSteps = computed(() => props.progress?.stages ?? [])

const SUB_STEP_LABELS: Record<string, string> = {
  rewrite: '查询重写',
  sparse: '稀疏检索',
  dense: '稠密检索',
  rrf: '融合排序',
  rerank: '重排序',
}

function subStepLabel(st: { stage?: string; skipped?: string | number }): string {
  const key = st.stage ?? ''
  return SUB_STEP_LABELS[key] ?? key
}

function formatMs(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  if (ms >= 10) return `${Math.round(ms)}ms`
  return `${ms.toFixed(1)}ms`
}
</script>

<style scoped>
.stream-stepper {
  display: inline-block;
  max-width: 76%;
  padding: 12px 16px;
  background: #f0f5ff;
  border: 1px solid #d6e4ff;
  border-radius: 10px;
  min-width: 260px;
  text-align: left;
}

.stepper-steps {
  display: flex;
  align-items: center;
  gap: 4px;
}

.stepper-step {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #94a3b8;
}

.stepper-step.is-active {
  color: #1677ff;
  font-weight: 500;
}

.stepper-step.is-done {
  color: #15803d;
}

.step-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #e8e8e8;
  font-size: 10px;
  color: #fff;
}

.stepper-step.is-active .step-dot {
  background: #1677ff;
}

.stepper-step.is-done .step-dot {
  background: #15803d;
}

.step-spinner {
  width: 8px;
  height: 8px;
  border: 2px solid rgba(255, 255, 255, 0.45);
  border-top-color: #fff;
  border-radius: 50%;
  animation: stepper-spin 0.8s linear infinite;
}

@keyframes stepper-spin {
  to {
    transform: rotate(360deg);
  }
}

.step-line {
  width: 26px;
  height: 1px;
  background: #d9d9d9;
}

.stepper-current {
  margin-top: 8px;
  font-size: 13px;
  color: #1677ff;
}

.stepper-substeps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.substep {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #fff;
  border: 1px solid #d6e4ff;
  font-size: 12px;
  color: #4b5563;
}

.substep-ms {
  font-style: normal;
  font-variant-numeric: tabular-nums;
  color: #1677ff;
}

.stepper-bar {
  margin-top: 8px;
  height: 6px;
  background: #e8e8e8;
  border-radius: 3px;
  overflow: hidden;
}

.stepper-fill {
  height: 100%;
  background: linear-gradient(90deg, #1677ff, #69b1ff);
  border-radius: 3px;
  transition: width 0.3s ease;
}

/* 生成期间紧凑态：子步骤/进度条隐藏（渲染条件已控），整体收窄字号 */
.stream-stepper.is-compact {
  padding: 8px 12px;
  min-width: 0;
}

.stream-stepper.is-compact .stepper-current {
  margin-top: 4px;
  font-size: 12px;
}

.stream-stepper.is-compact .step-label {
  font-size: 12px;
}
</style>
