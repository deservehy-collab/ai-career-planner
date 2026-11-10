/**
 * 岗位分析：发起 → 轮询 → 保存结果。
 *
 * 协议（backend/app.py）：
 *   POST /api/analyze-and-save → 202 {status:"queued", run_id, kind:"analyze_and_save"}
 *   GET  /api/runs/{run_id} → {status: queued|running|succeeded|failed, result, error, ...}
 *
 * ⚠️ 分析不再走只读 /api/analyze，而是走 /api/analyze-and-save：
 * 首次分析会顺手把产出的学习/项目计划存成第 1 轮任务（根治冷启动死锁）。
 * 因此 `result` 是**带包装的**：分析结果在 `result.analysis` 里，外层是 round_created /
 * warning 这些建轮次元信息。本 store 对外暴露的 `result` 已解包成投影结果本身，
 * 视图层按扁平字段读即可（见 applySuccess）。
 *
 * ⚠️ 绝不传 growth_events
 * ---------------------------------------
 * 分析链路的成长存储被隔离到临时目录（backend/analyze.py 的 _isolated_growth_store），
 * 即使传了成长事件、返回体里也可能写着 history_written=true，**实际上什么都没落盘**。
 * 成长一律走「提交任务完成 → 待处理成果 → 生成下一轮」那条路径，不从这里走。
 *
 * 分析失败时画像不受任何影响：画像在 profile store / localStorage 里，
 * 这里只读不写。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '../api/client'
import {
  getRun,
  startAnalyzeAndSave,
  startPlanAnalysis,
} from '../api/endpoints'
import { pollRun, RUN_STATUS } from '../composables/useRunPolling'
import { formatRunError } from '../utils/format'
import { usePlansStore } from './plans'
import { useProfileStore } from './profile'

/** 界面用的状态机：idle 之外都直接映射后端四态。 */
export const ANALYSIS_STATUS = {
  IDLE: 'idle',
  QUEUED: RUN_STATUS.QUEUED,
  RUNNING: RUN_STATUS.RUNNING,
  SUCCEEDED: RUN_STATUS.SUCCEEDED,
  FAILED: RUN_STATUS.FAILED,
}

function requestKey() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return `analysis-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export const useAnalysisStore = defineStore('analysis', () => {
  const runId = ref(null)
  const status = ref(ANALYSIS_STATUS.IDLE)
  const result = ref(null)
  /**
   * 建轮次元信息（结果外层）：是否只是预览、新轮次号、旧轮次号、warning、结局。
   * 预览时 `round_created=false` 且带着 `existing_round_no`。
   */
  const roundInfo = ref(null)
  const errorMessage = ref('')
  const errorKind = ref(null) // network | notfound | timeout | run_failed | http | null
  const startedAt = ref(null)
  const finishedAt = ref(null)
  /**
   * 后端回报的节点级进度（backend/jobs.py 的 TaskRecord.progress）。
   * 每项形如 {node, status, started_at, finished_at, duration_ms, error}，
   * status 为 running | done | failed | skipped | canceled。旧记录没有这个字段，
   * 一律按空数组处理 —— 界面会退回"整条轨"的说法，不猜单节点状态。
   */
  const progress = ref([])
  /** 当前正在执行的节点名；没有在跑节点时为 null。 */
  const currentNode = ref(null)

  // 本次分析的目标岗位，用于结果区标题
  const targetJob = ref('')
  // 本次分析归属的档案；新建档案时是刚创建的那份
  const planId = ref(null)

  let controller = null

  const isRunning = computed(
    () => status.value === ANALYSIS_STATUS.QUEUED || status.value === ANALYSIS_STATUS.RUNNING,
  )
  const hasResult = computed(() => !!result.value)
  const lastError = computed(() => errorMessage.value)

  /** 后端汇总方案（final_plan 由 build_final_plan 现算）。 */
  const finalPlan = computed(() => result.value?.final_plan ?? null)
  /** 岗位匹配结论（build_final_plan 里的 jd_match）。 */
  const jdMatch = computed(() => finalPlan.value?.jd_match ?? null)
  /**
   * 本次岗位要求的主依据：user_pasted | knowledge_base | model_only（backend/analyze.py）。
   * 旧结果里没有这个键时按"有没有粘原文"推断，不让界面因为缺字段显示"-"。
   */
  const requirementBasis = computed(() => {
    const raw = result.value?.requirement_basis
    if (raw) return raw
    return result.value?.user_jd_provided ? 'user_pasted' : 'model_only'
  })
  /**
   * 本次运行中云端自动降级到本地的提示（backend/analyze.py 的 llm_warnings）。
   * 空数组表示全程都按所选通道跑完；有内容就必须显示，不能让用户误以为
   * 云端成功了。
   */
  const llmWarnings = computed(() => result.value?.llm_warnings || [])

  /** 已有轮次时默认只预览：本次分析没有改动任务清单。 */
  const isPreview = computed(() => roundInfo.value?.preview === true)

  /** 本次分析没有另存/新建轮次时的提示（预览、部分失败都靠它说明真相）。 */
  const roundWarning = computed(() => roundInfo.value?.warning || '')

  /** 事后查询一次运行记录（不轮询），用于"稍后再查"。 */
  async function fetchOnce(id = runId.value) {
    if (!id) return null
    const record = await getRun(id, {}, planId.value)
    applyProgress(record)
    if (record.status === RUN_STATUS.SUCCEEDED) applySuccess(record)
    else if (record.status === RUN_STATUS.FAILED) applyFailure(record)
    else status.value = record.status
    return record
  }

  /** 把后端回报的节点进度接到界面状态上（旧记录缺字段时清空，不沿用上一次）。 */
  function applyProgress(record) {
    progress.value = Array.isArray(record?.progress) ? record.progress : []
    currentNode.value = record?.current_node ?? null
  }

  function applySuccess(record) {
    status.value = ANALYSIS_STATUS.SUCCEEDED
    applyProgress(record)
    // /api/analyze-and-save 把只读分析结果**包在 analysis 里**，外层是建轮次的元信息
    // （round_created / round_no / warning）；只读的 /api/analyze 则直接返回投影结果。
    // 这里必须解包，否则 user_skills / job_requirements / skill_gaps / job_evidence /
    // retrieval_status / growth_diff / final_plan 全都会读成 undefined —— 外层只有
    // learning_plan 与 project_plan 两个副本，所以现象是"学习和项目有、其他全空"。
    const raw = record.result ?? null
    const outer = raw?.analysis ? raw : null
    result.value = outer ? outer.analysis : raw
    roundInfo.value = outer
      ? {
          newPlan: !!outer.new_plan,
          roundCreated: !!outer.round_created,
          roundNo: outer.round_no ?? null,
          existingRoundNo: outer.existing_round_no ?? null,
          targetJob: outer.target_job || '',
          goal: outer.goal || null,
          warning: outer.warning || '',
          // 有旧轮次、又没新建，就是纯预览 —— 任务清单一个字节都没动
          preview: !outer.new_plan && !outer.round_created && outer.existing_round_no != null,
        }
      : null
    finishedAt.value = record.finished_at || new Date().toISOString()
    errorMessage.value = ''
    errorKind.value = null

    // 保存结构化能力表，供后续"提交任务完成"当 profile_skills 用。
    const profileStore = useProfileStore()
    const skills = result.value?.user_skills
    if (Array.isArray(skills) && skills.length) {
      profileStore.setLastSkills(skills, finishedAt.value)
    }
  }

  function applyFailure(record) {
    status.value = ANALYSIS_STATUS.FAILED
    applyProgress(record)
    result.value = null
    roundInfo.value = null
    finishedAt.value = record.finished_at || new Date().toISOString()
    // 后端把异常消息放在 error 字段（backend/jobs.py 的 TaskRecord），格式是
    // "ValueError: 具体原因"；类名不给使用者看（见 utils/format.js 的 formatRunError）
    errorMessage.value = formatRunError(record.error, '分析没有完成，这次没拿到具体原因。')
    errorKind.value = 'run_failed'
  }

  function describeError(error) {
    if (error instanceof ApiError) {
      if (error.isNetworkError) return { kind: 'network', message: error.message }
      if (error.isNotFound) {
        return {
          kind: 'notfound',
          message:
            '这次分析记录已经找不到了：档案可能已被删除，或服务重启后运行记录被清空。' +
            '如果档案还在，请重新发起分析。',
        }
      }
      if (error.status === 408) return { kind: 'timeout', message: error.message }
      return { kind: 'http', message: error.message }
    }
    if (error?.name === 'AbortError') return null // 主动取消，不算错误
    return { kind: 'http', message: error?.message || '出了点问题，请稍后再试。' }
  }

  /**
   * 发起分析并轮询到终态。
   *
   * @param {object} params
   * @param {object} params.profile    → UserProfileRequest 四字段
   * @param {string} params.targetJob  → AnalyzeRequest.target_job（必填）
   * @param {string} [params.jdText]   → AnalyzeRequest.jd_text：用户粘贴的招聘要求原文
   * @param {boolean} [params.newPlan] → 旧账号级路径：已有轮次时另存为新计划
   * @param {string} [params.planId]   → 档案级分析：写进这份档案
   * @param {boolean} [params.createArchive] → 先创建新档案，再跑首次分析
   * @param {'initial'|'preview'} [params.kind] → 档案内首次分析 / 只读预览
   */
  async function start({
    profile, targetJob: job, jdText: pastedJd = '', newPlan = false,
    planId: archiveId = null, createArchive = false, kind = 'initial',
  }) {
    cancel()

    runId.value = null
    status.value = ANALYSIS_STATUS.QUEUED
    result.value = null
    roundInfo.value = null
    errorMessage.value = ''
    errorKind.value = null
    startedAt.value = new Date().toISOString()
    finishedAt.value = null
    progress.value = []
    currentNode.value = null
    targetJob.value = job
    planId.value = archiveId

    const pasted = String(pastedJd || '').trim()

    controller = new AbortController()
    const { signal } = controller

    const body = {
      user_profile: profile,
      target_job: job,
      // 不传 growth_events（见文件头说明）
    }
    if (pasted) body.jd_text = pasted
    if (newPlan) body.new_plan = true

    try {
      let accepted
      const plans = usePlansStore()
      if (!archiveId && !createArchive) {
        // 旧账号级路径，供既有测试与迁移期使用
        accepted = await startAnalyzeAndSave(body)
      } else {
        if (!archiveId) {
          const created = await plans.create({
            targetJob: job, jdText: pasted, profile,
          })
          archiveId = created.plan_id
          planId.value = archiveId
        }
        accepted = await startPlanAnalysis(archiveId, {
          request_key: requestKey(),
          kind,
        })
      }
      runId.value = accepted.run_id
      status.value = accepted.status || ANALYSIS_STATUS.QUEUED

      const record = await pollRun(accepted.run_id, {
        signal,
        planId: archiveId || null,
        onTick: (tick) => {
          status.value = tick.status
          applyProgress(tick)
        },
      })

      if (record.status === RUN_STATUS.SUCCEEDED) applySuccess(record)
      else applyFailure(record)
      if (archiveId) {
        // 运行期间用户可能已切到别的档案：只刷新列表，不把人家拽回来。
        const stillHere = plans.activeId === null || plans.activeId === archiveId
        await plans.load()
        if (stillHere) plans.select(archiveId)
      }
    } catch (error) {
      const described = describeError(error)
      if (!described) {
        // 主动取消：回到未开始，不报错
        status.value = ANALYSIS_STATUS.IDLE
        return
      }
      status.value = ANALYSIS_STATUS.FAILED
      result.value = null
      roundInfo.value = null
      errorMessage.value = described.message
      errorKind.value = described.kind
      finishedAt.value = new Date().toISOString()
    } finally {
      controller = null
    }
  }

  /** 取消轮询（离开页面 / 重新发起时调用）。后台任务本身不受影响，只是不再等它。 */
  function cancel() {
    if (controller) {
      controller.abort()
      controller = null
    }
  }

  /** 清空本次结果与错误（不影响画像）。 */
  function reset() {
    cancel()
    runId.value = null
    status.value = ANALYSIS_STATUS.IDLE
    result.value = null
    roundInfo.value = null
    errorMessage.value = ''
    errorKind.value = null
    startedAt.value = null
    finishedAt.value = null
    progress.value = []
    currentNode.value = null
  }

  return {
    runId,
    status,
    result,
    roundInfo,
    errorMessage,
    errorKind,
    startedAt,
    finishedAt,
    progress,
    currentNode,
    targetJob,
    planId,
    isRunning,
    hasResult,
    lastError,
    finalPlan,
    jdMatch,
    requirementBasis,
    llmWarnings,
    isPreview,
    roundWarning,
    start,
    cancel,
    reset,
    fetchOnce,
  }
})