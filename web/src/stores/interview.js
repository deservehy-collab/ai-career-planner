/**
 * 面试陪练结果缓存。
 *
 * 出题一次要等几十秒，而 SPA 切页会卸载页面组件。结果和进行中的请求都放在
 * 这里，按「账号 + 档案」分开：切去别的页面再回来，题目还在，也不会重复出题。
 * 成功后同时写本机 localStorage，刷新后仍能恢复。
 *
 * 仍然只存本机，不写后端任何业务数据。
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'

import { generateInterviewQuestions } from '../api/endpoints'
import { readJson, writeJson } from '../utils/storage'
import { useAuthStore } from './auth'

const KEY_PREFIX = 'career-agent:interview'

export const useInterviewStore = defineStore('interview', () => {
  const auth = useAuthStore()

  const results = ref({})
  const pending = ref({})
  const errors = ref({})
  // key → promise：同一个档案正在出题时，切页回来不会发第二次请求
  const inflight = new Map()

  function scope(planId) {
    const userId = auth.user?.id
    if (!userId || !planId) return null
    return { key: `${KEY_PREFIX}:${userId}:${planId}` }
  }

  function hydrate(planId) {
    const s = scope(planId)
    if (!s || results.value[s.key] !== undefined) return
    const saved = readJson(s.key, null)
    if (saved && Array.isArray(saved.questions)) {
      results.value = { ...results.value, [s.key]: saved }
    }
  }

  function resultFor(planId) {
    const s = scope(planId)
    return s ? results.value[s.key] || null : null
  }

  function isPending(planId) {
    const s = scope(planId)
    return s ? !!pending.value[s.key] : false
  }

  function errorFor(planId) {
    const s = scope(planId)
    return s ? errors.value[s.key] || '' : ''
  }

  function generate(planId, resume) {
    const s = scope(planId)
    if (!s) return Promise.resolve(null)
    if (inflight.has(s.key)) return inflight.get(s.key)

    pending.value = { ...pending.value, [s.key]: true }
    errors.value = { ...errors.value, [s.key]: '' }

    const task = (async () => {
      try {
        const data = await generateInterviewQuestions({ plan_id: planId, resume })
        if (Array.isArray(data?.questions) && data.questions.length) {
          const payload = { ...data, savedAt: new Date().toISOString() }
          results.value = { ...results.value, [s.key]: payload }
          writeJson(s.key, payload)
        }
        return data
      } catch (exc) {
        errors.value = {
          ...errors.value,
          [s.key]: exc?.message || '出题失败了，请稍后再试。',
        }
        return null
      } finally {
        pending.value = { ...pending.value, [s.key]: false }
        inflight.delete(s.key)
      }
    })()

    inflight.set(s.key, task)
    return task
  }

  return { results, pending, errors, hydrate, resultFor, isPending, errorFor, generate }
})
