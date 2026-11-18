<template>
  <section class="stage">
    <div class="preview">
      <div ref="frameEl" class="frame" :class="{ 'is-audio': !isVideo }">
        <video
          v-if="isVideo"
          ref="mediaEl"
          class="media"
          :src="sourceUrl"
          preload="metadata"
          playsinline
          @timeupdate="onTime"
          @play="state.playing = true"
          @pause="state.playing = false"
          @ended="state.playing = false"
        ></video>
        <audio
          v-else
          ref="mediaEl"
          class="media is-hidden"
          :src="sourceUrl"
          preload="metadata"
          @timeupdate="onTime"
          @play="state.playing = true"
          @pause="state.playing = false"
          @ended="state.playing = false"
        ></audio>

        <div v-if="!isVideo" class="audio-face">
          <div class="audio-bars" aria-hidden="true">
            <i v-for="n in 48" :key="n" :style="{ height: `${barHeight(n)}%` }"></i>
          </div>
          <span class="audio-name">{{ project?.source_name }}</span>
        </div>

        <span class="frame-badge">{{ project?.kind === 'video' ? '预览' : '音频' }} · {{ durationLabel }}</span>

        <div class="frame-overlay" role="group" aria-label="字幕叠加">
          <button
            v-for="item in OVERLAYS"
            :key="item.key"
            type="button"
            :class="{ 'is-on': state.overlay === item.key }"
            @click="state.overlay = item.key"
          >{{ item.label }}</button>
        </div>

        <button class="frame-play" type="button" title="播放 / 暂停" aria-label="播放 / 暂停" @click="toggle">
          <Pause v-if="state.playing" :size="18" :stroke-width="1.8" />
          <Play v-else :size="18" :stroke-width="1.8" />
        </button>

        <div v-if="state.overlay !== 'off' && currentCue" class="frame-sub">
          <b v-if="state.overlay === 'role'">{{ roleName(currentCue) }}</b>
          <span>{{ currentCue.text }}</span>
        </div>
      </div>
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
        <button class="bar-play" type="button" title="播放 / 暂停" aria-label="播放 / 暂停" @click="toggle">
          <Pause v-if="state.playing" :size="12" :stroke-width="2" />
          <Play v-else :size="12" :stroke-width="2" />
        </button>
        <span class="bar-time">{{ clock }}<span> / {{ durationLabel }}</span></span>
        <span class="bar-grow"></span>
        <span class="bar-hint">{{ hint }}</span>
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
// 中列：预览 + 下区。下区是**一块共用区域**，时间轴与字幕切换显示，
// 不并排常显——各占一半高度时两边都看不清。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Pause, Play } from 'lucide-vue-next'
import SubtitleTable from './SubtitleTable.vue'
import TrackTimeline from './TrackTimeline.vue'
import { cues, project, roleName, seekTo, state } from '../store'
import { mediaUrl } from '../api'

const VIEWS = [
  { key: 'timeline', label: '时间轴' },
  { key: 'seg', label: '字幕' },
]

const OVERLAYS = [
  { key: 'off', label: '关' },
  { key: 'text', label: '文本' },
  { key: 'role', label: '文本 + 角色' },
]

const mediaEl = ref(null)
const frameEl = ref(null)

const isVideo = computed(() => project.value?.kind === 'video')
const sourceUrl = computed(() => (project.value ? mediaUrl(project.value.id, 'source') : ''))

const durationLabel = computed(() => {
  const total = Math.round((project.value?.duration_ms || 0) / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
})

const clock = computed(() => {
  const total = Math.max(0, Math.round(state.playTime * 1000))
  const minutes = Math.floor(total / 60000)
  const seconds = Math.floor((total % 60000) / 1000)
  const millis = total % 1000
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')},${String(millis).padStart(3, '0')}`
})

const currentCue = computed(() => {
  const time = state.playTime * 1000
  return cues.value.find((cue) => time >= cue.start_ms && time < cue.end_ms) || null
})

const hint = computed(() => {
  if (state.view === 'seg') {
    const stats = state.detail?.stats || { total: 0, ready: 0 }
    return `${stats.total} 段 · ${stats.ready} 已生成 · 失焦即提交`
  }
  return `${cues.value.length} 段 · ${project.value ? '播放头随预览' : '无项目'}`
})

function barHeight(n) {
  // 纯装饰，不表示真实波形：真波形要走服务端峰值，那属于下一版。
  const wave = Math.abs(Math.sin(n * 1.7) * Math.cos(n * 0.43))
  return 18 + Math.round(wave * 74)
}

let frameObserver = null

function fitFrame() {
  const box = frameEl.value?.parentElement
  if (!box) return
  const w = box.clientWidth
  const h = box.clientHeight
  if (w <= 0 || h <= 0) return
  const width = Math.min(w, (h * 16) / 9)
  frameEl.value.style.width = `${width}px`
  // 高度自己定：纯音频项目里没有 <video> 撑开这个盒子，
  // 只给宽度会让整个预览区塌成 0 高。
  frameEl.value.style.height = `${(width * 9) / 16}px`
}

onMounted(() => {
  fitFrame()
  frameObserver = new ResizeObserver(fitFrame)
  if (frameEl.value?.parentElement) frameObserver.observe(frameEl.value.parentElement.parentElement || frameEl.value.parentElement)
})

onBeforeUnmount(() => {
  frameObserver?.disconnect()
  mediaEl.value?.pause()
})

watch(
  () => project.value?.id,
  () => {
    state.playTime = 0
    state.playing = false
  }
)

function onTime(event) {
  state.playTime = event.target.currentTime || 0
}

function toggle() {
  const media = mediaEl.value
  if (!media) return
  if (media.paused) void media.play()
  else media.pause()
}

function seek(seconds) {
  seekTo(seconds)
}

// 段落表与时间轴都会要求跳转：媒体只有这一份，由它执行。
watch(
  () => state.seek.n,
  () => {
    const media = mediaEl.value
    if (media) media.currentTime = state.seek.at
  }
)

function onSelect(cue) {
  state.selectedCue = cue.id
  state.view = 'seg'
}
</script>

<style scoped>
.stage {
  display: grid;
  grid-template-rows: minmax(0, 1fr) 316px;
  min-height: 0;
  gap: 8px;
}

.preview {
  display: grid;
  place-items: center;
  min-height: 0;
  padding: 2px 0;
}

.frame {
  position: relative;
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
  background: #0d1420;
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
  color: #fff;
  font-size: 10px;
  letter-spacing: 0.04em;
}

.frame-overlay {
  position: absolute;
  right: 9px;
  top: 8px;
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: 7px;
  background: var(--sh-mask);
}

.frame-overlay button {
  height: 20px;
  padding: 0 7px;
  border-radius: 5px;
  font-size: 10.5px;
  color: rgba(255, 255, 255, 0.72);
}

.frame-overlay button.is-on {
  background: rgba(255, 255, 255, 0.9);
  color: #16233a;
  font-weight: 600;
}

.frame-play {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--sh-glass-strong);
  color: var(--sh-text);
  box-shadow: var(--sh-shadow-pop);
  border: 1px solid var(--sh-glass-border);
}

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
  background: rgba(8, 12, 20, 0.68);
  color: #fff;
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
}

.frame-sub b {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: #9ec5ff;
}

.frame-sub span { word-break: break-word; }

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
  height: 38px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px 0 8px;
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
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12);
}

.bar-play {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast, #fff);
}

.bar-time {
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 10.5px;
  color: var(--sh-text-2);
  font-variant-numeric: tabular-nums;
}

.bar-time span { color: var(--sh-text-4); }

.bar-grow { flex: 1; }

.bar-hint {
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.panel-body {
  flex: 1;
  min-height: 0;
  padding: 8px 12px 10px;
}
</style>
