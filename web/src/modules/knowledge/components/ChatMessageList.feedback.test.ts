// @vitest-environment jsdom
// P2-1 满意度反馈组件行为：默认不渲染 / flag-on 渲染并可上报 / 静默失败回滚 /
// 挂载探活自动发现（GET /feedback/status 200→渲染、404→不渲染）。
//
// ⚠ 顺序约定：probeFeedbackEnabled 的模块级 memo 只缓存"成功"——探活 200 的
// 用例必须放在本文件最后，避免污染后续用例的探活结果。
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChatMessageList from './ChatMessageList.vue'

const assistantMsg = { id: 'a1', role: 'assistant' as const, content: '回答内容' }

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChatMessageList 反馈按钮（P2-1）', () => {
  it('探活 404（后端 flag-off）不渲染 👍/👎（fail-open 与基线等价）', async () => {
    const fetchMock = mockFetch(404, { detail: 'Not Found' })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(ChatMessageList, {
      props: { messages: [assistantMsg], sessionId: 's1' },
    })
    expect(wrapper.findAll('.feedback-btn')).toHaveLength(0)

    // 探活已发出且为 GET /feedback/status
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('/api/v1/feedback/status')

    // 探活完成后仍未渲染（404 → fail-open false）
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('.feedback-btn')).toHaveLength(0)
  })

  it('feedbackEnabled=true 跳过探活，点击 up 上报成功并锁定一票', async () => {
    const fetchMock = mockFetch(200, { status: 'queued', event_id: 'e1' })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(ChatMessageList, {
      props: { messages: [assistantMsg], feedbackEnabled: true, sessionId: 's1' },
    })
    const buttons = wrapper.findAll('.feedback-btn')
    expect(buttons).toHaveLength(2)

    await buttons[0].trigger('click')
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/feedback')
    expect(url).not.toContain('/status')
    const payload = JSON.parse(String(init.body))
    expect(payload).toMatchObject({ session_id: 's1', message_id: 'a1', rating: 'up' })
    // 已反馈：按钮禁用（一票一消息）
    expect(wrapper.findAll('.feedback-btn')[0].attributes('disabled')).toBeDefined()
  })

  it('后端 404（flag-off）静默失败：不弹错、状态回滚可重试', async () => {
    const fetchMock = mockFetch(404, { detail: 'Not Found' })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(ChatMessageList, {
      props: { messages: [assistantMsg], feedbackEnabled: true, sessionId: 's1' },
    })
    await wrapper.findAll('.feedback-btn')[1].trigger('click')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())

    // 状态已回滚：按钮不再禁用（用户可稍后重试，交互无中断感）
    await vi.waitFor(() => {
      expect(wrapper.findAll('.feedback-btn')[1].attributes('disabled')).toBeUndefined()
    })
  })

  it('无 sessionId 时不渲染按钮（缺归属上下文即无从上报）', () => {
    const wrapper = mount(ChatMessageList, {
      props: { messages: [assistantMsg], feedbackEnabled: true },
    })
    expect(wrapper.findAll('.feedback-btn')).toHaveLength(0)
  })

  it('探活 200（后端 flag-on）自动发现：prop 未传也渲染 👍/👎', async () => {
    const fetchMock = mockFetch(200, { enabled: true })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(ChatMessageList, {
      props: { messages: [assistantMsg], sessionId: 's1' },
    })
    expect(wrapper.findAll('.feedback-btn')).toHaveLength(0)

    await vi.waitFor(() => {
      expect(wrapper.findAll('.feedback-btn')).toHaveLength(2)
    })
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('/api/v1/feedback/status')
  })
})
