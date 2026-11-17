<template>
  <!-- 勾选里不支持这一项的引擎：沿用语气词那排禁用标签，名字本身就说清了是谁。 -->
  <span
    v-for="name in names"
    :key="String(name)"
    class="marks-off-tag"
    role="note"
    :title="`${name} 不支持${feature}，${effect}`"
    :aria-label="`${name} 不支持${feature}`"
  >
    <Ban :size="11" :stroke-width="2" aria-hidden="true" />
    {{ name }}
  </span>
</template>

<script setup>
// @ts-check
import { Ban } from 'lucide-vue-next'

defineProps({
  /** 不支持这一项的引擎名（已从勾选引擎里筛好）。 */
  names: { type: Array, default: () => [] },
  /** 这一项叫什么：语气词 / 情绪 / 引导指令。 */
  feature: { type: String, default: '' },
  /** 悬停里补一句"它会怎样"，默认 = 不会生效。 */
  effect: { type: String, default: '这一项不会生效' },
})
</script>
