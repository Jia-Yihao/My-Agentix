<template>
  <div class="vdb-page">
    <header class="vdb-header">
      <h1>向量数据库管理</h1>
      <div class="vdb-stats">
        <span class="stat">{{ stats.total_documents }} 文档</span>
        <span class="stat">{{ stats.total_chunks }} 片段</span>
        <span class="stat">后端: {{ stats.backend }}</span>
      </div>
    </header>

    <div class="vdb-toolbar">
      <div class="vdb-search">
        <input v-model="search" placeholder="搜索文档名称、内容、标签..." @input="onSearch" />
      </div>
      <select v-model="categoryFilter" @change="loadDocuments">
        <option value="">所有分类</option>
        <option v-for="cat in categories" :key="cat" :value="cat">{{ cat }}</option>
      </select>
      <button class="btn-classify" :disabled="classifying" @click="runClassify">
        {{ classifying ? '分类中...' : '智能分类' }}
      </button>
      <button class="btn-upload" @click="triggerUpload">上传文档</button>
      <input ref="uploadInput" type="file" accept=".pdf" multiple style="display:none" @change="handleUpload" />
    </div>

    <div v-if="!kbId" class="vdb-hint">
      请先在知识库页面上传文档，或进入「个人知识库」后刷新本页。
    </div>

    <div class="vdb-grid">
      <div v-for="doc in documents" :key="doc.id" class="vdb-card">
        <div class="vdb-card-header">
          <span class="vdb-category-tag">{{ doc.category }}</span>
          <button class="vdb-delete-btn" title="删除" @click="confirmDelete(doc)">×</button>
        </div>
        <div class="vdb-card-title" :title="doc.title">{{ doc.title }}</div>
        <div class="vdb-card-tags">
          <span v-for="tag in doc.tags" :key="tag" class="vdb-tag">{{ tag }}</span>
          <span v-if="!doc.tags.length" class="vdb-tag vdb-tag--none">无标签</span>
        </div>
        <div class="vdb-card-meta">
          <span>{{ doc.chunk_count }} chunks</span>
          <span v-if="doc.size">· {{ formatSize(doc.size) }}</span>
          <span>· {{ doc.status }}</span>
        </div>
      </div>
    </div>

    <div v-if="loading" class="vdb-loading">加载中...</div>
    <div v-if="!loading && !documents.length" class="vdb-empty">暂无索引文档</div>

    <div class="vdb-pagination" v-if="totalPages > 1">
      <button :disabled="page <= 1" @click="page--; loadDocuments()">上一页</button>
      <span>{{ page }} / {{ totalPages }}</span>
      <button :disabled="page >= totalPages" @click="page++; loadDocuments()">下一页</button>
    </div>

    <div v-if="uploading" class="vdb-upload-overlay">
      <div class="vdb-upload-progress">
        <div class="vdb-upload-bar"><div class="vdb-upload-fill" :style="{ width: uploadProgress + '%' }"></div></div>
        <span>上传中... {{ uploadProgress }}%</span>
      </div>
    </div>

    <div v-if="showDeleteConfirm" class="vdb-overlay" @click.self="showDeleteConfirm = null">
      <div class="vdb-confirm-box">
        <h3>确认删除</h3>
        <p>确定要从向量库中删除「{{ showDeleteConfirm.title }}」吗？此操作不可撤销。</p>
        <div class="vdb-confirm-actions">
          <button @click="showDeleteConfirm = null">取消</button>
          <button class="btn-danger" :disabled="deleting" @click="doDelete">{{ deleting ? '删除中...' : '确认删除' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  fetchVectorDbStats, fetchVectorDbDocuments, deleteVectorDbDocument, classifyDocuments,
  type VectorDbDoc, type VectorDbStats,
} from '../api/vectorDb'
import type { AxiosProgressEvent } from 'axios'
import { http } from '../api/http'
import { listKnowledgeBases } from '../api/kb'

const router = useRouter()
const documents = ref<VectorDbDoc[]>([])
const stats = ref<VectorDbStats>({ total_documents: 0, total_chunks: 0, collections: 0, backend: 'local' })
const search = ref('')
const categoryFilter = ref('')
const kbId = ref('')
const page = ref(1)
const pageSize = 20
const total = ref(0)
const loading = ref(false)
const classifying = ref(false)
const uploading = ref(false)
const uploadProgress = ref(0)
const deleting = ref(false)
const showDeleteConfirm = ref<VectorDbDoc | null>(null)
const uploadInput = ref<HTMLInputElement | null>(null)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const categories = computed(() => [...new Set(documents.value.map(d => d.category))].filter(Boolean))

let searchTimer: ReturnType<typeof setTimeout> | null = null

function onSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { page.value = 1; loadDocuments() }, 300)
}

async function loadDocuments() {
  loading.value = true
  try {
    const res = await fetchVectorDbDocuments({
      search: search.value, category: categoryFilter.value,
      page: page.value, page_size: pageSize,
    })
    documents.value = res.data
    total.value = res.total
  } catch { documents.value = []; total.value = 0 }
  finally { loading.value = false }
}

async function loadStats() {
  try { stats.value = await fetchVectorDbStats() } catch {}
}

async function runClassify() {
  classifying.value = true
  try {
    const ids = documents.value.map(d => d.id)
    await classifyDocuments(ids)
    await loadDocuments()
  } finally { classifying.value = false }
}

function confirmDelete(doc: VectorDbDoc) { showDeleteConfirm.value = doc }
async function doDelete() {
  if (!showDeleteConfirm.value || deleting.value) return
  deleting.value = true
  try {
    await deleteVectorDbDocument(showDeleteConfirm.value.id)
    showDeleteConfirm.value = null
    await loadDocuments()
    await loadStats()
  } finally { deleting.value = false }
}

function triggerUpload() {
  if (!kbId.value) { alert('请先选择一个知识库'); router.push('/knowledge/kb'); return }
  uploadInput.value?.click()
}
async function handleUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files
  if (!files?.length || !kbId.value) return
  uploading.value = true; uploadProgress.value = 0
  const total = files.length
  for (let i = 0; i < total; i++) {
    try {
      const fd = new FormData()
      fd.append('file', files[i])
      // 不手动设 Content-Type：FormData 交 axios/浏览器自动带 boundary
      // （手动设 'multipart/form-data' 不带 boundary 是经典坑；axios 1.x 虽会
      // 兜底清掉，但显式删除更不易误读）
      await http.post('/documents', fd, {
        params: { kb_id: kbId.value },
        onUploadProgress: (evt: AxiosProgressEvent) => {
          if (evt.total) uploadProgress.value = Math.round(((i + (evt.loaded / evt.total)) / total) * 100)
        },
      })
    } catch {}
  }
  uploading.value = false
  input.value = ''
  await loadDocuments()
  await loadStats()
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

onMounted(async () => {
  loadStats()
  loadDocuments()
  // 上传目标兜底：kbId 原为空 ref 且无任何赋值路径，上传按钮永远走「请先选择」
  // alert 分支（实测 bug）。与工作台同源拉知识库列表，默认选中第一个；
  // 列表为空时保留原守卫（引导用户先去建库）。
  try {
    const kbs = await listKnowledgeBases()
    if (!kbId.value && kbs.length > 0) kbId.value = kbs[0]!.id
  } catch { /* 拉取失败保持空值，由 triggerUpload 守卫兜底 */ }
})
onUnmounted(() => { if (searchTimer) clearTimeout(searchTimer) })
</script>

<style scoped>
.vdb-page { padding: 24px; max-width: 1200px; margin: 0 auto; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif; }
.vdb-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.vdb-header h1 { font-size: 24px; margin: 0; }
.vdb-stats { display: flex; gap: 16px; }
.stat { background: #f0f5ff; color: #1677ff; padding: 4px 12px; border-radius: 12px; font-size: 13px; }
.vdb-toolbar { display: flex; gap: 12px; margin-bottom: 20px; align-items: center; }
.vdb-search input { width: 260px; padding: 8px 12px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 14px; }
select { padding: 8px 12px; border: 1px solid #d9d9d9; border-radius: 6px; font-size: 14px; }
.btn-classify { padding: 8px 16px; background: #722ed1; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
.btn-classify:disabled { opacity: 0.6; }
.btn-upload { padding: 8px 16px; background: #1677ff; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; margin-left: auto; }
.vdb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.vdb-card { background: #fff; border: 1px solid #e8e8e8; border-radius: 10px; padding: 16px; transition: box-shadow 0.2s; }
.vdb-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
.vdb-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.vdb-category-tag { background: #e6f7ff; color: #1890ff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.vdb-delete-btn { background: none; border: none; color: #ff4d4f; font-size: 18px; cursor: pointer; padding: 0 4px; }
.vdb-card-title { font-size: 15px; font-weight: 500; margin-bottom: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vdb-card-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.vdb-tag { background: #f5f5f5; color: #666; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.vdb-tag--none { color: #ccc; }
.vdb-card-meta { font-size: 12px; color: #999; }
.vdb-loading, .vdb-empty { text-align: center; padding: 48px; color: #999; font-size: 15px; }
.vdb-pagination { display: flex; justify-content: center; gap: 16px; margin-top: 24px; align-items: center; }
.vdb-pagination button { padding: 6px 14px; border: 1px solid #d9d9d9; border-radius: 6px; background: #fff; cursor: pointer; }
.vdb-pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
.vdb-upload-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.vdb-upload-progress { background: #fff; padding: 32px; border-radius: 12px; text-align: center; min-width: 300px; }
.vdb-upload-bar { height: 8px; background: #e8e8e8; border-radius: 4px; overflow: hidden; margin-bottom: 12px; }
.vdb-upload-fill { height: 100%; background: #1677ff; border-radius: 4px; transition: width 0.3s; }
.vdb-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.vdb-confirm-box { background: #fff; padding: 24px; border-radius: 12px; max-width: 400px; width: 90%; }
.vdb-confirm-box h3 { margin: 0 0 12px; font-size: 18px; }
.vdb-confirm-box p { color: #666; margin-bottom: 20px; }
.vdb-confirm-actions { display: flex; gap: 12px; justify-content: flex-end; }
.vdb-confirm-actions button { padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 14px; }
.btn-danger { background: #ff4d4f; color: #fff; border: none; }
</style>
