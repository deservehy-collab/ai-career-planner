<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import AppBadge from '../components/AppBadge.vue'

import { INPUT_MAX_CHARS, useChatSession } from '../composables/useChatSession'
import { useHealthStore } from '../stores/health'
import { renderMarkdown } from '../utils/markdown'

/**
 * AI 助手：随时问求职与职业发展的对话页。
 *
 * 三条实现口径（都能对回代码位置）：
 *   1. **真流式**：fetch + ReadableStream 逐块读 SSE（api/chatStream.js），
 *      后端 backend/chat.py 直连本地 Ollama 的 /api/chat 增量推送 ——
 *      没有任何"定时器逐字打印完整答案"的假流式。
 *   2. **能真取消**：「停止生成」调 AbortController.abort()，后端生成器随之关闭
 *      上游连接、模型停止推理，所以这个按钮才有资格显示出来。
 *   3. **不碰业务状态**：聊天与七节点规划工作流隔离，不写画像、不写 runs/。
 *
 * 顶部的模型状态来自 `GET /api/health` 的真实探测结果，并按当前通道
 * （本地 Ollama / 云端 API）分别判断，不是装饰。
 *
 * 注意：composable 返回的是 ref，这里**必须解构**成本页的顶层绑定 ——
 * 模板只会自动解构顶层 ref，`chat.messages` 这种嵌套写法拿到的是 Ref 本身，
 * v-for / v-if 会把它当对象遍历（本项目的 pinia store 不会有这个问题）。
 */
const health = useHealthStore()
const {
  messages, sending, inputError, requestError, hasConversation, send, stop, retry, clear,
} = useChatSession()

const input = ref('')
const listRef = ref(null)
/** 用户是否贴在底部：他在读历史消息时不强行把他拽回底部。 */
const pinnedToBottom = ref(true)
const confirmClear = ref(false)

const quickQuestions = [
  '我想进入 AI 应用开发，需要学习哪些技能？',
  '如何准备 AI 应用开发岗位的面试？',
  '帮我分析 LangChain、LangGraph 和 RAG 的学习顺序。',
  '如何准备一个有竞争力的 AI 项目？',
]

const channelInfo = computed(() => health.data?.llm_channel || null)
const isCloud = computed(() => channelInfo.value?.channel === 'cloud')
const cloudKeyMissing = computed(() => isCloud.value && !channelInfo.value?.cloud_configured)

// 状态徽章只说"能不能聊"，模型名与端口属于排查读数，留在「系统状态」页。
const modelState = computed(() => {
  const data = health.data
  if (!data) {
    if (health.unreachable) return { tone: 'danger', text: '连不上服务，无法对话' }
    return { tone: 'warn', text: health.loading ? '正在检查…' : '尚未检查' }
  }
  const info = data.llm_channel
  if (info?.channel === 'cloud') {
    if (!info.cloud_configured) return { tone: 'danger', text: '云端 AI 还没配置好' }
    if (info.cloud_auth_failed) return { tone: 'danger', text: '云端 AI 登录失败' }
    return { tone: 'ok', text: '云端 AI 就绪' }
  }
  const ollama = data.ollama
  if (!ollama?.ok) return { tone: 'danger', text: '本机 AI 没启动' }
  if (!ollama.chat_model_ready) return { tone: 'danger', text: '本机 AI 还没装好' }
  return { tone: 'ok', text: '本机 AI 就绪' }
})

const canSend = computed(() => !sending.value && input.value.trim().length > 0)

function scrollToBottom() {
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function onListScroll() {
  const el = listRef.value
  if (!el) return
  pinnedToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

// 新消息与每个增量都遵循同一条规则：贴着底部就跟到底部，正在看历史就不动
watch(
  () => [messages.value.length, messages.value.at(-1)?.text.length ?? 0],
  async () => {
    await nextTick()
    if (pinnedToBottom.value) scrollToBottom()
  },
)

onMounted(async () => {
  if (!health.data && !health.loading) await health.refresh()
  // 空态要停在顶部：窄屏上一滚到底会把主张和起点卡顶出视野。
  if (messages.value.length) scrollToBottom()
})

async function handleSubmit() {
  if (!canSend.value) return
  const question = input.value.trim()
  input.value = ''
  pinnedToBottom.value = true
  await send(question)
}

async function handleQuick(question) {
  if (sending.value) return
  pinnedToBottom.value = true
  await send(question)
}

function handleClear() {
  clear()
  confirmClear.value = false
  input.value = ''
}
</script>

<template>
  <div class="chat-page">
    <div class="page-head chat-head">
      <div>
        <p class="chat-eyebrow">AI 职业助手</p>
        <h1>把问题直接问出来</h1>
        <p class="chat-sub">学习路线、项目选择、简历措辞、面试准备，都可以从一句话开始。</p>
      </div>
      <div class="chat-head-side">
        <AppBadge :text="modelState.text" :tone="modelState.tone" />
        <button
          class="btn btn-ghost btn-sm"
          type="button"
          :disabled="health.loading"
          @click="health.refresh()"
        >
          {{ health.loading ? '检测中…' : '重新检测' }}
        </button>
      </div>
    </div>

    <p v-if="health.unreachable" class="notice notice-danger">
      连不上服务，消息发不出去。请确认服务已经启动，
      或到<router-link to="/status">系统状态</router-link>页看看具体原因。
    </p>
    <p v-else-if="cloudKeyMissing" class="notice notice-danger">
      当前用的是云端 AI，但没读到云端密钥。请到
      <router-link to="/status">系统状态</router-link>页检查配置，或改用本机 AI。
    </p>
    <p v-else-if="requestError" class="notice notice-danger">{{ requestError }}</p>

    <section class="chat-shell card" :class="{ 'is-empty': !hasConversation }">
      <div ref="listRef" class="chat-scroll" @scroll="onListScroll">
        <!-- 初始状态：欢迎 + 快捷提问（不放假历史、不放假统计） -->
        <div v-if="!hasConversation" class="chat-empty">
          <div class="chat-hero">
            <span class="chat-hero-mark" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M11.4 3.6 13.2 8.6 18.2 10.4 13.2 12.2 11.4 17.2 9.6 12.2 4.6 10.4 9.6 8.6Z" />
                <path d="M18.3 15.6 19.1 17.7 21.2 18.5 19.1 19.3 18.3 21.4 17.5 19.3 15.4 18.5 17.5 17.7Z" />
              </svg>
            </span>
            <h2>先说清楚你要去哪儿</h2>
            <p>描述目标岗位或眼下的卡点，我会顺着你的情况往下聊。回答只用于这次对话，不会改动你的档案和成长记录。</p>
          </div>

          <div class="quick-grid">
            <button
              v-for="(question, i) in quickQuestions"
              :key="question"
              class="quick-item"
              type="button"
              :disabled="sending"
              @click="handleQuick(question)"
            >
              <span class="quick-no">{{ String(i + 1).padStart(2, '0') }}</span>
              <span class="quick-text">{{ question }}</span>
              <svg
                class="quick-go"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                stroke-width="1.6"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <path d="M3 8h8.5M8 4.6 11.6 8 8 11.4" />
              </svg>
            </button>
          </div>

          <p class="chat-empty-foot">Enter 发送 · Shift + Enter 换行</p>
        </div>

        <div v-else class="chat-list">
          <article
            v-for="message in messages"
            :key="message.id"
            class="msg"
            :class="[`msg-${message.role}`, `is-${message.state}`]"
          >
            <header class="msg-meta">
              <span class="msg-dot" aria-hidden="true" />
              <span class="msg-who">{{ message.role === 'user' ? '你' : 'AI 助手' }}</span>
              <span v-if="message.state === 'streaming'" class="msg-live">正在生成…</span>
              <AppBadge v-else-if="message.state === 'aborted'" text="已停止" tone="warn" />
              <AppBadge
                v-else-if="message.state === 'error' && message.role === 'assistant'"
                text="未完成"
                tone="danger"
              />
            </header>

            <div v-if="message.role === 'user'" class="msg-body msg-plain">{{ message.text }}</div>
            <!-- 安全：utils/markdown.js 先整体转义、只插入白名单标签，模型输出不可能带 HTML -->
            <div
              v-else-if="message.text"
              class="msg-body msg-markdown"
              v-html="renderMarkdown(message.text)"
            />

            <p v-if="message.state === 'streaming' && !message.text" class="msg-waiting">
              AI 正在准备，第一个字出来后会逐字显示…
            </p>
            <span v-if="message.state === 'streaming'" class="msg-caret" aria-hidden="true" />

            <p v-if="message.error" class="msg-error">
              <span>{{ message.error }}</span>
              <button
                v-if="message.role === 'assistant'"
                class="btn btn-sm"
                type="button"
                :disabled="sending"
                @click="retry(message.id)"
              >
                重试这一个问题
              </button>
            </p>
          </article>
        </div>
      </div>

      <footer class="chat-composer">
        <textarea
          v-model="input"
          class="chat-input"
          rows="3"
          :maxlength="INPUT_MAX_CHARS"
          placeholder="例如：投 AI 应用开发岗，简历里该写哪些项目？（Enter 发送，Shift+Enter 换行）"
          @keydown.enter.exact.prevent="handleSubmit"
        />
        <div class="composer-row">
          <p v-if="inputError" class="composer-error">{{ inputError }}</p>
          <p v-else class="chat-count">
            {{ input.length }} / {{ INPUT_MAX_CHARS }} 字
          </p>

          <div class="composer-actions">
            <template v-if="confirmClear">
              <button class="btn btn-sm btn-danger" type="button" @click="handleClear">确认清空</button>
              <button class="btn btn-ghost btn-sm" type="button" @click="confirmClear = false">取消</button>
            </template>
            <button
              v-else-if="hasConversation"
              class="btn btn-ghost btn-sm"
              type="button"
              @click="confirmClear = true"
            >
              清空会话
            </button>

            <button v-if="sending" class="btn btn-danger" type="button" @click="stop()">
              停止生成
            </button>
            <button v-else class="btn btn-primary" type="button" :disabled="!canSend" @click="handleSubmit">
              发送
            </button>
          </div>
        </div>
      </footer>
    </section>
  </div>
</template>
