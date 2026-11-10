<script setup>
import AgentTag from './AgentTag.vue'
import AppBadge from './AppBadge.vue'
import { formatImportance } from '../utils/format'
import {
  evidenceKindText,
  gapStatusTone,
  priorityTone,
  sourceTypeText,
  sourceTypeTone,
  verificationText,
} from '../utils/enums'

/**
 * 能力差距（agents/assessment_agent.py）。
 * 条目真实字段：{skill, status, display_status, verification, supported_by,
 *   current_level, required_level, importance, priority, gap,
 *   evidence_kind, evidence_ref, evidence_note, requirement_source?}
 *
 * status / display_status / priority 全部由代码判定（不是模型说的）。
 * 等级达标但没有项目或成长记录时，display_status 是「疑似具备」/「未验证」，
 * 不会显示成「已满足」。
 *
 * `requirement_source` 说的是**这条岗位要求本身**从哪来（后端 source_attribution 算的）：
 * 与左边"学生证据"是两个轴 —— 一个是岗位要得不硬，一个是你有没有实据。
 * 旧结果里没有这个字段，就不摆。
 */
defineProps({
  gaps: { type: Array, default: () => [] },
  showSupportedBy: { type: Boolean, default: true },
  /** 可选的 Agent 归属标记（传节点名才显示） */
  agentNode: { type: String, default: '' },
})
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h2>
        能力差距
        <span class="sub">等级与证据分开判定：等级达标但缺项目/成长记录时标为疑似具备或未验证</span>
      </h2>
      <div class="btn-row">
        <span class="badge">{{ gaps.length }} 项</span>
        <AgentTag v-if="agentNode" :node="agentNode" />
      </div>
    </div>

    <div class="card-body">
      <div v-if="!gaps.length" class="empty">没有能力差距数据。</div>

      <div v-else class="table-wrap">
        <table class="data data--gaps">
          <thead>
            <tr>
              <th>技能</th>
              <th>状态</th>
              <th>优先级</th>
              <th>当前 → 要求</th>
              <th>重要度</th>
              <th>差距说明 / 证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in gaps" :key="item.skill">
              <td>
                <strong>{{ item.skill }}</strong>
                <div v-if="item.requirement_source" class="faint">
                  <AppBadge
                    :text="sourceTypeText(item.requirement_source)"
                    :tone="sourceTypeTone(item.requirement_source)"
                  />
                </div>
              </td>
              <td>
                <AppBadge
                  :text="item.display_status || item.status"
                  :tone="gapStatusTone(item.display_status || item.status)"
                />
              </td>
              <td><AppBadge :text="item.priority" :tone="priorityTone(item.priority)" /></td>
              <td class="muted">
                {{ item.current_level || '无' }} → {{ item.required_level || '未要求' }}
              </td>
              <td>{{ formatImportance(item.importance) }}</td>
              <td class="muted">
                {{ item.gap || '—' }}
                <div
                  v-if="showSupportedBy && item.supported_by && item.supported_by !== '无'"
                  class="faint"
                >
                  支撑技能：{{ item.supported_by }}
                  <template v-if="item.evidence_kind">
                    · {{ evidenceKindText(item.evidence_kind) }}
                  </template>
                </div>
                <div v-if="item.evidence_ref" class="faint">佐证：{{ item.evidence_ref }}</div>
                <div
                  v-if="item.evidence_note && item.evidence_note !== item.evidence_ref"
                  class="faint"
                >
                  证据：{{ item.evidence_note }}
                </div>
                <div
                  v-if="item.verification && item.verification !== 'direct'"
                  class="faint is-warn"
                >
                  {{ verificationText(item.verification) }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
