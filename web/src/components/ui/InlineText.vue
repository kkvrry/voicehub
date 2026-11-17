<template>
  <textarea
    v-if="multiline"
    ref="el"
    class="vinline is-multiline"
    :class="{ 'is-editing': editing }"
    :value="modelValue"
    :placeholder="placeholder"
    :aria-label="ariaLabel"
    :maxlength="maxlength"
    rows="1"
    @focus="onFocus"
    @blur="onBlur"
    @input="onInput"
  ></textarea>
  <input
    v-else
    ref="el"
    class="vinline"
    :class="{ 'is-editing': editing }"
    type="text"
    :value="modelValue"
    :placeholder="placeholder"
    :aria-label="ariaLabel"
    :maxlength="maxlength"
    @focus="onFocus"
    @blur="onBlur"
    @input="onInput"
    @keydown.enter.prevent="el?.blur()"
    @keydown.esc.prevent="reset"
  />
</template>

<script setup>
// @ts-check
/** 就地可编辑的一段字：**没有输入框的样子**。
 *
 *  样式上它和正文一模一样（无边框、无底色、行高与段落一致），悬停才浮出一层很淡的底，
 *  聚焦时给一圈柔和光环——"这里能改"是在交互里说的，不是靠一个常驻的方框喊出来。
 *  参考界面的描述与标签都是这个路子：读的时候是段落，改的时候才是表单。
 *
 *  多行时按内容长高（`scrollHeight`），所以空着只剩一行、写满了自己撑开，不需要拖手柄。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '点击输入' },
  /** 段落（多行、自动长高）还是单行。 */
  multiline: { type: Boolean, default: false },
  ariaLabel: { type: String, default: '' },
  maxlength: { type: Number, default: 200 },
})
const emit = defineEmits(['change', 'done'])

const el = ref(/** @type {any} */ (null))
const draft = ref(props.modelValue)
/** 正在改这一格——样式挂在**状态类**上而不是 `:focus` 上：
 *  弹层里这一小块拿不到 `:focus` 的计算样式（实测 `matches(':focus')` 为真而样式不落地），
 *  而"亮起来"是给人看的，不能赌伪类。 */
const editing = ref(false)
/** 停手一会儿就存：多行段落不一定有"失焦"这个时刻（切页、Esc、鼠标点走都可能不触发），
 *  拖着不存等于让用户白打一段字。600ms 是"打完一句"的量级，不会每敲一个字写一次库。 */
let idle = 0

/** 让高度贴着内容：先归零再量，否则删字之后不会缩回去。 */
function resize() {
  const node = el.value
  if (!props.multiline || !node) return
  node.style.height = 'auto'
  node.style.height = `${node.scrollHeight}px`
}

/**
 * @param {any} event
 */
function onInput(event) {
  draft.value = event.target.value
  resize()
  window.clearTimeout(idle)
  idle = window.setTimeout(commit, 600)
}

/** 提交：值真变了才发（父组件那边还会再比一次，两次都比是为了不让空改动写库）。 */
function commit() {
  window.clearTimeout(idle)
  const value = (el.value?.value ?? '').trim()
  if (value !== props.modelValue) emit('change', value)
  resize()
}

function onFocus() {
  editing.value = true
}

function onBlur() {
  editing.value = false
  commit()
  // 不管值有没有变都说一声：外面靠它退出就地编辑的状态。
  emit('done')
}

/** 让外面能把光标放进来（点击标题那一瞬间）。 */
function focus() {
  const node = el.value
  if (!node) return
  node.focus()
  node.select?.()
}
defineExpose({ focus })

/** Esc 放弃这一轮的改动（单行用得上；多行留给浏览器的默认行为之外的选择）。 */
function reset() {
  if (el.value) el.value.value = props.modelValue
  draft.value = props.modelValue
}

onMounted(resize)
onBeforeUnmount(() => window.clearTimeout(idle))
watch(
  () => props.modelValue,
  (value) => {
    // 正在打字时**不回写** DOM：存一次就回写一次的话，回来的旧值会把刚敲的字盖掉。
    const node = el.value
    if (node && document.activeElement !== node && node.value !== value) node.value = value
    resize()
  }
)
</script>
