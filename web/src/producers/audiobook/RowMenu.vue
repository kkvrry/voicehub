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
  </span>
</template>

<script setup>
// @ts-check
// 行尾「⋯」只是共享菜单的一个锚点：菜单内容与右键完全同一份。
import { computed, ref } from 'vue'
import { Ellipsis } from 'lucide-vue-next'
import {
  closeContextMenu,
  menuState,
  openObjectMenu,
} from '../../composables/contextMenu'

const props = defineProps({
  disabled: { type: Boolean, default: false },
  type: { type: String, required: true },
  context: { type: Object, default: null },
})

const trigger = ref(/** @type {HTMLElement | null} */ (null))
const open = computed(() => menuState.open && menuState.trigger === trigger.value)

function toggle() {
  if (props.disabled) return
  if (open.value) {
    closeContextMenu({ restoreFocus: true })
    return
  }
  openObjectMenu(props.type, props.context || {}, { anchor: trigger.value })
}
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
</style>
