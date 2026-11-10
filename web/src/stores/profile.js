/**
 * 用户画像 + 本次求职目标（会话内状态 + 本地持久化）。
 *
 * 两个语义不同的东西，**分开对待**：
 *
 *   1. 自述信息 `{major, grade, skills: string[], projects: string[]}`
 *      → 就是后端 `UserProfileRequest` 的四个字段，用户自己填的。
 *   2. 最近一次分析返回的**结构化能力表** `[{skill, level, evidence}]`
 *      → 用户填不出来，只能由分析产生，且是几分钟推理换来的，
 *        提交任务完成时当 `profile_skills` 回传，让"升级预览"和下一轮
 *        growth_node 用同一基线（见 backend/schemas.py 的说明）。删除需用户明确操作。
 *
 * 两者**都持久化**到 localStorage，外加本次的目标岗位与招聘要求原文。
 *
 * ⚠️ 这里改过一次口径，别再改回去
 * ------------------------------------------------
 * 早期要求"自述画像刷新即清空"，理由是怕上一次分析填的专业/岗位悄悄溜进下一次。
 * 但分析流程改成"粘贴招聘要求原文"之后，那条规则会让每次分析都得重填一遍画像、
 * 重粘一遍原文 —— 一天用两次就用不下去了。
 *
 * 防串味的职责改由界面上那颗**「清空自述画像」**按钮承担（`clearProfile`）：
 * 换求职方向时点一下，五个字段连同原文一起归零，而分析产物（能力表）不动。
 */

import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'

import { LAST_SKILLS_KEY, PROFILE_KEY, readJson, writeJson } from '../utils/storage'

function loadInitialSkills() {
  const saved = readJson(LAST_SKILLS_KEY, null)
  if (!saved || typeof saved !== 'object') return { skills: [], at: null }
  return {
    skills: Array.isArray(saved?.skills) ? saved.skills : [],
    at: typeof saved.at === 'string' ? saved.at : null,
  }
}

function loadInitialProfile() {
  const saved = readJson(PROFILE_KEY, null)
  if (!saved || typeof saved !== 'object') return {}
  return {
    major: typeof saved.major === 'string' ? saved.major : '',
    grade: typeof saved.grade === 'string' ? saved.grade : '',
    skills: Array.isArray(saved.skills) ? saved.skills : [],
    projects: Array.isArray(saved.projects) ? saved.projects : [],
    targetJob: typeof saved.targetJob === 'string' ? saved.targetJob : '',
    jdText: typeof saved.jdText === 'string' ? saved.jdText : '',
    jdTargetJob: typeof saved.jdTargetJob === 'string' ? saved.jdTargetJob : '',
  }
}

/** 招聘要求原文的字符上限，与后端 `config.JD_TEXT_MAX_CHARS` 同口径。 */
export const JD_TEXT_LIMIT = 6000

export const useProfileStore = defineStore('profile', () => {
  const initial = loadInitialProfile()

  // 自述画像：进页面就从本地读回来，不用每次重填
  const major = ref(initial.major ?? '')
  const grade = ref(initial.grade ?? '')
  const skills = ref(initial.skills ?? [])
  const projects = ref(initial.projects ?? [])
  const targetJob = ref(initial.targetJob ?? '')

  /** 用户当场粘贴的招聘要求原文；空 = 本次没有任何原文依据，岗位要求全由模型推断。 */
  const jdText = ref(initial.jdText ?? '')

  /** 这份原文是给哪个目标岗位粘的：换岗位后据此提醒原文可能已过期。 */
  const jdTargetJob = ref(initial.jdTargetJob ?? '')

  const initialSkills = loadInitialSkills()

  /** 最近一次分析得到的结构化能力表（与上面的自述技能是两回事）。 */
  const lastSkills = ref(initialSkills.skills)
  const lastSkillsAt = ref(initialSkills.at)

  /** 存储写入失败（配额满 / 隐私模式）时置为 true，界面上给一句提示。 */
  const storageFailed = ref(false)

  const hasSelfReportedSkills = computed(() => skills.value.length > 0)
  const hasLastSkills = computed(() => lastSkills.value.length > 0)

  /** 后端 AnalyzeRequest.target_job 要求 minLength 1 —— 这是硬性前置。 */
  const canAnalyze = computed(() => targetJob.value.trim().length > 0)

  const hasJdText = computed(() => jdText.value.trim().length > 0)

  const jdTextLength = computed(() => jdText.value.trim().length)

  /** 粘的原文是"岗位说明书"而不是"我的简历"——界面文案也跟着这条口径写。 */
  const jdTextOverflow = computed(() => jdTextLength.value > JD_TEXT_LIMIT)

  /** 粘过原文、之后又改了目标岗位：提醒用户原文多半已经不是这个岗位的了。 */
  const jdTargetStale = computed(() => {
    if (!hasJdText.value) return false
    return !!jdTargetJob.value && !!targetJob.value.trim()
      && jdTargetJob.value !== targetJob.value.trim()
  })

  /** 非阻塞提示：画像太空时分析仍可跑，但结果质量会差。 */
  const profileWarnings = computed(() => {
    const warnings = []
    if (!major.value.trim()) warnings.push('未填写专业')
    if (!grade.value.trim()) warnings.push('未填写年级')
    if (!skills.value.length) warnings.push('未填写自述技能')
    return warnings
  })

  function persistSkills() {
    storageFailed.value = !writeJson(LAST_SKILLS_KEY, {
      skills: lastSkills.value,
      at: lastSkillsAt.value,
    })
  }

  function persistProfile() {
    storageFailed.value = !writeJson(PROFILE_KEY, {
      major: major.value,
      grade: grade.value,
      skills: skills.value,
      projects: projects.value,
      targetJob: targetJob.value,
      jdText: jdText.value,
      jdTargetJob: jdTargetJob.value,
    })
  }

  /** 发起分析时记下"这份原文是给哪个岗位用的"，用于换岗位后的过期提醒。 */
  function markJdBoundTo(job) {
    jdTargetJob.value = hasJdText.value ? String(job ?? '').trim() : ''
  }

  /** 分析成功后由 analysis store 调用。 */
  function setLastSkills(list, at = new Date().toISOString()) {
    lastSkills.value = Array.isArray(list) ? list.filter((item) => item && item.skill) : []
    lastSkillsAt.value = at
    persistSkills()
  }

  /** 提交给后端的 UserProfileRequest（四字段，与 backend/schemas.py 一一对应）。 */
  function toRequestProfile() {
    return {
      major: major.value.trim(),
      grade: grade.value.trim(),
      skills: [...skills.value],
      projects: [...projects.value],
    }
  }

  /**
   * 清空自述画像与本次求职目标（原文一起清）；
   * **不清**结构化能力表 —— 那是分析产物，删除需用户明确操作。
   */
  function clearProfile() {
    major.value = ''
    grade.value = ''
    skills.value = []
    projects.value = []
    targetJob.value = ''
    jdText.value = ''
    jdTargetJob.value = ''
  }

  function clearLastSkills() {
    lastSkills.value = []
    lastSkillsAt.value = null
    persistSkills()
  }

  // 画像与分析产物都落盘；两边写入失败都给同一句界面提示。
  watch([major, grade, skills, projects, targetJob, jdText, jdTargetJob], persistProfile, { deep: true })
  watch(lastSkills, persistSkills, { deep: true })

  return {
    major,
    grade,
    skills,
    projects,
    targetJob,
    jdText,
    jdTargetJob,
    jdTargetStale,
    lastSkills,
    lastSkillsAt,
    storageFailed,
    hasSelfReportedSkills,
    hasLastSkills,
    hasJdText,
    jdTextLength,
    jdTextOverflow,
    canAnalyze,
    profileWarnings,
    setLastSkills,
    toRequestProfile,
    markJdBoundTo,
    clearProfile,
    clearLastSkills,
  }
})
