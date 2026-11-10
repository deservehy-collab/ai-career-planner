/**
 * 7 个 Agent 的静态元数据 + 由分析结果反推的"各节点产出摘要"。
 *
 * 两条硬规矩
 * ----------
 * 1. **可追溯**：每个条目都保留 `node` —— 与 `graph/workflow.py` 的 `NODES` / `ORDER`
 *    里的实际节点名逐字一致。展示名可以面向评委优化，但绝不能与真实节点失去对应关系。
 *    源码路径**不再进界面**：使用者只看中文展示名与职责，实现细节回代码里查。
 *
 * 2. **不虚构执行状态**：节点执行状态只来自后端 run 记录的 `progress` / `current_node`
 *    （backend/jobs.py，由图执行时自己回报）；这里只描述静态结构与产出摘要，
 *    本文件不按时间推算任何节点状态。
 */

/** 与 graph/workflow.py 的 ORDER 完全一致。 */
export const WORKFLOW_ORDER = [
  'profile',
  'growth',
  'retrieval',
  'job',
  'assessment',
  'learning',
  'project',
]

/** 色族：认知 / 依据 / 匹配 / 规划。依据整理单独一色，是全站唯一的紫。 */
export const AGENT_GROUP = {
  cognition: { label: '认知', color: 'var(--agent-cognition)', soft: 'var(--agent-cognition-soft)' },
  retrieval: { label: '依据', color: 'var(--agent-retrieval)', soft: 'var(--agent-retrieval-soft)' },
  match: { label: '匹配', color: 'var(--agent-match)', soft: 'var(--agent-match-soft)' },
  plan: { label: '规划', color: 'var(--agent-plan)', soft: 'var(--agent-plan-soft)' },
}

export const AGENTS = [
  {
    node: 'profile',
    order: 1,
    displayName: '能力画像',
    group: 'cognition',
    duty: '读取专业、年级、自己填写的技能和项目经历，按证据分级整理出能力表，并给出优势和短板。',
    outputs: ['user_skills', 'user_strengths', 'user_weaknesses'],
    primaryKey: 'user_skills',
    anchor: 'card-profile',
    outputLabel: '项能力',
  },
  {
    node: 'growth',
    order: 2,
    displayName: '成长追踪',
    group: 'cognition',
    duty: '把成长记录和当前画像合在一起，按这一轮的成长事件升级技能等级，并记下升级前后的差别。',
    outputs: ['user_skills_before', 'skill_updates', 'growth_diff'],
    primaryKey: 'growth_diff',
    anchor: 'card-growth',
    outputLabel: '项等级变化',
  },
  {
    node: 'retrieval',
    order: 3,
    displayName: '招聘要求整理',
    group: 'retrieval',
    duty: '把你粘贴的招聘要求逐句拆开，作为这次分析的事实依据；没贴招聘要求时，改用同类岗位资料里登记过出处的摘要（可以回查，但不是这家公司的原话）。只整理依据，不做判定。',
    outputs: ['job_evidence', 'retrieval_status', 'retrieval_diagnostics'],
    primaryKey: 'job_evidence',
    anchor: 'card-evidence',
    outputLabel: '条依据',
  },
  {
    node: 'job',
    order: 4,
    displayName: '岗位要求',
    group: 'match',
    duty: '从你粘贴的招聘要求里逐条抽出能力要求，并标注每条要求在原话里的出处和证据强度。',
    outputs: ['job_requirements'],
    primaryKey: 'job_requirements',
    anchor: 'card-requirements',
    outputLabel: '项要求',
  },
  {
    node: 'assessment',
    order: 5,
    displayName: '能力差距',
    group: 'match',
    duty: '把岗位要求和你的能力逐项对照。等级、状态、优先级都按固定规则判定，不让 AI 随口给。',
    outputs: ['skill_gaps'],
    primaryKey: 'skill_gaps',
    anchor: 'card-gaps',
    outputLabel: '项差距',
  },
  {
    node: 'learning',
    order: 6,
    displayName: '学习路线',
    group: 'plan',
    duty: '按缺口优先级与技能前置关系排出学习阶段，每个阶段给出目标、前置要求与可验证的完成标准。',
    outputs: ['learning_plan'],
    primaryKey: 'learning_plan',
    anchor: 'card-learning',
    outputLabel: '个阶段',
  },
  {
    node: 'project',
    order: 7,
    displayName: '实战项目',
    group: 'plan',
    duty: '结合技能缺口和学习路线推荐实战项目，给出技术栈、对应阶段和可以验收的最终成果。',
    outputs: ['project_plan'],
    primaryKey: 'project_plan',
    anchor: 'card-projects',
    outputLabel: '个项目',
  },
]

const BY_NODE = new Map(AGENTS.map((agent) => [agent.node, agent]))

export function getAgent(node) {
  return BY_NODE.get(node) ?? null
}

/**
 * 此刻真正在执行的节点（并行时可能不止一个），按工作流展示顺序排出。
 * 只认后端回报的 ``running``；旧记录没有 progress 时才退回 ``currentNode``。
 */
export function runningAgents(progress = [], currentNode = null) {
  const running = new Set(
    (Array.isArray(progress) ? progress : [])
      .filter((item) => item?.status === 'running' && item.node)
      .map((item) => item.node),
  )
  const nodes = WORKFLOW_ORDER.filter((node) => running.has(node))
  if (!nodes.length && currentNode) nodes.push(currentNode)
  return nodes.map((node) => getAgent(node)).filter(Boolean)
}

export function agentColor(node) {
  const agent = BY_NODE.get(node)
  return agent ? AGENT_GROUP[agent.group].color : 'var(--text-muted)'
}

// ── 产出摘要（只在拿到 result 后调用）────────────────────────────────────

function countOf(value) {
  return Array.isArray(value) ? value.length : 0
}

/**
 * 单节点摘要。
 *
 * 三种状态必须分清楚，**不许把"没有数据"一律说成"节点没产出"**：
 *
 *   present=false —— 结果里**根本没有**该节点的产出字段。我们对这个节点一无所知，
 *                    只能说"没有数据"，不能断言它没产出（那是猜测）。
 *   present=true, count=0 —— 字段在、但是空数组：节点确实产出了 0 条，如实说。
 *   degraded —— 在 count 的基础上，再用后端自己的状态字段判断是否走了降级路径。
 *
 * `degraded` 的判据**全部来自真实字段**：
 *   - profile / job / assessment / learning / project：产出数组为空
 *   - retrieval：状态不是 user_pasted / official_digest，或状态与条数互相矛盾
 *     （后端只会给 user_pasted / official_digest / no_source 三值）
 *   - growth：`growth_diff.outcome` 为 `store_unreadable` 或 `gate_unavailable`
 *     （注意：`no_events` **不是**降级 —— 本轮本来就没有成长事件，属正常）
 */
function summarizeNode(agent, result) {
  const has = (key) => Object.prototype.hasOwnProperty.call(result, key)

  // 该节点的"主产出字段"不在结果里 → 无从判断，中性表述
  if (!has(agent.primaryKey)) {
    return { present: false, count: 0, degraded: false, note: '这一步暂时没有可显示的结果' }
  }

  const growthDiff = result.growth_diff || {}

  switch (agent.node) {
    case 'profile': {
      const count = countOf(result.user_skills)
      return { present: true, count, degraded: count === 0, note: count === 0 ? '未解析出任何技能' : '' }
    }
    case 'growth': {
      const outcome = growthDiff.outcome
      const count = countOf(result.skill_updates)
      return {
        present: true,
        count,
        degraded: outcome === 'store_unreadable' || outcome === 'gate_unavailable',
        // no_events 是"正常但没有"，措辞要和"降级"分开
        note: outcome === 'no_events' ? '这一轮没有新的成长事件，等级没有变化（正常）' : '',
        outcome: outcome ?? null,
      }
    }
    case 'retrieval': {
      const status = result.retrieval_status || ''
      const count = countOf(result.job_evidence)
      let note = ''
      let degraded = false
      if (status === 'user_pasted') {
        // 正常情况下 status=user_pasted 必然切出原话；两者矛盾时按"有问题"处理，
        // 不要因为状态字段好看就把零证据显示成正常。
        degraded = count === 0
        if (count === 0) note = '显示用了你粘贴的要求，却没有整理出可用的句子'
      } else if (status === 'official_digest') {
        degraded = count === 0
        note = degraded
          ? '显示用了同类岗位资料，却没有整理出条目'
          : `没有粘贴招聘要求，这次改用同类岗位资料 ${count} 条（可以参考，但不是这家公司的原话）`
      } else if (status === 'no_source') {
        degraded = true
        note = '没有可用的招聘要求，岗位要求改由 AI 按相近岗位推断（已如实标注）'
      } else {
        degraded = true
        note = '看不出这次的依据从哪来'
      }
      return { present: true, count, degraded, note, status }
    }
    case 'job': {
      const count = countOf(result.job_requirements)
      return { present: true, count, degraded: count === 0, note: count === 0 ? '未生成岗位要求' : '' }
    }
    case 'assessment': {
      const count = countOf(result.skill_gaps)
      return { present: true, count, degraded: count === 0, note: count === 0 ? '未生成能力差距' : '' }
    }
    case 'learning': {
      const count = countOf(result.learning_plan)
      return { present: true, count, degraded: count === 0, note: count === 0 ? '未生成学习阶段' : '' }
    }
    case 'project': {
      const count = countOf(result.project_plan)
      return { present: true, count, degraded: count === 0, note: count === 0 ? '未推荐项目' : '' }
    }
    default:
      return { present: false, count: 0, degraded: false, note: '' }
  }
}

/**
 * 把分析结果摊成「节点 → 产出摘要」。
 * result 为空（未运行 / 运行中）时返回 null —— 调用方据此渲染"未产出"的中性格。
 */
export function summarizeWorkflow(result) {
  if (!result) return null
  const summary = {}
  for (const agent of AGENTS) summary[agent.node] = summarizeNode(agent, result)
  return summary
}
