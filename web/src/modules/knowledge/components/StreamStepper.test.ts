import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { StreamProgress } from '../api/chatStreamHandlers'
import StreamStepper from './StreamStepper.vue'

function progress(stage: string, extra: Partial<StreamProgress> = {}): StreamProgress {
  return { stage, message: '', ...extra }
}

describe('StreamStepper（W6 B4 三态步骤器）', () => {
  it('渲染三态步骤（准备会话 → 检索知识库 → 生成回答）', () => {
    const wrapper = mount(StreamStepper, { props: { progress: progress('retrieval') } })
    const labels = wrapper.findAll('.step-label').map((n) => n.text())
    expect(labels).toEqual(['准备会话', '检索知识库', '生成回答'])
  })

  it('stage 推进：之前步骤 done、当前 active、之后 pending', () => {
    const wrapper = mount(StreamStepper, { props: { progress: progress('llm_generate') } })
    const states = wrapper.findAll('.stepper-step').map((n) => n.classes())
    expect(states[0]).toContain('is-done')
    expect(states[1]).toContain('is-done')
    expect(states[2]).toContain('is-active')
  })

  it('未知 stage 全部 pending，不崩溃', () => {
    const wrapper = mount(StreamStepper, { props: { progress: progress('weird_stage') } })
    expect(wrapper.findAll('.stepper-step').every((n) => n.classes().includes('is-pending'))).toBe(true)
  })

  it('progress.message 显示当前文案', () => {
    const wrapper = mount(StreamStepper, {
      props: { progress: progress('retrieval', { message: '检索知识库…' }) },
    })
    expect(wrapper.find('.stepper-current').text()).toBe('检索知识库…')
  })

  it('进度条：step/total 百分比', () => {
    const wrapper = mount(StreamStepper, {
      props: { progress: progress('retrieval', { step: 2, total: 4 }) },
    })
    expect(wrapper.find('.stepper-fill').attributes('style')).toContain('width: 50%')
  })

  it('检索四阶段子步骤 + ms 徽标（非紧凑）', () => {
    const wrapper = mount(StreamStepper, {
      props: {
        progress: progress('retrieval', {
          stages: [
            { stage: 'rewrite', ms: 12.4 },
            { stage: 'sparse', ms: 45 },
            { stage: 'dense', ms: 1200 },
            { stage: 'rerank', skipped: 1 },
          ],
        }),
      },
    })
    const subs = wrapper.findAll('.substep').map((n) => n.text())
    expect(subs).toHaveLength(4)
    expect(subs[0]).toContain('查询重写')
    expect(subs[1]).toContain('稀疏检索')
    expect(subs[3]).toContain('重排序')
    expect(subs[0]).toContain('12ms')
    expect(subs[1]).toContain('45ms')
    expect(subs[2]).toContain('1.20s')
  })

  it('compact 生成态隐藏子步骤（正文与 stepper 并存不喧宾）', () => {
    const wrapper = mount(StreamStepper, {
      props: {
        compact: true,
        progress: progress('llm_generate', {
          message: '生成回答…',
          stages: [{ stage: 'rewrite', ms: 10 }],
        }),
      },
    })
    expect(wrapper.find('.stream-stepper').classes()).toContain('is-compact')
    expect(wrapper.find('.stepper-substeps').exists()).toBe(false)
    expect(wrapper.find('.stepper-current').exists()).toBe(true)
  })

  it('未知子 stage 原样回退显示', () => {
    const wrapper = mount(StreamStepper, {
      props: { progress: progress('retrieval', { stages: [{ stage: 'custom_x' }] }) },
    })
    expect(wrapper.find('.substep').text()).toContain('custom_x')
  })
})
