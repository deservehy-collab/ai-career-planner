/**
 * 求职档案上下文：账号下的档案列表、当前档案与切换代次。
 *
 * 所有业务页面都从 `activeId` 取档案身份；切换时 `epoch + 1`，页面在
 * 每个异步回调里同时核对账号、档案与代次，迟到的旧响应不会覆盖新档案。
 *
 * 当前选择按账号分别保存在本地：退出后换账号不会继承上一账号的档案。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  createPlan as createPlanRequest,
  deletePlan as deletePlanRequest,
  getAccountProfile,
  listPlans,
  saveAccountProfile,
} from '../api/endpoints'
import { readJson, removeKey, writeJson } from '../utils/storage'
import { useAuthStore } from './auth'

const activeKey = (userId) => `career-agent:active-plan:${userId}`

function newRequestKey() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return `plan-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export const usePlansStore = defineStore('plans', () => {
  const auth = useAuthStore()

  const items = ref([])
  const total = ref(0)
  const loading = ref(false)
  const error = ref('')
  const activeId = ref(null)
  const epoch = ref(0)
  const profile = ref(null)
  const profileVersion = ref(0)

  const active = computed(
    () => items.value.find((item) => item.id === activeId.value) || null,
  )
  const hasPlans = computed(() => items.value.length > 0)
  const activeTitle = computed(() => active.value?.title || active.value?.target_job || '')

  function restoreActive() {
    const uid = auth.user?.id
    if (!uid) return
    const saved = readJson(activeKey(uid), null)
    if (typeof saved === 'string' && items.value.some((item) => item.id === saved)) {
      activeId.value = saved
    }
  }

  function persistActive() {
    const uid = auth.user?.id
    if (!uid) return
    if (activeId.value) writeJson(activeKey(uid), activeId.value)
    else removeKey(activeKey(uid))
  }

  function select(id) {
    const next = id && items.value.some((item) => item.id === id) ? id : null
    // A→B→A 也要换代：只比较 id 无法识别"切走了又切回来"的迟到响应。
    epoch.value += 1
    activeId.value = next
    persistActive()
  }

  async function load() {
    if (!auth.user?.id) return []
    loading.value = true
    error.value = ''
    try {
      const data = await listPlans({ limit: 100 })
      items.value = Array.isArray(data.items) ? data.items : []
      total.value = Number(data.total || items.value.length)
      if (!items.value.some((item) => item.id === activeId.value)) {
        activeId.value = null
        restoreActive()
      }
      persistActive()
      return items.value
    } catch (exc) {
      error.value = exc?.message || '档案列表没有加载出来'
      return []
    } finally {
      loading.value = false
    }
  }

  async function create(form) {
    const created = await createPlanRequest({
      request_key: newRequestKey(),
      title: form.title || form.targetJob || form.target_job || '',
      target_job: form.targetJob || form.target_job || '',
      jd_text: form.jdText || form.jd_text || '',
      user_profile: form.user_profile || form.profile || {
        major: '', grade: '', skills: [], projects: [],
      },
    })
    await load()
    select(created.plan_id)
    return created
  }

  async function remove(id) {
    await deletePlanRequest(id)
    const wasActive = id === activeId.value
    await load()
    if (wasActive) select(items.value[0]?.id || null)
    else epoch.value += 1
    return { wasActive, nextId: activeId.value }
  }

  async function loadProfile() {
    const data = await getAccountProfile()
    profile.value = data
    profileVersion.value = Number(data.version || 0)
    return data
  }

  async function saveProfile(form) {
    const data = await saveAccountProfile({
      major: form.major || '',
      grade: form.grade || '',
      skills: form.skills || [],
      projects: form.projects || [],
      version: profileVersion.value,
    })
    profile.value = data
    profileVersion.value = Number(data.version || 0)
    return data
  }

  function reset() {
    epoch.value += 1
    items.value = []
    total.value = 0
    activeId.value = null
    profile.value = null
    profileVersion.value = 0
    error.value = ''
  }

  return {
    items,
    total,
    loading,
    error,
    activeId,
    epoch,
    active,
    activeTitle,
    hasPlans,
    profile,
    profileVersion,
    load,
    loadProfile,
    saveProfile,
    select,
    create,
    remove,
    reset,
  }
})
