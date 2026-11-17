<template>
  <div
    v-show="visible"
    class="sh-row"
    :class="{ 'sh-row--stack': stacked, 'sh-row--wide-control': wide }"
    :data-row-id="id"
  >
    <div class="sh-row-main">
      <div class="sh-row-title">
        {{ title }}
        <slot name="badge" />
      </div>
      <div v-if="desc" class="sh-row-desc">{{ desc }}</div>
      <slot name="below" />
    </div>
    <div class="sh-row-control">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, reactive, useId, watch } from 'vue'
import { CARD_KEY, CARD_TITLE_KEY, SECTION_KEY, useSearch, type SearchRow } from '../search'

const props = withDefaults(
  defineProps<{
    title: string
    desc?: string
    keywords?: string
    rowId?: string
    stacked?: boolean
    wide?: boolean
  }>(),
  { desc: '', keywords: '', rowId: '', stacked: false, wide: false }
)

const search = useSearch()
const cardId = inject(CARD_KEY, '')
const cardTitle = inject(CARD_TITLE_KEY, '')
const section = inject(SECTION_KEY, { id: '', title: '', desc: '' })

// useId() 只能在 setup 里调一次，不能塞进 computed。
const autoId = useId()
const id = computed(() => props.rowId || `row-${autoId}`)

const entry = reactive<SearchRow>({
  id: id.value,
  section: section.id,
  card: cardId,
  title: props.title,
  desc: props.desc,
  keywords: props.keywords,
  context: `${section.title} ${cardTitle}`,
})

const visible = computed(() => {
  if (!search) return true
  return search.matches(entry)
})

onMounted(() => {
  search?.rows.push(entry)
})

onBeforeUnmount(() => {
  const index = search?.rows.indexOf(entry) ?? -1
  if (index >= 0) search?.rows.splice(index, 1)
})

watch(
  () => [props.title, props.desc, props.keywords],
  () => {
    entry.title = props.title
    entry.desc = props.desc
    entry.keywords = props.keywords
  }
)
</script>
