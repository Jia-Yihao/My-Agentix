import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ComposerBox from './ComposerBox.vue'

describe('ComposerBox', () => {
  it('renders placeholder and emits send when enabled', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '测试问题',
        placeholder: '请输入问题',
        disabledSend: false,
      },
    })

    expect(wrapper.find('.composer-textarea').attributes('placeholder')).toBe('请输入问题')
    await wrapper.find('.composer-send-btn').trigger('click')
    expect(wrapper.emitted('send')).toHaveLength(1)
  })

  it('does not emit send when disabled', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        disabledSend: true,
      },
    })

    await wrapper.find('.composer-send-btn').trigger('click')
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('gives the send control an accessible label', () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '测试问题',
        disabledSend: false,
      },
    })

    expect(wrapper.get('.composer-send-btn').attributes('aria-label')).toBe('发送消息')
  })

  it('exposes the knowledge base as an optional composer switch', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        enableKbPicker: true,
        knowledgeEnabled: true,
        kbOptions: [{ id: 'kb-1', name: '民法典知识库', type: '个人知识库' }],
        selectedKbId: 'kb-1',
      },
    })

    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('true')
    expect(wrapper.get('.kb-select-button').text()).toContain('民法典知识库')
    await wrapper.get('[role="switch"]').trigger('click')
    expect(wrapper.emitted('update:knowledgeEnabled')?.[0]).toEqual([false])
  })

  it('prevents switching chat backends while a response is streaming', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        knowledgeEnabled: true,
        knowledgeToggleDisabled: true,
      },
    })

    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[role="switch"]').trigger('click')
    expect(wrapper.emitted('update:knowledgeEnabled')).toBeUndefined()
  })

  it('keeps the assistant attachment available when the knowledge switch is off', () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        enableAttachment: true,
        knowledgeEnabled: false,
        attachmentLabel: '上传助手附件',
        attachmentAccept: '.txt,.md,.docx',
        kbOptions: [{ id: 'kb-1', name: '民法典知识库', type: '个人知识库' }],
      },
    })

    expect(wrapper.find('.kb-select-button').exists()).toBe(false)
    expect(wrapper.get('.attachment-button').text()).toContain('上传助手附件')
    expect(wrapper.get('input[type="file"]').attributes('accept')).toContain('.docx')
  })

  it('requires a selected knowledge base before allowing PDF upload', () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        enableAttachment: true,
        knowledgeEnabled: true,
        kbOptions: [{ id: 'kb-1', name: '民法典知识库', type: '个人知识库' }],
        selectedKbId: null,
      },
    })

    expect(wrapper.get('.attachment-button').attributes('disabled')).toBeDefined()
  })

  it('opens and closes the knowledge picker from its button', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        knowledgeEnabled: true,
        kbOptions: [{ id: 'kb-1', name: '民法典知识库', type: '个人知识库' }],
        selectedKbId: 'kb-1',
      },
    })
    const picker = wrapper.get('.kb-select-button')

    await picker.trigger('click')
    expect(wrapper.find('.kb-dropdown-item').exists()).toBe(true)
    await picker.trigger('click')
    expect(wrapper.find('.kb-dropdown-item').exists()).toBe(false)
  })

  it('emits the selected PDF when attachment upload is enabled', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        enableAttachment: true,
        knowledgeEnabled: true,
      },
    })
    const file = new File(['test pdf'], 'reference.pdf', { type: 'application/pdf' })
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })

    await input.trigger('change')

    expect(wrapper.emitted('attachment')?.[0]).toEqual([file])
    expect((input.element as HTMLInputElement).value).toBe('')
  })

  it('disables attachment selection while a file is uploading', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        enableAttachment: true,
        knowledgeEnabled: true,
        uploading: true,
        uploadProgress: 45,
      },
    })

    expect(wrapper.get('.attachment-button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.attachment-button').text()).toContain('45%')
  })

  // ---------------------------------------------------------------------------
  // W6 B7：流式期间发送键变「停止」
  // ---------------------------------------------------------------------------

  it('streaming 时发送键切换为停止键（点它 emit stop 而非 send）', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '问题',
        disabledSend: false,
        streaming: true,
      },
    })

    const btn = wrapper.find('.composer-send-btn')
    expect(btn.classes()).toContain('is-stop')
    expect(btn.attributes('aria-label')).toBe('停止生成')
    await btn.trigger('click')
    expect(wrapper.emitted('stop')).toHaveLength(1)
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('非 streaming 恢复发送键（不 emit stop）', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '问题',
        disabledSend: false,
        streaming: false,
      },
    })

    const btn = wrapper.find('.composer-send-btn')
    expect(btn.classes()).not.toContain('is-stop')
    await btn.trigger('click')
    expect(wrapper.emitted('send')).toHaveLength(1)
    expect(wrapper.emitted('stop')).toBeUndefined()
  })

  it('streaming 且输入为空时停止键仍可点（与发送禁用解耦）', async () => {
    const wrapper = mount(ComposerBox, {
      props: {
        modelValue: '',
        disabledSend: true,
        streaming: true,
      },
    })

    await wrapper.find('.composer-send-btn').trigger('click')
    expect(wrapper.emitted('stop')).toHaveLength(1)
  })
})
