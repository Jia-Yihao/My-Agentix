import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import KbRagSettings from './KbRagSettings.vue'
import { fetchKbSettings, updateKbSettings } from '../api/settings'
import type { RagConfig } from '../api/settings'

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
    search_k: 20,
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
    expect(wrapper.text()).toContain('Dense + BM25/z-score')
    expect(wrapper.text()).toContain('返回数 (K)')
    expect(wrapper.text()).not.toContain('Reranker')
    expect(wrapper.text()).not.toContain('融合候选')
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

// §24.3 T1 ⑪：后端把 SETTINGS_AUTHZ_ENABLED 打开后，这块面板第一次会出现
// 「已登录但无权」的 403/404。原实现 GET 失败静默吞掉、PUT 失败只显示
// axios 的「Request failed with status code 403」，两种情况都让配置者无法判断
// 自己差的是哪一层权限；且 GET 失败后表单里躺着一整份前端默认值，再点保存
// 会把默认值整批盖到该 kb 的真实配置上。下面钉住这两条。

type AxiosishError = Error & { response?: { status: number; data: unknown } }

/** 造一个与 agentix/retrieval/api/response.py::error() 同形状的 axios 错误 */
function apiError(status: number, errorCode: string, message: string, userHint: string): AxiosishError {
  const err: AxiosishError = new Error(`Request failed with status code ${status}`)
  err.response = {
    status,
    data: { request_id: 'req-1', error_code: errorCode, message, user_hint: userHint },
  }
  return err
}

const FORBIDDEN_OTHER_TENANT = apiError(403, 'FORBIDDEN', '无权访问该知识库', '你没有访问该资源的权限')
const FORBIDDEN_NOT_ADMIN = apiError(403, 'FORBIDDEN', '需要管理员权限', '你没有访问该资源的权限')

const LOADED_CONFIG: Partial<RagConfig> = { llm_model: 'qwen2.5-7b-instruct', search_k: 5 }

/** 面板是 mount(open=false) 后再置 true 才触发 watch 拉取的 */
async function openPanel(kbId = 'kb-1') {
  const wrapper = mount(KbRagSettings, { props: { open: false, kbId, kbName: '测试知识库' } })
  await wrapper.setProps({ open: true })
  return wrapper
}

describe('KbRagSettings 授权错误可见性', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('GET 403 显示服务端的具体原因，而不是通用 user_hint', async () => {
    vi.mocked(fetchKbSettings).mockRejectedValueOnce(FORBIDDEN_OTHER_TENANT)
    const wrapper = await openPanel('kb-other-tenant')

    expect(wrapper.find('.error-hint').text()).toBe('无权访问该知识库')
  })

  it('GET 失败时保存按钮禁用，防止把前端默认值盖成真实配置', async () => {
    vi.mocked(fetchKbSettings).mockRejectedValueOnce(FORBIDDEN_OTHER_TENANT)
    const wrapper = await openPanel()

    expect(wrapper.find('.btn-save').attributes('disabled')).toBeDefined()
    await wrapper.find('.btn-save').trigger('click')
    expect(updateKbSettings).not.toHaveBeenCalled()
  })

  it('GET 非授权失败（无响应体）沿用共享助手的 Error.message，同样禁用保存', async () => {
    // getApiErrorHint 在没有响应体时优先 Error.message、最后才用兜底文案，
    // 这里钉住「不论失败形态，loadFailed 都会拦住保存」这条与文案无关的行为。
    vi.mocked(fetchKbSettings).mockRejectedValueOnce(new Error('Network Error'))
    const wrapper = await openPanel()

    expect(wrapper.find('.error-hint').text()).toBe('Network Error')
    expect(wrapper.find('.btn-save').attributes('disabled')).toBeDefined()
  })

  it('GET 完全没有 message 时回落到兜底文案', async () => {
    vi.mocked(fetchKbSettings).mockRejectedValueOnce({})
    const wrapper = await openPanel()

    expect(wrapper.find('.error-hint').text()).toBe('配置加载失败，当前显示的是默认值')
  })

  it('GET 成功后保存可用且真的发出 PUT', async () => {
    vi.mocked(fetchKbSettings).mockResolvedValueOnce(LOADED_CONFIG as RagConfig)
    const wrapper = await openPanel()

    expect(wrapper.find('.error-hint').exists()).toBe(false)
    expect(wrapper.find('.btn-save').attributes('disabled')).toBeUndefined()
    await wrapper.find('.btn-save').trigger('click')
    expect(updateKbSettings).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.saved-hint').text()).toBe('已保存')
  })

  it('PUT 403 显示「需要管理员权限」而不是 axios 的状态码文案', async () => {
    vi.mocked(updateKbSettings).mockRejectedValueOnce(FORBIDDEN_NOT_ADMIN)
    const wrapper = await openPanel()

    await wrapper.find('.btn-save').trigger('click')

    const hint = wrapper.find('.error-hint').text()
    expect(hint).toBe('需要管理员权限')
    expect(hint).not.toContain('status code')
    // 失败时不能同时挂着「已保存」
    expect(wrapper.find('.saved-hint').exists()).toBe(false)
  })
})
