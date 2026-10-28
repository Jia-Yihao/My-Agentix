// T14 虚拟滚动组件行为（默认关）。
//
// 覆盖面与验收口径：
// - 默认（不传 virtual）/ 显式 virtual=false ⇒ 全量渲染 `.modal-message`，且没有任何占位节点
//   （= 与基线逐位等价）；
// - virtual=true 且消息数远大于窗口 ⇒ 只渲染窗口内消息（数量 < 总数），顶/底两个占位元素存在；
// - 虚拟模式下批次 C1 折叠中段机制仍以 displayMessages 为数据源，折叠条随窗口滚进滚出。
//
// 断言只走 DOM 与纯函数返回值，不依赖真实滚动时序：happy-dom 无布局，直接把 scrollTop
// 定义到目标值再触发 scroll 事件即可（组件在量到 clientHeight=0 时回落默认视口高度）。
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ChatMessageList from './ChatMessageList.vue'

type ChatModalMessageArg = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

function makeMessages(count: number): ChatModalMessageArg[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `m${i}`,
    role: 'user' as const,
    content: `消息${i}`,
  }))
}

function mountList(messages: ChatModalMessageArg[], virtual?: boolean) {
  return mount(ChatMessageList, {
    props: virtual === undefined ? { messages } : { messages, virtual },
  })
}

type ListWrapper = ReturnType<typeof mountList>

/** 把滚动位置摆到 scrollTop 像素并触发 scroll（happy-dom 无布局，故不靠真实滚动） */
async function scrollTo(wrapper: ListWrapper, scrollTop: number): Promise<void> {
  Object.defineProperty(wrapper.element, 'scrollTop', {
    value: scrollTop,
    configurable: true,
    writable: true,
  })
  await wrapper.trigger('scroll')
}

/** 读取占位元素的内联高度（px）；元素不存在则由 find 的 exists 断言兜住 */
function spacerHeightPx(wrapper: ListWrapper, which: 'top' | 'bottom'): number {
  const el = wrapper.find(`.virtual-spacer--${which}`)
  expect(el.exists()).toBe(true)
  return Number.parseFloat(el.attributes('style')!.replace('height:', ''))
}

describe('ChatMessageList 虚拟滚动（T14，默认关）', () => {
  it('不传 virtual ⇒ 全部消息照常渲染，且无占位元素（与基线一致）', () => {
    const wrapper = mountList(makeMessages(120))

    expect(wrapper.findAll('.modal-message')).toHaveLength(120)
    expect(wrapper.find('.virtual-spacer').exists()).toBe(false)
    expect(wrapper.text()).toContain('消息0')
    expect(wrapper.text()).toContain('消息119')
  })

  it('显式 virtual=false ⇒ 与不传等价（全量渲染、无占位）', () => {
    const wrapper = mountList(makeMessages(120), false)

    expect(wrapper.findAll('.modal-message')).toHaveLength(120)
    expect(wrapper.find('.virtual-spacer').exists()).toBe(false)
  })

  it('virtual=true ⇒ 只渲染窗口内消息，顶/底占位按窗口外条数撑高', async () => {
    const total = 150
    const wrapper = mountList(makeMessages(total), true)

    // 初始停在顶部：上方无占位，下方占位撑起剩余内容
    const initialRows = wrapper.findAll('.modal-message').length
    expect(initialRows).toBeGreaterThan(0)
    expect(initialRows).toBeLessThan(total)
    expect(spacerHeightPx(wrapper, 'top')).toBe(0)
    expect(spacerHeightPx(wrapper, 'bottom')).toBeGreaterThan(0)

    // 滚到中部（估算行高 72 ⇒ 第 50 条附近）：上下占位都非 0
    await scrollTo(wrapper, 50 * 72)

    const midRows = wrapper.findAll('.modal-message').length
    expect(midRows).toBeGreaterThan(0)
    expect(midRows).toBeLessThan(total)
    expect(spacerHeightPx(wrapper, 'top')).toBeGreaterThan(0)
    expect(spacerHeightPx(wrapper, 'bottom')).toBeGreaterThan(0)
    // 窗口内是第 50 条附近，首条消息已不在 DOM 里
    expect(wrapper.text()).toContain('消息50')
    expect(wrapper.text()).not.toContain('消息0')

    // 滚回顶部：上方占位归 0（占位随滚动重算，不是静态撑高）
    await scrollTo(wrapper, 0)
    expect(spacerHeightPx(wrapper, 'top')).toBe(0)
    expect(wrapper.text()).toContain('消息0')
  })

  it('virtual=true 时折叠中段仍生效，折叠条随窗口滚进滚出', async () => {
    // 250 条 ⇒ 批次 C1 折叠为「首 50 + 尾 150」= 200 条数据源（中段 50 条被折叠）
    const wrapper = mountList(makeMessages(250), true)

    // 窗口在顶部（首 50 条之外）：折叠条不在窗口内，不渲染
    expect(wrapper.findAll('.modal-message').length).toBeLessThan(200)
    expect(wrapper.find('.message-fold-bar').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('消息60')

    // 滚到第 50 条附近：折叠条随窗口进入 DOM
    await scrollTo(wrapper, 50 * 72)
    const bar = wrapper.find('.message-fold-bar')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('50')
    // 折叠仍生效：中段消息依旧不在数据源里
    expect(wrapper.text()).not.toContain('消息60')
  })
})
