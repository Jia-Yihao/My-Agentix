import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import ChatMessageList from './ChatMessageList.vue'

const citation = {
  chunk_id: 'c1',
  snippet: '引用片段的完整内容',
  source: 'demo.pdf',
  page_no: 3,
  document_id: 'doc-42',
}

function mountList() {
  return mount(ChatMessageList, {
    props: {
      messages: [
        {
          id: 'a1',
          role: 'assistant' as const,
          content: '根据文档…',
          citations: [citation],
        },
      ],
    },
  })
}

/** 卡片 Teleport 到 body，因此从 document.body 上取 */
function cardInBody(): HTMLElement | null {
  return document.body.querySelector('.citation-preview-card')
}

const wrappers: { unmount: () => void }[] = []

afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount())
  document.body.innerHTML = ''
})

function mountTracked() {
  const wrapper = mountList()
  wrappers.push(wrapper)
  return wrapper
}

/** 用指定 citation 挂载并纳入 afterEach 清理 */
function mountTrackedWith(citation: Record<string, unknown>) {
  const wrapper = mount(ChatMessageList, {
    props: {
      messages: [
        {
          id: 'a1',
          role: 'assistant' as const,
          content: '根据文档…',
          citations: [citation as never],
        },
      ],
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

function wait(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

describe('引用来源悬浮预览卡片', () => {
  it('默认不渲染卡片，内联引用列表保持不变', async () => {
    const wrapper = mountTracked()

    expect(cardInBody()).toBeNull()
    expect(wrapper.text()).toContain('引用来源')
    expect(wrapper.text()).toContain('demo.pdf')
    expect(wrapper.text()).toContain('引用片段')
  })

  it('悬停引用项时在 body 上叠加显示更完整的预览内容', async () => {
    const wrapper = mountTracked()

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    const card = cardInBody()
    expect(card).not.toBeNull()
    expect(card?.textContent).toContain('demo.pdf')
    expect(card?.textContent).toContain('第 3 页')
    expect(card?.textContent).toContain('doc-42')
    expect(card?.textContent).toContain('引用片段的完整内容')
  })

  it('聚焦引用项同样触发卡片（键盘可达）', async () => {
    const wrapper = mountTracked()

    await wrapper.find('.source-item').trigger('focusin')
    await nextTick()

    expect(cardInBody()).not.toBeNull()
  })

  it('召回溯源：预览卡头部展示片段标号', async () => {
    const wrapper = mountTrackedWith({ ...citation, ref: 'S2', cited: true })

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    expect(cardInBody()?.querySelector('.citation-preview-ref')?.textContent).toBe('[S2]')
  })

  it('召回溯源：无标号时不渲染标号位（老数据兼容）', async () => {
    const wrapper = mountTracked()

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    expect(cardInBody()?.querySelector('.citation-preview-ref')).toBeNull()
  })

  it('鼠标离开后延迟隐藏，指针移入卡片则保留', async () => {
    const wrapper = mountTracked()

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()
    expect(cardInBody()).not.toBeNull()

    await wrapper.find('.source-item').trigger('mouseleave')
    await nextTick()
    // 延迟期内仍可见，留出指针移入卡片的时间
    expect(cardInBody()).not.toBeNull()

    cardInBody()?.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()
    await wait(200)
    expect(cardInBody()).not.toBeNull()

    cardInBody()?.dispatchEvent(new MouseEvent('mouseleave'))
    await wait(200)
    expect(cardInBody()).toBeNull()
  })

  it('Escape 立即隐藏卡片', async () => {
    const wrapper = mountTracked()

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()
    expect(cardInBody()).not.toBeNull()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wait(200)

    expect(cardInBody()).toBeNull()
  })
})

/** happy-dom 没有布局引擎，offsetWidth/Height 恒为 0，需要打桩才能验证定位算法 */
function stubBox(viewport: { width: number; height: number }, card: { width: number; height: number }) {
  const windowWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth')!
  const windowHeight = Object.getOwnPropertyDescriptor(window, 'innerHeight')!
  const elementWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!
  const elementHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')!

  Object.defineProperty(window, 'innerWidth', { value: viewport.width, configurable: true })
  Object.defineProperty(window, 'innerHeight', { value: viewport.height, configurable: true })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { get: () => card.width, configurable: true })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { get: () => card.height, configurable: true })

  return () => {
    Object.defineProperty(window, 'innerWidth', windowWidth)
    Object.defineProperty(window, 'innerHeight', windowHeight)
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', elementWidth)
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', elementHeight)
  }
}

function stubAnchorRect(element: HTMLElement, rect: Partial<DOMRect>) {
  element.getBoundingClientRect = () =>
    ({
      top: 0,
      bottom: 0,
      left: 0,
      right: 0,
      width: 0,
      height: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
      ...rect,
    }) as DOMRect
}

describe('引用预览卡片定位', () => {
  it('空间充足时叠加覆盖在锚点之上（左上角对齐锚点）', async () => {
    const restore = stubBox({ width: 1024, height: 800 }, { width: 300, height: 200 })
    try {
      const wrapper = mountTracked()
      const item = wrapper.find('.source-item').element as HTMLElement
      stubAnchorRect(item, { top: 100, bottom: 120, left: 200 })

      await wrapper.find('.source-item').trigger('mouseenter')
      await nextTick()
      await nextTick()

      const card = cardInBody()
      expect(card?.style.top).toBe('100px')
      expect(card?.style.left).toBe('200px')
      // Teleport 到 body：脱离组件子树（及所有 overflow / transform 容器），fixed 才不会被裁剪
      expect(document.body.contains(card)).toBe(true)
      expect(wrapper.element.contains(card)).toBe(false)
    } finally {
      restore()
    }
  })

  it('贴近视口底部时向上夹紧，卡片不超出视口', async () => {
    const restore = stubBox({ width: 1024, height: 768 }, { width: 300, height: 200 })
    try {
      const wrapper = mountTracked()
      const item = wrapper.find('.source-item').element as HTMLElement
      stubAnchorRect(item, { top: 700, bottom: 720, left: 200 })

      await wrapper.find('.source-item').trigger('mouseenter')
      await nextTick()
      await nextTick()

      // 叠加定位 top=anchor.top=700，但 700+200 > 768-12，向上夹紧到 768-200-12=556
      expect(cardInBody()?.style.top).toBe('556px')
    } finally {
      restore()
    }
  })

  it('上下空间都不足时夹紧在视口内，且横向不超出视口', async () => {
    const restore = stubBox({ width: 1024, height: 300 }, { width: 300, height: 400 })
    try {
      const wrapper = mountTracked()
      const item = wrapper.find('.source-item').element as HTMLElement
      stubAnchorRect(item, { top: 100, bottom: 120, left: 900 })

      await wrapper.find('.source-item').trigger('mouseenter')
      await nextTick()
      await nextTick()

      const card = cardInBody()
      expect(card?.style.top).toBe('12px')
      expect(card?.style.left).toBe('712px')
    } finally {
      restore()
    }
  })
})

describe('引用卡片文件级元信息', () => {
  it('有文件信息时展示文件名/扩展名/大小/目录，且内联列表优先显示文件名', async () => {
    const wrapper = mountTrackedWith({
      chunk_id: 'c1',
      snippet: '引用片段',
      source: 'source-name.pdf',
      page_no: 3,
      document_id: 'doc-42',
      document_title: '演示文档',
      file_name: 'demo.pdf',
      folder_path: '制度/公开',
      file_size: 1536,
      file_ext: 'pdf',
    })

    // 内联列表：file_name 优先，取不到才回退 source
    expect(wrapper.text()).toContain('demo.pdf')
    expect(wrapper.text()).not.toContain('source-name.pdf')

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    const card = cardInBody()
    expect(card?.querySelector('.citation-preview-source')?.textContent).toBe('demo.pdf')
    expect(card?.querySelector('.citation-preview-ext')?.textContent).toBe('pdf')
    expect(card?.querySelector('.citation-preview-meta')?.textContent).toContain('制度/公开')
    expect(card?.querySelector('.citation-preview-meta')?.textContent).toContain('1.5 KB')
    expect(card?.textContent).toContain('第 3 页')
    expect(card?.textContent).toContain('doc-42')
    expect(card?.textContent).toContain('引用片段')
  })

  it('旧数据没有文件字段时不渲染对应元素，也不出现 undefined / null', async () => {
    const wrapper = mountTrackedWith({
      chunk_id: 'c1',
      snippet: '引用片段',
      source: 'demo.pdf',
    })

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    const card = cardInBody()
    expect(card?.querySelector('.citation-preview-meta')).toBeNull()
    expect(card?.querySelector('.citation-preview-ext')).toBeNull()
    expect(card?.querySelector('.citation-preview-source')?.textContent).toBe('demo.pdf')
    expect(card?.textContent).not.toContain('undefined')
    expect(card?.textContent).not.toContain('null')
  })
})

describe('引用卡片效力状态徽标（R2-2 前端消费）', () => {
  it('in_force 引用在来源列表与预览卡片展示"现行有效"徽标', async () => {
    const wrapper = mountTrackedWith({
      chunk_id: 'c1',
      snippet: '现行法条',
      source: 'law.pdf',
      effective_status: 'in_force',
    })

    // 来源 chip 上即有徽标
    expect(wrapper.find('.source-status.is-ok').text()).toBe('现行有效')

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    const card = cardInBody()
    expect(card?.querySelector('.citation-preview-status.is-ok')?.textContent).toBe('现行有效')
    expect(card?.querySelector('.citation-preview-replaced')).toBeNull()
  })

  it('已失效条文展示 danger 徽标与替代法提示', async () => {
    const wrapper = mountTrackedWith({
      chunk_id: 'c2',
      snippet: '已废止条文',
      source: 'old-law.pdf',
      effective_status: 'repealed',
      replaced_by: '民法典',
    })

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    const card = cardInBody()
    expect(card?.querySelector('.citation-preview-status.is-danger')?.textContent).toBe('已废止')
    expect(card?.querySelector('.citation-preview-replaced')?.textContent).toContain('民法典')
  })

  it('未知/缺失效力状态不渲染徽标，也不出现 undefined', async () => {
    const wrapper = mountTrackedWith({
      chunk_id: 'c3',
      snippet: '存量数据',
      source: 'demo.pdf',
      effective_status: null,
    })

    await wrapper.find('.source-item').trigger('mouseenter')
    await nextTick()

    expect(wrapper.find('.source-status').exists()).toBeFalsy()
    const card = cardInBody()
    expect(card?.querySelector('.citation-preview-status')).toBeNull()
    expect(card?.textContent).not.toContain('undefined')
  })
})
