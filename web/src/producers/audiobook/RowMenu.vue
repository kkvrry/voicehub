<template>
  <span class="row-menu">
    <button
      ref="trigger"
      type="button"
      class="row-more"
      :disabled="disabled"
      title="更多操作"
      aria-label="更多操作"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click.stop="toggle"
    >
      <Ellipsis :size="15" :stroke-width="2" aria-hidden="true" />
    </button>

    <!-- 菜单挂在弹层容器上：表格自己会滚，绝对定位在单元格里会被裁掉。 -->
    <Teleport :to="overlayTarget">
      <Transition name="rowmenu">
        <div v-if="open" class="row-menu-pop" :style="pos" role="menu" @click.stop>
          <button type="button" class="row-menu-item" role="menuitem" @click="pick('regenerate')">
            <RefreshCw :size="14" :stroke-width="1.8" aria-hidden="true" />
            重新生成
          </button>
          <button
            type="button"
            class="row-menu-item is-danger"
            role="menuitem"
            @click="pick('remove')"
          >
            <Trash2 :size="14" :stroke-width="1.8" aria-hidden="true" />
            删除这一行
          </button>
        </div>
      </Transition>
    </Teleport>
  </span>
</template>

<script setup>
// @ts-check
import { onBeforeUnmount, ref } from 'vue'
import { Ellipsis, RefreshCw, Trash2 } from 'lucide-vue-next'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['regenerate', 'remove'])

const trigger = ref(/** @type {HTMLElement | null} */ (null))
const open = ref(false)
const pos = ref({})

/** 贴着按钮右下角展开；贴到窗口底边就翻到上方。 */
function place() {
  const box = trigger.value?.getBoundingClientRect()
  if (!box) return
  const width = 172
  const height = 70
  const left = Math.min(Math.max(8, box.right - width), window.innerWidth - width - 8)
  const below = box.bottom + 6
  const top = below + height > window.innerHeight - 8 ? box.top - height - 6 : below
  pos.value = { left: `${left}px`, top: `${Math.max(8, top)}px`, width: `${width}px` }
}

function close() {
  open.value = false
  document.removeEventListener('mousedown', onOutside, true)
  document.removeEventListener('keydown', onKey, true)
  window.removeEventListener('scroll', close, true)
  window.removeEventListener('resize', close)
}

/** @param {MouseEvent} event */
function onOutside(event) {
  const target = /** @type {HTMLElement | null} */ (event.target)
  if (!target?.closest('.row-menu-pop, .row-more')) close()
}

/** @param {KeyboardEvent} event */
function onKey(event) {
  if (event.key === 'Escape') close()
}

function toggle() {
  if (props.disabled) return
  if (open.value) {
    close()
    return
  }
  place()
  open.value = true
  document.addEventListener('mousedown', onOutside, true)
  document.addEventListener('keydown', onKey, true)
  window.addEventListener('scroll', close, true)
  window.addEventListener('resize', close)
}

/** @param {'regenerate'|'remove'} action */
function pick(action) {
  close()
  emit(action)
}

onBeforeUnmount(close)
</script>

<style scoped>
.row-menu {
  margin-left: auto;
  display: flex;
}
/* 三点键比播放键更轻：没有底色，悬停才浮出来 */
.row-more {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}
.row-more:hover:not(:disabled) {
  background: var(--sh-chip);
  color: var(--sh-text-2);
}
.row-more:disabled {
  opacity: 0.4;
  cursor: default;
}
.row-menu-pop {
  position: fixed;
  z-index: 30;
  display: flex;
  flex-direction: column;
  padding: 4px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-pop);
}
.row-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
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
.row-menu-item:hover {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.row-menu-item.is-danger {
  color: var(--sh-danger);
}
.row-menu-item.is-danger:hover {
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
}
.rowmenu-enter-active,
.rowmenu-leave-active {
  transition: opacity 120ms var(--sh-ease), transform 120ms var(--sh-ease);
}
.rowmenu-enter-from,
.rowmenu-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}
</style>
