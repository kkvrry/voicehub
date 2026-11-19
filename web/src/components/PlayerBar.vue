<template>
  <!-- 播放条：嵌在顶栏左侧，只在有东西可播时出现（`store.player.url`）。
       一行到底——传输键、身份、进度、开卡与收条，没有第二层。声音归隐藏的
       `<audio id="vh-player">`，所以换页与列表重画都断不了它。
       身份只写音色名与样例位置：样例的台词属于详情抽屉，不搬到这里。 -->
  <div class="pb-bar" :class="{ 'is-playing': playing }" role="group" aria-label="正在播放">
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

    <span class="pb-ident">
      <span class="pb-name" :title="identTitle">{{ name }}</span>
      <!-- 样例位置：这条音色有多段时才知道"还有几条没听"。 -->
      <span v-if="multi" class="pb-count">{{ index + 1 }} / {{ queue.length }}</span>
    </span>

    <!-- 加载失败时没有进度可说，位置让给原因（完整的一句在 title 里）。 -->
    <span v-if="error" class="pb-error" :title="error">{{ error }}</span>
    <!-- 下沿那条线分三层：轨道、进度填充（带流光）、透明的手柄层。
         填充宽度只认 `--p`，动画不碰布局。 -->
    <span v-else class="pb-track" :style="{ '--p': ratio }" aria-hidden="true">
      <span class="pb-fill" />
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

    <!-- 正在放的是一条音色的样例时，这几件事在任何页面都能做：
         听到合适的声音就地收藏、就地指定给角色，或打开它的卡。 -->
    <template v-if="voice">
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
        title="指定给角色"
        aria-label="指定给角色"
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
    </template>

    <button
      type="button"
      class="pb-btn is-close"
      title="关掉播放条"
      aria-label="关掉播放条"
      @click="actions.stop()"
    >
      <X :size="16" :stroke-width="1.8" />
    </button>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
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
import { formatClock } from '../utils/format'

const position = computed(() => store.player.position || 0)
const duration = computed(() => store.player.duration || 0)
const playing = computed(() => store.player.playing)
const error = computed(() => store.player.error)
const queue = computed(() => store.player.queue || [])
const index = computed(() => store.player.index || 0)
const multi = computed(() => queue.value.length > 1)
/** 下沿那条线画到哪儿：`--p` 同时给轨道底色与拇指位置，不靠第二个元素。 */
const ratio = computed(() =>
  duration.value ? `${Math.min(100, (position.value / duration.value) * 100)}%` : '0%'
)

/** 这条音频属于哪个音色：只有音色样例带（行音频、章节成品不带）。 */
const voice = computed(() => queue.value[index.value]?.voice || null)
const favorite = computed(() => Boolean(voice.value?.favorite))
/** 身份只给两样：音色名（没有音色时退回曲目标题）与样例位置。 */
const name = computed(() => voice.value?.shown_name || store.player.label)
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
