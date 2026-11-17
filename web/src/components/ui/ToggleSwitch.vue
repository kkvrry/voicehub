<template>
  <button
    type="button"
    class="sh-toggle"
    role="switch"
    :aria-checked="modelValue"
    :aria-label="label"
    :aria-describedby="describedby"
    :disabled="disabled"
    :title="title || label"
    @click="toggle"
  />
</template>

<script setup lang="ts">
const props = withDefaults(
  defineProps<{
    modelValue: boolean
    label: string
    disabled?: boolean
    describedby?: string
    title?: string
  }>(),
  { disabled: false, describedby: undefined, title: '' }
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function toggle() {
  if (props.disabled) return
  emit('update:modelValue', !props.modelValue)
}
</script>
