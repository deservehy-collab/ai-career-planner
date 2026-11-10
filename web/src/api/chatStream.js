/**
 * AI 助手的流式请求：`POST /api/chat/stream`（SSE）。
 *
 * 为什么不复用 client.js 的 request()：那个实现把整个响应体读成一份 JSON
 * （`response.json()`），与"边生成边显示"是冲突的。这里只复用它的两件事：
 * 同一套 Bearer token 读取，和同一套错误文案口径（describeDetail），
 * 保证鉴权失效、后端挂了这些情况在其他页面和聊天页表现一致。
 *
 * 协议与 backend/chat.py 一一对应（UTF-8，消息之间以空行分隔）：
 *   event: start  data: {"model": "qwen2.5:3b"}
 *   event: delta  data: {"text": "增量文本"}
 *   event: done   data: {"chars": 128}
 *   event: error  data: {"message": "给人看的一句话"}
 *
 * 取消：调用方传 AbortController 的 signal。abort() 会掐断 fetch，后端随之关闭
 * 上游连接。注意 Ollama 不保证立即终止已开始的生成（它只有一个推理槽），
 * 真正防止"无限生成占死服务"的是后端下发的 num_predict 上限。
 */

import {
  ApiError, NETWORK_ERROR_STATUS, describeDetail, redirectToLogin,
} from './client'
import { clearAuthStorage, getAuthToken } from '../utils/storage'

const BASE = '/api'

/** SSE 的一帧可能是 `event: x` + `data: {...}`，也可能只有 data；解析成 {event, data}。 */
function parseFrame(frame) {
  let event = 'message'
  const dataLines = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue // 空行与注释帧
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return { event, data: null }
  const text = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(text) }
  } catch {
    // 后端保证 data 是单行 JSON；真出现坏帧说明协议对不上，如实报错而不是猜
    return { event, data: null, broken: true }
  }
}

async function readErrorDetail(response) {
  const text = await response.text().catch(() => '')
  if (!text) return null
  try {
    return JSON.parse(text).detail ?? text
  } catch {
    return text
  }
}

/**
 * 发起一次流式对话。
 *
 * @param {object}   options
 * @param {string}   options.message   本轮提问（非空）
 * @param {Array}    options.history   本轮之前的对话 [{role, content}]，后端还会二次裁剪
 * @param {AbortSignal} options.signal 取消信号
 * @param {Function} options.onStart   收到 start 事件（模型已接受请求）
 * @param {Function} options.onDelta   收到文本增量
 * @param {Function} options.onDone    正常结束
 * @returns {Promise<number>} 已接收的字符数
 */
export async function streamChat({ message, history = [], signal, onStart, onDelta, onDone }) {
  const init = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message, history }),
    signal,
  }

  const token = getAuthToken()
  if (token) init.headers.Authorization = `Bearer ${token}`

  let response
  try {
    response = await fetch(`${BASE}/chat/stream`, init)
  } catch (cause) {
    if (cause && cause.name === 'AbortError') throw cause
    throw new ApiError(
      '连不上服务，这次消息没有发出去。稍等一会儿再试。',
      { status: NETWORK_ERROR_STATUS, cause },
    )
  }

  if (!response.ok) {
    const detail = await readErrorDetail(response)
    // 与 client.js 同口径：业务接口 401 = 登录态失效，清掉本地 token 并回登录页
    if (response.status === 401) {
      clearAuthStorage()
      redirectToLogin()
    }
    throw new ApiError(
      describeDetail(detail) || '请求没有成功，请稍后再试。',
      { status: response.status, detail },
    )
  }

  if (!response.body) {
    throw new ApiError('这个浏览器版本太旧，用不了边生成边显示的聊天，换一个新一点的浏览器再试。')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let chars = 0
  let finished = false

  const handle = (frame) => {
    const { event, data, broken } = parseFrame(frame)
    if (broken) throw new ApiError('回答传输出错了，这次生成已中止，请再问一次。')
    if (event === 'start') {
      if (onStart) onStart(data || {})
      return
    }
    if (event === 'delta') {
      const text = typeof data?.text === 'string' ? data.text : ''
      if (text) {
        chars += text.length
        if (onDelta) onDelta(text)
      }
      return
    }
    if (event === 'error') {
      throw new ApiError(data?.message || '模型生成失败，请稍后重试。', { status: 200, detail: data })
    }
    if (event === 'done') {
      finished = true
      if (onDone) onDone(data || {})
    }
  }

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let cut = buffer.indexOf('\n\n')
      while (cut !== -1) {
        const frame = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        handle(frame.replace(/\r/g, ''))
        cut = buffer.indexOf('\n\n')
      }
    }

    // 收尾：连接已断但没收到 done，属于中途断流，要如实告知而不是当成成功
    const tail = buffer.replace(/\r/g, '').trim()
    if (tail) handle(tail)
    if (!finished) {
      throw new ApiError(
        '回答在中途断开了（连接被切断）。已显示的内容是模型真实生成的部分，可点「重试」重新提问。',
      )
    }
  } finally {
    // 主动释放读取器（异常/取消路径下也释放，避免连接悬挂）
    reader.cancel().catch(() => {})
  }

  return chars
}
