<script setup>
import { computed } from 'vue'

import AppBadge from './AppBadge.vue'
import { AGENTS, AGENT_GROUP, getAgent, runningAgents } from '../utils/agents'

/**
 * Agent 工作流流程轨。
 *
 * 状态来源分两层
 * --------------
 * - **执行中 / 失败**：读后端 run 记录的 ``progress``/``current_node``（backend/jobs.py）。
 *   这是图执行时自己回报的节点状态（running/done/failed/skipped/canceled），
 *   所以可以点名"正在跑哪一步、断在哪一步"；字段缺失（旧记录）时退回整条轨的说法，
 *   不按时间猜单节点状态。
 * - **成功之后**：再用 summarizeWorkflow(result) 的真实产出计数回填各节点。
 */
const props = defineProps({
  /** summarizeWorkflow(result) 的结果；null 表示还没有结果 */
  summary: { type: Object, default: null },
  /** 运行状态：idle / queued / running / succeeded / failed */
  status: { type: String, default: 'idle' },
  /** 后端回报的节点进度（TaskRecord.progress） */
  progress: { type: Array, default: () => [] },
  /** 当前正在执行的节点名 */
  currentNode: { type: String, default: null },
})

const emit = defineEmits(['navigate'])

const PROGRESS_BADGE = {
  running: { text: '进行中', tone: 'primary' },
  done: { text: '已完成', tone: 'ok' },
  failed: { text: '失败', tone: 'danger' },
  canceled: { text: '已停止', tone: 'warn' },
  skipped: { text: '已跳过', tone: 'muted' },
}

const isRunning = computed(() => props.status === 'queued' || props.status === 'running')
const isInterrupted = computed(() => props.status === 'failed')

const progressByNode = computed(() => {
  const map = {}
  for (const item of props.progress) {
    if (item?.node) map[item.node] = item
  }
  return map
})

function stateOf(agent) {
  const item = progressByNode.value[agent.node]
  return item ? PROGRESS_BADGE[item.status] ?? null : null
}

const currentAgents = computed(() => runningAgents(props.progress, props.currentNode))
const runningNames = computed(() =>
  currentAgents.value.map((agent) => `「${agent.displayName}」`).join(''),
)
const stalledAgent = computed(() => {
  const stalled = [...props.progress]
    .reverse()
    .find((item) => item.status === 'failed' || item.status === 'canceled')
  return stalled ? getAgent(stalled.node) : null
})
const stalledStatus = computed(() => {
  const stalled = [...props.progress]
    .reverse()
    .find((item) => item.status === 'failed' || item.status === 'canceled')
  return stalled?.status ?? null
})

/**
 * 单个节点的产出摘要；没有结果时返回 null（节点整体呈中性，不显示任何产出）。
 * present === false 表示"结果里根本没有该节点的产出字段"——一无所知，不能说它失败或降级。
 */
function outputOf(agent) {
  const item = props.summary?.[agent.node]
  if (!item) return null

  if (item.present === false) {
    return { text: '暂无结果', tone: 'muted', note: item.note || '' }
  }

  return {
    text: `${item.count} ${agent.outputLabel}`,
    tone: item.degraded ? 'warn' : item.count > 0 ? 'ok' : 'muted',
    note: item.note || '',
  }
}

/** 预计算每个节点要渲染的全部内容，模板里不再重复计算。 */
const nodes = computed(() =>
  AGENTS.map((agent) => {
    const group = AGENT_GROUP[agent.group]
    return {
      ...agent,
      colorStyle: { '--agent-color': group.color, '--agent-soft': group.soft },
      output: outputOf(agent),
      state: stateOf(agent),
      tip: agent.duty,
    }
  }),
)

function goTo(agent) {
  if (!props.summary) return
  const target = document.getElementById(agent.anchor)
  if (!target) return
  target.scrollIntoView({ behavior: 'smooth', block: 'start' })
  emit('navigate', agent.node)
}
</script>

<template>
  <section class="rail" aria-label="规划步骤">
    <div class="rail-head">
      <h2>规划步骤</h2>
      <AppBadge v-if="isRunning" text="执行中" tone="primary" />
      <AppBadge v-else-if="isInterrupted" text="执行中断" tone="danger" />
      <AppBadge v-else-if="summary" text="已完成" tone="ok" />
      <AppBadge v-else text="待运行" tone="muted" />
    </div>

    <div class="agent-rail" :class="{ 'is-running': isRunning, 'is-interrupted': isInterrupted }">
      <button
        v-for="node in nodes"
        :key="node.node"
        type="button"
        class="agent-node"
        :class="{ 'is-idle': !summary, 'is-degraded': node.output?.tone === 'warn' }"
        :style="node.colorStyle"
        :disabled="!summary"
        :title="node.tip"
        @click="goTo(node)"
      >
        <span class="agent-node-top">
          <span class="agent-tag-num">{{ node.order }}</span>
          <span class="agent-node-name">{{ node.displayName }}</span>
        </span>

        <span class="agent-node-duty">{{ node.duty }}</span>

        <span v-if="node.state" class="agent-node-state">
          <AppBadge :text="node.state.text" :tone="node.state.tone" />
        </span>

        <template v-if="node.output">
          <span class="agent-node-out">
            <AppBadge :text="node.output.text" :tone="node.output.tone" />
          </span>
          <span v-if="node.output.note" class="agent-node-note">{{ node.output.note }}</span>
        </template>
      </button>
    </div>

    <div v-if="isRunning" class="notice notice-info">
      <template v-if="status === 'queued'">
        任务已排队，前面跑完就轮到你，请稍候…
      </template>
      <template v-else-if="currentAgents.length === 1">
        正在执行「{{ currentAgents[0].displayName }}」 · 第 {{ currentAgents[0].order }}/7 步
      </template>
      <template v-else-if="currentAgents.length > 1">
        正在并行执行{{ runningNames }}
      </template>
      <template v-else>
        七个步骤正在依次进行，全部跑完才会给出结果。
      </template>
    </div>

    <div v-else-if="isInterrupted" class="notice notice-danger">
      <template v-if="stalledAgent && stalledStatus === 'canceled'">
        「{{ stalledAgent.displayName }}」这一步被停止，档案和已完成的结果都保留。
      </template>
      <template v-else-if="stalledAgent">
        「{{ stalledAgent.displayName }}」这一步中断了，具体原因见右侧进度卡。
      </template>
      <template v-else>
        执行中断了。暂时看不出是哪一步出的问题，具体原因见右侧进度卡。
      </template>
    </div>
  </section>
</template>
