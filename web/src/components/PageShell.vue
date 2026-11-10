<script setup>
import { computed, useSlots } from 'vue'

defineProps({
  eyebrow: { type: String, default: '' },
  title: { type: String, required: true },
  railLabel: { type: String, default: '次要读数' },
  /** 页面级变体：只改这一页的外壳（例如概览整页摊平、不套玻璃卡）。 */
  variant: { type: String, default: '' },
})

const slots = useSlots()
const hasRail = computed(() => !!slots.rail)
</script>

<template>
  <div class="pg" :class="variant ? `pg--${variant}` : null">
    <header class="pg-mast">
      <div>
        <p v-if="eyebrow" class="pg-eyebrow">{{ eyebrow }}</p>
        <h1 class="pg-title">{{ title }}</h1>
      </div>
      <p v-if="slots.deck" class="pg-deck"><slot name="deck" /></p>
    </header>

    <div class="pg-body" :class="hasRail ? 'pg-body--rail' : 'pg-body--full'">
      <section class="pg-main">
        <slot />
      </section>

      <aside v-if="hasRail" class="pg-index" :aria-label="railLabel">
        <slot name="rail" />
      </aside>
    </div>
  </div>
</template>
