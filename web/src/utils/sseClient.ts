import { ApiError, TOKEN_STORAGE_KEY } from '@/api/http'
import type {
  ApiResponse,
  CitationResponse,
  SseMessageDelta,
  SseMessageError,
  SseMessageStart,
} from '@/types/api'

/**
 * SSE 客户端（Chat 专用）。
 *
 * 浏览器原生 EventSource 不支持 POST / 自定义请求头，
 * 因此这里用 fetch + ReadableStream 手动解析。
 *
 * 后端事件契约（docs/design/FRONTEND_API_SPEC.md §3）：
 *   message.start / message.delta / message.end / message.error
 * 另外兼容通用约定的 `done` 事件（与 message.end 等同处理）。
 */

export interface SseFrame {
  event: string
  data: string
}

/** message.end / done 的归一化载荷；done 可能不带 messageId / runId。 */
export interface ChatStreamEnd {
  messageId?: number
  runId?: number
  citations: CitationResponse[]
  truncated?: boolean
  finishReason?: string | null
}

export interface ChatStreamHandlers {
  /** 首事件，进入「生成中」状态 */
  onStart?: (data: SseMessageStart) => void
  /** 增量文本 */
  onDelta?: (data: SseMessageDelta) => void
  /** 成功终态；message.end 与 done 都会走到这里 */
  onEnd?: (data: ChatStreamEnd) => void
  /** 后端通过 message.error 下发的失败终态 */
  onServerError?: (data: SseMessageError) => void
  /** 流正常结束（无论成功还是 message.error 之后） */
  onClose?: () => void
}

export interface ChatStreamRequest {
  conversationId: number
  content: string
  /**
   * 幂等键：同一次发送（含自动重试）必须复用同一个值。
   * 不传时由 streamChatMessage 生成一个；调用方若要重试同一问题，应自己生成并复用。
   */
  clientRequestId?: string
  signal?: AbortSignal
}

/**
 * 读取并解析 SSE 流。
 *
 * - `TextDecoder` 使用 `{ stream: true }`，跨 chunk 的多字节 UTF-8 字符会被正确拼接；
 * - 按行解析，未完成的半行 / 半个事件留在 buffer 里等下一个 chunk；
 * - 同时兼容 `\n`、`\r\n`、`\r` 三种换行；
 * - 以空行分隔事件，多行 `data:` 用 `\n` 拼接。
 */
export async function consumeSseStream(
  stream: ReadableStream<Uint8Array>,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let eventName = 'message'
  const dataLines: string[] = []

  const dispatch = () => {
    if (dataLines.length > 0) {
      onFrame({ event: eventName, data: dataLines.join('\n') })
    }
    eventName = 'message'
    dataLines.length = 0
  }

  const handleLine = (rawLine: string) => {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line === '') {
      dispatch()
      return
    }
    // 以冒号开头的是注释 / 心跳，忽略
    if (line.startsWith(':')) {
      return
    }

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }

    if (field === 'event') {
      eventName = value || 'message'
    } else if (field === 'data') {
      dataLines.push(value)
    }
    // id / retry 字段对本项目无意义，忽略
  }

  const drain = (final: boolean) => {
    let start = 0

    for (let i = 0; i < buffer.length; i += 1) {
      const char = buffer[i]

      if (char === '\n') {
        handleLine(buffer.slice(start, i))
        start = i + 1
        continue
      }

      if (char === '\r') {
        // 末尾的 CR 可能是被 chunk 截断的 CRLF，等下一个 chunk 再判断
        if (i + 1 >= buffer.length) {
          break
        }
        if (buffer[i + 1] === '\n') {
          continue // CRLF 交给 \n 分支统一处理
        }
        handleLine(buffer.slice(start, i))
        start = i + 1
      }
    }

    buffer = buffer.slice(start)

    if (final) {
      if (buffer.length > 0) {
        handleLine(buffer)
      }
      buffer = ''
      dispatch()
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })
      drain(false)
    }

    // flush 解码器里可能残留的半个多字节字符
    buffer += decoder.decode()
    drain(true)
  } finally {
    reader.releaseLock()
  }
}

/**
 * 发送一条用户消息并以 SSE 接收助手回复。
 *
 * 建流前的错误（对话不存在 / 已归档 / 参数错误）后端返回普通 JSON，
 * 这里会转成 ApiError 抛出；客户端中止会抛出 AbortError。
 */
export async function streamChatMessage(
  { conversationId, content, clientRequestId, signal }: ChatStreamRequest,
  handlers: ChatStreamHandlers = {},
): Promise<void> {
  const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8080').replace(/\/+$/, '')
  const token = localStorage.getItem(TOKEN_STORAGE_KEY)

  const response = await fetch(
    `${baseUrl}/api/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        content,
        clientRequestId: clientRequestId ?? newClientRequestId(),
      }),
      signal,
    },
  )

  const contentType = response.headers.get('content-type') ?? ''
  if (!response.ok || !contentType.includes('text/event-stream')) {
    throw await toApiError(response)
  }
  if (!response.body) {
    throw new ApiError(0, '当前浏览器不支持流式响应')
  }

  await consumeSseStream(response.body, (frame) => {
    const payload = parsePayload(frame.data)

    switch (frame.event) {
      case 'message.start':
        handlers.onStart?.(payload as SseMessageStart)
        break

      case 'message.delta': {
        const delta = payload as Partial<SseMessageDelta> | null
        if (typeof delta?.delta === 'string' && delta.delta.length > 0) {
          handlers.onDelta?.({ delta: delta.delta })
        }
        break
      }

      case 'message.end':
      // 兼容通用约定的 done 事件
      case 'done':
        handlers.onEnd?.(normalizeEnd(payload))
        break

      case 'message.error': {
        const error = payload as Partial<SseMessageError> | null
        handlers.onServerError?.({
          runId: typeof error?.runId === 'number' ? error.runId : 0,
          code: typeof error?.code === 'number' ? error.code : 500,
          message:
            typeof error?.message === 'string' && error.message
              ? error.message
              : '生成回复失败',
        })
        break
      }

      default:
        // 未知事件忽略，保证向前兼容
        break
    }
  })

  handlers.onClose?.()
}

/**
 * 生成后端要求的 clientRequestId（8–64 字符）。
 * 优先用 crypto.randomUUID；非安全上下文（如局域网 http）没有该 API 时退回随机串。
 */
function newClientRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

async function toApiError(response: Response): Promise<ApiError> {
  let payload: ApiResponse<unknown> | null = null
  try {
    payload = (await response.json()) as ApiResponse<unknown>
  } catch {
    // 非 JSON 响应体，走下面的兜底文案
  }

  return new ApiError(
    payload?.code ?? response.status,
    payload?.message ?? `请求失败（HTTP ${response.status}）`,
    response.status,
  )
}

function parsePayload(data: string): unknown {
  if (!data) {
    return null
  }
  try {
    return JSON.parse(data)
  } catch {
    // 非 JSON 的 data 原样返回，避免一个坏事件中断整条流
    return data
  }
}

function normalizeEnd(payload: unknown): ChatStreamEnd {
  const raw =
    payload !== null && typeof payload === 'object'
      ? (payload as Record<string, unknown>)
      : {}

  return {
    messageId: typeof raw.messageId === 'number' ? raw.messageId : undefined,
    runId: typeof raw.runId === 'number' ? raw.runId : undefined,
    citations: normalizeCitations(raw.citations),
    truncated: raw.truncated === true ? true : undefined,
    finishReason: typeof raw.finishReason === 'string' ? raw.finishReason : null,
  }
}

function normalizeCitations(value: unknown): CitationResponse[] {
  if (!Array.isArray(value)) {
    return []
  }

  const citations: CitationResponse[] = []
  for (const item of value) {
    if (item === null || typeof item !== 'object') {
      continue
    }
    const raw = item as Record<string, unknown>
    if (typeof raw.documentId !== 'number' || typeof raw.chunkId !== 'number') {
      continue
    }
    citations.push({
      documentId: raw.documentId,
      chunkId: raw.chunkId,
      chunkIndex: typeof raw.chunkIndex === 'number' ? raw.chunkIndex : null,
      sourceTitle: typeof raw.sourceTitle === 'string' ? raw.sourceTitle : '',
      available: raw.available !== false,
    })
  }
  return citations
}
