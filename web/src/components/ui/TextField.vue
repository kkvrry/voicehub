<template>
  <div class="tf" :class="{ 'is-disabled': disabled }">
    <span v-if="$slots.prefix" class="tf-prefix"><slot name="prefix" /></span>
    <input
      :id="id"
      class="sh-field"
      :class="{ 'is-mono': mono }"
      :value="modelValue"
      :type="type"
      :placeholder="placeholder"
      :readonly="readonly"
      :disabled="disabled"
      :aria-label="label"
      :autocomplete="autocomplete"
      @input="onInput"
      @keydown.enter="$emit('enter')"
    />
    <button
      v-if="clearable && modelValue && !readonly && !disabled"
      type="button"
      class="tf-clear"
      :aria-label="`清空${label}`"
      @click="clear"
    >
      <X :size="14" :stroke-width="1.8" />
    </button>
    <span v-if="suffix" class="tf-suffix">{{ suffix }}</span>
  </div>
</template>

<script setup lang="ts">
import { X } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    modelValue: string
    label: string
    placeholder?: string
    type?: string
    mono?: boolean
    readonly?: boolean
    disabled?: boolean
    clearable?: boolean
    suffix?: string
    id?: string
    autocomplete?: string
  }>(),
  {
    placeholder: '',
    type: 'text',
    mono: false,
    readonly: false,
    disabled: false,
    clearable: false,
    suffix: '',
    id: undefined,
    autocomplete: 'off',
  }
)

const emit = defineEmits<{ 'update:modelValue': [value: string]; enter: [] }>()

function onInput(event: Event) {
  emit('update:modelValue', (event.target as HTMLInputElement).value)
}

function clear() {
  emit('update:modelValue', '')
}
</script>

<style scoped>
.tf {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  width: 100%;
}
.tf .sh-field {
  flex: 1;
  min-width: 0;
}
.tf-prefix {
  flex: none;
  display: grid;
  place-items: center;
  color: var(--sh-text-4);
}
.tf-suffix {
  flex: none;
  font-size: 11.6px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.tf-clear {
  position: absolute;
  right: 7px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
  transition: color 130ms, background 130ms;
}
.tf-clear:hover {
  color: var(--sh-text-2);
  background: var(--sh-hover);
}
.tf-clear + .tf-suffix,
.tf .sh-field {
  padding-right: 10px;
}
.tf:has(.tf-clear) .sh-field {
  padding-right: 32px;
}
</style>
