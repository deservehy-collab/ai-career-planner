<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import AppBadge from './AppBadge.vue'
import { formatDuration } from '../utils/format'
import { runStatusText, runStatusTone } from '../utils/enums'
import { runningAgents, WORKFLOW_ORDER } from '../utils/agents'

/**
 * 后台运行进度卡。
 *
 * run 记录只有四态（queued / running / succeeded / failed），**没有百分比**；
 * 节点级信息来自后端回报的 `progress` / `current_node`，所以这里可以点名
 * "正在跑哪一步、已经完成几步"，但依然不画百分比、不按时间估算剩余。
 */
const props = defineProps({
  status: { type: String, default: 'idle' },
  startedAt: { type: String, default: null },
  finishedAt: { type: String, default: null },
  errorMessage: { type: String, default: '' },
  errorKind: { type: String, default: null },
  title: { type: String, default: '分析进度' },
  progress: { type: Array, default: () => [] },
  currentNode: { type: String, default: null },
})

const now = ref(Date.now())
let timer = null

function startTicking() {
  stopTicking()
  now.value = Date.now()
  timer = setInterval(() => {
    now.value = Date.now()
  }, 1000)
}

function stopTicking() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

const isRunning = computed(() => props.status === 'queued' || props.status === 'running')

watch(
  isRunning,
  (running) => {
    if (running) startTicking()
    else stopTicking()
  },
  { immediate: true },
)

onBeforeUnmount(stopTicking)

const elapsedSeconds = computed(() => {
  if (!props.startedAt) return 0
  const from = new Date(props.startedAt).getTime()
  const to = props.finishedAt ? new Date(props.finishedAt).getTime() : now.value
  if (Number.isNaN(from) || Number.isNaN(to)) return 0
  return Math.max(0, Math.round((to - from) / 1000))
})

const statusText = computed(() => {
  if (props.status === 'idle') return '尚未开始'
  return runStatusText(props.status)
})

const tone = computed(() => runStatusTone(props.status))

const currentAgents = computed(() => runningAgents(props.progress, props.currentNode))
const runningNames = computed(() =>
  currentAgents.value.map((agent) => `「${agent.displayName}」`).join(''),
)
const totalSteps = WORKFLOW_ORDER.length
// 按节点去重：同一步被重复回报（旧记录/重跑）时，不能把"已完成"算到 8/7。
const doneCount = computed(
  () =>
    Math.min(
      new Set(props.progress.filter((item) => item.status === 'done' && item.node).map((item) => item.node))
        .size,
      totalSteps,
    ),
)</script>

<template>
  <section v-if="status !== 'idle'" class="card">
    <div class="card-head">
      <h2>
        {{ title }}
        <span class="sub">在本机 AI 上分析，通常需要几十秒到几分钟</span>
      </h2>
      <AppBadge :text="statusText" :tone="tone" />
    </div>

    <div class="card-body">
      <div v-if="isRunning">
        <div class="progress-indeterminate" />
        <p class="muted run-line">
          <template v-if="status === 'queued'">
            任务已排队，前面跑完就轮到你，请稍候…
          </template>
          <template v-else-if="currentAgents.length === 1">
            正在执行「{{ currentAgents[0].displayName }}」 · 第 {{ currentAgents[0].order }}/{{ totalSteps }} 步…
          </template>
          <template v-else-if="currentAgents.length > 1">
            正在并行执行{{ runningNames }}…
          </template>
          <template v-else>
            正在分析岗位、能力和学习路线…
          </template>
        </p>
        <p class="faint run-line run-line--tight">
          已等待 <strong>{{ formatDuration(elapsedSeconds) }}</strong>
          <template v-if="doneCount"> · 已完成 {{ doneCount }}/{{ totalSteps }} 步</template>
        </p>
      </div>

      <div v-else-if="status === 'succeeded'">
        <div class="notice notice-ok">分析完成，用时 {{ formatDuration(elapsedSeconds) }}。</div>
      </div>

      <div v-else-if="status === 'failed'">
        <div class="notice notice-danger">
          <strong>
            {{
              errorKind === 'network'
                ? '连不上服务'
                : errorKind === 'notfound'
                  ? '这次记录已失效'
                  : errorKind === 'timeout'
                    ? '等待超时'
                    : '分析失败'
            }}
          </strong>
          <div class="run-line run-line--tight">{{ errorMessage }}</div>
          <div class="run-line run-line--loose faint">你的画像已保留，可以直接重新发起。</div>
        </div>
      </div>
    </div>
  </section>
</template>
