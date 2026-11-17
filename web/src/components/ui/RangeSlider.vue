<template>
  <div class="sh-slider" :class="{ 'is-disabled': disabled }">
    <input
      type="range"
      :value="modelValue"
      :min="min"
      :max="max"
      :step="step"
      :disabled="disabled"
      :aria-label="label"
      :aria-valuetext="`${modelValue}${unit}`"
      :style="{ '--sh-p': `${percent}%` }"
      @input="onInput"
    />
    <span class="sh-slider-value">{{ modelValue }}{{ unit }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: number
    min: number
    max: number
    step?: number
    label: string
    unit?: string
    disabled?: boolean
  }>(),
  { step: 1, unit: '', disabled: false }
)

const emit = defineEmits<{ 'update:modelValue': [value: number] }>()

const percent = computed(() => {
  const span = props.max - props.min
  if (span <= 0) return 0
  return Math.min(100, Math.max(0, ((props.modelValue - props.min) / span) * 100))
})

function onInput(event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (!Number.isNaN(value)) emit('update:modelValue', value)
}
</script>

<style scoped>
.sh-slider.is-disabled {
  opacity: 0.6;
}
</style>
