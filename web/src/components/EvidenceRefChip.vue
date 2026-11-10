<script setup>
import { computed } from 'vue'

/**
 * 证据编号芯片（E01 / E02 …）。
 *
 * 编号由后端生成：agents/job_agent.py 的 `evidence_ref(index)` 返回 `E{index+1:02d}`，
 * 即**编号 = job_evidence 数组位置 + 1**。前端据此把「岗位要求 ↔ 原始证据」连起来。
 *
 * 没有原话支撑的要求（`evidence_status = model_supplement`）传空 code，
 * 此时渲染成**不可点击的占位芯片**，明确写出"原话里没这句" ——
 * 不能让一条没有依据的要求看起来像有依据。
 */
const props = defineProps({
  /** 形如 "E01"；为空表示这条要求没有原话支撑 */
  code: { type: String, default: '' },
  /** 当前是否处于定位高亮态 */
  active: { type: Boolean, default: false },
  /** 无 code 时显示的占位文案 */
  placeholder: { type: String, default: '无原话支撑' },
})

const emit = defineEmits(['select'])

// 后端只产出 E + 两位以上数字；不匹配的一律不当成有效编号
const CODE_PATTERN = /^E\d{2,}$/

const normalized = computed(() => String(props.code || '').trim().toUpperCase())
const valid = computed(() => CODE_PATTERN.test(normalized.value))

function handleClick() {
  if (!valid.value) return
  emit('select', normalized.value)
}
</script>

<template>
  <button
    v-if="valid"
    type="button"
    class="chip"
    :class="{ 'is-active': active }"
    :title="`查看第 ${normalized.slice(1)} 句招聘要求原话`"
    @click="handleClick"
  >
    {{ normalized }}
  </button>

  <span v-else class="chip chip-placeholder" :title="placeholder">{{ placeholder }}</span>
</template>
