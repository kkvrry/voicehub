<template>
  <div class="top-filter">
    <span class="top-filter-sep" aria-hidden="true"></span>
    <button
      ref="anchorEl"
      type="button"
      class="top-filter-btn"
      :class="{ 'is-on': activeCount }"
      :aria-label="activeCount ? `筛选音色，已启用 ${activeCount} 项` : '筛选音色'"
      aria-haspopup="dialog"
      :aria-expanded="open"
      :title="title"
      :aria-keyshortcuts="ariaKeyshortcuts"
      @click="open = !open"
    >
      <ListFilter :size="15" :stroke-width="1.8" />
      <span v-if="activeCount" class="top-filter-badge">{{ activeCount }}</span>
    </button>

    <Popover :open="open" :anchor="anchorEl" :width="330" label="筛选音色" @close="open = false">
      <div class="vpop-head">
        筛选音色
        <span v-if="activeCount" class="vpop-hint">{{ activeCount }} 项生效</span>
      </div>

      <section class="tf-section">
        <div class="tf-label">性别</div>
        <div class="vpop-grid">
          <button
            type="button"
            class="tag-chip"
            :class="{ active: !store.filters.gender }"
            :aria-pressed="!store.filters.gender"
            @click="pickGender('')"
          >
            全部
          </button>
          <button
            v-for="[value, label] in vocab.genders"
            :key="value"
            type="button"
            class="tag-chip"
            :class="{ active: store.filters.gender === value }"
            :aria-pressed="store.filters.gender === value"
            @click="pickGender(value)"
          >
            {{ label }}
          </button>
        </div>
      </section>

      <section class="tf-section">
        <div class="tf-label">年龄感</div>
        <div class="vpop-grid">
          <button
            type="button"
            class="tag-chip"
            :class="{ active: !store.filters.age }"
            :aria-pressed="!store.filters.age"
            @click="pickAge('')"
          >
            全部
          </button>
          <button
            v-for="[value, label] in vocab.ages"
            :key="value"
            type="button"
            class="tag-chip"
            :class="{ active: store.filters.age === value }"
            :aria-pressed="store.filters.age === value"
            @click="pickAge(value)"
          >
            {{ label }}
          </button>
        </div>
      </section>

      <section class="tf-section">
        <div class="tf-label">语言</div>
        <div class="vpop-grid">
          <button
            type="button"
            class="tag-chip"
            :class="{ active: !store.filters.language }"
            :aria-pressed="!store.filters.language"
            @click="pickLanguage('')"
          >
            全部
          </button>
          <button
            v-for="[value, label] in vocab.languages"
            :key="value"
            type="button"
            class="tag-chip"
            :class="{ active: store.filters.language === value }"
            :aria-pressed="store.filters.language === value"
            @click="pickLanguage(value)"
          >
            {{ label }}
          </button>
        </div>
      </section>

      <section class="tf-section">
        <div class="tf-label">情绪 / 标签</div>
        <div v-if="tags.length" class="vpop-grid">
          <button
            type="button"
            class="tag-chip"
            :class="{ active: !store.filters.tag }"
            :aria-pressed="!store.filters.tag"
            @click="pickTag('')"
          >
            全部
          </button>
          <button
            v-for="entry in tags"
            :key="entry.tag"
            type="button"
            class="tag-chip"
            :class="{ active: store.filters.tag === entry.tag }"
            :aria-pressed="store.filters.tag === entry.tag"
            :title="`筛出 ${entry.count} 个`"
            @click="pickTag(entry.tag)"
          >
            {{ entry.tag }}<span v-if="entry.count" class="tag-count">{{ entry.count }}</span>
          </button>
        </div>
        <p v-else class="tf-empty">当前范围内还没有标签。</p>
      </section>

      <button v-if="activeCount" type="button" class="vpop-clear" @click="clearFilters">
        清除筛选
      </button>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ListFilter } from 'lucide-vue-next'
import { actions, store, vocab } from '../store'
import { useSettingsStore } from '../store/settings'
import { comboFromEvent, recorder } from '../settings/keymap'
import { prettyCombo } from '../settings/utils'
import Popover from './ui/Popover.vue'

const open = ref(false)
const anchorEl = ref(/** @type {any} */ (null))
const settings = useSettingsStore()

const activeCount = computed(
  () =>
    Number(Boolean(store.filters.gender)) +
    Number(Boolean(store.filters.age)) +
    Number(Boolean(store.filters.language)) +
    Number(Boolean(store.filters.tag))
)

/** 标题里带上键位：这枚按钮是面板唯一常驻的入口，快捷方式只能落在这儿才被发现。 */
const title = computed(() => {
  const head = activeCount.value ? `筛选音色（${activeCount.value} 项生效）` : '筛选音色'
  const keys = prettyCombo(settings.shortcuts.openFilters).join(' + ')
  return keys ? `${head} · ${keys}` : head
})

/** ARIA 的写法是 `Control+Shift+F`，与存储里的 `Ctrl` 不同。 */
const ariaKeyshortcuts = computed(() =>
  settings.shortcuts.openFilters.replace(/\bCtrl\b/g, 'Control')
)

/** 键位只在面板存在的页面上生效（音色库 / 合成 / 生产方）。
 *  录制键位时让路；只有"裸键"才让给输入——带修饰键的组合不会是打字，输入框里也照常可用。 */
/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (recorder.action) return
  const combo = comboFromEvent(event)
  if (!combo || combo !== settings.shortcuts.openFilters) return
  const bareKey = !combo.includes('+')
  const target = /** @type {HTMLElement | null} */ (event.target)
  const typing =
    !!target &&
    (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
  if (bareKey && typing) return
  event.preventDefault()
  open.value = !open.value
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

/** 标签云已按当前范围算好；当前选中的那枚若被别的条件挤出前 16，补在最前面。 */
const tags = computed(() => {
  const cloud = (store.tagCloud || []).slice(0, 16)
  const active = store.filters.tag
  if (active && !cloud.some((entry) => entry.tag === active)) {
    return [{ tag: active, count: 0 }, ...cloud]
  }
  return cloud
})

/** @param {string} value */
async function pickGender(value) {
  store.filters.gender = store.filters.gender === value ? '' : value
  await actions.loadVoices()
}

/** @param {string} value */
async function pickAge(value) {
  store.filters.age = store.filters.age === value ? '' : value
  await actions.loadVoices()
}

/** @param {string} value */
async function pickLanguage(value) {
  store.filters.language = store.filters.language === value ? '' : value
  await actions.loadVoices()
}

/** @param {string} value */
async function pickTag(value) {
  store.filters.tag = store.filters.tag === value ? '' : value
  await actions.loadVoices()
}

async function clearFilters() {
  store.filters.gender = ''
  store.filters.age = ''
  store.filters.language = ''
  store.filters.tag = ''
  await actions.loadVoices()
}
</script>

<style scoped>
.top-filter {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.top-filter-sep {
  width: 1px;
  height: 18px;
  background: var(--sh-line);
}
.top-filter-btn {
  position: relative;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
  transition: color var(--sh-t-fast), background var(--sh-t-fast);
}
.top-filter-btn:hover {
  color: var(--sh-text-2);
  background: var(--sh-hover);
}
.top-filter-btn:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 1px;
}
.top-filter-btn.is-on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.top-filter-badge {
  position: absolute;
  top: -3px;
  right: -4px;
  min-width: 14px;
  height: 14px;
  padding: 0 3px;
  display: grid;
  place-items: center;
  border-radius: 999px;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
  font-size: 9px;
  font-weight: 700;
  line-height: 1;
}
.tf-section + .tf-section {
  margin-top: 10px;
  padding-top: 9px;
  border-top: 1px solid var(--sh-line-soft);
}
.tf-label {
  margin-bottom: 6px;
  font-size: 10.8px;
  font-weight: 600;
  color: var(--sh-text-4);
}
.tf-empty {
  margin: 0;
  font-size: 11.4px;
  color: var(--sh-text-4);
}
</style>
