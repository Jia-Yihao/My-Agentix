import { reactive } from 'vue'

/**
 * 零依赖 Toast（Phase H · W6 B10）。
 *
 * 模块级单例队列 + `ToastHost` 挂根渲染；替换 `window.alert` 类打断式
 * 反馈（useKbModalChat 的失败提示等）。自动消失（默认 3s），`duration`
 * 可注入（测试用 fake timers 控制）。
 */

export type ToastType = 'success' | 'error' | 'info'

export type ToastItem = {
  id: number
  message: string
  type: ToastType
}

export type ToastOptions = {
  type?: ToastType
  /** 自动消失毫秒数；0 = 不自动消失（需手动 dismiss） */
  duration?: number
}

const DEFAULT_DURATION = 3000

const state = reactive<{ items: ToastItem[] }>({ items: [] })
let nextId = 1
const timers = new Map<number, ReturnType<typeof setTimeout>>()

export function toast(message: string, options: ToastOptions = {}): number {
  const id = nextId++
  const type = options.type ?? 'info'
  const duration = options.duration ?? DEFAULT_DURATION
  state.items.push({ id, message, type })
  if (duration > 0) {
    timers.set(
      id,
      setTimeout(() => {
        dismissToast(id)
      }, duration),
    )
  }
  return id
}

export function dismissToast(id: number): void {
  const timer = timers.get(id)
  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(id)
  }
  const idx = state.items.findIndex((t) => t.id === id)
  if (idx >= 0) state.items.splice(idx, 1)
}

export function clearToasts(): void {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  state.items.splice(0, state.items.length)
}

export function useToast() {
  return {
    items: state.items,
    toast,
    dismiss: dismissToast,
  }
}
