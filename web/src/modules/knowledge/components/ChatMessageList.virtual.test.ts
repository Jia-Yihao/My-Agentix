// T14 虚拟滚动组件行为（默认关）。
//
// 覆盖面与验收口径：
// - 默认（不传 virtual）/ 显式 virtual=false ⇒ 全量渲染 `.modal-message`，且没有任何占位节点
//   （= 与基线逐位等价）；
// - virtual=true 且消息数远大于窗口 ⇒ 只渲染窗口内消息，行数与两个占位高度都按窗口反推精确钉住，顶/底两个占位元素存在；
// - 虚拟模式下批次 C1 折叠中段机制仍以 displayMessages 为数据源，折叠条随窗口滚进滚出；
// - 虚拟态点击折叠条 ⇒ 展开（G2：此前折叠点击展开只覆盖关闭态）；
// - 虚拟态量到 clientHeight=0 ⇒ 视口回落 FALLBACK_VIEWPORT_HEIGHT 被精确钉住（G3：全用精确断言）；
// - 不传 virtual 与 virtual=false 的 html 逐字相等 + 关闭态结构无虚拟滚动痕迹（G6：替代已删除的仓外比对脚本）。
//
// 断言只走 DOM 与纯函数返回值，不依赖真实滚动时序：happy-dom 无布局，直接把 scrollTop
// 定义到目标值再触发 scroll 事件即可（组件在量到 clientHeight=0 时回落默认视口高度）。
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ChatMessageList from './ChatMessageList.vue'

type ChatModalMessageArg = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

function makeMessages(count: number): ChatModalMessageArg[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `m${i}`,
    role: 'user' as const,
    content: `消息${i}`,
  }))
}

function mountList(messages: ChatModalMessageArg[], virtual?: boolean) {
  return mount(ChatMessageList, {
    props: virtual === undefined ? { messages } : { messages, virtual },
  })
}

type ListWrapper = ReturnType<typeof mountList>

/** 把滚动位置摆到 scrollTop 像素并触发 scroll（happy-dom 无布局，故不靠真实滚动） */
async function scrollTo(wrapper: ListWrapper, scrollTop: number): Promise<void> {
  Object.defineProperty(wrapper.element, 'scrollTop', {
    value: scrollTop,
    configurable: true,
    writable: true,
  })
  await wrapper.trigger('scroll')
}

/** 读取占位元素的内联高度（px）；元素不存在则由 find 的 exists 断言兜住 */
function spacerHeightPx(wrapper: ListWrapper, which: 'top' | 'bottom'): number {
  const el = wrapper.find(`.virtual-spacer--${which}`)
  expect(el.exists()).toBe(true)
  return Number.parseFloat(el.attributes('style')!.replace('height:', ''))
}

describe('ChatMessageList 虚拟滚动（T14，默认关）', () => {
  it('不传 virtual ⇒ 全部消息照常渲染，且无占位元素（与基线一致）', () => {
    const wrapper = mountList(makeMessages(120))

    expect(wrapper.findAll('.modal-message')).toHaveLength(120)
    expect(wrapper.find('.virtual-spacer').exists()).toBe(false)
    expect(wrapper.text()).toContain('消息0')
    expect(wrapper.text()).toContain('消息119')
  })

  it('显式 virtual=false ⇒ 与不传等价（全量渲染、无占位）', () => {
    const wrapper = mountList(makeMessages(120), false)

    expect(wrapper.findAll('.modal-message')).toHaveLength(120)
    expect(wrapper.find('.virtual-spacer').exists()).toBe(false)
  })

  it('virtual=true ⇒ 只渲染窗口内消息，顶/底占位按窗口外条数撑高', async () => {
    const total = 150
    const wrapper = mountList(makeMessages(total), true)

    // 初始停在顶部：上方无占位，下方占位撑起剩余内容
    // 精确值反推（庚-2 G2-B）：视口=FALLBACK_VIEWPORT_HEIGHT(600)、DEFAULT_ROW_HEIGHT=72 ⇒
    // visibleRows=ceil(600/72)=9，加 DEFAULT_OVERSCAN=4 ⇒ 初始窗口 [0, 13)。
    // 原 :81/:82 两条不等式（>0 且 <total）合并为下面这一条 toBe(13)：13 蕴含 >0 且 <150，
    // 合并不丢判别力。
    const initialRows = wrapper.findAll('.modal-message').length
    expect(initialRows).toBe(13)
    expect(spacerHeightPx(wrapper, 'top')).toBe(0)
    // 底部占位＝窗口外条数×行高：(150-13)×72（原 :84 的 >0 换精确值）
    expect(spacerHeightPx(wrapper, 'bottom')).toBe((150 - 13) * 72)

    // 滚到中部（估算行高 72 ⇒ 第 50 条附近）：上下占位都非 0
    await scrollTo(wrapper, 50 * 72)

    // scrollTop=50×72 ⇒ first=50 ⇒ 窗口 [50-4, 50+9+4)=[46, 63)（visibleRows=ceil(600/72)=9）。
    // 原 :90/:91 两条不等式（>0 且 <total）合并为下面这一条 toBe(63-46)：17 蕴含 >0 且 <150，
    // 合并不丢判别力。
    const midRows = wrapper.findAll('.modal-message').length
    expect(midRows).toBe(63 - 46)
    // 顶部占位＝窗口前条数×行高：46×72（原 :92 的 >0 换精确值）
    expect(spacerHeightPx(wrapper, 'top')).toBe(46 * 72)
    // 底部占位＝窗口外条数×行高：(150-63)×72（原 :93 的 >0 换精确值）
    expect(spacerHeightPx(wrapper, 'bottom')).toBe((150 - 63) * 72)
    // 窗口内是第 50 条附近，首条消息已不在 DOM 里
    expect(wrapper.text()).toContain('消息50')
    expect(wrapper.text()).not.toContain('消息0')

    // 滚回顶部：上方占位归 0（占位随滚动重算，不是静态撑高）
    await scrollTo(wrapper, 0)
    expect(spacerHeightPx(wrapper, 'top')).toBe(0)
    expect(wrapper.text()).toContain('消息0')
  })

  it('virtual=true 时折叠中段仍生效，折叠条随窗口滚进滚出', async () => {
    // 250 条 ⇒ 批次 C1 折叠为「首 50 + 尾 150」= 200 条数据源（中段 50 条被折叠）
    const wrapper = mountList(makeMessages(250), true)

    // 窗口在顶部（首 50 条之外）：折叠条不在窗口内，不渲染
    // 250 条经批次 C1 折叠为 200 条数据源后初始窗口仍是 [0, 13)（visibleRows=ceil(600/72)=9
    // 加 overscan 4）——原 :109 的 <200 松判据换成精确 13（此 13 是「200 条数据源的初始窗口」）
    expect(wrapper.findAll('.modal-message')).toHaveLength(13)
    expect(wrapper.find('.message-fold-bar').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('消息60')

    // 滚到第 50 条附近：折叠条随窗口进入 DOM
    await scrollTo(wrapper, 50 * 72)
    const bar = wrapper.find('.message-fold-bar')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('50')
    // 折叠仍生效：中段消息依旧不在数据源里
    expect(wrapper.text()).not.toContain('消息60')
  })

  it('virtual=true 点击折叠条 ⇒ 展开：折叠条消失、中段进窗口（G2）', async () => {
    // G2：既有 4 条里折叠条点击只在关闭态被覆盖，虚拟态下"点了不展开"这种缺陷当时测不出，本条补上。
    // 判据「渲染消息数升到全量」在本组件实测不确（2026-10-31 实测只覆盖折叠条，未覆盖全量）：
    // 窗口行数由 scrollTop 与视口高度决定，
    // 展开前后都落在窗口 [46, 63) ⇒ 行数不变；展开的可观测形态＝中段消息进窗口 + 底部占位随数据源 200→250 变高。
    const wrapper = mountList(makeMessages(250), true)

    // 滚到第 50 条附近：折叠条随窗口进入 DOM（定位方式与上一条用例同源）
    await scrollTo(wrapper, 50 * 72)
    const bar = wrapper.find('.message-fold-bar')
    expect(bar.exists()).toBe(true)
    expect(wrapper.text()).not.toContain('消息60')
    const bottomBefore = spacerHeightPx(wrapper, 'bottom')

    await bar.trigger('click')

    // 展开：foldExpanded=true ⇒ hiddenCount 归 0 ⇒ 折叠条消失，中段"消息60"进入数据源与窗口
    expect(wrapper.find('.message-fold-bar').exists()).toBe(false)
    expect(wrapper.text()).toContain('消息60')
    // 精确断言（行数、占位高度都按窗口 [46, 63) 反推）：展开前 bottom=(200-63)×72，展开后=(250-63)×72
    expect(bottomBefore).toBe((200 - 63) * 72)
    expect(wrapper.findAll('.modal-message')).toHaveLength(63 - 46)
    expect(spacerHeightPx(wrapper, 'bottom')).toBe((250 - 63) * 72)
  })

  it('virtual=true 量到 clientHeight=0 ⇒ 视口回落 FALLBACK_VIEWPORT_HEIGHT，精确钉住（G3）', async () => {
    // G3 实际缺口（按实测改判，两段式）：前半面——台账点名的 `>=`/`<=` 形态（GreaterThanOrEqual/
    // LessThanOrEqual 两族 matcher）实测在 web/src 的**断言面** 0 命中；对该词做全量 grep 只命中本注释自身的这几行
    // （2026-10-31 实测：web/src 下命中全在本文件此处，无一处是断言；该时点本注释含两族全称）——**除本注释自指外**断言面才是 0 命中。
    // 后半面——登记时点严格不等式（GreaterThan/LessThan 两族 matcher）尚有 8 处（登记时点行号
    // :81 :82 :84 :90 :91 :92 :93 :109，均属本文件既有旧用例）⇒ 当时按「形态已迁移、未全闭合」登记；
    // 该 8 处已由庚-2 G2-B 批（2026-11-01）改为 6 处精确断言（:81+:82 与 :90+:91 各自合并，
    // toBe 蕴含原上下界、判据只紧不松）；本条钉的是 G3 后半面「回落分支未被钉住」。
    // happy-dom 里 clientHeight 恒 0，
    // 现有虚拟态用例全部经由 ChatMessageList.vue::refreshMetrics 的回落分支
    // （viewportPx = src.clientHeight > 0 ? src.clientHeight : FALLBACK_VIEWPORT_HEIGHT，
    // 符号 FALLBACK_VIEWPORT_HEIGHT 定义在 composables/useVirtualWindow.ts），却没人钉它——
    // 谁把回落"清理"成直接取量值，窗口退化成 [0, DEFAULT_OVERSCAN) 只剩 overscan 条，现有用例仍全绿。
    // 本条同时兑现 G3 处置列「改精确断言」：全部 toBe/toHaveLength，不用不等式。
    // 反推：视口=FALLBACK_VIEWPORT_HEIGHT(600) ⇒ visibleRows=ceil(600/72)=9 ⇒ 初始窗口 [0, 9+4)=[0, 13)。
    const wrapper = mountList(makeMessages(150), true)
    // 触发一次 scroll 让 onMounted 的补量与重渲染确定完成（若回落被去掉，此处视口变 0，本条必红）
    await scrollTo(wrapper, 0)

    expect(wrapper.findAll('.modal-message')).toHaveLength(13)
    expect(wrapper.text()).toContain('消息12')
    expect(wrapper.text()).not.toContain('消息13')
    expect(spacerHeightPx(wrapper, 'top')).toBe(0)
    expect(spacerHeightPx(wrapper, 'bottom')).toBe((150 - 13) * 72)
  })

  it('关闭态行为等价：不传 virtual 与 virtual=false 的 html 逐字相等，结构无虚拟滚动痕迹（G6）', () => {
    // G6：交付时那次「关闭态行为等价」比对靠仓外脚本，脚本已删 ⇒ 结论不可复跑（台账原话）。
    // 换成仓内可复跑断言：随测试套每次执行，红了就有人看见。刻意不用 toMatchSnapshot——
    // 快照面会把"不可复跑"换个形态继续存在（本文件既有用例也都没走快照）。
    const messages = makeMessages(120) // 与既有两条关闭态用例同源（120 条）
    const implicit = mountList(messages)
    const explicit = mountList(messages, false)

    // 两种挂载方式逐字相等——原脚本比的就是这个，现在它在仓内复跑
    expect(explicit.html()).toBe(implicit.html())
    // 关闭态结构面三条：消息节点数=全量条数；占位元素数=0；不出现虚拟滚动用的定位样式
    expect(implicit.findAll('.modal-message')).toHaveLength(120)
    expect(explicit.findAll('.modal-message')).toHaveLength(120)
    expect(implicit.findAll('.virtual-spacer')).toHaveLength(0)
    expect(explicit.findAll('.virtual-spacer')).toHaveLength(0)
    // translateY 实测 ChatMessageList.vue 全文 grep 0 命中 ⇒ 下面两条当前恒真，属前瞻守卫：
    // 钉的是「将来若改用 translateY 定位时关闭态仍不得出现该样式」，不声称当下有判别力。
    expect(implicit.html()).not.toContain('translateY')
    expect(explicit.html()).not.toContain('translateY')
  })
})
