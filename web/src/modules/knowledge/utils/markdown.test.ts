// @vitest-environment jsdom
// DOMPurify 依赖真实 DOM 语义（happy-dom 的节点突变/资源加载与它不兼容），
// 消毒断言必须在 jsdom 下执行才代表真实浏览器行为。
import { describe, expect, it } from 'vitest'

import { escapeHtml, memoRenderMarkdown, renderMarkdown } from './markdown'

describe('escapeHtml', () => {
  it('转义五类关键字符', () => {
    expect(escapeHtml(`<a href="x">&'`)).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;')
  })
})

describe('renderMarkdown（marked + dompurify，绝不裸 v-html）', () => {
  it('基础 markdown：标题/粗体/列表', () => {
    const html = renderMarkdown('# 标题\n\n**粗**\n\n- a\n- b')
    expect(html).toContain('<h1')
    expect(html).toContain('<strong>粗</strong>')
    expect(html).toContain('<li>')
  })

  it('GFM 表格与换行（breaks）', () => {
    const html = renderMarkdown('| a | b |\n| - | - |\n| 1 | 2 |\n\n第一行\n第二行')
    expect(html).toContain('<table>')
    expect(html).toContain('<br')
  })

  it('XSS：script 标签被剥除', () => {
    const html = renderMarkdown('安全<script>alert(1)</script>文本')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('alert(1)')
    expect(html).toContain('文本')
  })

  it('XSS：img onerror 处理器被剥除', () => {
    const html = renderMarkdown('<img src=x onerror="alert(2)">')
    expect(html).not.toContain('onerror')
  })

  it('XSS：javascript: 协议链接被消毒', () => {
    const html = renderMarkdown('[点我](javascript:alert(3))')
    expect(html.toLowerCase()).not.toContain('javascript:')
  })

  it('XSS：iframe 剥除', () => {
    expect(renderMarkdown('<iframe src="https://evil"></iframe>')).not.toContain('<iframe')
  })

  it('空文本返回空串', () => {
    expect(renderMarkdown('')).toBe('')
    expect(memoRenderMarkdown('')).toBe('')
  })

  it('内联代码与代码块保留', () => {
    const html = renderMarkdown('`code`\n\n```\nblock\n```')
    expect(html).toContain('<code>')
    expect(html).toContain('<pre>')
  })
})

describe('memoRenderMarkdown（同输入缓存）', () => {
  it('同文本第二次返回同一引用（命中缓存不重复 parse）', () => {
    const text = '# 缓存命中测试\n\n**x**'
    const first = memoRenderMarkdown(text)
    const second = memoRenderMarkdown(text)
    expect(second).toBe(first)
  })

  it('不同文本各自解析', () => {
    expect(memoRenderMarkdown('# A')).not.toBe(memoRenderMarkdown('# B'))
  })
})
