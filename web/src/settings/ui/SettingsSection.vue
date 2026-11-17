<template>
  <section v-show="!hidden" :id="`sec-${id}`" class="sh-sec" :data-section="id">
    <header class="sh-sec-head">
      <div v-if="$slots.icon" class="sh-sec-icon"><slot name="icon" /></div>
      <div class="sh-sec-head-main">
        <h2 class="sh-sec-title">{{ title }}</h2>
        <div v-if="desc" class="sh-sec-sub">{{ desc }}</div>
      </div>
      <div v-if="actionsVisible || $slots.meta" class="sh-sec-meta">
        <slot name="meta" />
        <SettingsActions v-if="actionsVisible" />
      </div>
    </header>
    <slot />
  </section>
</template>

<script setup lang="ts">
import { computed, provide } from 'vue'
import { SECTION_KEY, hitTest, useActiveSection, useSearch } from '../search'
import SettingsActions from './SettingsActions.vue'

const props = withDefaults(
  defineProps<{
    id: string
    title: string
    desc?: string
    /** 要不要在标题右侧摆「保存设置」那一组。插件分组自己有保存按钮。 */
    showActions?: boolean
  }>(),
  { desc: '', showActions: true }
)

const search = useSearch()
const active = useActiveSection()
const actionsVisible = computed(() => props.showActions && active?.value === props.id)

provide(SECTION_KEY, { id: props.id, title: props.title, desc: props.desc })

const rowsInSection = computed(() =>
  (search?.rows || []).filter((row) => row.section === props.id)
)

const hidden = computed(() => {
  const query = search?.query.value.trim() || ''
  if (!query) return false
  if (hitTest(query, props.title, props.desc)) return false
  return !rowsInSection.value.some((row) => search?.matches(row))
})
</script>

<style scoped>
.sh-sec-head-main {
  min-width: 0;
}
</style>
