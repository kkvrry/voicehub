<template>
  <!-- 隐藏 / 取消隐藏：**卡片与详情共用这一颗**。
       图标说的是"点下去会发生什么"（看得见时给 EyeOff、已隐藏时给 Eye），不是"现在是什么状态"。
       从前卡片上是 emoji（🙈 / 👁）、详情里是图标，同一件事两处长成两个样子，
       最容易被当成两个功能。 -->
  <button
    type="button"
    class="vh-icon hide-btn"
    :title="hidden ? '取消隐藏' : '隐藏（不再出现在候选里）'"
    :aria-label="hidden ? '取消隐藏' : '隐藏'"
    :aria-pressed="hidden"
    @click.stop="$emit('toggle')"
  >
    <Eye v-if="hidden" :size="16" />
    <EyeOff v-else :size="16" />
  </button>
</template>

<script setup>
// @ts-check
import { Eye, EyeOff } from 'lucide-vue-next'

defineProps({
  hidden: { type: Boolean, default: false },
})
defineEmits(['toggle'])
</script>
