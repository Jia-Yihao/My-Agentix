import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import HomeWelcomeHero from './HomeWelcomeHero.vue'

describe('HomeWelcomeHero', () => {
  it('explains that the assistant remains available when knowledge search is off', () => {
    const wrapper = mount(HomeWelcomeHero, {
      props: { knowledgeEnabled: false },
    })

    expect(wrapper.text()).toContain('知识库检索已关闭，可以直接向智能助手提问')
    expect(wrapper.text()).not.toContain('先选择或创建一个知识库')
  })

  it('asks for a knowledge base only when knowledge search is enabled', () => {
    const wrapper = mount(HomeWelcomeHero, {
      props: { knowledgeEnabled: true },
    })

    expect(wrapper.text()).toContain('先选择或创建一个知识库，再开始提问')
  })

  it('emits the selected suggestion as a prompt', async () => {
    const wrapper = mount(HomeWelcomeHero, {
      props: { knowledgeEnabled: false },
    })

    await wrapper.get('.suggestion-card').trigger('click')

    expect(wrapper.emitted('select-prompt')?.[0]).toEqual([
      '借条没有写还款日期，诉讼时效从什么时候起算？',
    ])
  })
})
