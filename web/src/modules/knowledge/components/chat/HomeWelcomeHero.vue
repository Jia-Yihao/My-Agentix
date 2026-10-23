<template>
  <div class="home-content">
    <div class="home-hero">
      <div class="home-logo">
        <MascotLogo :size="64" />
      </div>
      <h1 class="home-title">今天想了解什么？</h1>
      <p class="home-subtitle">
        <template v-if="!knowledgeEnabled">
          知识库检索已关闭，可以直接向智能助手提问
        </template>
        <template v-else-if="selectedKbName">
          已选择 <strong>{{ selectedKbName }}</strong>，回答将尽量附带来源引用
        </template>
        <template v-else>先选择或创建一个知识库，再开始提问</template>
      </p>
      <div class="suggestion-grid">
        <button
          v-for="suggestion in suggestions"
          :key="suggestion.question"
          type="button"
          class="suggestion-card"
          @click="$emit('select-prompt', suggestion.question)"
        >
          <span class="suggestion-tag">{{ suggestion.tag }}</span>
          <span class="suggestion-question">{{ suggestion.question }}</span>
        </button>
      </div>
    </div>
    <div class="ai-input-bar">
      <slot />
      <p class="home-hint">Enter 发送 · Shift + Enter 换行 · 回答可能包含来源引用</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import MascotLogo from '../MascotLogo.vue'

defineProps<{
  selectedKbName?: string
  knowledgeEnabled: boolean
}>()

defineEmits<{
  'select-prompt': [question: string]
}>()

const suggestions = [
  { tag: '数据检索', question: '示例数据集 A 中关于条目生效时间的说明在哪里？' },
  { tag: '文档问答', question: '帮我总结这份测试文档里的关键条目' },
  { tag: '数据检索', question: '示例数据集中各类条目的编号规则是什么？' },
  { tag: '案例分析', question: '对比示例数据集 B 与 C 的条目结构差异' },
]
</script>

<style scoped>
.home-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: #f6f6f4;
  padding: 24px 32px 36px;
  min-height: 0;
  overflow-y: auto;
}

.home-hero {
  width: min(680px, 100%);
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.home-logo {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.home-title {
  font-size: 22px;
  font-weight: 600;
  color: #1f2328;
  margin-bottom: 7px;
  line-height: 1.3;
}

.home-subtitle {
  font-size: 14px;
  color: #6b7280;
  margin-bottom: 22px;
}

.home-subtitle strong {
  color: #3c46b1;
  font-weight: 600;
}

.suggestion-grid {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}

.suggestion-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 7px;
  min-height: 82px;
  padding: 12px 14px;
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 12px;
  background: #fff;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}

.suggestion-card:hover {
  border-color: rgba(79, 91, 213, 0.45);
  background: #fbfbfe;
  transform: translateY(-1px);
}

.suggestion-tag {
  padding: 2px 8px;
  border-radius: 6px;
  background: #eef0fc;
  color: #3c46b1;
  font-size: 11px;
}

.suggestion-question {
  color: #1f2328;
  font-size: 13px;
  line-height: 1.5;
}

.ai-input-bar {
  width: min(680px, 100%);
  padding: 0;
  background: transparent;
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 0;
}

.home-hint {
  margin-top: 10px;
  color: #9ca3af;
  font-size: 12px;
}

@media (max-width: 640px) {
  .home-content {
    padding: 22px 16px;
    justify-content: flex-start;
  }

  .suggestion-grid {
    grid-template-columns: 1fr;
  }
}
</style>
