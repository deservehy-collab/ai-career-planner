<script setup>
import { computed } from 'vue'

import AppBadge from './AppBadge.vue'

import {
  requirementBasisText,
  requirementBasisTone,
  sourceTypeText,
  sourceTypeTone,
} from '../utils/enums'

/**
 * 本次分析的资料来源账（backend/analyze.py 投影的 `source_report`）。
 *
 * 为什么这张卡不自己算：一条要求的来路散在 evidence_status / importance_basis /
 * level_basis.origin / provenance 四套字段里，后端已经把它们收敛成
 * user_jd / official_data / knowledge_base / model_inference 四值并算好计数。
 * 前端**只读这份结论**，两处各判一次必然会漂成两个真相。
 *
 * 口径只有一条：回查不到证据的，一律算模型推断。
 */
const props = defineProps({
  /** source_report；null 表示这份结果里没有来源统计（较早保存的结果就是这样） */
  report: { type: Object, default: null },
  /** 本次岗位要求的主依据三档（backend/analyze.py requirement_basis） */
  basis: { type: String, default: '' },
})

const ORDER = ['user_jd', 'official_data', 'knowledge_base', 'model_inference']

const counts = computed(() => props.report?.counts || {})
const total = computed(() => counts.value.total ?? 0)
const officialIds = computed(() => props.report?.official_source_ids || [])
const modelOnlySkills = computed(() => props.report?.model_only_skills || [])
const evidenceSplit = computed(() => props.report?.evidence_by_source || null)
const evidenceSentences = computed(() => props.report?.evidence_sentence_count ?? 0)
// 原话与整理稿不是一回事：后端按 provenance 数好了，这里只读，不自己判。
// 旧结果里没有 evidence_by_source，退回"全部按原话计"，宁可少说一句也不编数。
const pastedSentences = computed(() => evidenceSplit.value?.user_jd ?? evidenceSentences.value)
const digestEntries = computed(() => evidenceSplit.value?.official_data ?? 0)
const catalogMatched = computed(() => props.report?.catalog_profile_matched)
const targetJob = computed(() => props.report?.target_job || '这个岗位')
const userJdCount = computed(() => counts.value.user_jd ?? 0)
const modelOnlyCount = computed(() => counts.value.model_inference ?? 0)

/**
 * 陌生岗位提示。四种情况的措辞不能合成一句：
 *   false —— 目录读得好好的，这个岗位确实不在里面
 *   null  —— 目录本身读不出来，不许谎称"不在"
 *   false + 有粘贴原文 —— 画像帮不上，但你粘的原话照样能逐条回查
 *   false + 只有内置摘要 —— 那是这一轮带出来的资料，同样能回查，但不是这家公司的话
 *
 * 「粘了原文」不等于「要求对得上原文」：实测里目录外岗位粘 3 句原文、0 条要求命中，
 * 这时说"照样能逐条回查"就是把模型补的要求算成了有据，所以按后端计数分开讲。
 */
const unknownJob = computed(() => {
  if (!props.report) return null
  if (catalogMatched.value === null) {
    return { tone: 'warn', text: '岗位资料这次读不出来，无法判断这个岗位有没有记录 —— 先按「没有」对待。' }
  }
  if (catalogMatched.value !== false) return null
  if (pastedSentences.value > 0) {
    if (userJdCount.value === 0) {
      return {
        tone: 'warn',
        text:
          `「${targetJob.value}」不在已有的岗位资料里，这次粘贴了 ${pastedSentences.value} 句要求，` +
          `但没有一条岗位要求能对上原话 —— 下面 ${modelOnlyCount.value} 条都按 AI 推断对待，别当硬性要求。`,
      }
    }
    return {
      tone: 'info',
      text:
        `「${targetJob.value}」不在已有的岗位资料里，资料帮不上；你粘贴的 ${pastedSentences.value} 句要求里，` +
        `${userJdCount.value} 条岗位要求能直接对上原话` +
        (modelOnlyCount.value ? `，其余 ${modelOnlyCount.value} 条是 AI 按常识补的` : '') +
        '。',
    }
  }
  if (digestEntries.value > 0) {
    return {
      tone: 'info',
      text:
        `「${targetJob.value}」不在已有的岗位资料里，但这次带出了 ${digestEntries.value} 条同类岗位资料 —— ` +
        '能回查到同一句话，只是它讲的是同类岗位，不是这家公司的要求。',
    }
  }
  return {
    tone: 'danger',
    text:
      `「${targetJob.value}」不在已有的岗位资料里，这次也没有粘贴招聘要求 —— ` +
      '下面每一条岗位要求都是 AI 按常识推断的，没有一句能回查。',
  }
})

/** 没粘原文、只靠内置摘要跑的一轮：把"能回查 ≠ 这家公司的原话"这句话摆在显眼处。 */
const digestOnly = computed(() => {
  if (!props.report) return null
  if (pastedSentences.value > 0 || digestEntries.value === 0) return null
  return {
    tone: 'info',
    text:
      `这次没有粘贴招聘要求，${digestEntries.value} 条依据全部来自同类岗位资料，共 ${officialIds.value.length} 份。` +
      '逐条都能回查到原句，但它们是同类岗位的通行说法，不代表这家公司真实写过 —— 所以不给"满足这份招聘要求"的匹配度。',
  }
})

const shownNotice = computed(() => unknownJob.value || digestOnly.value)
const noticeClass = computed(() => `notice notice-${shownNotice.value?.tone || 'info'}`)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>
        这次用了哪些资料
        <span class="sub">按「能不能回查到原话」数清每条岗位要求的来路，不留模糊地带</span>
      </h2>
      <div class="btn-row">
        <AppBadge
          v-if="basis"
          :text="requirementBasisText(basis)"
          :tone="requirementBasisTone(basis)"
        />
      </div>
    </div>

    <div class="card-body">
      <div v-if="!report" class="empty">
        这份结果里没有资料统计，应该是较早保存下来的一次分析 ——
        重新分析一次就能看到每条要求的来路。
      </div>

      <template v-else>
        <div class="metric-strip">
          <div v-for="key in ORDER" :key="key" class="metric">
            <div class="metric-label">{{ sourceTypeText(key) }}</div>
            <div class="metric-value">{{ counts[key] ?? 0 }} 条</div>
          </div>
          <div class="metric">
            <div class="metric-label">你粘贴的要求</div>
            <div class="metric-value">{{ pastedSentences }} 句</div>
          </div>
          <div v-if="digestEntries" class="metric">
            <div class="metric-label">同类岗位资料</div>
            <div class="metric-value">{{ digestEntries }} 条</div>
          </div>
        </div>

        <div v-if="shownNotice" :class="noticeClass">{{ shownNotice.text }}</div>

        <template v-if="modelOnlySkills.length">
          <h3 class="sub-title is-warn block-note">AI 推断出来的要求（你粘贴的要求和岗位资料里都没有）</h3>
          <div class="tags">
            <AppBadge v-for="skill in modelOnlySkills" :key="skill" :text="skill" tone="danger" />
          </div>
          <p class="faint block-note">
            这几条<strong>没有删掉</strong>，因为 AI 按常识补的要求也可能是对的；
            但学习路线和项目建议里都只把它们当参考，不算硬性要求。
            想让它们变成依据，去招聘页把「任职要求」整段复制过来重新分析一次。
          </p>
        </template>

        <p v-else-if="total" class="faint block-note">
          这次 {{ total }} 条岗位要求都能回查到依据，没有纯靠 AI 补的要求。
        </p>
      </template>
    </div>
  </section>
</template>
