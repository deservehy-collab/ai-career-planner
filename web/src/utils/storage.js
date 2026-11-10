/**
 * localStorage 读写小工具。
 *
 * 画像**不做后端持久化**（后端没有画像接口），由前端保存、每次请求携带。
 * 两个键分开存，语义不同、不要混：
 *   career-agent:profile      —— 用户自述：major / grade / skills / projects /
 *                                targetJob，外加 jdText（本次粘贴的招聘要求原文）
 *   career-agent:last-skills  —— 最近一次分析返回的**结构化能力表** [{skill, level, evidence}]
 *
 * 后者是提交任务完成时 `profile_skills` 的来源（让"升级预览"与下一轮真实判定同一基线）。
 * 前者的 jdText 会随 `next-round` 一起回传，好让下一轮沿用同一份岗位要求，不必重粘。
 */

export const PROFILE_KEY = 'career-agent:profile'
export const LAST_SKILLS_KEY = 'career-agent:last-skills'
export const TOKEN_KEY = 'career-agent:auth-token'
export const USER_KEY = 'career-agent:auth-user'
/**
 * AI 助手的**当前会话**（单会话、非历史归档）：
 * 与画像同口径 —— 后端没有聊天表，只存在这台浏览器里，可随时清空。
 */
export const CHAT_SESSION_KEY = 'career-agent:chat-session'

/** localStorage 不可用时（隐私模式 / 被禁用）也不能让页面崩掉。 */
function safeStorage() {
  try {
    const probe = '__career_agent_probe__'
    window.localStorage.setItem(probe, '1')
    window.localStorage.removeItem(probe)
    return window.localStorage
  } catch {
    return null
  }
}

export function readJson(key, fallback) {
  const storage = safeStorage()
  if (!storage) return fallback
  try {
    const raw = storage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return parsed ?? fallback
  } catch {
    // 存的内容坏了就当没有 —— 不删用户数据，只是本次不用
    return fallback
  }
}

/**
 * 写入 JSON。
 * @returns {boolean} 是否写入成功（配额满 / 被禁用时返回 false，由调用方决定是否提示）
 */
export function writeJson(key, value) {
  const storage = safeStorage()
  if (!storage) return false
  try {
    storage.setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export function removeKey(key) {
  const storage = safeStorage()
  if (!storage) return
  try {
    storage.removeItem(key)
  } catch {
    /* 忽略 */
  }
}

// ── 认证：token 与登录用户 ────────────────────────────────────────────
// token 是裸字符串（不加 JSON 引号），方便 client.js 在 pinia 初始化之前
// 也能直接读取并附加到请求头。

export function getAuthToken() {
  const storage = safeStorage()
  if (!storage) return null
  try {
    return storage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setAuthToken(token) {
  const storage = safeStorage()
  if (!storage) return false
  try {
    storage.setItem(TOKEN_KEY, token)
    return true
  } catch {
    return false
  }
}

export const getStoredUser = () => readJson(USER_KEY, null)
export const setStoredUser = (user) => writeJson(USER_KEY, user)

export function clearAuthStorage() {
  removeKey(TOKEN_KEY)
  removeKey(USER_KEY)
}

/** 把多行 / 顿号 / 逗号分隔的文本解析成去重后的字符串数组。 */
export function parseList(text) {
  if (Array.isArray(text)) return text.map((s) => String(s).trim()).filter(Boolean)
  return String(text || '')
    .split(/[\n,，、;；]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 数组 → 便于编辑的多行文本。 */
export function formatList(list) {
  return Array.isArray(list) ? list.join('\n') : ''
}
