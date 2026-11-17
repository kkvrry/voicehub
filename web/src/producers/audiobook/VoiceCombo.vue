<template>
  <!-- 音色输入框：可以直接打字筛，也可以从列表里点。
       边框画在外层「字段」上，右侧插槽放试听 / AI 这类按钮——在用户眼里
       它们和输入框是同一个控件，不是摆在旁边的两个圆点。
       下拉挂在 .app-shell 上（fixed），不看表格滚动容器的裁切。
       滚轮不换值：分配表里滚动是浏览，滚过一格就悄悄换掉音色太容易误操作。 -->
  <div class="voice-field" :class="{ 'is-disabled': disabled }">
    <input
      ref="input"
      :value="text"
      type="text"
      class="voice-combo"
      :disabled="disabled"
      :placeholder="placeholder"
      :aria-label="label"
      role="combobox"
      aria-autocomplete="list"
      :aria-expanded="open"
      @focus="openMenu"
      @input="onInput"
      @keydown="onKeydown"
      @blur="close"
    />
    <div v-if="$slots.suffix" class="voice-field-acts">
      <slot name="suffix" />
    </div>
  </div>
  <Teleport :to="overlayTarget">
    <ul
      v-if="open"
      ref="menu"
      class="voice-menu"
      role="listbox"
      :aria-label="label"
      :style="menuStyle"
    >
      <li v-for="(item, index) in filtered" :key="item.key || 'auto'">
        <button
          type="button"
          role="option"
          class="voice-option"
          :class="{ on: item.key === value, hot: index === active }"
          :aria-selected="item.key === value"
          tabindex="-1"
          @mousedown.prevent
          @click="pick(item.key)"
        >
          <Check v-if="item.key === value" :size="11" :stroke-width="2.2" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </button>
      </li>
      <li v-if="!filtered.length" class="voice-menu-empty">没有匹配的音色</li>
    </ul>
  </Teleport>
</template>

<script setup>
// @ts-check
import { Check } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  value: { type: String, default: '' },
  /** ``[{ key, label, keywords? }]``：``keywords`` 是不显示在标签行里的搜索词
   *  （标签、语言等），手动输入时一并参与匹配。 */
  options: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
  label: { type: String, default: '音色' },
  placeholder: { type: String, default: '' },
})
const emit = defineEmits(['change'])

const input = ref(/** @type {any} */ (null))
const menu = ref(/** @type {any} */ (null))
const open = ref(false)
const query = ref('')
/** 正在打字：输入框显示查询词，而不是当前选中项的名字。 */
const editing = ref(false)
const active = ref(-1)
const menuStyle = ref({})

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const optionList = computed(() => /** @type {any[]} */ (props.options))
const selected = computed(() =>
  optionList.value.find((item) => item.key === props.value)
)
const label = computed(() => selected.value?.label || props.value || '')
const text = computed(() => (editing.value ? query.value : label.value))
/** 手动输入给相近候选：先按打完的词做包含匹配（标签、语言也参与）；
 *  全都对不上再按字符顺序凑——「温柔女」也能落到「温柔 · 女·青年」。 */
const filtered = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  if (!keyword) return optionList.value
  /** @param {any} item */
  const hay = (item) => `${item.label} ${item.keywords || ''}`.toLowerCase()
  const terms = keyword.split(/\s+/).filter(Boolean)
  const strict = optionList.value.filter(/** @param {any} item */ (item) => terms.every((term) => hay(item).includes(term)))
  if (strict.length) return strict
  const needle = terms.join('')
  return optionList.value.filter(/** @param {any} item */ (item) => isSubsequence(needle, hay(item)))
})

/**
 * @param {any} needle
 * @param {any} text
 */
function isSubsequence(needle, text) {
  let index = 0
  for (const char of text) {
    if (char === needle[index]) index += 1
    if (index === needle.length) return true
  }
  return index === needle.length
}

function place() {
  const rect = input.value?.getBoundingClientRect()
  if (!rect) return
  const width = Math.max(rect.width, 220)
  const height = Math.min(240, Math.max(60, filtered.value.length * 27 + 12))
  const below = rect.bottom + 4 + height <= window.innerHeight - 8
  menuStyle.value = {
    left: `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`,
    top: below ? `${rect.bottom + 4}px` : `${Math.max(8, rect.top - height - 4)}px`,
    width: `${width}px`,
    maxHeight: `${height}px`,
  }
}

function openMenu() {
  if (props.disabled || open.value) return
  open.value = true
  editing.value = false
  query.value = ''
  const index = props.options.findIndex(/** @param {any} item */ (item) => item.key === props.value)
  active.value = Math.max(0, index)
  void nextTick(() => {
    place()
    // 全选当前名字：第一下打字就是替换，而不是接在「胡桃 · 女·青年」后面。
    input.value?.select()
    menu.value?.querySelector('.voice-option.hot')?.scrollIntoView({ block: 'nearest' })
  })
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', close)
}

function close() {
  if (!open.value) return
  open.value = false
  editing.value = false
  query.value = ''
  active.value = -1
  window.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', close)
}

/**
 * @param {any} event
 */
function onScroll(event) {
  if (menu.value?.contains(event.target)) return
  close()
}

/**
 * @param {any} event
 */
function onInput(event) {
  if (!open.value) openMenu()
  editing.value = true
  query.value = event.target.value
  active.value = filtered.value.length ? 0 : -1
  void nextTick(place)
}

/**
 * @param {number} step
 */
function move(step) {
  if (!open.value) {
    openMenu()
    return
  }
  const size = filtered.value.length
  if (!size) return
  active.value = (active.value + step + size) % size
  void nextTick(() => {
    place()
    menu.value?.querySelector('.voice-option.hot')?.scrollIntoView({ block: 'nearest' })
  })
}

/**
 * @param {string} key
 */
function pick(key) {
  close()
  if (key !== props.value) emit('change', key)
}

/**
 * @param {any} event
 */
function onKeydown(event) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    move(event.key === 'ArrowDown' ? 1 : -1)
  } else if (event.key === 'Enter') {
    if (!open.value) return
    event.preventDefault()
    const item = filtered.value[active.value] || filtered.value[0]
    if (item) pick(item.key)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    close()
  }
}

onBeforeUnmount(close)
</script>

<style scoped>
/* 字段外框：边框、背景、焦点环都在这一层，内部输入框只管文字。 */
.voice-field {
  display: flex;
  align-items: center;
  width: 100%;
  min-width: 0;
  height: 36px;
  padding-right: 3px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
  transition: border-color var(--sh-t-fast) var(--sh-ease), box-shadow var(--sh-t-fast) var(--sh-ease);
}
.voice-field:hover:not(.is-disabled) {
  border-color: var(--sh-accent);
}
.voice-field:focus-within {
  border-color: var(--sh-accent);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.voice-field.is-disabled {
  opacity: 0.55;
}
.voice-combo {
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0 9px;
  border: 0;
  background: transparent;
  color: var(--sh-text);
  font: inherit;
  font-size: 12.5px;
  text-overflow: ellipsis;
}
.voice-combo::placeholder {
  color: var(--sh-text-4);
}
.voice-combo:focus {
  outline: none;
}
.voice-combo:disabled {
  cursor: default;
}
/* 插槽里的按钮：贴边竖排，与输入框同高的是「控件的动作区」，不是独立圆点。 */
.voice-field-acts {
  display: flex;
  flex: none;
  align-items: center;
  gap: 2px;
  padding-left: 3px;
  border-left: 1px solid var(--sh-line-soft);
}
.voice-menu {
  position: fixed;
  z-index: 60;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  list-style: none;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-xs);
  background: var(--sh-glass-strong);
  box-shadow: var(--sh-shadow-pop);
}
.voice-option {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 7px;
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
.voice-option span {
  overflow: hidden;
  text-overflow: ellipsis;
}
.voice-option:hover,
.voice-option.hot {
  background: var(--sh-accent-soft);
}
.voice-option.on {
  color: var(--sh-accent);
}
.voice-menu-empty {
  padding: 6px 8px;
  color: var(--sh-text-4);
  font-size: 11px;
}
</style>
