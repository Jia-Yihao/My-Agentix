import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import KbRagSettings from './KbRagSettings.vue'

// Mock API
vi.mock('../api/settings', () => ({
  fetchKbSettings: vi.fn().mockResolvedValue({
    llm_provider: 'openai-compatible',
    llm_base_url: 'http://localhost:1234/v1',
    llm_model: 'qwen2.5-7b',
    embedding_base_url: 'http://localhost:1234/v1',
    embedding_model: 'text-embedding-bge-m3',
    chunk_strategy: 'auto',
    chunk_size: 500,
    chunk_overlap: 100,
    search_k: 5,
    sparse_k: 10,
    dense_k: 10,
    fusion_k: 15,
  }),
  updateKbSettings: vi.fn().mockResolvedValue({}),
}))

describe('KbRagSettings', () => {
  it('renders when open', () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    expect(wrapper.text()).toContain('RAG 模型配置')
    expect(wrapper.text()).toContain('测试知识库')
  })

  it('does not render when closed', () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: false,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    expect(wrapper.find('.rag-settings-panel').exists()).toBe(false)
  })

  it('emits close when clicking overlay', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await wrapper.find('.rag-settings-overlay').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('emits close when clicking close button', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await wrapper.find('.btn-close').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('displays LLM settings', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    // Wait for settings to load
    await vi.dynamicImportSettled()

    expect(wrapper.text()).toContain('LLM 模型')
    expect(wrapper.text()).toContain('提供商')
    expect(wrapper.text()).toContain('端点 URL')
    expect(wrapper.text()).toContain('模型名')
  })

  it('displays Embedding settings', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await vi.dynamicImportSettled()

    expect(wrapper.text()).toContain('Embedding')
  })

  it('displays chunk settings', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await vi.dynamicImportSettled()

    expect(wrapper.text()).toContain('切分策略')
    expect(wrapper.text()).toContain('块大小')
    expect(wrapper.text()).toContain('重叠')
  })

  it('displays retrieval settings', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await vi.dynamicImportSettled()

    expect(wrapper.text()).toContain('检索参数')
    expect(wrapper.text()).toContain('返回数 (K)')
    expect(wrapper.text()).toContain('稀疏召回')
    expect(wrapper.text()).toContain('稠密召回')
  })

  it('has save button', async () => {
    const wrapper = mount(KbRagSettings, {
      props: {
        open: true,
        kbId: 'kb-1',
        kbName: '测试知识库',
      },
    })

    await vi.dynamicImportSettled()

    expect(wrapper.find('.btn-save').exists()).toBe(true)
    expect(wrapper.find('.btn-save').text()).toContain('保存')
  })
})
