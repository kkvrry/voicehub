<template>
  <!-- 头像来源单选：具体站点由后端下发，一次只查一个。
       选项本身是站点名，收哪类角色写在悬停提示里。 -->
  <div class="asc" role="radiogroup" :aria-label="label">
    <button
      v-for="item in sources"
      :key="item.key"
      type="button"
      class="sh-btn is-ghost asc-chip"
      :class="{ 'is-on': item.key === modelValue }"
      role="radio"
      :aria-checked="item.key === modelValue"
      :title="item.hint || item.label"
      @click="$emit('update:modelValue', item.key)"
    >
      {{ item.label }}
    </button>
    <p v-if="!sources.length" class="asc-empty">正在读取来源…</p>
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    modelValue: string
    sources: { key: string; label: string; hint?: string }[]
    label?: string
  }>(),
  { label: '来源' }
)

defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<style scoped>
.asc {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.asc-chip {
  height: 27px;
  padding: 0 11px;
  border-radius: 999px;
  font-size: 12px;
}
.asc-chip.is-on {
  color: var(--sh-accent);
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.asc-empty {
  margin: 0;
  font-size: 12px;
  color: var(--sh-text-4);
}
</style>
