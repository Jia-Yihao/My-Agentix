/**
 * R2-2 前端消费：法条效力状态 → 展示视图（徽标文案 + 语气色）。
 *
 * 后端契约（retrieval/domain/policies/legal_effectiveness.py）：
 *   in_force | expired | repealed | pending（缺失/未知 → 前端不渲染徽标）
 * 兼容接受中文别名（后端入库侧归一化前的历史数据）。
 */

export type EffectiveTone = 'ok' | 'warn' | 'danger'

export interface EffectiveStatusView {
  label: string
  tone: EffectiveTone
}

const VIEW_MAP: Record<string, EffectiveStatusView> = {
  in_force: { label: '现行有效', tone: 'ok' },
  有效: { label: '现行有效', tone: 'ok' },
  现行有效: { label: '现行有效', tone: 'ok' },
  expired: { label: '已失效', tone: 'danger' },
  已失效: { label: '已失效', tone: 'danger' },
  repealed: { label: '已废止', tone: 'danger' },
  已废止: { label: '已废止', tone: 'danger' },
  pending: { label: '未生效', tone: 'warn' },
  未生效: { label: '未生效', tone: 'warn' },
}

/** 状态 → 徽标视图；空值/未知值返回 null（调用方据此不渲染）。 */
export function effectiveStatusView(status?: string | null): EffectiveStatusView | null {
  if (!status) return null
  return VIEW_MAP[String(status).trim()] ?? null
}
