/**
 * 会话列表跨标签页同步（批次 B4 / audit-2026-09-28 §2 B4）。
 *
 * 此前在标签页 A 新建/删除/重命名会话后，标签页 B 的列表保持旧状态
 * （直至关点刷新才一致）。经 `BroadcastChannel('agentix:conversations')`
 * 广播三类变更事件，接收方**合并**进本地列表（不整表覆盖，保留本地
 * `local-` 草稿与分页状态）。
 *
 * BroadcastChannel 不向发送方投递（规范行为），故无需自回声过滤；
 * 环境不支持（SSR / 老浏览器 / 测试环境）时静默降级为单标签页运行。
 */

import type { ConversationItem } from '../types/conversation'

export const CONVERSATION_SYNC_CHANNEL = 'agentix:conversations'

export type ConversationSyncEvent =
  | { type: 'create'; item: ConversationItem }
  | { type: 'delete'; id: string }
  | { type: 'rename'; id: string; title: string }

export type ConversationSync = {
  post(event: ConversationSyncEvent): void
  close(): void
}

/**
 * 打开同步通道。`onEvent` 只会被**其它标签页**的投递触发。
 * 返回的 `post` 尽力而为：序列化/关闭竞态失败不影响本标签页操作。
 */
export function openConversationSync(
  onEvent: (event: ConversationSyncEvent) => void,
): ConversationSync {
  if (typeof BroadcastChannel === 'undefined') {
    return { post: () => {}, close: () => {} }
  }
  const channel = new BroadcastChannel(CONVERSATION_SYNC_CHANNEL)
  channel.onmessage = (ev: MessageEvent) => {
    const data: unknown = ev?.data
    if (!data || typeof data !== 'object') return
    const type = (data as { type?: unknown }).type
    if (type !== 'create' && type !== 'delete' && type !== 'rename') return
    onEvent(data as ConversationSyncEvent)
  }
  return {
    post: (event) => {
      try {
        channel.postMessage(event)
      } catch {
        // 尽力而为：跨标签页同步失败不阻断本地变更
      }
    },
    close: () => {
      try {
        channel.close()
      } catch {
        // 已关闭/环境差异：忽略
      }
    },
  }
}
