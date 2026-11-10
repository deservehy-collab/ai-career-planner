<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import AgentTag from '../components/AgentTag.vue'
import AppBadge from '../components/AppBadge.vue'
import GoalCard from '../components/GoalCard.vue'
import NextRoundResult from '../components/NextRoundResult.vue'
import PageShell from '../components/PageShell.vue'

import {
  addManualEvent,
  getGrowthHistory,
  getPending,
  listTasks,
  startNextRound,
  voidGrowthEvent,
  withdrawPending,
} from '../api/endpoints'
import { pollRun } from '../composables/useRunPolling'
import { usePlansStore } from '../stores/plans'
import { useProfileStore } from '../stores/profile'
import {
  gateStatusText,
  goalStatusText,
  goalStatusTone,
  knowledgeBaseStatusText,
  storeStatusText,
  storeStatusTone,
} from '../utils/enums'
import { formatDateTime, formatRunError, joinList } from '../utils/format'

/**
 * 成长记录页（交接台布局）。
 *
 * 左侧账簿按「待交接 → 已入账」分段，右侧行动栏常驻显示本轮计划与主操作。
 * 三块数据各有出处，**不要混为一谈**：
 *   待处理成果 —— GET /api/growth/pending   （只读；还没交给成长节点的成果）
 *   成长历史   —— GET /api/growth/history   （只读；**真正落盘**的那份）
 *   下一轮生成 —— POST /api/growth/next-round（正常记账：把待处理成果写进成长历史）
 *
 * 「下一轮」会重跑整条流水线（多次本地模型推理）并修改成长数据，因此：
 *   · 必须二次确认；
 *   · 必须携带真实画像（缺画像会让 profile 节点报错、该次 run 变 failed）；
 *   · 没有待处理成果时直接禁用，不做无意义的推理。
 *
 * 另外两个**用户主动的修正**入口，都不删除原始记录：
 *   撤回待处理 —— POST /api/growth/pending/withdraw（任务退回未完成、证据清空）
 *   作废已入账 —— POST /api/growth/history/void（追加调整记录并按剩余成果重算能力表）
 */
const profile = useProfileStore()
const plans = usePlansStore()

/** 当前选中的档案：所有成长数据都只在这个档案里读写。 */
const activePlanId = computed(() => plans.activeId)

const loading = ref(false)
const loadError = ref('')

const pending = ref(null)
const history = ref(null)
/** 任务清单只读快照：当前计划的目标/轮次、持久化结局、未保存的恢复标记。 */
const plan = ref(null)

const confirming = ref(false)
const running = ref(false)
const runId = ref(null)
const runStatus = ref('idle')
const runResult = ref(null)
const runError = ref('')

const pendingEvents = computed(() => pending.value?.events || [])
const pendingTasks = computed(() => pending.value?.tasks || [])
const pendingWarnings = computed(() => pending.value?.warnings || [])
/** false 表示成长历史不可读、**无法判断哪些已处理** —— 是"判不了"，不是"没有待办"。 */
const pendingRestored = computed(() => pending.value?.restored !== false)

const historyEntries = computed(() => history.value?.history || [])
const latestSkills = computed(() => history.value?.latest_skills || [])
const baseline = computed(() => history.value?.baseline || null)

/** 账簿按新到旧读：最新一条排在最上面，作废时仍用原始索引回写。 */
const historyRows = computed(() =>
  historyEntries.value.map((entry, index) => ({ entry, index })).reverse(),
)

/** 待交接按原始索引成对拿任务：分页后仍能对上来源任务与撤回入口。 */
const pendingRows = computed(() =>
  pendingEvents.value.map((event, index) => ({
    event,
    task: pendingTasks.value[index] || null,
    index,
  })),
)

/** 账簿分页：一页 5 条，清单再长也不把页面拉成无尽的带子。 */
const PAGE_SIZE = 5

const pendingPage = ref(1)
const pendingPageCount = computed(() =>
  Math.max(1, Math.ceil(pendingRows.value.length / PAGE_SIZE)),
)
const pendingPageRows = computed(() =>
  pendingRows.value.slice(
    (pendingPage.value - 1) * PAGE_SIZE,
    pendingPage.value * PAGE_SIZE,
  ),
)

const historyPage = ref(1)
const historyPageCount = computed(() =>
  Math.max(1, Math.ceil(historyRows.value.length / PAGE_SIZE)),
)
const historyPageRows = computed(() =>
  historyRows.value.slice(
    (historyPage.value - 1) * PAGE_SIZE,
    historyPage.value * PAGE_SIZE,
  ),
)

function goPendingPage(delta) {
  pendingPage.value = Math.min(
    pendingPageCount.value,
    Math.max(1, pendingPage.value + delta),
  )
}

function goHistoryPage(delta) {
  historyPage.value = Math.min(
    historyPageCount.value,
    Math.max(1, historyPage.value + delta),
  )
}

/** 撤回/作废后条目变少，停在空页就退到最后一页。 */
watch(pendingPageCount, (total) => {
  if (pendingPage.value > total) pendingPage.value = total
})
watch(historyPageCount, (total) => {
  if (historyPage.value > total) historyPage.value = total
})

/** 展开状态按原始索引记；默认全部收起，账簿先保持可扫读的一行高度。 */
const expandedEntries = ref({})

function isEntryExpanded(index) {
  return expandedEntries.value[index] === true
}

function toggleEntry(index) {
  expandedEntries.value[index] = !isEntryExpanded(index)
}

/** 验证记录里的 ``changed_skills`` 后端存的是计数（int），旧数据里见过数组，两种都认。 */
function changedSkillCount(entry) {
  const value = entry?.changed_skills
  if (Array.isArray(value)) return value.length
  return Number(value) || 0
}

/** 折叠行上的一句话摘要：先看等级有没有变，再看带了几条成果。 */
function entrySummary(entry) {
  if (entry?.kind === 'verification') {
    const changed = changedSkillCount(entry)
    return changed ? `补凭证 · ${changed} 项改为有凭证` : '补凭证 · 能力等级没有变化'
  }
  const parts = []
  const changes = (entry.level_changes || []).length
  parts.push(
    changes
      ? `${changes} 项等级变化`
      : entry.kind === 'adjustment'
        ? '能力表已重算'
        : '无等级变化',
  )
  const events = (entry.events || []).length
  if (events) parts.push(`${events} 条成果`)
  const blocked = (entry.blocked_upgrades || []).length
  if (blocked) parts.push(`${blocked} 项被前置检查拦下`)
  return parts.join(' · ')
}

/** 历史事件没有 evidence_kind：有凭证按凭证算，没有就是自述完成。 */
function eventKind(event) {
  if (event?.evidence_kind) return event.evidence_kind
  return event?.evidence ? 'growth' : ''
}

const hasProof = (event) => eventKind(event) === 'growth'

/** 单说「正常」看不出在说什么，补上「技能标准」前缀。 */
function knowledgeBadgeText(value) {
  return `技能标准：${knowledgeBaseStatusText(value)}`
}

const baselineLabel = computed(() => {
  if (!baseline.value) return ''
  return baseline.value.source === 'user_profile'
    ? '首次分析时填的个人画像'
    : '初始能力表'
})

/** 当前计划：下一轮只认这一轮保存的目标与招聘要求。 */
const currentPlan = computed(() => plan.value?.latest_round || null)
const currentRoundNo = computed(() => currentPlan.value?.round ?? null)
const planTarget = computed(() => String(currentPlan.value?.target_job || '').trim())
const planRoundsCount = computed(() => plan.value?.rounds?.length ?? 1)

/** 持久化结局：刷新页面后仍然显示"已达成 / 依据不足"。 */
const goalRecord = computed(() => plan.value?.latest_goal || null)
const persistedGoal = computed(() => goalRecord.value?.goal || null)
const goalClosure = computed(
  () =>
    persistedGoal.value?.status === 'achieved'
    || persistedGoal.value?.status === 'basis_insufficient',
)

/** 上一次没保存下来的计划：有 plans 时点按钮会直接补存，不重跑模型。 */
const recovery = computed(() => plan.value?.pending_next_round || null)
const recoveryHasPlans = computed(() => recovery.value?.has_plans === true)

/** 目标已达成时不再生成；换岗位要先在分析页「开始新计划」，新计划会带回 in_progress。 */
const goalAchieved = computed(() => persistedGoal.value?.status === 'achieved')

/** 有待处理成果、或有一份算好但没存下的计划，才值得跑一次。 */
const canGenerate = computed(
  () =>
    (pendingEvents.value.length > 0 || recoveryHasPlans.value) &&
    !goalAchieved.value &&
    !running.value,
)

const blockedReason = computed(() => {
  if (running.value) return ''
  if (goalAchieved.value) {
    return '当前目标已达成：所有岗位要求都有项目或成长记录作为直接证据，无需再生成下一轮。要换方向，去「岗位分析」页换目标岗位并点「开始新计划」。'
  }
  if (recoveryHasPlans.value) return ''
  if (!pendingRestored.value) return '成长记录读不出来，无法确认待处理成果是否完整，已禁用生成。'
  if (pendingEvents.value.length === 0) {
    return '当前没有待交接成果。先去「学习与项目」页标记任务完成（可附凭证），或补录一条成果。'
  }
  return ''
})

const generateLabel = computed(() => {
  if (running.value) return `执行中…（${runStatus.value}）`
  return recoveryHasPlans.value ? '补存上次计划' : '生成下一轮计划'
})

async function load() {
  const captured = { planId: plans.activeId, epoch: plans.epoch }
  if (!captured.planId) {
    pending.value = null
    history.value = null
    plan.value = null
    loadError.value = ''
    loading.value = false
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const [pendingData, historyData, taskData] = await Promise.all([
      getPending({}, captured.planId),
      getGrowthHistory({}, captured.planId),
      listTasks({}, {}, captured.planId),
    ])
    if (plans.activeId !== captured.planId || plans.epoch !== captured.epoch) return
    pending.value = pendingData
    history.value = historyData
    plan.value = taskData
  } catch (error) {
    if (plans.activeId !== captured.planId || plans.epoch !== captured.epoch) return
    loadError.value = error?.message || '加载成长数据失败'
  } finally {
    if (plans.activeId === captured.planId && plans.epoch === captured.epoch) {
      loading.value = false
    }
  }
}

onMounted(load)
watch(() => [plans.activeId, plans.epoch], () => {
  // 换档案就从头看，别停在上一个档案的第 3 页。
  pendingPage.value = 1
  historyPage.value = 1
  load()
})

function askConfirm() {
  if (!canGenerate.value) return
  confirming.value = true
  runError.value = ''
  runResult.value = null
}

async function confirmGenerate() {
  if (!canGenerate.value || running.value) return

  const planId = plans.activeId
  if (!planId) {
    runError.value = '还没有选择求职档案。请先在「我的档案」里选择或新建一份。'
    return
  }

  confirming.value = false
  running.value = true
  runError.value = ''
  runResult.value = null
  runStatus.value = 'queued'

  // 目标岗位与招聘原文一律由后端从当前档案的快照里取：请求里不再带，
  // 避免用户改了画像里的目标却把正在执行的计划悄悄带偏（后端也会拒绝不一致的请求）。
  const body = { user_profile: profile.toRequestProfile() }

  try {
    const accepted = await startNextRound(body, planId)
    runId.value = accepted.run_id
    runStatus.value = accepted.status || 'queued'

    const record = await pollRun(accepted.run_id, {
      planId,
      onTick: (tick) => {
        runStatus.value = tick.status
      },
    })

    if (plans.activeId === planId) {
      if (record.status === 'succeeded') {
        runResult.value = record.result || null
      } else {
        runError.value = formatRunError(record.error, '生成失败，这次没拿到具体原因。')
      }
    }
    await load()
  } catch (error) {
    if (error?.name === 'AbortError') {
      runStatus.value = 'idle'
      return
    }
    if (plans.activeId === planId) {
      runError.value = error?.message || '生成下一轮失败'
      runStatus.value = 'failed'
    }
  } finally {
    running.value = false
  }
}

function reasonOf(error, fallback) {
  const detail = error?.detail
  return (detail && (detail.reason || detail.message)) || error?.message || fallback
}

// ── 撤回待处理成果（还没入账，只动 loop.json）────────────────────────────
const withdrawTarget = ref(null)
const withdrawBusy = ref(false)
const withdrawMessage = ref('')
const withdrawError = ref('')

function askWithdraw(taskId) {
  if (!taskId || withdrawBusy.value) return
  withdrawTarget.value = taskId
  withdrawMessage.value = ''
  withdrawError.value = ''
}

async function confirmWithdraw() {
  if (!withdrawTarget.value || withdrawBusy.value) return
  const planId = plans.activeId
  if (!planId) {
    withdrawError.value = '还没有选择求职档案。'
    return
  }
  withdrawBusy.value = true
  withdrawError.value = ''
  try {
    await withdrawPending(withdrawTarget.value, planId)
    withdrawMessage.value = '已撤回：任务退回未完成，完成证据已清空，这条成果不会记进成长记录。'
    withdrawTarget.value = null
    await load()
  } catch (error) {
    withdrawError.value = reasonOf(error, '撤回失败，记录未改动')
  } finally {
    withdrawBusy.value = false
  }
}

// ── 作废已入账成果（追加调整记录，不删原记录）────────────────────────────
const voiding = ref({
  entryIndex: null,
  eventIndex: null,
  reason: '',
  busy: false,
  error: '',
})
const voidMessage = ref('')

function askVoid(entryIndex, eventIndex) {
  if (running.value || voiding.value.busy) return
  voiding.value = { entryIndex, eventIndex, reason: '', busy: false, error: '' }
  voidMessage.value = ''
}

function resetVoidForm() {
  voiding.value = { entryIndex: null, eventIndex: null, reason: '', busy: false, error: '' }
}

function cancelVoid() {
  if (voiding.value.busy) return
  resetVoidForm()
}

function isVoided(entry, eventIndex) {
  return (entry.voided_event_indices || []).includes(eventIndex)
}

function voidedTitles(entry) {
  return (entry.voided || [])
    .map((item) => item.title)
    .filter(Boolean)
    .join('、')
}

async function confirmVoid() {
  const { entryIndex, eventIndex, reason } = voiding.value
  if (entryIndex === null || eventIndex === null || voiding.value.busy) return
  if (reason.trim().length < 2) {
    voiding.value.error = '请填写作废原因（至少 2 个字）'
    return
  }
  const planId = plans.activeId
  if (!planId) {
    voiding.value.error = '还没有选择求职档案。'
    return
  }
  voiding.value.busy = true
  voiding.value.error = ''
  try {
    await voidGrowthEvent({
      entry_index: entryIndex,
      event_index: eventIndex,
      reason: reason.trim(),
    }, planId)
    voidMessage.value = '已作废：原记录保留，能力表已按剩余成果重新计算。'
    resetVoidForm()
    await load()
  } catch (error) {
    voiding.value.error = reasonOf(error, '作废失败，记录未改动')
  } finally {
    voiding.value.busy = false
  }
}

// ── 手动补录成果（打破空系统死锁）────────────────────────────────────────
function blankManual(open = false) {
  return {
    kind: 'learning',
    title: '',
    skills: '',
    evidence: '',
    open,
    submitting: false,
    error: '',
  }
}

const manual = ref(blankManual())
const manualNotice = ref('')
/** 抽屉关掉后把焦点送回触发按钮，键盘用户不会掉到页面顶部。 */
let lastFocused = null

function restoreFocus() {
  nextTick(() => lastFocused?.focus?.())
}

function openManual() {
  lastFocused = document.activeElement
  manual.value.error = ''
  manualNotice.value = ''
  manual.value.open = true
  nextTick(() => document.getElementById('manual-title')?.focus())
}

function closeManual() {
  if (manual.value.submitting) return
  manual.value.open = false
  manual.value.error = ''
  restoreFocus()
}

/** 「清空」只清字段，抽屉保持打开。 */
function resetManualFields() {
  manual.value = blankManual(true)
}

/** 补录成功后收起抽屉并清空表单。 */
function clearManualForm() {
  manual.value = blankManual(false)
  restoreFocus()
}

async function submitManualEvent() {
  if (manual.value.submitting) return
  const title = manual.value.title.trim()
  const evidence = manual.value.evidence.trim()
  if (!title) {
    manual.value.error = '请填写成果标题'
    return
  }

  manual.value.submitting = true
  manual.value.error = ''
  try {
    const planId = plans.activeId
    if (!planId) {
      manual.value.error = '还没有选择求职档案。请先到「我的档案」选择或新建一份。'
      return
    }
    const skills = manual.value.skills
      .split(/[\n,，、;；]/)
      .map((s) => s.trim())
      .filter(Boolean)
    const body = {
      kind: manual.value.kind,
      title,
      skills,
    }
    // 凭证选填：不填就是自述成果，升级照规则走，但按未验证处理。
    if (evidence) body.evidence = evidence
    if (profile.targetJob.trim()) body.target_job = profile.targetJob.trim()
    await addManualEvent(body, planId)
    clearManualForm()
    manualNotice.value = `已补录「${title}」，它已进入待交接列表。`
    await load()
  } catch (error) {
    const detail = error?.detail
    const reason =
      (detail && (detail.reason || detail.message)) ||
      error?.message ||
      '补录成果失败'
    manual.value.error = reason
  } finally {
    manual.value.submitting = false
  }
}
</script>

<template>
  <PageShell title="成长记录" variant="flat">
    <div v-if="loading && !pending && !history" class="gr-loading">正在加载成长数据…</div>

    <div v-else-if="loadError" class="notice notice-danger">
      <strong>加载成长数据失败</strong>
      <div class="notice-body">{{ loadError }}</div>
      <div class="btn-row"><button class="btn" @click="load">重试</button></div>
    </div>

    <div v-else-if="!plans.activeId" class="notice notice-warn">
      <strong>还没有选择求职档案</strong>
      <div class="notice-body">请先到「我的档案」选择或新建一份。</div>
      <div class="btn-row"><router-link class="btn btn-sm" to="/home">去我的档案</router-link></div>
    </div>

    <template v-else>
      <p class="faint gr-plan-line">
        当前档案：{{ plans.active?.title }}
        <template v-if="plans.active?.target_job && plans.active.target_job !== plans.active.title">
          · {{ plans.active.target_job }}
        </template>
      </p>

      <!-- 目标收尾：达成 / 依据不足，放在两栏之上整宽显示 -->
      <GoalCard
        v-if="goalClosure"
        :goal="persistedGoal"
        :target-job="planTarget"
      />

      <div class="gr-layout">
      <!-- 本轮：整页唯一的着色块，只回答「现在什么状态、能做什么」 -->
      <section class="gr-now" aria-labelledby="gr-now-title">
        <div class="gr-now-lead">
          <p class="gr-now-eyebrow">本轮计划</p>
          <template v-if="currentRoundNo">
            <p class="gr-now-round">
              <span class="gr-now-no">第 {{ currentRoundNo }} 轮</span>
              <span class="gr-now-total">共 {{ planRoundsCount }} 轮</span>
            </p>
            <h2 id="gr-now-title" class="gr-now-target">{{ planTarget || '未记录目标岗位' }}</h2>
            <div class="gr-now-badges">
              <AppBadge
                v-if="goalRecord"
                :text="goalStatusText(goalRecord.status)"
                :tone="goalStatusTone(goalRecord.status)"
              />
              <AppBadge v-else text="结局待判定" tone="muted" />
            </div>
          </template>
          <p v-else class="gr-now-none">
            还没有任务轮次。<router-link to="/analyze">去「岗位分析」</router-link>
            完成一次分析，首次分析会存成第 1 轮。
          </p>
        </div>
      </section>

      <div class="gr-aux">
      <div class="gr-next">
        <button
          class="btn btn-primary gr-next-btn"
          type="button"
          :disabled="!canGenerate"
          @click="askConfirm"
        >
          <span v-if="running" class="spinner" />
          {{ generateLabel }}
        </button>
        <p v-if="blockedReason" class="gr-next-hint">{{ blockedReason }}</p>
      </div>

      <div v-if="confirming" class="growth-confirm gr-confirm">
        <strong class="growth-confirm__title">
          {{ recoveryHasPlans ? '确认补存上次的计划？' : '确认生成下一轮？' }}
        </strong>
        <p class="growth-confirm__text">
          <template v-if="recoveryHasPlans">
            将把上次算好的计划存进任务清单，不重新跑 AI，也不重复记账。
          </template>
          <template v-else>
            把 <strong>{{ pendingEvents.length }} 条</strong>待交接成果正式记账，并重新规划学习与项目；
            成长记录和任务清单会一起更新。
          </template>
        </p>
        <div class="btn-row">
          <button class="btn btn-sm" type="button" @click="confirming = false">取消</button>
          <button class="btn btn-sm btn-primary" type="button" @click="confirmGenerate">确认并开始</button>
        </div>
      </div>

      <div v-if="recovery" class="notice notice-warn gr-below">
        <strong>{{ recoveryHasPlans ? '有一份算好但没保存的计划' : '上次生成在保存前中断' }}</strong>
        <div class="notice-body">
          <template v-if="recoveryHasPlans">
            已经算好<template v-if="recovery.source_round">（基于第 {{ recovery.source_round }} 轮）</template>，
            但没存进任务清单。点上方按钮只补存，不重新跑 AI。
          </template>
          <template v-else>
            再点上方按钮会按当时提交的成果重试；已经入账的成果不会重复升级。
          </template>
        </div>
      </div>

      <div v-if="runError" class="notice notice-danger gr-below">{{ runError }}</div>

      <section v-if="runResult" class="gr-result">
        <NextRoundResult :result="runResult" />
      </section>
      </div>

      <div class="gr-flow">
          <!-- 待交接 -->
          <section class="gr-ledger gr-ledger--pending" aria-labelledby="gr-pending-title">
            <header class="gr-ledger-head">
              <div class="gr-ledger-lead">
                <h2 id="gr-pending-title" class="gr-ledger-title">待交接</h2>
              </div>
              <div class="btn-row gr-ledger-tools">
                <AppBadge
                  :text="`${pendingEvents.length} 条`"
                  :tone="pendingEvents.length ? 'warn' : 'ok'"
                />
                <AgentTag node="growth" />
                <button class="btn btn-sm btn-ghost" type="button" @click="openManual">
                  ＋ 补录成果
                </button>
                <button class="btn btn-sm" type="button" :disabled="loading" @click="load">
                  {{ loading ? '刷新中…' : '刷新' }}
                </button>
              </div>
            </header>

            <div class="gr-ledger-body">
              <div v-if="manualNotice" class="notice notice-ok">{{ manualNotice }}</div>
              <div v-if="withdrawMessage" class="notice notice-ok">{{ withdrawMessage }}</div>

              <!-- 「判不了」与「没有」必须分开说 -->
              <div v-if="!pendingRestored" class="notice notice-warn flush-last">
                <strong>无法判断待交接成果</strong>
                <div class="notice-body">
                  成长记录读不出来，因此没法确定哪些成果已经处理过。为避免重复提交，这次不列出待交接成果。
                  <ul class="mini-list">
                    <li v-for="(warn, i) in pendingWarnings" :key="i">{{ warn }}</li>
                  </ul>
                </div>
              </div>

              <template v-else>
                <div v-if="!pendingEvents.length" class="growth-empty">
                  <p class="growth-empty__title">没有待交接成果</p>
                  <p class="growth-empty__text">
                    在「学习与项目」标记任务完成后，成果会先出现在这里。
                  </p>
                  <div class="btn-row">
                    <router-link class="btn btn-sm" to="/tasks">去标记完成任务</router-link>
                    <button class="btn btn-sm btn-ghost" type="button" @click="openManual">
                      ＋ 补录一条成果
                    </button>
                  </div>
                </div>

                <template v-else>
                  <article
                    v-for="row in pendingPageRows"
                    :key="row.index"
                    class="evidence-item pending-item"
                  >
                    <header class="evidence-head">
                      <AppBadge
                        :text="row.event.type === 'project' ? '项目成果' : '学习成果'"
                        :tone="row.event.type === 'project' ? 'ok' : 'primary'"
                      />
                      <AppBadge
                        :text="hasProof(row.event) ? '已附凭证' : '自述完成 · 未附凭证'"
                        :tone="hasProof(row.event) ? 'ok' : 'warn'"
                      />
                      <strong>{{ row.event.title }}</strong>
                      <span class="faint">{{ formatDateTime(row.event.at) }}</span>
                      <button
                        v-if="row.task?.task_id"
                        class="btn btn-sm evidence-action"
                        type="button"
                        :disabled="withdrawBusy"
                        @click="askWithdraw(row.task.task_id)"
                      >
                        撤回
                      </button>
                    </header>
                    <p v-if="row.event.evidence" class="evidence-quote">
                      {{ row.event.evidence }}
                    </p>
                    <div class="evidence-meta">
                      <span>涉及技能：{{ joinList(row.event.skills, '（未关联技能）') }}</span>
                      <span v-if="row.task">
                        来源任务：{{ row.task.title || '本轮任务' }}
                      </span>
                      <router-link
                        v-if="!hasProof(row.event)"
                        class="evidence-action-link"
                        to="/tasks"
                      >
                        去任务页补凭证
                      </router-link>
                    </div>

                    <div
                      v-if="row.task?.task_id && withdrawTarget === row.task.task_id"
                      class="notice notice-warn sub-block"
                    >
                      <strong>撤回这条成果？</strong>
                      <div class="notice-body">
                        对应任务会退回「未完成」，完成证据会被清空；这条成果不会记进成长记录。
                        之后可以在「学习与项目」页重新提交。
                      </div>
                      <div v-if="withdrawError" class="notice notice-danger sub-block">{{ withdrawError }}</div>
                      <div class="btn-row">
                        <button class="btn" :disabled="withdrawBusy" @click="withdrawTarget = null">取消</button>
                        <button class="btn btn-danger" :disabled="withdrawBusy" @click="confirmWithdraw">
                          <span v-if="withdrawBusy" class="spinner" />
                          {{ withdrawBusy ? '撤回中…' : '确认撤回' }}
                        </button>
                      </div>
                    </div>
                  </article>

                  <nav v-if="pendingPageCount > 1" class="gr-pager" aria-label="待交接分页">
                    <button
                      class="btn btn-sm btn-ghost"
                      type="button"
                      :disabled="pendingPage === 1"
                      @click="goPendingPage(-1)"
                    >
                      上一页
                    </button>
                    <span class="gr-pager__pos">第 {{ pendingPage }} / {{ pendingPageCount }} 页</span>
                    <button
                      class="btn btn-sm btn-ghost"
                      type="button"
                      :disabled="pendingPage === pendingPageCount"
                      @click="goPendingPage(1)"
                    >
                      下一页
                    </button>
                  </nav>
                </template>

                <div v-for="(warn, i) in pendingWarnings" :key="i" class="notice notice-warn sub-block">{{ warn }}</div>
                <div v-if="pending?.skipped?.length" class="notice notice-info sub-block">
                  有 {{ pending.skipped.length }} 条已完成任务没有形成成果。
                </div>
              </template>
            </div>
          </section>

          <!-- 交接点：只在真有成果要交时出现，说明这一步发生在哪 -->
          <div v-if="pendingEvents.length" class="gr-handover" aria-hidden="true">
            <span class="gr-handover-pill">生成下一轮时，才记进成长记录</span>
            <span class="gr-handover-rule" />
          </div>

          <!-- 已入账 -->
          <section class="gr-ledger gr-ledger--posted" aria-labelledby="gr-history-title">
            <header class="gr-ledger-head">
              <div class="gr-ledger-lead">
                <h2 id="gr-history-title" class="gr-ledger-title">已入账</h2>
              </div>
              <div class="btn-row gr-ledger-tools">
                <AppBadge
                  :text="`${history?.confirmed_count ?? 0} 次确认`"
                  :tone="(history?.confirmed_count ?? 0) ? 'ok' : 'muted'"
                />
                <AppBadge
                  v-if="history?.voided_count"
                  :text="`${history.voided_count} 条已作废`"
                  tone="warn"
                />
                <AppBadge
                  v-if="history"
                  :text="storeStatusText(history.status)"
                  :tone="storeStatusTone(history.status)"
                />
              </div>
            </header>

            <div class="gr-ledger-body">
              <div v-if="history && !history.writable" class="notice notice-warn flush-last">
                <strong>成长记录读不出来</strong>
                <div class="notice-body">{{ history.warning }}</div>
              </div>

              <template v-else>
                <div v-if="voidMessage" class="notice notice-ok">{{ voidMessage }}</div>

                <div v-if="!historyEntries.length" class="growth-empty">
                  <p class="growth-empty__title">还没有成长记录</p>
                </div>

                <template v-else>
                  <div class="growth-entries">
                    <article
                      v-for="row in historyPageRows"
                      :key="row.index"
                      class="growth-entry"
                      :class="{
                        'growth-entry--adjust': row.entry.kind === 'adjustment',
                        'is-open': isEntryExpanded(row.index),
                      }"
                    >
                      <button
                        class="growth-entry__head"
                        type="button"
                        :aria-expanded="isEntryExpanded(row.index)"
                        :aria-controls="`growth-entry-body-${row.index}`"
                        @click="toggleEntry(row.index)"
                      >
                        <span class="growth-entry__top">
                          <span class="growth-entry__date">{{ formatDateTime(row.entry.at) }}</span>
                          <span class="growth-entry__job">{{ row.entry.target_job || '未记录目标岗位' }}</span>
                          <svg
                            class="growth-entry__chevron"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                            aria-hidden="true"
                          >
                            <polyline points="6 9 12 15 18 9" />
                          </svg>
                        </span>
                        <span class="growth-entry__meta">
                          <AppBadge v-if="row.entry.kind === 'adjustment'" text="作废调整" tone="warn" />
                          <AppBadge v-if="row.entry.kind === 'verification'" text="补凭证" tone="ok" />
                          <AppBadge
                            v-if="row.entry.knowledge_base_status"
                            :text="knowledgeBadgeText(row.entry.knowledge_base_status)"
                            :tone="row.entry.knowledge_base_status === 'ok' ? 'ok' : 'warn'"
                          />
                          <span class="growth-entry__sum faint">{{ entrySummary(row.entry) }}</span>
                        </span>
                      </button>

                      <div
                        v-if="isEntryExpanded(row.index)"
                        :id="`growth-entry-body-${row.index}`"
                        class="growth-entry__body"
                      >
                        <p v-if="row.entry.kind === 'adjustment'" class="growth-entry__reason">
                          作废原因：{{ row.entry.reason || '（未记录）' }}
                          <template v-if="voidedTitles(row.entry)">；涉及成果：{{ voidedTitles(row.entry) }}</template>
                        </p>
                        <p v-if="row.entry.kind === 'verification'" class="growth-entry__reason">
                          补凭证验证：{{ changedSkillCount(row.entry) }} 项技能从自述改为有凭证。
                          原来那条成长记录原样保留，一条都没有改动。
                        </p>

                        <ul v-if="(row.entry.level_changes || []).length" class="growth-changes">
                          <li v-for="change in row.entry.level_changes" :key="change.skill">
                            <span class="growth-changes__skill">{{ change.skill }}</span>
                            <span class="growth-changes__path">
                              {{ change.from || '无' }} <span class="faint">→</span> {{ change.to }}
                            </span>
                            <span class="growth-changes__note">
                              {{ gateStatusText(change.gate_status) }}
                              <template v-if="change.source"> · 来源：{{ change.source }}</template>
                            </span>
                          </li>
                        </ul>
                        <p v-else class="faint growth-entry__quiet">
                          {{ row.entry.kind === 'adjustment' ? '作废后能力等级没有变化。' : '本次没有技能等级变化。' }}
                        </p>

                        <div v-if="(row.entry.events || []).length" class="growth-entry__events">
                          <div
                            v-for="(e, j) in (row.entry.events || [])"
                            :key="j"
                            class="history-event"
                            :class="{ 'history-event--voided': isVoided(row.entry, j) }"
                          >
                            <div class="history-event-head">
                              <strong>成果：{{ e.title }}</strong>
                              <AppBadge
                                :text="hasProof(e) ? '有凭证' : '自述完成'"
                                :tone="hasProof(e) ? 'ok' : 'warn'"
                              />
                              <AppBadge v-if="isVoided(row.entry, j)" text="已作废" tone="muted" />
                              <button
                                v-else
                                class="btn btn-sm btn-ghost history-void-btn"
                                type="button"
                                :disabled="running || voiding.busy"
                                @click="askVoid(row.index, j)"
                              >
                                作废
                              </button>
                            </div>
                            <p v-if="e.evidence" class="evidence-quote">{{ e.evidence }}</p>
                            <p v-if="e.note" class="faint history-event-note">心得：{{ e.note }}</p>

                            <div
                              v-if="voiding.entryIndex === row.index && voiding.eventIndex === j"
                              class="void-form"
                            >
                              <label :for="`void-reason-${row.index}-${j}`">
                                作废原因（会留在成长记录里，至少 2 个字）
                              </label>
                              <input
                                :id="`void-reason-${row.index}-${j}`"
                                v-model="voiding.reason"
                                type="text"
                                maxlength="200"
                                :disabled="voiding.busy"
                                placeholder="例如：证据填错了，实际没有完成这条成果"
                                @keyup.enter="confirmVoid"
                              />
                              <div v-if="voiding.error" class="void-form-error">{{ voiding.error }}</div>
                              <div class="btn-row">
                                <button class="btn" :disabled="voiding.busy" @click="cancelVoid">取消</button>
                                <button class="btn btn-danger" :disabled="voiding.busy" @click="confirmVoid">
                                  <span v-if="voiding.busy" class="spinner" />
                                  {{ voiding.busy ? '作废中…' : '确认作废' }}
                                </button>
                              </div>
                            </div>
                          </div>
                        </div>

                        <p v-if="(row.entry.blocked_upgrades || []).length" class="faint growth-entry__quiet">
                          被前置检查拦下 {{ row.entry.blocked_upgrades.length }} 项
                        </p>
                      </div>
                    </article>
                  </div>

                  <nav v-if="historyPageCount > 1" class="gr-pager" aria-label="已入账分页">
                    <button
                      class="btn btn-sm btn-ghost"
                      type="button"
                      :disabled="historyPage === 1"
                      @click="goHistoryPage(-1)"
                    >
                      上一页
                    </button>
                    <span class="gr-pager__pos">第 {{ historyPage }} / {{ historyPageCount }} 页</span>
                    <button
                      class="btn btn-sm btn-ghost"
                      type="button"
                      :disabled="historyPage === historyPageCount"
                      @click="goHistoryPage(1)"
                    >
                      下一页
                    </button>
                  </nav>

                  <p v-if="baseline" class="growth-baseline faint">
                    能力基线：{{ baselineLabel }} · 建立于 {{ formatDateTime(baseline.at) }}
                  </p>
                </template>
              </template>
            </div>
          </section>
      </div>

      <aside class="gr-rail" aria-label="本轮读数">
        <p class="gr-rail-eyebrow">本轮读数</p>
        <dl class="gr-rail-stats">
          <div>
            <dt>待交接</dt>
            <dd>{{ pendingEvents.length }}<span class="gr-rail-unit">条</span></dd>
          </div>
          <div>
            <dt>已入账</dt>
            <dd>{{ history?.confirmed_count ?? 0 }}<span class="gr-rail-unit">次</span></dd>
          </div>
          <div>
            <dt>最近能力</dt>
            <dd>{{ latestSkills.length }}<span class="gr-rail-unit">项</span></dd>
          </div>
        </dl>

        <div v-if="latestSkills.length" class="gr-rail-skills">
          <h3 class="gr-rail-title">最近一次确认的能力</h3>
          <div class="tags tags--tight">
            <span v-for="item in latestSkills" :key="item.skill" class="badge badge-ok">
              {{ item.skill }} · {{ item.level || '无' }}
            </span>
          </div>
        </div>

      </aside>
      </div>
    </template>

    <!-- 手动补录：低频入口收进右侧抽屉，交接待交接账簿留在视野里 -->
    <Teleport to="body">
      <div v-if="manual.open" class="reg-sheet-backdrop" @click.self="closeManual">
        <section
          class="reg-sheet growth-sheet"
          role="dialog"
          aria-modal="true"
          aria-labelledby="growth-sheet-title"
          @keydown.esc="closeManual"
        >
          <div class="reg-sheet__head">
            <div>
              <div class="reg-sheet__eyebrow">手动补录</div>
              <h2 id="growth-sheet-title">补录一条成果</h2>
            </div>
            <button class="reg-sheet__close" type="button" aria-label="关闭" @click="closeManual">×</button>
          </div>


          <div v-if="manual.error" class="notice notice-danger">{{ manual.error }}</div>

          <div class="reg-sheet__field">
            <label>成果类型</label>
            <div class="growth-sheet__kinds">
              <button
                class="btn"
                type="button"
                :class="manual.kind !== 'learning' && 'btn-ghost'"
                :aria-pressed="manual.kind === 'learning'"
                :disabled="manual.submitting"
                @click="manual.kind = 'learning'"
              >
                学习成果
              </button>
              <button
                class="btn"
                type="button"
                :class="manual.kind !== 'project' && 'btn-ghost'"
                :aria-pressed="manual.kind === 'project'"
                :disabled="manual.submitting"
                @click="manual.kind = 'project'"
              >
                项目成果
              </button>
            </div>
          </div>

          <div class="reg-sheet__field">
            <label for="manual-title">成果标题</label>
            <input
              id="manual-title"
              v-model="manual.title"
              type="text"
              :disabled="manual.submitting"
              placeholder="如：完成 Git 入门 / 搭建校园二手交易小项目"
            />
          </div>

          <div class="reg-sheet__field">
            <label for="manual-skills">
              涉及技能
              <small>多个用逗号或换行分隔，如：Git, Python</small>
            </label>
            <textarea
              id="manual-skills"
              v-model="manual.skills"
              class="growth-sheet__skills"
              :disabled="manual.submitting"
              rows="2"
              placeholder="Git，Docker"
            ></textarea>
          </div>

          <div class="reg-sheet__field">
            <label for="manual-evidence">
              成果凭证（选填）
              <small>链接、截图位置或产出说明；不填也能补录，只是按自述成果记账</small>
            </label>
            <textarea
              id="manual-evidence"
              v-model="manual.evidence"
              :disabled="manual.submitting"
              rows="4"
              placeholder="例如：https://github.com/你的账号/仓库 或 掌握了 clone/commit/push 并把项目推到了远程仓库"
            ></textarea>
          </div>

          <div class="reg-sheet__foot">
            <button
              class="btn btn-ghost"
              type="button"
              :disabled="manual.submitting"
              @click="resetManualFields"
            >
              清空
            </button>
            <button class="btn" type="button" :disabled="manual.submitting" @click="closeManual">关闭</button>
            <button
              class="btn btn-primary"
              type="button"
              :disabled="manual.submitting"
              @click="submitManualEvent"
            >
              <span v-if="manual.submitting" class="spinner" />
              {{ manual.submitting ? '提交中…' : '补录成果' }}
            </button>
          </div>
        </section>
      </div>
    </Teleport>
  </PageShell>
</template>
