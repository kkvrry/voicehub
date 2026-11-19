<template>
  <div class="wf" :class="{ mini, editable }">
    <div
      ref="plotEl"
      class="wf-plot"
      :class="{ 'is-editable': editable && total > 0 }"
      :tabindex="editable && total > 0 ? 0 : -1"
      @pointerdown="beginSelection"
    >
      <div class="wf-canvas">
        <span
          v-if="hasSel"
          class="wf-sel"
          :style="{ left: `${selLeft}%`, width: `${selWidth}%` }"
          aria-hidden="true"
        />
        <template v-if="bars.length">
          <svg class="wf-bars is-base" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <path :d="basePath" />
          </svg>
          <span
            v-for="(gap, index) in gaps"
            :key="`gap-${index}`"
            class="wf-gap"
            :style="{ left: `${gap.left}%`, width: `${gap.width}%` }"
            aria-hidden="true"
          />
          <svg class="wf-bars is-on" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <defs>
              <clipPath :id="clipId" clipPathUnits="userSpaceOnUse">
                <rect
                  v-for="(segment, index) in kept"
                  :key="index"
                  :x="segment.left"
                  y="0"
                  :width="segment.width"
                  height="100"
                />
              </clipPath>
            </defs>
            <path :d="basePath" :clip-path="`url(#${clipId})`" />
          </svg>
        </template>
        <div v-else-if="!loaded" class="wf-loading" aria-hidden="true">
          <i v-for="index in 28" :key="index" :style="{ height: `${18 + ((index * 17) % 58)}%` }" />
        </div>
        <div v-else class="wf-unavailable" aria-hidden="true"><i /></div>
      </div>

      <template v-if="editable && total > 0 && hasSel">
        <button
          type="button"
          role="slider"
          aria-orientation="horizontal"
          class="wf-handle is-start"
          :style="{ left: `${selLeft}%` }"
          aria-label="选区起点"
          :aria-valuemin="0"
          :aria-valuemax="total"
          :aria-valuenow="Number(selFrom.toFixed(3))"
          :title="`选区起点 ${clock(selFrom)} · 方向键 ±10 ms，Shift ±100 ms`"
          @pointerdown.stop="beginDrag($event, 'start')"
          @keydown="nudge($event, 'start')"
        >
          <span class="wf-grip" aria-hidden="true" />
        </button>
        <button
          type="button"
          role="slider"
          aria-orientation="horizontal"
          class="wf-handle is-end"
          :style="{ left: `${selRight}%` }"
          aria-label="选区终点"
          :aria-valuemin="0"
          :aria-valuemax="total"
          :aria-valuenow="Number(selTo.toFixed(3))"
          :title="`选区终点 ${clock(selTo)} · 方向键 ±10 ms，Shift ±100 ms`"
          @pointerdown.stop="beginDrag($event, 'end')"
          @keydown="nudge($event, 'end')"
        >
          <span class="wf-grip" aria-hidden="true" />
        </button>
      </template>
    </div>

    <div v-if="!mini" class="wf-ruler" aria-hidden="true">
      <span>{{ clock(0) }}</span>
      <span>{{ clock(total / 2) }}</span>
      <span>{{ clock(total) }}</span>
    </div>
  </div>
</template>

<script setup>
// @ts-check
// 波形与裁剪选区：数据来自 `loadWaveform`（同一 URL 只解一次）。
// 组件只画保留区间与选区、报告拖动结果；多段合并、删除与撤销都在调用方的 store 里。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { loadWaveform, resample } from '../../composables/waveform'

const MIN_SELECTION = 0.01
let clipSeq = 0

const props = defineProps({
  url: { type: String, default: '' },
  /** 库里记的时长；读不到时用解码得到的时长兜底。 */
  duration: { type: Number, default: 0 },
  /** 保留的区间 `[{start, end}, ...]`；空表按整段画。 */
  segments: { type: Array, default: () => [] },
  /** 当前选区 `{start, end}`；空表示没有选区。 */
  selection: { type: Object, default: null },
  mini: { type: Boolean, default: false },
  editable: { type: Boolean, default: false },
  /** 替换样例后由调用方加一：URL 不变，但磁盘上的音频已经换了。 */
  revision: { type: Number, default: 0 },
})

const emit = defineEmits(['select'])

const clipId = `wf-clip-${(clipSeq += 1)}`
const plotEl = ref(/** @type {HTMLElement|null} */ (null))
const peaks = ref(/** @type {number[]} */ ([]))
const decodedDuration = ref(0)
const loaded = ref(false)
let token = 0
/** @type {'' | 'new' | 'start' | 'end'} */
let dragMode = ''
let dragAnchor = 0
let dragStart = 0
let dragEnd = 0

const total = computed(() => {
  const value = props.duration > 0 ? props.duration : decodedDuration.value
  return Math.max(0, Number(value) || 0)
})

/** 保留的区间（秒），夹在时长内并排好序。 */
const keptRanges = computed(() => {
  if (total.value <= 0) return []
  /** @type {{ start: number, end: number }[]} */
  const rows = []
  for (const item of /** @type {any[]} */ (props.segments || [])) {
    const start = clamp(Number(item?.start) || 0, 0, total.value)
    const end = clamp(Number(item?.end) || 0, 0, total.value)
    if (end > start) rows.push({ start, end })
  }
  rows.sort((a, b) => a.start - b.start)
  return rows.length ? rows : [{ start: 0, end: total.value }]
})

/** 保留区间在波形上的位置（百分比）。 */
const kept = computed(() =>
  keptRanges.value.map((/** @type {any} */ row) => ({
    left: (row.start / total.value) * 100,
    width: ((row.end - row.start) / total.value) * 100,
  }))
)

/** 被删掉的区间（百分比），画在波形上作为暗区。 */
const gaps = computed(() => {
  const rows = keptRanges.value
  if (!rows.length) return []
  const out = []
  let cursor = 0
  for (const row of rows) {
    if (row.start > cursor + 0.001) {
      out.push({ left: (cursor / total.value) * 100, width: ((row.start - cursor) / total.value) * 100 })
    }
    cursor = Math.max(cursor, row.end)
  }
  if (cursor < total.value - 0.001) {
    out.push({ left: (cursor / total.value) * 100, width: ((total.value - cursor) / total.value) * 100 })
  }
  return out
})

const selFrom = computed(() =>
  clamp(Number(/** @type {any} */ (props.selection)?.start) || 0, 0, total.value)
)
const selTo = computed(() =>
  clamp(Number(/** @type {any} */ (props.selection)?.end) || 0, 0, total.value)
)
const hasSel = computed(() => selTo.value - selFrom.value >= MIN_SELECTION)
const selLeft = computed(() => (total.value > 0 ? (selFrom.value / total.value) * 100 : 0))
const selRight = computed(() => (total.value > 0 ? (selTo.value / total.value) * 100 : 0))
const selWidth = computed(() => Math.max(0, selRight.value - selLeft.value))

const bars = computed(() => resample(peaks.value, props.mini ? 42 : 150))
const basePath = computed(() => barPath(bars.value))

/**
 * 一排竖条合成一条 path：每根一个矩形子路径。
 * 用 path 而不是几十个 `<i>`：一行里只有一个 SVG 节点，滚动时不抖。
 *
 * @param {number[]} values
 */
function barPath(values) {
  const count = values.length
  if (!count) return ''
  const gap = 100 / count
  const width = Math.max(0.35, gap * 0.58)
  const parts = []
  for (let index = 0; index < count; index += 1) {
    const height = Math.max(1.2, values[index] * 92)
    const x = index * gap + (gap - width) / 2
    const y = 50 - height / 2
    parts.push(
      `M${x.toFixed(2)} ${y.toFixed(2)}h${width.toFixed(2)}v${height.toFixed(2)}h-${width.toFixed(2)}z`
    )
  }
  return parts.join('')
}

/**
 * @param {number} value
 * @param {number} low
 * @param {number} high
 */
function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high)
}

/**
 * @param {number} seconds
 */
function clock(seconds) {
  const safe = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(safe / 60)
  const rest = Math.round(safe % 60)
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

/**
 * @param {number} value
 */
function round(value) {
  return Math.round(value * 1000) / 1000
}

/**
 * @param {PointerEvent} event
 */
function timeAt(event) {
  const rect = plotEl.value?.getBoundingClientRect()
  if (!rect?.width || total.value <= 0) return 0
  const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1)
  return ratio * total.value
}

/**
 * @param {number} start
 * @param {number} end
 */
function emitSelection(start, end) {
  const a = round(clamp(start, 0, total.value))
  const b = round(clamp(end, 0, total.value))
  emit('select', { start: Math.min(a, b), end: Math.max(a, b) })
}

/**
 * 在波形空白处按下：从这一点开始框选。
 * @param {PointerEvent} event
 */
function beginSelection(event) {
  if (!props.editable || total.value <= 0) return
  if (event.button !== 0) return
  event.preventDefault()
  // preventDefault 会抢走默认聚焦：方向键要能接着用，必须显式聚焦。
  plotEl.value?.focus({ preventScroll: true })
  dragMode = 'new'
  dragAnchor = timeAt(event)
  dragStart = dragAnchor
  dragEnd = dragAnchor
  listen()
}

/**
 * 拖动选区两端。
 * @param {PointerEvent} event
 * @param {'start' | 'end'} side
 */
function beginDrag(event, side) {
  if (!props.editable || total.value <= 0 || !hasSel.value) return
  event.preventDefault()
  // preventDefault 之后按钮不会自己拿到焦点；不补焦点，方向键会落空。
  const button = /** @type {HTMLElement|null} */ (event.currentTarget)
  button?.focus?.({ preventScroll: true })
  dragMode = side
  dragStart = selFrom.value
  dragEnd = selTo.value
  listen()
}

function listen() {
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', endDrag, { once: true })
  window.addEventListener('pointercancel', endDrag, { once: true })
}

/**
 * @param {PointerEvent} event
 */
function onMove(event) {
  if (!dragMode || total.value <= 0) return
  const time = timeAt(event)
  if (dragMode === 'new') {
    dragStart = Math.min(dragAnchor, time)
    dragEnd = Math.max(dragAnchor, time)
    emitSelection(dragStart, dragEnd)
    return
  }
  if (dragMode === 'start') {
    dragStart = clamp(time, 0, dragEnd - MIN_SELECTION)
    emitSelection(dragStart, dragEnd)
    return
  }
  dragEnd = clamp(time, dragStart + MIN_SELECTION, total.value)
  emitSelection(dragStart, dragEnd)
}

function endDrag() {
  if (dragMode === 'new' && dragEnd - dragStart < MIN_SELECTION) {
    // 没拖出东西就是清掉选区；点一下不留下宽度为 0 的选区。
    emitSelection(dragAnchor, dragAnchor)
  } else if (dragMode === 'new') {
    // 框完选区把焦点交给起点把手：接着按方向键就能细调，不必再找它。
    void nextTick(() => {
      const handle = plotEl.value?.querySelector('.wf-handle.is-start')
      if (handle instanceof HTMLElement) handle.focus({ preventScroll: true })
    })
  }
  dragMode = ''
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
}

/**
 * 方向键细调：默认 10 ms，Shift 100 ms，Alt 1 ms。
 * @param {KeyboardEvent} event
 * @param {'start' | 'end'} side
 */
function nudge(event, side) {
  if (!props.editable || total.value <= 0 || !hasSel.value) return
  const step = event.altKey ? 0.001 : event.shiftKey ? 0.1 : 0.01
  let delta = 0
  if (event.key === 'ArrowLeft') delta = -step
  if (event.key === 'ArrowRight') delta = step
  if (!delta) return
  event.preventDefault()
  if (side === 'start') {
    emitSelection(clamp(selFrom.value + delta, 0, selTo.value - MIN_SELECTION), selTo.value)
    return
  }
  emitSelection(selFrom.value, clamp(selTo.value + delta, selFrom.value + MIN_SELECTION, total.value))
}

async function load() {
  const current = ++token
  const url = props.url
  peaks.value = []
  decodedDuration.value = 0
  loaded.value = false
  if (!url) {
    loaded.value = true
    return
  }
  const result = await loadWaveform(url)
  if (current !== token) return
  peaks.value = result.peaks
  decodedDuration.value = result.duration
  loaded.value = true
}

watch(() => [props.url, props.revision], load, { immediate: true })

onBeforeUnmount(() => {
  token += 1
  dragMode = ''
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
})
</script>

<style scoped>
.wf {
  min-width: 0;
}
.wf-plot {
  position: relative;
  height: 88px;
  border-radius: var(--sh-r-xs);
  background: var(--sh-field);
  box-shadow: var(--sh-inset-hi);
  touch-action: none;
}
.wf-plot.is-editable {
  cursor: crosshair;
}
.wf-plot.is-editable:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 1px;
}
.wf-canvas {
  position: absolute;
  inset: 0;
  overflow: hidden;
  border-radius: inherit;
}
.wf.mini .wf-plot {
  height: 26px;
  background: transparent;
  box-shadow: none;
  border-radius: 0;
  pointer-events: none;
}
.wf-sel {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 2;
  background: var(--sh-accent-soft);
  border-left: 1px solid var(--sh-accent-ring);
  border-right: 1px solid var(--sh-accent-ring);
  pointer-events: none;
}
.wf-gap {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 1;
  background: var(--sh-field);
  opacity: 0.62;
  pointer-events: none;
}
.wf-gap::after {
  content: '';
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    135deg,
    transparent 0 4px,
    var(--sh-line-soft) 4px 5px
  );
}
.wf-bars {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
.wf-bars.is-base {
  fill: var(--sh-tree-line);
  opacity: 0.6;
}
.wf-bars.is-on {
  fill: var(--sh-accent);
  opacity: 0.95;
  z-index: 1;
}
.wf-loading {
  position: absolute;
  inset: 6px 8px;
  display: flex;
  align-items: center;
  gap: 2px;
  opacity: 0.55;
}
.wf-loading i {
  flex: 1;
  min-width: 1px;
  border-radius: 1px;
  background: var(--sh-line);
  animation: wf-pulse 1.2s var(--sh-ease) infinite alternate;
}
@keyframes wf-pulse {
  from { opacity: 0.35; }
  to { opacity: 0.85; }
}
.wf-unavailable {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
}
.wf-unavailable i {
  width: 70%;
  border-top: 1px dashed var(--sh-line);
}
.wf.mini .wf-unavailable i {
  border-top-style: solid;
  border-top-color: var(--sh-line-soft);
}
.wf-handle {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 3;
  width: 16px;
  padding: 0;
  border: 0;
  background: transparent;
  transform: translateX(-50%);
  cursor: ew-resize;
}
.wf-handle::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 7px;
  width: 2px;
  background: var(--sh-accent);
  box-shadow: 0 0 0 1px var(--sh-panel);
}
.wf-grip {
  position: absolute;
  top: 3px;
  left: 50%;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--sh-accent);
  box-shadow: 0 0 0 2px var(--sh-panel);
  transform: translateX(-50%);
}
.wf-handle:focus-visible {
  outline: none;
}
.wf-handle:focus-visible .wf-grip,
.wf-handle:focus-visible::before {
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.wf-ruler {
  display: flex;
  justify-content: space-between;
  padding: 3px 1px 0;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  color: var(--sh-text-4);
}
</style>
