// @vitest-environment jsdom
// XSS 断言依赖 DOMPurify（需真实 DOM 语义，见 markdown.test.ts 头注）。
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ChatMessageList from './ChatMessageList.vue'

describe('ChatMessageList', () => {
  it('renders user and assistant messages', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        variant: 'page',
        messages: [
          { id: 'u1', role: 'user', content: '你好' },
          { id: 'a1', role: 'assistant', content: '您好，有什么可以帮您？' },
        ],
      },
    })

    expect(wrapper.text()).toContain('你好')
    expect(wrapper.text()).toContain('您好，有什么可以帮您？')
  })

  it('shows citation sources for assistant messages', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '根据文档…',
            citations: [
              {
                chunk_id: 'c1',
                snippet: '引用片段',
                source: 'demo.pdf',
                page_no: 1,
              },
            ],
          },
        ],
      },
    })

    expect(wrapper.text()).toContain('引用来源')
    expect(wrapper.text()).toContain('demo.pdf')
    expect(wrapper.text()).toContain('引用片段')
  })

  it('召回溯源：引用卡渲染 [S1] 标号，被答案引用的卡片加高亮类', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '根据文档…见 [S1]',
            citations: [
              { chunk_id: 'c1', snippet: '被引用片段', source: 'a.pdf', ref: 'S1', cited: true },
              { chunk_id: 'c2', snippet: '未引用片段', source: 'b.pdf', ref: 'S2', cited: false },
            ],
          },
        ],
      },
    })

    const items = wrapper.findAll('.source-item')
    expect(items).toHaveLength(2)
    expect(items[0].find('.source-ref').text()).toBe('[S1]')
    expect(items[1].find('.source-ref').text()).toBe('[S2]')
    expect(items[0].classes()).toContain('is-cited')
    expect(items[1].classes()).not.toContain('is-cited')
  })

  it('召回溯源：未开启溯源时不渲染标号（老数据兼容）', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '根据文档…',
            citations: [{ chunk_id: 'c1', snippet: '片段', source: 'a.pdf' }],
          },
        ],
      },
    })

    expect(wrapper.find('.source-ref').exists()).toBe(false)
    expect(wrapper.find('.source-item').classes()).not.toContain('is-cited')
  })

  it('pending 状态下进度条优先于占位渲染（审查批次 2026-09-28）', () => {
    // progress 帧只在正文出现前发出；原实现占位 v-if 抢占 → fast 模式下
    // 进度条（含阶段文案/百分比）永远不可达。
    // W6 B4：进度 UI 换为 StreamStepper 三态步骤器（progress 不再被首 token 清除）
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '',
            progress: { stage: 'retrieval', message: '检索知识库', step: 2, total: 3 },
          },
        ],
        pendingAssistantId: 'a1',
      },
    })

    expect(wrapper.find('.stream-stepper').exists()).toBe(true)
    expect(wrapper.text()).toContain('检索知识库')
    expect(wrapper.find('.stepper-step.is-active').exists()).toBe(true)
    // 占位（三点动画）不得与步骤器同时出现
    expect(wrapper.find('.chat-thinking-placeholder, .thinking-placeholder').exists()).toBe(false)
  })

  it('无进度时 pending 仍渲染占位', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [{ id: 'a1', role: 'assistant', content: '' }],
        pendingAssistantId: 'a1',
      },
    })
    expect(wrapper.find('.stream-stepper').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('检索知识库')
  })

  it('批次 C1：>200 条折叠中段（首 50 + 尾 150），点击展开全部', async () => {
    const messages = Array.from({ length: 250 }, (_, i) => ({
      id: `m${i}`,
      role: 'user' as const,
      content: `消息${i}`,
    }))
    const wrapper = mount(ChatMessageList, { props: { messages } })

    // 折叠态：首 50 + 尾 150 = 200 个消息节点（中段 50..99 被折叠）
    expect(wrapper.findAll('.modal-message')).toHaveLength(200)
    const bar = wrapper.find('.message-fold-bar')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('50')
    expect(wrapper.text()).toContain('消息0')
    expect(wrapper.text()).toContain('消息249')
    expect(wrapper.text()).not.toContain('消息60')

    await bar.trigger('click')
    expect(wrapper.findAll('.modal-message')).toHaveLength(250)
    expect(wrapper.find('.message-fold-bar').exists()).toBe(false)
    expect(wrapper.text()).toContain('消息60')
  })

  it('批次 C1：≤200 条不折叠', () => {
    const messages = Array.from({ length: 200 }, (_, i) => ({
      id: `m${i}`,
      role: 'user' as const,
      content: `消息${i}`,
    }))
    const wrapper = mount(ChatMessageList, { props: { messages } })

    expect(wrapper.findAll('.modal-message')).toHaveLength(200)
    expect(wrapper.find('.message-fold-bar').exists()).toBe(false)
  })

  // ---------------------------------------------------------------------------
  // W6 B5：工具活动卡（tool_running 独立成卡，不污染思考区）
  // ---------------------------------------------------------------------------

  it('工具卡：活动态显示 spinner + 冻结文案', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '',
            toolActivity: { label: '正在检索法条' },
          },
        ],
      },
    })
    const card = wrapper.find('.tool-card')
    expect(card.exists()).toBe(true)
    expect(wrapper.find('.tool-running').exists()).toBe(true)
    expect(card.text()).toContain('正在检索法条')
  })

  it('工具卡：历史折叠为 details（计数 + 条目）', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '完成',
            toolHistory: ['检索法条', '查案例'],
          },
        ],
      },
    })
    const hist = wrapper.find('.tool-history')
    expect(hist.exists()).toBe(true)
    expect(hist.text()).toContain('工具活动（2）')
    expect(hist.text()).toContain('检索法条')
    expect(hist.text()).toContain('查案例')
  })

  // ---------------------------------------------------------------------------
  // W6 B8：耗时芯片（终帧 duration_ms/ttft_ms）
  // ---------------------------------------------------------------------------

  it('耗时芯片渲染耗时/首字（秒与毫秒格式）', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '回答',
            timing: { durationMs: 3210, ttftMs: 480 },
          },
        ],
      },
    })
    const chip = wrapper.find('.timing-chip')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toContain('耗时 3.21s')
    expect(chip.text()).toContain('首字 480ms')
  })

  it('无 timing 不渲染芯片', () => {
    const wrapper = mount(ChatMessageList, {
      props: { messages: [{ id: 'a1', role: 'assistant', content: '回答' }] },
    })
    expect(wrapper.find('.timing-chip').exists()).toBe(false)
  })

  // ---------------------------------------------------------------------------
  // W6 A3/B6：markdown 富文本 + 流式光标（绝不裸 v-html）
  // ---------------------------------------------------------------------------

  it('助手消息 markdown 渲染（粗体→strong），用户消息保持纯文本', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          { id: 'u1', role: 'user', content: '**不应加粗**' },
          { id: 'a1', role: 'assistant', content: '**加粗文本**' },
        ],
      },
    })
    const md = wrapper.find('.modal-message-text--md')
    expect(md.exists()).toBe(true)
    expect(md.html()).toContain('<strong>加粗文本</strong>')
    const userText = wrapper.findAll('.modal-message-text:not(.modal-message-text--md)')
    expect(userText[0].text()).toBe('**不应加粗**')
  })

  it('XSS：script 标签不进入助手 DOM', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [{ id: 'a1', role: 'assistant', content: '安全<script>alert(1)</script>文本' }],
      },
    })
    expect(wrapper.html()).not.toContain('<script>')
    expect(wrapper.text()).toContain('安全')
  })

  it('流式中（pending）有内容时显示光标', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [{ id: 'a1', role: 'assistant', content: '生成到一半' }],
        pendingAssistantId: 'a1',
      },
    })
    expect(wrapper.find('.stream-caret').exists()).toBe(true)
  })

  it('非流式助手消息无光标', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [{ id: 'a1', role: 'assistant', content: '已完成' }],
        pendingAssistantId: 'other',
      },
    })
    expect(wrapper.find('.stream-caret').exists()).toBe(false)
  })

  // ---------------------------------------------------------------------------
  // W6 B11：复制 / 重新生成
  // ---------------------------------------------------------------------------

  it('复制按钮：clipboard 可用时写剪贴板并 toast', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    const wrapper = mount(ChatMessageList, {
      props: { messages: [{ id: 'a1', role: 'assistant', content: '可复制内容' }] },
    })
    const btns = wrapper.findAll('.msg-action-btn')
    expect(btns[0].text()).toBe('复制')
    await btns[0].trigger('click')
    expect(writeText).toHaveBeenCalledWith('可复制内容')
  })

  it('重新生成按钮：仅末条助手 + 非流式 + 有内容时出现', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          { id: 'u1', role: 'user', content: '问一' },
          { id: 'a1', role: 'assistant', content: '答一' },
          { id: 'u2', role: 'user', content: '问二' },
          { id: 'a2', role: 'assistant', content: '答二' },
        ],
      },
    })
    const regen = wrapper.findAll('.msg-action-btn').filter((b) => b.text() === '重新生成')
    expect(regen).toHaveLength(1)
    expect(wrapper.findAll('.modal-message')[3].text()).toContain('答二')
  })

  it('重新生成按钮：流式期间不显示（停止后可点）', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          { id: 'u1', role: 'user', content: '问' },
          { id: 'a1', role: 'assistant', content: '部分' },
        ],
        pendingAssistantId: 'a1',
      },
    })
    expect(
      wrapper.findAll('.msg-action-btn').filter((b) => b.text() === '重新生成'),
    ).toHaveLength(0)
  })

  it('点击重新生成 emit regenerate', async () => {
    const wrapper = mount(ChatMessageList, {
      props: { messages: [{ id: 'a1', role: 'assistant', content: '回答' }] },
    })
    const regen = wrapper.findAll('.msg-action-btn').find((b) => b.text() === '重新生成')!
    await regen.trigger('click')
    expect(wrapper.emitted('regenerate')).toHaveLength(1)
  })

  // ---------------------------------------------------------------------------
  // W6 B9：执行轨迹受控展开（流式展开 + 最新高亮）
  // ---------------------------------------------------------------------------

  it('流式中轨迹 details 展开，末步 is-latest 高亮', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '生成中',
            trace: [
              { kind: 'stage', label: '检索', at: 1 },
              { kind: 'tool', label: '工具A', at: 2 },
            ],
          },
        ],
        pendingAssistantId: 'a1',
      },
    })
    const panel = wrapper.find('details.trace-panel')
    expect(panel.exists()).toBe(true)
    expect(panel.attributes('open')).toBeDefined()
    const steps = wrapper.findAll('.trace-step')
    expect(steps).toHaveLength(2)
    expect(steps[1].classes()).toContain('is-latest')
    expect(steps[0].classes()).not.toContain('is-latest')
  })

  it('流式结束后轨迹释放受控（无 open 属性）', () => {
    const wrapper = mount(ChatMessageList, {
      props: {
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: '完成',
            trace: [{ kind: 'done', label: '完成', at: 3 }],
          },
        ],
        pendingAssistantId: null,
      },
    })
    expect(wrapper.find('details.trace-panel').attributes('open')).toBeUndefined()
  })
})
