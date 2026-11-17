<template>
  <div ref="box" class="combo" :class="{ 'combo-wide': wide }">
    <div class="combo-box">
      <input
        class="field-input combo-input"
        :value="value ?? ''"
        :aria-label="field.label"
        :placeholder="placeholder"
        @input="onInput"
        @focus="onFocus"
        @keydown.esc="open = false"
        @keydown.enter.prevent="takeActive"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
      />
      <button
        type="button"
        class="combo-caret"
        :title="`候选（${rows.length} 条）`"
        aria-label="候选"
        @mousedown.prevent
        @click="toggle"
      >
        ▾
      </button>
    </div>
    <!-- 自己画的下拉：候选要"点开就是全部"，原生 `<input list>` 会按框里的值过滤。
         `up`：贴着滚动容器底边往下开会被裁掉，放不下就朝上开。 -->
    <ul v-if="open && rows.length" class="combo-menu" :class="{ up: dropUp }">
      <li v-for="(pair, index) in rows" :key="pair[0]">
        <button
          type="button"
          :class="{ on: pair[0] === (value ?? ''), hot: index === active }"
          @mousedown.prevent="take(pair[0])"
        >
          {{ pair[1] }}
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup>
// @ts-check
/** 候选 + 手输的组合框。与 select 的差别：下拉关不住人，框里照样能自己打——
 *  候选从来不是白名单。值不进组件，改动 emit 出去；空串 = "没要求"。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  field: { type: Object, required: true },
  //: 候选：``[[值, 说法], …]``。**规范自带的 + 选中引擎声明的，已经合好了一份**
  //: （见 store/engine.js 的 choicesOf）——这一层不再分辨候选是谁给的。
  rows: { type: Array, default: () => [] },
  value: { type: [String, Number], default: '' },
  placeholder: { type: String, default: '' },
  //: 单行框在多列网格里只占一格；想让它占一整行时由调用方说。
  wide: { type: Boolean, default: false },
})
const emit = defineEmits(['update'])

/** 键盘走到第几条（-1 = 还没走过）。 */
const active = ref(-1)
const open = ref(false)
/** 下拉朝上开还是朝下开（估计的菜单高度 240px）。 */
const dropUp = ref(false)
const box = ref(/** @type {any} */ (null))

function onFocus() {
  open.value = true
  dropUp.value = needsToFlip()
}

function toggle() {
  open.value = !open.value
  if (open.value) dropUp.value = needsToFlip()
}

/** 头顶还有多少地方比"下面那张菜单要占的地方"更紧，就朝上开。 */
function needsToFlip() {
  const rect = box.value?.getBoundingClientRect()
  if (!rect) return false
  return rect.bottom + 240 > window.innerHeight
}

/**
 * @param {number} step
 */
function move(step) {
  if (!open.value) {
    open.value = true
    dropUp.value = needsToFlip()
  }
  const count = props.rows.length
  if (!count) return
  active.value = (active.value + step + count) % count
}

/** @param {Event} event */
function onInput(event) {
  emit('update', props.field.key, /** @type {HTMLInputElement} */ (event.target).value)
}

/** 回车取键盘走到的那一条；没走过就不猜。 */
function takeActive() {
  const rows = /** @type {any[][]} */ (props.rows)
  if (open.value && active.value >= 0 && rows[active.value]) {
    take(rows[active.value][0])
  }
}

/**
 * @param {any} value
 */
function take(value) {
  emit('update', props.field.key, value)
  open.value = false
  active.value = -1
}

/**
 * 点到别处就收起。不挂 blur：它比菜单那一下 mousedown 先到，
 * "点候选"会变成"先把菜单关掉"，所以监听整个文档、只在外面才关。
 *
 * @param {any} event
 */
function onDocClick(event) {
  if (open.value && box.value && !box.value.contains(event.target)) open.value = false
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>
