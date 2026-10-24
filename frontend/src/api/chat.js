import request from './request'

// 同步问答
export function chat(data) {
  return request.post('/chat', data)
}

// 流式问答（fetch + SSE）
export async function chatStream(question, token, onMessage, onDone, onError) {
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ question }),
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6)
          if (data === '[DONE]') {
            onDone && onDone()
            return
          }
          onMessage && onMessage(data)
        }
      }
    }
    onDone && onDone()
  } catch (e) {
    onError && onError(e)
  }
}

/**
 * Agent 富事件流（/api/chat/agent/stream）
 *
 * 与 chatStream 的区别：多了「过程事件」——分诊意图、工具调用、引用校验。
 * 事件协议（每行 `data: <json>` 或 `data: <纯文本>`）：
 *   {type:'start'} / {type:'intent', intent, label} / {type:'step', text}
 *   {type:'tool', tool, ok, ms, summary} / {type:'references', references[, verified]}
 *   {type:'citation_check', coverage, decision, dropped, unsupported}
 *   {type:'fallback', reason} / {type:'error', message} / {type:'done', tool_calls, duration_ms}
 * 纯文本 chunk（模型逐段输出）不带 type。后端降级时会先发 fallback，
 * 之后透传固定管线的 chunk（首包是 {references:[...]}），因此这里始终保留
 * 「按 references 包解析」的分支，保证降级后引用仍能正常显示。
 *
 * handlers: { onTrace, onText, onReferences, onCitationCheck, onDone, onError }
 */
export async function chatAgentStream(question, token, handlers = {}) {
  const { onTrace, onText, onReferences, onCitationCheck, onDone, onError } = handlers
  try {
    const response = await fetch('/api/chat/agent/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ question }),
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6)
        if (data === '[DONE]') {
          onDone && onDone()
          return
        }

        let evt = null
        try {
          const parsed = JSON.parse(data)
          if (parsed && typeof parsed === 'object') evt = parsed
        } catch (e) {
          // 模型逐段输出的纯文本，不是 JSON
        }

        if (evt && evt.type) {
          switch (evt.type) {
            case 'intent':
              onTrace && onTrace({ kind: 'intent', intent: evt.intent, label: evt.label })
              break
            case 'step':
              onTrace && onTrace({ kind: 'step', text: evt.text })
              break
            case 'tool':
              onTrace &&
                onTrace({
                  kind: 'tool',
                  tool: evt.tool,
                  ok: evt.ok,
                  ms: evt.ms,
                  summary: evt.summary,
                })
              break
            case 'references':
              onReferences && onReferences(evt.references || [], !!evt.verified)
              break
            case 'citation_check':
              onCitationCheck && onCitationCheck(evt)
              break
            case 'fallback':
              onTrace && onTrace({ kind: 'fallback', reason: evt.reason })
              break
            case 'error':
              onTrace && onTrace({ kind: 'error', message: evt.message })
              break
            case 'done':
              onTrace &&
                onTrace({
                  kind: 'done',
                  tool_calls: evt.tool_calls,
                  duration_ms: evt.duration_ms,
                })
              break
            case 'start':
            default:
              break
          }
        } else if (evt && Array.isArray(evt.references)) {
          // 固定管线协议的首包（Agent 降级后会出现）
          onReferences && onReferences(evt.references, false)
        } else {
          onText && onText(data)
        }
      }
    }
    onDone && onDone()
  } catch (e) {
    onError && onError(e)
  }
}
