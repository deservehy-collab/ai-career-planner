<script setup>
import { computed } from 'vue'

import AgentTag from './AgentTag.vue'
import AppBadge from './AppBadge.vue'
import EvidenceRefChip from './EvidenceRefChip.vue'

import {
  profileOriginText,
  profileOriginTone,
  profileSourceText,
  profileSourceTone,
} from '../utils/enums'

/**
 * 岗位画像（agents/job_profile.py）。
 * 真实字段：{target_job, summary, core_tasks[], tools[], deliverables[],
 *   entry_requirements[], career_path[]}，每条是
 *   {name, detail, source(jd|standard|model), evidence_refs[], source_notes[]}
 *
 * 这三档来源是**代码回查出来的**，不是模型自报的：原话里找不到就标 model。
 * origin（整份画像从哪来）与每条的 source 是两个轴，分开摆。
 */
const props = defineProps({
  profile: { type: Object, default: null },
  /** job_profile_origin：cache / generated / knowledge_base / unavailable */
  origin: { type: String, default: '' },
  warnings: { type: Array, default: () => [] },
  /** 可选的 Agent 归属标记（传节点名才显示） */
  agentNode: { type: String, default: '' },
  activeRef: { type: String, default: '' },
})

const emit = defineEmits(['select-ref'])

const SECTIONS = [
  ['core_tasks', '核心工作任务'],
  ['tools', '常用工具与方法'],
  ['deliverables', '交付物'],
  ['entry_requirements', '入行门槛'],
  ['career_path', '成长台阶'],
]

const entries = computed(() =>
  SECTIONS.map(([key, label]) => ({
    key,
    label,
    items: Array.isArray(props.profile?.[key]) ? props.profile[key] : [],
  })).filter((section) => section.items.length),
)

const entryCount = computed(() => entries.value.reduce((sum, s) => sum + s.items.length, 0))

function refsOf(item) {
  return Array.isArray(item?.evidence_refs) ? item.evidence_refs.filter(Boolean) : []
}

function notesOf(item) {
  return (Array.isArray(item?.source_notes) ? item.source_notes : []).filter(Boolean)
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>
        岗位资料
        <span class="sub">这个岗位日常做什么、用什么工具、交付什么、怎么入行 —— 每条都标了是从哪来的</span>
      </h2>
      <div class="btn-row">
        <AppBadge
          v-if="origin"
          :text="profileOriginText(origin)"
          :tone="profileOriginTone(origin)"
        />
        <span v-if="entryCount" class="badge">{{ entryCount }} 条</span>
        <AgentTag v-if="agentNode" :node="agentNode" />
      </div>
    </div>

    <div class="card-body">
      <div v-if="!profile || !entryCount" class="empty">
        本次没有可用的岗位资料。
        <span v-if="origin === 'unavailable'">
          岗位资料没有生成出来，岗位要求与学习路线仍按已有依据正常产出。
        </span>
      </div>

      <template v-else>
        <p v-if="profile.summary" class="muted">{{ profile.summary }}</p>

        <div v-for="(warn, i) in warnings" :key="`w-${i}`" class="notice notice-warn block-note">
          {{ warn }}
        </div>

        <div class="grid grid-2">
          <section v-for="section in entries" :key="section.key">
            <h3 class="sub-title">{{ section.label }}</h3>
            <ul class="plain-list">
              <li v-for="item in section.items" :key="`${section.key}-${item.name}`">
                <strong>{{ item.name }}</strong>
                <AppBadge
                  :text="profileSourceText(item.source)"
                  :tone="profileSourceTone(item.source)"
                />
                <div v-if="refsOf(item).length" class="tags">
                  <EvidenceRefChip
                    v-for="code in refsOf(item)"
                    :key="code"
                    :code="code"
                    :active="activeRef === code"
                    @select="emit('select-ref', $event)"
                  />
                </div>
                <p v-if="item.detail" class="faint">{{ item.detail }}</p>
                <details v-if="notesOf(item).length" class="fold">
                  <summary>对应原话 {{ notesOf(item).length }} 句</summary>
                  <div class="fold-body">
                    <ul class="quote-list">
                      <li v-for="(note, i) in notesOf(item)" :key="i">「{{ note }}」</li>
                    </ul>
                  </div>
                </details>
              </li>
            </ul>
          </section>
        </div>

        <p class="faint block-note">
          「你粘贴的要求里有」= 这句能回到你粘的内容里核对；「同类岗位的常见要求（人工整理）」= 出自人工维护的整理稿，
          不是这家公司的说法；「AI 补充（要求里没写）」= 你粘的内容里没有，只能当思路参考。
        </p>
      </template>
    </div>
  </section>
</template>
