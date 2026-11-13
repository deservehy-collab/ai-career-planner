<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'
import { readJson, writeJson } from '../utils/storage'

// 左侧固定侧边栏（桌面可收起，窄屏为抽屉 + 顶部移动条）。
// 状态点/退出逻辑与原顶栏一致，直接复用 health / auth store。
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const health = useHealthStore()

const COLLAPSE_KEY = 'career-agent:sidebar-collapsed'
const collapsed = ref(readJson(COLLAPSE_KEY, false))
const drawerOpen = ref(false)

function toggleCollapsed() {
  collapsed.value = !collapsed.value
  writeJson(COLLAPSE_KEY, collapsed.value)
}

// 换页后抽屉要收回（移动端点菜单跳转常见预期）
watch(() => route.fullPath, () => { drawerOpen.value = false })

const menu = [
  { to: '/overview', label: '概览', icon: 'M4 11l8-7 8 7M6 9.5V20h12V9.5M10 20v-5h4v5' },
  { to: '/assistant', label: 'AI 助手', icon: 'M4 5h16v10H9l-5 4zM8 9h8M8 12h5' },
  { to: '/home', label: '我的档案', icon: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM5 20c0-3.3 3.1-6 7-6s7 2.7 7 6' },
  { to: '/analyze', label: '岗位分析', icon: 'M10 3a7 7 0 1 0 7 7 7 7 0 0 0-7-7zm0 3.5a3.5 3.5 0 1 0 3.5 3.5M17 17l4 4' },
  { to: '/tasks', label: '学习与项目', icon: 'M4 6h3l1.5 1.5L12 4M4 12h3l1.5 1.5L12 10M4 18h3l1.5 1.5L12 16M15 6h5M15 12h5M15 18h5' },
  { to: '/growth', label: '成长记录', icon: 'M4 19h16M6 15l4-4 3 3 5-6M18 8h3v3' },
  { to: '/resume', label: '简历制作', icon: 'M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6' },
  { to: '/interview', label: '面试陪练', icon: 'M5 4h14a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-8.5L6 20v-4H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM10.4 9.4a1.8 1.8 0 0 1 3.4.6c0 1.2-1.8 1.3-1.8 2.6M12 15.2h.01' },
  { to: '/status', label: '系统状态', icon: 'M12 3a9 9 0 1 0 9 9 9 9 0 0 0-9-9zm0 4.5V12l3 2' },
]

/** 父级菜单在子页也要亮着：/tasks/learning 仍属于「学习与项目」。 */
function isSectionActive(to) {
  return route.path === to || route.path.startsWith(`${to}/`)
}

const dotClass = computed(() => {
  if (health.unreachable) return 'dot dot-danger'
  if (health.status === 'ok') return 'dot dot-ok'
  return 'dot dot-warn'
})

const statusText = computed(() => {
  if (health.loading && !health.data) return '检查中…'
  if (health.unreachable) return '服务未连接'
  return health.status === 'ok' ? '系统就绪' : '部分功能受限'
})

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-shell">
    <aside
      id="app-sidebar"
      class="app-sidebar"
      :class="{ 'is-collapsed': collapsed, 'is-open': drawerOpen }"
    >
      <div class="sidebar-brand">
        <span class="sidebar-logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <circle cx="5" cy="12" r="2.2" />
            <circle cx="19" cy="6" r="2.2" />
            <circle cx="19" cy="18" r="2.2" />
            <path d="M7.2 11l9.6-4M7.2 13l9.6 4" />
          </svg>
        </span>
        <div class="brand">AI 职业<span>规划平台</span></div>
      </div>

      <nav class="sidebar-nav" aria-label="主导航">
        <RouterLink
          v-for="item in menu"
          :key="item.to"
          :to="item.to"
          class="sidebar-link"
          :class="{ 'is-section-active': isSectionActive(item.to) }"
          :title="item.label"
          :aria-label="item.label"
          :aria-current="isSectionActive(item.to) ? 'page' : undefined"
        >
          <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="item.icon" />
          </svg>
          <span class="sidebar-label">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <div class="sidebar-status" :title="statusText">
          <span :class="dotClass" />
          <span class="sidebar-status-text">{{ statusText }}</span>
        </div>
        <div v-if="auth.isLoggedIn" class="sidebar-user auth-user">
          <span class="auth-username">
            {{ auth.user?.username || '已登录' }}
          </span>
          <button
            class="btn btn-logout"
            type="button"
            title="退出登录"
            aria-label="退出登录"
            @click="onLogout"
          >
            <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M9 4H5v16h4M15 8l4 4-4 4M19 12H9" />
            </svg>
            <span class="sidebar-label">退出</span>
          </button>
        </div>
        <button
          class="sidebar-collapse"
          type="button"
          :aria-label="collapsed ? '展开菜单' : '收起菜单'"
          :aria-pressed="collapsed"
          @click="toggleCollapsed"
        >
          <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="collapsed ? 'M4 6h16M4 12h10M4 18h16M20 8l-3 4 3 4' : 'M4 6h16M4 12h10M4 18h16M17 8l3 4-3 4'" />
          </svg>
          <span class="sidebar-collapse-text">{{ collapsed ? '展开菜单' : '收起菜单' }}</span>
        </button>
      </div>
    </aside>

    <div
      v-if="drawerOpen"
      class="drawer-overlay"
      aria-hidden="true"
      @click="drawerOpen = false"
    />

    <main class="app-main">
      <div class="mobile-bar">
        <button
          class="mobile-menu-btn"
          type="button"
          aria-controls="app-sidebar"
          :aria-expanded="drawerOpen"
          :aria-label="drawerOpen ? '关闭菜单' : '打开菜单'"
          @click="drawerOpen = !drawerOpen"
        >
          <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <div class="brand">AI 职业<span>规划平台</span></div>
        <span class="topbar-status" :title="statusText">
          <span :class="dotClass" />
          <span class="faint">{{ statusText }}</span>
        </span>
      </div>
      <div class="app-main-inner" :class="{ 'is-wide': route.meta.wide }">
        <slot />
      </div>
    </main>
  </div>
</template>
