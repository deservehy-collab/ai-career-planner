import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '../stores/auth'

// 页面与后端能力的对应关系：
//   /overview  概览（产品入口页，登录后的默认落地页；只发 GET，纯只读）
//   /assistant AI 助手（POST /api/chat/stream 流式对话，与工作流无关）
//   /home      我的档案（原有页面，画像与最近能力表；指标与产品介绍已收敛到 /overview）
//   /analyze   岗位分析（七节点工作流：岗位要求 / 技能差距 / 学习路线 / 项目推荐都在它的结果里）
//   /tasks     学习与项目（/tasks/learning 学习路线、/tasks/projects 实战项目）
//   /growth    成长记录
//   /status    系统状态（/status/data 资料与存储、/status/diag 排查配置）
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/RegisterView.vue'),
    meta: { title: '注册', public: true },
  },
  // 概览是默认页：老的 "/" 与老的 "home" 都仍在，不破坏已有链接
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: () => import('../views/OverviewView.vue'),
    meta: { title: '概览' },
  },
  {
    path: '/assistant',
    name: 'assistant',
    component: () => import('../views/AssistantView.vue'),
    meta: { title: 'AI 助手' },
  },
  {
    path: '/home',
    name: 'home',
    component: () => import('../views/HomeView.vue'),
    meta: { title: '我的档案' },
  },
  {
    path: '/analyze',
    name: 'analyze',
    component: () => import('../views/AnalyzeView.vue'),
    meta: { title: '岗位分析' },
  },
  // 学习与项目、系统状态是多级页面：每个子页有自己的地址，
  // 刷新、收藏、浏览器前进后退都落到同一页，不靠页内状态记住位置。
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/TasksView.vue'),
    props: { tab: 'tasks' },
    meta: { title: '学习与项目', sub: '任务清单' },
  },
  {
    path: '/tasks/learning',
    name: 'tasks-learning',
    component: () => import('../views/TasksView.vue'),
    props: { tab: 'learning' },
    meta: { title: '学习与项目', sub: '学习路线' },
  },
  {
    path: '/tasks/projects',
    name: 'tasks-projects',
    component: () => import('../views/TasksView.vue'),
    props: { tab: 'projects' },
    meta: { title: '学习与项目', sub: '实战项目' },
  },
  {
    path: '/growth',
    name: 'growth',
    component: () => import('../views/GrowthView.vue'),
    meta: { title: '成长记录' },
  },
  {
    path: '/status',
    name: 'status',
    component: () => import('../views/StatusView.vue'),
    props: { tab: 'ai' },
    meta: { title: '系统状态', sub: 'AI 与运行' },
  },
  {
    path: '/status/data',
    name: 'status-data',
    component: () => import('../views/StatusView.vue'),
    props: { tab: 'data' },
    meta: { title: '系统状态', sub: '资料与存储' },
  },
  {
    path: '/status/diag',
    name: 'status-diag',
    component: () => import('../views/StatusView.vue'),
    props: { tab: 'diag' },
    meta: { title: '系统状态', sub: '排查配置' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

// 一级路径段：/tasks/learning 与 /tasks 同属「学习与项目」。
function sectionOf(path) {
  return path.split('/')[1] || ''
}

const router = createRouter({
  history: createWebHistory(),
  routes,
  // 子页之间（任务清单 ↔ 学习路线、AI 与运行 ↔ 资料与存储）原地换页，
  // 不把用户拽回页头；浏览器前进后退仍回到记录位置，换到别的板块才回顶部。
  scrollBehavior: (to, from, saved) => {
    if (saved) return saved
    if (sectionOf(to.path) === sectionOf(from.path)) return false
    return { top: 0 }
  },
})

// 路由守卫：除 meta.public（登录/注册）外一律要求登录。
// 本地有 token 但本会话还没验证过有效性时，先打一次 /api/auth/me：
// token 过期/被篡改会在那里暴露（client.js 同时清掉本地登录态）。
router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已登录且已验证的用户不该再停留在登录/注册页
    if (auth.isLoggedIn && auth.verified) return { path: '/' }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  if (!auth.verified) {
    try {
      await auth.fetchMe()
    } catch {
      auth.logout()
      return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
    }
  }
  return true
})

router.afterEach((to) => {
  const { title, sub } = to.meta || {}
  const parts = [sub, title].filter(Boolean)
  document.title = parts.length ? `${parts.join(' · ')} · AI 职业规划平台` : 'AI 职业规划平台'
})

export default router
