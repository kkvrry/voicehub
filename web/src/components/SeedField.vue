<template>
  <!-- 种子：默认显示 -1。两颗按钮分工明确——
       「随机」填 -1（每次由引擎抽，结果不可复现）；「固定」当场抽一个数（可复现）。 -->
  <div class="seed-row">
    <input
      class="field-input"
      type="number"
      :value="display"
      min="-1"
      max="2147483647"
      step="1"
      aria-label="种子"
      @change="onChange"
    />
    <button type="button" class="field-btn" title="每次随机（发 -1，由引擎抽取）" @click="randomMode">
      随机
    </button>
    <button type="button" class="field-btn" title="抽一个固定种子，这一条可以复现" @click="fixed">
      固定
    </button>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'

const props = defineProps({ modelValue: { type: [Number, String], default: '' } })
const emit = defineEmits(['update'])

/** 没填过也显示 -1：它读作"随机"，不改值时这一格不进请求。 */
const display = computed(() =>
  props.modelValue === '' || props.modelValue === null ? -1 : props.modelValue
)

/** @param {Event} event */
function onChange(event) {
  const raw = /** @type {HTMLInputElement} */ (event.target).value
  if (raw === '') {
    emit('update', '')
    return
  }
  const value = Number(raw)
  emit('update', Number.isFinite(value) ? value : '')
}

function randomMode() {
  emit('update', -1)
}

function fixed() {
  emit('update', Math.floor(Math.random() * 2147483647))
}
</script>
