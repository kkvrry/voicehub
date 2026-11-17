<template>
  <div v-if="error" class="ai-suggest is-error" role="status">
    <TriangleAlert :size="14" :stroke-width="1.8" class="ai-suggest-icon" />
    <span class="ai-suggest-text">{{ error }}</span>
    <span class="ai-suggest-spacer" />
    <button type="button" class="sh-btn is-sm is-ghost" @click="$emit('retry')">重试</button>
    <button
      type="button"
      class="sh-btn is-icon ai-suggest-close"
      title="关闭"
      aria-label="关闭 AI 提示"
      @click="$emit('dismiss')"
    >
      <X :size="14" :stroke-width="1.8" />
    </button>
  </div>
  <div v-else-if="suggestion" class="ai-suggest" :class="{ 'is-warn': hasWarning }" role="status">
    <Sparkles :size="14" :stroke-width="1.8" class="ai-suggest-icon" />
    <span class="ai-suggest-text">
      <b>AI 建议</b>
      <template v-if="suggestion.explanation"> · {{ suggestion.explanation }}</template>
      <span class="sh-chip" :class="{ 'is-warn': !count }">{{ countText }}</span>
    </span>
    <span v-if="stale" class="ai-suggest-note">规则已修改，命中数按生成时的规则计算。</span>
    <span v-else-if="warningsText" class="ai-suggest-note is-warn" :title="warningsText">
      {{ warningsText }}
    </span>
    <span class="ai-suggest-spacer" />
    <button type="button" class="sh-btn is-sm is-ghost" @click="$emit('undo')">撤销</button>
    <button
      type="button"
      class="sh-btn is-icon ai-suggest-close"
      title="关闭"
      aria-label="关闭 AI 提示"
      @click="$emit('dismiss')"
    >
      <X :size="14" :stroke-width="1.8" />
    </button>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { Sparkles, TriangleAlert, X } from 'lucide-vue-next'

const props = defineProps({
  suggestion: { type: Object, default: null },
  error: { type: String, default: '' },
  stale: { type: Boolean, default: false },
})
defineEmits(['undo', 'dismiss', 'retry'])

const count = computed(() => Number(props.suggestion?.preview?.total) || 0)
const countText = computed(() => {
  if (!count.value) return '当前正文没有命中'
  return props.suggestion?.preview?.truncated ? `命中 ${count.value}+ 处` : `命中 ${count.value} 处`
})
const warnings = computed(() => props.suggestion?.preview?.warnings || [])
const warningsText = computed(() => warnings.value[0] || '')
const hasWarning = computed(() => warnings.value.length > 0 || !count.value)
</script>

<style scoped>
.ai-suggest {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 8px;
  padding: 6px 8px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-accent-soft);
  font-size: 11px;
  line-height: 1.5;
  color: var(--sh-text-2);
}
.ai-suggest.is-warn {
  border-color: color-mix(in srgb, var(--sh-warn) 35%, var(--sh-line-soft));
  background: var(--sh-warn-soft);
}
.ai-suggest.is-error {
  border-color: color-mix(in srgb, var(--sh-warn) 45%, var(--sh-line-soft));
  background: var(--sh-warn-soft);
  color: var(--sh-warn);
}
.ai-suggest-icon {
  flex: none;
  color: var(--sh-accent);
}
.ai-suggest.is-warn .ai-suggest-icon,
.ai-suggest.is-error .ai-suggest-icon {
  color: var(--sh-warn);
}
.ai-suggest-text {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.ai-suggest-text b {
  font-weight: 650;
  color: var(--sh-text);
}
.ai-suggest-note {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sh-text-4);
}
.ai-suggest-note.is-warn {
  color: var(--sh-warn);
}
.ai-suggest-spacer {
  flex: 1;
}
.ai-suggest-close {
  width: 24px;
  height: 24px;
  border: 0;
  background: transparent;
  color: var(--sh-text-4);
}
.ai-suggest-close:hover {
  color: var(--sh-accent);
}
</style>
