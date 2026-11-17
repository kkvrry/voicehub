<template>
  <!-- 一条音频的"体积"：上下对称的一片。形状**从音频本身算**，不是随机数：
       拿不到就整块不画，宁可少一块信息，也不摆一排与这段声音无关的竖条。
       `progress` > 0 时再叠一层：底层压暗、已播那一截全亮——播放条走到哪儿，
       这一片就亮到哪儿；点某一格定位也算这里（定位由用的人接 click）。 -->
  <span v-if="peaks.length" class="vwave" :class="{ 'is-part': progress > 0 }" aria-hidden="true">
    <svg
      v-for="layer in layers"
      :key="layer"
      :class="layer === 'on' ? 'vwave-on' : ''"
      :viewBox="`0 0 ${peaks.length - 1} 100`"
      preserveAspectRatio="none"
      :style="layer === 'on' ? { clipPath: `inset(0 ${100 - played}% 0 0)` } : undefined"
    >
      <path class="vwave-fill" :d="area" />
      <path class="vwave-edge" :d="edge" />
    </svg>
  </span>
</template>

<script setup>
// @ts-check
/** 波形：解码一次、按 RMS 降采样成固定点数，结果按 URL 缓存。
 *
 *  为什么是 RMS 而不是峰值：峰值取一小段里的最大值，安静处只要有一根毛刺就被拉满，
 *  画出来满屏同高——上次那排"死硬的直线"就有这个原因。RMS 是这一小段的能量，
 *  起伏跟着声音走，才像一段话。
 *
 *  为什么画成一片而不是一排竖条：64px 里塞几十根 1px 的条，每根都被压到亚像素，
 *  形状就糊成一把梳子；SVG 面积图不受分辨率限制，宽窄都能画出真实轮廓。
 *  解码用 `OfflineAudioContext`：它不出声，不会撞上"没有用户手势不许起音频"。
 *
 *  解码时顺手把时长交出去：调用方要一直显示"这条多长"，再单独探一次元数据
 *  就多一趟请求；`decodeAudioData` 的 AudioBuffer 本来就知道。
 */
import { computed, onMounted, ref, watch } from 'vue'

/** 点数：一句 1~2 秒的话分到 96 格，约 15ms 一格，音节之间的起落正好落在格子里。
 *  行内波形现在是一条整宽的条（原来只有 96px），格子得跟着密一点，否则一段话
 *  只剩十来个折点。 */
const POINTS = 96

const props = defineProps({
  url: { type: String, default: '' },
  /** 已播比例（0–1）：0 = 只画形状，不叠亮层。 */
  progress: { type: Number, default: 0 },
})

const emit = defineEmits(['duration'])

/** 两层：底层是整条形状，叠的那层只留已播的一截（靠 clip-path 截）。 */
const played = computed(() => Math.min(Math.max(props.progress, 0), 1) * 100)
const layers = computed(() => (played.value > 0 ? ['base', 'on'] : ['base']))

const peaks = ref([])

/** 同一个 URL 只解一次：详情里来回开关、同一段音频被两处引用，都不必再解一遍。 */
/** @type {Map<string, any>} */
const cache = new Map()
/** @type {OfflineAudioContext | null} */
let decoder = null

/** @param {string} url */
function decode(url) {
  if (cache.has(url)) return cache.get(url)
  const job = (async () => {
    const nothing = { peaks: [], duration: 0 }
    const response = await fetch(url)
    if (!response.ok) return nothing
    const raw = await response.arrayBuffer()
    if (!decoder) {
      // Safari 老版本只有带前缀的构造器；Window 类型里没有它，按 any 取。
      const win = /** @type {any} */ (window)
      const Offline = win.OfflineAudioContext || win.webkitOfflineAudioContext
      if (!Offline) return nothing
      decoder = new Offline(1, 1, 44100)
    }
    const audio = await /** @type {OfflineAudioContext} */ (decoder).decodeAudioData(raw)
    const duration = Number(audio.duration) || 0
    const data = audio.getChannelData(0)
    if (!data.length) return { peaks: [], duration }
    const step = Math.max(1, Math.floor(data.length / POINTS))
    const values = []
    for (let point = 0; point < POINTS; point += 1) {
      const from = point * step
      const to = Math.min(from + step, data.length)
      let energy = 0
      for (let i = from; i < to; i += 1) energy += data[i] * data[i]
      values.push(Math.sqrt(energy / Math.max(1, to - from)))
    }
    const top = Math.max(...values)
    if (!top) return { peaks: [], duration }
    // 归一化后开一点方（0.85）：轻声不至于缩成一条线，气口也不会被抬成"还在说话"。
    return {
      duration,
      peaks: values.map((value) => 0.05 + Math.pow(value / top, 0.85) * 0.95),
    }
  })().catch(() => ({ peaks: [], duration: 0 }))
  cache.set(url, job)
  return job
}

/** 上半片的点；下半片是它的镜像。中点 50，最多探到 ±46（留 4 给描边）。 */
const top = computed(() =>
  peaks.value.map((peak, index) => `${index},${(50 - peak * 46).toFixed(1)}`)
)
const bottom = computed(() =>
  [...peaks.value]
    .reverse()
    .map((peak, index) => `${peaks.value.length - 1 - index},${(50 + peak * 46).toFixed(1)}`)
)
const area = computed(() =>
  peaks.value.length ? `M${[...top.value, ...bottom.value].join('L')}Z` : ''
)
const edge = computed(() => (peaks.value.length ? `M${top.value.join('L')}` : ''))

/** @param {string} url */
async function load(url) {
  emit('duration', 0)
  if (!url) {
    peaks.value = []
    return
  }
  const result = await decode(url)
  peaks.value = result.peaks
  if (result.duration) emit('duration', result.duration)
}

onMounted(() => load(props.url))
watch(() => props.url, load)
</script>
