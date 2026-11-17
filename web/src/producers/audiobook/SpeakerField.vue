<template>
  <div ref="box" class="speaker-field">
    <UserRound class="speaker-icon" :size="14" :stroke-width="1.8" aria-hidden="true" />
    <input
      class="speaker-input"
      :value="text"
      :disabled="disabled"
      :placeholder="placeholder"
      :aria-label="placeholder"
      spellcheck="false"
      @input="onInput"
      @focus="open = true"
      @blur="commit"
      @keydown.enter.prevent="commit"
      @keydown.esc="reset"
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
    />
    <button
      type="button"
      class="speaker-caret"
      tabindex="-1"
      :disabled="disabled"
      aria-label="候选角色"
      @mousedown.prevent
      @click="toggle"
    >
      <ChevronDown :size="12" :stroke-width="1.8" aria-hidden="true" />
    </button>
    <ul v-if="open && options.length" class="speaker-menu" :class="{ up: dropUp }">
      <li v-for="(name, index) in options" :key="name">
        <button
          type="button"
          :class="{ on: name === text, hot: index === active }"
          @mousedown.prevent="take(name)"
        >
          {{ name }}
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup>
// @ts-check
/** 角色输入：候选是参考样本，不是白名单——新角色直接打进去。
 *
 *  提交时机为回车 / 失焦 / 取候选；空串按「旁白」提交。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ChevronDown, UserRound } from 'lucide-vue-next'

const props = defineProps({
  value: { type: String, default: '' },
  options: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
  placeholder: { type: String, default: '角色' },
})
const emit = defineEmits(['change'])

const text = ref(props.value || '')
const open = ref(false)
const active = ref(-1)
const dropUp = ref(false)
const box = ref(/** @type {HTMLElement | null} */ (null))

watch(
  () => props.value,
  (value) => {
    text.value = value || ''
  }
)

const menuHeight = computed(() => Math.min(220, props.options.length * 26 + 8))

function needsFlip() {
  const rect = box.value?.getBoundingClientRect()
  return rect ? rect.bottom + menuHeight.value > window.innerHeight : false
}

/** @param {Event} event */
function onInput(event) {
  text.value = /** @type {HTMLInputElement} */ (event.target).value
  open.value = true
  active.value = -1
}

function reset() {
  text.value = props.value || ''
  open.value = false
  active.value = -1
}

function commit() {
  open.value = false
  const next = text.value.trim() || '旁白'
  text.value = next
  if (next === (props.value || '')) return
  emit('change', next)
}

function toggle() {
  open.value = !open.value
  active.value = -1
  if (open.value) dropUp.value = needsFlip()
}

/** @param {number} step */
function move(step) {
  if (!open.value) {
    open.value = true
    dropUp.value = needsFlip()
  }
  const count = props.options.length
  if (!count) return
  active.value = (active.value + step + count) % count
}

/** @param {string} name */
function take(name) {
  text.value = name
  open.value = false
  active.value = -1
  if (name !== (props.value || '')) emit('change', name)
}

/** 点别处收起：不在输入框里按下的点击不留给 blur（取候选那一下自己处理）。 */
/** @param {MouseEvent} event */
function onDocDown(event) {
  const target = /** @type {Node | null} */ (event.target)
  if (open.value && box.value && !box.value.contains(target)) open.value = false
}
onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocDown))
</script>

<style scoped>
/* 一整块是下拉框：图标 + 名字 + 箭头，边框由外层给，输入框本身透明 */
.speaker-field {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 8px;
  min-width: 0;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-xs);
  background: var(--sh-panel);
  transition: border-color var(--sh-t-fast) var(--sh-ease), box-shadow var(--sh-t-fast) var(--sh-ease);
}
.speaker-field:hover {
  border-color: var(--sh-line);
}
.speaker-field:focus-within {
  border-color: var(--sh-accent);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.speaker-icon {
  flex: none;
  color: var(--sh-text-4);
}
.speaker-input {
  flex: 1;
  min-width: 0;
  padding: 0 12px 0 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: calc(var(--ws-font, 12.5px) - 0.4px);
  line-height: 1.55;
  text-overflow: ellipsis;
}
.speaker-input:focus {
  outline: none;
}
.speaker-caret {
  position: absolute;
  top: 50%;
  right: 5px;
  width: 14px;
  height: 18px;
  display: grid;
  place-items: center;
  padding: 0;
  transform: translateY(-50%);
  border: 0;
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
  transition: color var(--sh-t-fast) var(--sh-ease);
}
.speaker-caret:hover {
  color: var(--sh-accent);
}
.speaker-menu {
  position: absolute;
  top: calc(100% + 2px);
  left: 0;
  z-index: 20;
  min-width: max(100%, 96px);
  max-height: 220px;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  list-style: none;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-xs);
  background: var(--sh-glass-strong);
  box-shadow: var(--sh-shadow-pop);
}
.speaker-menu.up {
  top: auto;
  bottom: calc(100% + 2px);
}
.speaker-menu button {
  display: block;
  width: 100%;
  padding: 4px 8px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11.5px;
  text-align: left;
  white-space: nowrap;
  cursor: pointer;
}
.speaker-menu button:hover,
.speaker-menu button.hot {
  background: var(--sh-accent-soft);
}
.speaker-menu button.on {
  color: var(--sh-accent);
}
</style>
