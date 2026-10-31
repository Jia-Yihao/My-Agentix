// T14 虚拟滚动窗口推导（纯函数，无需浏览器布局）。
//
// 覆盖面与验收口径：
// - 总数小于窗口容量 ⇒ 全渲染、两个占位为 0；
// - 滚到中部 ⇒ start/end 与 overscan 的折算正确，占位高度 = 窗口外条数 × 估算行高；
// - 滚到底 / scrollTop 超出内容高度 ⇒ end 钳到 total、底部占位归 0；
// - 非法参数（total=0/负数/NaN、行高 0、scrollTop 为负、viewport 非数）⇒ 不抛异常；
// - viewportHeight=0 的退化契约（G7）⇒ 分两支：overscan>0 时窗口退化为仅 first±overscan 那几条（**非空**）；
//   overscan=0（双 0）时窗口区间为空（零条渲染），但占位不归零，与 EMPTY_WINDOW 字面不同。
//
// 刻意不在这里断言滚动平滑度：可变高度是固定估算，平滑性需要真实布局，happy-dom 下无法验证。
import { describe, expect, it } from 'vitest'

import { computeVirtualWindow } from './useVirtualWindow'

describe('computeVirtualWindow（T14 窗口推导）', () => {
  it('总数小于窗口容量 ⇒ 全渲染、两个占位为 0', () => {
    const win = computeVirtualWindow({
      total: 3,
      scrollTop: 0,
      viewportHeight: 600,
      rowHeight: 100,
      overscan: 2,
    })
    expect(win).toEqual({ start: 0, end: 3, topPad: 0, bottomPad: 0 })
  })

  it('滚到中部 ⇒ start 含 overscan、end 含 overscan、占位按估算行高折算', () => {
    const win = computeVirtualWindow({
      total: 1000,
      scrollTop: 3050, // 估算行高 100 ⇒ 首个可见行 = 30
      viewportHeight: 500, // ⇒ 可见 5 行
      rowHeight: 100,
      overscan: 2,
    })
    expect(win.start).toBe(28) // 30 - 2
    expect(win.end).toBe(37) // 30 + 5 + 2
    expect(win.topPad).toBe(28 * 100)
    expect(win.bottomPad).toBe((1000 - 37) * 100)
  })

  it('overscan=0 ⇒ 窗口恰好覆盖可见行', () => {
    const win = computeVirtualWindow({
      total: 1000,
      scrollTop: 3050,
      viewportHeight: 500,
      rowHeight: 100,
      overscan: 0,
    })
    expect(win.start).toBe(30)
    expect(win.end).toBe(35)
  })

  it('滚到底 ⇒ end 钳到 total、底部占位归 0', () => {
    const win = computeVirtualWindow({
      total: 100,
      scrollTop: 9800, // 远超内容高度：首行下标钳到 total-1
      viewportHeight: 300,
      rowHeight: 100,
      overscan: 2,
    })
    expect(win.end).toBe(100)
    expect(win.bottomPad).toBe(0)
    expect(win.start).toBe(96) // 98 - 2
  })

  it('total=0 ⇒ 返回空窗口且不抛异常', () => {
    expect(() =>
      computeVirtualWindow({ total: 0, scrollTop: 0, viewportHeight: 600, rowHeight: 72 }),
    ).not.toThrow()
    expect(
      computeVirtualWindow({ total: 0, scrollTop: 0, viewportHeight: 600, rowHeight: 72 }),
    ).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0 })
  })

  it('非法参数不抛异常：负数/NaN 总数、行高 0、负 scrollTop、非数视口', () => {
    for (const total of [-5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(
        computeVirtualWindow({ total, scrollTop: 0, viewportHeight: 600, rowHeight: 72 }),
      ).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0 })
    }

    // 负 scrollTop 夹到 0：不能出现 start > end 的反向窗口
    const negativeTop = computeVirtualWindow({
      total: 10,
      scrollTop: -500,
      viewportHeight: 300,
      rowHeight: 100,
      overscan: 1,
    })
    expect(negativeTop).toEqual({ start: 0, end: 4, topPad: 0, bottomPad: 600 })

    // 行高 0 ⇒ 回落缺省估算行高，不产生 Infinity/NaN
    const badRow = computeVirtualWindow({
      total: 100,
      scrollTop: 0,
      viewportHeight: 144, // 缺省估算行高 72 ⇒ 可见 2 行
      rowHeight: 0,
      overscan: 0,
    })
    expect(badRow.start).toBe(0)
    expect(badRow.end).toBe(2)
    expect(Number.isFinite(badRow.topPad)).toBe(true)
    expect(Number.isFinite(badRow.bottomPad)).toBe(true)

    // 视口非数 ⇒ 按 0 处理（只剩 overscan 条，但不抛异常、不产生 NaN）
    const badViewport = computeVirtualWindow({
      total: 50,
      scrollTop: 0,
      viewportHeight: Number.NaN,
      rowHeight: 100,
      overscan: 2,
    })
    expect(badViewport).toEqual({ start: 0, end: 2, topPad: 0, bottomPad: 48 * 100 })
  })

  it('viewportHeight=0 的两支退化：overscan>0 ⇒ 仅 overscan 条，双 0 ⇒ 区间为空但占位不归零（G7 退化契约）', () => {
    // G7（按简报实测改判）：台账 G7 行括注"当前实现返回空窗口"不确——但「不是空窗口」只对 **overscan>0** 支成立，
    // 整条契约要分两支说：viewportHeight sanitize 到 0 只让 visibleRows=0；overscan>0 时窗口仍按 first ± overscan
    // 取值 ⇒ 退化为仅 overscan 那几条，**不是空窗口**；overscan=0（双 0）时窗口区间确实为空（零条渲染），
    // 只是占位不按空窗口归零，与 total<=0 早退的 EMPTY_WINDOW 字面不同。
    // 上一条用例钉的是"非数视口"（NaN），本条单独钉显式 0 这一 sanitize 来源；
    // 既有「overscan=0 ⇒ 窗口恰好覆盖可见行」（`:41`）的视口是 500，双 0 组合此前未钉，由本条下方追加的 expect 补上。
    // 参数与上一条同形（total 50 / rowHeight 100 / overscan 2），断言测到的精确值。
    const win = computeVirtualWindow({
      total: 50,
      scrollTop: 0,
      viewportHeight: 0,
      rowHeight: 100,
      overscan: 2,
    })
    expect(win).toEqual({ start: 0, end: 2, topPad: 0, bottomPad: 48 * 100 })

    // 双 0 退化形态（viewportHeight=0 且 overscan=0）：窗口区间为空（零条渲染），
    // 但占位不按空窗口归零，与 total<=0 早退的 EMPTY_WINDOW（四字段全 0）字面不同。
    const zeroZero = computeVirtualWindow({
      total: 50,
      scrollTop: 0,
      viewportHeight: 0,
      rowHeight: 100,
      overscan: 0,
    })
    // （期望值按实测落笔：bottomPad 走 (total - end) × rowHeight = 50 × 100，不归零。）
    expect(zeroZero).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 5000 })
  })
})
