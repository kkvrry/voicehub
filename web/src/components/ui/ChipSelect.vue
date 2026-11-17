<template>
  <div class="vchip-field" @click.stop>
    <button
      ref="triggerEl"
      type="button"
      class="vchip"
      :class="[`tone-${currentTone}`, { 'is-blank': !modelValue }]"
      :title="`点击更换${label}`"
      :aria-expanded="open"
      :aria-label="`${label}：${text}`"
      @click.stop="toggle"
    >
      <span v-if="modelValue" class="vchip-dot" aria-hidden="true"></span>
      <span class="vchip-text">{{ text }}</span>
      <span class="vchip-caret" aria-hidden="true">▾</span>
    </button>

    <Popover :open="open" :anchor="triggerEl" :label="label" @close="close">
      <div class="vpop-head">{{ label }}</div>
      <template v-for="section in sections" :key="section.label || '__all__'">
        <div v-if="section.label" class="vpop-sub">{{ section.label }}</div>
        <div class="vpop-grid">
          <button
            v-for="pair in section.options"
            :key="pair[0]"
            type="button"
            class="vchip"
            :class="[`tone-${toneOf(pair[0])}`, { 'is-active': pair[0] === modelValue }]"
            @click="pick(pair[0])"
          >
            {{ pair[1] }}
          </button>
        </div>
      </template>
      <!-- 情绪这类自由文本没有完整词表：留一个输入口，自己打的词也能存下来。 -->
      <form v-if="allowCustom" class="vpop-custom" @submit.prevent="commitCustom">
        <input
          ref="inputEl"
          v-model="draft"
          type="text"
          maxlength="20"
          :placeholder="`自定义${label}，回车确认`"
        />
      </form>
      <button v-if="clearable && modelValue" type="button" class="vpop-clear" @click="pick('')">
        清空{{ label }}
      </button>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
import { computed, nextTick, ref } from 'vue'
import Popover from './Popover.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  /** `[[值, 显示名], …]`，与后端 `/api/v1/tree` 的 vocab 同一形状。 */
  options: { type: Array, default: () => [] },
  /** 分组摆法：`[{ label, options: [[值, 显示名], …] }]`；给了就不看 `options`。 */
  groups: { type: Array, default: () => [] },
  /** 值 → 色调，见 `ui/tones.js`。 */
  toneOf: { type: Function, default: () => 'gray' },
  label: { type: String, default: '选项' },
  /** 未标时按钮上写什么。 */
  placeholder: { type: String, default: '未标' },
  clearable: { type: Boolean, default: true },
  allowCustom: { type: Boolean, default: false },
})
const emit = defineEmits(['change'])

const open = ref(false)
const triggerEl = ref(/** @type {any} */ (null))
const inputEl = ref(/** @type {any} */ (null))
const draft = ref('')

/** 一列或几列：没有分组时就是一列无标题的，模板不必分两种写法。 */
const sections = computed(() =>
  /** @type {any[]} */ (
    props.groups.length ? props.groups : [{ label: '', options: props.options }]
  )
)

const currentTone = computed(() => (props.modelValue ? props.toneOf(props.modelValue) : 'gray'))
const text = computed(() => {
  if (!props.modelValue) return props.placeholder
  const found = /** @type {any[][]} */ (props.options).find(
    (pair) => pair[0] === props.modelValue
  )
  // 词表里没有的值原样显示：旧数据 / 手改过的 sidecar 不该变成空白。
  return found ? found[1] : props.modelValue
})

async function toggle() {
  open.value = !open.value
  if (open.value && props.allowCustom) {
    draft.value = ''
    await nextTick()
    inputEl.value?.focus()
  }
}

function close() {
  open.value = false
}

/**
 * @param {any} value
 */
function pick(value) {
  close()
  if (value !== props.modelValue) emit('change', value)
}

function commitCustom() {
  const value = draft.value.trim()
  draft.value = ''
  if (value) pick(value)
}
</script>
