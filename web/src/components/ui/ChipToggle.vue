<!-- 复选标签：日志级别、标签筛选这类「多选、就地切换」的地方用它。
     原生 checkbox 在浅色玻璃底上像另一个时代的控件；这里保留键盘与读屏语义
     （role=checkbox + 空格/回车），只是把外观换成同一套圆角与浅蓝高亮。 -->
<template>
  <button
    type="button"
    class="chip-toggle"
    :class="[`is-${tone}`, { 'is-on': modelValue }]"
    role="checkbox"
    :aria-checked="modelValue"
    :disabled="disabled"
    :title="title || label"
    @click="toggle"
  >
    <Check v-if="modelValue" :size="12" :stroke-width="1.8" aria-hidden="true" />
    <span><slot>{{ label }}</slot></span>
    <span v-if="count !== undefined" class="chip-toggle-count">{{ count }}</span>
  </button>
</template>

<script setup lang="ts">
import { Check } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    label: string
    count?: number
    tone?: 'neutral' | 'warn' | 'danger' | 'ok'
    disabled?: boolean
    title?: string
  }>(),
  { count: undefined, tone: 'neutral', disabled: false, title: '' }
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function toggle() {
  if (props.disabled) return
  emit('update:modelValue', !props.modelValue)
}
</script>

<style scoped>
.chip-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--sh-line);
  background: var(--sh-glass-soft);
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11.8px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  transition: color var(--sh-t-fast) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}
.chip-toggle:hover:not(:disabled) {
  color: var(--sh-text-2);
  border-color: var(--sh-accent);
}
.chip-toggle:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 2px;
}
.chip-toggle.is-on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  border-color: transparent;
  box-shadow: inset 0 0 0 1px var(--sh-accent-ring);
}
.chip-toggle.is-on.is-warn {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
  box-shadow: inset 0 0 0 1px rgba(192, 122, 29, 0.3);
}
.chip-toggle.is-on.is-danger {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
  box-shadow: inset 0 0 0 1px rgba(217, 87, 75, 0.3);
}
.chip-toggle.is-on.is-ok {
  color: var(--sh-ok);
  background: var(--sh-ok-soft);
  box-shadow: inset 0 0 0 1px rgba(14, 157, 116, 0.3);
}
.chip-toggle:disabled {
  opacity: 0.45;
  cursor: default;
}
.chip-toggle-count {
  font-size: 10.5px;
  font-weight: 500;
  color: var(--sh-text-4);
}
.chip-toggle.is-on .chip-toggle-count {
  color: currentColor;
  opacity: 0.75;
}
</style>
