<template>
  <button
    ref="trigger"
    type="button"
    class="emotion-field"
    :class="value ? `has-value tone-${emotionTone(value)}` : ''"
    :disabled="disabled"
    :aria-label="value ? `情绪：${value}` : '设置情绪'"
    aria-haspopup="listbox"
    :aria-expanded="open"
    :aria-activedescendant="open && active >= 0 ? `emotion-opt-${active}` : undefined"
    @click="toggle"
    @keydown="onKeydown"
  >
    <component
      :is="emotionIcon(value)"
      v-if="value && emotionIcon(value)"
      :size="13"
      :stroke-width="1.9"
      class="emotion-icon"
      aria-hidden="true"
    />
    <span class="emotion-label">{{ value }}</span>
  </button>

  <Teleport :to="overlayTarget">
    <ul
      v-if="open"
      ref="menu"
      class="emotion-menu"
      role="listbox"
      aria-label="情绪"
      :style="menuStyle"
    >
      <li v-for="(item, index) in items" :key="item.value || '__none__'">
        <div v-if="item.group" class="emotion-group" role="presentation">{{ item.group }}</div>
        <button
          :id="`emotion-opt-${index}`"
          type="button"
          role="option"
          class="emotion-option"
          :class="{ on: item.value === (value || ''), hot: index === active }"
          :aria-selected="item.value === (value || '')"
          tabindex="-1"
          @mousedown.prevent
          @click="pick(item.value)"
        >
          <component
            :is="item.icon"
            v-if="item.icon"
            :size="14"
            :stroke-width="1.8"
            aria-hidden="true"
          />
          <span>{{ item.label }}</span>
        </button>
      </li>
    </ul>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import {
  Angry,
  Annoyed,
  Eye,
  Frown,
  Ghost,
  Meh,
  Minus,
  PartyPopper,
  Smile,
  Snowflake,
  Waves,
  Wind,
} from 'lucide-vue-next'
import { emotionTone } from '../../components/ui/tones'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  value: { type: String, default: '' },
  /** 分组的候选：`[{ label, words }]`。分组只影响摆法，值本身仍是自由文本。 */
  groups: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['change'])

/** 词 → 小图标。**没有对应图标不是缺口**：词照样列、照样选，只是前面少一枚章。
 *  复合情绪大多没有一眼能认的图形，就不硬挑一个（挑错了比没有更误导）。 */
const EMOTION_ICONS = {
  开心: Smile,
  悲伤: Frown,
  愤怒: Angry,
  恐惧: Ghost,
  惊讶: Eye,
  兴奋: PartyPopper,
  平静: Waves,
  冷漠: Snowflake,
  无奈: Meh,
  释然: Wind,
  // 旧词：候选里没有了，已有的行还要正常显示。
  中性: Meh,
  难过: Frown,
  生气: Angry,
  害怕: Ghost,
  厌恶: Annoyed,
}
/** 摊平成一列，组名挂在每组第一条上：键盘上下键走的是同一列，不必再算一次偏移。 */
const groupList = computed(() => /** @type {any[]} */ (props.groups))
const items = computed(() => {
  const rows = [{ value: '', label: '不设置', icon: Minus, group: '' }]
  for (const group of groupList.value) {
    const words = group.words || []
    words.forEach(
      /** @param {string} name @param {number} at */
      (name, at) => {
        rows.push({
          value: name,
          label: name,
          icon: /** @type {Record<string, any>} */ (EMOTION_ICONS)[name] || null,
          group: at === 0 ? group.label : '',
        })
      }
    )
  }
  return rows
})

const trigger = ref(/** @type {any} */ (null))
const menu = ref(/** @type {any} */ (null))
const open = ref(false)
const active = ref(-1)
const menuStyle = ref({})

/**
 * @param {any} name
 */
function emotionIcon(name) {
  return /** @type {Record<string, any>} */ (EMOTION_ICONS)[name] || null
}

function place() {
  const anchor = trigger.value?.getBoundingClientRect()
  if (!anchor) return
  const width = Math.max(anchor.width, 148)
  const headers = items.value.filter(/** @param {any} item */ (item) => item.group).length
  const height = Math.min(300, items.value.length * 30 + headers * 20 + 8)
  const gap = 4
  const below = anchor.bottom + gap + height <= window.innerHeight - 8
  menuStyle.value = {
    left: `${Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))}px`,
    top: below ? `${anchor.bottom + gap}px` : `${Math.max(8, anchor.top - height - gap)}px`,
    width: `${width}px`,
    maxHeight: `${height}px`,
  }
}

/**
 * @param {number} step
 */
function openMenu(step = 0) {
  if (props.disabled || open.value) return
  open.value = true
  const current = items.value.findIndex(/** @param {any} item */ (item) => item.value === (props.value || ''))
  active.value = step ? (Math.max(0, current) + step + items.value.length) % items.value.length : Math.max(0, current)
  void nextTick(() => {
    place()
    menu.value?.querySelector(`#emotion-opt-${active.value}`)?.scrollIntoView({ block: 'nearest' })
  })
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
 * @param {any} value
 */
function pick(value) {
  close()
  if (value !== (props.value || '')) emit('change', value)
}

/**
 * @param {number} step
 */
function move(step) {
  if (!open.value) {
    openMenu(step)
    return
  }
  active.value = (active.value + step + items.value.length) % items.value.length
  menu.value?.querySelector(`#emotion-opt-${active.value}`)?.scrollIntoView({ block: 'nearest' })
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
    else if (active.value >= 0) pick(items.value[active.value].value)
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

onBeforeUnmount(close)
</script>

<style scoped>
/* 与角色列同高同边的下拉：空白就是没设情绪，不再用「—」占位。
   没有下拉箭头时它就是一段可选中的文字，居中放——靠左会读成"这一栏空着"。 */
.emotion-field {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-xs);
  background: var(--sh-panel);
  color: var(--sh-text-2);
  font: inherit;
  font-size: calc(var(--ws-font, 12.5px) - 0.4px);
  text-align: center;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), box-shadow var(--sh-t-fast) var(--sh-ease);
}
.emotion-field:hover {
  border-color: var(--sh-line);
}
.emotion-field:focus-visible {
  outline: none;
  border-color: var(--sh-accent);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.emotion-field:disabled {
  opacity: 0.55;
  cursor: default;
}
.emotion-field.has-value {
  border-color: color-mix(in srgb, currentColor 18%, transparent);
  background: var(--tone-bg, var(--sh-panel));
  color: var(--tone-fg, var(--sh-text-2));
}
.emotion-icon {
  flex: none;
  opacity: 0.92;
}
.emotion-label {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.emotion-menu {
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
.emotion-group {
  padding: 6px 8px 2px;
  color: var(--sh-text-3);
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
.emotion-option {
  display: flex;
  align-items: center;
  gap: 8px;
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
.emotion-option:hover,
.emotion-option.hot {
  background: var(--sh-accent-soft);
}
.emotion-option.on {
  color: var(--sh-accent);
}
</style>
