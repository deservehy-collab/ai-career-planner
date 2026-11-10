<script setup>
import { computed } from 'vue'

import AppBadge from './AppBadge.vue'
import { goalBasisText, goalStatusText, goalStatusTone } from '../utils/enums'

/**
 * 目标结局卡：只在"收尾"时出现。
 *
 * - achieved            全部要求都有直接证据，且要求本身依据可靠；
 * - basis_insufficient  能力表已达标，但岗位要求是模型推断，不能宣布达成。
 *
 * in_progress 不渲染这张卡 —— 那还没到结局。
 */
const props = defineProps({
  goal: { type: Object, required: true },
  targetJob: { type: String, default: '' },
})

const isAchieved = computed(() => props.goal.status === 'achieved')
const ungrounded = computed(() => props.goal.ungrounded_requirements || [])
</script>

<template>
  <section class="card goal-card" :class="isAchieved ? 'goal-card--done' : 'goal-card--warn'">
    <div class="card-head">
      <h2>
        {{ isAchieved ? '目标结局' : '目标结局：无法确认' }}
        <span class="sub">由岗位要求与证据逐项判定，不看任务完成了多少</span>
      </h2>
      <AppBadge :text="goalStatusText(goal.status)" :tone="goalStatusTone(goal.status)" />
    </div>

    <div class="card-body">
      <p class="goal-message">{{ goal.message }}</p>

      <dl class="kv kv--mid">
        <dt>目标岗位</dt>
        <dd>{{ targetJob || '—' }}</dd>
        <dt>要求依据</dt>
        <dd>{{ goalBasisText(goal.basis) }}</dd>
        <dt>达标情况</dt>
        <dd>
          {{ goal.satisfied_count ?? 0 }} / {{ goal.required_count ?? 0 }} 项
          有项目或成长记录作为直接证据
        </dd>
      </dl>

      <div v-if="isAchieved" class="notice notice-ok sub-block">
        <strong>这个目标到此收尾。</strong>
        <div class="notice-body">
          系统不会自动生成下一轮任务。想继续，去「岗位分析」换一个目标岗位，
          或粘贴一份新的招聘要求重新分析。
        </div>
      </div>

      <div v-else class="notice notice-warn sub-block">
        <strong>不能宣布达成。</strong>
        <div class="notice-body">
          把真实招聘要求粘到「岗位分析」再跑一次，确认这份要求本身有出处，
          才能判断是否真的达标。
        </div>
        <ul v-if="ungrounded.length" class="mini-list">
          <li v-for="skill in ungrounded" :key="skill">
            「{{ skill }}」是 AI 补充的，你粘贴的招聘要求里没有出现
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>
