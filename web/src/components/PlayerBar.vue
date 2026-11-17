<template>
  <!-- 播放小窗：右下角一颗**圆钮**，鼠标移上去展开成一条坞。只在有东西可播时出现。
       收起时它只占一个 48px 的角、内容不为它让位；展开或固定后（见 theme.css 的
       `:has(.player-fold.is-open)`）才给各页滚动容器末尾留那一截。
       声音仍归隐藏的 <audio id="vh-player">，所以列表怎么重画都断不了播放。 -->
  <div
    ref="foldEl"
    class="player-fold"
    :class="{ 'is-open': open, 'is-pinned': pinned, 'is-playing': playing, 'is-dragging': dragging }"
    :data-side="side"
    :style="posStyle"
    @pointerenter="onHoverIn"
    @pointerleave="onHoverOut"
    @focusin="onFocusIn"
    @focusout="onFocusOut"
  >
    <!-- 圆钮：收起态唯一的入口。单击播放/暂停，双击固定，按住拖走，右键回默认位置。
         外面一圈是进度：不用展开也知道这段放到哪儿了。 -->
    <button
      ref="chipEl"
      type="button"
      class="pb-chip"
      :style="{ '--p': ratio }"
      :title="chipTitle"
      :aria-label="chipTitle"
      :aria-expanded="open"
      @pointerdown="startDrag"
      @click="onChipClick"
      @dblclick.prevent="togglePin"
      @contextmenu.prevent="resetPos"
    >
      <span class="pb-ring" aria-hidden="true" />
      <span class="pb-disc" aria-hidden="true">
        <Pause v-if="playing" :size="15" :stroke-width="1.8" fill="currentColor" />
        <Play v-else :size="15" :stroke-width="1.8" fill="currentColor" />
      </span>
    </button>

    <!-- 展开坞：左身份、正中传输、右边时间码 + 两颗动作键 + 关闭。
         没有"收起"键——鼠标离开即收起，圆钮本身就承担收起；进度条贴在坞的下沿。 -->
    <div ref="dockEl" class="pb-dock">
      <div class="pb-cell pb-cell-ident">
        <div class="pb-ident">
          <div class="pb-label" :title="label">{{ label }}</div>
          <div class="pb-sub">
            <span v-if="sub" :title="sub">{{ sub }}</span>
            <!-- 队列位置是"还有几条没听"的唯一说法；只有一条时它没有信息量。 -->
            <span v-if="multi" class="pb-count">{{ index + 1 }} / {{ queue.length }}</span>
          </div>
        </div>
      </div>

      <div class="pb-cell pb-cell-main">
        <!-- 上一段 / 下一段只在队列里真有好几条时出现。顺序照播放器老习惯：⏮ ▶ ⏭。 -->
        <button
          v-if="multi"
          class="pb-btn"
          type="button"
          title="上一段"
          aria-label="上一段"
          :disabled="index === 0"
          @click="actions.prevTrack()"
        >
          <SkipBack :size="15" :stroke-width="1.8" />
        </button>
        <button
          class="pb-btn play"
          type="button"
          :title="playing ? '暂停' : '播放'"
          :aria-label="playing ? '暂停' : '播放'"
          @click="actions.togglePlay()"
        >
          <Pause v-if="playing" :size="15" :stroke-width="1.8" fill="currentColor" />
          <Play v-else :size="15" :stroke-width="1.8" fill="currentColor" />
        </button>
        <button
          v-if="multi"
          class="pb-btn"
          type="button"
          title="下一段"
          aria-label="下一段"
          :disabled="index >= queue.length - 1"
          @click="actions.nextTrack()"
        >
          <SkipForward :size="15" :stroke-width="1.8" />
        </button>
      </div>

      <div class="pb-cell pb-cell-side">
        <!-- 加载不了就在坞上说（toast 会一闪而过）；此时没有进度可说，时间码让给它。 -->
        <span v-if="error" class="pb-error" :title="error">{{ error }}</span>
        <span v-else class="pb-time">{{ formatClock(position) }} / {{ formatClock(duration) }}</span>

        <!-- 正在放的是某条音色的样例时，这两件事在任何页面都能做：
             听到合适的声音就地收藏、就地指定给角色，不必先回创作页。 -->
        <template v-if="voice">
          <button
            type="button"
            class="pb-btn pb-act"
            :class="{ lit: favorite }"
            :title="favorite ? '取消收藏' : '收藏'"
            :aria-label="favorite ? '取消收藏' : '收藏'"
            @click="toggleFavorite"
          >
            <Star :size="14" :stroke-width="1.8" :fill="favorite ? 'currentColor' : 'none'" />
          </button>
          <button
            type="button"
            class="pb-btn pb-act"
            title="指定给角色"
            aria-label="指定给角色"
            @click="assignVoice"
          >
            <UserRoundPlus :size="14" :stroke-width="1.8" />
          </button>
        </template>

        <button
          type="button"
          class="pb-btn close"
          title="关掉播放条"
          aria-label="关掉播放条"
          @click="actions.stop()"
        >
          <X :size="14" :stroke-width="1.8" />
        </button>
      </div>

      <input
        class="pb-seek"
        type="range"
        min="0"
        :max="duration || 0"
        step="0.1"
        :value="position"
        :disabled="!duration"
        aria-label="播放进度"
        @input="onSeek"
        @pointermove="onSeekHover"
        @pointerleave="hoverAt = null"
      />
      <span class="pb-bub" :style="bubbleStyle">{{ bubbleText }}</span>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
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

const label = computed(() => store.player.label)
const sub = computed(() => store.player.sub)
const position = computed(() => store.player.position || 0)
const duration = computed(() => store.player.duration || 0)
const playing = computed(() => store.player.playing)
const error = computed(() => store.player.error)
const queue = computed(() => store.player.queue || [])
const index = computed(() => store.player.index || 0)
const multi = computed(() => queue.value.length > 1)
const ratio = computed(() =>
  duration.value ? Math.min(1, Math.max(0, position.value / duration.value)) : 0
)

/** 这条音频属于哪个音色：只有音色样例带（行音频、章节成品不带），
 *  带上了才谈得上「收藏」「指定」。 */
const voice = computed(() => queue.value[index.value]?.voice || null)
const favorite = computed(() => Boolean(voice.value?.favorite))

async function toggleFavorite() {
  if (!voice.value) return
  await actions.setPrefs(voice.value, { favorite: !favorite.value })
}

function assignVoice() {
  if (!voice.value) return
  actions.requestAssign(voice.value)
}

/* ---- 圆钮与坞 ----
   展开/收起归这里（悬停与聚焦驱动），固定态存 localStorage：
   刷新与换页都还在，且收起时内容不为它让位。 */
const GRACE = 260 // 从圆钮挪进展开条那段路：不留宽限，斜着走一下闪回
const DRAG_MIN = 4 // 位移超过它就不算点击，免得挪位置顺手把声音按停

const POS_KEY = 'vh.player.pos'
const PIN_KEY = 'vh.player.pinned'

const foldEl = ref(/** @type {any} */ (null))
const chipEl = ref(/** @type {any} */ (null))
const dockEl = ref(/** @type {any} */ (null))
const open = ref(false)
const pinned = ref(readBoolean(PIN_KEY))
const dragging = ref(false)
const side = ref('l')
const hoverAt = ref(/** @type {any} */ (null))
const pos = ref(readPosition())

const chipTitle = computed(() =>
  pinned.value
    ? '已固定：单击播放/暂停，双击取消固定（可拖动，右键回到默认位置）'
    : '单击播放/暂停，双击固定（可拖动，右键回到默认位置）'
)

/** 内联 left/top 只在拖过之后才有：没拖过时由 theme.css 贴右下角。 */
const posStyle = computed(() =>
  pos.value
    ? { left: `${pos.value.left}px`, top: `${pos.value.top}px`, right: 'auto', bottom: 'auto' }
    : {}
)

const bubbleStyle = computed(() =>
  hoverAt.value === null ? {} : { '--hx': hoverAt.value }
)
const bubbleText = computed(() =>
  formatClock(hoverAt.value === null ? 0 : hoverAt.value * duration.value)
)

/**
 * @param {string} key
 */
function readBoolean(key) {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

function readPosition() {
  try {
    const raw = localStorage.getItem(POS_KEY)
    if (!raw) return null
    const value = JSON.parse(raw)
    if (Number.isFinite(value?.left) && Number.isFinite(value?.top)) return value
  } catch {
    // 坏值当没存过：一颗挪不动的播放条比回到默认位置更糟。
  }
  return null
}

function savePosition() {
  try {
    if (pos.value) localStorage.setItem(POS_KEY, JSON.stringify(pos.value))
  } catch {
    // 存不了就只这次生效——不影响播放。
  }
}

/* 没有悬停可用（触屏/手写笔）时，点圆钮只管展开与收起：坞没展开之前，
   播放键在触屏上是够不着的，把这一次点击让给展开比让给播放有用。 */
const canHover = window.matchMedia('(hover: hover)')
const fitsDesktop = window.matchMedia('(min-width: 721px)')

/** @type {ReturnType<typeof setTimeout> | undefined} */
let grace

function wantOpen() {
  clearTimeout(grace)
  open.value = true
}

function wantClose() {
  clearTimeout(grace)
  if (pinned.value) return
  grace = setTimeout(() => {
    open.value = false
  }, GRACE)
}

const onHoverIn = () => canHover.matches && wantOpen()
const onHoverOut = () => canHover.matches && wantClose()
const onFocusIn = () => canHover.matches && wantOpen()
const onFocusOut = () => canHover.matches && wantClose()

/** 单击只改播放状态，绝不动坞的结构：坞整块重画会换掉圆钮的节点，
 *  第二下就等不到 dblclick 了（固定也就跟着失效）。 */
function onChipClick() {
  if (suppress) return
  if (canHover.matches) {
    actions.togglePlay()
    return
  }
  open.value = !open.value
}

function togglePin() {
  if (suppress) return
  pinned.value = !pinned.value
  try {
    if (pinned.value) localStorage.setItem(PIN_KEY, '1')
    else localStorage.removeItem(PIN_KEY)
  } catch {
    // 存不了就只这次生效。
  }
  if (pinned.value) wantOpen()
  else wantClose()
}

/**
 * 触屏展开之后，点坞外面的任何地方收起：没有 hover 可以离开。
 *
 * @param {any} event
 */
function onOutsidePointer(event) {
  if (canHover.matches || !open.value || pinned.value) return
  if (foldEl.value?.contains(event.target)) return
  open.value = false
}

/* ---- 坞朝哪边伸 ----
   圆钮拖到靠左时，坞改朝右伸，别顶出窗口。窄屏它是贴底横条，没有翻转方向一说。 */
function updateSide() {
  if (!fitsDesktop.matches) {
    side.value = 'l'
    return
  }
  const chip = chipEl.value
  const dock = dockEl.value
  if (!chip || !dock) return
  const rect = chip.getBoundingClientRect()
  const roomLeft = rect.left - 12
  const roomRight = window.innerWidth - rect.right - 12
  side.value = roomLeft < dock.offsetWidth && roomRight >= roomLeft ? 'r' : 'l'
}

/* ---- 拖动：整颗圆钮跟着指针走，位置存 localStorage，换页不断声也不回弹 ---- */
const drag = ref(/** @type {any} */ (null))
let suppress = false

/**
 * @param {any} left
 * @param {any} top
 * @param {any} width
 * @param {any} height
 */
function clampToViewport(left, top, width, height) {
  const maxLeft = Math.max(0, window.innerWidth - width)
  const maxTop = Math.max(0, window.innerHeight - height)
  return {
    left: Math.min(Math.max(0, left), maxLeft),
    top: Math.min(Math.max(0, top), maxTop),
  }
}

/**
 * @param {any} event
 */
function startDrag(event) {
  if (event.button !== 0 || !fitsDesktop.matches) return
  const el = chipEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  // 先把当前位置固化成 left/top：不这么做，第一次拖动会从默认位置直接跳到指针下。
  pos.value = { left: rect.left, top: rect.top }
  drag.value = {
    pointerId: event.pointerId,
    x0: event.clientX,
    y0: event.clientY,
    moved: false,
    dx: event.clientX - rect.left,
    dy: event.clientY - rect.top,
    width: rect.width,
    height: rect.height,
  }
  suppress = false
  event.currentTarget.setPointerCapture?.(event.pointerId)
  window.addEventListener('pointermove', onDrag)
  window.addEventListener('pointerup', endDrag)
}

/**
 * @param {any} event
 */
function onDrag(event) {
  const current = drag.value
  if (!current || event.pointerId !== current.pointerId) return
  if (!current.moved) {
    if (Math.hypot(event.clientX - current.x0, event.clientY - current.y0) < DRAG_MIN) return
    current.moved = true
    dragging.value = true
  }
  pos.value = clampToViewport(
    event.clientX - current.dx,
    event.clientY - current.dy,
    current.width,
    current.height
  )
  // 等这一帧的位置真落到 DOM 上再量朝向：同步量读到的是上一帧那颗圆钮。
  void nextTick(updateSide)
}

/**
 * @param {any} event
 */
function endDrag(event) {
  const current = drag.value
  if (!current || event.pointerId !== current.pointerId) return
  dragging.value = false
  if (current.moved) {
    // 这一次按下是"挪位置"，不是"按播放/固定"。
    suppress = true
    wantClose()
    savePosition()
    void nextTick(updateSide)
  }
  drag.value = null
  window.removeEventListener('pointermove', onDrag)
  window.removeEventListener('pointerup', endDrag)
}

/** 右键：回到 CSS 给的默认右下角（双击已经让给"固定"了）。 */
function resetPos() {
  pos.value = null
  try {
    localStorage.removeItem(POS_KEY)
  } catch {
    // 存不了就只这次生效。
  }
  // 等样式真的撤掉再量方向：这时读到的才是回位之后的那颗圆钮。
  nextTick(updateSide)
}

function onResize() {
  if (!pos.value || !foldEl.value) return
  const rect = foldEl.value.getBoundingClientRect()
  pos.value = clampToViewport(pos.value.left, pos.value.top, rect.width, rect.height)
  updateSide()
}

onMounted(async () => {
  window.addEventListener('resize', onResize)
  document.addEventListener('pointerdown', onOutsidePointer, true)
  open.value = pinned.value
  await nextTick()
  onResize()
})

onBeforeUnmount(() => {
  clearTimeout(grace)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('pointermove', onDrag)
  window.removeEventListener('pointerup', endDrag)
  document.removeEventListener('pointerdown', onOutsidePointer, true)
})

/**
 * 用 `input` 而不是 `change`：松手才动的话，滑块会被 timeupdate 一直拽回原处。
 *
 * @param {any} event
 */
function onSeek(event) {
  actions.seek(event.target.value)
}

/**
 * @param {any} event
 */
function onSeekHover(event) {
  const rect = event.target.getBoundingClientRect()
  if (!rect.width) return
  hoverAt.value = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
}
</script>
