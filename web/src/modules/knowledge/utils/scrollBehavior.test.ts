import { describe, expect, it } from 'vitest'

import {
  STICK_BOTTOM_THRESHOLD_PX,
  bottomGap,
  shouldStickToBottom,
} from './scrollBehavior'

describe('scrollBehavior（W6 A2 智能吸底纯函数）', () => {
  it('bottomGap = scrollHeight - scrollTop - clientHeight', () => {
    expect(bottomGap({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 })).toBe(600)
    expect(bottomGap({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })).toBe(0)
  })

  it('bottomGap 对负值（内容少于视口/回弹超界）归 0', () => {
    expect(bottomGap({ scrollTop: 500, scrollHeight: 800, clientHeight: 400 })).toBe(0)
    expect(bottomGap({ scrollTop: 2, scrollHeight: 400, clientHeight: 400 })).toBe(0)
    expect(bottomGap({ scrollTop: 400, scrollHeight: 400, clientHeight: 400 })).toBe(0)
  })

  it('默认阈值 80px 内算贴底（≤80 跟随，81 起不打扰）', () => {
    expect(STICK_BOTTOM_THRESHOLD_PX).toBe(80)
    expect(shouldStickToBottom({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })).toBe(true)
    expect(shouldStickToBottom({ scrollTop: 521, scrollHeight: 1000, clientHeight: 400 })).toBe(true)
    expect(shouldStickToBottom({ scrollTop: 520, scrollHeight: 1000, clientHeight: 400 })).toBe(true)
    expect(shouldStickToBottom({ scrollTop: 519, scrollHeight: 1000, clientHeight: 400 })).toBe(false)
  })

  it('上翻阅读（缺口 > 阈值）不贴底 → 流式不打扰', () => {
    expect(shouldStickToBottom({ scrollTop: 0, scrollHeight: 5000, clientHeight: 500 })).toBe(false)
  })

  it('空内容（gap 0）视为贴底', () => {
    expect(shouldStickToBottom({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 })).toBe(true)
  })

  it('自定义阈值：0 只在完全贴底时跟随', () => {
    const s = { scrollTop: 599, scrollHeight: 1000, clientHeight: 400 }
    expect(shouldStickToBottom(s, 0)).toBe(false)
    expect(shouldStickToBottom({ ...s, scrollTop: 600 }, 0)).toBe(true)
  })

  it('负阈值钳为 0（等同完全贴底）', () => {
    expect(shouldStickToBottom({ scrollTop: 599, scrollHeight: 1000, clientHeight: 400 }, -10)).toBe(false)
  })
})
