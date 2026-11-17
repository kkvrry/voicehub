<template>
  <img
    ref="el"
    :src="src"
    :alt="alt"
    :width="width"
    :height="height"
    loading="lazy"
    decoding="async"
  />
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { useLazyImage } from '../../composables/lazyImage'

const props = defineProps({
  /** 真实封面地址；空串时保持透明占位，不发请求。 */
  src: { type: String, default: '' },
  alt: { type: String, default: '' },
  width: { type: [String, Number], default: undefined },
  height: { type: [String, Number], default: undefined },
})

const { el, src } = useLazyImage(computed(() => props.src))
// 模板里 `ref="el"` 用到它；脚本侧不读，显式消费一次避免"未使用"告警。
void el
</script>
