<script setup>
import { computed } from 'vue'

import { AGENT_GROUP, getAgent } from '../utils/agents'

/**
 * Agent 归属标记：挂在结果卡头部，说明这张卡是哪个节点产出的。
 *
 * 元数据全部取自 utils/agents.js（U1），本组件不重复定义任何 Agent 信息。
 * 鼠标悬停只显示这个节点**干什么**，不显示源码路径 —— 实现细节不进使用者界面。
 */
const props = defineProps({
  /** 与 graph/workflow.py 一致的节点名：profile / growth / retrieval / … */
  node: { type: String, required: true },
  /** 可选的补充说明（如"已降级"），不传则不显示 */
  note: { type: String, default: '' },
  /** 是否显示序号圆点，默认显示 */
  showOrder: { type: Boolean, default: true },
})

const agent = computed(() => getAgent(props.node))

const style = computed(() => {
  const group = agent.value ? AGENT_GROUP[agent.value.group] : null
  if (!group) return {}
  return { '--agent-color': group.color, '--agent-soft': group.soft }
})

const title = computed(() =>
  agent.value ? agent.value.duty : '这部分的说明暂时读不出来',
)
</script>

<template>
  <span v-if="agent" class="agent-tag" :style="style" :title="title">
    <span v-if="showOrder" class="agent-tag-num">{{ agent.order }}</span>
    {{ agent.displayName }}
    <span v-if="note">· {{ note }}</span>
  </span>

  <!-- 认不出的步骤名：只给一句用户能懂的话，内部标识不往界面上摆。 -->
  <span v-else class="badge badge-warn" :title="title">暂不可用</span>
</template>
