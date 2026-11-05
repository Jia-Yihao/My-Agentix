/**
 * 智能吸底纯函数。
 *
 * 用户滚到底部附近（缺口 ≤ 阈值）视为「贴底」——流式新内容才继续自动
 * 下滚；用户上翻阅读时不打扰（配合「↓ 回到底部」胶囊手动回底）。
 */

/** 贴底阈值（px）：距底部 80px 内算贴底 */
export const STICK_BOTTOM_THRESHOLD_PX = 80

export type BottomScrollState = {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/**
 * 距底部缺口（px）：`scrollHeight - scrollTop - clientHeight`（下限 0）。
 * 负值（容器内容少于视口/回弹超界）归 0。
 */
export function bottomGap(state: BottomScrollState): number {
  const gap = state.scrollHeight - state.scrollTop - state.clientHeight
  return gap > 0 ? gap : 0
}

/** 是否贴底（缺口 ≤ 阈值）。空内容（gap 0）视为贴底。 */
export function shouldStickToBottom(
  state: BottomScrollState,
  thresholdPx: number = STICK_BOTTOM_THRESHOLD_PX,
): boolean {
  return bottomGap(state) <= Math.max(0, thresholdPx)
}
