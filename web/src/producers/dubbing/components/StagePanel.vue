<template>
  <section
    ref="stageEl"
    class="stage"
    :class="{ 'is-max': state.panelMax }"
    :style="state.panelMax ? null : { '--panel-h': `${panelH}px` }"
  >
    <div ref="previewEl" class="preview">
      <div ref="frameEl" class="frame" :class="{ 'is-audio': !isVideo }">
        <video
          v-if="isVideo"
          ref="mediaEl"
          class="media"
          :src="sourceUrl"
          preload="metadata"
          playsinline
          @click="toggle"
          @timeupdate="syncTime"
          @loadedmetadata="syncTime"
          @play="onPlay"
          @pause="onPause"
          @ended="onPause"
        ></video>
        <audio
          v-else
          ref="mediaEl"
          class="media is-hidden"
          :src="sourceUrl"
          preload="metadata"
          @timeupdate="syncTime"
          @loadedmetadata="syncTime"
          @play="onPlay"
          @pause="onPause"
          @ended="onPause"
        ></audio>

        <div v-if="!isVideo" class="audio-face" @click="toggle">
          <div class="audio-bars" aria-hidden="true">
            <i v-for="n in 48" :key="n" :style="{ height: `${barHeight(n)}%` }"></i>
          </div>
          <span class="audio-name">{{ project?.source_name }}</span>
        </div>

        <span class="frame-badge">{{ isVideo ? '预览' : '音频' }}</span>

        <p v-if="sourceMissing" class="stage-warn" role="alert">
          源文件不在了：原声试听不可用{{ isVideo ? '，导出成片也会失败' : '' }}。在项目头重新指定路径。
        </p>

        <button
          v-if="!state.playing"
          class="frame-play"
          type="button"
          title="播放"
          aria-label="播放"
          @click="toggle"
        >
          <Play :size="20" :stroke-width="1.8" />
        </button>

        <div v-if="state.overlay !== 'off' && currentCue" class="frame-sub">
          <b v-if="state.overlay === 'role'">{{ roleName(currentCue) }}</b>
          <span>{{ currentCue.text }}</span>
        </div>
      </div>

      <div class="transport">
        <button class="tp-play" type="button" :title="state.playing ? '暂停' : '播放'" :aria-label="state.playing ? '暂停' : '播放'" @click="toggle">
          <Pause v-if="state.playing" :size="14" :stroke-width="1.8" />
          <Play v-else :size="14" :stroke-width="1.8" />
        </button>
        <span class="tp-time">{{ clockTime }}<span> / {{ durationLabel }}</span></span>
        <input
          class="tp-bar"
          type="range"
          min="0"
          :max="Math.max(0.1, duration)"
          step="0.01"
          :value="scrubValue"
          :style="{ '--p': `${progress}%` }"
          aria-label="播放进度"
          @pointerdown="scrubbing = true"
          @pointerup="scrubbing = false"
          @change="scrubbing = false"
          @input="onScrub"
        />
        <div class="tp-overlay" role="group" aria-label="字幕叠加">
          <button
            v-for="item in OVERLAYS"
            :key="item.key"
            type="button"
            :class="{ 'is-on': state.overlay === item.key }"
            @click="state.overlay = item.key"
          >{{ item.label }}</button>
        </div>
      </div>
    </div>

    <div
      class="split"
      role="separator"
      tabindex="0"
      aria-orientation="horizontal"
      aria-label="调整字幕区高度"
      title="拖动调整高度；双击恢复默认"
      @pointerdown="startDrag"
      @dblclick="resetHeight"
      @keydown.up.prevent="nudge(-24)"
      @keydown.down.prevent="nudge(24)"
    >
      <i></i>
    </div>

    <section class="panel">
      <div class="panel-bar">
        <div class="modes" role="tablist" aria-label="下区视图">
          <button
            v-for="item in VIEWS"
            :key="item.key"
            type="button"
            role="tab"
            :aria-selected="state.view === item.key"
            :class="{ 'is-on': state.view === item.key }"
            @click="state.view = item.key"
          >{{ item.label }}</button>
        </div>
        <span class="bar-hint">{{ hint }}</span>
        <span class="bar-grow"></span>
        <button
          class="bar-toggle"
          type="button"
          :title="state.panelMax ? '显示预览' : '字幕占满整列'"
          :aria-label="state.panelMax ? '显示预览' : '字幕占满整列'"
          :aria-pressed="state.panelMax"
          @click="togglePanelMax"
        >
          <Minimize2 v-if="state.panelMax" :size="14" :stroke-width="1.8" />
          <Maximize2 v-else :size="14" :stroke-width="1.8" />
        </button>
      </div>

      <div class="panel-body">
        <TrackTimeline
          v-if="state.view === 'timeline'"
          @seek="seek"
          @select="onSelect"
        />
        <SubtitleTable v-else />
      </div>
    </section>
  </section>
</template>

<script setup>
// 中列：预览 + 下区。下区是**一块共用区域**，时间轴与字幕切换显示。
//
// 高度由分隔条决定并记住：字幕是这里唯一要反复改的东西，固定 316px 只能看四行，
// 滚一格就翻掉半屏。预览没有进度条时也分不清播放到哪，所以预览下面常驻一条走带。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Maximize2, Minimize2, Pause, Play } from 'lucide-vue-next'
import { actions as hostActions, store as hostStore } from '../../../store'
import SubtitleTable from './SubtitleTable.vue'
import TrackTimeline from './TrackTimeline.vue'
import {
  cues,
  project,
  roleName,
  seekTo,
  setPanelHeight,
  state,
  togglePanelMax,
} from '../store'
import { mediaUrl } from '../api'

const VIEWS = [
  { key: 'timeline', label: '时间轴' },
  { key: 'subtitle', label: '字幕' },
]

const OVERLAYS = [
  { key: 'off', label: '关' },
  { key: 'text', label: '文本' },
  { key: 'role', label: '文本 + 角色' },
]

const stageEl = ref(null)
const previewEl = ref(null)
const frameEl = ref(null)
const mediaEl = ref(null)
/** 下区高度（px）。0 = 还没量过；拖动与键盘调整都改它，松手写回 store。 */
const panelH = ref(0)
const scrubbing = ref(false)
const scrubValue = ref(0)

const isVideo = computed(() => project.value?.kind === 'video')
const sourceMissing = computed(() => state.detail?.source_ok === false)
const sourceUrl = computed(() => (project.value ? mediaUrl(project.value.id, 'source') : ''))
const duration = computed(() => (project.value?.duration_ms || 0) / 1000)

const durationLabel = computed(() => {
  const total = Math.round(duration.value)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
})

const clockTime = computed(() => {
  const total = Math.max(0, Math.floor(state.playTime))
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
})

const progress = computed(() => {
  if (!duration.value) return 0
  return Math.min(100, Math.max(0, (scrubValue.value / duration.value) * 100))
})

const currentCue = computed(() => {
  const time = state.playTime * 1000
  return cues.value.find((cue) => time >= cue.start_ms && time < cue.end_ms) || null
})

const hint = computed(() => {
  const stats = state.detail?.stats
  const pending = Object.keys(state.pending).length
  const picked = state.selectedIds.length
  if (state.view === 'subtitle') {
    if (!stats?.total) return '还没有段落'
    const base = `${stats.total} 段 · ${stats.dubbed} 需配音 · ${stats.ready} 已配音`
    // 写库一时失败的那几段：说清楚"还在这儿、在重试"，别让人以为改丢了。
    const more = []
    if (pending) more.push(`${pending} 段未保存`)
    if (picked) more.push(`已选 ${picked} 段`)
    return more.length ? `${base} · ${more.join(' · ')}` : base
  }
  return `${cues.value.length} 段`
})

function barHeight(n) {
  // 纯装饰，不表示真实波形：真波形要走服务端峰值，那属于下一版。
  const wave = Math.abs(Math.sin(n * 1.7) * Math.cos(n * 0.43))
  return 18 + Math.round(wave * 74)
}

// ---- 尺寸 ----------------------------------------------------------------

let observer = null

/** 预览框按可用高度与 16:9 取小：宽了会顶进下区，窄了上下留一条黑边。 */
function fitFrame() {
  const box = previewEl.value
  const frame = frameEl.value
  if (!box || !frame) return
  const bar = box.querySelector('.transport')
  const room = box.clientHeight - (bar?.offsetHeight || 34) - 8
  const width = Math.max(120, Math.min(box.clientWidth, (room * 16) / 9))
  frame.style.width = `${Math.round(width)}px`
  frame.style.height = `${Math.round((width * 9) / 16)}px`
  // 走带与画面同宽：两边对齐才不会看着像两截。
  if (bar) bar.style.width = `${Math.round(width)}px`
}

/** 下区高度：拖过就用拖出来的值，没拖过按窗口比例给一个默认值。
 *
 *  `keep` 是窗口尺寸变化时的重新夹取：只把当前值限制进可用范围，不重算默认值——
 *  拖动的每一步都会改变预览高度，观察者若在这里重算一次默认值，那一下就被抹掉了。
 */
function layout(keep = false) {
  const stage = stageEl.value
  if (!stage) return
  const height = stage.clientHeight
  const bottom = Math.max(200, height - 180)
  const wanted = state.panelHeight || Math.round(Math.min(640, Math.max(300, height * 0.5)))
  const next = keep ? panelH.value || wanted : wanted
  panelH.value = Math.min(Math.max(200, next), bottom)
  fitFrame()
}

function nudge(step) {
  const bottom = Math.max(200, (stageEl.value?.clientHeight || 640) - 180)
  panelH.value = Math.min(bottom, Math.max(200, panelH.value + step))
  setPanelHeight(panelH.value)
}

function resetHeight() {
  setPanelHeight(0)
  panelH.value = 0
  layout()
}

function startDrag(event) {
  if (event.button !== 0) return
  const startY = event.clientY
  const startH = panelH.value
  const stage = stageEl.value
  const limit = Math.max(200, (stage?.clientHeight || 640) - 180)
  const move = (moveEvent) => {
    const next = Math.min(limit, Math.max(200, startH - (moveEvent.clientY - startY)))
    panelH.value = next
  }
  const stop = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    document.body.classList.remove('is-row-resizing')
    setPanelHeight(panelH.value)
  }
  document.body.classList.add('is-row-resizing')
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop)
  event.preventDefault()
}

onMounted(() => {
  layout()
  observer = new ResizeObserver(() => layout(true))
  const stage = stageEl.value
  if (stage) observer.observe(stage)
  const preview = previewEl.value
  if (preview) observer.observe(preview)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  stopClock()
  mediaEl.value?.pause()
})

// ---- 播放 ----------------------------------------------------------------

let raf = 0
/** 试听一段原声时的停点（秒）；0 = 没在试听，正常播放。 */
let auditionEnd = 0

/** 播放时按帧推进播放头：`timeupdate` 只有 4 Hz，进度条看着是一格一格跳的。 */
function startClock() {
  stopClock()
  const step = () => {
    const media = mediaEl.value
    if (!media || media.paused) {
      raf = 0
      return
    }
    state.playTime = media.currentTime || 0
    if (!scrubbing.value) scrubValue.value = state.playTime
    // 试听一段就停在这一段的结尾：不停的话它就顺着整片放下去了。
    if (auditionEnd && state.playTime >= auditionEnd - 0.02) {
      auditionEnd = 0
      media.pause()
      return
    }
    raf = window.requestAnimationFrame(step)
  }
  raf = window.requestAnimationFrame(step)
}

function stopClock() {
  if (raf) window.cancelAnimationFrame(raf)
  raf = 0
}

function onPlay() {
  // 预览出声时宿主播放条让位：两边各有一个 <audio>，同时响就是两个声音。
  if (hostStore.player.playing) hostActions.stop()
  state.playing = true
  startClock()
}

function onPause() {
  state.playing = false
  stopClock()
  syncTime()
}

function syncTime() {
  const media = mediaEl.value
  if (!media) return
  const at = media.currentTime || 0
  state.playTime = at
  if (!scrubbing.value) scrubValue.value = at
}

function toggle() {
  const media = mediaEl.value
  if (!media) return
  if (media.paused) {
    auditionEnd = 0
    void media.play()
  } else {
    media.pause()
  }
}

/** 拖动即跳：等松手才跳会让人以为没拖上。 */
function onScrub(event) {
  auditionEnd = 0
  scrubValue.value = Number(event.target.value) || 0
  seekTo(scrubValue.value)
}

function seek(seconds) {
  auditionEnd = 0
  seekTo(seconds)
}

watch(
  () => state.seek.n,
  () => {
    // 手动跳走就不再是"试听那一段"了：停点要清掉，否则放一半自己停住。
    auditionEnd = 0
    const media = mediaEl.value
    if (media) media.currentTime = state.seek.at
    scrubValue.value = state.seek.at
  }
)

// 试听某一段：跳到开头、放到结尾。与逐段配音互斥（那边开始前会先叫停预览）。
watch(
  () => state.audition.n,
  () => {
    const media = mediaEl.value
    const { start, end } = state.audition
    if (!media || !end) {
      auditionEnd = 0
      return
    }
    auditionEnd = end
    media.currentTime = start
    scrubValue.value = start
    void media.play()
  }
)

// 逐段配音开始播放：原声先停，两个声音不能叠在一起。
watch(
  () => state.pause.n,
  () => {
    auditionEnd = 0
    mediaEl.value?.pause()
  }
)

// 宿主播放条开始播（音色栏试听、连播队列）：预览停下。
watch(
  () => hostStore.player.playing,
  (playing) => {
    if (!playing) return
    auditionEnd = 0
    mediaEl.value?.pause()
  }
)

watch(
  () => project.value?.id,
  () => {
    state.playTime = 0
    state.playing = false
    scrubValue.value = 0
  }
)

watch(
  () => state.panelMax,
  () => {
    // 预览收起来时下区拿全部高度；展开后按记着的像素还原。
    window.requestAnimationFrame(() => layout())
  }
)

function onSelect(cue) {
  state.selectedCue = cue.id
  state.view = 'subtitle'
}
</script>

<style scoped>
.stage {
  --panel-h: 320px;
  display: grid;
  grid-template-rows: minmax(150px, 1fr) 6px var(--panel-h);
  min-height: 0;
  gap: 0;
}

/* 三行显式定位：放大时前两行被 `display:none` 摘掉，自动排布会把面板塞进第 1 行。 */
.preview { grid-row: 1; }
.split { grid-row: 2; }
.panel { grid-row: 3; }

.stage.is-max {
  grid-template-rows: 0 0 minmax(0, 1fr);
}

.stage.is-max .preview,
.stage.is-max .split {
  display: none;
}

.preview {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 0;
  padding: 2px 0 0;
}

/* 源文件丢了是错误状态，不是一句提示：盖在画面上，别让人点播放才发现放不出来。 */
.stage-warn {
  position: absolute;
  left: 10px;
  right: 10px;
  bottom: 10px;
  z-index: 2;
  margin: 0;
  padding: 7px 10px;
  border: 1px solid var(--sh-warn);
  border-radius: var(--sh-r-xs);
  background: var(--sh-warn-soft);
  color: var(--sh-warn);
  font-size: 11px;
  line-height: 1.5;
  text-align: center;
}

.frame {
  position: relative;
  flex: none;
  box-sizing: border-box;
  max-width: 100%;
  border-radius: var(--sh-r-md);
  overflow: hidden;
  background: var(--sh-chip);
  border: 1px solid var(--sh-line);
}

.frame.is-audio { background: var(--sh-field); }

.media {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: var(--sh-media-bg);
  cursor: pointer;
}

.media.is-hidden { display: none; }

.audio-face {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  cursor: pointer;
}

.audio-bars {
  display: flex;
  align-items: center;
  gap: 3px;
  height: 46%;
  width: 62%;
}

.audio-bars i {
  flex: 1;
  border-radius: 2px;
  background: var(--sh-accent);
  opacity: 0.35;
}

.audio-name {
  font-size: 11.5px;
  color: var(--sh-text-3);
  max-width: 70%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.frame-badge {
  position: absolute;
  left: 10px;
  top: 9px;
  padding: 2px 7px;
  border-radius: 5px;
  background: var(--sh-mask);
  color: var(--sh-media-text);
  font-size: 10px;
  letter-spacing: 0.04em;
  pointer-events: none;
}

.frame-play {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: var(--sh-glass-strong);
  color: var(--sh-text);
  box-shadow: var(--sh-shadow-pop);
  border: 1px solid var(--sh-glass-border);
  opacity: 0.92;
}

.frame-play:hover { opacity: 1; }

.frame-sub {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  max-width: 84%;
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 5px 12px;
  border-radius: 8px;
  background: var(--sh-media-scrim);
  color: var(--sh-media-text);
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
  pointer-events: none;
}

.frame-sub b {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--sh-media-sub);
}

.frame-sub span { word-break: break-word; }

/* ---- 走带 ---- */
.transport {
  flex: none;
  width: 100%;
  height: 34px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 2px;
}

.tp-play {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex: none;
  border-radius: 50%;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
}

.tp-time {
  flex: none;
  font-family: var(--sh-mono);
  font-size: 10.5px;
  color: var(--sh-text-2);
  font-variant-numeric: tabular-nums;
}

.tp-time span { color: var(--sh-text-4); }

.tp-bar {
  flex: 1;
  min-width: 60px;
  height: 16px;
  margin: 0;
  appearance: none;
  background: transparent;
  cursor: pointer;
}

.tp-bar::-webkit-slider-runnable-track {
  height: 4px;
  border-radius: 2px;
  background: linear-gradient(
    to right,
    var(--sh-accent) 0%,
    var(--sh-accent) var(--p, 0%),
    var(--sh-track) var(--p, 0%),
    var(--sh-track) 100%
  );
}

.tp-bar:hover::-webkit-slider-runnable-track {
  background: linear-gradient(
    to right,
    var(--sh-accent) 0%,
    var(--sh-accent) var(--p, 0%),
    var(--sh-line) var(--p, 0%),
    var(--sh-line) 100%
  );
}

.tp-bar::-webkit-slider-thumb {
  appearance: none;
  width: 11px;
  height: 11px;
  margin-top: -3.5px;
  border-radius: 50%;
  background: var(--sh-accent);
  border: 2px solid var(--sh-panel);
  box-shadow: var(--sh-shadow-1);
}

.tp-bar::-moz-range-track {
  height: 4px;
  border-radius: 2px;
  background: var(--sh-chip);
}

.tp-bar::-moz-range-progress {
  height: 4px;
  border-radius: 2px;
  background: var(--sh-accent);
}

.tp-bar::-moz-range-thumb {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--sh-accent);
  border: 2px solid var(--sh-panel);
}

.tp-bar:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 4px;
  border-radius: 4px;
}

.tp-overlay {
  flex: none;
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border-radius: 6px;
  background: var(--sh-chip);
}

.tp-overlay button {
  height: 20px;
  padding: 0 7px;
  border-radius: 4px;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.tp-overlay button.is-on {
  background: var(--sh-panel);
  color: var(--sh-text);
  font-weight: 600;
}

/* ---- 分隔条 ---- */
.split {
  position: relative;
  display: grid;
  place-items: center;
  cursor: row-resize;
  touch-action: none;
}

.split i {
  width: 44px;
  height: 3px;
  border-radius: 2px;
  background: var(--sh-line);
  transition: background var(--sh-t-fast) ease, width var(--sh-t-fast) ease;
}

.split:hover i,
.split:focus-visible i,
:global(body.is-row-resizing) .split i {
  width: 64px;
  background: var(--sh-accent-ring);
}

.split:focus-visible {
  outline: none;
}

.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-1);
  overflow: hidden;
}

.panel-bar {
  flex: none;
  height: 36px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px 0 8px;
  border-bottom: 1px solid var(--sh-line-soft);
}

.modes {
  display: inline-flex;
  padding: 2px;
  border-radius: 7px;
  background: var(--sh-chip);
  border: 1px solid var(--sh-line);
}

.modes button {
  height: 22px;
  padding: 0 10px;
  border-radius: 5px;
  font-size: 11.5px;
  color: var(--sh-text-3);
}

.modes button.is-on {
  background: var(--sh-panel);
  color: var(--sh-text);
  font-weight: 600;
  box-shadow: var(--sh-shadow-1);
}

.bar-hint {
  font-size: 10.5px;
  color: var(--sh-text-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-grow { flex: 1; }

.bar-toggle {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 5px;
  color: var(--sh-text-3);
}

.bar-toggle:hover { background: var(--sh-chip); color: var(--sh-accent); }

.panel-body {
  flex: 1;
  min-height: 0;
  padding: 6px 10px 8px;
}
</style>
