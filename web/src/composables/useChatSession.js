/**
 * AI 助手的会话状态（**轻量、单会话**）。
 *
 * 边界，说清楚免得和求职规划的业务状态混淆：
 *   - 这里只有聊天消息，**不会**写画像、不会调 /api/analyze、不会碰 growth.json；
 *     规划的业务状态仍然只由 stores/analysis.js 与后端工作流负责。
 *   - 会话只有一路：没有多会话列表、没有后端聊天表。消息存在这台浏览器的
 *     localStorage（与画像同口径），刷新后仍在，点「清空会话」即彻底删除。
 *   - 状态是**模块级单例**：助手页切走再回来，消息、sending 和正在跑的请求
 *     都还在；整页刷新才会中断请求，但完整回合已经写入 localStorage。
 *   - 上下文长度有硬上限（见 HISTORY_LIMIT），发送前裁剪；后端还会二次裁剪。
 *
 * 状态机（每条 AI 消息）：
 *   streaming → done        正常收尾
 *   streaming → aborted     用户点了「停止生成」，已出的字保留
 *   streaming → error       请求失败 / 断流 / 模型报错，可「重试」
 */

import { computed, ref } from 'vue'

import { streamChat } from '../api/chatStream'
import {
  CHAT_SESSION_KEY, readJson, removeKey, writeJson,
} from '../utils/storage'

/** 与 backend/chat.py 的 CHAT_MAX_HISTORY_MESSAGES 同口径：只带最近 10 条历史。 */
export const HISTORY_LIMIT = 10
/** 本地只保留最近 40 条消息（不做历史归档，避免越存越慢）。 */
export const STORED_LIMIT = 40
/** 与 backend/chat.py 的 CHAT_MAX_INPUT_CHARS 一致：前端先拦一次，省一次往返。 */
export const INPUT_MAX_CHARS = 2000

let seq = 0

function nextId() {
  seq += 1
  return `msg-${Date.now()}-${seq}`
}

function loadStored() {
  const stored = readJson(CHAT_SESSION_KEY, null)
  if (!Array.isArray(stored)) return []
  return stored
    .filter(
      (item) =>
        item
        && (item.role === 'user' || item.role === 'assistant')
        && typeof item.text === 'string'
        && item.text,
    )
    .slice(-STORED_LIMIT)
    .map((item) => ({ id: nextId(), role: item.role, text: item.text, state: 'done', error: '' }))
}

function createSession() {
  const messages = ref(loadStored())
  const sending = ref(false)
  const inputError = ref('')
  /** 本轮请求整体失败的提示（网络、校验、模型不可达）。 */
  const requestError = ref('')

  const hasConversation = computed(() => messages.value.length > 0)

  function current(id) {
    return messages.value.find((item) => item.id === id) || null
  }

  /** 只把"已经完整结束"的消息交给模型，避免把半成品或错误文本喂回去。 */
  function buildHistory() {
    return messages.value
      .filter((item) => item.state === 'done' && item.text)
      .slice(-HISTORY_LIMIT)
      .map((item) => ({ role: item.role, content: item.text }))
  }

  /** 只落盘完整回合；streaming / error 的半成品不写本地。 */
  function persist() {
    const stable = messages.value.filter((item) => item.state === 'done' && item.text)
    // 回合的结尾必须是 AI 回答：提问成功但回答失败/被停止时，那条用户消息是孤零零的，
    // 留下它会让刷新后出现"有问题没有答案"的历史，也会被当成上下文带进下一轮。
    let end = stable.length - 1
    while (end >= 0 && stable[end].role !== 'assistant') end -= 1
    writeJson(
      CHAT_SESSION_KEY,
      stable.slice(0, end + 1).slice(-STORED_LIMIT).map(({ role, text }) => ({ role, text })),
    )
  }

  // 当前这轮流式请求的取消句柄；stop() 用它掐断 fetch。
  let controller = null

  async function runTurn(text, replyId) {
    sending.value = true
    controller = new AbortController()
    try {
      await streamChat({
        message: text,
        history: buildHistory(),
        signal: controller.signal,
        onDelta: (chunk) => {
          const reply = current(replyId)
          if (reply) reply.text += chunk
        },
      })
      const reply = current(replyId)
      if (reply) {
        // 一个字都没生成 = 模型空转，如实标错，不伪装成成功回答
        if (reply.text) {
          reply.state = 'done'
        } else {
          reply.state = 'error'
          reply.error = '模型没有返回任何内容，请重试或换个问法。'
        }
      }
    } catch (error) {
      const reply = current(replyId)
      // AbortError 只可能来自用户点「停止生成」或清空会话：这是主动停止，不是失败，
      // 一个都还没出来也要标成 aborted（已停止），不能谎报成出错。
      const aborted = error && error.name === 'AbortError'
      if (reply) {
        reply.state = aborted ? 'aborted' : 'error'
        reply.error = aborted
          ? (reply.text ? '' : '已在模型输出第一个字之前停止。')
          : (error?.message || '聊天请求失败，请稍后重试。')
      }
      requestError.value = aborted ? '' : (error?.message || '聊天请求失败，请稍后重试。')
    } finally {
      controller = null
      sending.value = false
      persist()
    }
  }

  /**
   * 发一轮对话。
   * @param {string} rawText 用户输入
   * @returns {Promise<boolean>} 是否真正发出了请求
   */
  async function send(rawText) {
    if (sending.value) return false
    const text = String(rawText ?? '').trim()

    if (!text) {
      inputError.value = '请输入想问的内容'
      return false
    }
    if (text.length > INPUT_MAX_CHARS) {
      inputError.value = `问题太长了（${text.length} 字），请控制在 ${INPUT_MAX_CHARS} 字以内`
      return false
    }

    inputError.value = ''
    requestError.value = ''

    messages.value.push({ id: nextId(), role: 'user', text, state: 'done', error: '' })
    const replyId = nextId()
    messages.value.push({ id: replyId, role: 'assistant', text: '', state: 'streaming', error: '' })

    // runTurn 里取的 history 只含 done 的消息，本轮提问与空的占位回答都不会混进历史
    await runTurn(text, replyId)
    return true
  }

  /** 真正的取消：abort 会让后端立即关闭与 Ollama 的上游连接。 */
  function stop() {
    controller?.abort()
  }

  /** 重试某条 AI 回答：删掉它自己（保留原提问），用同一条提问再请求一次。 */
  async function retry(id) {
    if (sending.value) return false
    const index = messages.value.findIndex((item) => item.id === id)
    if (index < 1) return false
    const question = [...messages.value.slice(0, index)].reverse()
      .find((item) => item.role === 'user')
    if (!question) return false

    messages.value.splice(index, 1)
    const replyId = nextId()
    messages.value.splice(index, 0, { id: replyId, role: 'assistant', text: '', state: 'streaming', error: '' })
    await runTurn(question.text, replyId)
    return true
  }

  /** 清空当前会话：中断进行中的请求，并删除本地存储。 */
  function clear() {
    stop()
    messages.value = []
    inputError.value = ''
    requestError.value = ''
    removeKey(CHAT_SESSION_KEY)
  }

  return {
    messages, sending, inputError, requestError, hasConversation, send, stop, retry, clear,
  }
}

// 页面卸载不能把会话一起带走：缓存唯一实例，切路由后仍由同一个 runTurn 收尾。
let session = null

export function useChatSession() {
  if (!session) session = createSession()
  return session
}
