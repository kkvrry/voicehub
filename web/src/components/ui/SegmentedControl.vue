<template>
  <div
    class="sh-seg"
    :class="{ 'is-block': block }"
    role="radiogroup"
    :aria-label="label"
    @keydown="onKeydown"
  >
    <button
      v-for="option in options"
      :key="String(option.value)"
      type="button"
      class="sh-seg-btn"
      role="radio"
      :aria-checked="option.value === modelValue"
      :tabindex="option.value === modelValue ? 0 : -1"
      :disabled="option.disabled"
      :title="option.title || option.label"
      @click="pick(option)"
    >
      <component :is="option.icon" v-if="option.icon" :size="14" :stroke-width="1.8" aria-hidden="true" />
      <span>{{ option.label }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import type { SegmentOption } from './types'

const props = withDefaults(
  defineProps<{
    modelValue: string | number
    options: SegmentOption[]
    label: string
    block?: boolean
  }>(),
  { block: false }
)

const emit = defineEmits<{ 'update:modelValue': [value: string | number] }>()

function pick(option: SegmentOption) {
  if (option.disabled || option.value === props.modelValue) return
  emit('update:modelValue', option.value)
}

/** 单选组的方向键约定：← → 在选项间移动并立即选中（WAI-ARIA radiogroup）。 */
function onKeydown(event: KeyboardEvent) {
  const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']
  if (!keys.includes(event.key)) return
  const enabled = props.options.filter((option) => !option.disabled)
  if (enabled.length < 2) return
  event.preventDefault()
  const current = enabled.findIndex((option) => option.value === props.modelValue)
  const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1
  const next = enabled[(current + step + enabled.length) % enabled.length]
  emit('update:modelValue', next.value)
  const group = event.currentTarget as HTMLElement
  const index = props.options.findIndex((option) => option.value === next.value)
  const button = group.querySelectorAll<HTMLButtonElement>('.sh-seg-btn')[index]
  button?.focus()
}
</script>
