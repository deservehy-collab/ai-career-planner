<script setup>
import { computed, onMounted, ref } from 'vue'

import AppBadge from '../components/AppBadge.vue'
import SectionNav from '../components/SectionNav.vue'
import { setLlmChannel } from '../api/endpoints'
import { useHealthStore } from '../stores/health'
import { AGENTS } from '../utils/agents'
import { formatDateTime } from '../utils/format'
import { storeStatusText, storeStatusTone } from '../utils/enums'

/**
 * 系统状态页：直接展示 `GET /api/health` 的真实返回。
 * 三类依赖 —— 模型通道 / Ollama / 业务知识库 / 两个 store —— 逐项给出可达性与失败原因。
 *
 * 「模型通道」不是普通设置项：分析为什么慢、会不会占本机 GPU，都由它决定，
 * 所以放在主栏第一张卡；总体状态只做摘要，详细原因留给下面的检查项。
 */
/** 子页由路由传进来：AI 与运行 / 资料与存储 / 排查配置各自有独立地址。 */
const props = defineProps({
  tab: { type: String, default: 'ai' },
})

const health = useHealthStore()

onMounted(() => {
  if (!health.data) health.refresh()
})

const data = computed(() => health.data)

const activeTab = computed(() => props.tab)
const pageTitle = computed(
  () => ({ ai: 'AI 与运行', data: '资料与存储', diag: '排查配置' }[props.tab] || 'AI 与运行'),
)
const tabItems = [
  { key: 'ai', to: '/status', label: 'AI 与运行' },
  { key: 'data', to: '/status/data', label: '资料与存储' },
  { key: 'diag', to: '/status/diag', label: '排查配置' },
]

// ── 模型通道切换 ─────────────────────────────────────────────────────────
const channelBusy = ref(false)
const channelError = ref('')
// 同一条错误只显示在触发它的控件旁：卡片里点选和右侧按钮都能切通道，
// 两处同时报错会让人以为失败了两遍。
const channelErrorAt = ref('')

const channelInfo = computed(() => data.value?.llm_channel)
const isCloud = computed(() => channelInfo.value?.channel === 'cloud')
const cloudSelectable = computed(() => !!channelInfo.value?.cloud_configured)

async function switchChannel(value, origin = 'card') {
  channelError.value = ''
  channelErrorAt.value = origin
  channelBusy.value = true
  try {
    await setLlmChannel(value)
    await health.refresh()
  } catch (error) {
    const raw = error?.message || ''
    channelError.value = raw.includes('CLOUD_API_KEY')
      ? '还没有配置云端密钥，暂时不能切到云端。'
      : raw || '切换失败，请重试'
  } finally {
    channelBusy.value = false
  }
}

const switchTarget = computed(() => (isCloud.value ? 'local' : 'cloud'))
const switchLabel = computed(() => (isCloud.value ? '切回本机' : '切到云端'))
const switchDisabled = computed(
  () => channelBusy.value || (switchTarget.value === 'cloud' && !cloudSelectable.value),
)

async function switchToOther() {
  if (switchDisabled.value) return
  await switchChannel(switchTarget.value, 'rail')
}

// ── 体检摘要 ─────────────────────────────────────────────────────────────
const modelReady = computed(() => {
  const info = channelInfo.value
  if (!info) return null
  if (info.channel === 'cloud') return !!info.cloud_configured && !info.cloud_auth_failed
  return !!data.value?.ollama?.ok && !!data.value?.ollama?.chat_model_ready
})

// 岗位资料库是后加的段落：老后端不返回时不参与判定，返回了就必须 ok。
const knowledgeReady = computed(() => {
  const k = data.value?.knowledge
  if (!k) return false
  const jobsOk = k.jobs ? !!k.jobs.ok : true
  return !!k.skills?.ok && !!k.projects?.ok && jobsOk
})

const storesReady = computed(
  () => !!data.value?.stores?.growth?.writable && !!data.value?.stores?.loop?.writable,
)

const workflowNodes = AGENTS.length

/**
 * 后端理论上总会返回全部段落（backend/services.py::health），
 * 但字段缺失时**逐段降级**，不让整页崩掉，也不去猜缺失段落的状态。
 */
const SECTION_LABELS = {
  llm_channel: 'AI 运行方式',
  ollama: '本机 AI',
  knowledge: '岗位与技能资料',
  stores: '成长与任务数据',
  config: '运行设置',
}

const missingSections = computed(() => {
  if (!data.value) return []
  return Object.entries(SECTION_LABELS)
    .filter(([key]) => !data.value[key])
    .map(([, label]) => label)
})

const heroChecks = computed(() => {
  const d = data.value
  return [
    {
      key: 'model',
      label: 'AI 运行方式',
      value: d?.llm_channel ? (isCloud.value ? '云端运行' : '本机运行') : '没检查到',
      ok: d?.llm_channel ? modelReady.value : null,
    },
    {
      key: 'ollama',
      label: '本机 AI',
      value: d?.ollama ? (d.ollama.ok ? '可用' : '不可用') : '没检查到',
      ok: d?.ollama ? !!d.ollama.ok : null,
    },
    {
      key: 'knowledge',
      label: '岗位与技能资料',
      value: d?.knowledge ? (knowledgeReady.value ? '正常' : '异常') : '没检查到',
      ok: d?.knowledge ? knowledgeReady.value : null,
    },
    {
      key: 'stores',
      label: '成长与任务数据',
      value: d?.stores ? (storesReady.value ? '正常' : '需处理') : '没检查到',
      ok: d?.stores ? storesReady.value : null,
    },
  ]
})

const heroBadge = computed(() => {
  if (!data.value) {
    if (health.loading) return { text: '检查中…', tone: 'muted' }
    if (health.unreachable) return { text: '连不上', tone: 'danger' }
    return { text: '待检查', tone: 'muted' }
  }
  if (missingSections.value.length) return { text: '结果不完整', tone: 'warn' }
  return data.value.status === 'ok'
    ? { text: '正常', tone: 'ok' }
    : { text: '部分功能受限', tone: 'warn' }
})

const heroTitle = computed(() => {
  if (!data.value) {
    if (health.loading) return '正在检查…'
    if (health.unreachable) return '连不上服务'
    return '尚未检查'
  }
  if (missingSections.value.length) return '检查结果不完整'
  return data.value.status === 'ok' ? '主要功能可用' : '部分功能暂时用不了'
})

const heroNote = computed(() => {
  if (!data.value) {
    if (health.loading) return '正在读取检查结果…'
    if (health.unreachable) return '连不上服务；确认服务已经启动、网络正常后再试。'
    return '还没有检查结果。'
  }
  if (missingSections.value.length) {
    return '这次没读到部分检查内容，下面只展示已经拿到的部分；可以稍后点「重新检查」再看。'
  }
  return data.value.status === 'ok'
    ? 'AI、岗位资料和成长记录都可用。'
    : '有功能暂时用不了，下面指出具体是哪一项。'
})

const cloudKeyText = computed(() => {
  if (!channelInfo.value) return '未知'
  return cloudSelectable.value ? '已配置' : '未配置'
})

/** 整体「降级」时，把"哪些功能会受影响"讲清楚，而不是只说一句状态不好。 */
const degradedImpacts = computed(() => {
  const d = data.value
  if (!d || d.status === 'ok') return []
  const list = []
  if (d.llm_channel?.channel === 'cloud' && !d.llm_channel.cloud_configured)
    list.push('选了云端 AI 但还没配置密钥，分析和聊天都用不了（请配置或改回本机）')
  if (d.llm_channel?.cloud_auth_failed)
    list.push('云端密钥无效，分析已经自动改用本机 AI')
  if (d.llm_channel?.channel !== 'cloud' && !d.ollama?.ok)
    list.push('岗位分析暂时用不了（本机 AI 不可用）')
  else if (d.llm_channel?.channel !== 'cloud' && !d.ollama?.chat_model_ready)
    list.push('本机缺了聊天要用的模型，分析会失败')
  if (d.knowledge?.skills && !d.knowledge.skills.ok) list.push('技能前置检查不会执行，升级判定会偏宽松')
  if (d.knowledge?.projects && !d.knowledge.projects.ok) list.push('项目推荐可能为空')
  if (d.stores?.growth && !d.stores.growth.writable) list.push('成长记录暂时存不了，新的成长无法保存')
  if (d.stores?.loop && !d.stores.loop.writable) list.push('任务清单暂时存不了，完成证据无法登记')
  return list
})
</script>

<template>
  <div>
    <nav class="pg-crumb" aria-label="页面位置">
      <RouterLink to="/status">系统状态</RouterLink>
      <span aria-hidden="true">/</span>
      <span>{{ pageTitle }}</span>
    </nav>

    <div class="page-head status-head">
      <div>
        <h1>{{ pageTitle }}</h1>
      </div>
      <div class="status-head__actions">
        <span class="faint">
          {{ health.fetchedAt ? `检查于 ${formatDateTime(health.fetchedAt)}` : '尚未检查' }}
        </span>
        <button class="btn btn-sm" :disabled="health.loading" @click="health.refresh()">
          {{ health.loading ? '检查中…' : '重新检查' }}
        </button>
      </div>
    </div>

    <SectionNav v-if="data" :items="tabItems" :active="activeTab" label="系统状态子页面" />

    <section class="card status-hero">
      <div class="card-body">
        <div class="status-hero__body">
          <div class="status-hero__lead">
            <div class="status-hero__title-row">
              <h2>{{ heroTitle }}</h2>
              <AppBadge :text="heroBadge.text" :tone="heroBadge.tone" />
            </div>
            <p class="status-hero__note">{{ heroNote }}</p>
            <div class="btn-row status-hero__actions">
              <router-link class="btn btn-sm" to="/analyze">去岗位分析</router-link>
            </div>
          </div>

          <div v-if="data" class="status-hero__checks">
            <div
              v-for="item in heroChecks"
              :key="item.key"
              class="status-mini"
              :class="item.ok === true ? 'is-ok' : item.ok === false ? 'is-bad' : 'is-muted'"
            >
              <span class="status-mini__dot" aria-hidden="true" />
              <span class="status-mini__label">{{ item.label }}</span>
              <strong class="status-mini__value">{{ item.value }}</strong>
            </div>
          </div>
        </div>

        <div v-if="health.errorMessage && !data" class="notice notice-danger status-hero__alert">
          <strong>连不上服务</strong>
          <div class="notice-body">{{ health.errorMessage }}</div>
        </div>
        <div v-else-if="missingSections.length" class="notice notice-warn status-hero__alert">
          这次没读到：<strong>{{ missingSections.join('、') }}</strong>。
          对应的检查项先空着，稍后点「重新检查」再看一次。
        </div>
        <div v-else-if="degradedImpacts.length" class="notice notice-warn status-hero__alert">
          <strong>「部分功能受限」意味着这些地方受影响：</strong>
          <ul>
            <li v-for="(item, i) in degradedImpacts" :key="i">{{ item }}</li>
          </ul>
        </div>
      </div>
    </section>

    <div v-if="data" class="status-desk" :class="{ 'status-desk--single': activeTab !== 'ai' }">
      <div class="status-main">
        <template v-if="activeTab === 'ai'">
        <!-- AI 运行方式：分析快不快、要不要连网，都由这里决定 -->
        <section v-if="data.llm_channel" class="card status-channel">
          <div class="card-head">
            <h2>AI 运行方式</h2>
            <AppBadge
              :text="isCloud ? '云端运行' : '本机运行'"
              :tone="isCloud ? 'primary' : 'muted'"
            />
          </div>

          <div class="card-body">
            <div class="status-channel__now">
              <div>
                <p class="status-channel__eyebrow">当前使用的 AI</p>
                <p class="status-channel__model">{{ isCloud ? '云端 AI' : '本机 AI' }}</p>
              </div>
              <AppBadge
                :text="modelReady === null ? '未知' : modelReady ? '可用' : '需要处理'"
                :tone="modelReady === null ? 'muted' : modelReady ? 'ok' : 'warn'"
              />
            </div>

            <div class="status-choices" role="radiogroup" aria-label="选择 AI 运行方式">
              <label class="radio status-choice" :class="{ 'is-disabled': channelBusy }">
                <input
                  type="radio"
                  name="llm-channel"
                  value="local"
                  :checked="!isCloud"
                  :disabled="channelBusy"
                  @change="switchChannel('local')"
                />
                <span class="status-choice__body">
                  <span class="status-choice__top">
                    <strong>本机运行</strong>
                    <AppBadge v-if="!isCloud" text="使用中" tone="primary" />
                    <AppBadge v-else text="免费" tone="muted" />
                  </span>
                  <span class="status-choice__note">
                    不联网、不花钱，但更慢，且占用本机显卡。
                  </span>
                </span>
              </label>

              <label
                class="radio status-choice"
                :class="{ 'is-disabled': channelBusy || !cloudSelectable }"
              >
                <input
                  type="radio"
                  name="llm-channel"
                  value="cloud"
                  :checked="isCloud"
                  :disabled="channelBusy || !cloudSelectable"
                  @change="switchChannel('cloud')"
                />
                <span class="status-choice__body">
                  <span class="status-choice__top">
                    <strong>云端运行</strong>
                    <AppBadge v-if="isCloud" text="使用中" tone="primary" />
                    <AppBadge v-else-if="cloudSelectable" text="不占显卡" tone="muted" />
                  </span>
                  <span v-if="!cloudSelectable" class="faint">还没有配置云端密钥，暂时不能选</span>
                  <span class="status-choice__note">
                    {{
                      cloudSelectable
                        ? '更快，不占本机显卡；需要联网，高峰期可能排队。'
                        : '配置好云端密钥后，才能把分析切到云端。'
                    }}
                  </span>
                </span>
              </label>
            </div>

            <div v-if="data.llm_channel.cloud_auth_failed" class="notice notice-warn">
              云端密钥验证失败过，分析已经自动切回本机。请检查配置里的云端密钥是否正确、额度是否用完。
            </div>
            <div v-if="channelError && channelErrorAt === 'card'" class="notice notice-danger">
              {{ channelError }}
            </div>
          </div>
        </section>

        <!-- 本机 AI -->
        <section v-if="data.ollama" class="card status-check">
            <div class="card-head">
              <h2>本机 AI</h2>
              <AppBadge
                :text="data.ollama.ok && data.ollama.chat_model_ready ? '可用' : '不可用'"
                :tone="data.ollama.ok && data.ollama.chat_model_ready ? 'ok' : 'danger'"
              />
            </div>
            <div class="card-body">
              <p v-if="!data.ollama.ok" class="notice notice-warn">
                {{
                  isCloud
                    ? '本机 AI 没启动；现在用的是云端，暂时不受影响。'
                    : '本机 AI 没启动：分析和聊天暂时都用不了，先把它启动起来。'
                }}
              </p>
              <p v-else-if="!data.ollama.chat_model_ready" class="notice notice-warn">
                这台电脑上缺了分析要用的模型，分析和聊天暂时用不了。
              </p>

              <details class="fold">
                <summary>模型与连接详情（排查用）</summary>
                <div class="fold-body">
                  <dl class="status-pairs">
                    <div>
                      <dt>服务地址</dt>
                      <dd class="mono">{{ data.ollama.base_url }}</dd>
                    </div>
                    <div>
                      <dt>聊天与分析用的模型</dt>
                      <dd>
                        <code class="mono">{{ data.ollama.chat_model }}</code>
                        <AppBadge
                          :text="data.ollama.chat_model_ready ? '已安装' : '没装'"
                          :tone="data.ollama.chat_model_ready ? 'ok' : 'danger'"
                        />
                      </dd>
                    </div>
                  </dl>

                  <div class="status-sub">
                    <p class="status-sub__label">这台电脑上已有的模型</p>
                    <div v-if="data.ollama.models?.length" class="tags tags--tight">
                      <span v-for="name in data.ollama.models" :key="name" class="badge mono">{{ name }}</span>
                    </div>
                    <span v-else class="faint">没有读到模型列表</span>
                  </div>

                  <div v-if="data.ollama.error" class="mono is-error">{{ data.ollama.error }}</div>
                </div>
              </details>
            </div>
          </section>
        </template>

        <template v-else-if="activeTab === 'data'">
          <!-- 岗位与技能资料 -->
          <section v-if="data.knowledge" class="card status-check">
            <div class="card-head">
              <h2>岗位与技能资料</h2>
              <AppBadge
                :text="knowledgeReady ? '正常' : '异常'"
                :tone="knowledgeReady ? 'ok' : 'danger'"
              />
            </div>
            <div class="card-body">
              <dl class="status-pairs">
                <div>
                  <dt>技能标准</dt>
                  <dd>
                    <AppBadge
                      :text="data.knowledge.skills.ok
                        ? (Number.isFinite(data.knowledge.skills.count) ? `${data.knowledge.skills.count} 项技能` : '已读到')
                        : '读不出来'"
                      :tone="data.knowledge.skills.ok ? 'ok' : 'danger'"
                    />
                  </dd>
                </div>
                <div>
                  <dt>项目库</dt>
                  <dd>
                    <AppBadge
                      :text="data.knowledge.projects.ok
                        ? (Number.isFinite(data.knowledge.projects.count) ? `${data.knowledge.projects.count} 个项目` : '已读到')
                        : '读不出来'"
                      :tone="data.knowledge.projects.ok ? 'ok' : 'danger'"
                    />
                  </dd>
                </div>
                <div v-if="data.knowledge.jobs">
                  <dt>岗位资料</dt>
                  <dd>
                    <AppBadge
                      :text="data.knowledge.jobs.ok
                        ? (Number.isFinite(data.knowledge.jobs.count) && Number.isFinite(data.knowledge.jobs.evidence_entries)
                          ? `${data.knowledge.jobs.count} 个岗位 · ${data.knowledge.jobs.evidence_entries} 条可回查资料`
                          : '已读到')
                        : '读不出来'"
                      :tone="data.knowledge.jobs.ok ? 'ok' : 'danger'"
                    />
                  </dd>
                </div>
              </dl>

              <div v-if="data.knowledge.skills.error" class="mono is-error">
                技能标准：{{ data.knowledge.skills.error }}
              </div>
              <div v-if="data.knowledge.projects.error" class="mono is-error">
                项目库：{{ data.knowledge.projects.error }}
              </div>
              <div v-if="data.knowledge.jobs && data.knowledge.jobs.error" class="mono is-error">
                岗位资料：{{ data.knowledge.jobs.error }}
              </div>
            </div>
          </section>

          <!-- 存储 -->
          <section v-if="data.stores" class="card status-stores">
          <div class="card-head">
            <h2>成长与任务数据</h2>
            <AppBadge
              :text="storesReady ? '可保存' : '需要处理'"
              :tone="storesReady ? 'ok' : 'warn'"
            />
          </div>
          <div class="card-body">
            <div class="status-store">
              <div class="status-store__row">
                <div class="status-store__id">
                  <strong>成长记录</strong>
                </div>
                <div class="status-store__badges">
                  <AppBadge
                    v-if="data.stores.growth.status"
                    :text="storeStatusText(data.stores.growth.status)"
                    :tone="storeStatusTone(data.stores.growth.status)"
                  />
                  <span class="status-pill" :class="data.stores.growth.writable ? 'is-ok' : 'is-bad'">
                    {{ data.stores.growth.writable ? '可保存' : '不能保存' }}
                  </span>
                  <span class="status-pill" :class="data.stores.growth.exists ? 'is-ok' : 'is-muted'">
                    {{ data.stores.growth.exists ? '已有数据' : '还没有数据' }}
                  </span>
                </div>
                <div v-if="data.stores.growth.warning" class="is-warn status-store__warn">
                  {{ data.stores.growth.warning }}
                </div>
              </div>

              <div class="status-store__row">
                <div class="status-store__id">
                  <strong>任务清单</strong>
                  <span v-if="Number.isFinite(data.stores.loop.rounds)" class="faint">
                    {{ data.stores.loop.rounds }} 轮
                  </span>
                </div>
                <div class="status-store__badges">
                  <AppBadge
                    v-if="data.stores.loop.status"
                    :text="storeStatusText(data.stores.loop.status)"
                    :tone="storeStatusTone(data.stores.loop.status)"
                  />
                  <span class="status-pill" :class="data.stores.loop.writable ? 'is-ok' : 'is-bad'">
                    {{ data.stores.loop.writable ? '可保存' : '不能保存' }}
                  </span>
                  <span class="status-pill" :class="data.stores.loop.exists ? 'is-ok' : 'is-muted'">
                    {{ data.stores.loop.exists ? '已有数据' : '还没有数据' }}
                  </span>
                </div>
                <div v-if="data.stores.loop.warning" class="is-warn status-store__warn">
                  {{ data.stores.loop.warning }}
                </div>
              </div>
            </div>
          </div>
        </section>
        </template>

        <section v-else-if="data.config" class="card status-config">
          <div class="card-head">
            <h2>配置详情（排查用）</h2>
          </div>
          <div class="card-body">
            <dl class="status-pairs status-pairs--tight">
              <div>
                <dt>对话模型</dt>
                <dd class="mono">{{ data.config.chat_model }}</dd>
              </div>
              <div>
                <dt>数据保存位置</dt>
                <dd class="mono status-config__path">{{ data.config.runs_dir }}</dd>
              </div>
              <div v-if="data.stores?.growth">
                <dt>成长记录保存位置</dt>
                <dd class="mono status-config__path">{{ data.stores.growth.path }}</dd>
              </div>
              <div v-if="data.stores?.loop">
                <dt>任务清单保存位置</dt>
                <dd class="mono status-config__path">{{ data.stores.loop.path }}</dd>
              </div>
              <div>
                <dt>同时处理的任务数</dt>
                <dd>{{ data.config.task_workers }}</dd>
              </div>
            </dl>
          </div>
        </section>
      </div>

      <!-- 常驻运行情况：解释"为什么慢"并给出切换入口 -->
      <aside v-if="activeTab === 'ai'" class="status-rail">
        <section class="card status-run">
          <div class="card-body">
            <p class="status-run__eyebrow">运行情况</p>
            <dl class="status-run__facts">
              <div>
                <dt>同时处理</dt>
                <dd>{{ data.config?.task_workers ?? 1 }} 个任务</dd>
              </div>
              <div>
                <dt>规划步骤</dt>
                <dd>{{ workflowNodes }} 个，按顺序执行</dd>
              </div>
              <div>
                <dt>当前 AI</dt>
                <dd>{{ channelInfo ? (isCloud ? '云端' : '本机') : '未知' }}</dd>
              </div>
              <div>
                <dt>云端密钥</dt>
                <dd>{{ cloudKeyText }}</dd>
              </div>
            </dl>

            <button
              v-if="channelInfo"
              class="btn btn-primary status-run__action"
              type="button"
              :disabled="switchDisabled"
              @click="switchToOther"
            >
              {{ channelBusy ? '切换中…' : switchLabel }}
            </button>

            <div v-if="channelError && channelErrorAt === 'rail'" class="notice notice-danger">
              {{ channelError }}
            </div>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>
