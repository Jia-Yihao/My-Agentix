import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it } from 'vitest'

import ChatSidebar from './ChatSidebar.vue'

describe('ChatSidebar', () => {
  it('exposes an expand control when collapsed', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    const wrapper = mount(ChatSidebar, {
      props: {
        variant: 'home',
        collapsed: true,
        conversations: [],
      },
      global: { plugins: [router] },
    })

    const expandButton = wrapper.get('.sidebar-collapse')
    expect(expandButton.attributes('aria-label')).toBe('展开侧边栏')
    await expandButton.trigger('click')
    expect(wrapper.emitted('toggle-sidebar')).toHaveLength(1)
  })
})
