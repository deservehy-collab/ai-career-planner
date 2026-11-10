/**
 * AI 助手会话：切路由不丢、刷新后仍在（P3 前端自动化）。
 *
 * `useChatSession` 故意是模块级单例：AssistantView 卸载后同一个 runTurn
 * 还要继续收字。这里用 `vi.resetModules()` 区分"换页面"（模块实例保留）
 * 与"整页刷新"（模块重载，只能靠 localStorage 恢复）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const streamChat = vi.fn()
vi.mock('../../api/chatStream', () => ({
  streamChat: (...args) => streamChat(...args),
}))

const CHAT_SESSION_KEY = 'career-agent:chat-session'

async function loadSession() {
  const module = await import('../useChatSession')
  return module.useChatSession()
}

async function reloadPage() {
  vi.resetModules()
  return loadSession()
}

function plainMessages(session) {
  return session.messages.value.map(({ role, text, state }) => ({ role, text, state }))
}

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
  streamChat.mockReset()
})

describe('useChatSession 跨路由与刷新', () => {
  it('重新挂载页面时拿到同一个会话，消息和进行中的请求都还在', async () => {
    streamChat.mockImplementation(async ({ onDelta }) => {
      onDelta('你好，')
      onDelta('我是职业规划助手。')
    })

    const session = await loadSession()
    await session.send('在吗？')

    // 模拟切走再切回 AssistantView：组件重新调用 useChatSession()
    const remounted = (await import('../useChatSession')).useChatSession()

    expect(remounted).toBe(session)
    expect(plainMessages(session)).toEqual([
      { role: 'user', text: '在吗？', state: 'done' },
      { role: 'assistant', text: '你好，我是职业规划助手。', state: 'done' },
    ])
  })

  it('整页刷新后从 localStorage 恢复完整回合', async () => {
    streamChat.mockImplementation(async ({ onDelta }) => onDelta('收到，我们开始。'))

    const session = await loadSession()
    await session.send('帮我规划会计岗位')

    const stored = JSON.parse(localStorage.getItem(CHAT_SESSION_KEY))
    expect(stored).toEqual([
      { role: 'user', text: '帮我规划会计岗位' },
      { role: 'assistant', text: '收到，我们开始。' },
    ])

    // 整页刷新 = 模块重新加载；localStorage 保留
    const reloaded = await reloadPage()
    expect(plainMessages(reloaded)).toEqual([
      { role: 'user', text: '帮我规划会计岗位', state: 'done' },
      { role: 'assistant', text: '收到，我们开始。', state: 'done' },
    ])
  })

  it('回答失败只保留完整回合，不把孤零零的提问写进本地历史', async () => {
    streamChat.mockRejectedValue(new Error('模型不可达'))

    const session = await loadSession()
    await session.send('这一轮会失败')

    expect(JSON.parse(localStorage.getItem(CHAT_SESSION_KEY))).toEqual([])
    expect(session.messages.value.map((item) => item.state)).toEqual(['done', 'error'])
  })

  it('清空会话会同时删除本地存储', async () => {
    streamChat.mockImplementation(async ({ onDelta }) => onDelta('好的'))
    const session = await loadSession()
    await session.send('一句话')

    session.clear()

    expect(session.messages.value).toEqual([])
    expect(localStorage.getItem(CHAT_SESSION_KEY)).toBeNull()
  })
})
