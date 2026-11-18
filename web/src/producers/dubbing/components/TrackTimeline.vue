<template>
  <div class="tl">
    <div class="tl-ruler-row">
      <span class="tl-gutter"></span>
      <div ref="rulerEl" class="tl-ruler">
        <span
          v-for="tick in ticks"
          :key="tick.at"
          class="tl-tick"
          :style="{ left: `${(tick.at / duration) * 100}%` }"
        >{{ tick.label }}</span>
      </div>
    </div>

    <div class="tl-lanes" @click="onLaneClick">
      <div class="tl-lane">
        <span class="tl-label">原声</span>
        <div class="tl-track">
          <div v-if="originalUrl" class="tl-clip" :class="{ mute: muted }">
            <span class="tl-note">{{ muted ? '静音（已重配）' : projectLabel }}</span>
          </div>
          <span v-else class="tl-note is-empty">还没有抽出音轨</span>
        </div>
      </div>

      <div class="tl-lane">
        <span class="tl-label">配音</span>
        <div class="tl-track" ref="cueLaneEl">
          <canvas ref="canvasEl" class="tl-canvas"></canvas>
          <span v-if="!cues.length" class="tl-note is-empty">未转录</span>
        </div>
      </div>

      <div class="tl-lane">
        <span class="tl-label">成品</span>
        <div class="tl-track">
          <div v-if="mixUrl" class="tl-clip is-mix">
            <span class="tl-note">已贴齐并混音</span>
          </div>
          <span v-else class="tl-note is-empty">未混音</span>
        </div>
      </div>

      <div class="tl-playhead" :style="{ transform: `translateX(${playheadPx}px)` }" aria-hidden="true"></div>
    </div>
  </div>
</template>

<script setup>
// 全片概览：段落在整段时长里只有几像素宽，逐段建 DOM 只是画一条色带。
// 色带用 canvas 一次绘完，段块留给放大后的段落表——两千段也不掉帧。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cues, mixReady, project, roleOf, state } from '../store'
import { mediaUrl } from '../api'

const emit = defineEmits(['seek', 'select'])

const canvasEl = ref(null)
const cueLaneEl = ref(null)
const rulerEl = ref(null)
const width = ref(0)

const duration = computed(() => (project.value?.duration_ms || 0) / 1000)
const playheadPx = computed(() => {
  if (!duration.value || !width.value) return 0
  return Math.min(width.value, Math.max(0, (state.playTime / duration.value) * width.value))
})
const originalUrl = computed(() => (project.value ? mediaUrl(project.value.id, 'original') : ''))
const mixUrl = computed(() => (mixReady.value ? mediaUrl(project.value.id, 'mix') : ''))
const muted = computed(() => Number(project.value?.options?.original_gain ?? -60) <= -60)
const projectLabel = computed(() =>
  project.value?.kind === 'video' ? '视频音轨' : project.value?.source_name || '源音频'
)

const ticks = computed(() => {
  const total = duration.value
  if (!total) return []
  // 五格等分看着最稳：太密看不清，太疏没有参照。
  return Array.from({ length: 5 }, (_, index) => {
    const at = (total / 5) * index
    return { at, label: label(at) }
  })
})

function label(seconds) {
  const total = Math.round(seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

let observer = null

function draw() {
  const lane = cueLaneEl.value
  const canvas = canvasEl.value
  if (!lane || !canvas) return
  const w = lane.clientWidth
  const h = lane.clientHeight
  if (!w || !h) return
  width.value = w
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  canvas.width = Math.round(w * dpr)
  canvas.height = Math.round(h * dpr)
  canvas.style.width = `${w}px`
  canvas.style.height = `${h}px`
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)
  // canvas 不认 CSS 变量：没有配色的角色退回当前主题的强调色。
  const fallback =
    getComputedStyle(canvas).getPropertyValue('--sh-accent').trim() || '#2f7bff'
  const total = duration.value || 1
  const band = Math.max(8, h - 14)
  const top = (h - band) / 2
  for (const cue of cues.value) {
    const x = (cue.start_ms / 1000 / total) * w
    const cw = Math.max(1, ((cue.end_ms - cue.start_ms) / 1000 / total) * w - 0.6)
    ctx.fillStyle = roleOf(cue)?.color || fallback
    // 没有产物的段压暗：色带同时表示"这一段是谁"和"有没有配音"。
    ctx.globalAlpha = cue.state === 'ready' ? 0.82 : cue.state === 'failed' ? 0.35 : 0.45
    ctx.fillRect(x, top, cw, band)
  }
  ctx.globalAlpha = 1
}

onMounted(() => {
  draw()
  observer = new ResizeObserver(draw)
  if (cueLaneEl.value) observer.observe(cueLaneEl.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch([cues, () => project.value?.id], () => draw(), { deep: false })

function onLaneClick(event) {
  const lane = cueLaneEl.value
  if (!lane || !duration.value) return
  const rect = lane.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  const seconds = ratio * duration.value
  emit('seek', seconds)
  const hit = cues.value.find(
    (cue) => seconds >= cue.start_ms / 1000 && seconds < cue.end_ms / 1000
  )
  if (hit) emit('select', hit)
}
</script>

<style scoped>
.tl {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
  height: 100%;
}

.tl-ruler-row {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  flex: none;
}

.tl-ruler {
  position: relative;
  height: 16px;
}

.tl-tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 10px;
  color: var(--sh-text-4);
}

.tl-lanes {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tl-lane {
  flex: 1 1 0;
  min-height: 26px;
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  align-items: center;
}

.tl-label {
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.tl-track {
  position: relative;
  height: 100%;
  border-radius: 6px;
  background: var(--sh-chip);
  overflow: hidden;
}

.tl-canvas {
  position: absolute;
  inset: 0;
  cursor: pointer;
}

.tl-clip {
  position: absolute;
  inset: 3px 0;
  display: flex;
  align-items: center;
  padding: 0 8px;
  border-radius: 5px;
  background: var(--sh-accent-soft);
  border: 1px solid var(--sh-accent-ring);
  color: var(--sh-accent);
  font-size: 10.5px;
}

.tl-clip.mute {
  background: var(--sh-chip);
  border: 1px dashed var(--sh-line);
  color: var(--sh-text-4);
}

.tl-clip.is-mix {
  background: var(--sh-ok-soft);
  border-color: transparent;
  color: var(--sh-ok);
}

.tl-note {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl-note.is-empty {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  /* 显式给高度：`inset: 0` 在这个 flex 轨道里只量到 2px（行高的一半），
     标签就被压成一条线，看不见。 */
  height: 100%;
  display: flex;
  align-items: center;
  padding: 0 8px;
  color: var(--sh-text-4);
  font-size: 10.5px;
}

.tl-playhead {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 56px;
  width: 1px;
  background: var(--sh-accent);
  pointer-events: none;
  will-change: transform;
}

.tl-playhead::before {
  content: '';
  position: absolute;
  top: 0;
  left: -3px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sh-accent);
}
</style>
