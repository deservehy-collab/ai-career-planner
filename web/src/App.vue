<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppSidebar from './components/AppSidebar.vue'
import HealthBanner from './components/HealthBanner.vue'
import { useAuthStore } from './stores/auth'
import { useHealthStore } from './stores/health'
import { usePlansStore } from './stores/plans'

// 应用启动先体检一次：后续所有页面的可用性判断都基于这份结果。
// 但业务接口现在都要求登录 —— 未登录（登录/注册页）不做体检，
// 避免把"未登录"误报成"后端挂了"。
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const health = useHealthStore()
const plans = usePlansStore()

// 登录/注册页不套导航与体检横幅，整屏只放认证卡片
const bare = computed(() => route.meta.public === true)

function checkHealth() {
  if (auth.isLoggedIn && !health.data && !health.loading) health.refresh()
}

async function syncPlans() {
  if (!auth.isLoggedIn) return
  if (!plans.items.length) await plans.load()
  const requested = typeof route.query.plan === 'string' ? route.query.plan : ''
  if (!requested) return
  if (plans.items.some((item) => item.id === requested)) {
    if (plans.activeId !== requested) plans.select(requested)
  } else {
    // URL 指定了不存在/已删除/他人的档案：维持"无档案"，不偷偷换成另一份。
    if (plans.activeId) plans.select(null)
  }
}

onMounted(() => {
  checkHealth()
  syncPlans()
})
watch(() => auth.isLoggedIn, () => {
  checkHealth()
  syncPlans()
})
watch(() => auth.user?.id, (next, previous) => {
  if (previous !== undefined && next !== previous) {
    plans.reset()
    syncPlans()
  }
})
watch(() => route.query.plan, syncPlans)
</script>

<template>
  <router-view v-if="bare" />
  <AppSidebar v-else>
    <HealthBanner />
    <!-- 这里**不要**用 <Transition mode="out-in"> 做切页：Vue 的离场阶段靠双
         requestAnimationFrame 推进，页面不可见（后台标签页 / 内嵌面板）时 rAF 不跑，
         旧页面会一直挂着不换。切页动效全部写在 main.css 里由元素新建时自然触发。 -->
    <router-view />
  </AppSidebar>
</template>
