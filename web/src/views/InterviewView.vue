<script setup>
import { computed, onMounted, ref, watch } from 'vue'

import { useAuthStore } from '../stores/auth'
import { useInterviewStore } from '../stores/interview'
import { usePlansStore } from '../stores/plans'
import { normalizeResume } from '../utils/resume'
import { readJson } from '../utils/storage'

/**
 * 面试陪练。
 *
 * 只做两件事：读当前档案、读本机简历草稿，然后向 `POST /api/interview/questions`
 * 要题。题目缓存在 interview store（按账号 + 档案），切页回来不丢、也不会重发。
 * 不写档案、不写成长记录。
 */

const DRAFT_PREFIX = 'career-agent:resume'

const CATEGORY_ORDER = [
  { key: '技术', label: '技术题', id: 'tech' },
  { key: '项目', label: '项目深挖', id: 'project' },
  { key: '动机', label: '动机与匹配', id: 'motivation' },
  { key: '反问', label: '反问面试官', id: 'ask' },
]

const plans = usePlansStore()
const auth = useAuthStore()
const interview = useInterviewStore()

const opened = ref({})

const planId = computed(() => plans.activeId)
const result = computed(() => (planId.value ? interview.resultFor(planId.value) : null))
const items = computed(() => result.value?.questions || [])
const loading = computed(() => interview.isPending(planId.value))
const error = computed(() => interview.errorFor(planId.value))
const canGenerate = computed(() => !!planId.value && !loading.value)

/** 封面目录：四类固定列出，没出题时也留 0，版面不发塌。 */
const toc = computed(() =>
  CATEGORY_ORDER.map((category) => ({
    ...category,
    count: items.value.filter((item) => item.category === category.key).length,
  })),
)

const groups = computed(() => {
  const buckets = new Map(CATEGORY_ORDER.map((category) => [category.key, []]))
  let seq = 0
  for (const item of items.value) {
    const bucket = buckets.get(item.category)
    if (!bucket) continue
    seq += 1
    bucket.push({ ...item, slot: `${item.category}-${seq}`, no: String(seq).padStart(2, '0') })
  }
  return CATEGORY_ORDER
    .map((category) => ({ ...category, items: buckets.get(category.key) }))
    .filter((group) => group.items.length)
})

function draftResume(id) {
  const userId = auth.user?.id
  if (!userId || !id) return null
  const payload = readJson(`${DRAFT_PREFIX}:${userId}:${id}`)
  return payload?.resume ? normalizeResume(payload.resume) : null
}

function generate() {
  if (!planId.value) return
  interview.generate(planId.value, draftResume(planId.value))
}

function jump(id) {
  document.getElementById(`iv-${id}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

function toggle(slot) {
  opened.value = { ...opened.value, [slot]: !opened.value[slot] }
}

watch(
  () => plans.activeId,
  (id) => {
    opened.value = {}
    if (id) interview.hydrate(id)
  },
  { immediate: true },
)

onMounted(async () => {
  if (!plans.items.length) await plans.load()
  if (!plans.activeId && plans.items.length === 1) {
    plans.select(plans.items[0].id)
  }
})

</script>

<template>
  <div class="iv-page">
    <header class="iv-head">
      <h1>面试陪练</h1>
      <span v-if="items.length" class="iv-stamp">{{ items.length }} 题 · {{ groups.length }} 类</span>
    </header>

    <div class="iv-layout">
      <aside class="iv-cover">
        <div class="iv-cover-head">
          <span class="iv-cover-label">当前档案</span>
          <strong class="iv-cover-plan" :class="{ 'is-empty': !plans.activeId }">
            {{ plans.activeTitle || '未选择档案' }}
          </strong>
        </div>

        <div class="iv-cover-count" :class="{ 'is-zero': !items.length }">
          <b>{{ String(items.length).padStart(2, '0') }}</b>
          <span>道题</span>
        </div>

        <ul class="iv-toc">
          <li v-for="row in toc" :key="row.key">
            <a v-if="row.count" :href="`#iv-${row.id}`" @click.prevent="jump(row.id)">
              <span class="iv-toc-label">{{ row.label }}</span>
              <span class="iv-toc-leader" aria-hidden="true" />
              <b>{{ row.count }}</b>
            </a>
            <span v-else class="iv-toc-row is-idle">
              <span class="iv-toc-label">{{ row.label }}</span>
              <span class="iv-toc-leader" aria-hidden="true" />
              <b>0</b>
            </span>
          </li>
        </ul>

        <button
          class="btn btn-primary iv-generate"
          type="button"
          :disabled="!canGenerate"
          @click="generate"
        >
          {{ loading ? '生成中…' : items.length ? '换一批' : '生成 6 道题' }}
        </button>
      </aside>

      <section class="iv-main">
        <div v-if="!plans.activeId" class="iv-gate">
          <h2 class="iv-gate-title">还没有可用的求职档案</h2>
          <RouterLink class="btn" to="/home">去我的档案</RouterLink>
        </div>

        <div v-else-if="loading" class="iv-loading" aria-live="polite">
          <p class="iv-loading-head">
            <span class="iv-loading-dot" aria-hidden="true" />正在出题…
          </p>
          <div class="iv-skeleton-grid" aria-hidden="true">
            <div v-for="index in 6" :key="index" class="iv-skeleton">
              <span class="iv-skeleton-no">{{ String(index).padStart(2, '0') }}</span>
              <span class="iv-skeleton-body">
                <span class="iv-skeleton-bar" />
                <span class="iv-skeleton-bar is-short" />
              </span>
            </div>
          </div>
        </div>

        <div v-else-if="error" class="iv-error" role="alert">
          <p>{{ error }}</p>
          <button class="btn" type="button" @click="generate">重试</button>
        </div>

        <div v-else-if="!items.length" class="iv-empty">
          <div class="iv-empty-head">
            <h2 class="iv-empty-title">还没有题目</h2>
            <ul class="iv-chips">
              <li v-for="row in toc" :key="row.key">{{ row.label }}</li>
            </ul>
          </div>
          <div class="iv-ghosts" aria-hidden="true">
            <span v-for="index in 6" :key="index" class="iv-ghost">
              {{ String(index).padStart(2, '0') }}
            </span>
          </div>
        </div>

        <div v-else class="iv-groups">
          <section v-for="group in groups" :id="`iv-${group.id}`" :key="group.key" class="iv-group">
            <header class="iv-group-head">
              <h2 class="iv-group-title">{{ group.label }}</h2>
              <span class="iv-group-count">{{ group.items.length }}</span>
              <span class="iv-group-rule" aria-hidden="true" />
            </header>
            <div class="iv-cards">
              <article v-for="item in group.items" :key="item.slot" class="iv-card">
                <span class="iv-no" aria-hidden="true">{{ item.no }}</span>
                <div class="iv-card-body">
                  <h3 class="iv-question">{{ item.question }}</h3>

                  <button
                    v-if="item.key_points.length || item.follow_up"
                    class="iv-toggle"
                    type="button"
                    :aria-expanded="!!opened[item.slot]"
                    @click="toggle(item.slot)"
                  >
                    {{ opened[item.slot] ? '收起参考要点' : '查看参考要点' }}
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
                         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M6 9l6 6 6-6" />
                    </svg>
                  </button>

                  <div v-if="opened[item.slot]" class="iv-points">
                    <ul v-if="item.key_points.length">
                      <li v-for="(point, index) in item.key_points" :key="index">{{ point }}</li>
                    </ul>
                    <p v-if="item.follow_up" class="iv-follow">追问：{{ item.follow_up }}</p>
                  </div>
                </div>
              </article>
            </div>
          </section>
        </div>
      </section>
    </div>
  </div>
</template>
