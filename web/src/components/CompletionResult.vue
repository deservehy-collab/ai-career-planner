<script setup>
import { computed } from 'vue'

import AgentTag from './AgentTag.vue'
import AppBadge from './AppBadge.vue'

import {
  baselineSourceText,
  baselineSourceTone,
  completionStatusText,
  completionStatusTone,
  eventStatusText,
  eventStatusTone,
  gateStatusText,
  gateStatusTone,
  growthOutcomeText,
  growthOutcomeTone,
  knowledgeBaseStatusText,
  knowledgeBaseStatusTone,
  loopStatusText,
  loopStatusTone,
} from '../utils/enums'
import { formatDateTime, formatEvidence, joinList } from '../utils/format'

/**
 * 「提交任务完成」的结果面板。
 *
 * 后端 `POST /api/tasks/{id}/complete` 的返回体里塞了**三件性质完全不同的事**
 * （见 agents/growth_loop.py 的模块说明），必须分开呈现，绝不能让用户看见
 * "任务已完成"就以为"技能已经升上去了"：
 *
 *   ① 任务完成记录 —— 唯一真正落盘的一步（写 runs/loop.json）
 *   ② 成长成果     —— 这条任务能否构造成长事件（可能构不成）
 *   ③ 技能升级     —— **只是预览**，本接口不写 runs/growth.json；
 *                     正式落盘发生在「成长记录」页的「生成下一轮」时由 growth_node 完成
 */
const props = defineProps({
  /** complete 接口的原始返回体 */
  result: { type: Object, required: true },
})

const r = computed(() => props.result || {})
const updates = computed(() => r.value.updates || [])
const blocked = computed(() => r.value.blocked || [])
const skipped = computed(() => r.value.skipped || [])
const invalidEvents = computed(() => r.value.invalid_events || [])
const duplicateEvents = computed(() => r.value.duplicate_events || [])
const unmappedSkills = computed(() => r.value.unmapped_skills || [])
const warnings = computed(() => r.value.warnings || [])

const hasEvent = computed(() => !!r.value.growth_event)
/** 本次是否真的算出了升级计划（注意：是"计划"，不是"已升级"）。 */
const hasPreview = computed(() => r.value.growth_evaluated === true && updates.value.length > 0)

/**
 * 这条成果是自述完成还是附了凭证。
 * 历史数据没有 evidence_kind：有证据就按凭证算，没有就是自述。
 */
const eventProofText = computed(() => {
  const event = r.value.growth_event || {}
  const kind = event.evidence_kind || (event.evidence ? 'growth' : '')
  return kind === 'growth' || kind === 'project'
    ? '已附凭证'
    : '自述完成 · 未附凭证'
})
const eventHasProof = computed(() => eventProofText.value === '已附凭证')

const isHistoryOnly = computed(() => r.value.baseline_source === 'history_only')

/**
 * 后台把同一句失败原因同时放在 `reason` 和 `warnings` 里，②③两块又各读一遍，
 * 结果用户看见三遍同一句话。原因按"谁拥有它"只说一次：
 * 完成记录没写成 → ①；写成了但构不成成果 → ②；其余 → ③。
 */
const eventFailed = computed(
  () => !hasEvent.value && !!r.value.reason && r.value.completion_written === true,
)
const reasonShownElsewhere = computed(
  () => r.value.completion_written === false || eventFailed.value,
)
const previewNote = computed(() =>
  reasonShownElsewhere.value ? '' : r.value.reason || '',
)
const visibleWarnings = computed(() =>
  warnings.value.filter((w) => !(reasonShownElsewhere.value && w === r.value.reason)),
)

/**
 * 技能名没进技能标准词表有两种下场，得说清楚是哪一种：
 * 词表压根没有这项技能 → 按任务里写的原名记账；同时沾上好几项 → 不猜，本次不升级。
 * 两边都取后端已经算好的字段对照，前端不重实现判定规则。
 */
const nameKey = (s) => String(s ?? '').trim().toLowerCase().replace(/ /g, '')
const canonicalKeys = computed(() => new Set((r.value.skills_canonical || []).map(nameKey)))
const skillsBookedByRawName = computed(() =>
  unmappedSkills.value.filter((s) => canonicalKeys.value.has(nameKey(s))),
)
const skillsNotJudged = computed(() =>
  unmappedSkills.value.filter((s) => !canonicalKeys.value.has(nameKey(s))),
)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>提交结果</h2>
      <div class="btn-row">
        <AppBadge :text="loopStatusText(r.status)" :tone="loopStatusTone(r.status)" />
        <AgentTag node="growth" />
      </div>
    </div>

    <div class="card-body">
      <!-- ① 任务完成记录 —— 唯一真正落盘的一步 -->
      <div class="result-block">
        <h3 class="result-title">
          ① 任务完成记录
          <span class="sub">这一步已经正式保存，下面三块里只有它是真正存下来的</span>
        </h3>
        <div class="btn-row btn-row--tight">
          <AppBadge
            :text="completionStatusText(r.completion_status)"
            :tone="completionStatusTone(r.completion_status)"
          />
          <span class="faint">已保存：{{ r.completion_written ? '是' : '否' }}</span>
        </div>
        <dl class="kv kv--mid">
          <dt>完成时间</dt>
          <dd>{{ formatDateTime(r.task?.completed_at) }}</dd>
          <dt>提交凭证</dt>
          <dd class="preserve-text">
            {{ r.task?.evidence ? formatEvidence(r.task.evidence) : '（没附凭证 · 按自述完成记账）' }}
          </dd>
          <template v-if="r.task?.note">
            <dt>心得笔记</dt>
            <dd class="preserve-text">{{ r.task.note }}</dd>
          </template>
        </dl>
        <p v-if="r.already_completed" class="faint result-note">
          该任务此前已完成，这次<strong>没有重复保存</strong>，第一次提交的证据不会被覆盖。
        </p>
        <p v-if="!r.completion_written && !r.already_completed" class="faint result-note">
          完成记录<strong>没有保存</strong>：{{ r.reason || '原因未给出' }}。
        </p>
      </div>

      <!-- ② 成长成果 -->
      <div class="result-block">
        <h3 class="result-title">
          ② 成长成果
          <span class="sub">这条任务能不能算作一次成长记录</span>
        </h3>
        <div class="btn-row btn-row--tight">
          <AppBadge
            :text="eventStatusText(r.growth_event_status)"
            :tone="eventStatusTone(r.growth_event_status)"
          />
          <AppBadge
            v-if="hasEvent"
            :text="eventProofText"
            :tone="eventHasProof ? 'ok' : 'warn'"
          />
        </div>

        <template v-if="hasEvent">
          <dl class="kv kv--mid">
            <dt>成果类型</dt>
            <dd>{{ r.growth_event.type === 'project' ? '项目成果' : '学习成果' }}</dd>
            <dt>成果标题</dt>
            <dd>{{ r.growth_event.title }}</dd>
            <dt>涉及技能</dt>
            <dd>{{ joinList(r.growth_event.skills, '（未关联技能）') }}</dd>
            <dt>依据类型</dt>
            <dd>{{ eventProofText }}</dd>
            <dt>凭证内容</dt>
            <dd class="preserve-text">
              {{ r.growth_event.evidence || '（没附凭证，只记了完成）' }}
            </dd>
            <template v-if="r.growth_event.note">
              <dt>心得笔记</dt>
              <dd class="preserve-text">{{ r.growth_event.note }}</dd>
            </template>
          </dl>
        </template>
        <p v-else-if="eventFailed" class="faint flush-first">
          没有构造成长成果：{{ r.reason }}。
          <strong>任务完成记录不受影响</strong>，只是这条任务无法作为技能升级的依据。
        </p>

        <div v-if="skillsBookedByRawName.length" class="notice notice-info">
          技能标准清单里没有这几项，已按任务里写的原名记账：
          {{ joinList(skillsBookedByRawName) }}
        </div>
        <div v-if="skillsNotJudged.length" class="notice notice-warn">
          这几项技能名同时对应好几项技能，系统不猜，本次没有据此升级：
          {{ joinList(skillsNotJudged) }}。把任务名或技能写得更具体一些就能认出来。
        </div>
        <div v-if="duplicateEvents.length" class="notice notice-info">
          这条成果之前已经记过了，这次跳过，不会重复升级。
        </div>
        <div v-for="(item, i) in invalidEvents" :key="i" class="notice notice-warn">
          这条成果没有通过检查：{{ item.reason }}
        </div>
      </div>

      <!-- ③ 技能升级预览 -->
      <div class="result-block flush-last">
        <h3 class="result-title">
          ③ 技能升级预览
          <span class="sub">只是预览，不是已升级</span>
        </h3>

        <div class="notice notice-warn notice--tight">
          <strong>预览还没有记进成长记录。</strong>
          提交这一步不会改动成长记录；技能等级正式生效，是在「成长记录」页点
          「生成下一轮」的时候。
        </div>

        <template v-if="!r.growth_evaluated">
          <div class="btn-row">
            <AppBadge
              :text="growthOutcomeText(r.growth_outcome) || '本次没有评估升级'"
              :tone="growthOutcomeTone(r.growth_outcome)"
            />
          </div>
          <p v-if="previewNote" class="faint result-note">{{ previewNote }}</p>
        </template>

        <template v-else>
          <div class="btn-row">
            <AppBadge
              :text="growthOutcomeText(r.growth_outcome)"
              :tone="growthOutcomeTone(r.growth_outcome)"
            />
            <span v-if="r.knowledge_base_status" class="faint">
              技能标准：{{ knowledgeBaseStatusText(r.knowledge_base_status) }}
            </span>
          </div>

          <div v-if="hasPreview" class="sub-block">
            <h4 class="mini-title">预计等级变化（{{ updates.length }} 项）</h4>
            <ul class="muted mini-list">
              <li v-for="item in updates" :key="item.skill">
                <strong>{{ item.skill }}</strong>：{{ item.from || '无' }} → {{ item.to }}
                <span class="faint">
                  （前置检查：{{ gateStatusText(item.gate_status) }}
                  <template v-if="item.gate_unavailable"> ⚠ 没有经过检查</template>）
                </span>
              </li>
            </ul>
          </div>
          <p v-else class="faint result-note">本次没有产生任何等级提升。</p>

          <div v-if="blocked.length" class="sub-block">
            <h4 class="mini-title">被前置检查拦下（{{ blocked.length }} 项）</h4>
            <ul class="muted mini-list">
              <li v-for="item in blocked" :key="item.skill">
                {{ item.skill }}：{{ item.current_level || '无' }} → 本可升到 {{ item.candidate_level }}
                <span class="faint">
                  （{{ gateStatusText(item.gate_status) }}
                  · 缺前置：{{ joinList(item.missing_prerequisites, '未给出') }}）
                </span>
                <span v-if="item.gate_unavailable" class="faint">
                  ⚠ 这项技能因为技能标准读不出来，没有经过前置检查，不能当成"已通过"
                </span>
              </li>
            </ul>
          </div>

          <div v-if="skipped.length" class="sub-block">
            <h4 class="mini-title">未升级的候选（{{ skipped.length }} 项）</h4>
            <ul class="muted mini-list">
              <li v-for="item in skipped" :key="item.skill">{{ item.skill }}：{{ item.reason }}</li>
            </ul>
          </div>

          <!-- 基线提示（措辞按约束：只说明更严格，不承诺结果一定更好） -->
          <div v-if="isHistoryOnly" class="notice notice-warn sub-block flush-last">
            <AppBadge
              :text="baselineSourceText(r.baseline_source)"
              :tone="baselineSourceTone(r.baseline_source)"
            />
            这次预览只看成长记录，可能比结合当前画像的评估更严格。
            在「岗位分析」页完成一次分析后，再提交任务，预览和正式判定就会用同一份数据。
          </div>
        </template>
      </div>

      <div v-for="(warn, i) in visibleWarnings" :key="i" class="notice notice-warn">{{ warn }}</div>
    </div>
  </section>
</template>
