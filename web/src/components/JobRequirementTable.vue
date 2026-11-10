<script setup>
import { computed } from 'vue'

import AgentTag from './AgentTag.vue'
import AppBadge from './AppBadge.vue'
import EvidenceRefChip from './EvidenceRefChip.vue'

import { formatImportance } from '../utils/format'
import {
  evidenceStatusText,
  evidenceStatusTone,
  importanceBasisText,
  importanceBasisTone,
  levelOriginText,
  levelOriginTone,
  sourceTypeText,
  sourceTypeTone,
} from '../utils/enums'

/**
 * 岗位要求（agents/job_agent.py）。
 * 真实字段：{skill, importance(1~5), level, reason, evidence_status, evidence_refs[],
 *   importance_basis, level_basis{origin, quote, by_company},
 *   source_notes[]（本条要求反查到的原话句子）}
 *
 * 证据强度必须三态分开：
 *   supported           原话里逐字写着这个技能
 *   partially_supported 只靠别名/语义对上，强度弱于前者
 *   model_supplement    原话里根本没有，这条是模型补充的 → 渲染成**不可点击的占位芯片**，
 *                       绝不能让它看起来像有证据
 *
 * evidence_refs 是一串 E 编号（如 ["E02","E07"]），点击后由父组件定位到原文面板的对应句子。
 *
 * basis（三档，见 utils/enums REQUIREMENT_BASIS）决定"依据"一列摆什么：
 *   user_pasted  用户当场粘贴的招聘要求原文 → 直接反显原话
 *   knowledge_base 没有原文，靠内置岗位画像 → 原话栏写画像依据
 *   model_only   两者都没有 → 整栏标灰，说明这条只是模型推测
 *
 * `citations` 是后端 `source_report.citations`，与本表**逐条同序**：
 * 每条要求的来路（原文/公开资料/画像/模型推断）由后端算好，前端只摆不判。
 * 不传（较早保存的结果没有 source_report）时退回本行原有的证据强度表达。
 */
const props = defineProps({
  requirements: { type: Array, default: () => [] },
  /** 可选的 Agent 归属标记（传节点名才显示） */
  agentNode: { type: String, default: '' },
  /** 当前被选中的证据编号，用于芯片高亮 */
  activeRef: { type: String, default: '' },
  /** 本次岗位要求的主依据档位 */
  basis: { type: String, default: 'model_only' },
  /** source_report.citations：与 requirements 同序的逐条来源视图 */
  citations: { type: Array, default: null },
})

const emit = defineEmits(['select-ref'])

const isUserJd = computed(() => props.basis === 'user_pasted')
const isGuesswork = computed(() => props.basis === 'model_only')

const basisColumnLabel = computed(() => (isUserJd.value ? '你粘的原话' : '依据'))

/** 没有原话时那句实话 —— 三档各有各的说法，不能统一写成"无证据"糊过去。 */
const noSourceHint = computed(() =>
  isUserJd.value ? '你粘贴的要求里没有这句，是 AI 补充的' : '没有任何依据材料',
)

/** 按下标取这一行的来源视图；越界或缺失返回 null（旧结果就是这样）。 */
function citationAt(index) {
  const item = props.citations?.[index]
  return item && typeof item === 'object' ? item : null
}

function refsOf(item) {
  return Array.isArray(item?.evidence_refs) ? item.evidence_refs.filter(Boolean) : []
}

/** 本条要求反查到的原话句子。 */
function notesOf(item) {
  return (Array.isArray(item?.source_notes) ? item.source_notes : []).filter(Boolean)
}

/** 标题下那句副标题：三档依据各有各的说法，不能统一写成"模型推测"。 */
const subText = computed(() => {
  if (props.basis === 'user_pasted') return '每一条都从你粘贴的招聘要求里得出，「依据」一栏摆出对应的原话'
  if (props.basis === 'knowledge_base') return '没有粘贴招聘要求：技能清单按同类岗位的常见要求整理，资料里没覆盖到的条目才由 AI 补充'
  return '既没有粘贴招聘要求，岗位也不在已有资料里：以下每条都是 AI 按常识推测，只能当思路参考'
})

function levelQuote(item) {
  return item?.level_basis?.quote || ''
}</script>

<template>
  <section class="card" :class="{ 'is-uncertain': isGuesswork }">
    <div class="card-head">
      <h2>
        岗位要求
        <span class="sub">{{ subText }}</span>
      </h2>
      <div class="btn-row">
        <span class="badge">{{ requirements.length }} 项</span>
        <AgentTag v-if="agentNode" :node="agentNode" />
      </div>
    </div>

    <div class="card-body">
      <div v-if="!requirements.length" class="empty">没有生成岗位要求。</div>

      <div v-else class="table-wrap">
        <table class="data data--req">
          <thead>
            <tr>
              <th>技能</th>
              <th>重要度</th>
              <th>证据强度</th>
              <th>编号</th>
              <th>{{ basisColumnLabel }}</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, i) in requirements" :key="item.skill">
              <td>
                <strong>{{ item.skill }}</strong>
                <AppBadge
                  v-if="citationAt(i)"
                  class="req-source"
                  :text="sourceTypeText(citationAt(i).source_type)"
                  :tone="sourceTypeTone(citationAt(i).source_type)"
                />
                <div v-if="item.level" class="faint">
                  要求等级：{{ item.level }}
                  <span v-if="item.level_basis?.origin">（{{ levelOriginText(item.level_basis.origin) }}）</span>
                </div>
                <div v-if="levelQuote(item)" class="faint">原话：「{{ levelQuote(item) }}」</div>
              </td>

              <td>
                {{ formatImportance(item.importance) }}
                <div v-if="item.importance_basis" class="faint">
                  <AppBadge
                    :text="importanceBasisText(item.importance_basis)"
                    :tone="importanceBasisTone(item.importance_basis)"
                  />
                </div>
              </td>

              <td>
                <AppBadge
                  :text="evidenceStatusText(item.evidence_status)"
                  :tone="evidenceStatusTone(item.evidence_status)"
                />
              </td>

              <!-- 可追溯：编号来自证据数组位置，点击跳到对应原文 -->
              <td>
                <div v-if="refsOf(item).length" class="tags">
                  <EvidenceRefChip
                    v-for="code in refsOf(item)"
                    :key="code"
                    :code="code"
                    :active="activeRef === code"
                    @select="emit('select-ref', $event)"
                  />
                </div>
                <EvidenceRefChip v-else code="" placeholder="无证据支撑" />
              </td>

              <td>
                <details v-if="notesOf(item).length" class="fold">
                  <summary>原话 {{ notesOf(item).length }} 句</summary>
                  <div class="fold-body">
                    <ul class="quote-list">
                      <li v-for="(note, k) in notesOf(item)" :key="k">「{{ note }}」</li>
                    </ul>
                  </div>
                </details>
                <span v-else class="faint">{{ noSourceHint }}</span>
              </td>

              <td class="muted">{{ item.reason || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="requirements.length" class="faint block-note">
        <template v-if="isUserJd">
          标着「AI 补充」的条目<strong>不在你粘贴的内容里</strong>，是 AI 按常识补的 ——
          可以不当真，也可以回去核对招聘页是否漏粘了一段。
        </template>
        <template v-else-if="basis === 'knowledge_base'">
          这次没有粘贴招聘要求，技能清单来自<strong>同类岗位的常见要求（人工整理）</strong> ——
          有据可查，但说的是"这类岗位一般要什么"，不等于这家公司写的门槛；
          资料里没覆盖到的条目仍由 AI 补充，重要度与等级仍以整理稿为准。
        </template>
        <template v-else>
          这次既没有粘贴招聘要求，岗位也不在已有资料里，上面每一条都是 AI 按行业常识推断的，
          <strong>没有原话可核对</strong>，因此也不给匹配度结论。
        </template>
      </p>
    </div>
  </section>
</template>
