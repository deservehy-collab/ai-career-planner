<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppBadge from '../components/AppBadge.vue'
import CompletionResult from '../components/CompletionResult.vue'
import SectionNav from '../components/SectionNav.vue'

import {
  attachTaskEvidence,
  clearTasks,
  completeTask,
  getPending,
  listTasks,
} from '../api/endpoints'
import { usePlansStore } from '../stores/plans'
import {
  goalStatusText,
  requirementBasisText,
  requirementBasisTone,
  retrievalStatusText,
  storeStatusText,
  taskKindText,
} from '../utils/enums'
import { formatEvidence, joinList } from '../utils/format'

/**
 * 学习与项目页。
 *
 * 数据来自 `GET /api/tasks`：
 *   rounds[]        —— 全部轮次的摘要（只有计数）
 *   latest_round    —— **最新一轮的完整对象**，含 learning_plan / project_plan 明细
 *   tasks[]         —— 按 round_no 过滤后的任务清单
 *
 * ⚠️ 计划明细（阶段目标 / 完成标准 / 交付物）**只有 latest_round 里才有**；
 *    旧轮次在 rounds[] 中只有计数，后端也没有按轮次取计划的接口。
 *    所以旧轮次如实标注"计划明细仅最新一轮可查"，不做任何补全或猜测。
 *
 * 「清空任务清单」走 `POST /api/tasks/clear`：**先备份再重置**，只动任务清单，
 * 成长历史一行不碰。但打卡攒下、还没进成长历史的成果是挂在清单里的，
 * 会跟着一起消失 —— 所以弹层先查一次未记账条数摆给用户看，勾一下才放行。
 */
/** 子页由路由传进来：任务清单 / 学习路线 / 实战项目各自有独立地址。 */
const props = defineProps({
  tab: { type: String, default: 'tasks' },
})

const plans = usePlansStore()

// 与 backend/config.py 的 EVIDENCE_MAX_LENGTH 一致（前端无法读取后端配置，此处同步维护）
const EVIDENCE_MAX_LENGTH = 5000
// 可选心得笔记的上限，与 backend/config.py 的 NOTE_MAX_CHARS 一致
const NOTE_MAX_CHARS = 1000

const loading = ref(false)
const loadError = ref('')
const data = ref(null)

const selectedRound = ref(null)
const activeTab = computed(() => props.tab)
const pageTitle = computed(
  () => ({ tasks: '任务清单', learning: '学习路线', projects: '实战项目' }[props.tab] || '任务清单'),
)

const completingTask = ref(null)
/** `complete` = 完成任务（凭证/心得都选填）；`attach` = 给已自述完成的任务补凭证。 */
const sheetMode = ref('complete')
const evidenceText = ref('')
const evidenceError = ref('')
/** 可选心得：写不写都能完成；写了单独存，不冒充凭证。 */
const noteText = ref('')
const submitting = ref(false)
const submitError = ref('')
/** 一键完成不走弹层，错误必须落在清单页上，否则失败看起来就是"没反应"。 */
const directError = ref('')
const submitResult = ref(null)
const attachResult = ref(null)
/** 正在播放「咻」离场动画的任务；动画结束才重新拉清单。 */
const leavingTaskId = ref('')
/** 与 `.task-row.is-leaving` 的动画时长保持一致（400ms）。 */
const LEAVE_MS = 400
let leaveTimer = null

// ── 清空任务清单 ───────────────────────────────────────────────────────
const clearing = ref(false)
const clearBusy = ref(false)
const clearError = ref('')
const clearResult = ref(null)
const dropPending = ref(false)
/** 打开清空弹层时锁定的档案；切换档案后必须放弃这次清空，不能落到新档案上。 */
const clearPlanId = ref(null)
/** `null` = 还没查到；数字 = 打卡攒下、还没进成长历史的成果条数。 */
const pendingCount = ref(null)

const rounds = computed(() => data.value?.rounds || [])
const latestRound = computed(() => data.value?.latest_round || null)
/** 持久化结局：刷新页面后仍然知道当前计划是"已达成 / 依据不足 / 进行中"。 */
const latestGoalRecord = computed(() => data.value?.latest_goal || null)
const latestGoal = computed(() => latestGoalRecord.value?.goal || null)
/** 上次生成下一轮没存下来的恢复标记（只读提示，不在这里操作）。 */
const recovery = computed(() => data.value?.pending_next_round || null)
const latestRoundNo = computed(() => latestRound.value?.round ?? null)
const tasks = computed(() => data.value?.tasks || [])
const summary = computed(() => data.value?.summary || { total: 0, completed: 0, pending: 0 })

/** 存储不可写时禁止提交 —— 否则会做出"看起来提交成功、其实没落盘"的假象。 */
const readOnly = computed(() => data.value != null && data.value.writable === false)

const visibleTasks = computed(() =>
  selectedRound.value == null ? tasks.value : tasks.value.filter((t) => t.round === selectedRound.value),
)
const visibleDone = computed(() => visibleTasks.value.filter((t) => t.status === 'completed').length)
const visiblePending = computed(() => visibleTasks.value.length - visibleDone.value)

/** 左栏进度条：没有任务时按 0%，不让 0/0 变成 NaN。 */
const progressPct = computed(() => {
  const total = visibleTasks.value.length
  return total ? Math.round((visibleDone.value / total) * 100) : 0
})

/** 编号跟着整份清单走，筛到某一轮时也还是原来那个号，不对不上。 */
const taskNumbers = computed(() => {
  const map = new Map()
  tasks.value.forEach((task, index) => map.set(task.task_id, index + 1))
  return map
})

function taskNo(task) {
  return String(taskNumbers.value.get(task?.task_id) ?? '—').padStart(2, '0')
}

/**
 * 任务记录本身不存难度；只有最新一轮的计划明细里才有项目难度 / 学习预计周数。
 * 旧轮次没有明细，取不到就整段不显示，不按标题猜一个。
 */
const taskLoadHints = computed(() => {
  const round = latestRound.value
  const map = new Map()
  if (!round) return map
  for (const task of tasks.value) {
    if (task.round !== round.round) continue
    const plan = (task.kind === 'project' ? round.project_plan : round.learning_plan) || []
    const item = plan[task.index - 1]
    if (!item) continue
    const text = task.kind === 'project'
      ? String(item.difficulty || '').trim()
      : (item.estimated_weeks ? `约 ${item.estimated_weeks} 周` : '')
    if (text) map.set(task.task_id, text)
  }
  return map
})

function taskLoad(task) {
  return taskLoadHints.value.get(task?.task_id) || ''
}

/** 按轮次分组：全部轮次视图里，组头负责交代每一段属于哪一轮。 */
const groupedTasks = computed(() => {
  const groups = []
  const seen = new Map()
  for (const task of visibleTasks.value) {
    let group = seen.get(task.round)
    if (!group) {
      group = { round: task.round, tasks: [], done: 0 }
      seen.set(task.round, group)
      groups.push(group)
    }
    group.tasks.push(task)
    if (task.status === 'completed') group.done += 1
  }
  return groups
})

/** 书签墙只放待办；已完成的收进下方折叠册，但记录与补凭证入口都还在。 */
function pendingOf(group) {
  return group.tasks.filter((task) => task.status !== 'completed')
}

function doneOf(group) {
  return group.tasks.filter((task) => task.status === 'completed')
}

const currentRound = computed(() => {
  const roundNo = selectedRound.value ?? latestRoundNo.value
  return rounds.value.find((round) => round.round === roundNo) || null
})
const targetJob = computed(() => currentRound.value?.target_job || '未记录')

const listStatusText = computed(() => {
  if (readOnly.value) return '只读 · 暂时不能提交'
  if (data.value?.store_status === 'ok') return '已保存 · 可以提交'
  return storeStatusText(data.value?.store_status)
})

const runningStatus = computed(() => {
  if (readOnly.value) return '只读'
  if (visiblePending.value) return '执行中'
  return visibleTasks.value.length ? '已完成' : '暂无任务'
})

/** 只有选中最新一轮（或未筛选）时才有计划明细可展示。 */
const planDetailRound = computed(() => {
  if (selectedRound.value == null) return latestRound.value
  return selectedRound.value === latestRoundNo.value ? latestRound.value : null
})

const learningPlan = computed(() => planDetailRound.value?.learning_plan || [])
const projectPlan = computed(() => planDetailRound.value?.project_plan || [])

/** 学习路线总周数：与后端 total_weeks 同口径，非整数周按 0 计。 */
const learningWeeks = computed(() =>
  learningPlan.value.reduce((total, stage) => {
    const weeks = Number(stage?.estimated_weeks)
    return total + (Number.isInteger(weeks) ? weeks : 0)
  }, 0),
)

/** 阶段 / 项目卡片顶部的汇总：按 key 数不重复的条目。 */
function uniqueCount(items, key) {
  const seen = new Set()
  for (const item of items) {
    for (const value of item?.[key] || []) {
      const text = String(value ?? '').trim()
      if (text) seen.add(text)
    }
  }
  return seen.size
}

const learningGapCount = computed(() => uniqueCount(learningPlan.value, 'covers'))
const projectTechCount = computed(() => uniqueCount(projectPlan.value, 'tech_stack'))
const projectGapCount = computed(() => uniqueCount(projectPlan.value, 'covers_gaps'))

const tabItems = computed(() => [
  { key: 'tasks', to: '/tasks', label: '任务清单', count: summary.value.total || 0 },
  { key: 'learning', to: '/tasks/learning', label: '学习路线', count: learningPlan.value.length || 0 },
  { key: 'projects', to: '/tasks/projects', label: '实战项目', count: projectPlan.value.length || 0 },
])

/**
 * 这一轮绑死的依据快照（loop.json 里的 snapshot）。
 * 「生成下一轮」只认它，不让模型重新推断 —— 所以这里也得让用户看见它是什么：
 * 有没有原文、几条要求、依据是哪一档。快照缺失（较早的轮次）时整块不出现。
 */
const roundSnapshot = computed(() => planDetailRound.value?.snapshot || null)
const snapshotBasis = computed(() => roundSnapshot.value?.job_requirements_basis || '')
const snapshotRequirementCount = computed(
  () => (roundSnapshot.value?.job_requirements || []).length,
)
const snapshotEvidenceCount = computed(() => (roundSnapshot.value?.job_evidence || []).length)
// 依据是"你粘的原话"还是"内置资料摘要"，读后端存的 retrieval_status，
// 前端不按 provenance 自己数一遍（两处判定必然漂移）。
const snapshotEvidenceStatus = computed(() => roundSnapshot.value?.retrieval_status || '')
const snapshotJdText = computed(() => String(roundSnapshot.value?.jd_text || '').trim())

/**
 * 「做到什么程度算完成」不在任务记录里 —— 任务只是可勾选的一行，
 * 文字原样躺在轮次的计划明细中，这里按 轮次 + 类型 + 序号 回填给用户照着写。
 * 旧轮次只有计数、没有明细，取不到就不显示这一行，绝不拿标题凑一句标准出来。
 */
const completingCriteria = computed(() => {
  const task = completingTask.value
  const round = latestRound.value
  if (!task || !round || round.round !== task.round) return null
  const isProject = task.kind === 'project'
  const plan = (isProject ? round.project_plan : round.learning_plan) || []
  const text = String((isProject ? plan[task.index - 1]?.deliverable
    : plan[task.index - 1]?.completion_criteria) || '').trim()
  return text ? { label: isProject ? '最终成果' : '完成标准', text } : null
})

/**
 * 清空要不要先勾一下：
 * 打卡攒下的成果还挂在任务清单里，要等「生成下一轮」才正式进成长历史 ——
 * 清空会把这批**还没记账**的进度一起带走，所以必须显式确认。
 * 条数查不到时按"不用勾"处理：后端还会再拦一次，不会偷偷丢。
 */
const clearNeedsAck = computed(() => (pendingCount.value ?? 0) > 0)
const clearBlocked = computed(() => clearNeedsAck.value && !dropPending.value)

async function load() {
  const captured = { planId: plans.activeId, epoch: plans.epoch }
  if (!captured.planId) {
    data.value = null
    loadError.value = ''
    loading.value = false
    return
  }
  loading.value = true
  loadError.value = ''
  directError.value = ''
  try {
    const result = await listTasks({}, {}, captured.planId)
    if (plans.activeId !== captured.planId || plans.epoch !== captured.epoch) return
    data.value = result
  } catch (error) {
    if (plans.activeId !== captured.planId || plans.epoch !== captured.epoch) return
    data.value = null
    loadError.value = error?.message || '加载任务失败'
  } finally {
    if (plans.activeId === captured.planId && plans.epoch === captured.epoch) {
      loading.value = false
    }
  }
}

onMounted(load)
watch(() => [plans.activeId, plans.epoch], load)

function pad2(index) {
  return String(index + 1).padStart(2, '0')
}

/** 行内日期只到"几月几日"；解析失败就不显示，不假装成功。 */
function shortDate(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`
}

/** 弹层/抽屉打开前所在的元素：关闭后把焦点还回去，键盘用户不会掉回页首。 */
let lastFocused = null

/** 历史任务没有 evidence_kind：有凭证按凭证算，没有就是自述完成。 */
function taskEvidenceKind(task) {
  if (task?.evidence_kind) return task.evidence_kind
  return task?.evidence ? 'growth' : ''
}

const needsProof = (task) =>
  task?.status === 'completed' && taskEvidenceKind(task) !== 'growth'

function openSheet(task, mode) {
  lastFocused = document.activeElement
  sheetMode.value = mode
  completingTask.value = task
  evidenceText.value = ''
  evidenceError.value = ''
  noteText.value = ''
  submitError.value = ''
  const focusId = mode === 'attach' ? 'tv-attach-evidence' : 'tv-evidence'
  nextTick(() => document.getElementById(focusId)?.focus())
}

function openComplete(task) {
  openSheet(task, 'complete')
}

function openAttach(task) {
  openSheet(task, 'attach')
}

function restoreFocus() {
  nextTick(() => lastFocused?.focus?.())
  lastFocused = null
}

function closeComplete() {
  completingTask.value = null
  sheetMode.value = 'complete'
  evidenceText.value = ''
  evidenceError.value = ''
  noteText.value = ''
  submitError.value = ''
  restoreFocus()
}

/** 与后端 StringConstraints(strip_whitespace, max_length=5000) 对齐。
 *  完成任务时凭证可选；补凭证时才要求非空。 */
function validateEvidence(text, { required = false } = {}) {
  const trimmed = String(text ?? '').trim()
  if (!trimmed) {
    return required ? '补凭证要先写下凭证是什么：链接、截图位置或产出都行。' : ''
  }
  if (trimmed.length > EVIDENCE_MAX_LENGTH) {
    return `证据过长（${trimmed.length} 字），最多 ${EVIDENCE_MAX_LENGTH} 字。`
  }
  return ''
}

/** 附言校验：与后端 CompleteTaskRequest 的 note / 合计上限对齐。 */
function validateExtras() {
  const note = noteText.value.trim()
  if (note.length > NOTE_MAX_CHARS) return `心得笔记过长（${note.length} 字），最多 ${NOTE_MAX_CHARS} 字。`
  const total = evidenceText.value.trim().length + note.length
  if (total > EVIDENCE_MAX_LENGTH) {
    return `证据与心得笔记合计过长（${total} 字），最多 ${EVIDENCE_MAX_LENGTH} 字。`
  }
  return ''
}

function currentPlanSkills() {
  // 用**当前档案**的能力表当基线，不能拿本机缓存里其他岗位的分析结果；
  // 没做过分析时没有这张表，此时后端会退回 history_only 基线并如实提示。
  return plans.active?.current_skills || []
}

async function showCompletion(result) {
  submitResult.value = result
  attachResult.value = null
  completingTask.value = null
  evidenceText.value = ''
  noteText.value = ''
  // 提交行的按钮会随任务变成"已完成"而消失，焦点不能再还给它。
  lastFocused = null
  await load()
  // 结果改在独立抽屉里打开。焦点送进去，但不滚页面 ——
  // 之前把结果块滚进视野，会把站在清单中段的用户整个拽回页首。
  nextTick(() => document.getElementById('tv-result')?.focus({ preventScroll: true }))
}

/** 结果抽屉的书眉：这次到底是提交完成，还是补凭证。 */
const resultEyebrow = computed(() => {
  if (attachResult.value) return '补凭证 · 已经记到任务上'
  const task = submitResult.value?.task || {}
  const round = task.round != null ? `第 ${task.round} 轮` : '任务提交'
  return task.title ? `${round} · ${task.title}` : round
})

/** 收起结果：焦点回到当前页签，页面停在哪里就还在哪里。 */
function closeResult() {
  submitResult.value = null
  attachResult.value = null
  nextTick(() => {
    document.querySelector('.section-nav__link.is-active')?.focus?.({ preventScroll: true })
  })
}

/** 系统开了「减少动态效果」时不做离场等待，避免为了动画拖慢操作。 */
function prefersReducedMotion() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * 完成落库后先给这一行挂上 `is-leaving`，让它缩小、淡出、向左滑走，
 * 动画结束再刷新清单 —— 否则列表一刷新，卡片会瞬间消失，看不出发生了什么。
 */
function playLeave(taskId) {
  if (!taskId || prefersReducedMotion()) return Promise.resolve()
  leavingTaskId.value = taskId
  return new Promise((resolve) => {
    leaveTimer = window.setTimeout(() => {
      leaveTimer = null
      leavingTaskId.value = ''
      resolve()
    }, LEAVE_MS)
  })
}

// 动画没播完就切页时把计时器收掉，避免回来时凭空多拉一次清单。
onBeforeUnmount(() => {
  if (leaveTimer === null) return
  window.clearTimeout(leaveTimer)
  leaveTimer = null
})

/** 一键完成：不填凭证、不写心得，直接记一条自述完成。 */
async function completeTaskDirect(task) {
  if (!task || submitting.value || readOnly.value) return
  const planId = plans.activeId
  if (!planId) {
    directError.value = '还没有选择求职档案。请先到「我的档案」选择或新建一份。'
    return
  }

  submitting.value = true
  directError.value = ''
  const body = {}
  const planSkills = currentPlanSkills()
  if (planSkills.length) body.profile_skills = planSkills

  try {
    const result = await completeTask(task.task_id, body, planId)
    await playLeave(task.task_id)
    await showCompletion(result)
  } catch (error) {
    directError.value = error?.message || '提交失败'
  } finally {
    submitting.value = false
  }
}

async function submit() {
  if (!completingTask.value || submitting.value) return

  const mode = sheetMode.value
  const problem = (mode === 'attach'
    ? validateEvidence(evidenceText.value, { required: true })
    : validateEvidence(evidenceText.value) || validateExtras())
  evidenceError.value = problem
  if (problem) return

  const planId = plans.activeId
  if (!planId) {
    submitError.value = '还没有选择求职档案。请先到「我的档案」选择或新建一份。'
    return
  }

  submitting.value = true
  submitError.value = ''

  const taskId = completingTask.value.task_id
  const evidence = evidenceText.value.trim()

  try {
    if (mode === 'attach') {
      attachResult.value = await attachTaskEvidence(taskId, { evidence }, planId)
      submitResult.value = null
      completingTask.value = null
      evidenceText.value = ''
      lastFocused = null
      await load()
      nextTick(() => document.getElementById('tv-attach-result')?.focus({ preventScroll: true }))
      return
    }

    const body = {}
    if (evidence) body.evidence = evidence
    const planSkills = currentPlanSkills()
    if (planSkills.length) body.profile_skills = planSkills
    // 心得单独存，不并进凭证 —— 后方记录里两栏分得清。
    const note = noteText.value.trim()
    if (note) body.note = note

    const result = await completeTask(taskId, body, planId)
    // 先收起抽屉，让清单里的那一行露出来，再播离场动画。
    completingTask.value = null
    await playLeave(taskId)
    await showCompletion(result)
  } catch (error) {
    submitError.value = error?.message || '提交失败'
  } finally {
    submitting.value = false
  }
}

// ── 清空任务清单 ───────────────────────────────────────────────────────

/** 打开弹层时先查一次未记账成果，让用户在点下去之前就看到会带走几条真实进度。 */
async function openClear() {
  const planId = plans.activeId
  if (!planId) return
  lastFocused = document.activeElement
  clearing.value = true
  clearPlanId.value = planId
  clearError.value = ''
  dropPending.value = false
  pendingCount.value = null
  try {
    const pending = await getPending({}, planId)
    if (plans.activeId !== planId) return
    pendingCount.value = (pending.events || []).length
  } catch {
    pendingCount.value = null
  }
}

function closeClear() {
  clearing.value = false
  clearPlanId.value = null
  clearError.value = ''
  dropPending.value = false
  restoreFocus()
}

async function confirmClear() {
  if (clearBusy.value || clearBlocked.value) return
  const planId = clearPlanId.value
  if (!planId || plans.activeId !== planId) {
    clearError.value = '档案已经切换，这次清空没有执行。请在当前档案里重新打开清空。'
    return
  }

  clearBusy.value = true
  clearError.value = ''
  try {
    const result = await clearTasks(dropPending.value, planId)
    if (result.status === 'cleared') {
      clearing.value = false
      clearResult.value = result
      dropPending.value = false
      pendingCount.value = 0
      selectedRound.value = null
      restoreFocus()
      await load()
      return
    }
    if (result.status === 'needs_confirmation') {
      // 后端比前端更清楚有哪些成果没记账，以它的条数为准，勾上再重来一次。
      pendingCount.value = result.pending_events ?? 0
      clearError.value = `有 ${result.pending_events} 条成果还没正式记进成长记录，清空会把它们一起丢掉。`
      return
    }
    clearError.value = result.warning || '任务清单没能清空，已有记录一条都没动。'
  } catch (error) {
    clearError.value = error?.message || '清空失败'
  } finally {
    clearBusy.value = false
  }
}
</script>

<template>
  <div>
    <div v-if="!plans.activeId" class="notice notice-warn">
      <strong>还没有选择求职档案</strong>
      <div class="notice-body">请先到「我的档案」选择或新建一份。</div>
      <div class="btn-row"><router-link class="btn btn-sm" to="/home">去我的档案</router-link></div>
    </div>
    <div v-else class="faint">当前档案：{{ plans.active?.title }}</div>

    <!-- 加载 / 失败 -->
    <div v-if="loading && !data" class="card">
      <div class="card-body"><div class="empty">正在加载任务…</div></div>
    </div>

    <div v-else-if="loadError" class="notice notice-danger">
      <strong>加载任务失败</strong>
      <div class="notice-body">{{ loadError }}</div>
      <div class="btn-row"><button class="btn" @click="load">重试</button></div>
    </div>

    <article v-else-if="data" class="reg">
      <header>
        <div class="reg-running">
          <span class="reg-running__right">
            当前 <b>{{ selectedRound == null ? '全部轮次' : `第 ${selectedRound} 轮` }}</b>
            · {{ runningStatus }}
          </span>
        </div>

        <div class="pg-mast pg-mast--sub">
          <div>
            <nav class="pg-crumb" aria-label="页面位置">
              <RouterLink to="/tasks">学习与项目</RouterLink>
              <span aria-hidden="true">/</span>
              <span>{{ pageTitle }}</span>
            </nav>
            <h1 class="pg-title">{{ pageTitle }}</h1>
          </div>
        </div>
      </header>

      <!-- 一键完成失败：没有弹层可挂错误，直接落在清单上 -->
      <div v-if="directError" class="notice notice-danger" role="alert">
        <strong>这条任务没有记成完成</strong>
        <div class="notice-body">{{ directError }}</div>
      </div>

      <!-- 存储只读 / 异常 -->
      <div v-if="!data.writable" class="notice notice-warn">
        <strong>任务清单现在不能安全保存</strong>
        <div class="notice-body">
          {{ data.warning || '任务清单暂时读不出来（可能已损坏或没有保存权限）。为避免弄丢已有记录，提交功能先停用；具体原因见「系统状态」页。' }}
        </div>
      </div>

      <div v-else-if="data.store_status !== 'ok' && data.store_status !== 'empty'" class="notice notice-warn">
        任务清单这次没能正常读取：{{ storeStatusText(data.store_status) }}。
      </div>

      <!-- 持久化结局：刷新后仍然看得见，不要求重跑一次分析 -->
      <div
        v-if="latestGoal?.status === 'achieved'"
        class="notice notice-ok"
      >
        <strong>{{ goalStatusText(latestGoal.status) }} · {{ latestGoalRecord.target_job || targetJob }}</strong>
        <div class="notice-body">
          {{ latestGoal.message }}
          要开始新的求职目标，去「岗位分析」页换目标岗位并点「开始新计划」，旧轮次会原样保留。
        </div>
      </div>
      <div
        v-else-if="latestGoal?.status === 'basis_insufficient'"
        class="notice notice-warn"
      >
        <strong>{{ goalStatusText(latestGoal.status) }} · {{ latestGoalRecord.target_job || targetJob }}</strong>
        <div class="notice-body">{{ latestGoal.message }}</div>
      </div>

      <!-- 恢复标记：计划已经算好但没落盘，去成长记录页一键补存 -->
      <div v-if="recovery" class="notice notice-warn">
        <strong>{{ recovery.has_plans ? '有一份算好但没保存的计划' : '上次生成在保存前中断' }}</strong>
        <div class="notice-body">
          <template v-if="recovery.has_plans">
            这份计划
            <template v-if="recovery.source_round">基于第 {{ recovery.source_round }} 轮</template>
            已经算好，但没能存进任务清单。去
            <router-link to="/growth">成长记录</router-link>
            页点「补存上次计划」即可，不会重新跑 AI。
          </template>
          <template v-else>
            去 <router-link to="/growth">成长记录</router-link> 页再点一次「生成下一轮」即可重试；
            已经入账的成果不会重复升级。
          </template>
        </div>
      </div>

      <!-- 清空结果 -->
      <div v-if="clearResult" class="notice notice-ok">
        <strong>任务清单已清空</strong>
        <div class="notice-body">
          删掉了 {{ clearResult.rounds_removed }} 轮、共 {{ clearResult.tasks_removed }} 项任务；
          <strong>成长记录没有改动</strong>，已经正式记下来的成长一条都不会少。
          <template v-if="clearResult.backup">
            清空前的原清单已经备份到这台电脑的数据文件夹里，需要时可以找回。
          </template>
          <template v-if="clearResult.pending_events">
            另外，{{ clearResult.pending_events }} 条还没记账的成果按你的确认一起丢弃了。
          </template>
        </div>
        <div class="btn-row btn-row--block">
          <button class="btn" @click="clearResult = null">收起清空结果</button>
        </div>
      </div>

      <!-- 空态 -->
      <section v-if="!rounds.length" class="reg-empty">
        <h2 class="reg-empty-title">还没有任何轮次</h2>
        <p>
          还没有任务。做一次<router-link to="/analyze">岗位分析</router-link>，它给出的学习阶段与实战项目
          会自动存成第 1 轮；之后的每一轮，在<router-link to="/growth">成长记录</router-link>页点「生成下一轮」创建。
        </p>
      </section>

      <template v-else>
        <SectionNav :items="tabItems" :active="activeTab" label="学习与项目子页面" />

        <template v-if="activeTab === 'tasks'">
        <div class="pg-body">
          <aside class="pg-index" aria-label="进度与轮次">
            <div class="reg-figure-label">执行进度</div>
            <div class="reg-figure">
              <span>{{ visibleDone }}</span><i>/</i><span>{{ visibleTasks.length }}</span>
              <small>项已完成</small>
            </div>
            <div
              class="task-progress"
              role="progressbar"
              :aria-valuenow="progressPct"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="`任务完成进度 ${progressPct}%`"
            >
              <span class="task-progress__fill" :style="{ width: `${progressPct}%` }"></span>
            </div>
            <p class="task-progress-label">{{ progressPct }}% 完成</p>

            <dl class="reg-facts">
              <div><dt>待完成</dt><dd>{{ visiblePending }} 项</dd></div>
              <div>
                <dt>轮次</dt>
                <dd>{{ selectedRound == null ? `${rounds.length} 轮` : `第 ${selectedRound} 轮` }}</dd>
              </div>
              <div><dt>目标岗位</dt><dd class="muted">{{ targetJob }}</dd></div>
              <div><dt>清单状态</dt><dd class="muted">{{ listStatusText }}</dd></div>
            </dl>

            <nav class="reg-rounds" aria-label="按轮次筛选任务">
              <p class="reg-rounds__label">按轮次查看</p>
              <button
                class="reg-round"
                :class="{ 'is-active': selectedRound === null }"
                :aria-pressed="selectedRound === null"
                type="button"
                @click="selectedRound = null"
              >
                全部轮次 <b>{{ summary.total }}</b>
              </button>
              <button
                v-for="round in rounds"
                :key="round.round"
                class="reg-round"
                :class="{ 'is-active': selectedRound === round.round }"
                :aria-pressed="selectedRound === round.round"
                type="button"
                @click="selectedRound = round.round"
              >
                第 {{ round.round }} 轮 <b>{{ round.completed_count }} / {{ round.task_count }}</b>
              </button>
            </nav>

            <div class="reg-actions">
              <button class="reg-quiet" type="button" :disabled="loading" @click="load">
                {{ loading ? '刷新中…' : '刷新' }}
              </button>
              <button
                class="reg-quiet reg-quiet--danger"
                type="button"
                :disabled="readOnly || clearing"
                @click="openClear"
              >
                清空任务清单
              </button>
            </div>
          </aside>

          <section class="pg-main" aria-label="任务清单">
            <div class="reg-sec-head">
              <span>{{ visibleTasks.length }} 项 · {{ visiblePending }} 项待完成</span>
              <span v-if="visibleDone" class="task-sec-hint">{{ visibleDone }} 项已完成</span>
            </div>

            <div v-if="!visibleTasks.length" class="empty">该轮次没有任务。</div>

            <template v-else>
              <div v-for="group in groupedTasks" :key="group.round" class="reg-group">
                <div class="reg-group-head">
                  <span>第 {{ group.round }} 轮</span>
                  <span><b>{{ group.done }} / {{ group.tasks.length }}</b> 完成</span>
                </div>

                <!-- 待办列表：一项一行。点完成后先挂 is-leaving 播「咻」的离场动画 -->
                <div v-if="pendingOf(group).length" class="task-list">
                  <article
                    v-for="task in pendingOf(group)"
                    :key="task.task_id"
                    class="reg-row task-row"
                    :class="{ 'is-leaving': leavingTaskId === task.task_id }"
                  >
                    <span class="task-dot" :class="`is-${task.kind}`" aria-hidden="true"></span>

                    <div class="task-main">
                      <div class="task-title-line">
                        <span class="task-no">{{ taskNo(task) }}</span>
                        <h3 class="reg-task-title">{{ task.title || '（无标题）' }}</h3>
                        <span class="task-kind">{{ taskKindText(task.kind) }}</span>
                      </div>
                      <div class="task-meta">
                        <span v-for="skill in task.skills || []" :key="skill" class="task-chip">{{ skill }}</span>
                        <span v-if="!(task.skills || []).length" class="task-chip is-none">未关联技能</span>
                        <span v-if="taskLoad(task)" class="task-load">{{ taskLoad(task) }}</span>
                        <span class="task-round">第 {{ task.round }} 轮</span>
                        <span class="task-state">待完成</span>
                      </div>
                    </div>

                    <div class="task-actions">
                      <button
                        class="reg-submit reg-complete task-btn task-btn--primary"
                        type="button"
                        :disabled="readOnly || submitting"
                        @click="completeTaskDirect(task)"
                      >
                        标记完成
                      </button>
                      <button
                        class="reg-submit reg-more task-btn"
                        type="button"
                        :disabled="readOnly || submitting"
                        @click="openComplete(task)"
                      >
                        附凭证 / 心得
                      </button>
                    </div>
                  </article>
                </div>
                <p v-else class="task-all-done">这一轮的任务都已完成，记录收在下方「已完成」里。</p>

                <!-- 已完成：变灰加删除线，记录与补凭证入口都保留 -->
                <details v-if="group.done" class="task-done">
                  <summary>
                    <span>已完成 {{ group.done }} 项</span>
                    <span class="task-done-hint">展开可以补凭证</span>
                  </summary>
                  <div class="task-list task-list--done">
                    <article
                      v-for="task in doneOf(group)"
                      :key="task.task_id"
                      class="reg-row task-row task-row--done is-done"
                    >
                      <span class="task-dot is-done" aria-hidden="true"></span>

                      <div class="task-main">
                        <div class="task-title-line">
                          <span class="task-no">{{ taskNo(task) }}</span>
                          <h3 class="reg-task-title">{{ task.title || '（无标题）' }}</h3>
                          <span class="task-kind">{{ taskKindText(task.kind) }}</span>
                          <span v-if="task.completed_at" class="task-date">
                            {{ shortDate(task.completed_at) }}
                          </span>
                        </div>
                        <div class="task-meta">
                          <span
                            v-if="taskEvidenceKind(task) === 'growth'"
                            class="reg-mark reg-mark--ok"
                          >已附凭证</span>
                          <span v-else class="reg-mark reg-mark--warn">自述完成 · 未附凭证</span>
                          <span v-if="task.evidence" class="task-note">
                            <b>凭证</b>{{ formatEvidence(task.evidence) }}
                          </span>
                          <span v-if="task.note" class="task-note">
                            <b>心得</b>{{ task.note }}
                          </span>
                        </div>
                      </div>

                      <div v-if="needsProof(task)" class="task-actions">
                        <button
                          class="reg-submit reg-attach task-btn"
                          type="button"
                          :disabled="readOnly || submitting"
                          @click="openAttach(task)"
                        >
                          补凭证
                        </button>
                      </div>
                    </article>
                  </div>
                </details>
              </div>
            </template>
          </section>
        </div>

        <footer class="reg-foot">
          <span>共 {{ rounds.length }} 轮 · {{ summary.total }} 项任务</span>
        </footer>
        </template>

        <section v-else-if="!planDetailRound" class="reg-app">
          <p class="reg-app-note">更早的轮次只保留了任务计数，明细只有最新一轮可查。</p>
        </section>

        <section v-else-if="activeTab === 'learning'" class="reg-app learn-page">
          <header class="learn-head">
            <div class="learn-head__copy">
              <p class="learn-eyebrow">第 {{ planDetailRound.round }} 轮 · 学习路线</p>
              <h2 class="learn-head__title">
                {{ learningPlan.length ? `${learningPlan.length} 个阶段，按顺序推进` : '这一轮没有安排学习阶段' }}
              </h2>
              <p class="learn-lede">每个阶段都写着起点、要补的缺口和做到什么算完成。</p>
            </div>
            <dl class="learn-stats">
              <div><dt>阶段</dt><dd>{{ learningPlan.length }}</dd></div>
              <div><dt>预计</dt><dd>{{ learningWeeks ? `${learningWeeks} 周` : '未估' }}</dd></div>
              <div><dt>覆盖缺口</dt><dd>{{ learningGapCount }}</dd></div>
            </dl>
          </header>

          <!-- 这一轮绑的依据快照：下一轮「生成下一轮」只认它，不再重新推断岗位要求 -->
          <aside v-if="roundSnapshot" class="learn-basis">
            <div class="learn-basis__row">
              <span class="learn-basis__label">本轮岗位依据</span>
              <AppBadge
                v-if="snapshotBasis"
                class="reg-basis-badge"
                :text="requirementBasisText(snapshotBasis)"
                :tone="requirementBasisTone(snapshotBasis)"
              />
              <span class="learn-basis__counts">
                {{ snapshotRequirementCount }} 条岗位要求 ·
                {{ snapshotEvidenceCount
                  ? `${snapshotEvidenceCount} 条依据（${retrievalStatusText(snapshotEvidenceStatus)}）`
                  : '没有可回查的依据' }}
              </span>
            </div>
            <details v-if="snapshotJdText" class="fold learn-basis__fold">
              <summary>展开这一轮粘贴的招聘要求</summary>
              <div class="fold-body">
                <p class="muted">{{ snapshotJdText }}</p>
              </div>
            </details>
            <p v-else class="learn-basis__note">
              这一轮没有粘贴招聘要求：岗位要求来自同类岗位资料或 AI 推断，
              之后生成的每一轮都沿用同一套要求。
            </p>
          </aside>

          <ol v-if="learningPlan.length" class="learn-route">
            <li v-for="(stage, i) in learningPlan" :key="i" class="learn-step">
              <span class="learn-node" aria-hidden="true">{{ pad2(i) }}</span>
              <article class="learn-card">
                <header class="learn-card__head">
                  <div>
                    <p class="learn-step-label">阶段 {{ stage.stage || i + 1 }}</p>
                    <h3>{{ stage.topic || `阶段 ${stage.stage || i + 1}` }}</h3>
                  </div>
                  <span v-if="stage.estimated_weeks" class="learn-weeks">
                    约 {{ stage.estimated_weeks }} 周
                  </span>
                </header>

                <p v-if="stage.goal" class="learn-goal">
                  <span>目标</span>{{ stage.goal }}
                </p>

                <div v-if="(stage.covers || []).length" class="learn-gaps">
                  <span class="learn-label">覆盖缺口</span>
                  <span v-for="gap in stage.covers" :key="gap" class="learn-chip">{{ gap }}</span>
                </div>

                <p class="learn-prereq">
                  <span>前置</span>{{ stage.prerequisite || '无特殊前置' }}
                </p>

                <div v-if="stage.completion_criteria" class="learn-done">
                  <span class="learn-done__label">完成标准</span>
                  <p>{{ stage.completion_criteria }}</p>
                </div>
              </article>
            </li>
          </ol>
          <div v-else class="empty">这一轮没有安排学习阶段。</div>
        </section>

        <section v-else class="reg-app proj-page">
          <header class="proj-head">
            <div class="proj-head__copy">
              <p class="proj-eyebrow">第 {{ planDetailRound.round }} 轮 · 实战项目</p>
              <h2 class="proj-head__title">
                {{ projectPlan.length ? `${projectPlan.length} 个项目，边做边补缺口` : '这一轮没有安排实战项目' }}
              </h2>
              <p class="proj-lede">每个项目都列了目标、技术栈、覆盖的缺口和最终要交出的成果。</p>
            </div>
            <dl class="proj-stats">
              <div><dt>项目</dt><dd>{{ projectPlan.length }}</dd></div>
              <div><dt>技术栈</dt><dd>{{ projectTechCount }}</dd></div>
              <div><dt>覆盖缺口</dt><dd>{{ projectGapCount }}</dd></div>
            </dl>
          </header>

          <div v-if="projectPlan.length" class="proj-grid">
            <article
              v-for="(project, i) in projectPlan"
              :key="i"
              class="proj-card"
              :class="{ 'is-blocked': project.blocked_reason }"
            >
              <header class="proj-card__head">
                <span class="proj-no" aria-hidden="true">{{ pad2(i) }}</span>
                <div>
                  <h3>{{ project.name || `项目 ${i + 1}` }}</h3>
                  <p v-if="project.difficulty || project.stage" class="proj-tags">
                    <span v-if="project.difficulty" class="proj-tag proj-tag--level">
                      难度 {{ project.difficulty }}
                    </span>
                    <span v-if="project.stage" class="proj-tag">阶段 {{ project.stage }}</span>
                  </p>
                </div>
              </header>

              <p v-if="project.goal" class="proj-goal">
                <span>目标</span>{{ project.goal }}
              </p>

              <dl class="proj-facts">
                <dt>技术栈</dt>
                <dd>{{ joinList(project.tech_stack) }}</dd>
                <dt>覆盖缺口</dt>
                <dd>{{ joinList(project.covers_gaps) }}</dd>
              </dl>

              <div v-if="project.deliverable" class="proj-deliverable">
                <span>最终成果</span>
                <p>{{ project.deliverable }}</p>
              </div>

              <p v-if="project.blocked_reason" class="proj-blocked">
                {{ project.blocked_reason }}
              </p>
            </article>
          </div>
          <div v-else class="empty">这一轮没有安排实战项目。</div>
        </section>
      </template>
    </article>

    <!-- 弹层挂到 body：留在视图根里会被切页的 opacity 动画困成新的层叠上下文，
         窄屏时固定抽屉会被顶部移动条盖住。 -->
    <Teleport to="body">
    <!-- 提交证据：右侧抽屉，写证据时清单还在视野里 -->
    <div v-if="completingTask" class="reg-sheet-backdrop" @click.self="closeComplete">
      <section
        class="reg-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tv-modal-title"
        @keydown.esc="closeComplete"
      >
        <div class="reg-sheet__head">
          <div>
            <div class="reg-sheet__eyebrow">
              任务 {{ taskNo(completingTask) }} · {{ taskKindText(completingTask.kind) }} ·
              第 {{ completingTask.round }} 轮
            </div>
            <h2 id="tv-modal-title">
              {{ sheetMode === 'attach' ? '补上任务凭证' : '完成任务' }}
            </h2>
          </div>
          <button class="reg-sheet__close" type="button" aria-label="关闭" @click="closeComplete">
            ×
          </button>
        </div>

        <p class="reg-sheet__task">{{ completingTask.title || '（无标题）' }}</p>
        <p class="reg-sheet__skills">
          <template v-if="(completingTask.skills || []).length">
            关联技能：{{ (completingTask.skills || []).join('、') }}
          </template>
          <template v-else>
            这条任务没有关联技能 —— 提交只会留下一条完成记录，不会带来技能升级。
          </template>
        </p>

        <div v-if="completingCriteria" class="reg-sheet__criteria">
          <span>{{ completingCriteria.label }}</span>
          <p>{{ completingCriteria.text }}</p>
        </div>

        <!-- 补凭证：只收凭证，心得不在这里写（原来那条自述记录一条不改） -->
        <div v-if="sheetMode === 'attach'" class="reg-sheet__field">
          <label for="tv-attach-evidence">
            凭证
            <small>链接、截图位置或产出说明都行；这里写的才算直接证据，心得不算</small>
          </label>
          <textarea
            id="tv-attach-evidence"
            v-model="evidenceText"
            rows="4"
            placeholder="例：https://github.com/你的账号/仓库/commit/… 或 产出文件路径 / 运行截图位置"
          />
          <div class="reg-sheet__count">
            {{ evidenceText.trim().length }} / {{ EVIDENCE_MAX_LENGTH }}
          </div>
        </div>

        <template v-else>
          <div class="reg-sheet__field">
            <label for="tv-evidence">
              凭证（选填）
              <small>
                <template v-if="completingCriteria">
                  照上面的{{ completingCriteria.label }}，有链接、截图或产出就贴在这里
                </template>
                <template v-else>链接、截图位置或产出说明；不填也能完成</template>
              </small>
            </label>
            <textarea
              id="tv-evidence"
              v-model="evidenceText"
              rows="4"
              placeholder="例：https://github.com/你的账号/仓库/commit/… 或 产出文件路径"
            />
            <div class="reg-sheet__count">
              {{ evidenceText.trim().length }} / {{ EVIDENCE_MAX_LENGTH }}
            </div>
          </div>

          <div class="reg-sheet__field">
            <label for="tv-note">心得笔记（选填）</label>
            <textarea
              id="tv-note"
              v-model="noteText"
              rows="3"
              placeholder="例：卡得最久的是红黑树的删除旋转，最后照着可视化动图一步步推通了"
            />
            <div class="reg-sheet__count">{{ noteText.trim().length }} / {{ NOTE_MAX_CHARS }}</div>
          </div>
        </template>

        <div v-if="evidenceError" class="notice notice-warn">{{ evidenceError }}</div>
        <div v-if="submitError" class="notice notice-danger">{{ submitError }}</div>

        <div class="reg-sheet__foot">
          <button class="btn" :disabled="submitting" @click="closeComplete">取消</button>
          <button class="btn btn-primary" :disabled="submitting" @click="submit">
            <span v-if="submitting" class="spinner" />
            {{ submitting ? '提交中…' : (sheetMode === 'attach' ? '确认补上' : '确认完成') }}
          </button>
        </div>
      </section>
    </div>

    <!-- 提交结果：单独一张抽屉。不再塞回清单顶部，提交完页面停在原地 -->
    <div
      v-if="submitResult || attachResult"
      class="reg-result-backdrop"
      @click.self="closeResult"
      @keydown.esc="closeResult"
    >
      <section
        class="reg-result-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="提交结果"
      >
        <header class="reg-result-drawer__head">
          <p class="reg-result-drawer__eyebrow">{{ resultEyebrow }}</p>
          <button
            class="reg-result-drawer__close"
            type="button"
            aria-label="关闭结果"
            @click="closeResult"
          >
            ×
          </button>
        </header>

        <div class="reg-result-drawer__body">
          <div v-if="submitResult" id="tv-result" tabindex="-1" aria-live="polite">
            <CompletionResult :result="submitResult" />
          </div>

          <!-- 补凭证：不套 CompletionResult（那套讲的是提交完成，不是补凭证） -->
          <div
            v-else
            id="tv-attach-result"
            class="notice notice-ok"
            tabindex="-1"
            aria-live="polite"
          >
            <strong>凭证已经记到这条任务上</strong>
            <div class="notice-body">
              <template v-if="attachResult.verified">
                成长记录里也已经翻成有凭证；原来那条自述记录原样保留，一条都没有改动。
              </template>
              <template v-else-if="attachResult.pending_next_round">
                成长记录里还没有这条成果 —— 下一轮生成时会按有凭证入账。
              </template>
              <template v-else>
                任务上的凭证已经保存；成长记录里没有需要改写的自述成果。
              </template>
              <template v-if="attachResult.reason"> {{ attachResult.reason }}</template>
            </div>
          </div>
        </div>

        <footer class="reg-result-drawer__foot">
          <button class="btn btn-primary" type="button" @click="closeResult">收起结果</button>
        </footer>
      </section>
    </div>

    <!-- 清空弹层 -->
    <div v-if="clearing" class="modal-backdrop" @click.self="closeClear">
      <div
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tv-clear-title"
        @keydown.esc="closeClear"
      >
        <div class="modal-head">
          <h2 id="tv-clear-title">清空任务清单</h2>
        </div>
        <div class="modal-body">
          <p class="muted flush-first">
            会删掉 <strong>{{ rounds.length }}</strong> 轮、共
            <strong>{{ summary.total }}</strong> 项任务（其中已完成
            {{ summary.completed }} 项），删完这一页回到「还没有任务」的状态。
          </p>

          <dl class="kv kv--mid">
            <dt>成长记录</dt>
            <dd>不受影响。已经正式记下来的升级与成果一条都不会少。</dd>
            <dt>原清单</dt>
            <dd>清空前自动备份一份，照着备份名可以还原。</dd>
            <dt>未记账成果</dt>
            <dd>
              <template v-if="pendingCount === null">
                暂时查不到有没有。若真有，清空会先被拦住、一条都不会删，回来告诉你条数。
              </template>
              <template v-else-if="pendingCount === 0">
                没有。打卡攒下的成果都已经正式记账了。
              </template>
              <template v-else>
                <strong class="is-warn">{{ pendingCount }} 条</strong>还没进成长记录，
                清空会把这批真实进度一起带走。
              </template>
            </dd>
          </dl>

          <label v-if="clearNeedsAck" class="clear-ack">
            <input v-model="dropPending" type="checkbox" />
            <span>我确认连同这 {{ pendingCount }} 条未记账成果一起丢掉</span>
          </label>

          <div v-if="clearError" class="notice notice-danger">{{ clearError }}</div>

          <div class="notice notice-info flush-last">
            这里只清任务清单。想清空<b>画像与求职目标</b>，去「岗位分析」页用那边的清空按钮。
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" :disabled="clearBusy" @click="closeClear">取消</button>
          <button class="btn btn-danger" :disabled="clearBusy || clearBlocked" @click="confirmClear">
            <span v-if="clearBusy" class="spinner" />
            {{ clearBusy ? '清空中…' : '确认清空' }}
          </button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>
