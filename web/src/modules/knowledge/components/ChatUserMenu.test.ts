import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { currentUser } from '../auth/session'
import ChatUserMenu from './ChatUserMenu.vue'

describe('ChatUserMenu', () => {
  it('provides keyboard-accessible account and logout menu controls', async () => {
    const originalUser = currentUser.value
    currentUser.value = { id: 'test-user', email: 'test@example.com', username: 'tester' }
    const wrapper = mount(ChatUserMenu)

    expect(wrapper.get('.user-info').element.tagName).toBe('BUTTON')
    expect(wrapper.get('.user-info').attributes('aria-haspopup')).toBe('menu')
    await wrapper.get('.user-info').trigger('click')
    expect(wrapper.get('[role="menuitem"]').element.tagName).toBe('BUTTON')
    expect(wrapper.get('.user-info').attributes('aria-expanded')).toBe('true')

    wrapper.unmount()
    currentUser.value = originalUser
  })
})
