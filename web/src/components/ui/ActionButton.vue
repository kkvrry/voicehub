<template>
  <button
    :type="type"
    class="sh-btn"
    :class="[`is-${variant}`, { 'is-sm': size === 'sm', 'is-block': block, 'is-loading': loading }]"
    :disabled="disabled || loading"
    :title="title"
    :aria-busy="loading"
    @click="$emit('click', $event)"
  >
    <Loader2 v-if="loading" class="sh-spin" :size="size === 'sm' ? 13 : 14" :stroke-width="1.8" aria-hidden="true" />
    <slot />
  </button>
</template>

<script setup lang="ts">
import { Loader2 } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    variant?: 'primary' | 'soft' | 'ghost' | 'danger' | 'icon'
    size?: 'sm' | 'md'
    type?: 'button' | 'submit'
    loading?: boolean
    disabled?: boolean
    block?: boolean
    title?: string
  }>(),
  {
    variant: 'ghost',
    size: 'md',
    type: 'button',
    loading: false,
    disabled: false,
    block: false,
    title: '',
  }
)

defineEmits<{ click: [event: MouseEvent] }>()
</script>

<style scoped>
.sh-btn.is-block {
  display: flex;
  width: 100%;
}
.sh-btn.is-loading {
  cursor: progress;
}
</style>
