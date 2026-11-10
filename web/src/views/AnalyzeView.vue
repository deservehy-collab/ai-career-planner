<script setup>
import { computed, nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AgentPipeline from '../components/AgentPipeline.vue'
import AgentTag from '../components/AgentTag.vue'
import AppBadge from '../components/AppBadge.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import EvidenceProvenanceCard from '../components/EvidenceProvenanceCard.vue'
import GoalCard from '../components/GoalCard.vue'
import JdMatchCard from '../components/JdMatchCard.vue'
import JobProfileCard from '../components/JobProfileCard.vue'
import JobRequirementTable from '../components/JobRequirementTable.vue'
import LearningPlanList from '../components/LearningPlanList.vue'
import ProfileForm from '../components/ProfileForm.vue'
import ProjectPlanList from '../components/ProjectPlanList.vue'
import RunProgressCard from '../components/RunProgressCard.vue'
import SkillGapTable from '../components/SkillGapTable.vue'
import SkillTable from '../components/SkillTable.vue'

import { useAnalysisStore } from '../stores/analysis'
import { usePlansStore } from '../stores/plans'
import { JD_TEXT_LIMIT, useProfileStore } from '../stores/profile'
import { cancelPlanRun } from '../api/endpoints'
import { summarizeWorkflow } from '../utils/agents'
import {
  growthOutcomeText,
  growthOutcomeTone,
  levelTone,
  requirementBasisText,
  requirementBasisTone,
} from '../utils/enums'
import { joinList } from '../utils/format'

const profile = useProfileStore()
const analysis = useAnalysisStore()
const plans = usePlansStore()
const route = useRoute()
const router = useRouter()

/** 当前档案已有轮次时，「开始分析」只做预览；没有再建首轮。 */
const activeHasRound = computed(() => !!plans.active?.latest_round)
const startButtonLabel = computed(() => {
  if (analysis.isRunning) return '分析中…'
  if (plans.activeId) return activeHasRound.value ? '在档案内预览' : '完成首次分析'
  return '开始分析'
})

/** 招聘要求原文：存在 profile store 里，跟着 localStorage 一起留住，下次不用重粘。 */
const jdText = computed({
  get: () => profile.jdText,
  set: (value) => {
    profile.jdText = value
  },
})

// 注意：这里**不**在 onBeforeUnmount 里取消轮询。
// 分析结果存在 Pinia store 里、轮询由 store 驱动，切走页面时保持后台轮询，
// 回来时进度/结果都还在 —— 中途取消只会让一次几分钟的分析白等（后端仍在跑）。
// 只有「清空重来」会显式中止（analysis.reset()）。

const targetLabel = computed(() => {
  if (plans.activeId && !newPlanArmed.value) return '使用当前档案保存的招聘要求'
  return profile.hasJdText ? '你粘贴的招聘要求' : '没粘招聘要求（按相近岗位推断）'
})

/** 在已有档案里分析时，资料与招聘要求都用建档时的快照，本页表单不参与。 */
const usesFrozenSnapshot = computed(() => !!plans.activeId && !newPlanArmed.value)

/** 前端把能提前拦下的都拦掉，避免打到后端才报 422。 */
const blockingReason = computed(() => {
  if (usesFrozenSnapshot.value) return ''
  if (!profile.canAnalyze) return '请先填写「目标岗位」。'
  if (profile.jdTextOverflow) return '粘贴的内容超过 6000 字，请精简后再分析（太长 AI 也读不完）。'
  return ''
})

const result = computed(() => analysis.result)
const finalPlan = computed(() => analysis.finalPlan)
const requirementBasis = computed(() => analysis.requirementBasis)
/** 来源账与逐条引用：后端算好的前端只读部分（source_report），不在这里重算。 */
const sourceReport = computed(() => result.value?.source_report || null)
const citations = computed(() => sourceReport.value?.citations || null)
/** 只有收尾状态才出结局卡；进行中不提前宣布任何结论。 */
const goal = computed(() => finalPlan.value?.goal || null)
const showGoalCard = computed(
  () => goal.value?.status === 'achieved' || goal.value?.status === 'basis_insufficient',
)

/** 依据横幅的色调直接跟三档依据走，不和 goal.basis 混用。 */
const basisNoticeClass = computed(() => {
  const tone = requirementBasisTone(requirementBasis.value)
  if (tone === 'ok') return 'notice notice-ok'
  if (tone === 'warn') return 'notice notice-warn'
  return 'notice notice-danger'
})

/** 横幅里那句"这算什么依据"的补充说明。 */
const basisNote = computed(() => {
  if (requirementBasis.value === 'user_pasted') {
    return '岗位要求只从你粘贴的原话里得出，每条都能点开对照到具体句子。'
  }
  if (requirementBasis.value === 'knowledge_base') {
    return '没粘招聘要求：岗位要求按同类岗位资料整理（人工维护、有据可查），但不是这家公司的原话；具体门槛和达成结论仍以真实招聘要求为准。'
  }
  return '这次没有粘贴招聘要求，下面的岗位要求是 AI 凭常识给的 —— 只能当思路参考，别当硬性要求。'
})

// ── 预览 / 开始新计划 ──────────────────────────────────────────────────
const roundInfo = computed(() => analysis.roundInfo)
const isPreview = computed(() => analysis.isPreview)
const newPlanArmed = ref(false)


/** 一旦开始跑或已有结果，就切到两栏工作区。 */
const showWorkspace = computed(() => analysis.status !== 'idle' || !!result.value)

/**
 * 各节点的真实产出摘要（utils/agents.js）。
 * 没有结果时返回 null —— 流程轨据此保持中性格，不显示任何产出，也不标任何节点状态。
 */
const workflowSummary = computed(() => summarizeWorkflow(result.value))

/** 结论摘要的账本行：只填后端真给出来的字段，缺了就整行不出现。 */
const summaryRows = computed(() => {
  const plan = finalPlan.value
  if (!plan) return []
  const rows = []
  const job = plan.target_job || analysis.targetJob
  if (job) rows.push({ key: 'job', label: '目标岗位', value: job })
  if (typeof plan.learning_stage_count === 'number') {
    rows.push({ key: 'stages', label: '学习阶段', value: `${plan.learning_stage_count} 个` })
  }
  rows.push({
    key: 'projects',
    label: '推荐项目',
    value: `${plan.recommended_projects?.length ?? 0} 个`,
  })
  return rows
})

/** 报告角的印章：只映射后端四态，不另造阶段。 */
const statusStamp = computed(() => {
  const map = {
    queued: { text: '排队中', tone: 'run' },
    running: { text: '分析中', tone: 'run' },
    succeeded: { text: '已完成', tone: 'ok' },
    failed: { text: '未完成', tone: 'bad' },
  }
  return map[analysis.status] || { text: '待运行', tone: 'idle' }
})

// ── 成长节点（agents/growth_agent.py 的 growth_diff）────────────────────
const growthDiff = computed(() => result.value?.growth_diff || null)
const levelChanges = computed(() => growthDiff.value?.level_changes || [])
const blockedUpgrades = computed(() => growthDiff.value?.blocked_upgrades || [])
const growthWarnings = computed(() => growthDiff.value?.warnings || [])
const baselineSkills = computed(() => result.value?.user_skills_before || [])

// ── 岗位画像（agents/job_profile.py，由 job_node 使用/生成）──────────────
const jobProfile = computed(() => result.value?.job_profile || null)
const jobProfileOrigin = computed(() => result.value?.job_profile_origin || '')
const jobProfileWarnings = computed(() => result.value?.job_profile_warnings || [])

// ── 证据编号 → 证据面板定位 ────────────────────────────────────────────
const activeEvidenceRef = ref('')

function handleSelectRef(code) {
  if (!code) return
  if (activeEvidenceRef.value === code) {
    // 再点同一个编号：先清空再置回，让面板的高亮动画能重新触发
    activeEvidenceRef.value = ''
    nextTick(() => {
      activeEvidenceRef.value = code
    })
    return
  }
  activeEvidenceRef.value = code
}

function runAnalysis(newPlan = false) {
  if (blockingReason.value || analysis.isRunning) return

  activeEvidenceRef.value = ''
  newPlanArmed.value = false
  const job = profile.targetJob.trim()
  // 记下这份原文是给哪个岗位粘的，之后换岗位时界面会提醒原文可能已过期。
  profile.markJdBoundTo(job)
  const params = {
    profile: profile.toRequestProfile(),
    targetJob: job,
    jdText: jdText.value,
  }
  if (newPlan || !plans.activeId) {
    // 主动开始新岗位计划 = 新档案。旧档案的分析、任务、成长全部留在原处。
    analysis.start({ ...params, createArchive: true, kind: 'initial' })
    return
  }
  analysis.start({
    ...params,
    planId: plans.activeId,
    kind: activeHasRound.value ? 'preview' : 'initial',
  })
}

function handleStart() {
  runAnalysis(false)
}

function handleStartNewPlan() {
  if (blockingReason.value || analysis.isRunning) return
  newPlanArmed.value = true
}

function handleReset() {
  activeEvidenceRef.value = ''
  newPlanArmed.value = false
  analysis.reset()
}

// ── 停止分析：二选一（保留档案 / 连同档案一起删）──────────────────────
// 停止分析本身不再强制删档案：
// - 只停止：调用单次运行的中止接口，档案里的轮次/任务/成长一条不动；
// - 停止并删除：复用既有 DELETE /plans/{id}（级联删除，drop_plan 让迟到结果作废）。
// 两条路都不另造中断机制 —— 模型调用无法安全中断，但结果可以被判定为不落盘。
const confirmingStop = ref(false)
const stopping = ref(false)
/** '' | 'keep' | 'delete'：哪颗按钮在等，避免两颗一起转圈。 */
const stoppingAction = ref('')
const stopError = ref('')
const stopNotice = ref('')

/** 只有「正在跑 + 有档案归属」才停得了；旧账号级无档案路径不提供停止删除。 */
const canStop = computed(() => analysis.isRunning && !!analysis.planId)
const stopPlan = computed(() => {
  const id = analysis.planId
  if (!id) return null
  return plans.items.find((item) => item.id === id) || null
})
const stopPlanTitle = computed(
  () => stopPlan.value?.title || stopPlan.value?.target_job || analysis.targetJob || '这份档案',
)

function askStop() {
  if (!canStop.value || stopping.value) return
  confirmingStop.value = true
  stopError.value = ''
  stopNotice.value = ''
}

function cancelStop() {
  if (stopping.value) return
  confirmingStop.value = false
}

/** 只停止这次分析，档案与已有记录全部保留。 */
async function stopOnly() {
  const id = analysis.planId
  const runId = analysis.runId
  if (!id || !runId || stopping.value) return
  const title = stopPlanTitle.value
  stopping.value = true
  stoppingAction.value = 'keep'
  stopError.value = ''
  try {
    const outcome = await cancelPlanRun(id, runId)
    analysis.reset()
    confirmingStop.value = false
    stopNotice.value = outcome?.canceled
      ? `已停止这次分析，档案「${title}」原样保留：已有轮次、任务和成长记录都没动。`
      : `这次分析在你点停止前已经跑完，结果已保存到档案「${title}」；这里不再显示它。`
  } catch (error) {
    // 停止请求没送达：不 reset，保持"仍在跑"的真相，让用户能再点一次。
    stopError.value =
      `停止没有送达服务：${error?.message || '请稍后再试'}。`
      + '分析可能还在后台跑，可以再点一次。'
  } finally {
    stopping.value = false
    stoppingAction.value = ''
  }
}

async function confirmStop() {
  const id = analysis.planId
  if (!id || stopping.value) return
  stopping.value = true
  stoppingAction.value = 'delete'
  stopError.value = ''
  const title = stopPlanTitle.value
  // 先中止前端轮询并清空本次状态：迟到的回调不会再把已删档案选回来。
  analysis.reset()
  try {
    const result = await plans.remove(id)
    stopNotice.value = result.wasActive
      ? (result.nextId
          ? `已停止分析并删除档案「${title}」，已切换到剩下的档案。`
          : `已停止分析并删除档案「${title}」。现在没有档案了，可以重新填写画像开始新档案。`)
      : `已停止分析并删除档案「${title}」。当前所在档案没有变化。`
    confirmingStop.value = false
    // URL 还指着刚删掉的档案时一并换掉，刷新后不会落到不存在的 id 上。
    const query = { ...(route?.query || {}) }
    if (query.plan === id) {
      if (result.nextId) query.plan = result.nextId
      else delete query.plan
      router?.replace({ query })
    }
  } catch (error) {
    stopError.value =
      `删除档案失败：${error?.message || '请稍后再试'}。分析已停止等待，但档案没有删除，`
      + '可以到「我的档案」重试删除。'
    confirmingStop.value = false
  } finally {
    stopping.value = false
    stoppingAction.value = ''
  }
}
</script>

<template>
  <div class="an-page">
    <header class="an-mast">
      <div class="an-mast-grid">
        <div class="an-mast-main">
          <p class="an-kicker">Job Analysis · 岗位诊断</p>
          <h1 class="an-title">岗位分析</h1>
          <p class="an-lede">
            七个智能体依次读完你的画像和招聘原文，给出岗位要求、能力缺口与学习路线。
            每份档案独立结算，换岗位就换一份档案。
          </p>
        </div>

        <aside class="an-spec" aria-label="当前档案">
          <p class="an-spec-label">当前档案</p>
          <template v-if="plans.active">
            <p class="an-spec-title">{{ plans.active.title }}</p>
            <dl class="an-spec-list">
              <div>
                <dt>目标岗位</dt>
                <dd>{{ plans.active.target_job }}</dd>
              </div>
              <div>
                <dt>分析依据</dt>
                <dd>建档时的资料快照</dd>
              </div>
            </dl>
            <p class="an-spec-note">要分析另一个岗位，请点下面的「开始新档案」。</p>
          </template>
          <template v-else>
            <p class="an-spec-title an-spec-title--empty">还没有求职档案</p>
            <p class="an-spec-note">填好画像和招聘要求后点「开始分析」，会新建一份独立档案。</p>
          </template>
        </aside>
      </div>
    </header>

    <!-- 输入区：两张编号纸页，编号即阅读顺序 -->
    <section class="an-intake" aria-label="分析输入">
      <article class="an-sheet an-sheet--profile">
        <header class="an-sheet-head">
          <span class="an-sheet-no">01</span>
          <div class="an-sheet-title">
            <h2>个人画像</h2>
            <p>专业、年级、技能与项目 —— 分析的评估基准。</p>
          </div>
        </header>
        <ProfileForm />
      </article>

      <article class="an-sheet an-sheet--job">
        <header class="an-sheet-head">
          <span class="an-sheet-no">02</span>
          <div class="an-sheet-title">
            <h2>招聘原文</h2>
            <p>把「任职要求」整段粘进来，不用整理。</p>
          </div>
          <span class="an-sheet-tag">{{ targetLabel }}</span>
        </header>

        <div class="field">
          <textarea
            id="an-jd-text"
            v-model="jdText"
            rows="7"
            aria-label="招聘要求"
            placeholder="去招聘页把「任职要求」整段复制过来，不用整理。例：&#10;1、本科及以上学历，新闻学、汉语言文学相关专业优先&#10;2、具备扎实的文案撰写能力，能独立完成公众号推文&#10;3、熟练使用 PS、剪映等图文与视频工具"
          />
          <div class="btn-row btn-row--tight">
            <span class="faint field-count">{{ profile.jdTextLength }} / {{ JD_TEXT_LIMIT }} 字</span>
            <button v-if="profile.hasJdText" class="btn btn-sm" @click="jdText = ''">
              清空招聘要求
            </button>
          </div>
        </div>

        <div v-if="profile.jdTargetStale" class="notice notice-warn">
          这份招聘要求是给「{{ profile.jdTargetJob }}」粘的，当前目标岗位是「{{ profile.targetJob }}」。
          如果岗位换了，请重新粘贴对应岗位的「任职要求」；如果只是改了个说法，请把目标岗位改回去。
        </div>

        <div v-if="blockingReason" class="notice notice-warn">{{ blockingReason }}</div>

        <div class="an-actions">
          <button
            class="btn btn-primary"
            :disabled="!!blockingReason || analysis.isRunning"
            @click="handleStart"
          >
            <span v-if="analysis.isRunning" class="spinner" />
            {{ startButtonLabel }}
          </button>
          <button
            v-if="canStop"
            class="btn btn-danger an-stop"
            type="button"
            :disabled="stopping"
            @click="askStop"
          >
            停止分析
          </button>
          <button
            class="btn"
            :disabled="!!blockingReason || analysis.isRunning"
            @click="handleStartNewPlan"
          >
            开始新档案
          </button>
          <button v-if="analysis.hasResult || analysis.errorMessage" class="btn" @click="handleReset">
            清空结果
          </button>
        </div>

        <div v-if="stopNotice" class="notice notice-ok sub-block an-stop-notice">{{ stopNotice }}</div>
        <div v-if="stopError" class="notice notice-danger sub-block an-stop-error">{{ stopError }}</div>

        <div v-if="confirmingStop" class="notice notice-danger sub-block an-stop-confirm">
          <strong>停止「{{ stopPlanTitle }}」这次分析？</strong>
          <div class="notice-body">
            停止只放弃这次正在跑的分析，结果不再保存；档案里的
            {{ stopPlan?.counts?.rounds || 0 }} 轮分析、{{ stopPlan?.counts?.tasks || 0 }} 项任务、
            {{ stopPlan?.counts?.growth || 0 }} 条成长记录都<strong>原样保留</strong>。
            删掉整份档案是另一个选择，会连这些记录一起永久删除。
          </div>
          <div class="btn-row">
            <button class="btn" type="button" :disabled="stopping" @click="cancelStop">继续分析</button>
            <button
              class="btn btn-primary an-stop-keep"
              type="button"
              :disabled="stopping"
              @click="stopOnly"
            >
              <span v-if="stoppingAction === 'keep'" class="spinner" />
              {{ stoppingAction === 'keep' ? '停止中…' : '只停止分析（保留档案）' }}
            </button>
            <button
              class="btn btn-danger an-stop-delete"
              type="button"
              :disabled="stopping"
              @click="confirmStop"
            >
              <span v-if="stoppingAction === 'delete'" class="spinner" />
              {{ stoppingAction === 'delete' ? '删除中…' : '停止并删除整份档案' }}
            </button>
          </div>
          <p class="an-stop-danger-hint">
            选「停止并删除整份档案」会连同 {{ stopPlan?.counts?.rounds || 0 }} 轮分析、
            {{ stopPlan?.counts?.tasks || 0 }} 项任务、{{ stopPlan?.counts?.growth || 0 }} 条成长记录
            一起永久删除，无法撤销；账号、基础资料和其他档案不受影响。
          </p>
        </div>

        <div v-if="newPlanArmed" class="notice notice-warn sub-block">
          <strong>新建一份求职档案？</strong>
          <div class="notice-body">
            会重新跑一遍七个步骤（几十秒到几分钟），并把这次的画像、目标岗位和招聘要求冻结成<strong>一份独立档案</strong>。
            当前档案的轮次、任务与成长记录都不会被修改或删除。
          </div>
          <div class="btn-row">
            <button class="btn" :disabled="analysis.isRunning" @click="newPlanArmed = false">取消</button>
            <button class="btn btn-primary" :disabled="analysis.isRunning" @click="runAnalysis(true)">
              确认新建档案
            </button>
          </div>
        </div>
      </article>
    </section>

    <!-- 工作区：左侧流程轨，右侧诊断报告 -->
    <section v-if="showWorkspace" class="an-work" aria-label="分析工作区">
      <aside class="an-track">
        <AgentPipeline
          :summary="workflowSummary"
          :status="analysis.status"
          :progress="analysis.progress"
          :current-node="analysis.currentNode"
        />
      </aside>

      <div class="an-report">
        <header class="an-report-head">
          <div>
            <p class="an-report-kicker">Analysis Report</p>
            <h2>{{ analysis.targetJob || finalPlan?.target_job || '本次分析' }}</h2>
            <p class="an-report-sub">七个节点的产出按顺序排在下面，点左侧节点可直接跳到对应段落。</p>
          </div>
          <span class="an-stamp" :class="`is-${statusStamp.tone}`">{{ statusStamp.text }}</span>
        </header>

        <RunProgressCard
          :status="analysis.status"
          :started-at="analysis.startedAt"
          :finished-at="analysis.finishedAt"
          :error-message="analysis.errorMessage"
          :error-kind="analysis.errorKind"
          :progress="analysis.progress"
          :current-node="analysis.currentNode"
          title="分析进度"
        />

        <!--
          结果还没回来时的占位。后端只提供排队/运行中/成功/失败四态，**没有进度百分比**，
          节点级状态由进度卡和左侧流程轨按后端回报显示；这里只有灰条，不画假进度。
        -->
        <template v-if="analysis.isRunning && !result">
          <div class="sk-stack" aria-hidden="true">
            <div v-for="block in 3" :key="block" class="card sk-card">
              <div class="sk-bar sk-title" />
              <div class="sk-bar sk-line" />
              <div class="sk-bar sk-line" />
              <div class="sk-bar sk-line" />
              <div class="sk-bar sk-line" />
            </div>
          </div>
        </template>

        <template v-if="result">
          <!-- 轮次去向：预览 / 新建 / 另存必须分开说，别让用户以为预览已经改了任务 -->
          <div v-if="isPreview" class="notice notice-warn notice--tight">
            <strong>本次只是预览，任务清单没有改动</strong>
            <p class="notice-body">{{ analysis.roundWarning }}</p>
            <div class="btn-row">
              <button
                class="btn btn-sm"
                :disabled="analysis.isRunning"
                @click="runAnalysis(true)"
              >
                把这个结果另存为新计划（会重跑一遍）
              </button>
            </div>
          </div>
          <div v-else-if="roundInfo?.roundCreated" class="notice notice-ok notice--tight">
            <strong>
              {{ roundInfo.newPlan
                ? `已另存为第 ${roundInfo.roundNo} 轮计划`
                : `已保存为第 ${roundInfo.roundNo} 轮任务` }}
            </strong>
            <p class="notice-body">
              旧轮次与旧任务原样保留，去「学习与项目」页就能看到这一轮的任务。
            </p>
          </div>
          <div v-else-if="analysis.roundWarning" class="notice notice-warn notice--tight">
            <strong>任务清单没有变化</strong>
            <p class="notice-body">{{ analysis.roundWarning }}</p>
          </div>

          <!-- 本次岗位要求的依据来源：三档，决定下面几栏能不能当真 -->
          <div :class="basisNoticeClass" class="notice--tight">
            <strong>{{ requirementBasisText(requirementBasis) }}</strong>
            <p class="notice-body">{{ basisNote }}</p>
          </div>

          <!-- 来源账：每条岗位要求从哪来、各多少条，先摆在这里 -->
          <EvidenceProvenanceCard :report="sourceReport" :basis="requirementBasis" />

          <!-- 云端限流/超时/key 失效时的自动降级提示：必须让用户知道实际用了哪个模型 -->
          <div
            v-for="(warn, i) in analysis.llmWarnings"
            :key="`llm-${i}`"
            class="notice notice-warn notice--tight"
          >
            <strong>分析方式提示</strong>
            <p class="notice-body">{{ warn }}</p>
          </div>

          <!-- 结局：只在目标达成 / 依据不足无法确认时出现，进行中不渲染 -->
          <GoalCard
            v-if="showGoalCard"
            :goal="goal"
            :target-job="finalPlan?.target_job || analysis.targetJob"
          />
          <div v-if="showGoalCard && isPreview" class="notice notice-info block-note">
            以上是基于当前数据的预览结论，<strong>当前第 {{ roundInfo?.existingRoundNo }} 轮计划没有被改写</strong>。
            要把这次结论落到计划里，请点上面的「把这个结果另存为新计划」。
          </div>

          <!-- 汇总：final_plan 由图后的确定性汇总得出，不属于任一 Agent 节点 -->
          <section class="panel">
            <div class="panel-head">
              <h2>结论摘要</h2>
            </div>

            <dl v-if="summaryRows.length" class="ledger">
              <div v-for="row in summaryRows" :key="row.key">
                <dt>{{ row.label }}</dt>
                <dd>{{ row.value }}</dd>
              </div>
            </dl>

            <div v-if="finalPlan?.next_action" class="notice notice-info block-note">
              <strong>下一步：</strong>{{ finalPlan.next_action }}
            </div>

            <div class="grid grid-2 form-actions">
              <div>
                <h3 class="sub-title is-warn">最需要补的缺口</h3>
                <div class="tags">
                  <span v-for="gap in finalPlan?.top_gaps || []" :key="gap" class="badge badge-danger">
                    {{ gap }}
                  </span>
                  <span v-if="!(finalPlan?.top_gaps || []).length" class="faint">没有高优先级缺口</span>
                </div>
              </div>
              <div>
                <h3 class="sub-title is-ok">已满足的岗位要求</h3>
                <div class="tags">
                  <span v-for="skill in finalPlan?.already_satisfied || []" :key="skill" class="badge badge-ok">
                    {{ skill }}
                  </span>
                  <span v-if="!(finalPlan?.already_satisfied || []).length" class="faint">暂无</span>
                </div>
                <template v-if="(finalPlan?.needs_verification || []).length">
                  <h3 class="sub-title is-warn block-note">待验证（等级达标，缺项目/成长证据）</h3>
                  <div class="tags">
                    <AppBadge
                      v-for="skill in finalPlan.needs_verification"
                      :key="skill"
                      :text="skill"
                      tone="warn"
                    />
                  </div>
                </template>
              </div>
            </div>
          </section>

          <JdMatchCard :match="analysis.jdMatch" :basis="requirementBasis" />

          <div id="card-profile">
            <SkillTable
              :skills="result.user_skills || []"
              :strengths="result.user_strengths || []"
              :weaknesses="result.user_weaknesses || []"
              agent-node="profile"
            />
          </div>

          <!-- 成长追踪：growth_node 的产出（含能力基线），本轮无事件时如实说明 -->
          <div id="card-growth">
            <section class="card">
              <div class="card-head">
                <h2>成长追踪</h2>
                <div class="btn-row">
                  <AppBadge
                    v-if="growthDiff?.outcome"
                    :text="growthOutcomeText(growthDiff.outcome)"
                    :tone="growthOutcomeTone(growthDiff.outcome)"
                  />
                  <AgentTag node="growth" />
                </div>
              </div>
              <div class="card-body">
                <div v-if="!growthDiff" class="empty">这次结果里没有成长追踪数据。</div>
                <template v-else>
                  <div class="metric-strip">
                    <div class="metric">
                      <div class="metric-label">收到成长事件</div>
                      <div class="metric-value">{{ growthDiff.events_received ?? 0 }} 条</div>
                    </div>
                    <div class="metric">
                      <div class="metric-label">等级变化</div>
                      <div class="metric-value">{{ levelChanges.length }} 项</div>
                    </div>
                    <div class="metric">
                      <div class="metric-label">被前置检查拦下</div>
                      <div class="metric-value">{{ blockedUpgrades.length }} 项</div>
                    </div>
                    <div class="metric">
                      <div class="metric-label">能力基线条目</div>
                      <div class="metric-value">{{ baselineSkills.length }} 项</div>
                    </div>
                  </div>

                  <template v-if="baselineSkills.length">
                    <h3 class="sub-title block-note">合并成长记录后的能力基线</h3>
                    <div class="tags">
                      <span v-for="item in baselineSkills" :key="item.skill">
                        <AppBadge
                          :text="`${item.skill} · ${item.level || '无'}`"
                          :tone="levelTone(item.level)"
                        />
                      </span>
                    </div>
                  </template>

                  <template v-if="levelChanges.length">
                    <h3 class="sub-title block-note">技能等级变化</h3>
                    <ul class="plain-list muted">
                      <li v-for="item in levelChanges" :key="item.skill">
                        {{ item.skill }}：{{ item.from || '无' }} → {{ item.to }}
                        <span class="faint">（来源成果：{{ item.source }}）</span>
                      </li>
                    </ul>
                  </template>

                  <template v-if="blockedUpgrades.length">
                    <h3 class="sub-title block-note">被前置检查拦下的升级</h3>
                    <ul class="plain-list muted">
                      <li v-for="item in blockedUpgrades" :key="item.skill">
                        {{ item.skill }}（缺前置：{{ joinList(item.missing_prerequisites, '未给出') }}）
                      </li>
                    </ul>
                  </template>

                  <div v-for="(warn, i) in growthWarnings" :key="i" class="notice notice-warn block-note">
                    {{ warn }}
                  </div>
                </template>
              </div>
            </section>
          </div>

          <!-- 依据面板：EvidencePanel 自带 id="card-evidence" -->
          <EvidencePanel
            :evidence="result.job_evidence ?? null"
            :status="result.retrieval_status || ''"
            :diagnostics="result.retrieval_diagnostics ?? null"
            :requirements="result.job_requirements ?? null"
            :active-ref="activeEvidenceRef"
            @select="handleSelectRef"
          />

          <div id="card-requirements">
            <JobRequirementTable
              :requirements="result.job_requirements || []"
              agent-node="job"
              :active-ref="activeEvidenceRef"
              :basis="requirementBasis"
              :citations="citations"
              @select-ref="handleSelectRef"
            />

            <JobProfileCard
              :profile="jobProfile"
              :origin="jobProfileOrigin"
              :warnings="jobProfileWarnings"
              agent-node="job"
              :active-ref="activeEvidenceRef"
              @select-ref="handleSelectRef"
            />
          </div>

          <div id="card-gaps">
            <SkillGapTable :gaps="result.skill_gaps || []" agent-node="assessment" />
          </div>

          <div id="card-learning">
            <LearningPlanList :stages="result.learning_plan || []" agent-node="learning" />
          </div>

          <div id="card-projects">
            <ProjectPlanList :projects="result.project_plan || []" agent-node="project" />
          </div>
        </template>
      </div>
    </section>
  </div>
</template>
