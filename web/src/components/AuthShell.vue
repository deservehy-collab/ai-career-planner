<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 认证页骨架：编辑器式封面 —— 顶栏 → 中间规划画布 → 右侧属性面板。
 *
 * 登录表单就是右侧那块"属性面板"，左侧画布展示产品自己的工作流：
 * 目标岗位 → 岗位画像 → 能力差距 → 学习任务 → 成长记录。
 * 画布纯装饰（对读屏隐藏），页面不发任何请求。
 */
defineProps({
  title: { type: String, required: true },
})

// 节点宽度是百分比、高度随文字变，连线只能量出来再画，否则不同宽度下会脱节
const canvasRef = ref(null)
const wires = ref([])
let observer = null

const LINKS = [
  { from: 'input', to: 'analysis', axis: 'h' },
  { from: 'analysis', to: 'gap', axis: 'v' },
  { from: 'tasks', to: 'gap', axis: 'h' },
  { from: 'tasks', to: 'growth', axis: 'v' },
]

const round = (n) => Math.round(n * 10) / 10

function buildWire(a, b, axis, base) {
  if (axis === 'h') {
    const x1 = round(a.right - base.left)
    const y1 = round(a.top - base.top + a.height / 2)
    const x2 = round(b.left - base.left)
    const y2 = round(b.top - base.top + b.height / 2)
    const dx = Math.max(26, Math.abs(x2 - x1) / 2)
    return { d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`, x1, y1, x2, y2 }
  }
  const x1 = round(a.left - base.left + a.width / 2)
  const y1 = round(a.bottom - base.top)
  const x2 = round(b.left - base.left + b.width / 2)
  const y2 = round(b.top - base.top)
  const dy = Math.max(22, Math.abs(y2 - y1) / 2)
  return { d: `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`, x1, y1, x2, y2 }
}

function measure() {
  const canvas = canvasRef.value
  if (!canvas) return
  const base = canvas.getBoundingClientRect()
  if (base.width < 240 || base.height < 160) {
    wires.value = []
    return
  }
  const rects = {}
  canvas.querySelectorAll('[data-node]').forEach((el) => {
    rects[el.dataset.node] = el.getBoundingClientRect()
  })
  wires.value = LINKS.filter((l) => rects[l.from] && rects[l.to]).map((l) =>
    buildWire(rects[l.from], rects[l.to], l.axis, base),
  )
}

onMounted(() => {
  measure()
  if (typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(measure)
  observer.observe(canvasRef.value)
  canvasRef.value.querySelectorAll('[data-node]').forEach((el) => observer.observe(el))
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})
</script>

<template>
  <div class="auth-shell">
    <header class="auth-top">
      <div class="auth-mark">
        <span class="auth-logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round">
            <circle cx="5" cy="12" r="2.2" />
            <circle cx="19" cy="6" r="2.2" />
            <circle cx="19" cy="18" r="2.2" />
            <path d="M7.2 11l9.6-4M7.2 13l9.6 4" />
          </svg>
        </span>
        <span>AI 职业规划平台</span>
      </div>
    </header>

    <div class="auth-main">
      <section class="auth-stage">
        <h1 class="auth-headline">把求职这条路<br />画成一张能走的图</h1>

        <div class="auth-canvas-bar" aria-hidden="true">
          <span class="auth-canvas-path"><b>2026 秋招 · 产品经理</b><i>/</i>规划画布</span>
          <span class="auth-canvas-tools"><i>自动布局</i><i>网格 16</i><i>100%</i></span>
        </div>

        <div ref="canvasRef" class="auth-canvas" aria-hidden="true">
          <svg class="auth-wires">
            <defs>
              <linearGradient id="auth-wire-grad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stop-color="#2dd4bf" />
                <stop offset="55%" stop-color="#0ea5e9" />
                <stop offset="100%" stop-color="#3b82f6" />
              </linearGradient>
            </defs>
            <g v-for="(wire, i) in wires" :key="i">
              <path :d="wire.d" />
              <circle :cx="wire.x1" :cy="wire.y1" r="2.6" />
              <circle :cx="wire.x2" :cy="wire.y2" r="2.6" />
            </g>
          </svg>

          <div class="auth-minimap">
            <span class="auth-mini auth-mini--input"></span>
            <span class="auth-mini auth-mini--analysis"></span>
            <span class="auth-mini auth-mini--gap"></span>
            <span class="auth-mini auth-mini--tasks"></span>
            <span class="auth-mini auth-mini--growth"></span>
            <i class="auth-mini-view"></i>
          </div>

          <div class="auth-note">
            <b>建议</b>
            <p>先补产品方法论，再做一份竞品分析。</p>
            <span>依据 2 条待办 · 3 处差距</span>
          </div>

          <article class="auth-node auth-node--input" data-node="input">
            <h3>目标岗位</h3>
            <p>产品经理 · 互联网 · 应届</p>
            <div class="auth-chips"><span>已锁定</span><span>2026 秋招</span></div>
          </article>

          <article class="auth-node auth-node--analysis" data-node="analysis">
            <h3>岗位画像</h3>
            <p>把 JD 拆成能力要求与证据线索。</p>
            <div class="auth-chips"><span>能力要求</span><span>证据来源</span><span>匹配 72%</span></div>
          </article>

          <article class="auth-node auth-node--gap auth-node--sel" data-node="gap">
            <span class="auth-h auth-h--tl"></span>
            <span class="auth-h auth-h--tr"></span>
            <span class="auth-h auth-h--bl"></span>
            <span class="auth-h auth-h--br"></span>
            <h3>能力差距</h3>
            <div class="auth-bars">
              <div class="auth-bar" style="--w: 62%; --c: linear-gradient(90deg, #2563eb 0%, #0ea5e9 55%, #14b8a6 100%)"><span>产品方法论</span><span>62</span><i></i></div>
              <div class="auth-bar" style="--w: 45%; --c: linear-gradient(90deg, #93c5fd 0%, #67e8f9 100%)"><span>数据分析</span><span>45</span><i></i></div>
            </div>
          </article>

          <article class="auth-node auth-node--tasks" data-node="tasks">
            <h3>学习任务</h3>
            <div class="auth-task"><i class="auth-check auth-check--on"></i>需求文档拆解</div>
            <div class="auth-task"><i class="auth-check"></i>竞品分析 1 份</div>
          </article>

          <article class="auth-node auth-node--growth" data-node="growth">
            <h3>成长记录</h3>
            <p>Lv.3 · 本周完成 4 项</p>
            <div class="auth-spark">
              <i style="height: 35%"></i>
              <i style="height: 50%"></i>
              <i style="height: 42%"></i>
              <i style="height: 72%"></i>
              <i style="height: 100%"></i>
            </div>
          </article>
        </div>
      </section>

      <aside class="auth-panel">
        <div class="auth-panel-body">
          <div class="auth-panel-inner">
            <h2>{{ title }}</h2>

            <slot />

            <footer v-if="$slots.foot" class="auth-foot">
              <slot name="foot" />
            </footer>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>
