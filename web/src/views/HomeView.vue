<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppBadge from '../components/AppBadge.vue'
import PageShell from '../components/PageShell.vue'

import { usePlansStore } from '../stores/plans'
import { useProfileStore } from '../stores/profile'
import { levelTone } from '../utils/enums'
import { formatDateTime, joinList } from '../utils/format'

/**
 * 我的档案：左边是这台设备上的基础资料与最近能力表，中间是账号下的求职档案列表。
 *
 * 两类数据必须分开读：
 *   基础资料 —— profile store（本机保存，新建档案时拷一份快照进档案）
 *   求职档案 —— plans store（服务端六表；每份档案的分析、任务、成长互不串用）
 *
 * 删除是**整份档案连同它的分析轮次、任务、成长记录一起删**，不是隐藏；
 * 账号、基础资料和其他档案不受影响，所以确认面板必须先把这句话摆出来。
 */
const profile = useProfileStore()
const plans = usePlansStore()
const router = useRouter()
const route = useRoute()

/** 能力表只显示**当前档案**的，不拿本地缓存的"最近一次分析"冒充。 */
const displaySkills = computed(() => plans.active?.current_skills || [])

const skillsAt = computed(
  () => plans.active?.latest_round?.finished_at || plans.active?.updated_at || null,
)

const filledFields = computed(() => {
  let count = 0
  if (profile.major.trim()) count += 1
  if (profile.grade.trim()) count += 1
  if (profile.targetJob.trim()) count += 1
  if (profile.skills.length) count += 1
  if (profile.projects.length) count += 1
  return count
})

const completeness = computed(() => Math.round((filledFields.value / 5) * 100))
const isProfileEmpty = computed(() => filledFields.value === 0)

// ── 求职档案列表 ───────────────────────────────────────────────────────
const archiveError = ref('')
const archiveNotice = ref('')
/** 正在等待确认删除的档案 id；同一时刻只允许一份，避免误点旁边的行。 */
const confirmingId = ref(null)
const deletingId = ref(null)

function countLabel(item) {
  const counts = item?.counts || {}
  return `${counts.rounds || 0} 轮 · ${counts.tasks || 0} 项任务 · ${counts.growth || 0} 条成长`
}

function isActive(item) {
  return item.id === plans.activeId
}

function openPlan(item) {
  plans.select(item.id)
  archiveNotice.value = ''
  archiveError.value = ''
  router?.push({ path: '/tasks', query: { plan: item.id } })
}

function askDelete(item) {
  if (deletingId.value) return
  confirmingId.value = item.id
  archiveError.value = ''
  archiveNotice.value = ''
}

function cancelDelete() {
  if (deletingId.value) return
  confirmingId.value = null
}

async function confirmDelete(item) {
  if (deletingId.value) return
  deletingId.value = item.id
  archiveError.value = ''
  try {
    const result = await plans.remove(item.id)
    archiveNotice.value = result.wasActive
      ? (result.nextId
          ? `已删除档案「${item.title}」。已切换到剩下的档案。`
          : `已删除档案「${item.title}」。现在没有档案了，可以点「新建档案」重新开始。`)
      : `已删除档案「${item.title}」。当前所在档案没有变化。`
    // URL 还指着刚删掉的档案时一并换掉，刷新后不会落到不存在的 id 上。
    const query = { ...(route?.query || {}) }
    if (query.plan === item.id) {
      if (result.nextId) query.plan = result.nextId
      else delete query.plan
      router?.replace({ query })
    }
    confirmingId.value = null
  } catch (error) {
    archiveError.value = error?.message || '删除档案失败，数据没有改动'
  } finally {
    deletingId.value = null
  }
}

async function refreshPlans() {
  archiveError.value = ''
  try {
    await plans.load()
  } catch (error) {
    archiveError.value = error?.message || '档案列表没有加载出来'
  }
}
</script>

<template>
  <PageShell title="我的档案" variant="flat">
    <!-- 档案头：目标岗位是整页的主语，完成度与主行动跟在下面 -->
    <section class="arc-head" aria-labelledby="arc-head-title">
      <div class="arc-head-main">
        <p class="arc-eyebrow">当前目标</p>
        <h2 id="arc-head-title" class="arc-target">{{ profile.targetJob || '还没有目标岗位' }}</h2>
        <p class="arc-line">
          {{ profile.major || '专业未填' }} · {{ profile.grade || '年级未填' }}
        </p>
        <div class="arc-meter">
          <span class="arc-meter-track" aria-hidden="true">
            <span class="arc-meter-fill" :style="{ width: `${completeness}%` }" />
          </span>
          <span class="arc-meter-text">画像填了 {{ filledFields }} / 5 项</span>
        </div>
        <div class="btn-row">
          <router-link class="btn btn-primary" to="/analyze">
            {{ isProfileEmpty ? '去填写画像' : '编辑画像并分析' }}
          </router-link>
          <router-link class="btn btn-ghost" to="/tasks">看学习与项目</router-link>
        </div>
      </div>

      <div class="arc-head-side">
        <p class="arc-side-no">
          {{ displaySkills.length }}<span>项能力</span>
        </p>
        <p class="arc-side-when">
          {{ skillsAt ? formatDateTime(skillsAt) : (plans.active ? '这份档案还没有分析结果' : '还没有选择档案') }}
        </p>
      </div>
    </section>

    <!-- 求职档案：每份档案自带分析、任务与成长，切换和删除都在这里 -->
    <section class="arc-plans" aria-labelledby="arc-plans-title">
      <header class="arc-panel-head">
        <h2 id="arc-plans-title" class="arc-panel-title">求职档案</h2>
        <div class="btn-row">
          <AppBadge
            :text="plans.loading ? '加载中…' : `${plans.total} 份`"
            :tone="plans.total ? 'ok' : 'muted'"
          />
          <button class="btn btn-sm btn-ghost" type="button" :disabled="plans.loading" @click="refreshPlans">
            刷新
          </button>
          <router-link class="btn btn-sm btn-primary" to="/analyze">＋ 新建档案</router-link>
        </div>
      </header>

      <div class="arc-panel-body">
        <div v-if="archiveError || plans.error" class="notice notice-danger">
          {{ archiveError || plans.error }}
        </div>
        <div v-if="archiveNotice" class="notice notice-ok">{{ archiveNotice }}</div>

        <div v-if="plans.loading && !plans.items.length" class="arc-empty">
          <p class="arc-empty__title">正在加载求职档案…</p>
        </div>

        <div v-else-if="!plans.items.length" class="arc-empty">
          <p class="arc-empty__title">还没有求职档案</p>
          <router-link class="btn btn-sm btn-primary" to="/analyze">去开始第一份档案</router-link>
        </div>

        <ul v-else class="arc-plan-list">
          <li
            v-for="item in plans.items"
            :key="item.id"
            class="arc-plan"
            :class="{ 'is-active': isActive(item) }"
          >
            <div class="arc-plan-top">
              <p class="arc-plan-title">
                <strong>{{ item.title || item.target_job }}</strong>
                <AppBadge v-if="isActive(item)" text="当前档案" tone="primary" />
                <AppBadge v-if="item.is_legacy" text="旧数据只读" tone="warn" />
              </p>
            </div>

            <p class="arc-plan-meta">
              <template v-if="item.target_job && item.target_job !== item.title">
                岗位：{{ item.target_job }} ·
              </template>
              {{ countLabel(item) }}
            </p>

            <div class="arc-plan-foot">
              <span class="arc-plan-when">
                最近更新 {{ item.updated_at ? formatDateTime(item.updated_at) : '未知' }}
              </span>
              <div class="arc-plan-actions">
                <button
                  v-if="!isActive(item)"
                  class="btn btn-sm"
                  type="button"
                  :disabled="!!deletingId"
                  @click="openPlan(item)"
                >
                  切换到此档案
                </button>
                <router-link
                  v-else
                  class="btn btn-sm btn-ghost"
                  :to="{ path: '/tasks', query: { plan: item.id } }"
                >
                  继续
                </router-link>
                <button
                  class="btn btn-sm btn-ghost arc-plan-delete"
                  type="button"
                  :disabled="!!deletingId || confirmingId === item.id"
                  @click="askDelete(item)"
                >
                  删除档案
                </button>
              </div>
            </div>

            <div v-if="confirmingId === item.id" class="arc-plan-confirm">
              <strong>删除「{{ item.title || item.target_job }}」？</strong>
              <p>
                会连同这份档案的 {{ item.counts?.rounds || 0 }} 轮分析、
                {{ item.counts?.tasks || 0 }} 项任务、{{ item.counts?.growth || 0 }} 条成长记录一起删除，无法撤销。
                账号、基础资料和其他档案不受影响；正在跑的分析结果会被丢弃。
              </p>
              <div class="btn-row">
                <button class="btn btn-sm" type="button" :disabled="!!deletingId" @click="cancelDelete">
                  取消
                </button>
                <button
                  class="btn btn-sm btn-danger"
                  type="button"
                  :disabled="!!deletingId"
                  @click="confirmDelete(item)"
                >
                  <span v-if="deletingId === item.id" class="spinner" />
                  {{ deletingId === item.id ? '删除中…' : '确认删除整份档案' }}
                </button>
              </div>
            </div>
          </li>
        </ul>
      </div>
    </section>

    <div class="arc-columns">
      <section class="arc-panel" aria-labelledby="arc-profile-title">
        <header class="arc-panel-head">
          <h2 id="arc-profile-title" class="arc-panel-title">我的画像</h2>
          <AppBadge
            :text="isProfileEmpty ? '尚未填写' : '已保存'"
            :tone="isProfileEmpty ? 'warn' : 'ok'"
          />
        </header>

        <div class="arc-panel-body">
          <div v-if="isProfileEmpty" class="arc-empty">
            <p class="arc-empty__title">还没有填写画像</p>
            <router-link class="btn btn-sm" to="/analyze">去填写</router-link>
          </div>
          <template v-else>
            <dl class="arc-fields">
              <div class="arc-field">
                <dt>专业</dt>
                <dd>{{ profile.major || '—' }}</dd>
              </div>
              <div class="arc-field">
                <dt>年级</dt>
                <dd>{{ profile.grade || '—' }}</dd>
              </div>
              <div class="arc-field">
                <dt>目标岗位</dt>
                <dd><strong>{{ profile.targetJob || '—' }}</strong></dd>
              </div>
              <div class="arc-field">
                <dt>自述技能</dt>
                <dd>{{ joinList(profile.skills, '未填写') }}</dd>
              </div>
              <div class="arc-field">
                <dt>项目经历</dt>
                <dd>{{ joinList(profile.projects, '未填写') }}</dd>
              </div>
            </dl>          </template>
        </div>
      </section>

      <section class="arc-panel" aria-labelledby="arc-skills-title">
        <header class="arc-panel-head">
          <h2 id="arc-skills-title" class="arc-panel-title">当前档案的能力表</h2>
          <span v-if="skillsAt" class="arc-panel-when">
            {{ formatDateTime(skillsAt) }}
          </span>
        </header>

        <div class="arc-panel-body">
          <div v-if="!displaySkills.length" class="arc-empty">
            <p class="arc-empty__title">
              {{ plans.active ? '这份档案还没有能力表' : '还没有选择档案' }}
            </p>
            <router-link class="btn btn-sm" to="/analyze">
              {{ plans.active ? '去分析' : '去新建或选择档案' }}
            </router-link>
          </div>
          <div v-else class="table-wrap">
            <table class="data data--skills">
              <thead>
                <tr>
                  <th>技能</th>
                  <th>等级</th>
                  <th>判断依据</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in displaySkills" :key="item.skill">
                  <td><strong>{{ item.skill }}</strong></td>
                  <td><AppBadge :text="item.level" :tone="levelTone(item.level)" /></td>
                  <td class="muted">{{ item.evidence || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  </PageShell>
</template>
