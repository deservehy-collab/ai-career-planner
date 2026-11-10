<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import AgentTag from './AgentTag.vue'
import AppBadge from './AppBadge.vue'
import EvidenceRefChip from './EvidenceRefChip.vue'

import { buildReferencedBy, evidenceCode } from '../utils/evidence'
import {
  requirementTypeText,
  requirementTypeTone,
  retrievalReasonText,
  retrievalStatusText,
  retrievalStatusTone,
} from '../utils/enums'

/**
 * 岗位依据面板：把「逐句依据 + 反向引用」合成一张卡。
 *
 * 硬约束
 * ------
 * 1. 每条依据的编号来自**数组位置**（见 utils/evidence.js），与后端 `evidence_ref()` 同规则；
 *    分组只影响显示顺序，绝不重排编号。
 * 2. 三种来路分开呈现：你粘贴的原文（``user_pasted``）、内置资料摘要
 *    （``official_digest``）、以及较早版本留下的知识库条目。把它们混在一栏里统称为
 *    「你提供的原文」，就是替一家从没看过的公司说话了。
 * 3. 没有依据时**如实空着**并说明原因，绝不拿模型推测冒充依据。
 * 4. 这里描述的是**岗位要什么**，不是**学生会什么**，副标题里明确写出来。
 */
const props = defineProps({
  /** job_evidence；null 表示结果里没有这个字段（与"空数组"不是一回事） */
  evidence: { type: Array, default: null },
  /** retrieval_status：user_pasted / official_digest / no_source */
  status: { type: String, default: '' },
  /** retrieval_diagnostics：句数、被过滤的超长部分、空原文的原因、coverage 等 */
  diagnostics: { type: Object, default: null },
  /** job_requirements，用于计算"这条依据被哪些岗位要求引用"；不传则不显示该项 */
  requirements: { type: Array, default: null },
  /** 父组件要求高亮的编号（如从岗位要求表点过来的 E 编号） */
  activeRef: { type: String, default: '' },
})

const emit = defineEmits(['select'])

const items = computed(() => (Array.isArray(props.evidence) ? props.evidence : null))
const count = computed(() => (items.value ? items.value.length : 0))
const dropped = computed(() => props.diagnostics?.dropped_sentences ?? 0)
const droppedEntries = computed(() => props.diagnostics?.dropped_entries ?? 0)
const reason = computed(() => retrievalReasonText(props.diagnostics?.reason))

/** 按 provenance 分组的顺序与措辞；未分组的旧条目落在 last。 */
const GROUP_META = {
  user_pasted: {
    key: 'user_pasted',
    title: '你粘贴的招聘要求',
    unit: '句原话',
    hint: '逐字摘自你复制进来的内容 —— 只有这一档是这家公司真实写过的话。',
  },
  official_digest: {
    key: 'official_digest',
    title: '同类岗位资料',
    unit: '条资料',
    hint: '来自人工整理的公开标准和招聘要求，能回查同一句话，但它是同类岗位的通行说法，不是这家公司的原话。',
  },
  other: {
    key: 'other',
    title: '其他资料条目',
    unit: '条旧依据',
    hint: '较早版本留下的资料检索结果，来源标记不完整，请按内容自行判断。',
  },
}
const GROUP_ORDER = ['user_pasted', 'official_digest', 'other']

const groups = computed(() => {
  if (!items.value) return []
  const buckets = new Map()
  items.value.forEach((item, index) => {
    const provenance = String(item?.provenance || '')
    const key = provenance === 'user_pasted' || provenance === 'official_digest' ? provenance : 'other'
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push({ code: evidenceCode(index), item })
  })
  return GROUP_ORDER.filter((key) => buckets.has(key)).map((key) => ({
    ...GROUP_META[key],
    list: buckets.get(key),
  }))
})

const headline = computed(() => {
  if (!items.value) return '岗位依据'
  const keys = groups.value.map((group) => group.key)
  if (keys.includes('user_pasted')) return '你提供的招聘要求'
  if (keys.includes('official_digest')) return '同类岗位资料（不是你粘贴的要求）'
  return '本次岗位依据'
})

const subtitle = computed(() => {
  const keys = groups.value.map((group) => group.key)
  if (keys.includes('user_pasted')) {
    return '下面每一句都是从你粘贴的内容里切出来的原话 —— 描述「岗位要什么」，不是「学生会什么」'
  }
  if (keys.includes('official_digest')) {
    return '这次没有粘贴招聘要求，下面每条都来自同类岗位资料 —— 可以回查，但不是这家公司的原话'
  }
  return '描述「岗位要什么」，不是「学生会什么」'
})

// 反向引用：编号 → 引用了它的岗位要求技能名
const referencedBy = computed(() => buildReferencedBy(props.requirements || [], items.value || []))

/** 本地闪光态：3 秒后自动收起，这样重复点击同一个编号能再次触发动画。 */
const flash = ref('')
let timer = null

const itemEls = new Map()
function setItemEl(code) {
  return (el) => {
    if (el) itemEls.set(code, el)
    else itemEls.delete(code)
  }
}

watch(
  () => props.activeRef,
  (code) => {
    if (!code) return
    flash.value = code
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      flash.value = ''
      timer = null
    }, 3000)

    const el = itemEls.get(code)
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <section id="card-evidence" class="card">
    <div class="card-head">
      <h2>
        {{ headline }}
        <span class="sub">{{ subtitle }}</span>
      </h2>
      <div class="btn-row">
        <AppBadge
          v-if="status"
          :text="retrievalStatusText(status)"
          :tone="retrievalStatusTone(status)"
        />
        <AppBadge v-if="items !== null" :text="`${count} 条依据`" :tone="count ? 'primary' : 'warn'" />
        <AgentTag node="retrieval" />
      </div>
    </div>

    <div class="card-body">
      <!-- 没有依据 ≠ 结果缺项：这两种情况的措辞必须分开 -->
      <div v-if="items === null" class="empty">
        这次没有拿到岗位依据。
      </div>

      <div v-else-if="!items.length" class="empty">
        {{ reason || '这次没有粘贴招聘要求，岗位也不在已有的岗位资料里' }} ——
        岗位要求仍会生成，但全部是 AI 推测，没有一句能回查。
        <strong>想让它当真，把招聘页的「任职要求」粘到上面那一栏。</strong>
      </div>

      <template v-else>
        <p v-if="dropped" class="notice notice-warn block-note">
          粘贴的内容太长，超出 {{ items.length }} 句的部分没有参与分析（已丢弃 {{ dropped }} 句）。
        </p>
        <p v-if="droppedEntries" class="notice notice-warn block-note">
          同类岗位资料太多，这次只带入了最重要的 {{ count }} 条（另有 {{ droppedEntries }} 条没参与分析）。
        </p>

        <details
          v-for="group in groups"
          :key="group.key"
          class="fold"
          :open="group.key === 'user_pasted'"
        >
          <summary>{{ group.title }}：{{ group.list.length }} {{ group.unit }}</summary>
          <div class="fold-body">
            <p class="faint block-note">{{ group.hint }}</p>
            <article
              v-for="entry in group.list"
              :key="entry.code"
              :ref="setItemEl(entry.code)"
              class="evidence-item"
              :class="{ 'is-flashing': flash === entry.code }"
            >
              <header class="evidence-head">
                <EvidenceRefChip
                  :code="entry.code"
                  :active="flash === entry.code"
                  @select="emit('select', $event)"
                />
                <strong v-if="entry.item.skill">{{ entry.item.skill }}</strong>
                <AppBadge
                  v-if="entry.item.requirement_type"
                  :text="requirementTypeText(entry.item.requirement_type)"
                  :tone="requirementTypeTone(entry.item.requirement_type)"
                />
              </header>

              <p class="evidence-quote">{{ entry.item.note || '（这条没有引用原句）' }}</p>

              <div
                v-if="(requirements || []).length && referencedBy[entry.code]?.length"
                class="evidence-meta"
              >
                <span class="evidence-refback">
                  被 {{ referencedBy[entry.code].length }} 条岗位要求引用：
                  {{ referencedBy[entry.code].join('、') }}
                </span>
              </div>
            </article>
          </div>
        </details>
      </template>
    </div>
  </section>
</template>
