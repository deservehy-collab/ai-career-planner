<script setup>
import { computed } from 'vue'

import PageShell from '../components/PageShell.vue'

import { usePlansStore } from '../stores/plans'
import { AGENTS, agentColor } from '../utils/agents'

/**
 * 概览：登录后落地的第一屏。它是一条**四站路线**，不是工作台 ——
 * 一眼看完「怎么用」，每一站都能点进去。
 *
 * 版面顺序就是阅读顺序：
 *   ① 路线图——四张站牌高低错落挂在一条主线上，当前站压在线上并盖章；
 *   ② 第 2 站的站牌里塞着七个步骤——这条链路真正跑的东西。
 *
 * 这一页走 `.pg--overview` 变体：不套 .pg-main 的玻璃卡，整页直接摊在
 * 极光底上，只有「现在」这一站是实色。
 *
 * 三条口径：
 * 1. 「现在在哪一站」只看**当前求职档案**（plans store，App 启动时已拉取）：
 *    没有档案 → 新建；有档案没分析 → 首次分析；已有轮次 → 去执行。
 *    不再拿本机缓存的"最近一次分析"冒充当前进度。
 * 2. 运维读数（模型通道、后端是否连得上）不进这页，那是「系统状态」的职责。
 * 3. 完整档案留在「我的档案」；页脚只给一行摘要和去处，不把同一张表摆两遍。
 */

const plans = usePlansStore()

/** 当前状态 → 「现在该做哪一件事」。全页只有这一个主行动。 */
const nextStep = computed(() => {
  if (!plans.hasPlans) {
    return {
      step: 2,
      desc: '还没有求职档案。填好基础资料、粘一段任职要求，就能建立第一份档案并开始分析。',
      to: '/analyze',
      cta: '新建档案',
      alt: { to: '/assistant', label: '方向还没定？先问两句' },
    }
  }
  if (!plans.activeId) {
    return {
      step: 2,
      desc: `账号下已有 ${plans.total} 份档案，先在「我的档案」里选一份，再继续它自己的任务与成长记录。`,
      to: '/home',
      cta: '选择档案',
      alt: { to: '/analyze', label: '或者新建一份' },
    }
  }
  if (!plans.active?.latest_round) {
    return {
      step: 2,
      desc: `档案「${plans.activeTitle}」还没有分析结果。粘一段任职要求进来，就能跑出这份档案的第一轮计划。`,
      to: '/analyze',
      cta: '开始分析',
      alt: { to: '/home', label: '先看看档案' },
    }
  }
  return {
    step: 3,
    desc: `档案「${plans.activeTitle}」已有计划与能力表。照着清单推进，做完交证据。`,
    to: '/tasks',
    cta: '去执行',
    alt: { to: '/growth', label: '看这份档案的成长记录' },
  }
})

const ROUTE = [
  {
    step: 1,
    phase: '提问',
    title: '先问 AI 助手',
    line: '方向、顺序、简历和项目怎么讲，先聊两句。它只说话，不动数据。',
    to: '/assistant',
    cta: '去提问',
  },
  {
    step: 2,
    phase: '发起',
    title: '发起一次求职规划',
    line: '填画像，把招聘要求整段粘进来；什么岗位都能分析。',
    to: '/analyze',
    cta: '去分析',
  },
  {
    step: 3,
    phase: '执行',
    title: '照着清单推进',
    line: '规划落成可执行清单：学习阶段与实战项目，做完一条交证据。',
    to: '/tasks',
    cta: '去执行',
  },
  {
    step: 4,
    phase: '复盘',
    title: '记账，进下一轮',
    line: '完成后升级技能等级，再生成下一轮计划。历史只增不删。',
    to: '/growth',
    cta: '去记账',
  },
]

/** 站牌状态只由本地画像推断：走过的填实线，当前站实色，没到的虚线。 */
const stops = computed(() =>
  ROUTE.map((stop) => ({
    ...stop,
    status:
      stop.step < nextStep.value.step
        ? 'done'
        : stop.step === nextStep.value.step
          ? 'current'
          : 'todo',
  })),
)

const routeState = computed(
  () => `第 ${nextStep.value.step - 1} 站已完成 · 第 ${nextStep.value.step} 站进行中`,
)

/** 第 2 站背后那条链：静态结构来自代码，不给单个节点编假进度。 */
// 短名是给 300px 宽的站牌算过的：六个字以上一颗 chip 就换行，完整名与职责仍在 :title。
const TICK_OVERRIDE = { retrieval: '资料整理' }

const flow = computed(() =>
  AGENTS.map((agent) => ({
    ...agent,
    short: TICK_OVERRIDE[agent.node] || agent.displayName.replace(' Agent', ''),
    color: agentColor(agent.node),
  })),
)

const outputs = [
  { key: 'req', label: '岗位要求', hash: '#card-requirements' },
  { key: 'gap', label: '能力差距', hash: '#card-gaps' },
  { key: 'learning', label: '学习路线', hash: '#card-learning' },
  { key: 'project', label: '项目推荐', hash: '#card-projects' },
]
</script>

<template>
  <PageShell title="概览" variant="overview">
    <section class="ov-map" aria-labelledby="ov-map-title">
      <div class="ov-map-head">
        <h2 id="ov-map-title" class="ov-kicker">路线 <span>/ Route</span></h2>
        <div class="ov-legend" aria-hidden="true">
          <span class="ov-legend-item">
            <span class="ov-legend-line ov-legend-line--done" />已走过
          </span>
          <span class="ov-legend-item">
            <span class="ov-legend-line" />还没到
          </span>
        </div>
        <p class="ov-route-state">{{ routeState }}</p>
      </div>

      <div class="ov-map-plane" :class="`is-step${nextStep.step}`">
        <div class="ov-line ov-line--done" aria-hidden="true">
          <span class="ov-track-start">START</span>
        </div>
        <div class="ov-line ov-line--todo" aria-hidden="true" />

        <ol class="ov-route">
          <li
            v-for="stop in stops"
            :key="stop.step"
            class="ov-stop"
            :class="[`ov-stop--${stop.step}`, `is-${stop.status}`]"
          >
            <p class="ov-stop-kicker">
              <span class="ov-stop-mark">{{ stop.step }}</span>
              <span class="ov-stop-phase">{{ stop.phase }}</span>
            </p>
            <h3 class="ov-stop-title">
              <RouterLink
                class="ov-stop-head"
                :to="stop.to"
                :aria-current="stop.status === 'current' ? 'step' : undefined"
              >{{ stop.title }}</RouterLink>
              <span v-if="stop.status === 'current'" class="ov-stop-flag">你在这</span>
            </h3>
            <p class="ov-stop-line">
              {{ stop.status === 'current' ? nextStep.desc : stop.line }}
            </p>

            <!-- 第 2 站里真正跑的七步：静态结构，不标单节点进度 -->
            <div v-if="stop.step === 2" class="ov-steps">
              <p class="ov-steps-head">
                <span>这一站会跑七步</span>
                <span class="ov-steps-count">7 STEPS</span>
              </p>
              <ol class="ov-chips">
                <li
                  v-for="agent in flow"
                  :key="agent.node"
                  class="ov-tick"
                  :style="{ '--agent-color': agent.color }"
                  :title="`${agent.displayName}：${agent.duty}`"
                >
                  <span class="ov-tick-no">{{ agent.order }}</span>
                  <span class="ov-tick-name">{{ agent.short }}</span>
                </li>
              </ol>
              <p class="ov-stop-outs">
                <span class="ov-stop-outs-label">跑完出</span>
                <RouterLink
                  v-for="item in outputs"
                  :key="item.key"
                  class="ov-chain-out"
                  :to="`/analyze${item.hash}`"
                >{{ item.label }}</RouterLink>
              </p>
            </div>

            <div v-if="stop.status === 'current'" class="ov-stop-act">
              <RouterLink class="btn ov-now-btn" :to="nextStep.to">{{ nextStep.cta }} →</RouterLink>
              <RouterLink class="ov-now-alt" :to="nextStep.alt.to">{{ nextStep.alt.label }}</RouterLink>
            </div>
            <RouterLink v-else class="ov-stop-go" :to="stop.to">{{ stop.cta }} →</RouterLink>
          </li>
        </ol>
      </div>
    </section>

  </PageShell>
</template>
