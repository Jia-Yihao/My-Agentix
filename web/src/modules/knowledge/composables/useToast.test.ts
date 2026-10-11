import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearToasts, dismissToast, toast, useToast } from './useToast'

describe('useToast（W6 B10 零依赖 Toast 单例）', () => {
  beforeEach(() => {
    clearToasts()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    clearToasts()
  })

  it('toast 入队并返回 id（默认 info / 3s 自动消失）', () => {
    const { items } = useToast()
    const id = toast('操作成功')
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ id, message: '操作成功', type: 'info' })
    vi.advanceTimersByTime(3000)
    expect(items).toHaveLength(0)
  })

  it('type 透传（error 等）', () => {
    const { items } = useToast()
    toast('删除失败', { type: 'error' })
    expect(items[0].type).toBe('error')
  })

  it('duration 可注入；0 = 不自动消失', () => {
    const { items } = useToast()
    toast('常驻', { duration: 0 })
    vi.advanceTimersByTime(60000)
    expect(items).toHaveLength(1)
  })

  it('手动 dismiss 清除条目与计时器', () => {
    const { items } = useToast()
    const id = toast('稍纵即逝')
    dismissToast(id)
    expect(items).toHaveLength(0)
    vi.advanceTimersByTime(3000)
    expect(items).toHaveLength(0)
  })

  it('多条共存且各自独立消失', () => {
    const { items } = useToast()
    toast('一', { duration: 1000 })
    toast('二', { duration: 5000 })
    vi.advanceTimersByTime(1000)
    expect(items.map((t) => t.message)).toEqual(['二'])
    vi.advanceTimersByTime(4000)
    expect(items).toHaveLength(0)
  })

  it('clearToasts 一次清空全部', () => {
    const { items } = useToast()
    toast('a')
    toast('b')
    clearToasts()
    expect(items).toHaveLength(0)
  })
})
