<template>
  <Teleport :to="overlayTarget">
    <Transition name="ctxmenu">
      <div
        v-if="menu.open"
        ref="panelEl"
        class="ctx-menu"
        :style="pos"
        role="menu"
        tabindex="-1"
        @contextmenu.prevent
        @keydown="onKeydown"
      >
        <template v-for="(group, gi) in menu.groups" :key="gi">
          <div v-if="gi" class="ctx-sep" role="separator"></div>
          <button
            v-for="item in group"
            :key="`${gi}-${item.key}`"
            type="button"
            class="ctx-item"
            :class="{
              'is-danger': item.danger,
              'is-open': hasSub(item) && submenu.open && submenu.parentKey === item.key,
            }"
            :data-key="item.key"
            role="menuitem"
            :aria-haspopup="hasSub(item) ? 'menu' : undefined"
            :aria-expanded="hasSub(item) ? submenu.open && submenu.parentKey === item.key : undefined"
            :disabled="item.disabled"
            :title="item.disabled ? item.reason || undefined : undefined"
            @mouseenter="onItemEnter(item, $event)"
            @click="onItemClick(item, $event)"
          >
            <component
              :is="item.icon"
              v-if="item.icon"
              :size="14"
              :stroke-width="1.8"
              aria-hidden="true"
            />
            <span class="ctx-label">{{ item.label }}</span>
            <ChevronRight
              v-if="hasSub(item)"
              class="ctx-caret"
              :size="13"
              :stroke-width="1.8"
              aria-hidden="true"
            />
          </button>
        </template>
      </div>
    </Transition>

    <Transition name="ctxmenu">
      <div
        v-if="submenu.open"
        ref="subEl"
        class="ctx-menu ctx-sub"
        :style="subPos"
        role="menu"
        tabindex="-1"
        @contextmenu.prevent
        @keydown="onKeydown"
      >
        <button
          v-for="item in submenu.items"
          :key="item.key"
          type="button"
          class="ctx-item"
          :class="{ 'is-danger': item.danger }"
          :data-key="item.key"
          role="menuitem"
          :disabled="item.disabled"
          :title="item.disabled ? item.reason || undefined : undefined"
          @click="run(item)"
        >
          <component
            :is="item.icon"
            v-if="item.icon"
            :size="14"
            :stroke-width="1.8"
            aria-hidden="true"
          />
          <span class="ctx-label">{{ item.label }}</span>
        </button>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
// 全应用唯一的一份右键菜单：光标模式贴在指针处，锚点模式贴在「⋯」按钮右下。
// 带 children 的项再展开一层子菜单（贴着这一项右缘）。
// 外点、滚动、resize、失焦与 Esc 都关；Esc 把焦点还给触发元素。
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ChevronRight } from 'lucide-vue-next'
import { overlayTarget } from '../../composables/overlayTarget'
import { closeContextMenu, menuState as menu } from '../../composables/contextMenu'

const panelEl = ref(/** @type {HTMLElement | null} */ (null))
const subEl = ref(/** @type {HTMLElement | null} */ (null))
const pos = ref({ left: '0px', top: '0px' })
const subPos = ref({ left: '0px', top: '0px' })
const submenu = ref({
  open: false,
  items: /** @type {any[]} */ ([]),
  parentKey: '',
})
let bound = false

/** @param {any} item */
function hasSub(item) {
  return Boolean(item.children?.length)
}

function place() {
  const el = panelEl.value
  if (!el) return
  const pad = 8
  const width = el.offsetWidth
  const height = el.offsetHeight
  let left = menu.x
  let top = menu.y
  const anchor = menu.anchor
  if (anchor?.isConnected) {
    const box = anchor.getBoundingClientRect()
    left = box.right - width
    top = box.bottom + 6
    if (top + height > window.innerHeight - pad) top = box.top - height - 6
  } else {
    left = menu.x + 2
    top = menu.y + 2
    if (left + width > window.innerWidth - pad) left = menu.x - width - 2
    if (top + height > window.innerHeight - pad) top = menu.y - height - 2
  }
  left = Math.min(Math.max(pad, left), window.innerWidth - width - pad)
  top = Math.min(Math.max(pad, top), window.innerHeight - height - pad)
  pos.value = { left: `${left}px`, top: `${top}px` }
}

/** @param {HTMLElement | null} [panel] */
function focusables(panel = panelEl.value) {
  return Array.from(
    panel?.querySelectorAll('[role="menuitem"]:not(:disabled)') || []
  ).filter((el) => el instanceof HTMLElement)
}

/** @param {HTMLElement | null} panel */
function focusFirst(panel) {
  const first = focusables(panel)[0]
  if (first instanceof HTMLElement) first.focus({ preventScroll: true })
  else panel?.focus({ preventScroll: true })
}

/** 子菜单贴着这一项的右缘；右边放不下翻到左边，上下夹在视口里。 */
/** @param {any} item @param {HTMLElement} el @param {boolean} focus */
function openSub(item, el, focus) {
  if (!hasSub(item)) return
  const box = el.getBoundingClientRect()
  submenu.value = { open: true, items: item.children, parentKey: item.key }
  void nextTick(() => {
    const node = subEl.value
    if (!node) return
    const width = node.offsetWidth
    const height = node.offsetHeight
    let left = box.right + 4
    if (left + width > window.innerWidth - 8) left = box.left - width - 4
    left = Math.max(8, left)
    let top = box.top - 4
    top = Math.min(Math.max(8, top), window.innerHeight - height - 8)
    subPos.value = { left: `${left}px`, top: `${top}px` }
    if (focus) focusFirst(node)
  })
}

/** @param {boolean} focusParent */
function closeSub(focusParent) {
  const key = submenu.value.parentKey
  if (!submenu.value.open) return
  submenu.value = { open: false, items: [], parentKey: '' }
  if (!focusParent) return
  const parent = Array.from(panelEl.value?.querySelectorAll('.ctx-item') || []).find(
    (el) => el instanceof HTMLElement && el.dataset.key === key
  )
  if (parent instanceof HTMLElement) parent.focus({ preventScroll: true })
}

/** @param {any} item @param {MouseEvent} event */
function onItemEnter(item, event) {
  if (!(event.currentTarget instanceof HTMLElement)) return
  if (hasSub(item)) {
    if (!submenu.value.open || submenu.value.parentKey !== item.key) {
      openSub(item, event.currentTarget, false)
    }
    return
  }
  closeSub(false)
}

/** @param {any} item @param {MouseEvent} event */
function onItemClick(item, event) {
  if (item.disabled) return
  if (hasSub(item)) {
    openSub(item, /** @type {HTMLElement} */ (event.currentTarget), true)
    return
  }
  run(item)
}

/** @param {string} key */
function itemByKey(key) {
  for (const group of menu.groups) {
    const found = group.find((item) => item.key === key)
    if (found) return found
  }
  return submenu.value.items.find((item) => item.key === key) || null
}

/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeContextMenu({ restoreFocus: true })
    return
  }
  if (event.key === 'Tab') {
    event.preventDefault()
    closeContextMenu({ restoreFocus: true })
    return
  }
  const active = document.activeElement
  const panel =
    active instanceof HTMLElement && active.closest('.ctx-menu')
      ? /** @type {HTMLElement} */ (active.closest('.ctx-menu'))
      : panelEl.value
  if (event.key === 'ArrowRight') {
    if (!(active instanceof HTMLElement) || !panelEl.value?.contains(active)) return
    const item = itemByKey(active.dataset.key || '')
    if (!item || !hasSub(item)) return
    event.preventDefault()
    openSub(item, active, true)
    return
  }
  if (event.key === 'ArrowLeft') {
    if (!(active instanceof HTMLElement) || !subEl.value?.contains(active)) return
    event.preventDefault()
    closeSub(true)
    return
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  const list = focusables(panel)
  if (!list.length) return
  event.preventDefault()
  const at = list.indexOf(/** @type {HTMLElement} */ (document.activeElement))
  let next = 0
  if (event.key === 'ArrowDown') next = at < 0 ? 0 : (at + 1) % list.length
  else if (event.key === 'ArrowUp') next = at <= 0 ? list.length - 1 : at - 1
  else if (event.key === 'End') next = list.length - 1
  const target = list[next]
  target.focus({ preventScroll: true })
  if (submenu.value.open && target.dataset.key !== submenu.value.parentKey) {
    closeSub(false)
  }
}

/** @param {MouseEvent} event */
function onOutside(event) {
  const target = event.target
  if (!(target instanceof Node)) return
  if (panelEl.value?.contains(target)) return
  if (subEl.value?.contains(target)) return
  // 只有锚点模式（行尾「⋯」）豁免触发元素：再点一次按钮由它自己收起。
  // 光标模式的触发元素是整行整卡，点行内任意处都该把菜单收掉。
  if (menu.anchor && menu.trigger?.contains(target)) return
  closeContextMenu()
}

/** @param {Event} event */
function onScroll(event) {
  const target = event.target
  if (target instanceof Node && panelEl.value?.contains(target)) {
    closeSub(false)
    return
  }
  if (target instanceof Node && subEl.value?.contains(target)) return
  closeContextMenu()
}

function onResize() {
  closeContextMenu()
}

function bind() {
  if (bound) return
  bound = true
  window.addEventListener('mousedown', onOutside, true)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', onResize)
  window.addEventListener('blur', onResize)
}

function unbind() {
  if (!bound) return
  bound = false
  window.removeEventListener('mousedown', onOutside, true)
  window.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('blur', onResize)
}

async function afterOpen() {
  await nextTick()
  place()
  focusFirst(panelEl.value)
}

/**
 * @param {any} item
 */
function run(item) {
  if (item.disabled) return
  closeContextMenu()
  void item.run?.()
}

watch(
  () => [menu.open, menu.session],
  ([open]) => {
    closeSub(false)
    if (!open) {
      unbind()
      return
    }
    bind()
    void afterOpen()
  },
  { flush: 'post' }
)

onBeforeUnmount(unbind)
</script>

<style scoped>
.ctx-menu {
  position: fixed;
  z-index: 80;
  min-width: 176px;
  max-width: 280px;
  max-height: 72vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 4px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-pop);
}

.ctx-sub {
  z-index: 81;
  min-width: 150px;
}

.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 30px;
  padding: 0 8px;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.ctx-item:hover:not(:disabled),
.ctx-item:focus-visible,
.ctx-item.is-open {
  outline: none;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.ctx-item:disabled {
  opacity: 0.45;
  cursor: default;
}

.ctx-item.is-danger {
  color: var(--sh-danger);
}

.ctx-item.is-danger:hover:not(:disabled),
.ctx-item.is-danger:focus-visible {
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
}

.ctx-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ctx-caret {
  flex: none;
  margin-left: auto;
  color: var(--sh-text-4);
}

.ctx-sep {
  height: 1px;
  margin: 4px 6px;
  background: var(--sh-line-soft);
}

.ctxmenu-enter-active,
.ctxmenu-leave-active {
  transition: opacity 120ms var(--sh-ease), transform 120ms var(--sh-ease);
}

.ctxmenu-enter-from,
.ctxmenu-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}
</style>