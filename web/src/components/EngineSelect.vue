<template>
  <!-- 引擎/角色下拉：与情绪格同一套自定义下拉（Teleport 列表 + 键盘导航 + 外点关闭），
       不用原生 <select>。两种形态：bare（角色栏里的紧凑文本）与 field（弹框里的表单格）。 -->
  <button
    :id="id"
    ref="trigger"
    type="button"
    class="engine-select"
    :class="{ 'is-bare': bare }"
    :disabled="disabled"
    :aria-label="label"
    aria-haspopup="listbox"
    :aria-expanded="open"
    :aria-activedescendant="open && active >= 0 ? `engine-opt-${active}` : undefined"
    :title="title"
    @click="toggle"
    @keydown="onKeydown"
    @wheel="onWheel"
  >
    <span class="engine-select-label">{{ currentLabel }}</span>
    <span class="engine-select-arrow" aria-hidden="true" />
  </button>

  <Teleport :to="overlayTarget">
    <ul
      v-if="open"
      ref="menu"
      class="engine-menu"
      role="listbox"
      :aria-label="label"
      :style="menuStyle"
      @wheel="onWheel"
    >
      <li v-for="(item, index) in options" :key="item.key">
        <button
          :id="`engine-opt-${index}`"
          type="button"
          role="option"
          class="engine-option"
          :class="{ on: item.key === value, hot: index === active }"
          :aria-selected="item.key === value"
          tabindex="-1"
          @mousedown.prevent
          @click="pick(item.key)"
        >
          <Check v-if="item.key === value" :size="12" :stroke-width="2.2" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </button>
      </li>
    </ul>
  </Teleport>
</template>

<script setup>
// @ts-check
import { Check } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { createWheelStepper } from '../utils/wheel-step'
import { overlayTarget } from '../composables/overlayTarget'

const props = defineProps({
  value: { type: String, default: '' },
  /** ``[{ key, label }]``：可选的引擎（键是引擎 key，说法由后端给）。 */
  options: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
  label: { type: String, default: '引擎' },
  title: { type: String, default: '' },
  /** 紧凑文本形态（角色栏）；缺省 = 表单格形态（与 ``.sh-field`` 同一边框背景）。 */
  bare: { type: Boolean, default: false },
  /** 落到按钮上的 id（弹框里的 ``<label for>`` 指着它）。 */
  id: { type: String, default: '' },
  /** 滚轮在框上换值；密集表单一类"滚过去就改掉"的场景显式关掉。 */
  wheel: { type: Boolean, default: true },
})
const emit = defineEmits(['change', 'open'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const optionList = computed(() => /** @type {any[]} */ (props.options))

const currentLabel = computed(() => {
  const found = optionList.value.find((item) => item.key === props.value)
  return found?.label || props.value || '—'
})

const trigger = ref(/** @type {any} */ (null))
const menu = ref(/** @type {any} */ (null))
const open = ref(false)
const active = ref(-1)
const menuStyle = ref({})

function place() {
  const anchor = trigger.value?.getBoundingClientRect()
  if (!anchor) return
  const width = Math.max(anchor.width, 200)
  const gap = 4
  // 在弹窗里展开时以弹窗为边界：空间不够就朝上开，
  // 否则列表会压到按钮栏底下，下半截的选项点不到。
  const modal = trigger.value?.closest('.vh-modal')?.getBoundingClientRect()
  const limitTop = (modal?.top ?? 0) + 8
  const limitBottom = (modal?.bottom ?? window.innerHeight) - 8
  const height = Math.min(260, props.options.length * 28 + 8)
  const belowRoom = limitBottom - anchor.bottom - gap
  const aboveRoom = anchor.top - gap - limitTop
  const below = belowRoom >= height || belowRoom >= aboveRoom
  const maxHeight = Math.max(56, Math.min(height, below ? belowRoom : aboveRoom))
  menuStyle.value = {
    left: `${Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))}px`,
    top: below ? `${anchor.bottom + gap}px` : `${Math.max(limitTop, anchor.top - gap - maxHeight)}px`,
    width: `${width}px`,
    maxHeight: `${maxHeight}px`,
  }
}

/**
 * @param {number} step
 */
function openMenu(step = 0) {
  if (props.disabled || open.value) return
  open.value = true
  const current = props.options.findIndex(/** @param {any} item */ (item) => item.key === (props.value || ''))
  active.value = step ? (Math.max(0, current) + step + props.options.length) % props.options.length : Math.max(0, current)
  void nextTick(() => {
    place()
    menu.value?.querySelector(`#engine-opt-${active.value}`)?.scrollIntoView({ block: 'nearest' })
  })
  // 选项可能是异步补进来的（样例列表）：给宿主一个"该加载了"的信号。
  emit('open')
  document.addEventListener('mousedown', onDocDown)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', close)
}

function close() {
  open.value = false
  active.value = -1
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', close)
}

/**
 * 表格滚动时菜单跟着走会错位；菜单自己滚动不算。
 *
 * @param {any} event
 */
function onScroll(event) {
  if (menu.value?.contains(event.target)) return
  close()
}

function toggle() {
  if (open.value) close()
  else openMenu()
}

/**
 * @param {string} key
 */
function pick(key) {
  close()
  if (key !== (props.value || '')) emit('change', key)
}

/**
 * @param {number} step
 */
function move(step) {
  if (!open.value) {
    openMenu(step)
    return
  }
  active.value = (active.value + step + props.options.length) % props.options.length
  menu.value?.querySelector(`#engine-opt-${active.value}`)?.scrollIntoView({ block: 'nearest' })
}

/**
 * @param {any} event
 */
function onKeydown(event) {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    move(event.key === 'ArrowDown' ? 1 : -1)
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    if (!open.value) openMenu()
    else if (active.value >= 0) pick(optionList.value[active.value].key)
  } else if (event.key === 'Escape') {
    close()
  }
}

/**
 * @param {any} event
 */
function onDocDown(event) {
  if (trigger.value?.contains(event.target) || menu.value?.contains(event.target)) return
  close()
}

const wheelStep = createWheelStepper()

/**
 * 滚轮 = 在框上停着就换值：不展开列表，也不要求先点过它。
 * 下拉展开时不接管：滚轮归列表自己滚，值不在这里变。
 *
 * @param {any} event
 */
function onWheel(event) {
  if (props.disabled || !props.wheel || open.value) return
  // 紧凑形态（角色栏）不接，那里翻页比换引擎常见。
  if (props.bare) return
  const dir = wheelStep(event)
  if (!dir) return
  const items = optionList.value
  if (items.length < 2) return
  event.preventDefault()
  const index = items.findIndex((item) => item.key === props.value)
  const base = index < 0 ? (dir > 0 ? -1 : 0) : index
  const next = items[(base + dir + items.length) % items.length]
  if (next && next.key !== (props.value || '')) emit('change', next.key)
}

/** 选项异步补进来时重算高度：否则菜单还按「只有一条」开着，装不下新来的样例。 */
watch(
  () => props.options,
  () => {
    if (open.value) place()
  }
)

onBeforeUnmount(close)
</script>

<style scoped>
/* 基座：布局与焦点两形态共用；长相分 field（表单格）与 bare（角色栏的紧凑文本）。 */
.engine-select {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  min-width: 0;
  height: 36px;
  padding: 0 30px 0 11px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
  color: var(--sh-text);
  font: inherit;
  font-size: 12.8px;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), box-shadow var(--sh-t-fast) var(--sh-ease),
    color var(--sh-t-fast) var(--sh-ease);
}
.engine-select:hover:not(:disabled) {
  border-color: var(--sh-accent);
}
.engine-select:focus-visible {
  outline: none;
  border-color: var(--sh-accent);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.engine-select:disabled {
  opacity: 0.55;
  cursor: default;
}
.engine-select-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.engine-select-arrow {
  position: absolute;
  right: 10px;
  top: 50%;
  width: 4px;
  height: 4px;
  border-right: 1.5px solid var(--sh-text-4);
  border-bottom: 1.5px solid var(--sh-text-4);
  transform: translateY(-65%) rotate(45deg);
}
/* 紧凑文本形态：无框、跟随文字色，箭头跟在文字后面。 */
.engine-select.is-bare {
  height: auto;
  padding: 0 2px;
  border-color: transparent;
  background: transparent;
  color: var(--sh-text-3);
  font-size: 11px;
  line-height: 1.5;
}
.engine-select.is-bare:hover:not(:disabled) {
  border-color: transparent;
  background: transparent;
  color: var(--sh-accent);
}
.engine-select.is-bare:hover:not(:disabled) .engine-select-arrow {
  border-color: var(--sh-accent);
}
.engine-select.is-bare .engine-select-arrow {
  position: static;
  flex: none;
  transform: translateY(-1px) rotate(45deg);
}
.engine-menu {
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
.engine-option {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
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
/* 样例名可能很长（情绪文案就是一句话）：单行省略，不折成两行把菜单撑高。 */
.engine-option span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.engine-option:hover,
.engine-option.hot {
  background: var(--sh-accent-soft);
}
.engine-option.on {
  color: var(--sh-accent);
}
</style>
