/**
 * R5c：跨模块共享的 SSE 帧解析（framing 层）。
 *
 * 方案 v2 D1/R5：chat 与 knowledge 两个模块此前各写了一套「字节 → 帧 →
 * data 行」的机械解析（缓冲拼接、帧边界切分、坏包容错、残留尾帧、
 * reader 锁释放），行为等价却各自演化。本模块只收口**机械层**：
 *
 * - 分块读取 + TextDecoder 流式解码；
 * - 帧边界切分（`"\n\n"` 与 `"\r\n\r\n"`）；
 * - `data:` 行提取（含多 data 行帧）与 `""` / `[DONE]` 过滤；
 * - 残留最后一帧（服务端不补尾空行）兜底派发；
 * - 缓冲溢出保护（先 cancel 再抛，防异常服务端撑爆内存）；
 * - finally 释放 reader 锁。
 *
 * **业务语义不在此层**：JSON 解析容错、词汇归一（type/event）、终态
 * （terminated）与回调分发仍由各模块的 onData 回调自行实现。
 */

/** 单个 SSE 帧的最大缓存（与两模块既有口径一致）。 */
export const SSE_MAX_BUFFER = 1024 * 1024;

/** 一个 SSE 块内全部 `data:` 行的载荷（已滤掉空行与 `[DONE]`）。 */
export function extractSseDataLines(block: string): string[] {
  return block
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^\s/, ''))
    .filter((line) => line !== '' && line !== '[DONE]')
}

/** 单条 data 载荷的消费者（同步/异步均可；抛错向外传播）。 */
export type SseDataConsumer = (dataLine: string) => void | Promise<void>;

/**
 * 以流式方式读取 SSE 响应，逐条 `data:` 载荷回调。
 *
 * - 自动处理分块、缓存残留 buffer、残留尾帧；
 * - 坏包（非 JSON 等）交由 `onData` 自行容错——本层只保证行提取；
 * - `onData` 抛错原样向外传播（截断检测依赖该语义）。
 */
export async function readSseData(
  res: Response,
  onData: SseDataConsumer,
  opts: { maxBuffer?: number } = {}
): Promise<void> {
  if (!res.body) throw new Error('响应体为空');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const maxBuffer = opts.maxBuffer ?? SSE_MAX_BUFFER;
  let buffer = '';

  const handleBlock = async (block: string): Promise<void> => {
    for (const dataLine of extractSseDataLines(block)) {
      await onData(dataLine);
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      if (buffer.length > maxBuffer) {
        await reader.cancel().catch(() => undefined);
        throw new Error('stream buffer overflow');
      }

      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() || '';
      for (const part of parts) {
        await handleBlock(part);
      }
    }
    // 残留的最后一帧（部分服务端不补尾空行）
    if (buffer.trim()) {
      await handleBlock(buffer);
    }
  } finally {
    reader.releaseLock();
  }
}
