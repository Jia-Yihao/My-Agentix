import DOMPurify from 'dompurify'
import { marked } from 'marked'

/**
 * 助手消息 markdown 渲染（Phase H · W6 A3）。
 *
 * 链路：`marked.parse`（GFM + breaks）→ `DOMPurify.sanitize` → `v-html`。
 * **绝不裸 v-html**：消毒失败 / 解析异常一律降级为转义纯文本（轻量兜底），
 * XSS 样本（`<script>` / `onerror=` / `javascript:` 链接）由 dompurify 剥除。
 *
 * 流式性能：解析只在**批渲染 flush 节拍**（50ms 窗口，见 useStreamBuffer）
 * 后的组件渲染中发生，而非每 token；`memoRenderMarkdown` 对同一输入文本
 * 返回缓存结果，避免同帧内重复解析。
 */

/** 转义 HTML（降级路径 / 用户消息不走 markdown 时使用） */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** markdown → 安全 HTML；异常时降级为转义纯文本（绝不输出未消毒 HTML） */
export function renderMarkdown(text: string): string {
  if (!text) return ''
  try {
    const raw = marked.parse(text, { async: false, gfm: true, breaks: true })
    return DOMPurify.sanitize(String(raw))
  } catch {
    return escapeHtml(text)
  }
}

const memoCache = new Map<string, string>()

/** 同一输入文本 → 缓存输出（组件每帧渲染不重复 parse；文本变化自动失效） */
export function memoRenderMarkdown(text: string): string {
  if (!text) return ''
  const hit = memoCache.get(text)
  if (hit !== undefined) return hit
  const html = renderMarkdown(text)
  // 缓存有界：长流文本持续增长，超限整体清空（命中率由最近一次主导）
  if (memoCache.size > 200) memoCache.clear()
  memoCache.set(text, html)
  return html
}
