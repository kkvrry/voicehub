<template>
  <!-- 弹层挂进 .app-shell（不是 body）：`--panel` / `--border` / `--accent-soft` 等
       旧变量名定义在 .app-shell 上，挂在 body 上整块弹层会丢背景与配色。
       挂在 shell 里同样不会被卡片的 hover 位移和 overflow 裁掉——它是卡片的兄弟节点，
       而 shell 自身（fixed + inset:0、无 transform）不裁剪 fixed 子元素。 -->
  <Teleport :to="overlayTarget">
    <div v-if="open" class="vpop-scrim" @click="$emit('close')"></div>
    <div v-if="open" ref="panelEl" class="vpop" :style="pos" role="dialog" :aria-label="label">
      <slot />
    </div>
  </Teleport>
</template>

<script setup>
// @ts-check
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** 贴在哪个元素下面。传元素本身（`ref.value`），不是组件实例。 */
  anchor: { type: Object, default: null },
  width: { type: Number, default: 248 },
  label: { type: String, default: '选择' },
})
const emit = defineEmits(['close'])

onMounted(() => {
  // 懒挂载的用法（GenderPicker 那类：`v-if="open"` 才挂组件）走不到 watch——
  // 挂上来的时候 open 已经是 true。挂载即摆好，两边的用法都成立。
  if (props.open) {
    bind()
    void nextTick(place)
  }
})

const panelEl = ref(/** @type {HTMLElement | null} */ (null))
const pos = ref({})

/** 贴在锚点下方；下方放不下就翻到上方。左右夹在视口内，
 *  免得卡片贴着右边缘时弹层被切掉一半。 */
function place() {
  const el = props.anchor
  if (!el || !el.getBoundingClientRect) return
  const rect = el.getBoundingClientRect()
  const gap = 6
  const width = props.width
  const left = Math.max(10, Math.min(rect.left, window.innerWidth - width - 10))
  const height = panelEl.value?.offsetHeight || 240
  const below = rect.bottom + gap
  const top = below + height > window.innerHeight - 10 ? Math.max(10, rect.top - height - gap) : below
  pos.value = { left: `${left}px`, top: `${top}px`, width: `${width}px` }
}

/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
}

function bind() {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', place)
  window.addEventListener('scroll', place, true)
}

function unbind() {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', place)
  window.removeEventListener('scroll', place, true)
}

watch(
  () => props.open,
  async (open) => {
    if (!open) {
      unbind()
      return
    }
    bind()
    // 先按估算高度摆一次，量到真实高度后再摆一次（否则"翻到上方"会算错）。
    await nextTick()
    place()
  }
)

onBeforeUnmount(unbind)
</script>
