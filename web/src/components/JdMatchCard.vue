<script setup>
import { computed } from 'vue'

import AppBadge from './AppBadge.vue'
import { formatImportance, formatMatchRate, hasMatchRate } from '../utils/format'
import {
  evidenceStatusText,
  hasSolidBasis,
  jdOverallText,
  jdOverallTone,
  priorityTone,
  requirementBasisText,
} from '../utils/enums'

/**
 * 岗位匹配结论（graph/workflow.py build_final_plan → agents/jd_match.py compute_jd_match）。
 * 真实字段：{overall, requirement_total, requirement_judged, requirement_orphan,
 *   requirement_satisfied, requirement_shortfall, match_rate,
 *   hard_blocks[], key_gaps[], jd_source_warning}
 *
 * ⚠️ match_rate 为 null 表示"没有可判定要求"，必须显示成"无可判定要求"，
 * **不能显示成 0%** —— 那是把"判不了"说成"一个都不满足"。
 *
 * ⚠️ basis=model_only 时整栏数字都是空的（没有原话可对照 → 可判定要求必然为 0），
 * 这时候要讲的是"为什么没有"，不是摆一排 0 给用户看。
 */
const props = defineProps({
  match: { type: Object, default: null },
  /** 本次岗位要求的主依据档位（utils/enums REQUIREMENT_BASIS） */
  basis: { type: String, default: 'model_only' },
})

const rateText = computed(() => formatMatchRate(props.match?.match_rate))
const rateAvailable = computed(() => hasMatchRate(props.match?.match_rate))
/** 后端这里给的是**字符串数组**，直接 {{ }} 会被 JSON 化摆给用户看，必须先并成一句话。 */
const warning = computed(() => {
  const raw = props.match?.jd_source_warning
  if (Array.isArray(raw)) return raw.filter(Boolean).join(' ')
  return raw || null
})
const hardBlocks = computed(() => props.match?.hard_blocks || [])
const keyGaps = computed(() => props.match?.key_gaps || [])
const isGuesswork = computed(() => !hasSolidBasis(props.basis))
const isKnowledgeBase = computed(() => props.basis === 'knowledge_base')

const comparisonLabel = computed(() => {
  if (props.basis === 'user_pasted') return '你粘贴的招聘要求'
  if (isKnowledgeBase.value) return '同类岗位的常见要求（人工整理）'
  return 'AI 推断的岗位要求'
})
</script>

<template>
  <section v-if="match" class="card">
    <div class="card-head">
      <h2>
        {{ isGuesswork ? '岗位匹配度' : '招聘要求匹配' }}
        <span class="sub">
          七个步骤跑完后汇总得出
          · 对照：{{ comparisonLabel }}
        </span>
      </h2>
      <AppBadge
        v-if="isGuesswork"
        :text="isKnowledgeBase ? '没有这家公司的原话，不出匹配度' : '无法给出匹配度'"
        tone="muted"
      />
      <AppBadge v-else :text="jdOverallText(match.overall)" :tone="jdOverallTone(match.overall)" />
    </div>

    <div class="card-body">
      <!-- 没有原话可对照时，摆一排 0 是骗人的 —— 直接说清楚为什么给不出 -->
      <div v-if="isGuesswork" class="notice notice-warn">
        <strong>这次给不出匹配度。</strong>
        {{ requirementBasisText(basis) }} ——
        <template v-if="isKnowledgeBase">
          岗位要求来自人工整理的同类岗位资料，但没有这家公司的招聘原话，"你满足了几条要求"仍然没有一个可对照的答案。
        </template>
        <template v-else>
          没有可对照的招聘要求，"你满足了几条要求"这个问题还没有依据可答。
        </template>
        把招聘页的「任职要求」整段复制到分析页顶部的输入框再跑一次，这里就会按你粘的内容逐条判定。
      </div>

      <template v-else>
        <!-- 统计合并成一本账：一张卡里再套三张同规格小卡，就是后台模板的指纹 -->
        <dl class="ledger">
          <div>
            <dt>满足率</dt>
            <dd :class="{ 'is-mono': rateAvailable }">{{ rateText }}</dd>
          </div>
          <div>
            <dt>对得上原话的要求</dt>
            <dd>{{ match.requirement_total }} 项</dd>
          </div>
          <div>
            <dt>可判定</dt>
            <dd>{{ match.requirement_judged }} 项</dd>
          </div>
          <div>
            <dt>已满足</dt>
            <dd>{{ match.requirement_satisfied }} 项</dd>
          </div>
          <div>
            <dt>存在差距</dt>
            <dd>{{ match.requirement_shortfall }} 项</dd>
          </div>
          <div>
            <dt>证据不足</dt>
            <dd>{{ match.requirement_orphan }} 项</dd>
          </div>
        </dl>

        <p class="faint block-note">
          {{ rateAvailable
            ? `满足率的分母 = 可判定要求 ${match.requirement_judged} 项。`
            : '本次没有可判定要求，因此不出满足率（不是 0%）。' }}
        </p>
      </template>

      <div v-if="warning" class="notice notice-warn block-note">
        <strong>来源提示：</strong>{{ warning }}
      </div>

      <template v-if="!isGuesswork && hardBlocks.length">
        <h3 class="sub-title is-warn block-note">硬性缺口候选（{{ hardBlocks.length }} 项）</h3>
        <p class="faint">原话里写着这项要求，你的当前等级是"无"，而且它被列为硬性门槛。</p>
        <div class="tags">
          <span v-for="block in hardBlocks" :key="block.skill" class="badge badge-danger">
            {{ block.skill }}（要求 {{ block.required_level || '—' }}
            · {{ evidenceStatusText(block.evidence_status) }}）
          </span>
        </div>
      </template>

      <template v-if="!isGuesswork && keyGaps.length">
        <h3 class="sub-title block-note">关键差距（按优先级）</h3>
        <div class="tags">
          <span v-for="gap in keyGaps" :key="gap.skill">
            <AppBadge
              :text="`${gap.skill} · ${gap.priority || '—'} · 重要度 ${formatImportance(gap.importance)}`"
              :tone="priorityTone(gap.priority)"
            />
          </span>
        </div>
      </template>
    </div>
  </section>
</template>
