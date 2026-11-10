<script setup>
defineProps({
  items: { type: Array, required: true },
  active: { type: String, required: true },
  label: { type: String, default: '子页面导航' },
})
</script>

<template>
  <nav class="section-nav" :aria-label="label">
    <RouterLink
      v-for="item in items"
      :key="item.key"
      :to="item.to"
      class="section-nav__link"
      :class="{ 'is-active': active === item.key }"
      :aria-current="active === item.key ? 'page' : undefined"
    >
      <span>{{ item.label }}</span>
      <span v-if="item.count != null && item.count !== ''" class="section-nav__count">
        {{ item.count }}
      </span>
    </RouterLink>
  </nav>
</template>
