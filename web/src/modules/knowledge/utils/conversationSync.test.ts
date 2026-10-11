import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useConversationChat } from '../composables/useConversationChat'
import type { ConversationItem } from '../types/conversation'
import {
  CONVERSATION_SYNC_CHANNEL,
  openConversationSync,
  type ConversationSyncEvent,
} from './conversationSync'

vi.mock('../api/conversations', () => ({
  getConversation: vi.fn(async () => ({ data: { data: null } })),
  deleteConversation: vi.fn(async () => ({ data: {} })),
  listConversationsPaged: vi.fn(async () => ({ data: { data: { items: [] } } })),
  renameConversation: vi.fn(async () => ({ data: {} })),
  listKbConversations: vi.fn(async () => ({ data: [] })),
}))

/** 假 BroadcastChannel：投递给同名**其它**实例（不回声，符合规范）。 */
class FakeBroadcastChannel {
  static instances: FakeBroadcastChannel[] = []
  onmessage: ((ev: { data: ConversationSyncEvent }) => void) | null = null
  closed = false
  constructor(public name: string) {
    FakeBroadcastChannel.instances.push(this)
  }
  postMessage(data: ConversationSyncEvent) {
    for (const peer of FakeBroadcastChannel.instances) {
      if (peer === this || peer.closed || peer.name !== this.name) continue
      peer.onmessage?.({ data })
    }
  }
  close() {
    this.closed = true
  }
}

const item = (id: string, title: string): ConversationItem => ({
  id,
  title,
  updated_at: '2026-09-28T00:00:00.000Z',
  kb_id: 'kb-1',
})

describe('conversationSync（批次 B4 跨标签页同步）', () => {
  beforeEach(() => {
    FakeBroadcastChannel.instances = []
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('往返：本标签页广播 新建/删除/重命名，其它标签页收到且自身不回声', () => {
    const selfReceived: ConversationSyncEvent[] = []
    const peerReceived: ConversationSyncEvent[] = []
    const a = openConversationSync((e) => selfReceived.push(e))
    const b = openConversationSync((e) => peerReceived.push(e))

    expect(FakeBroadcastChannel.instances[0]?.name).toBe(CONVERSATION_SYNC_CHANNEL)

    a.post({ type: 'create', item: item('s1', '会话一') })
    a.post({ type: 'rename', id: 's1', title: '改名' })
    a.post({ type: 'delete', id: 's1' })

    expect(peerReceived).toEqual([
      { type: 'create', item: item('s1', '会话一') },
      { type: 'rename', id: 's1', title: '改名' },
      { type: 'delete', id: 's1' },
    ])
    expect(selfReceived).toEqual([])

    a.close()
    b.close()
    expect(FakeBroadcastChannel.instances.every((c) => c.closed)).toBe(true)
  })

  it('环境不支持 BroadcastChannel 时静默降级（post/close 不抛）', () => {
    vi.stubGlobal('BroadcastChannel', undefined)
    const sync = openConversationSync(() => {})
    expect(() => sync.post({ type: 'delete', id: 'x' })).not.toThrow()
    expect(() => sync.close()).not.toThrow()
  })

  it('非法事件形状被丢弃（不进入合并逻辑）', () => {
    const received: ConversationSyncEvent[] = []
    const a = openConversationSync(() => {})
    const b = openConversationSync((e) => received.push(e))

    a.post({ type: 'delete', id: 'ok' })
    // 直接向 b 的底层通道注入畸形载荷（模拟异构发布者）
    const raw = FakeBroadcastChannel.instances[1]
    raw.onmessage?.({ data: { type: 'unknown', id: 'x' } } as unknown as {
      data: ConversationSyncEvent
    })
    raw.onmessage?.({ data: 'not-an-object' } as unknown as {
      data: ConversationSyncEvent
    })

    expect(received).toEqual([{ type: 'delete', id: 'ok' }])
    a.close()
    b.close()
  })
})

describe('useConversationChat 接收合并（批次 B4）', () => {
  beforeEach(() => {
    FakeBroadcastChannel.instances = []
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('收到远端 新建/重命名/删除 事件后合并本地列表并清空选中', async () => {
    const chat = useConversationChat({ mode: 'home' })
    const channel = FakeBroadcastChannel.instances[0]
    expect(channel).toBeTruthy()
    const fire = (data: ConversationSyncEvent) => channel.onmessage?.({ data })

    // 远端新建 → 插入列表
    fire({ type: 'create', item: item('s1', '会话一') })
    expect(chat.conversations.value.map((c) => c.id)).toEqual(['s1'])

    // 选中（getConversation 已 mock）
    await chat.selectConversation(chat.conversations.value[0])
    expect(chat.currentConversation.value?.id).toBe('s1')

    // 远端重命名 → 列表与当前会话标题同步更新
    fire({ type: 'rename', id: 's1', title: '改名后的会话' })
    expect(chat.conversations.value[0]?.title).toBe('改名后的会话')
    expect(chat.currentConversation.value?.title).toBe('改名后的会话')

    // 远端删除 → 列表移除且当前选中被清空
    fire({ type: 'delete', id: 's1' })
    expect(chat.conversations.value).toHaveLength(0)
    expect(chat.currentConversation.value).toBeNull()
  })
})
