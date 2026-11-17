<template>
  <!-- 情绪：候选词铺开成标签（与语气词同一套 chip），再点一次取消；
       最后一个虚线框是"自己打一个"。候选不是白名单（规范里就是 combo）。 -->
  <div class="tag-pick">
    <button
      v-for="[value, label] in pairs"
      :key="value"
      type="button"
      class="mark-chip"
      :class="{ on: modelValue === value }"
      @click="pick(value)"
    >
      {{ label }}
    </button>
    <input
      class="tag-input"
      :value="draft"
      type="text"
      placeholder="自定义…"
      aria-label="自定义情绪"
      @input="onInput"
      @keydown.enter.prevent="commit"
      @blur="commit"
    />
  </div>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  /** 规范给的候选：``[[值, 说法], …]``（第一项是空值 = 不指定）。 */
  options: { type: Array, default: () => [] },
})
const emit = defineEmits(['update'])

// Array 型 prop 推出来是 unknown[]；这里按规范给的形状收窄一次。
const pairs = computed(() => /** @type {Array<[string, string]>} */ (props.options))

const isPreset = computed(() => pairs.value.some(([value]) => value === props.modelValue))
/** 手输框只装"候选之外"的那个词：选中候选时它保持空白。 */
const draft = ref(isPreset.value ? '' : props.modelValue)

watch(
  () => props.modelValue,
  (value) => {
    draft.value = pairs.value.some(([item]) => item === value) ? '' : value
  }
)

/** @param {Event} event */
function onInput(event) {
  draft.value = /** @type {HTMLInputElement} */ (event.target).value
}

/**
 * 点同一颗 = 取消（回到"不指定"）。
 * @param {string} value
 */
function pick(value) {
  emit('update', props.modelValue === value ? '' : value)
}

function commit() {
  const text = draft.value.trim()
  if (text === props.modelValue) return
  emit('update', text)
}
</script>
