/**
 * 简历内容模型。
 *
 * 口径：
 *   1. 自动带入只搬已保存的事实（档案快照、当前能力表、已完成任务与成长记录），
 *      不补造姓名、电话、学校、公司、奖项；
 *   2. 用户改过的内容才是草稿的真身，自动带入只在没有草稿时发生；
 *   3. 导出前一律先 normalize：去掉全空条目与首尾空白，空栏目整段不出现在纸面上。
 */

export const RESUME_VERSION = 2

const BASIC_FIELDS = ['name', 'gender', 'birth', 'phone', 'email', 'city', 'experience']
const INTENT_FIELDS = ['targetJob', 'city', 'availability', 'salary', 'industry']
const EDUCATION_FIELDS = ['school', 'major', 'degree', 'time', 'grade', 'gpa', 'ranking', 'courses']

export function emptyEducation() {
  return {
    school: '', major: '', degree: '', time: '', grade: '', gpa: '', ranking: '', courses: '',
  }
}

export function emptyEntry() {
  return { title: '', role: '', time: '', bullets: [] }
}

export function emptyResume() {
  return {
    version: RESUME_VERSION,
    basic: {
      name: '', gender: '', birth: '', phone: '', email: '', city: '', experience: '',
    },
    intent: { targetJob: '', city: '', availability: '', salary: '', industry: '' },
    education: [emptyEducation()],
    skills: [],
    internships: [],
    projects: [],
    campus: [],
    research: [],
    certificates: [],
    languages: [],
    interests: [],
    photo: '',
    summary: '',
  }
}

const text = (value) => String(value ?? '').trim()

function clip(value, max) {
  const valueText = text(value)
  return valueText.length > max ? `${valueText.slice(0, max)}…` : valueText
}

function monthOf(value) {
  const raw = text(value)
  const match = raw.match(/^(\d{4})-(\d{2})/)
  return match ? `${match[1]}.${match[2]}` : ''
}

function projectKey(title) {
  return text(title).replace(/\s+/g, '').toLowerCase()
}

function eventKind(event) {
  return text(event?.kind || event?.type)
}

function eventBullets(event) {
  const bullets = []
  const evidence = clip(event?.evidence, 240)
  if (evidence) bullets.push(evidence)
  const skills = Array.isArray(event?.skills)
    ? event.skills.map(text).filter(Boolean).slice(0, 8)
    : []
  if (skills.length) bullets.push(`涉及技能：${skills.join('、')}`)
  return bullets
}

/** 成长记录里的项目成果，按时间从新到旧。 */
function growthProjects(growthData) {
  const rows = []
  const history = Array.isArray(growthData?.history) ? growthData.history : []
  history.forEach((entry) => {
    const voided = new Set(Array.isArray(entry?.voided_event_indices) ? entry.voided_event_indices : [])
    const events = Array.isArray(entry?.events) ? entry.events : []
    events.forEach((event, index) => {
      if (voided.has(index) || eventKind(event) !== 'project') return
      const title = text(event?.title)
      if (!title) return
      rows.push({
        at: text(event?.at || entry?.at),
        item: {
          title,
          role: '',
          time: monthOf(event?.at || entry?.at),
          bullets: eventBullets(event),
        },
      })
    })
  })
  return rows
    .sort((left, right) => right.at.localeCompare(left.at))
    .map((row) => row.item)
}

/** 成长记录还没有时，用任务清单里**已完成**的实战项目兜底。 */
function completedProjectTasks(tasksData) {
  const rows = Array.isArray(tasksData?.tasks) ? tasksData.tasks : []
  return rows
    .filter((task) => task?.kind === 'project' && task?.status === 'completed' && text(task?.title))
    .sort((left, right) => text(right?.completed_at).localeCompare(text(left?.completed_at)))
    .map((task) => ({
      title: text(task.title),
      role: '',
      time: monthOf(task.completed_at),
      bullets: eventBullets(task),
    }))
}

function mergeProjects(primary, fallback, limit = 6) {
  const seen = new Set()
  const merged = []
  for (const item of [...primary, ...fallback]) {
    const key = projectKey(item.title)
    if (!key || seen.has(key)) continue
    seen.add(key)
    merged.push(item)
    if (merged.length >= limit) break
  }
  return merged
}

function skillGroups(plan) {
  const groups = new Map()
  const known = new Set()

  const push = (label, skill) => {
    const name = text(skill)
    if (!name) return
    const key = name.toLowerCase()
    if (known.has(key)) return
    known.add(key)
    const groupLabel = text(label) || '其他'
    if (!groups.has(groupLabel)) groups.set(groupLabel, [])
    groups.get(groupLabel).push(name)
  }

  const current = Array.isArray(plan?.current_skills) ? plan.current_skills : []
  current.forEach((item) => push(item?.level, item?.skill))

  const snapshot = plan?.profile_snapshot || {}
  const selfReported = Array.isArray(snapshot?.skills) ? snapshot.skills : []
  selfReported.forEach((skill) => push('其他', skill))

  return [...groups.entries()]
    .map(([label, items]) => ({ label, items }))
    .filter((group) => group.items.length)
}

function autofillSummary(plan, skills, tasksData) {
  const parts = []
  const targetJob = text(plan?.target_job)
  if (targetJob) parts.push(`求职目标：${targetJob}。`)

  const top = skills.flatMap((group) => group.items).slice(0, 6)
  if (top.length) parts.push(`技能储备：${top.join('、')}。`)

  const done = Array.isArray(tasksData?.tasks)
    ? tasksData.tasks.filter((task) => task?.status === 'completed').length
    : 0
  if (done) parts.push(`已完成 ${done} 项学习与项目任务。`)

  return parts.join(' ')
}

/**
 * 从当前档案带入一版可编辑草稿。
 * 任务与成长记录可以缺省，缺省时对应栏目保持为空。
 */
export function buildAutofillResume({ plan = null, tasksData = null, growthData = null } = {}) {
  const resume = emptyResume()
  const snapshot = plan?.profile_snapshot || {}

  resume.intent.targetJob = text(plan?.target_job)

  resume.education = [{
    school: text(snapshot.school),
    major: text(snapshot.major),
    degree: text(snapshot.degree),
    time: text(snapshot.time),
    grade: text(snapshot.grade),
    gpa: text(snapshot.gpa),
    ranking: text(snapshot.ranking),
    courses: text(snapshot.courses),
  }]

  resume.languages = Array.isArray(snapshot.languages)
    ? snapshot.languages.map(text).filter(Boolean)
    : []
  resume.interests = Array.isArray(snapshot.interests)
    ? snapshot.interests.map(text).filter(Boolean)
    : []

  resume.skills = skillGroups(plan)
  resume.projects = mergeProjects(growthProjects(growthData), completedProjectTasks(tasksData))
  resume.summary = autofillSummary(plan, resume.skills, tasksData)

  return resume
}

function countFilled(resume) {
  let filled = 0
  for (const field of BASIC_FIELDS) if (text(resume?.basic?.[field])) filled += 1
  for (const field of INTENT_FIELDS) if (text(resume?.intent?.[field])) filled += 1

  const education = Array.isArray(resume?.education) ? resume.education : []
  education.forEach((item) => {
    for (const field of EDUCATION_FIELDS) if (text(item?.[field])) filled += 1
  })

  const skills = Array.isArray(resume?.skills) ? resume.skills : []
  skills.forEach((group) => {
    const items = Array.isArray(group?.items) ? group.items.filter((item) => text(item)) : []
    if (text(group?.label) || items.length) filled += 1
  })

  for (const key of ['internships', 'projects', 'campus', 'research']) {
    const entries = Array.isArray(resume?.[key]) ? resume[key] : []
    entries.forEach((entry) => {
      for (const field of ['title', 'role', 'time']) if (text(entry?.[field])) filled += 1
      const bullets = Array.isArray(entry?.bullets) ? entry.bullets.filter((item) => text(item)) : []
      if (bullets.length) filled += 1
    })
  }

  const certificates = Array.isArray(resume?.certificates)
    ? resume.certificates.filter((item) => text(item))
    : []
  filled += certificates.length
  for (const key of ['languages', 'interests']) {
    const items = Array.isArray(resume?.[key]) ? resume[key].filter((item) => text(item)) : []
    filled += items.length
  }
  if (text(resume?.photo)) filled += 1
  if (text(resume?.summary)) filled += 1

  return filled
}

/** 工具栏读数：已填写多少项，以及六项必填还差几项。 */
export function resumeStats(resume) {
  const firstEducation = Array.isArray(resume?.education) ? resume.education[0] : null
  const required = [
    resume?.basic?.name,
    resume?.basic?.phone,
    resume?.basic?.email,
    resume?.intent?.targetJob,
    firstEducation?.school,
    firstEducation?.major,
  ]
  return {
    filled: countFilled(resume),
    pending: required.filter((value) => !text(value)).length,
  }
}

function trimEducation(item) {
  return {
    school: text(item?.school),
    major: text(item?.major),
    degree: text(item?.degree),
    time: text(item?.time),
    grade: text(item?.grade),
    gpa: text(item?.gpa),
    ranking: text(item?.ranking),
    courses: text(item?.courses),
  }
}

function trimEntry(entry) {
  return {
    title: text(entry?.title),
    role: text(entry?.role),
    time: text(entry?.time),
    bullets: Array.isArray(entry?.bullets)
      ? entry.bullets.map(text).filter(Boolean)
      : [],
  }
}

function entryHasContent(entry) {
  return !!(entry.title || entry.role || entry.time || entry.bullets.length)
}

function educationHasContent(entry) {
  return EDUCATION_FIELDS.some((field) => entry[field])
}

/** 导出前的清洗：空字段归零，空条目与空技能组整条去掉。 */
export function normalizeResume(resume) {
  const source = resume || {}
  return {
    version: RESUME_VERSION,
    basic: Object.fromEntries(BASIC_FIELDS.map((field) => [field, text(source.basic?.[field])])),
    intent: Object.fromEntries(INTENT_FIELDS.map((field) => [field, text(source.intent?.[field])])),
    education: (Array.isArray(source.education) ? source.education : [])
      .map(trimEducation)
      .filter(educationHasContent),
    skills: (Array.isArray(source.skills) ? source.skills : [])
      .map((group) => ({
        label: text(group?.label),
        items: Array.isArray(group?.items) ? group.items.map(text).filter(Boolean) : [],
      }))
      .filter((group) => group.label || group.items.length)
      .map((group) => (group.label ? group : { ...group, label: '其他' })),
    internships: (Array.isArray(source.internships) ? source.internships : [])
      .map(trimEntry).filter(entryHasContent),
    projects: (Array.isArray(source.projects) ? source.projects : [])
      .map(trimEntry).filter(entryHasContent),
    campus: (Array.isArray(source.campus) ? source.campus : [])
      .map(trimEntry).filter(entryHasContent),
    research: (Array.isArray(source.research) ? source.research : [])
      .map(trimEntry).filter(entryHasContent),
    certificates: (Array.isArray(source.certificates) ? source.certificates : [])
      .map(text).filter(Boolean),
    languages: (Array.isArray(source.languages) ? source.languages : [])
      .map(text).filter(Boolean),
    interests: (Array.isArray(source.interests) ? source.interests : [])
      .map(text).filter(Boolean),
    photo: text(source.photo),
    summary: text(source.summary),
  }
}

/** 导出文件名：去掉 Windows 不允许的字符，空名字回退到"我的简历"。 */
export function resumeFileName(resume, extension = 'docx') {
  const base = text(resume?.basic?.name) || text(resume?.intent?.targetJob) || '我的简历'
  return `${base.replace(/[\\/:*?"<>|]/g, '').slice(0, 40)}.${extension}`
}
