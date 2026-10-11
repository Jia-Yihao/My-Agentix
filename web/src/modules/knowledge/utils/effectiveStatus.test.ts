import { describe, expect, it } from 'vitest'

import { effectiveStatusView } from './effectiveStatus'

describe('法条效力状态视图（R2-2 前端消费）', () => {
  it('四规范状态映射为正确文案与语气', () => {
    expect(effectiveStatusView('in_force')).toEqual({ label: '现行有效', tone: 'ok' })
    expect(effectiveStatusView('expired')).toEqual({ label: '已失效', tone: 'danger' })
    expect(effectiveStatusView('repealed')).toEqual({ label: '已废止', tone: 'danger' })
    expect(effectiveStatusView('pending')).toEqual({ label: '未生效', tone: 'warn' })
  })

  it('兼容中文别名（后端入库侧历史数据）', () => {
    expect(effectiveStatusView('现行有效')?.label).toBe('现行有效')
    expect(effectiveStatusView('已废止')?.tone).toBe('danger')
    expect(effectiveStatusView('未生效')?.tone).toBe('warn')
  })

  it('空值与未知值返回 null（不渲染徽标）', () => {
    expect(effectiveStatusView(null)).toBeNull()
    expect(effectiveStatusView(undefined)).toBeNull()
    expect(effectiveStatusView('')).toBeNull()
    expect(effectiveStatusView('神秘状态')).toBeNull()
  })

  it('容忍首尾空白', () => {
    expect(effectiveStatusView(' in_force ')?.label).toBe('现行有效')
  })
})
