<template>
  <div ref="slotEl" class="grid-slot">
    <div class="grid">
      <template v-if="active">
        <VoiceCard
          v-for="voice in slice"
          :key="voice.id"
          :voice="voice"
          :class="sizeClass(voice)"
        />
      </template>
      <template v-else>
        <!-- 骨架只占住行列高，不建卡片组件、不拉封面。整块离开视口后回到这里。
             高度类要与真卡一致（见 sizeClass）：块激活时若行高变了，滚动位置会被
             浏览器锚定改写，屏幕中间就会露出整屏骨架。 -->
        <div
          v-for="voice in slice"
          :key="voice.id"
          class="card card-skeleton"
          :class="sizeClass(voice)"
          aria-hidden="true"
        />
      </template>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import VoiceCard from './VoiceCard.vue'

const props = defineProps({
  /** 全量列表；真正的切片只在块进入视口时才做。 */
  voices: { type: Array, required: true },
  start: { type: Number, required: true },
  count: { type: Number, required: true },
  /** 首块先挂真卡，避免第一帧整屏骨架。 */
  initialActive: { type: Boolean, default: false },
})

const slotEl = ref(null)
const active = ref(props.initialActive)
const slice = computed(() =>
  /** @type {any[]} */ (props.voices).slice(props.start, props.start + props.count)
)

/** 卡片多出来的是"标签行"和"描述行"两块可选内容，行高因此不是定值。
 *  骨架用同一组下界，网格自动把同一行的卡拉齐——真卡与骨架逐行等高，
 *  块切换时才不会改变整段高度。标签在卡上算一行（再多由 `.grid .card-tags` 裁掉），
 *  描述算两行（`.card-desc` 留满两行高）。 */
/** @param {any} voice */
function sizeClass(voice) {
  const hasTags = (voice.tags || []).length > 0
  const hasDesc = Boolean(voice.description)
  if (hasTags && hasDesc) return 'card-h-both'
  if (hasTags) return 'card-h-tags'
  if (hasDesc) return 'card-h-desc'
  return ''
}

/** @type {IntersectionObserver | null} */
let observer = null

onMounted(() => {
  observer = new IntersectionObserver(
    (entries) => {
      const entry = entries[0]
      if (entry) active.value = entry.isIntersecting
    },
    // 提前约一屏多挂载，滚到时卡片已经就位；离开后整块卸载成骨架。
    { rootMargin: '1200px 0px' }
  )
  if (slotEl.value) observer.observe(slotEl.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})
</script>
