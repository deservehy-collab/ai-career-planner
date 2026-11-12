<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { exportResumeDocx, exportResumePdf, getGrowthHistory, getPlan, listTasks } from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import { usePlansStore } from '../stores/plans'
import { saveBlob } from '../utils/download'
import {
  RESUME_VERSION,
  buildAutofillResume,
  emptyEducation,
  emptyEntry,
  emptyResume,
  normalizeResume,
  resumeFileName,
  resumeStats,
} from '../utils/resume'
import { readJson, writeJson } from '../utils/storage'

/**
 * 简历工作台。
 *
 * 内容来源只有两条：
 *   1. 当前档案已保存的事实（目标岗位、专业/年级、能力表、已完成任务、成长记录）；
 *   2. 用户自己在这里补填的内容。
 * 没有 AI 生成，也不会把没填过的姓名、公司、奖项编出来。
 *
 * 草稿按「账号 + 档案」存在本机 localStorage；切换档案不会串内容。
 * 导出直接下载文件：Word 走 `POST /api/resume/docx`，PDF 走 `POST /api/resume/pdf`，
 * 两者都只生成文件，不写业务数据。
 */

const plans = usePlansStore()
const auth = useAuthStore()

const EDUCATION_KEYS = [
  'school', 'major', 'degree', 'time', 'grade', 'gpa', 'ranking', 'courses',
]
const DRAFT_PREFIX = 'career-agent:resume'
const A4_WIDTH = 794
const A4_HEIGHT = 1123
const SAVE_DELAY = 350
const PHOTO_MAX_SOURCE = 8 * 1024 * 1024
const PHOTO_TARGET_WIDTH = 378
const PHOTO_TARGET_HEIGHT = 449

const SECTIONS = [
  { key: 'basic', no: '01', title: '基本信息', hint: '姓名、联系方式与照片' },
  { key: 'intent', no: '02', title: '求职意向', hint: '目标岗位、城市与薪资' },
  { key: 'education', no: '03', title: '教育经历', hint: '学校、成绩与课程' },
  { key: 'skills', no: '04', title: '专业技能', hint: '按熟练度分组' },
  { key: 'internships', no: '05', title: '实习 / 工作', hint: '单位、职责与成果' },
  { key: 'projects', no: '06', title: '项目经历', hint: '项目、职责与成果' },
  { key: 'campus', no: '07', title: '校园实践', hint: '学生工作、社团、竞赛' },
  { key: 'research', no: '08', title: '研究经历', hint: '课题、论文、实验室' },
  { key: 'certificates', no: '09', title: '证书与自评', hint: '证书、语言、兴趣与自评' },
]

const resume = ref(buildAutofillResume({}))
const loading = ref(false)
const loadError = ref('')
const actionError = ref('')
const photoError = ref('')
const toast = ref('')
const saving = ref(false)
const exporting = ref('')
const source = ref({ tasks: 0, growth: 0 })
const savedAt = ref(null)
const draftLoaded = ref(false)
const storageFailed = ref(false)
const activeSection = ref('basic')
const mobilePane = ref('edit')
const previewWidth = ref(720)
const sheetHeight = ref(A4_HEIGHT)

const stageRef = ref(null)
const sheetRef = ref(null)
let observer = null
let sheetObserver = null
let requestSeq = 0
let saveTimer = null
let toastTimer = null
let skipSave = false

const draftKey = computed(() => {
  const userId = auth.user?.id
  const planId = plans.activeId
  return userId && planId ? `${DRAFT_PREFIX}:${userId}:${planId}` : ''
})

const stats = computed(() => resumeStats(resume.value))
const canUse = computed(() => !!plans.activeId)
const canExport = computed(() => canUse.value && stats.value.filled > 0)

const sheetScale = computed(() => {
  const width = Math.max(320, previewWidth.value - 24)
  return Math.min(1, Math.max(0.38, width / A4_WIDTH))
})
const sheetStyle = computed(() => ({
  transform: `translateX(-50%) scale(${sheetScale.value})`,
}))
const sheetWrapStyle = computed(() => ({
  height: `${Math.round(sheetHeight.value * sheetScale.value)}px`,
}))
const pageCount = computed(() => Math.max(1, Math.ceil((sheetHeight.value - 1) / A4_HEIGHT)))
const pageCuts = computed(() => {
  const cuts = []
  for (let page = 1; page < pageCount.value; page += 1) cuts.push(page * A4_HEIGHT)
  return cuts
})

function degreeOf(education) {
  const ranks = [
    ['博士', 5], ['硕士', 4], ['研究生', 4], ['本科', 3], ['学士', 3],
    ['大专', 2], ['专科', 2], ['高职', 2], ['高中', 1], ['中专', 1],
  ]
  let best = ''
  let bestRank = 0
  ;(Array.isArray(education) ? education : []).forEach((item) => {
    const degree = str(item?.degree).trim()
    for (const [keyword, rank] of ranks) {
      if (degree.includes(keyword) && rank > bestRank) {
        best = degree
        bestRank = rank
      }
    }
  })
  return best
}

function ageOf(birth) {
  const match = str(birth).match(/(\d{4})\s*[-./年]?\s*(\d{1,2})?\s*[-./月]?\s*(\d{1,2})?/)
  if (!match) return ''
  const year = Number(match[1])
  const month = Number(match[2] || 1)
  const day = Number(match[3] || 1)
  if (year < 1900 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31) return ''
  const today = new Date()
  let age = today.getFullYear() - year
  if (today.getMonth() + 1 < month || (today.getMonth() + 1 === month && today.getDate() < day)) age -= 1
  return age >= 14 && age <= 80 ? String(age) : ''
}

const contactRows = computed(() => {
  const basic = resume.value.basic || {}
  const degree = degreeOf(resume.value.education)
  const rows = [
    [
      { label: '年龄', value: ageOf(basic.birth) },
      { label: '经验', value: basic.experience },
      { label: '手机', value: basic.phone },
      { label: '邮箱', value: basic.email },
    ],
    [
      { label: '性别', value: basic.gender },
      { label: '学历', value: degree },
      { label: '生日', value: basic.birth },
      { label: '现居', value: basic.city },
    ],
  ]
  return rows
    .map((row) => row.filter((cell) => str(cell.value).trim()))
    .filter((row) => row.length)
})

const intentRows = computed(() => {
  const intent = resume.value.intent || {}
  const first = [
    { label: '意向城市', value: intent.city },
    { label: '意向岗位', value: intent.targetJob },
    { label: '薪资要求', value: intent.salary },
    { label: '期望行业', value: intent.industry },
  ].filter((cell) => str(cell.value).trim())
  const second = [{ label: '求职状态', value: intent.availability }]
    .filter((cell) => str(cell.value).trim())
  return [first, second].filter((row) => row.length)
})

const skillLines = computed(() => {
  const certificates = visibleCertificates.value
  const groups = visibleSkills.value.map((group) => (
    group.label ? `${group.label} - ${group.items.join('、')}` : group.items.join('、')
  ))
  const languages = (resume.value.languages || []).map(str).filter(Boolean)
  const interests = (resume.value.interests || []).map(str).filter(Boolean)
  return [
    { label: '证书', value: certificates.join('、') },
    { label: '技能', value: groups.join('；') },
    { label: '语言', value: languages.join('、') },
    { label: '兴趣', value: interests.join('、') },
  ].filter((line) => line.value)
})

const summaryParagraphs = computed(() => str(resume.value.summary).split('\n')
  .map((line) => line.trim())
  .filter(Boolean)
  .map((line) => {
    const match = line.match(/^([^：:，。；\s]{1,10})[：:]\s*(.+)$/)
    return match
      ? { lead: match[1], text: match[2].trim() }
      : { lead: '', text: line }
  })
  .filter((item) => item.text))

const entrySections = computed(() => [
  { key: 'internships', title: '工作经历', numbered: true, entries: visibleEntries(resume.value.internships) },
  { key: 'projects', title: '项目经历', numbered: true, entries: visibleEntries(resume.value.projects) },
  { key: 'campus', title: '校园实践', numbered: true, entries: visibleEntries(resume.value.campus) },
  { key: 'research', title: '研究经历', numbered: false, entries: visibleEntries(resume.value.research) },
].filter((section) => section.entries.length))

const paperEmpty = computed(() => (
  !intentRows.value.length
  && !visibleEducation.value.length
  && !skillLines.value.length
  && !entrySections.value.length
  && !summaryParagraphs.value.length
))

const visibleEducation = computed(() => (resume.value.education || [])
  .filter((item) => EDUCATION_KEYS.some((field) => str(item?.[field]).trim())))

const visibleSkills = computed(() => (resume.value.skills || [])
  .map((group) => ({
    label: String(group?.label || '').trim(),
    items: (group?.items || []).map((item) => String(item || '').trim()).filter(Boolean),
  }))
  .filter((group) => group.items.length))

const visibleCertificates = computed(() => (resume.value.certificates || [])
  .map((item) => String(item || '').trim()).filter(Boolean))

const sourceLine = computed(() => {
  if (!plans.active) return '选择一份档案后，简历会自动带入已保存的信息。'
  const bits = [`档案「${plans.activeTitle}」`]
  if (source.value.tasks) bits.push(`已完成任务 ${source.value.tasks} 条`)
  if (source.value.growth) bits.push(`成长记录 ${source.value.growth} 条`)
  return draftLoaded.value
    ? `继续编辑本机草稿 · ${bits.join(' · ')}`
    : `已自动带入 ${bits.join(' · ')}`
})

const saveLabel = computed(() => {
  if (storageFailed.value) return '本机存不下草稿：请先导出 Word 备份'
  if (saving.value) return '保存中…'
  if (savedAt.value) return `已自动保存 ${clockOf(savedAt.value)}`
  return '改动会自动保存在本机'
})

const certificatesText = computed({
  get: () => (resume.value.certificates || []).join('\n'),
  set: (value) => {
    resume.value.certificates = String(value || '').split('\n')
  },
})

const languagesText = computed({
  get: () => (resume.value.languages || []).join('\n'),
  set: (value) => {
    resume.value.languages = String(value || '').split('\n')
  },
})

const interestsText = computed({
  get: () => (resume.value.interests || []).join('\n'),
  set: (value) => {
    resume.value.interests = String(value || '').split('\n')
  },
})

function clockOf(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function flash(message) {
  toast.value = message
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 2400)
}

function str(value) {
  return typeof value === 'string' ? value : ''
}

function coerceDraft(saved) {
  const base = emptyResume()
  const incoming = saved?.resume && typeof saved.resume === 'object' ? saved.resume : saved
  if (!incoming || typeof incoming !== 'object') return base

  const entries = (list) => (Array.isArray(list) ? list : [])
    .map((entry) => ({
      title: str(entry?.title),
      role: str(entry?.role),
      time: str(entry?.time),
      bullets: Array.isArray(entry?.bullets) ? entry.bullets.map(str) : [],
    }))

  return {
    ...base,
    basic: {
      ...base.basic,
      ...Object.fromEntries(Object.keys(base.basic).map((field) => [field, str(incoming.basic?.[field])])),
    },
    intent: {
      ...base.intent,
      ...Object.fromEntries(Object.keys(base.intent).map((field) => [field, str(incoming.intent?.[field])])),
    },
    education: Array.isArray(incoming.education) && incoming.education.length
      ? incoming.education.map((item) => ({
        ...emptyEducation(),
        ...Object.fromEntries(EDUCATION_KEYS.map((field) => [field, str(item?.[field])])),
      }))
      : [emptyEducation()],
    skills: (Array.isArray(incoming.skills) ? incoming.skills : []).map((group) => ({
      label: str(group?.label),
      items: Array.isArray(group?.items) ? group.items.map(str) : [],
    })),
    internships: entries(incoming.internships),
    projects: entries(incoming.projects),
    campus: entries(incoming.campus),
    research: entries(incoming.research),
    certificates: Array.isArray(incoming.certificates) ? incoming.certificates.map(str) : [],
    languages: Array.isArray(incoming.languages) ? incoming.languages.map(str) : [],
    interests: Array.isArray(incoming.interests) ? incoming.interests.map(str) : [],
    photo: str(incoming.photo),
    summary: str(incoming.summary),
  }
}

function applyResume(next) {
  skipSave = true
  resume.value = next
  nextTick(() => {
    skipSave = false
    measureSheet()
  })
}

function resetView() {
  applyResume(buildAutofillResume({}))
  source.value = { tasks: 0, growth: 0 }
  savedAt.value = null
  draftLoaded.value = false
  loadError.value = ''
}

function countGrowthEvents(growthData) {
  let total = 0
  for (const entry of growthData?.history || []) {
    const voided = new Set(Array.isArray(entry?.voided_event_indices) ? entry.voided_event_indices : [])
    const events = Array.isArray(entry?.events) ? entry.events : []
    events.forEach((_event, index) => {
      if (!voided.has(index)) total += 1
    })
  }
  return total
}

async function loadForPlan(planId, { ignoreDraft = false } = {}) {
  if (!planId) {
    resetView()
    return
  }
  const seq = ++requestSeq
  const epoch = plans.epoch
  const userId = auth.user?.id
  const key = draftKey.value
  loading.value = true
  loadError.value = ''

  try {
    const [plan, tasksData, growthData] = await Promise.all([
      getPlan(planId),
      listTasks({}, {}, planId).catch(() => null),
      getGrowthHistory({}, planId).catch(() => null),
    ])
    if (seq !== requestSeq || plans.activeId !== planId || plans.epoch !== epoch || auth.user?.id !== userId) {
      return
    }

    const taskList = Array.isArray(tasksData?.tasks) ? tasksData.tasks : []
    source.value = {
      tasks: taskList.filter((task) => task?.status === 'completed').length,
      growth: countGrowthEvents(growthData),
    }

    const saved = ignoreDraft ? null : readJson(key, null)
    if (saved && typeof saved === 'object') {
      applyResume(coerceDraft(saved))
      draftLoaded.value = true
      savedAt.value = typeof saved.savedAt === 'string' ? saved.savedAt : null
    } else {
      applyResume(buildAutofillResume({ plan, tasksData, growthData }))
      draftLoaded.value = false
      savedAt.value = null
    }
  } catch (exc) {
    if (seq !== requestSeq) return
    loadError.value = exc?.message || '档案内容没有读出来，请稍后再试。'
    resetView()
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

function persistDraft() {
  const key = draftKey.value
  if (!key) return
  saving.value = true
  const payload = { version: RESUME_VERSION, savedAt: new Date().toISOString(), resume: resume.value }
  const ok = writeJson(key, payload)
  storageFailed.value = !ok
  if (ok) savedAt.value = payload.savedAt
  window.setTimeout(() => { saving.value = false }, 250)
}

watch(resume, () => {
  if (skipSave || loading.value) return
  window.clearTimeout(saveTimer)
  saveTimer = window.setTimeout(persistDraft, SAVE_DELAY)
}, { deep: true })

watch(() => plans.activeId, (planId) => {
  if (planId) {
    loadForPlan(planId)
    nextTick(attachObserver)
  } else {
    requestSeq += 1
    resetView()
  }
}, { immediate: true })

onMounted(async () => {
  if (!plans.items.length) await plans.load()
  if (!plans.activeId && plans.items.length === 1) plans.select(plans.items[0].id)
  await nextTick()
  attachObserver()
  measureSheet()
})

onBeforeUnmount(() => {
  window.clearTimeout(saveTimer)
  window.clearTimeout(toastTimer)
  observer?.disconnect()
  sheetObserver?.disconnect()
  if (saveTimer) persistDraft()
})

function attachObserver() {
  observer?.disconnect()
  sheetObserver?.disconnect()
  const element = stageRef.value
  if (!element) return
  previewWidth.value = element.clientWidth || 720
  if (typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect?.width
    if (width) previewWidth.value = width
  })
  observer.observe(element)

  const sheet = sheetRef.value
  if (!sheet) return
  sheetObserver = new ResizeObserver(() => measureSheet())
  sheetObserver.observe(sheet)
}

function measureSheet() {
  const sheet = sheetRef.value
  if (!sheet) return
  const height = sheet.offsetHeight || A4_HEIGHT
  if (Math.abs(height - sheetHeight.value) > 1) sheetHeight.value = height
}

function onSelectPlan(event) {
  plans.select(event.target.value || null)
}

function toggleSection(key) {
  activeSection.value = activeSection.value === key ? '' : key
}

function sectionCount(key) {
  if (key === 'basic') {
    const fields = Object.values(resume.value.basic || {})
      .filter((value) => String(value || '').trim()).length
    return fields + (resume.value.photo ? 1 : 0)
  }
  if (key === 'intent') return Object.values(resume.value.intent || {}).filter((value) => String(value || '').trim()).length
  if (key === 'education') return visibleEducation.value.length
  if (key === 'skills') return visibleSkills.value.length
  if (key === 'certificates') {
    return visibleCertificates.value.length
      + (resume.value.languages || []).filter((item) => String(item || '').trim()).length
      + (resume.value.interests || []).filter((item) => String(item || '').trim()).length
      + (String(resume.value.summary || '').trim() ? 1 : 0)
  }
  return visibleEntries(resume.value[key]).length
}

function visibleEntries(list) {
  return (Array.isArray(list) ? list : []).filter((entry) => (
    String(entry?.title || '').trim()
    || String(entry?.role || '').trim()
    || String(entry?.time || '').trim()
    || (Array.isArray(entry?.bullets) && entry.bullets.some((bullet) => String(bullet || '').trim()))
  ))
}

function visibleBullets(entry) {
  return (Array.isArray(entry?.bullets) ? entry.bullets : [])
    .map((bullet) => String(bullet || '').trim())
    .filter(Boolean)
}

function educationLines(item) {
  const grade = str(item?.grade).trim()
  let degreeText = str(item?.degree).trim()
  if (grade) degreeText = degreeText ? `${degreeText}（${grade}）` : grade
  const gpa = str(item?.gpa).trim()
  const ranking = str(item?.ranking).trim()
  const score = gpa && ranking ? `${gpa}，${ranking}` : (gpa || ranking)
  return [
    { label: '学校名称', value: str(item?.school).trim() },
    { label: '专业名称', value: str(item?.major).trim() },
    { label: '学历', value: degreeText },
    { label: '学分绩点', value: score },
    { label: '核心课程', value: str(item?.courses).trim() },
  ].filter((line) => line.value)
}

function onPhotoPick(event) {
  const file = event.target?.files?.[0]
  if (event.target) event.target.value = ''
  if (!file) return
  photoError.value = ''
  if (!/^image\/(jpeg|png)$/.test(file.type)) {
    photoError.value = '只支持 JPG 或 PNG 图片。'
    return
  }
  if (file.size > PHOTO_MAX_SOURCE) {
    photoError.value = '照片超过 8MB，请换一张小一些的。'
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const image = new Image()
    image.onload = () => {
      try {
        const scale = Math.min(
          1,
          PHOTO_TARGET_WIDTH / image.width,
          PHOTO_TARGET_HEIGHT / image.height,
        )
        const width = Math.max(1, Math.round(image.width * scale))
        const height = Math.max(1, Math.round(image.height * scale))
        const canvas = document.createElement('canvas')
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d')
        if (!context) throw new Error('canvas unavailable')
        context.fillStyle = '#ffffff'
        context.fillRect(0, 0, width, height)
        context.drawImage(image, 0, 0, width, height)
        resume.value.photo = canvas.toDataURL('image/jpeg', 0.86)
      } catch {
        photoError.value = '照片处理失败，请换一张 JPG 或 PNG。'
      }
    }
    image.onerror = () => { photoError.value = '照片读不出来，请换一张 JPG 或 PNG。' }
    image.src = String(reader.result || '')
  }
  reader.onerror = () => { photoError.value = '照片读取失败，请重试。' }
  reader.readAsDataURL(file)
}

function removePhoto() {
  resume.value.photo = ''
  photoError.value = ''
}

function skillText(group) {
  return (group?.items || []).join('\n')
}

function setSkillText(group, value) {
  group.items = String(value || '').split('\n')
}

function bulletsText(entry) {
  return (entry?.bullets || []).join('\n')
}

function setBullets(entry, value) {
  entry.bullets = String(value || '').split('\n')
}

function addEducation() {
  resume.value.education.push(emptyEducation())
}

function removeEducation(index) {
  if (resume.value.education.length <= 1) {
    Object.assign(resume.value.education[0], emptyEducation())
    return
  }
  resume.value.education.splice(index, 1)
}

function addEntry(kind) {
  resume.value[kind].push(emptyEntry())
}

function removeEntry(kind, index) {
  resume.value[kind].splice(index, 1)
}

function addSkillGroup() {
  resume.value.skills.push({ label: '', items: [] })
}

function removeSkillGroup(index) {
  resume.value.skills.splice(index, 1)
}

async function rehydrate() {
  if (!plans.activeId) return
  const confirmed = window.confirm('重新从档案带入会覆盖当前手填内容，继续吗？')
  if (!confirmed) return
  await loadForPlan(plans.activeId, { ignoreDraft: true })
  flash('已按档案重新带入；刚才的草稿不再使用')
}

function exportPayload() {
  const payload = normalizeResume(resume.value)
  const filled = resumeStats(resume.value).filled - (payload.photo ? 1 : 0)
  if (filled <= 0) {
    actionError.value = '简历还是空的：先填写姓名、求职意向或任意一段经历再导出。'
    return null
  }
  return payload
}

async function onExport(format) {
  if (!canExport.value || exporting.value) return
  actionError.value = ''
  const payload = exportPayload()
  if (!payload) return
  exporting.value = format
  try {
    const blob = format === 'pdf'
      ? await exportResumePdf(payload)
      : await exportResumeDocx(payload)
    saveBlob(blob, resumeFileName(payload, format))
    flash(format === 'pdf' ? 'PDF 已导出' : 'Word 已导出')
  } catch (exc) {
    actionError.value = exc?.message || '导出没有成功，请稍后再试。'
  } finally {
    exporting.value = ''
  }
}
</script>

<template>
  <div class="rm-page">
    <header class="rm-bar">
      <div class="rm-bar-id">
        <p class="rm-kicker">CAREER DOCUMENT · 简历工作台</p>
        <div class="rm-title-row">
          <h1>简历制作</h1>
          <span class="rm-stamp">A4 · {{ pageCount }} 页</span>
        </div>
        <p class="rm-source">{{ sourceLine }}</p>
      </div>

      <div class="rm-bar-tools">
        <label class="rm-plan-picker">
          <span>档案</span>
          <select
            :value="plans.activeId || ''"
            :disabled="!plans.items.length"
            aria-label="选择简历来源档案"
            @change="onSelectPlan"
          >
            <option value="">选择档案</option>
            <option v-for="item in plans.items" :key="item.id" :value="item.id">
              {{ item.title || item.target_job }}
            </option>
          </select>
        </label>

        <div class="rm-stats" aria-live="polite">
          <span class="rm-stat">已填写 <strong>{{ stats.filled }}</strong> 项</span>
          <span class="rm-stat" :class="stats.pending ? 'is-pending' : 'is-done'">
            必填待补 <strong>{{ stats.pending }}</strong> 项
          </span>
        </div>

        <div class="rm-actions">
          <button
            type="button"
            class="btn rm-btn-ghost"
            :disabled="!canExport || !!exporting"
            @click="onExport('pdf')"
          >
            {{ exporting === 'pdf' ? '正在生成…' : '导出 PDF' }}
          </button>
          <button
            type="button"
            class="btn btn-primary rm-btn-primary"
            :disabled="!canExport || !!exporting"
            @click="onExport('docx')"
          >
            {{ exporting === 'docx' ? '正在生成…' : '导出 Word' }}
          </button>
        </div>
      </div>
    </header>

    <p v-if="toast" class="rm-toast" role="status">{{ toast }}</p>
    <p v-if="loadError || actionError" class="rm-alert" role="alert">
      {{ loadError || actionError }}
      <button v-if="loadError" type="button" class="rm-link-btn" @click="loadForPlan(plans.activeId)">重试</button>
    </p>

    <div v-if="plans.loading && !plans.items.length" class="rm-gate">
      <p class="rm-gate-kicker">读取档案</p>
      <h2>正在把档案内容取回来…</h2>
    </div>

    <div v-else-if="!plans.items.length" class="rm-gate">
      <p class="rm-gate-kicker">还没有可带入的档案</p>
      <h2>先建一份岗位档案，简历才有内容可写</h2>
      <p class="rm-gate-text">
        这里不会编造你的经历。建好档案并跑完岗位分析后，目标岗位、专业、技能和已完成的项目会自动出现在纸面上。
      </p>
      <RouterLink class="btn btn-primary" to="/analyze">去新建档案</RouterLink>
    </div>

    <div v-else-if="!plans.activeId" class="rm-gate">
      <p class="rm-gate-kicker">选择来源</p>
      <h2>先选一份档案</h2>
      <p class="rm-gate-text">每份档案对应一个求职方向，简历草稿也按档案分开保存，不会互相串内容。</p>
      <div class="rm-gate-actions">
        <select :value="plans.activeId || ''" aria-label="选择档案" @change="onSelectPlan">
          <option value="">选择档案</option>
          <option v-for="item in plans.items" :key="item.id" :value="item.id">
            {{ item.title || item.target_job }}
          </option>
        </select>
        <RouterLink class="btn btn-ghost" to="/home">管理档案</RouterLink>
      </div>
    </div>

    <div v-else class="rm-workspace" :class="`is-${mobilePane}`">
      <div class="rm-pane-switch" role="tablist" aria-label="预览与编辑">
        <button
          type="button"
          role="tab"
          :aria-selected="mobilePane === 'edit'"
          :class="{ 'is-active': mobilePane === 'edit' }"
          @click="mobilePane = 'edit'"
        >编辑</button>
        <button
          type="button"
          role="tab"
          :aria-selected="mobilePane === 'preview'"
          :class="{ 'is-active': mobilePane === 'preview' }"
          @click="mobilePane = 'preview'"
        >预览</button>
      </div>

      <section class="rm-preview" aria-label="A4 简历预览">
        <div class="rm-pane-head">
          <span class="rm-pane-title">纸面预览</span>
          <span class="rm-pane-note">210 × 297 mm · 内容超出自动续页</span>
        </div>

        <div ref="stageRef" class="rm-stage">
          <div class="rm-sheet-block">
            <p class="rm-sheet-tag">A4 · 共 {{ pageCount }} 页 · 虚线是分页位置</p>
            <div class="rm-sheet-wrap" :style="sheetWrapStyle">
              <article ref="sheetRef" class="rm-sheet" :style="sheetStyle">
                <span
                  v-for="cut in pageCuts"
                  :key="`cut-${cut}`"
                  class="rm-page-cut"
                  :style="{ top: `${cut}px` }"
                ></span>

                <header class="rm-paper-head">
                  <div class="rm-paper-head-main">
                    <div class="rm-paper-name" :class="{ 'is-empty': !resume.basic.name }">
                      {{ resume.basic.name || '你的姓名' }}
                    </div>
                    <div v-if="contactRows.length" class="rm-paper-contacts">
                      <p v-for="(row, index) in contactRows" :key="`contact-${index}`" class="rm-paper-contact">
                        <span v-for="cell in row" :key="cell.label" class="rm-paper-contact-item">
                          {{ cell.label }}：{{ cell.value }}
                        </span>
                      </p>
                    </div>
                    <p v-else class="rm-paper-contact is-empty">电话 · 邮箱 · 城市</p>
                  </div>
                  <div v-if="resume.photo" class="rm-paper-photo">
                    <img :src="resume.photo" alt="证件照" />
                  </div>
                </header>

                <p v-if="paperEmpty" class="rm-paper-hint">
                  在右侧填写内容，这里会按导出版式实时排版。
                </p>

                <section v-if="intentRows.length" class="rm-paper-sec rm-paper-sec--lead">
                  <h3>求职意向</h3>
                  <p
                    v-for="(row, index) in intentRows"
                    :key="`intent-${index}`"
                    class="rm-paper-flow rm-paper-intent"
                    :class="{ 'is-spread': row.length === 4 }"
                  >
                    <span v-for="cell in row" :key="cell.label" class="rm-paper-flow-item">
                      {{ cell.label }}：{{ cell.value }}
                    </span>
                  </p>
                </section>

                <section v-if="visibleEducation.length" class="rm-paper-sec">
                  <h3>教育经历</h3>
                  <div v-for="(item, index) in visibleEducation" :key="`edu-${index}`" class="rm-paper-entry">
                    <div class="rm-paper-line rm-paper-line--edu">
                      <span class="rm-paper-time">{{ item.time }}</span>
                      <strong class="rm-paper-edu-degree">
                        {{ item.degree ? `${item.degree}${item.grade ? `（${item.grade}）` : ''}` : item.school }}
                      </strong>
                    </div>
                    <p v-for="line in educationLines(item)" :key="line.label" class="rm-paper-detail">
                      <span class="rm-paper-detail-label">{{ line.label }}：</span>{{ line.value }}
                    </p>
                  </div>
                </section>

                <section v-for="section in entrySections" :key="section.key" class="rm-paper-sec">
                  <h3>{{ section.title }}</h3>
                  <div v-for="(entry, index) in section.entries" :key="`${section.key}-${index}`" class="rm-paper-entry">
                    <div class="rm-paper-line rm-paper-line--entry">
                      <span class="rm-paper-time">{{ entry.time }}</span>
                      <strong class="rm-paper-entry-title">{{ entry.title }}</strong>
                      <span class="rm-paper-role">{{ entry.role }}</span>
                    </div>
                    <ol v-if="section.numbered && visibleBullets(entry).length" class="rm-paper-list">
                      <li v-for="(bullet, bulletIndex) in visibleBullets(entry)" :key="bulletIndex">{{ bulletIndex + 1 }}. {{ bullet }}</li>
                    </ol>
                    <ul v-else-if="visibleBullets(entry).length" class="rm-paper-list rm-paper-list--plain">
                      <li v-for="(bullet, bulletIndex) in visibleBullets(entry)" :key="bulletIndex">{{ bullet }}</li>
                    </ul>
                  </div>
                </section>

                <section v-if="skillLines.length" class="rm-paper-sec">
                  <h3>技能证书</h3>
                  <p v-for="line in skillLines" :key="line.label" class="rm-paper-skill">
                    <strong>{{ line.label }}：</strong>{{ line.value }}
                  </p>
                </section>

                <section v-if="summaryParagraphs.length" class="rm-paper-sec">
                  <h3>自我评价</h3>
                  <p v-for="(item, index) in summaryParagraphs" :key="`summary-${index}`" class="rm-paper-summary">
                    <strong v-if="item.lead">{{ item.lead }}：</strong>{{ item.text }}
                  </p>
                </section>
              </article>
            </div>
          </div>

          <p v-if="pageCount > 1" class="rm-overflow-note">
            内容已经超过 A4 一页，导出 Word / PDF 时会按虚线位置自动续页。
          </p>
        </div>
      </section>

      <aside class="rm-editor" aria-label="简历内容编辑">
        <div class="rm-pane-head rm-editor-head">
          <span class="rm-pane-title">分区编辑</span>
          <span class="rm-pane-note">{{ saveLabel }}</span>
        </div>

        <div class="rm-sections">
          <section
            v-for="section in SECTIONS"
            :key="section.key"
            class="rm-sec"
            :class="{ 'is-open': activeSection === section.key }"
          >
            <button
              type="button"
              class="rm-sec-head"
              :aria-expanded="activeSection === section.key"
              :aria-controls="`rm-sec-${section.key}`"
              @click="toggleSection(section.key)"
            >
              <span class="rm-sec-no">{{ section.no }}</span>
              <span class="rm-sec-title">
                {{ section.title }}
                <small>{{ section.hint }}</small>
              </span>
              <span class="rm-sec-count">{{ sectionCount(section.key) }} 项</span>
              <svg class="rm-sec-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                <path d="M6 9l6 6 6-6" />
              </svg>
            </button>

            <div v-show="activeSection === section.key" :id="`rm-sec-${section.key}`" class="rm-sec-body">
              <template v-if="section.key === 'basic'">
                <div class="rm-photo-row">
                  <div class="rm-photo-frame" :class="{ 'is-empty': !resume.photo }">
                    <img v-if="resume.photo" :src="resume.photo" alt="证件照预览" />
                    <span v-else>未上传</span>
                  </div>
                  <div class="rm-photo-tools">
                    <span class="rm-photo-title">证件照（选填）</span>
                    <p class="rm-photo-note">只保存在这台电脑的草稿里，导出时嵌进文件，不会上传。</p>
                    <div class="rm-photo-actions">
                      <label class="btn btn-ghost rm-photo-pick">
                        选择照片
                        <input
                          type="file"
                          accept="image/jpeg,image/png"
                          aria-label="选择证件照"
                          @change="onPhotoPick"
                        />
                      </label>
                      <button
                        v-if="resume.photo"
                        type="button"
                        class="rm-link-btn"
                        @click="removePhoto"
                      >移除照片</button>
                    </div>
                    <p v-if="photoError" class="rm-photo-error" role="alert">{{ photoError }}</p>
                  </div>
                </div>
                <div class="rm-grid">
                  <label class="rm-field">
                    <span>姓名</span>
                    <input v-model="resume.basic.name" maxlength="60" placeholder="与证件一致" autocomplete="name" />
                  </label>
                  <label class="rm-field">
                    <span>性别</span>
                    <input v-model="resume.basic.gender" maxlength="100" placeholder="选填" />
                  </label>
                  <label class="rm-field">
                    <span>出生年月</span>
                    <input v-model="resume.basic.birth" maxlength="100" placeholder="如 2004.06" />
                  </label>
                  <label class="rm-field">
                    <span>工作经验</span>
                    <input v-model="resume.basic.experience" maxlength="100" placeholder="如 应届生 / 1 年" />
                  </label>
                  <label class="rm-field">
                    <span>电话</span>
                    <input v-model="resume.basic.phone" type="tel" maxlength="100" placeholder="校招常用手机号" autocomplete="tel" />
                  </label>
                  <label class="rm-field">
                    <span>邮箱</span>
                    <input v-model="resume.basic.email" type="email" maxlength="200" placeholder="name@example.com" autocomplete="email" />
                  </label>
                  <label class="rm-field">
                    <span>所在城市</span>
                    <input v-model="resume.basic.city" maxlength="100" placeholder="如 杭州" />
                  </label>
                </div>
              </template>

              <template v-else-if="section.key === 'intent'">
                <div class="rm-grid">
                  <label class="rm-field rm-field--wide">
                    <span>目标岗位</span>
                    <input v-model="resume.intent.targetJob" maxlength="200" placeholder="如 Java 后端开发" />
                  </label>
                  <label class="rm-field">
                    <span>期望城市</span>
                    <input v-model="resume.intent.city" maxlength="100" placeholder="如 杭州 / 上海" />
                  </label>
                  <label class="rm-field">
                    <span>求职状态</span>
                    <input v-model="resume.intent.availability" maxlength="100" placeholder="如 随时到岗 / 2027.07" />
                  </label>
                  <label class="rm-field">
                    <span>薪资要求</span>
                    <input v-model="resume.intent.salary" maxlength="100" placeholder="如 15-20K" />
                  </label>
                  <label class="rm-field">
                    <span>期望行业</span>
                    <input v-model="resume.intent.industry" maxlength="200" placeholder="如 互联网 / 制造业" />
                  </label>
                </div>
              </template>

              <template v-else-if="section.key === 'education'">
                <div v-for="(item, index) in resume.education" :key="`edu-edit-${index}`" class="rm-entry-card">
                  <div class="rm-entry-card-head">
                    <span>教育经历 {{ index + 1 }}</span>
                    <button type="button" class="rm-link-btn" @click="removeEducation(index)">移除</button>
                  </div>
                  <div class="rm-grid">
                    <label class="rm-field rm-field--wide">
                      <span>学校</span>
                      <input v-model="item.school" maxlength="200" placeholder="学校全称" />
                    </label>
                    <label class="rm-field">
                      <span>专业</span>
                      <input v-model="item.major" maxlength="200" placeholder="专业名称" />
                    </label>
                    <label class="rm-field">
                      <span>学历</span>
                      <input v-model="item.degree" maxlength="100" placeholder="本科 / 硕士" />
                    </label>
                    <label class="rm-field">
                      <span>在校时间</span>
                      <input v-model="item.time" maxlength="100" placeholder="2022.09 – 2026.06" />
                    </label>
                    <label class="rm-field">
                      <span>年级</span>
                      <input v-model="item.grade" maxlength="100" placeholder="如 大三" />
                    </label>
                    <label class="rm-field">
                      <span>GPA / 学分绩点</span>
                      <input v-model="item.gpa" maxlength="100" placeholder="如 3.7 / 4.0" />
                    </label>
                    <label class="rm-field">
                      <span>专业排名</span>
                      <input v-model="item.ranking" maxlength="100" placeholder="如 5 / 120" />
                    </label>
                    <label class="rm-field rm-field--wide">
                      <span>核心课程</span>
                      <input v-model="item.courses" maxlength="500" placeholder="如 数据结构、操作系统、数据库" />
                    </label>
                  </div>
                </div>
                <button type="button" class="rm-add" @click="addEducation">＋ 添加教育经历</button>
              </template>

              <template v-else-if="section.key === 'skills'">
                <div v-for="(group, index) in resume.skills" :key="`skill-edit-${index}`" class="rm-entry-card">
                  <div class="rm-entry-card-head">
                    <span>技能组 {{ index + 1 }}</span>
                    <button type="button" class="rm-link-btn" @click="removeSkillGroup(index)">移除</button>
                  </div>
                  <label class="rm-field">
                    <span>分组名</span>
                    <input v-model="group.label" maxlength="100" placeholder="如 熟练 / 掌握 / 了解" />
                  </label>
                  <label class="rm-field">
                    <span>技能（每行一个）</span>
                    <textarea
                      :value="skillText(group)"
                      rows="3"
                      maxlength="2000"
                      placeholder="Java&#10;Spring Boot"
                      @input="setSkillText(group, $event.target.value)"
                    />
                  </label>
                </div>
                <button type="button" class="rm-add" @click="addSkillGroup">＋ 添加技能组</button>
              </template>

              <template v-else-if="['internships', 'projects', 'campus', 'research'].includes(section.key)">
                <div v-for="(entry, index) in resume[section.key]" :key="`${section.key}-${index}`" class="rm-entry-card">
                  <div class="rm-entry-card-head">
                    <span>
                      {{ section.key === 'internships' ? '实习 / 工作' : section.key === 'projects' ? '项目' : section.key === 'campus' ? '校园实践' : '研究' }}
                      {{ index + 1 }}
                    </span>
                    <button type="button" class="rm-link-btn" @click="removeEntry(section.key, index)">移除</button>
                  </div>
                  <div class="rm-grid">
                    <label class="rm-field rm-field--wide">
                      <span>{{ section.key === 'campus' ? '组织 / 活动名称' : section.key === 'projects' ? '项目名称' : section.key === 'research' ? '课题 / 论文' : '单位名称' }}</span>
                      <input
                        v-model="entry.title"
                        maxlength="200"
                        :placeholder="section.key === 'internships' ? '公司 / 单位' : section.key === 'campus' ? '社团 / 学生会 / 竞赛' : section.key === 'research' ? '课题名称或论文题目' : '项目名称'"
                      />
                    </label>
                    <label class="rm-field">
                      <span>角色</span>
                      <input v-model="entry.role" maxlength="200" placeholder="如 后端开发" />
                    </label>
                    <label class="rm-field">
                      <span>时间</span>
                      <input v-model="entry.time" maxlength="100" placeholder="2026.03 – 2026.06" />
                    </label>
                  </div>
                  <label class="rm-field">
                    <span>职责与成果（每行一条）</span>
                    <textarea
                      :value="bulletsText(entry)"
                      rows="4"
                      maxlength="6000"
                      placeholder="负责什么&#10;做出了什么结果"
                      @input="setBullets(entry, $event.target.value)"
                    />
                  </label>
                </div>
                <button type="button" class="rm-add" @click="addEntry(section.key)">＋ 添加一条</button>
              </template>

              <template v-else-if="section.key === 'certificates'">
                <label class="rm-field">
                  <span>证书与荣誉（每行一条）</span>
                  <textarea
                    v-model="certificatesText"
                    rows="3"
                    maxlength="6000"
                    placeholder="大学英语六级&#10;校级一等奖学金"
                  />
                </label>
                <label class="rm-field">
                  <span>语言能力（每行一条）</span>
                  <textarea
                    v-model="languagesText"
                    rows="2"
                    maxlength="2000"
                    placeholder="英语 CET-6（580）&#10;普通话二级甲等"
                  />
                </label>
                <label class="rm-field">
                  <span>兴趣爱好（每行一条）</span>
                  <textarea
                    v-model="interestsText"
                    rows="2"
                    maxlength="2000"
                    placeholder="长跑&#10;开源社区"
                  />
                </label>
                <label class="rm-field">
                  <span>自我评价</span>
                  <textarea
                    v-model="resume.summary"
                    rows="4"
                    maxlength="3000"
                    placeholder="用两三句话说明方向匹配度与做事方式"
                  />
                </label>
              </template>
            </div>
          </section>
        </div>

        <footer class="rm-editor-foot">
          <button type="button" class="rm-link-btn" @click="rehydrate">重新从档案带入</button>
        </footer>
      </aside>
    </div>
  </div>
</template>
