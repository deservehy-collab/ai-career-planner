/**
 * 后端接口的一对一封装。
 * 请求体字段严格对应 backend/schemas.py，不自行发明字段。
 */

import { api } from './client'

// ── 认证 ──────────────────────────────────────────────────────────────

/** `POST /api/auth/register` → 201 {token, token_type, user} */
export const register = (body) => api.post('/auth/register', body)

/** `POST /api/auth/login` → {token, token_type, user}；401 用户名或密码错误 */
export const login = (body) => api.post('/auth/login', body)

/** `GET /api/auth/me` → {id, username, created_at}（需 Bearer token） */
export const getMe = (options) => api.get('/auth/me', options)

/** `POST /api/auth/logout` → {ok:true}。JWT 无状态，退出以前端丢弃 token 为准 */
export const logout = () => api.post('/auth/logout')

/** `GET /api/health` → {status, llm_channel, ollama, index, knowledge, stores, config} */
export const getHealth = (options) => api.get('/health', options)

// ── 账号基础资料与求职档案 ──────────────────────────────────────────────

/** `GET /api/profile` → {major,grade,skills,projects,version,updated_at} */
export const getAccountProfile = (options) => api.get('/profile', options)

/** `PUT /api/profile` → 版本冲突 409 */
export const saveAccountProfile = (body) => api.put('/profile', body)

/** `GET /api/plans?limit=&before=` → {items,total,next_before} */
export const listPlans = (params = {}, options) => {
  const query = new URLSearchParams()
  if (params.limit) query.set('limit', String(params.limit))
  if (params.before) query.set('before', params.before)
  const suffix = query.toString()
  return api.get(`/plans${suffix ? `?${suffix}` : ''}`, options)
}

/** `POST /api/plans` → 201 {plan_id,state,created}；同 request_key 幂等 */
export const createPlan = (body) => api.post('/plans', body)

/** `GET /api/plans/{id}` */
export const getPlan = (planId, options) =>
  api.get(`/plans/${encodeURIComponent(planId)}`, options)

/** `DELETE /api/plans/{id}` → 204（不存在/越权均 404） */
export const deletePlan = (planId) =>
  api.delete(`/plans/${encodeURIComponent(planId)}`)

/** `POST /api/plans/{id}/analyses` → 202 {run_id} */
export const startPlanAnalysis = (planId, body) =>
  api.post(`/plans/${encodeURIComponent(planId)}/analyses`, body)

/** `GET /api/plans/{id}/runs/{run_id}` */
export const getPlanRun = (planId, runId, options) =>
  api.get(
    `/plans/${encodeURIComponent(planId)}/runs/${encodeURIComponent(runId)}`,
    options,
  )

/**
 * `POST /api/plans/{id}/runs/{run_id}/cancel` →
 *   {plan_id, run_id, canceled, status, reason}
 *
 * 只停止这一次分析，**不删档案**。排队中的任务不再执行；已经在跑的推理无法
 * 掐断，但结果不会落盘（轮次/任务/成长都不动）。已经跑完的返回 canceled=false，
 * 调用方应如实告诉用户"结果已保存"。
 */
export const cancelPlanRun = (planId, runId) =>
  api.post(
    `/plans/${encodeURIComponent(planId)}/runs/${encodeURIComponent(runId)}/cancel`,
  )

/** `POST /api/plans/{id}/growth/next-round` → 202 {run_id} */
export const startPlanNextRound = (planId, body) =>
  api.post(`/plans/${encodeURIComponent(planId)}/growth/next-round`, body)

export const planPath = (planId, suffix) =>
  `/plans/${encodeURIComponent(planId)}/${suffix}`

/**
 * `GET /api/status/llm-channel` →
 *   {channel: "local"|"cloud", cloud_configured, active_model, cloud_auth_failed}
 */
export const getLlmChannel = (options) => api.get('/status/llm-channel', options)

/**
 * `PUT /api/status/llm-channel` → {channel, cloud_configured, active_model}
 *
 * 请求体：{channel: "local"|"cloud"}。未配 CLOUD_API_KEY 时切云端会 422，
 * 前端此时也会把云端选项置灰。切换只影响之后提交的任务。
 */
export const setLlmChannel = (channel) => api.put('/status/llm-channel', { channel })

/**
 * `POST /api/analyze` → 202 {status: "queued", run_id, kind: "analyze"}
 *
 * 请求体（AnalyzeRequest，user_profile 与 target_job 必填）：
 *   user_profile : {major, grade, skills: string[], projects: string[]}
 *   target_job   : string（minLength 1）
 *   jd_text      : string | null（可省）—— 用户当场粘贴的招聘要求原文，本次岗位要求的唯一依据。
 *                  上限 config.JD_TEXT_MAX_CHARS（6000 字），超长会 422，前端已提前拦住。
 *                  不传时岗位要求全由模型推断，前端整栏标灰并**不给**匹配度百分比。
 *   growth_events: object[]（可省）—— ⚠️ 分析链路是只读的，绝不在这里传成长事件
 */
export const startAnalyze = (payload) => api.post('/analyze', payload)

/**
 * `POST /api/analyze-and-save` → 202 {status:"queued", run_id, kind:"analyze_and_save"}
 *
 * 请求体在 AnalyzeRequest 基础上多一个 **new_plan**（默认 false）：
 *
 *   · 没有任何轮次时：本次分析照常存成第 1 轮（round_created=true）。
 *   · 已有轮次 + new_plan=false：**只预览**，不改任务清单；
 *     result 外层带 existing_round_no 与 warning 说明"本次只做预览"。
 *   · 已有轮次 + new_plan=true：另存为新的一轮（round_created=true），
 *     旧轮次、旧任务与成长历史全部原样保留。
 *
 * result 是**带包装的**：只读分析结果在 `result.analysis` 里，外层是
 * `{new_plan, round_created, round_no, existing_round_no, target_job,
 *   goal, goal_status, warning}`。
 * 每一轮都会保存 snapshot（目标、JD 原文、岗位要求、依据强度、结局），
 * 下一轮沿用这份快照，不再让模型重新推断。
 */
export const startAnalyzeAndSave = (payload) => api.post('/analyze-and-save', payload)

/**
 * `GET /api/runs/{run_id}` → 运行记录
 *   {run_id, kind, status: queued|running|succeeded|failed,
 *    result, error, created_at, started_at, finished_at}
 * 未知 run_id → 404。
 */
export const getRun = (runId, options, planId) =>
  planId
    ? getPlanRun(planId, runId, options)
    : api.get(`/runs/${encodeURIComponent(runId)}`, options)

// ── 任务 / 成长闭环 ────────────────────────────────────────────────────

/**
 * `GET /api/tasks?round_no=&status=` →
 *   {store_status, writable, warning,
 *    rounds:[{round,target_job,created_at,source,goal_status,task_count,completed_count}],
 *    latest_round:{round,created_at,target_job,learning_plan[],project_plan[],tasks[],snapshot{}},
 *    latest_goal:{round,status,goal{message,basis,...},target_job,evaluated_at} | null,
 *    pending_next_round:{created_at,source_round,target_job,has_plans,event_count} | null,
 *    tasks:[{task_id,kind,round,index,title,skills[],status,created_at,completed_at,evidence}],
 *    summary:{total,completed,pending}}
 *
 * 注意：learning_plan / project_plan 的**明细只在 latest_round 里**；
 * rounds[] 只有计数，旧轮次拿不到计划全文（后端未提供该接口）。
 *
 * `latest_goal` 是持久化的结局：刷新页面后仍能显示"已达成 / 依据不足"，
 * 不需要重跑分析。`pending_next_round.has_plans=true` 表示上次算好的计划
 * 没落盘，去成长记录页点「补存上次计划」即可（不重跑模型）。
 */
export const listTasks = (params = {}, options, planId) => {
  const query = new URLSearchParams()
  if (params.roundNo !== undefined && params.roundNo !== null) query.set('round_no', String(params.roundNo))
  if (params.status) query.set('status', params.status)
  const suffix = query.toString()
  const path = planId ? planPath(planId, 'tasks') : '/tasks'
  return api.get(`${path}${suffix ? `?${suffix}` : ''}`, options)
}

/**
 * `POST /api/tasks/{task_id}/complete` → 200（任务不存在 404 / ID 非法 422）
 *
 * 请求体：
 *   { evidence?: string(1~5000, 首尾空白会被拒),
 *     profile_skills?: [{skill,level,evidence}],  ← 传了它，预览才与 growth_node 同基线
 *     note?: string(≤1000, 选填心得笔记) }
 *
 * **点提交即完成**：`evidence` 可以不传。不传时记成 `self_report`（自述完成），
 * 技能照规则升级，但评估只算"未验证"；之后可用 `attachTaskEvidence` 补凭证。
 * `note` 单独保存，不并入证据，也不当凭证；两者合计 ≤ 5000 字，超了 422。
 *
 * 返回体同时包含三件事，**必须分开解读**：
 *   ① 任务完成记录 completion_status / completion_written / already_completed
 *   ② 成长成果     growth_event / growth_event_status
 *   ③ 升级预览     growth_evaluated / growth_outcome / upgraded / updates / blocked
 * ③ 只是**预览**：本接口绝不写 runs/growth.json。
 */
export const completeTask = (taskId, body, planId) =>
  api.post(
    planId
      ? planPath(planId, `tasks/${encodeURIComponent(taskId)}/complete`)
      : `/tasks/${encodeURIComponent(taskId)}/complete`,
    body,
  )

/**
 * `POST /api/tasks/{task_id}/evidence` → 200
 *
 * 给一条**已完成**任务补凭证：请求体 `{ evidence: string(1~5000) }`。
 * 成长历史里已有这条成果时追加一条 `verification` 记录（原记录一条不改），
 * 把自述升级翻成有凭证；还没有历史时只翻任务证据类型，等下一轮入账。
 * 任务尚未完成 → 409；凭证为空或超长 → 422。
 *
 * 返回 `{status, evidence_kind, verified, verification_written,
 * pending_next_round, task, reason}`。
 */
export const attachTaskEvidence = (taskId, body, planId) =>
  api.post(
    planId
      ? planPath(planId, `tasks/${encodeURIComponent(taskId)}/evidence`)
      : `/tasks/${encodeURIComponent(taskId)}/evidence`,
    body,
  )

/**
 * `POST /api/tasks/clear` →
 *   {status, rounds_removed, tasks_removed, backup, pending_events, warning}
 *
 * status 三种：`cleared`（已清空）/ `needs_confirmation`（有成果还没进成长历史，
 * 什么都没动）/ `refused`（清单文件不能安全写，或**成长历史不可读、判不了哪些
 * 成果已记账**，什么都没动）。
 *
 * 只重置任务清单，**成长历史一行都不动**。清空前一定先把原文件备份，
 * `backup` 就是备份名，照着它能还原。
 * 只有带着 `dropPendingEvents: true` 才会把还没记账的成果一起丢掉。
 */
export const clearTasks = (dropPendingEvents = false, planId) =>
  api.post(planId ? planPath(planId, 'tasks/clear') : '/tasks/clear',
    { drop_pending_events: dropPendingEvents })

/**
 * `GET /api/growth/pending` →
 *   {events:[{type,title,skills[],evidence,at}],
 *    tasks:[{task_id,title,fingerprint}],
 *    skipped[], warnings[], seen, restored}
 *
 * `restored === false` 表示成长历史不可读、**无法判断哪些已处理**，此时 events 一定是空数组 ——
 * 这是"判不了"，不是"没有待办"。
 */
export const getPending = (options, planId) =>
  api.get(planId ? planPath(planId, 'growth/pending') : '/growth/pending', options)

/**
 * `POST /api/growth/pending/withdraw` → {withdrawn:true, task_id, round, task}
 * 失败（422）→ {detail:{withdrawn:false, reason}}
 *
 * 只撤回**尚未入账**的成果：任务退回待完成并清空完成证据。
 * 已入账的成果必须用 voidGrowthEvent，否则成长历史会留下无源升级。
 */
export const withdrawPending = (taskId, planId) =>
  api.post(
    planId ? planPath(planId, 'growth/pending/withdraw') : '/growth/pending/withdraw',
    { task_id: taskId },
  )

/**
 * `POST /api/growth/manual-event` → {added: true, round, task_id, task}
 * 失败（422）→ {detail: {added:false, reason}}
 *
 * 请求体：{kind: "learning"|"project", title, skills: string[], evidence?,
 *          target_job?: string}
 * `evidence` 可选：不填就是自述成果，升级照规则走，但评估按未验证处理。
 * 用于打破空系统死锁：尚无任务时也可手动补录一条成果。
 */
export const addManualEvent = (body, planId) =>
  api.post(planId ? planPath(planId, 'growth/manual-event') : '/growth/manual-event', body)

/**
 * `GET /api/growth/history` →
 *   {status, writable, warning, path,
 *    history:[{at, kind?, events[], user_skills_before[], user_skills_after[],
 *              level_changes[], blocked_upgrades[], skipped[], invalid_events[],
 *              knowledge_base_status, target_job,
 *              voided_event_indices[]}],   ← 该条里已被作废的事件下标
 *    latest_skills:[{skill,level}], baseline:{source,at,user_skills[]},
 *    confirmed_count, voided_count}       ← 调整记录不计入确认次数
 */
export const getGrowthHistory = (options, planId) =>
  api.get(planId ? planPath(planId, 'growth/history') : '/growth/history', options)

/**
 * `POST /api/growth/history/void` → {voided:true, level_changes, latest_skills, ...}
 * 失败（422）→ {detail:{voided:false, reason}}
 *
 * 请求体：{entry_index, event_index, reason}（下标来自 getGrowthHistory 的 history 数组）。
 * 不删原记录：追加一条 kind="adjustment" 的调整记录并重算能力表；
 * 被作废的成果不会在下一轮自动重新入账。
 */
export const voidGrowthEvent = (body, planId) =>
  api.post(
    planId ? planPath(planId, 'growth/history/void') : '/growth/history/void',
    body,
  )

/**
 * `POST /api/growth/next-round` → **202** {status:"queued", run_id, kind:"next_round"}
 *
 * ⚠️ 这是**正常记账**的唯一入口，且会重跑整条流水线（多次本地模型推理）。
 *    调用前必须二次确认；`user_profile` 缺失会让 profile 节点报错、该次 run 变为 failed。
 *    用户显式更正不改走这里：撤回用 withdrawPending，作废用 voidGrowthEvent。
 *
 * 请求体只需要 `{user_profile}`；**不要**再传 target_job / jd_text：
 * 目标岗位与招聘原文一律从当前计划保存的 snapshot 里取。若仍传了与快照不一致
 * 的目标或原文，后端会在跑流水线之前直接拒绝（growth_outcome 为
 * `target_mismatch` / `jd_mismatch`，任务清单与成长历史都没有改动）。
 *
 * 结果用 `GET /api/runs/{run_id}` 查；`result` 有两种形状（无待办时会早退，
 * 不带 target_job / user_skills / growth_diff / learning_plan / project_plan 等字段）。
 *
 * 断点恢复：上次算好但没保存计划时，再调用一次会直接补存（`recovered=true`），
 * 不重跑模型、不重复记账。
 */
export const startNextRound = (body, planId) =>
  planId
    ? startPlanNextRound(planId, { request_key: body?.request_key || crypto.randomUUID() })
    : api.post('/growth/next-round', body)