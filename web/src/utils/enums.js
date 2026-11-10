/**
 * 后端枚举值 → 中文 + 视觉基调。
 * 取值全部来自实际代码（agents/ 与 backend/），不确定的一律落到"中性显示"，
 * 绝不猜测成一个看起来更漂亮的词。
 */

function lookup(map, value, fallback = null) {
  if (value === null || value === undefined || value === '') return fallback ?? '—'
  return map[value] ?? fallback ?? String(value)
}

// ── 画像表单选项（前端输入辅助，不是白名单）─────────────────────────────
// 专业按学科门类分组收录常见专业；不在名单里的走「其他（手动输入）」。
// 后端把专业当自由字符串处理，这份清单只为了让常用输入快一点 ——
// 系统必须对任何专业都能工作，不能离了这份清单就没法填。
export const MAJOR_GROUPS = [
  {
    label: '计算机 / 软件 / AI',
    options: ['计算机科学与技术', '软件工程', '人工智能', '数据科学与大数据技术', '网络工程', '信息安全', '物联网工程'],
  },
  {
    label: '电子信息 / 自动化',
    options: ['电子信息工程', '通信工程', '自动化', '电气工程及其自动化', '电子科学与技术', '微电子科学与工程'],
  },
  {
    label: '机械 / 土木 / 交通',
    options: ['机械设计制造及其自动化', '车辆工程', '土木工程', '工程管理', '建筑学', '交通运输'],
  },
  {
    label: '数理 / 化学 / 生物',
    options: ['数学与应用数学', '统计学', '物理学', '化学', '应用化学', '生物科学', '生物技术'],
  },
  {
    label: '经济 / 管理',
    options: ['经济学', '金融学', '国际经济与贸易', '会计学', '财务管理', '工商管理', '市场营销', '人力资源管理', '电子商务', '物流管理'],
  },
  {
    label: '法学 / 公共管理',
    options: ['法学', '知识产权', '社会工作', '行政管理', '劳动与社会保障'],
  },
  {
    label: '教育 / 体育',
    options: ['教育学', '学前教育', '小学教育', '体育教育', '运动训练'],
  },
  {
    label: '文学 / 语言 / 传媒',
    options: ['汉语言文学', '英语', '日语', '翻译', '新闻学', '传播学', '广告学', '网络与新媒体'],
  },
  {
    label: '医学 / 护理 / 药学',
    options: ['临床医学', '护理学', '药学', '医学检验技术', '康复治疗学', '口腔医学', '中医学'],
  },
  {
    label: '艺术 / 设计',
    options: ['视觉传达设计', '环境设计', '产品设计', '数字媒体艺术', '音乐学', '美术学'],
  },
  {
    label: '农 / 林 / 环境',
    options: ['农学', '园艺', '动物医学', '林学', '环境工程', '环境科学'],
  },
]

export const ALL_MAJORS = MAJOR_GROUPS.flatMap((group) => group.options)

/** 年级：覆盖本科 / 硕士 / 应届与毕业状态；后端同样按自由字符串处理。 */
export const GRADE_OPTIONS = ['大一', '大二', '大三', '大四', '研一', '研二', '研三', '应届毕业生', '已毕业']

// ── 岗位匹配总评（agents/jd_match.py）──────────────────────────────────
const JD_OVERALL = {
  satisfied: { text: '已满足', tone: 'ok' },
  partial: { text: '部分满足', tone: 'warn' },
  insufficient: { text: '证据不足，无法判定', tone: 'muted' },
}

export function jdOverallText(value) {
  return lookup(
    { satisfied: '已满足', partial: '部分满足', insufficient: '证据不足，无法判定' },
    value,
  )
}

export function jdOverallTone(value) {
  return JD_OVERALL[value]?.tone ?? 'muted'
}

// ── 技能缺口（agents/assessment_agent.py）──────────────────────────────
// status 只比等级；display_status 叠加证据：
//   已满足   —— 等级达标 + 同一技能有项目/成长记录
//   疑似具备 —— 靠另一个技能语义迁移，缺本项的直接证据
//   未验证   —— 等级达标但只有自述，没有项目/成长记录
const GAP_STATUS_TONE = {
  已满足: 'ok',
  疑似具备: 'warn',
  未验证: 'warn',
  部分满足: 'warn',
  明显不足: 'danger',
}

export function gapStatusTone(value) {
  return GAP_STATUS_TONE[value] ?? 'muted'
}

// 证据强度（assessment_agent.py VERIFICATION_*）
const VERIFICATION = {
  direct: { text: '有直接证据', tone: 'ok' },
  inferred: { text: '靠相近技能推断，没有这项技能的直接证据', tone: 'warn' },
  unverified: { text: '只有自述，还没有项目或成长记录', tone: 'warn' },
}

export function verificationText(value) {
  return VERIFICATION[value]?.text ?? (value ? String(value) : '—')
}

export function verificationTone(value) {
  return VERIFICATION[value]?.tone ?? 'muted'
}

// 支撑证据来自哪里（assessment_agent.py evidence_kind）
const EVIDENCE_KIND = {
  growth: '成长记录',
  project: '项目经历',
  self_report: '你自己填写的',
  unknown: '未标注依据',
  none: '无',
}

export function evidenceKindText(value) {
  return EVIDENCE_KIND[value] ?? (value ? String(value) : '—')
}

export function priorityTone(value) {
  return { 高: 'danger', 中: 'warn', 低: 'muted' }[value] ?? 'muted'
}

// ── 技能等级（agents/utils.py SKILL_LEVELS，四档固定）───────────────────
export const SKILL_LEVELS = ['入门', '初级', '中级', '高级']

export function levelTone(value) {
  return { 入门: 'muted', 初级: 'warn', 中级: 'primary', 高级: 'ok' }[value] ?? 'muted'
}

// ── 证据强度（agents/job_agent.py 的 _attach_evidence）─────────────────
// 真实取值只有三个：supported / partially_supported / model_supplement。
//   supported           —— 技能名在这句依据里逐字出现（能自己确认的最强依据）
//   partially_supported —— 只靠别名/语义对上，强度弱于前者
//   model_supplement    —— 依据里根本没有，这条要求是模型补充的（**不等于有依据**）
// 措辞刻意用「依据原句」而不是「招聘原文」：这句可能是你粘的原文，也可能是内置
// 资料摘要，两者由来源芯片（SOURCE_TYPE）分开标注，这里不替它背书。
// 文案与色调都从这里取，避免同一套词在 Text / Tone 两处各写一遍而漂移。
const EVIDENCE_STATUS = {
  supported: { text: '依据原句里有这句', tone: 'ok' },
  partially_supported: { text: '间接依据（说法相近）', tone: 'warn' },
  model_supplement: { text: 'AI 补充（要求里没写）', tone: 'warn' },
}

export function evidenceStatusText(value) {
  return EVIDENCE_STATUS[value]?.text ?? (value ? String(value) : '—')
}

export function evidenceStatusTone(value) {
  return EVIDENCE_STATUS[value]?.tone ?? 'muted'
}

// ── 判断依据来源（agents/job_agent.py）─────────────────────────────────
// importance_basis：profile_skill_importance（沿用岗位画像的人工评级）/ model_generated（模型给的）
const IMPORTANCE_BASIS = {
  profile_skill_importance: { text: '来自同类岗位资料的权重', tone: 'ok' },
  model_generated: { text: 'AI 判断', tone: 'muted' },
}

export function importanceBasisText(value) {
  return IMPORTANCE_BASIS[value]?.text ?? (value ? String(value) : '—')
}

export function importanceBasisTone(value) {
  return IMPORTANCE_BASIS[value]?.tone ?? 'muted'
}

// level_basis.origin 的三个真实取值：
//   jd_text       —— 被引用的那句依据里明确写了等级措辞（带 quote；
//                    这句可能是你粘的原文，也可能是内置资料摘要，见来源芯片）
//   profile_level —— 岗位画像里该技能标注的等级
//   model_inferred—— 模型自己推断的（**依据里没写等级**，不等于写了）
const LEVEL_ORIGIN = {
  jd_text: { text: '依据原句写明', tone: 'ok' },
  profile_level: { text: '同类岗位资料标注', tone: 'primary' },
  model_inferred: { text: 'AI 推断（要求里没写等级）', tone: 'muted' },
}

export function levelOriginText(value) {
  return LEVEL_ORIGIN[value]?.text ?? (value ? String(value) : '—')
}

export function levelOriginTone(value) {
  return LEVEL_ORIGIN[value]?.tone ?? 'muted'
}

// ── 岗位画像这条内容的来源（agents/job_profile.py SOURCE_*）─────────────
// 画像每条都带 source：jd 原话里有 / standard 内置知识库整理稿 / model 模型补的。
const PROFILE_SOURCE = {
  jd: { text: '你粘贴的要求里有', tone: 'ok' },
  standard: { text: '同类岗位的常见要求（人工整理）', tone: 'primary' },
  model: { text: 'AI 补充（要求里没写）', tone: 'warn' },
}

export function profileSourceText(value) {
  return PROFILE_SOURCE[value]?.text ?? (value ? String(value) : '—')
}

export function profileSourceTone(value) {
  return PROFILE_SOURCE[value]?.tone ?? 'muted'
}

// ── 整份画像从哪来（agents/job_agent.py profile_origin）────────────────
// cache 命中该用户此前生成的缓存；generated 本次现生成；
// knowledge_base 由内置画像直接拼出，没调模型；unavailable 本次没有画像。
const PROFILE_ORIGIN = {
  cache: { text: '沿用之前保存的岗位资料', tone: 'muted' },
  generated: { text: '本次生成', tone: 'primary' },
  knowledge_base: { text: '直接按同类岗位资料整理，没有调用 AI', tone: 'ok' },
  unavailable: { text: '本次没有岗位资料', tone: 'warn' },
}

export function profileOriginText(value) {
  return PROFILE_ORIGIN[value]?.text ?? (value ? String(value) : '—')
}

export function profileOriginTone(value) {
  return PROFILE_ORIGIN[value]?.tone ?? 'muted'
}

// ── 资料条目写的要求性质（knowledge/jobs/*.yaml 里 evidence.requirement_type）──
const REQUIREMENT_TYPE = {
  required: { text: '硬性要求', tone: 'danger' },
  preferred: { text: '优先考虑', tone: 'warn' },
  bonus: { text: '加分项', tone: 'muted' },
}

export function requirementTypeText(value) {
  return REQUIREMENT_TYPE[value]?.text ?? (value ? String(value) : '—')
}

export function requirementTypeTone(value) {
  return REQUIREMENT_TYPE[value]?.tone ?? 'muted'
}

// ── 一条岗位要求的来源（agents/source_attribution.py SOURCE_*）─────────
// 后端已把散在 evidence_status / importance_basis / level_basis.origin /
// provenance 里的线索收敛成这四值，并算好计数放进结果的 source_report。
// 前端只读这份结论，**不要再自己判一遍**（两处判定必然漂移）。
const SOURCE_TYPE = {
  user_jd: { text: '你粘贴的招聘要求', tone: 'ok' },
  official_data: { text: '公开资料摘要', tone: 'primary' },
  knowledge_base: { text: '同类岗位的常见要求', tone: 'warn' },
  model_inference: { text: 'AI 推断（无法回查依据）', tone: 'danger' },
}

export function sourceTypeText(value) {
  return SOURCE_TYPE[value]?.text ?? (value ? String(value) : '—')
}

export function sourceTypeTone(value) {
  return SOURCE_TYPE[value]?.tone ?? 'muted'
}

// ── 本次岗位要求的主依据（backend/analyze.py requirement_basis）─────────
// 三档：user_pasted 原话逐条可反查；knowledge_base 来自人工整理的内置岗位画像；
// model_only 纯模型推断。后两档都没有这家公司的原话，因此**不给匹配度百分比**。
const REQUIREMENT_BASIS = {
  user_pasted: { text: '依据：你粘贴的招聘要求', tone: 'ok' },
  knowledge_base: { text: '依据：同类岗位的常见要求（人工整理）', tone: 'warn' },
  model_only: { text: '依据不足：以下由 AI 按相近岗位推断，没有招聘要求可核对', tone: 'danger' },
}

export function requirementBasisText(value) {
  return REQUIREMENT_BASIS[value]?.text ?? (value ? String(value) : '—')
}

export function requirementBasisTone(value) {
  return REQUIREMENT_BASIS[value]?.tone ?? 'muted'
}

/** 有没有"能逐条对照原话"的依据（只有用户粘贴的原文算）。 */
export function hasSolidBasis(value) {
  return value === 'user_pasted'
}

// ── 依据整理状态（agents/retrieval_agent.py STATUS_*）───────────────────
const RETRIEVAL_STATUS = {
  user_pasted: '按你粘贴的要求',
  official_digest: '按同类岗位资料',
  no_source: '没有可参考的招聘要求',
}

export function retrievalStatusText(value) {
  return lookup(RETRIEVAL_STATUS, value)
}

export function retrievalStatusTone(value) {
  return { user_pasted: 'ok', official_digest: 'primary', no_source: 'warn' }[value] ?? 'muted'
}

// retrieval_diagnostics["reason"] 绝大多数是一句完整中文，直接透出；
// 只有少数机器标识需要翻译，不能把英文标识摆给用户看。
const RETRIEVAL_REASON_TOKENS = {
  user_jd_empty: '你粘贴的内容里没读出可用的要求句子（可能只粘了标题），下面的岗位要求是 AI 按相近岗位推测的',
}

export function retrievalReasonText(value) {
  const text = String(value ?? '').trim()
  if (!text) return ''
  return RETRIEVAL_REASON_TOKENS[text] ?? text
}

// ── 任务（agents/loop_store.py）────────────────────────────────────────
export function taskKindText(value) {
  return lookup({ learning: '学习任务', project: '项目任务' }, value)
}

export function taskStatusText(value) {
  return lookup({ pending: '待完成', completed: '已完成' }, value)
}

// ── 提交任务完成的整体结论（agents/growth_loop.py LOOP_*）───────────────
// ⚠️ 这四个值描述的是"整次提交"的结果，**不等于**技能真的升级了 —— 升级只是预览。
const LOOP_STATUS = {
  completed_upgraded: { text: '任务已完成，有升级预览（还没记进成长记录）', tone: 'primary' },
  completed_no_upgrade: { text: '任务已完成，这次没有升级预览', tone: 'muted' },
  completed_no_growth_event: { text: '任务已完成，但这次不算成长记录', tone: 'warn' },
  completion_failed: { text: '任务完成记录没有保存', tone: 'danger' },
}

export function loopStatusText(value) {
  return LOOP_STATUS[value]?.text ?? (value ? String(value) : '—')
}

export function loopStatusTone(value) {
  return LOOP_STATUS[value]?.tone ?? 'muted'
}

// ── 任务完成记录本身的写入状态（loop_store COMPLETE_*）─────────────────
const COMPLETION_STATUS = {
  completed: { text: '已记进任务记录', tone: 'ok' },
  already_completed: { text: '此前已完成，这次没有重复记录', tone: 'muted' },
  unknown_task: { text: '找不到这个任务', tone: 'danger' },
  invalid_task_id: { text: '任务编号不对', tone: 'danger' },
  invalid_evidence: { text: '证据填写不符合要求', tone: 'danger' },
}

export function completionStatusText(value) {
  return COMPLETION_STATUS[value]?.text ?? (value ? String(value) : '—')
}

export function completionStatusTone(value) {
  return COMPLETION_STATUS[value]?.tone ?? 'muted'
}

// ── 成长成果的构造状态（growth_loop EVENT_*）───────────────────────────
// 构不成果 ≠ 任务没完成；只是这条任务无法产生技能升级的依据。
const EVENT_STATUS = {
  ok: { text: '已记为成长记录', tone: 'ok' },
  no_evidence: { text: '不算成长记录：没有填写证据', tone: 'warn' },
  no_skills: { text: '不算成长记录：任务没有关联技能', tone: 'warn' },
  unknown_kind: { text: '不算成长记录：任务类型不对', tone: 'danger' },
  invalid_task: { text: '不算成长记录：任务数据有问题', tone: 'danger' },
}

export function eventStatusText(value) {
  return EVENT_STATUS[value]?.text ?? (value ? String(value) : '—')
}

export function eventStatusTone(value) {
  return EVENT_STATUS[value]?.tone ?? 'muted'
}

// ── 升级预览所用的基线（agents/growth_loop.py）─────────────────────────
//   profile_merged —— 历史 ∪ 本次画像，与 growth_node 完全一致（准）
//   history_only   —— 本次拿不到画像，只按历史算，**比真实判定更严格**
const BASELINE_SOURCE = {
  profile_merged: { text: '成长记录 + 当前画像（和正式判定用同一份数据）', tone: 'ok' },
  history_only: { text: '只看成长记录（没算进当前画像，判定偏严格）', tone: 'warn' },
}

export function baselineSourceText(value) {
  return BASELINE_SOURCE[value]?.text ?? (value ? String(value) : '—')
}

export function baselineSourceTone(value) {
  return BASELINE_SOURCE[value]?.tone ?? 'muted'
}

// ── 技能知识库加载状态（growth_rules KB_STATUS_*）──────────────────────
export function knowledgeBaseStatusText(value) {
  return lookup({ ok: '正常', load_failed: '读不出来（前置检查没有执行）' }, value)
}

export function knowledgeBaseStatusTone(value) {
  return { ok: 'ok', load_failed: 'warn' }[value] ?? 'muted'
}

// ── 运行状态（backend/jobs.py）────────────────────────────────────────
export function runStatusText(value) {
  return lookup(
    { queued: '排队中', running: '执行中', succeeded: '已完成', failed: '失败' },
    value,
  )
}

export function runStatusTone(value) {
  return { queued: 'muted', running: 'primary', succeeded: 'ok', failed: 'danger' }[value] ?? 'muted'
}

// ── 存储状态（agents/*_store.py）──────────────────────────────────────
export function storeStatusText(value) {
  return lookup(
    {
      ok: '正常',
      empty: '还没有数据（第一次使用）',
      corrupt: '数据损坏（原记录还在，没有被覆盖）',
      unreadable: '暂时读不出来',
      schema_mismatch: '旧版本保存的清单，暂时读不了',
    },
    value,
  )
}

export function storeStatusTone(value) {
  return { ok: 'ok', empty: 'muted', corrupt: 'danger', unreadable: 'danger', schema_mismatch: 'warn' }[value] ?? 'muted'
}

// ── 目标结局（agents/goal.py）──────────────────────────────────────────
// achieved / in_progress / basis_insufficient。
// basis_insufficient 不是失败，也不能说成"已完成"：能力表达标了，但岗位要求
// 本身没有可靠依据，任何达成结论都不成立。
const GOAL_STATUS = {
  achieved: { text: '目标已达成', tone: 'ok' },
  in_progress: { text: '尚未达成', tone: 'muted' },
  basis_insufficient: { text: '依据不足，无法确认达成', tone: 'warn' },
}

export function goalStatusText(value) {
  return GOAL_STATUS[value]?.text ?? (value ? String(value) : '—')
}

export function goalStatusTone(value) {
  return GOAL_STATUS[value]?.tone ?? 'muted'
}

const GOAL_BASIS = {
  user_pasted: '你粘贴的招聘要求',
  knowledge_base: '同类岗位的常见要求（人工整理）',
  model_only: 'AI 推断（没有招聘要求或岗位资料可依）',
}

export function goalBasisText(value) {
  return GOAL_BASIS[value] ?? (value ? String(value) : '—')
}

// ── 成长结论 / 前置门禁（agents/growth_agent.py、growth_rules.py）──────
// F1 不在界面上使用，但一次建好，后续页面直接复用，避免两处写法漂移。
export function growthOutcomeText(value) {
  return lookup(
    {
      upgraded: '有技能等级提升',
      no_upgrade: '有新的成果，但等级没有变化',
      no_events: '本轮没有新的成果',
      all_duplicates: '这些成果之前已经记过，没有重复升级',
      blocked_by_prerequisites: '升级被前置检查拦下',
      gate_unavailable: '技能标准读不出来，前置检查没有执行',
      invalid_events: '成果内容不完整，已跳过',
      store_unreadable: '成长记录读不出来（已按当前画像继续）',
      recovered: '已补存上次没保存的计划（没有重新跑 AI）',
      target_mismatch: '目标岗位与当前计划不一致，已拒绝执行',
      jd_mismatch: '招聘要求与当前计划不一致，已拒绝执行',
    },
    value,
  )
}

export function growthOutcomeTone(value) {
  return (
    {
      upgraded: 'ok',
      no_upgrade: 'muted',
      no_events: 'muted',
      all_duplicates: 'muted',
      blocked_by_prerequisites: 'warn',
      gate_unavailable: 'warn',
      invalid_events: 'warn',
      store_unreadable: 'danger',
      recovered: 'primary',
      target_mismatch: 'danger',
      jd_mismatch: 'danger',
    }[value] ?? 'muted'
  )
}

export function gateStatusText(value) {
  return lookup(
    {
      passed: '已通过（技能已收录且前置齐备）',
      not_covered: '已跳过（这项技能还没有等级标准）',
      unavailable: '未执行（技能标准读不出来）',
      blocked: '被拦截（前置技能不足）',
    },
    value,
  )
}

export function gateStatusTone(value) {
  return { passed: 'ok', not_covered: 'muted', unavailable: 'warn', blocked: 'danger' }[value] ?? 'muted'
}
