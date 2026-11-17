<template>
  <section v-show="!hidden" class="sh-card" :class="{ 'is-bare': bare }">
    <header v-if="title || $slots.head" class="sh-card-head">
      <div v-if="title && $slots.icon" class="sh-card-icon"><slot name="icon" /></div>
      <div v-if="title" class="sh-card-head-main">
        <h3 class="sh-card-title">{{ title }}</h3>
      </div>
      <div v-if="$slots.head" class="sh-card-head-side"><slot name="head" /></div>
    </header>
    <div class="sh-card-body">
      <slot />
    </div>
    <footer v-if="$slots.foot || $slots.note" class="sh-card-foot">
      <span v-if="$slots.note" class="sh-card-foot-note"><slot name="note" /></span>
      <div v-if="$slots.foot" class="sh-card-foot-actions"><slot name="foot" /></div>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, provide, reactive, useId } from 'vue'
import {
  CARD_KEY,
  CARD_TITLE_KEY,
  SECTION_KEY,
  hitTest,
  useSearch,
  type SearchRow,
} from '../search'

const props = withDefaults(
  defineProps<{
    title: string
    keywords?: string
    /** 弹窗里用：不再画第二层卡片，只留分组标题与行表。 */
    bare?: boolean
  }>(),
  { keywords: '', bare: false }
)

const search = useSearch()
const section = inject(SECTION_KEY, { id: '', title: '', desc: '' })
const cardId = `card-${useId()}`

provide(CARD_KEY, cardId)
provide(CARD_TITLE_KEY, props.title)

const metaRow = reactive<SearchRow>({
  id: cardId,
  section: section.id,
  card: cardId,
  title: props.title,
  keywords: props.keywords,
  context: section.title,
  meta: true,
})

const selfMatch = computed(() => {
  const query = search?.query.value || ''
  return hitTest(query, props.title, props.keywords, section.title)
})

const childRows = computed(() =>
  (search?.rows || []).filter((row) => row.card === cardId && !row.meta)
)

const hasMatchedChild = computed(() =>
  childRows.value.some((row) => (search ? search.matches(row) : true))
)

const hidden = computed(() => {
  const query = search?.query.value.trim() || ''
  return Boolean(query) && !selfMatch.value && !hasMatchedChild.value
})

onMounted(() => search?.rows.push(metaRow))
onBeforeUnmount(() => {
  const index = search?.rows.indexOf(metaRow) ?? -1
  if (index >= 0) search?.rows.splice(index, 1)
})
</script>

<style scoped>
.sh-card-head-main {
  min-width: 0;
}
</style>
