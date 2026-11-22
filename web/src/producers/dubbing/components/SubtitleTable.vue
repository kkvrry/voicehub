<template>
  <div class="seg-wrap">
    <div class="seg-tools">
      <div class="seg-search">
        <Search :size="14" :stroke-width="1.8" aria-hidden="true" />
        <input
          v-model="filter.text"
          type="search"
          class="seg-search-input"
          placeholder="筛文本或角色"
          aria-label="筛选段落文本或角色"
          @keydown.esc.prevent="filter.text = ''"
        />
        <button
          v-if="filter.text"
          type="button"
          class="seg-search-clear"
          title="清空筛选"
          aria-label="清空筛选"
          @click="filter.text = ''"
        >
          <X :size="12" :stroke-width="1.8" />
        </button>
      </div>
      <select v-model="filter.role" class="sh-field seg-filter" aria-label="按角色筛选">
        <option value="">全部角色</option>
        <option v-for="item in roles" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
      <select v-model="filter.status" class="sh-field seg-filter" aria-label="按状态筛选">
        <option value="">全部状态</option>
        <option value="ready">已配音</option>
        <option value="stale">待重配</option>
        <option value="empty">未配音</option>
        <option value="failed">失败</option>
        <option value="keep">原声</option>
        <option value="unsaved">未保存</option>
      </select>
      <button
        type="button"
        class="seg-play"
        :disabled="!canPlay"
        :title="queueHint"
        :aria-label="playWhole ? '从头播放导出结果' : '按顺序播放当前筛选的段落'"
        @click.stop="playFiltered"
      >
        <Play :size="14" :stroke-width="1.8" />
      </button>
      <span class="seg-count">{{ filtered.length }} / {{ cues.length }}</span>
      <button
        v-if="filtering"
        type="button"
        class="seg-clear-filters"
        title="清除筛选"
        @click="clearFilter"
      >
        清除
      </button>
    </div>

    <div v-if="state.selectedIds.length" class="seg-batch" role="toolbar" aria-label="批量操作">
      <span class="batch-count">已选 <b>{{ state.selectedIds.length }}</b> 段</span>
      <label class="batch-role">
        <span>改为角色</span>
        <select
          v-model="batchRole"
          class="sh-field"
          aria-label="把选中的段落改为某个角色"
          @change="onBatchRole(batchRole)"
        >
          <option value="" disabled>选择角色…</option>
          <option v-for="item in roles" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <button
        type="button"
        class="sh-btn is-sm is-soft"
        :disabled="!synthTargets || Boolean(state.busy)"
        :title="synthTargets ? `重新合成选中的 ${synthTargets} 段` : '选中的段落里没有可合成的段'"
        @click="onSynthSelected"
      >
        <Mic :size="14" :stroke-width="1.8" />
        重新配音
      </button>
      <button
        type="button"
        class="sh-btn is-sm is-ghost batch-danger"
        :disabled="Boolean(state.busy)"
        @click="onDeleteSelected"
      >
        <Trash2 :size="14" :stroke-width="1.8" />
        删除
      </button>
      <button type="button" class="seg-clear-filters" @click="clearPicks">取消选择</button>
    </div>

    <div class="seg-head" role="row">
      <button
        type="button"
        class="c-idx idx-all"
        role="checkbox"
        :aria-checked="allVisibleSelected ? 'true' : someVisibleSelected ? 'mixed' : 'false'"
        :title="allVisibleSelected ? '取消全选' : '全选当前筛选结果'"
        aria-label="全选当前筛选结果"
        @click="toggleAll"
      >
        <span>#</span>
      </button>
      <span class="c-tc">起止</span>
      <span class="c-cast">角色</span>
      <span class="c-text">文本</span>
      <span class="c-st">状态</span>
      <span class="c-act"></span>
    </div>

    <div ref="scrollEl" class="seg-scroll" tabindex="0" @scroll="onScroll" @keydown="onKey">
      <div :style="{ height: `${padTop}px` }"></div>
      <SubtitleRow
        v-for="cue in visible"
        :key="cue.id"
        :cue="cue"
        :role="roleOf(cue)"
        :roles="roles"
        :selected="cue.id === state.selectedCue"
        :picked="state.selectedIds.includes(cue.id)"
        :pending="Boolean(state.pending[cue.id])"
        :playing="voicePlaying === cue.id"
        :original-playing="originalPlaying === cue.id"
        @pick="pick"
        @check="onCheck"
        @select="select"
        @menu="onRowMenu"
        @audition="audition"
        @patch="onPatch"
      />
      <div :style="{ height: `${padBottom}px` }"></div>
      <p v-if="!cues.length" class="seg-empty">
        还没有段落。先做一次转录，或导入现成的 SRT / VTT 字幕。
      </p>
      <p v-else-if="!filtered.length" class="seg-empty">
        没有符合当前筛选的段落。
      </p>
    </div>
  </div>
</template>

<script setup>
// @ts-check
// 段落表：一行一段，文本与角色/音色都在行内改，行上还能分别试听原声与配音。
//
// 行高固定（52px）才能虚拟滚动：几千段时不逐段建 DOM。文本最多显示两行，
// 再长就在框内滚。筛选与多选都在表头之上，不挤进每一行。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Mic, Play, Search, Trash2, X } from 'lucide-vue-next'
import { actions as hostActions, store as hostStore } from '../../../store'
import SubtitleRow from './SubtitleRow.vue'
import {
  applyRoleToSelection,
  auditionCue,
  clearPicks,
  cues,
  deleteSelection,
  mergeCue,
  patchCue,
  pausePreview,
  pickRange,
  removeCue,
  roles,
  seekTo,
  setPicks,
  splitCue,
  state,
  synthOne,
  synthSelection,
  togglePick,
} from '../store'
import * as api from '../api'
import {
  canTakeOver,
  closeContextMenu,
  menuState,
  openObjectMenu,
} from '../../../composables/contextMenu'

const ROW_H = 52
const OVERSCAN = 6

const scrollEl = ref(/** @type {HTMLElement | null} */ (null))
const scrollTop = ref(0)
const viewHeight = ref(320)
/** 正在放的那一段配音（一次只放一条）。 */
const voicePlaying = ref('')
let voiceEl = /** @type {HTMLAudioElement | null} */ (null)
/** 正在放的那一版音频的地址要素：文件被重配或被移除时先停下。 */
let playingVersion = ''

const filter = reactive({ text: '', role: '', status: '' })
const batchRole = ref('')

const filtering = computed(() =>
  Boolean(filter.text.trim() || filter.role || filter.status)
)

/** @param {any} cue */
function cueRoleId(cue) {
  return cue.role_id || roles.value[0]?.id || ''
}

/** @param {any} cue */
function cueStatus(cue) {
  if (state.pending[cue.id]) return 'unsaved'
  if (!cue.dub) return 'keep'
  if (cue.state === 'failed') return 'failed'
  if (cue.state === 'stale') return 'stale'
  if (cue.state === 'ready' && cue.audio) return 'ready'
  return 'empty'
}

/** @param {any} cue */
function roleNameOf(cue) {
  return roles.value.find((/** @type {any} */ item) => item.id === cueRoleId(cue))?.name || '旁白'
}

const filtered = computed(() => {
  const words = filter.text.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return cues.value.filter((/** @type {any} */ cue) => {
    if (filter.role && cueRoleId(cue) !== filter.role) return false
    if (filter.status && cueStatus(cue) !== filter.status) return false
    if (!words.length) return true
    const hay = `${cue.text || ''} ${roleNameOf(cue)}`.toLowerCase()
    return words.every((word) => hay.includes(word))
  })
})

const paths = computed(() => state.detail?.paths || {})

/** 没筛选时播成片混音；筛选后按筛选结果逐段播。 */
const playWhole = computed(() => !filtering.value && Boolean(paths.value.mix))

const wholeTracks = computed(() => {
  if (!playWhole.value || !state.currentId) return []
  return [
    {
      url: api.mediaUrl(state.currentId, 'mix', '', paths.value.mix_version || ''),
      label: state.detail?.project?.name || '配音成片',
      sub: '成片混音 · 从头播放',
    },
  ]
})

/**
 * 当前筛选结果的连播队列。
 *
 * 配音段放这一段的产物；原声段（含改回原声但还留着旧配音的段）放源音轨的
 * 对应区间——界面上的「原声」就是最终成片里那一段实际会发出的声音。
 */
const queueTracks = computed(() => {
  const id = state.currentId
  if (!id) return []
  const original = paths.value.original ? api.mediaUrl(id, 'original') : ''
  return filtered.value.flatMap((/** @type {any} */ cue) => {
    const label = (cue.text || '').slice(0, 24) || `第 ${cue.ordinal} 段`
    if (cue.dub && cue.audio && cue.audio_ok !== false) {
      const version = cue.audio_version || (cue.audio_key || '').slice(0, 10)
      return [
        {
          url: api.mediaUrl(id, 'cue', cue.id, version),
          label,
          sub: `${roleNameOf(cue)} · 第 ${cue.ordinal} 段 · 配音`,
        },
      ]
    }
    if (!original) return []
    const start = Math.max(0, Number(cue.start_ms) || 0) / 1000
    const end = Math.max(0, Number(cue.end_ms) || 0) / 1000
    if (end <= start) return []
    return [
      {
        url: original,
        start,
        end,
        label,
        sub: `${roleNameOf(cue)} · 第 ${cue.ordinal} 段 · 原声`,
      },
    ]
  })
})

const canPlay = computed(() => playWhole.value || queueTracks.value.length > 0)

const queueHint = computed(() => {
  if (playWhole.value) {
    return paths.value.mix_current === false
      ? '从开头播放上一次成片混音（有改动，重新贴齐后更新）'
      : '从开头播放导出结果（成片混音）'
  }
  if (queueTracks.value.length) {
    return filtering.value
      ? `按顺序播放当前筛选的 ${queueTracks.value.length} 段，原声段放源音轨`
      : `按顺序播放全部 ${queueTracks.value.length} 段，原声段放源音轨（还没有成片混音）`
  }
  return filtering.value
    ? '当前筛选里没有可播放的段落'
    : '还没有可播放的段落：先贴齐混音或合成配音'
})

/** 连播走宿主播放条；本地原声试听与逐段配音先让位，避免两个声音一起响。 */
function playFiltered() {
  const tracks = playWhole.value ? wholeTracks.value : queueTracks.value
  if (!tracks.length) return
  stopVoice()
  pausePreview()
  hostActions.playAll(tracks, 0)
}

const start = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW_H) - OVERSCAN))
const count = computed(() => Math.ceil(viewHeight.value / ROW_H) + OVERSCAN * 2)
const end = computed(() => Math.min(filtered.value.length, start.value + count.value))
const visible = computed(() => filtered.value.slice(start.value, end.value))
const padTop = computed(() => start.value * ROW_H)
const padBottom = computed(() => Math.max(0, (filtered.value.length - end.value) * ROW_H))

const allVisibleSelected = computed(
  () =>
    filtered.value.length > 0 &&
    filtered.value.every((/** @type {any} */ cue) => state.selectedIds.includes(cue.id))
)

const someVisibleSelected = computed(
  () =>
    !allVisibleSelected.value &&
    filtered.value.some((/** @type {any} */ cue) => state.selectedIds.includes(cue.id))
)

/** 选中的段里真正能重配的条数：保留原声、缺音色的不算。 */
const synthTargets = computed(
  () =>
    cues.value.filter(
      (/** @type {any} */ cue) =>
        state.selectedIds.includes(cue.id) &&
        cue.dub &&
        String(cue.text || '').trim() &&
        roleOf(cue)?.engine &&
        roleOf(cue)?.voice_id
    ).length
)

/** 预览正在放、且播放头停在某一段里：那一行就是"原声正在响"。 */
const originalPlaying = computed(() => {
  if (!state.playing) return ''
  const ms = state.playTime * 1000
  const hit = cues.value.find((/** @type {any} */ cue) => ms >= cue.start_ms && ms < cue.end_ms)
  return hit?.id || ''
})

let observer = /** @type {ResizeObserver | null} */ (null)

/** @param {Event} event */
function onScroll(event) {
  const target = /** @type {HTMLElement | null} */ (event.target)
  scrollTop.value = target?.scrollTop || 0
}

function measure() {
  if (!scrollEl.value) return
  viewHeight.value = scrollEl.value.clientHeight || 320
}

onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  if (scrollEl.value) observer.observe(scrollEl.value)
  if (state.selectedCue) reveal(state.selectedCue, 'auto')
})

onBeforeUnmount(() => {
  observer?.disconnect()
  stopVoice()
})

watch(filter, () => {
  scrollTop.value = 0
  if (scrollEl.value) scrollEl.value.scrollTop = 0
})

watch(
  () => state.selectedCue,
  (/** @type {string} */ id) => {
    if (id) reveal(id, 'auto')
  }
)

/** 把某一行滚进视野；已经看得见就一动不动，免得表格自己跳。 */
/**
 * @param {string} id
 * @param {ScrollBehavior} behavior
 */
function reveal(id, behavior) {
  const index = filtered.value.findIndex((/** @type {any} */ cue) => cue.id === id)
  const view = scrollEl.value
  if (index < 0 || !view) return
  const top = index * ROW_H
  if (top >= view.scrollTop + ROW_H && top + ROW_H <= view.scrollTop + view.clientHeight - ROW_H) {
    return
  }
  const target = Math.max(0, top - view.clientHeight / 3)
  if (typeof view.scrollTo === 'function') view.scrollTo({ top: target, behavior })
  else view.scrollTop = target
}

/** 点行：选中并把播放头搬过来（"听这一段"的入口）。 */
/** @param {any} cue @param {MouseEvent} event */
function pick(cue, event) {
  const multi = event?.ctrlKey || event?.metaKey
  const range = event?.shiftKey
  if (range) {
    const ids = filtered.value.map((/** @type {any} */ item) => item.id)
    const anchor = ids.indexOf(state.anchorId)
    const at = ids.indexOf(cue.id)
    if (at >= 0) {
      const from = anchor >= 0 ? Math.min(anchor, at) : at
      const to = anchor >= 0 ? Math.max(anchor, at) : at
      pickRange(ids.slice(from, to + 1))
    }
    return
  }
  if (multi) {
    togglePick(cue.id)
    return
  }
  state.selectedCue = cue.id
  seekTo(cue.start_ms / 1000)
}

/** 点勾选框：Shift 连选；否则只切换这一行。 */
/** @param {any} cue @param {MouseEvent} event */
function onCheck(cue, event) {
  if (event?.shiftKey) {
    const ids = filtered.value.map((/** @type {any} */ item) => item.id)
    const anchor = ids.indexOf(state.anchorId)
    const at = ids.indexOf(cue.id)
    if (at >= 0) {
      const from = anchor >= 0 ? Math.min(anchor, at) : at
      const to = anchor >= 0 ? Math.max(anchor, at) : at
      pickRange(ids.slice(from, to + 1))
      return
    }
  }
  togglePick(cue.id)
}

function toggleAll() {
  if (allVisibleSelected.value) {
    const drop = new Set(filtered.value.map((/** @type {any} */ cue) => cue.id))
    setPicks(state.selectedIds.filter((/** @type {string} */ id) => !drop.has(id)))
    return
  }
  setPicks([...state.selectedIds, ...filtered.value.map((/** @type {any} */ cue) => cue.id)])
}

function clearFilter() {
  filter.text = ''
  filter.role = ''
  filter.status = ''
}

/** @param {string} roleId */
async function onBatchRole(roleId) {
  if (!roleId) return
  await applyRoleToSelection(roleId)
  batchRole.value = ''
}

async function onSynthSelected() {
  await synthSelection()
}

async function onDeleteSelected() {
  const count = state.selectedIds.length
  if (!count) return
  if (!window.confirm(`删除选中的 ${count} 段？它们的配音产物一并删除。`)) return
  await deleteSelection()
}

/** 点进文本：只选中，不搬播放头——用户是要改字，不是要从这里开始听。 */
/** @param {any} cue */
function select(cue) {
  state.selectedCue = cue.id
}

/** @param {any} cue @param {any} patch */
function onPatch(cue, patch) {
  void patchCue(cue, patch)
}

/** @param {any} cue */
function roleOf(cue) {
  const list = roles.value
  return list.find((/** @type {any} */ role) => role.id === cueRoleId(cue)) || list[0] || null
}

// ---- 试听 ----------------------------------------------------------------

function stopVoice() {
  if (voiceEl) {
    voiceEl.pause()
    voiceEl = null
  }
  voicePlaying.value = ''
  playingVersion = ''
}

/** 一条一段：原声走预览（能从这一句的开头放到结尾），配音单独放。
 *  两边互斥——以前预览在放原声、点一行又叠上配音，两个声音一起响。 */
/** @param {any} cue @param {string} kind */
function audition(cue, kind) {
  if (kind === 'original') {
    if (originalPlaying.value === cue.id) {
      pausePreview()
      return
    }
    stopVoice()
    if (hostStore.player.playing) hostActions.stop()
    auditionCue(cue)
    return
  }
  if (voicePlaying.value === cue.id) {
    stopVoice()
    return
  }
  stopVoice()
  pausePreview()
  if (hostStore.player.playing) hostActions.stop()
  if (!cue.audio) {
    state.error = '这一段还没有配音。'
    return
  }
  if (cue.audio_ok === false) {
    state.error = '这一段的旧音频文件已不在磁盘上：重配这一段后恢复。'
    return
  }
  // 与创作页同一条口径：版本优先用文件写入时刻，缺了就退回合成键，
  // 不能让同名文件被浏览器当成上一次的那一版。
  const version = cue.audio_version || (cue.audio_key || '').slice(0, 10)
  const url = api.mediaUrl(state.currentId, 'cue', cue.id, version)
  const el = new Audio(url)
  voiceEl = el
  voicePlaying.value = cue.id
  playingVersion = `${cue.audio || ''}|${version}`
  el.onended = () => {
    if (voiceEl === el) stopVoice()
  }
  el.onerror = () => {
    if (voiceEl !== el) return
    stopVoice()
    state.error = '这一段的音频读不出来：重配一次；如果换过音色，确认旧音频文件还在。'
  }
  void el.play().catch((error) => {
    if (voiceEl !== el) return
    stopVoice()
    state.error = `这一段的音频没法播放：${error?.message || '浏览器拒绝了播放'}。`
  })
}

// 预览开始播（用户按了播放/拖了进度）：逐段配音让位，不能两个一起响。
watch(
  () => state.playing,
  (playing) => {
    if (playing) stopVoice()
  }
)

// 宿主播放条开始播（音色栏试听、连播队列）：这里的两路声音都让位。
watch(
  () => hostStore.player.playing,
  (playing) => {
    if (!playing) return
    stopVoice()
    pausePreview()
  }
)

// 换音色 / 换样例会让这一段变成另一版：正放着旧音频就先停下。
// 底栏的播放键随后点一次就是新的一版，不会卡在"灯亮着却没有声音"。
watch(
  () => roles.value
    .map((/** @type {any} */ role) =>
      `${role.id}:${role.engine}:${role.voice_id}:${role.sample_mode || ''}:${role.sample_file || ''}`
    )
    .join('|'),
  () => {
    if (voicePlaying.value) stopVoice()
  }
)

// 文件被重配、被删或这一行没了：正在放的那一版已经不代表当前段落。
watch(
  () => {
    const cue = cues.value.find(
      (/** @type {any} */ item) => item.id === voicePlaying.value
    )
    return cue ? `${cue.audio || ''}|${cue.audio_version || cue.audio_key || ''}` : ''
  },
  (next) => {
    if (voicePlaying.value && playingVersion && next !== playingVersion) stopVoice()
  }
)

// ---- 菜单 ----------------------------------------------------------------

/** @param {any} cue */
function cueContext(cue) {
  const role = roleOf(cue)
  const voiced = Boolean(cue.dub && role?.engine && role?.voice_id && String(cue.text || '').trim())
  // 有可用配音就放配音；保留原声、旧文件丢失或改回原声的段放源音轨，
  // 不拿一条过期的配音顶替当前状态。
  const dubPlayable = Boolean(cue.dub && cue.audio && cue.audio_ok !== false)
  const playable = dubPlayable || Boolean(paths.value.source || paths.value.original)
  return {
    hasAudio: playable,
    canSynth: voiced,
    synthReason: cue.dub ? '角色还没有音色' : '这一段保留原声',
    play: () => audition(cue, dubPlayable ? 'dub' : 'original'),
    synth: () => synthOne(cue.id),
    split: () => splitHere(cue),
    merge: () => mergeNext(cue),
    remove: () => removeThis(cue),
  }
}

/** 行尾「⋯」与右键菜单同一个入口：按钮贴按钮开，右键贴光标开。 */
/** @param {any} cue @param {MouseEvent} event */
function onRowMenu(cue, event) {
  const trigger = event?.currentTarget
  if (trigger instanceof HTMLElement && trigger.classList.contains('rbtn')) {
    if (menuState.open && menuState.trigger === trigger) {
      closeContextMenu({ restoreFocus: true })
      return
    }
    select(cue)
    openObjectMenu('dubbing.cue', cueContext(cue), { anchor: trigger })
    return
  }
  if (!canTakeOver(event)) return
  state.selectedCue = cue.id
  seekTo(cue.start_ms / 1000)
  openObjectMenu('dubbing.cue', cueContext(cue), { event })
}

/** @param {any} cue */
async function splitHere(cue) {
  const at = Math.round(state.playTime * 1000)
  if (at <= cue.start_ms || at >= cue.end_ms) {
    state.error = '播放头不在这一段内：先点段落把播放头移过来。'
    return
  }
  await splitCue(cue, at)
}

/** @param {any} cue */
async function mergeNext(cue) {
  await mergeCue(cue)
}

/** @param {any} cue */
async function removeThis(cue) {
  await removeCue(cue)
}

/** @param {KeyboardEvent} event */
function onKey(event) {
  const target = /** @type {HTMLElement | null} */ (event.target)
  // 行内改字时，空格就是空格，Ctrl+A 就是全选这一格：表格快捷键不能抢。
  if (
    target &&
    (target.tagName === 'TEXTAREA' ||
      target.tagName === 'INPUT' ||
      target.isContentEditable)
  ) {
    return
  }
  const meta = event.ctrlKey || event.metaKey
  if (meta && event.key.toLowerCase() === 'a') {
    event.preventDefault()
    setPicks(filtered.value.map((/** @type {any} */ cue) => cue.id))
    return
  }
  if (event.key === ' ' && state.selectedCue) {
    event.preventDefault()
    togglePick(state.selectedCue)
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  if (!cues.value.length) return
  event.preventDefault()
  const list = filtered.value.length ? filtered.value : cues.value
  const index = list.findIndex((/** @type {any} */ cue) => cue.id === state.selectedCue)
  const step = event.key === 'ArrowDown' ? 1 : -1
  const next = list[Math.min(list.length - 1, Math.max(0, index + step))]
  if (!next) return
  state.selectedCue = next.id
  if (event.shiftKey) togglePick(next.id)
  seekTo(next.start_ms / 1000)
  reveal(next.id, 'auto')
}
</script>

<style scoped>
.seg-wrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  /* 列宽按"这张表实际有多宽"收缩，不看窗口：同一屏在 1366 上被左右两栏
     挤窄，在 1500 上又够用，用视口断点会把这两种情况混在一起。 */
  container-type: inline-size;
}

.seg-tools {
  flex: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
  min-height: 34px;
  padding: 0 4px 4px;
}

.seg-search {
  flex: 1 1 120px;
  min-width: 90px;
  max-width: 230px;
  display: flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 7px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-xs);
  background: var(--sh-field);
  color: var(--sh-text-4);
}

.seg-search:focus-within {
  border-color: var(--sh-accent-ring);
  background: var(--sh-field-focus);
}

.seg-search-input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--sh-text);
  font: inherit;
  font-size: 11.5px;
  outline: none;
}

.seg-search-input::placeholder { color: var(--sh-text-4); }

.seg-search-clear {
  flex: none;
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 4px;
  color: var(--sh-text-4);
}

.seg-search-clear:hover { background: var(--sh-chip); color: var(--sh-danger); }

.seg-filter {
  flex: none;
  width: auto;
  min-width: 92px;
  max-width: 128px;
  height: 26px;
  /* 箭头占右侧 20px：内边距压到 6px 时文字会顶到箭头下面，像被截断。 */
  padding: 0 26px 0 8px;
  font-size: 11px;
}

.seg-play {
  flex: none;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-xs);
  background: var(--sh-field);
  color: var(--sh-text-3);
}

.seg-play:hover:not(:disabled) {
  border-color: var(--sh-accent-ring);
  color: var(--sh-accent);
  background: var(--sh-hover);
}

.seg-play:disabled {
  opacity: 0.4;
  cursor: default;
}

.seg-count {
  flex: none;
  margin-left: auto;
  font-size: 10.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.seg-clear-filters {
  flex: none;
  font-size: 10.5px;
  color: var(--sh-text-3);
  padding: 2px 5px;
  border-radius: 4px;
}

.seg-clear-filters:hover { background: var(--sh-chip); color: var(--sh-accent); }

.seg-batch {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 34px;
  margin-bottom: 5px;
  padding: 4px 8px;
  border: 1px solid var(--sh-accent-ring);
  border-radius: var(--sh-r-xs);
  background: var(--sh-accent-soft);
}

.batch-count {
  flex: none;
  font-size: 11px;
  color: var(--sh-text-2);
}

.batch-count b { color: var(--sh-accent); }

.batch-role {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.batch-role select {
  width: 112px;
  height: 26px;
  padding: 0 6px;
  font-size: 11px;
}

.batch-danger { color: var(--sh-danger); }

.seg-head,
:deep(.srow) {
  display: grid;
  grid-template-columns: 40px 64px 128px minmax(0, 1fr) 52px 78px;
  align-items: center;
  gap: 6px;
  padding: 0 8px 0 8px;
}

.seg-head {
  flex: none;
  height: 26px;
  border-bottom: 1px solid var(--sh-line);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
  white-space: nowrap;
}

@container (max-width: 620px) {
  .seg-head,
  :deep(.srow) {
    grid-template-columns: 34px 48px 80px minmax(0, 1fr) 40px 70px;
    gap: 4px;
    padding: 0 6px;
  }

  :deep(.srow .c-tc) {
    font-size: 9px;
  }

  .seg-head .c-st,
  :deep(.srow .c-st) {
    font-size: 9.5px;
  }

  :deep(.srow .c-cast .cast-name),
  :deep(.srow .c-cast .cast-voice) {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :deep(.srow .c-cast) {
    gap: 4px;
  }

  :deep(.srow .c-act) {
    gap: 0;
  }

  :deep(.srow .rbtn) {
    width: 23px;
  }
}

.seg-head .c-st,
.seg-head .c-act {
  text-align: right;
}

.seg-head .c-idx {
  justify-self: start;
}

.seg-head .idx-all {
  display: grid;
  place-items: center;
  width: 32px;
  height: 22px;
  padding: 0;
  border-radius: 5px;
  color: var(--sh-text-4);
  font-family: var(--sh-mono);
  font-size: 11px;
  cursor: pointer;
}

.seg-head .idx-all:hover {
  background: var(--sh-hover);
  color: var(--sh-accent);
}

.seg-head .idx-all[aria-checked='true'],
.seg-head .idx-all[aria-checked='mixed'] {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.seg-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  /* 滚到底别把整页一起拖走：以前滚过最后一行，页面跟着滑到下一屏。 */
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.seg-scroll:focus-visible { outline: none; }

.seg-empty {
  padding: 18px 14px;
  font-size: 12px;
  color: var(--sh-text-3);
}
</style>
