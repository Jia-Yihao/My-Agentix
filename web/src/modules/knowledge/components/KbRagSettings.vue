<template>
  <div v-if="open" class="rag-settings-overlay" @click.self="$emit('close')">
    <div class="rag-settings-panel">
      <div class="rag-settings-header">
        <h3>RAG 模型配置 — {{ kbName }}</h3>
        <button class="btn-close" @click="$emit('close')">×</button>
      </div>

      <div class="rag-settings-body">
        <!-- LLM -->
        <fieldset>
          <legend>LLM 模型</legend>
          <label>提供商
            <select v-model="form.llm_provider">
              <option value="openai-compatible">OpenAI 兼容</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
            </select>
          </label>
          <label>端点 URL <input v-model="form.llm_base_url" /></label>
          <label>模型名 <input v-model="form.llm_model" /></label>
        </fieldset>

        <!-- Embedding -->
        <fieldset>
          <legend>Embedding</legend>
          <label>端点 URL <input v-model="form.embedding_base_url" /></label>
          <label>模型名 <input v-model="form.embedding_model" /></label>
        </fieldset>

        <!-- Chunk -->
        <fieldset>
          <legend>切分策略</legend>
          <label>策略
            <select v-model="form.chunk_strategy">
              <option value="auto">auto（自动）</option>
              <option value="recursive">recursive（递归）</option>
              <option value="paragraph">paragraph（段落）</option>
              <option value="fixed">fixed（固定）</option>
            </select>
          </label>
          <label>块大小 <input v-model.number="form.chunk_size" type="number" min="100" /></label>
          <label>重叠 <input v-model.number="form.chunk_overlap" type="number" min="0" /></label>
        </fieldset>

        <!-- Retrieval -->
        <fieldset>
          <legend>检索参数</legend>
          <p>默认策略：Dense + BM25/z-score（Dense 权重 α=0.85）</p>
          <label>返回数 (K) <input v-model.number="form.search_k" type="number" min="1" max="50" /></label>
        </fieldset>
      </div>

      <div class="rag-settings-footer">
        <span v-if="saved" class="saved-hint">已保存</span>
        <span v-if="err" class="error-hint">{{ err }}</span>
        <!-- loadFailed 时禁用：表单里是前端默认值，此时保存会把整份默认值盖掉真实配置 -->
        <button class="btn-save" :disabled="saving || loadFailed" @click="save">{{ saving ? '保存中...' : '保存' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { fetchKbSettings, updateKbSettings } from '../api/settings'
import { getApiErrorHint, getApiErrorPayload } from '../utils/errors'

const props = defineProps<{ open: boolean; kbId: string; kbName: string }>()
// close 事件由模板里的 $emit('close') 触发（父组件 PersonalKbView.vue 监听 @close），
// 这里保留 defineEmits 以维持事件的类型契约，无需再取返回值。
defineEmits<{ close: [] }>()

const form = reactive({
  llm_provider: 'openai-compatible', llm_base_url: '', llm_model: '',
  embedding_base_url: '', embedding_model: '',
  chunk_strategy: 'auto', chunk_size: 500, chunk_overlap: 100,
  search_k: 20,
})

const saved = ref(false)
const saving = ref(false)
const err = ref('')
const loadFailed = ref(false)

/**
 * 这块面板的读者是「正在配模型的人」，他需要知道差的是哪一层权限：
 * user_hint 对所有 FORBIDDEN 都是同一句「你没有访问该资源的权限」，分不开
 * 「不在你的租户」和「缺 admin」，所以这里取服务端的具体 message，取不到再退回通用 hint。
 */
function serverError(raw: unknown, fallback: string): string {
  return getApiErrorPayload(raw)?.message || getApiErrorHint(raw, fallback)
}

watch(() => props.open, async (v) => {
  if (!v || !props.kbId) return
  saved.value = false; err.value = ''; loadFailed.value = false
  try {
    const cfg = await fetchKbSettings(props.kbId)
    Object.assign(form, cfg)
  } catch (e) {
    // 后端 403/404 的具体原因要能被看见，否则用户只看到一堆默认值，
    // 以为「这个 kb 还没配过」而不是「我无权看它的配置」。
    err.value = serverError(e, '配置加载失败，当前显示的是默认值')
    loadFailed.value = true
  }
})

async function save() {
  if (!props.kbId || loadFailed.value) return
  saving.value = true; err.value = ''
  try {
    await updateKbSettings(props.kbId, { ...form })
    saved.value = true
  } catch (e) {
    // axios 的 Error.message 只有「Request failed with status code 403」；
    // 服务端的原因在 response.data 里，不取出来用户无从知道差什么权限。
    err.value = serverError(e, '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.rag-settings-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.3);
  display: flex; align-items: center; justify-content: center;
}
.rag-settings-panel {
  background: #fff; border-radius: 12px; width: 520px; max-height: 80vh;
  overflow-y: auto; box-shadow: 0 8px 32px rgba(0,0,0,0.15);
}
.rag-settings-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 20px; border-bottom: 1px solid #eee;
}
.rag-settings-header h3 { margin: 0; font-size: 16px; }
.btn-close { background: none; border: none; font-size: 22px; cursor: pointer; }
.rag-settings-body { padding: 20px; display: flex; flex-direction: column; gap: 16px; }
fieldset { border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px 16px; }
legend { font-weight: 600; font-size: 14px; color: #333; }
label { display: block; margin-top: 8px; font-size: 13px; color: #666; }
label:first-of-type { margin-top: 0; }
input, select { width: 100%; margin-top: 4px; padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; }
.rag-settings-footer {
  display: flex; justify-content: flex-end; align-items: center; gap: 12px;
  padding: 12px 20px; border-top: 1px solid #eee;
}
.btn-save {
  background: #1677ff; color: #fff; border: none; padding: 8px 20px;
  border-radius: 6px; cursor: pointer; font-size: 14px;
}
.btn-save:disabled { opacity: 0.6; cursor: not-allowed; }
.saved-hint { color: #52c41a; font-size: 13px; }
.error-hint { color: #ff4d4f; font-size: 13px; }
</style>
