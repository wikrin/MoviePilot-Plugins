export interface SseEvent {
  event: string
  id?: string
  data: string
}

/** 只保留未完成事件；不累计整个连接的正文。EOF 半帧不会被当作完整事件。 */
export class SseParser {
  private decoder = new TextDecoder('utf-8', { fatal: true })
  private line = ''
  private data: string[] = []
  private event = ''
  private id: string | undefined
  private afterCR = false
  private bytes = 0
  constructor(
    private dispatch: (event: SseEvent) => void,
    private maxBytes = 64 * 1024,
  ) {}

  get bufferedBytes() {
    return this.bytes
  }

  feed(chunk: Uint8Array) {
    this.consume(this.decoder.decode(chunk, { stream: true }))
  }

  finish() {
    this.consume(this.decoder.decode())
    // SSE 只有空行才提交事件；断线后的半帧必须从已提交游标重新补发。
    this.line = ''
    this.data = []
    this.event = ''
    this.id = undefined
    this.bytes = 0
    this.afterCR = false
  }

  private consume(text: string) {
    for (const char of text) {
      if (this.afterCR) {
        this.afterCR = false
        if (char === '\n') continue
      }
      const code = char.codePointAt(0)!
      this.bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4
      if (this.bytes > this.maxBytes) throw new Error('日志流事件超过大小限制')
      if (char === '\r' || char === '\n') {
        this.consumeLine()
        this.afterCR = char === '\r'
      } else this.line += char
    }
  }

  private consumeLine() {
    const line = this.line
    this.line = ''
    if (!line) {
      const record = this.data.length
        ? { event: this.event || 'message', id: this.id, data: this.data.join('\n') }
        : undefined
      this.data = []
      this.event = ''
      this.id = undefined
      this.bytes = 0
      if (record) this.dispatch(record)
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') this.data.push(value)
    else if (field === 'event') this.event = value
    else if (field === 'id' && !value.includes('\0')) this.id = value
  }
}

/** 用于流式请求的有限 JSON 错误响应；正常 SSE 不经过此函数。 */
export async function readStreamText(stream: ReadableStream<Uint8Array>, maxBytes = 16 * 1024): Promise<string> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let bytes = 0
  let text = ''
  try {
    while (true) {
      const result = await reader.read()
      if (result.done) break
      bytes += result.value.byteLength
      if (bytes > maxBytes) throw new Error('错误响应超过大小限制')
      text += decoder.decode(result.value, { stream: true })
    }
    return text + decoder.decode()
  } finally {
    try {
      await reader.cancel()
    } catch {
      /* 断开本身不改变原始错误 */
    }
    reader.releaseLock()
  }
}
