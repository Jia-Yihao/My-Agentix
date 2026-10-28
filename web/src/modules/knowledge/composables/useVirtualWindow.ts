/**
 * T14：消息列表虚拟滚动的窗口推导（纯函数）。
 *
 * 为什么抽成纯函数：窗口推导是本机制里唯一有分支的逻辑，抽出来才能被 vitest 直接钉住
 * ——happy-dom / jsdom 没有布局，量不出真实 `scrollHeight` / `clientHeight`，
 * 把推导留在组件里就只能靠"看起来对不对"来验收。
 *
 * 为什么按「估算行高」做窗口、滚动过程中不做逐条实测修正：
 * 消息体是可变高度（思考区 / 工具卡片 / 引用来源 / 证据展开 / 执行轨迹），精确测量需要
 * 真实布局并做滚动过程中的位置修正（measure + offset 补偿）。本轮刻意只做**固定估算 +
 * overscan 补偿**这一最小实现，理由是可变高度精确测量需要真实布局，happy-dom 下无法验证。
 * **因此本实现不承诺滚动平滑度，也不承诺占位高度与实际内容高度吻合**——估算偏差表现为
 * 滚动条长度与真实内容长度的差，属已知限制而非缺陷。
 */

/** 单条消息的估算行高（px）。可变高度下这是个折中值，见文件头注。 */
export const DEFAULT_ROW_HEIGHT = 72

/** 窗口上下各多渲染几条：抵消估算行高与真实高度的偏差，减少滚动时的空白闪烁。 */
export const DEFAULT_OVERSCAN = 4

/**
 * 量不到可视高度时的回落值（px）。
 * 两个真实场景都会量到 0：① 首帧布局尚未完成；② 纯 DOM 测试环境（无布局）。
 * 若此时按 0 计算，窗口只剩 overscan 条，表现为"列表几乎空白"——比多渲染几条糟得多。
 */
export const FALLBACK_VIEWPORT_HEIGHT = 600

export interface VirtualWindowParams {
  /** 数据源总条数（含滚动窗口外的消息） */
  total: number
  /** 滚动源当前 scrollTop（px） */
  scrollTop: number
  /** 滚动源可视高度（px） */
  viewportHeight: number
  /** 单条消息估算行高（px） */
  rowHeight: number
  /** 窗口上下额外渲染条数，缺省取 DEFAULT_OVERSCAN */
  overscan?: number
}

export interface VirtualWindow {
  /** 窗口起始下标（含） */
  start: number
  /** 窗口结束下标（不含） */
  end: number
  /** 窗口上方占位高度（px）= 窗口外上方条数 × 估算行高 */
  topPad: number
  /** 窗口下方占位高度（px）= 窗口外下方条数 × 估算行高 */
  bottomPad: number
}

const EMPTY_WINDOW: VirtualWindow = { start: 0, end: 0, topPad: 0, bottomPad: 0 }

/** 行高非法（0 / 负数 / NaN / Infinity）会让除法得出 Infinity 或反向窗口，一律回落缺省值。 */
function sanitizeRowHeight(rowHeight: number): number {
  return Number.isFinite(rowHeight) && rowHeight > 0 ? rowHeight : DEFAULT_ROW_HEIGHT
}

/** overscan 非法（NaN / 负数 / 小数）按 0 处理：少渲染几条总好过渲染负数条。 */
function sanitizeOverscan(overscan: number | undefined): number {
  if (!Number.isFinite(overscan)) return DEFAULT_OVERSCAN
  return Math.max(0, Math.floor(overscan as number))
}

/**
 * 推导虚拟滚动窗口。
 *
 * 契约（测试逐条钉住）：
 * - `total <= 0` / 非数 ⇒ 空窗口 `{ start: 0, end: 0, topPad: 0, bottomPad: 0 }`，不抛异常；
 * - 总数小于窗口容量 ⇒ `start = 0`、`end = total`、两个占位均为 0（全渲染）；
 * - 滚到中部 ⇒ `start = 首个可见行 - overscan`、`end = 首个可见行 + 可见行数 + overscan`；
 * - 滚到底（或 scrollTop 超出内容高度）⇒ `end` 钳到 `total`，`bottomPad` 归 0。
 */
export function computeVirtualWindow(params: VirtualWindowParams): VirtualWindow {
  const total = Math.floor(Number(params.total))
  if (!Number.isFinite(total) || total <= 0) return { ...EMPTY_WINDOW }

  const rowHeight = sanitizeRowHeight(params.rowHeight)
  const overscan = sanitizeOverscan(params.overscan)
  const scrollTop =
    Number.isFinite(params.scrollTop) && params.scrollTop > 0 ? params.scrollTop : 0
  const viewportHeight =
    Number.isFinite(params.viewportHeight) && params.viewportHeight > 0 ? params.viewportHeight : 0

  // 首行下标钳在 [0, total-1]：scrollTop 超出内容高度（容器尚未夹取时会出现）也要给出
  // 一个非空窗口，否则用户会看到一片空白。
  const first = Math.min(Math.floor(scrollTop / rowHeight), total - 1)
  const visibleRows = Math.ceil(viewportHeight / rowHeight)

  const start = Math.max(0, first - overscan)
  const end = Math.min(total, first + visibleRows + overscan)

  return {
    start,
    end,
    topPad: start * rowHeight,
    bottomPad: (total - end) * rowHeight,
  }
}
