<template>
  <!-- 播放条：顶栏中缝上的一只“涨潮块”，只在有东西可播时出现（`store.player.url`）。
       块里一行到底——身份、传输键、开卡与收条，没有第二层；底色本身就是进度，
       从左侧涨到当前时间，前沿一道水线。声音归隐藏的 `<audio id="vh-player">`，
       所以换页与列表重画都断不了它。
       身份只写音色名与样例位置：样例的台词属于详情抽屉，不搬到这里。 -->
  <div
    ref="barEl"
    class="pb-bar"
    :class="{ 'is-playing': playing }"
    role="group"
    aria-label="正在播放"
  >
    <!-- 涨潮层：只负责底色与前沿水线；文字和按钮压在它上面。 -->
    <span v-if="!error" class="pb-tide" aria-hidden="true"></span>

    <span class="pb-ident">
      <span class="pb-name" :title="identTitle">{{ name }}</span>
      <!-- 样例位置：这条音色有多段时才知道"还有几条没听"。加载失败时让位给原因。 -->
      <span v-if="multi && !error" class="pb-count">{{ index + 1 }} / {{ queue.length }}</span>
      <!-- 加载失败时没有进度可说，原因贴在身份下面。 -->
      <span v-if="error" class="pb-error" :title="error">{{ error }}</span>
    </span>

    <span class="pb-transport">
      <button
        v-if="multi"
        type="button"
        class="pb-btn"
        title="上一段"
        aria-label="上一段"
        :disabled="index === 0"
        @click="actions.prevTrack()"
      >
        <SkipBack :size="16" :stroke-width="1.8" />
      </button>

      <button
        type="button"
        class="pb-btn is-play"
        :title="playing ? '暂停' : '播放'"
        :aria-label="playing ? '暂停' : '播放'"
        @click="actions.togglePlay()"
      >
        <Pause v-if="playing" :size="16" :stroke-width="1.8" fill="currentColor" />
        <Play v-else :size="16" :stroke-width="1.8" fill="currentColor" />
      </button>

      <button
        v-if="multi"
        type="button"
        class="pb-btn"
        title="下一段"
        aria-label="下一段"
        :disabled="index >= queue.length - 1"
        @click="actions.nextTrack()"
      >
        <SkipForward :size="16" :stroke-width="1.8" />
      </button>
    </span>

    <input
      v-if="!error"
      class="pb-seek"
      type="range"
      min="0"
      :max="duration || 0"
      step="0.1"
      :value="position"
      :disabled="!duration"
      :title="clock"
      aria-label="播放进度"
      @input="onSeek"
    />

    <span class="pb-right">
      <!-- 正在放的是一条音色的样例时，这几件事在任何页面都能做：
           听到合适的声音就地收藏、就地指定给角色或章节，或打开它的卡。 -->
      <span v-if="voice" class="pb-actions">
        <button
          type="button"
          class="pb-btn is-act"
          :class="{ lit: favorite }"
          :title="favorite ? '取消收藏' : '收藏'"
          :aria-label="favorite ? '取消收藏' : '收藏'"
          @click="toggleFavorite"
        >
          <Star :size="16" :stroke-width="1.8" :fill="favorite ? 'currentColor' : 'none'" />
        </button>
        <button
          type="button"
          class="pb-btn is-act"
          :title="`指定给${store.assignTarget || '角色'}`"
          :aria-label="`指定给${store.assignTarget || '角色'}`"
          @click="assignVoice"
        >
          <UserRoundPlus :size="16" :stroke-width="1.8" />
        </button>
        <button
          type="button"
          class="pb-btn is-card"
          title="打开音色卡"
          aria-label="打开音色卡"
          @click="openCard"
        >
          <IdCard :size="16" :stroke-width="1.8" />
        </button>
      </span>

      <button
        type="button"
        class="pb-btn is-close"
        title="关掉播放条"
        aria-label="关掉播放条"
        @click="actions.stop()"
      >
        <X :size="16" :stroke-width="1.8" />
      </button>
    </span>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  IdCard,
  Pause,
  Play,
  SkipBack,
  SkipForward,
  Star,
  UserRoundPlus,
  X,
} from 'lucide-vue-next'
import { store, actions } from '../store'
import { liveFraction } from '../store/playback'
import { formatClock } from '../utils/format'

const position = computed(() => store.player.position || 0)
const duration = computed(() => store.player.duration || 0)
const playing = computed(() => store.player.playing)
const error = computed(() => store.player.error)
const queue = computed(() => store.player.queue || [])
const index = computed(() => store.player.index || 0)
const multi = computed(() => queue.value.length > 1)

/** 涨潮的画笔：`timeupdate` 只有约 4Hz，整块底色跟着它走会一跳一跳。
 *  播放时逐帧读 `<audio>` 的真实位置，把 `--p` 直接写到根节点上；
 *  暂停、拖动与换曲时再由 watcher 落一次。 */
/** @type {import('vue').Ref<HTMLElement | null>} */
const barEl = ref(null)
let frame = 0

function paint() {
  if (!barEl.value) return
  barEl.value.style.setProperty('--p', liveFraction().toFixed(4))
}

function tick() {
  paint()
  frame = window.requestAnimationFrame(tick)
}

function start() {
  if (frame) return
  frame = window.requestAnimationFrame(tick)
}

function stop() {
  if (frame) {
    window.cancelAnimationFrame(frame)
    frame = 0
  }
  paint()
}

onMounted(() => {
  paint()
  if (playing.value) start()
})

onBeforeUnmount(() => {
  if (frame) window.cancelAnimationFrame(frame)
})

watch(playing, (value) => {
  if (value) start()
  else stop()
})

// 暂停时拖动、换曲与元数据到达不会有逐帧循环，补一次静态落点。
watch(
  () => [store.player.url, store.player.position, store.player.duration],
  () => {
    if (!playing.value) paint()
  }
)

/** 这条音频属于哪个音色：只有音色样例带（行音频、章节成品不带）。 */
const voice = computed(() => queue.value[index.value]?.voice || null)
const favorite = computed(() => Boolean(voice.value?.favorite))
/** 身份只给两样：曲目标题（样例是文件名，音色卡队列是音色名）与样例位置。 */
const name = computed(() => store.player.label || voice.value?.shown_name || '')
const identTitle = computed(() =>
  [name.value, store.player.sub].filter(Boolean).join(' · ')
)
const clock = computed(() => `${formatClock(position.value)} / ${formatClock(duration.value)}`)

async function toggleFavorite() {
  if (!voice.value) return
  await actions.setPrefs(voice.value, { favorite: !favorite.value })
}

function assignVoice() {
  if (!voice.value) return
  actions.requestAssign(voice.value)
}

async function openCard() {
  if (!voice.value) return
  // 抽屉只长在非设置页；设置屏里点它先回音色库，否则按下去没有反应。
  if (store.view === 'settings') await actions.openLibrary()
  await actions.openDetail(voice.value)
}

/**
 * 用 `input` 而不是 `change`：松手才动的话，滑块会被 timeupdate 一直拽回原处。
 *
 * @param {any} event
 */
function onSeek(event) {
  actions.seek(event.target.value)
}
</script>
