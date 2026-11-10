<script setup>
import { computed } from 'vue'

import { useHealthStore } from '../stores/health'

// 全局告警条：只在"真的有问题"时出现，正常时完全隐藏，不占版面。
//
// 文案只说"哪件事做不了、该怎么办"，不报端口、模型名、文件路径这些读数 ——
// 那些是给排查用的，全在「系统状态」页里，这里只留一个入口。
const health = useHealthStore()

const issues = computed(() => {
  const data = health.data
  if (!data) return []
  const list = []
  if (!data.ollama?.ok) {
    list.push('本机 AI 没启动：分析和聊天都用不了，先把它启动起来。')
  } else if (!data.ollama.chat_model_ready) {
    list.push('本机 AI 还没装好：电脑上缺了聊天要用的模型。')
  }
  if (data.knowledge?.skills && !data.knowledge.skills.ok) {
    list.push('技能资料读不到：学习路线会保守处理，不会替你编能力标准。')
  }
  if (data.knowledge?.projects && !data.knowledge.projects.ok) {
    list.push('项目资料读不到：项目推荐会退成通用建议。')
  }
  if (data.stores?.growth && !data.stores.growth.writable) {
    list.push('成长记录暂时存不了：分析照样跑，但这次升级不会被保存。')
  }
  if (data.stores?.loop && !data.stores.loop.writable) {
    list.push('任务清单暂时存不了：提交完成会失败。')
  }
  return list
})

const visible = computed(() => health.unreachable || issues.value.length > 0)
</script>

<template>
  <div v-if="visible" class="notice notice-warn">
    <strong v-if="health.unreachable">连不上服务</strong>
    <strong v-else>有几件事现在做不了</strong>
    <div v-if="health.unreachable" class="notice-body">
      {{ health.errorMessage }}
    </div>
    <ul v-else>
      <li v-for="issue in issues" :key="issue">{{ issue }}</li>
    </ul>
    <div class="btn-row">
      <button class="btn" :disabled="health.loading" @click="health.refresh()">
        {{ health.loading ? '检查中…' : '重新检查' }}
      </button>
      <router-link class="btn btn-ghost" to="/status">查看系统状态</router-link>
    </div>
  </div>
</template>
