<template>
  <div class="seg-wrap">
    <div class="seg-head">
      <span class="c-i">#</span>
      <span class="c-tc">起止</span>
      <span class="c-d">时长</span>
      <span class="c-role">角色</span>
      <span class="c-x">文本</span>
      <span class="c-st">状态</span>
      <span class="c-act"></span>
    </div>

    <div ref="scrollEl" class="seg-scroll" @scroll="onScroll">
      <div :style="{ height: `${padTop}px` }"></div>
      <div
        v-for="cue in visible"
        :key="cue.id"
        class="seg-row"
        :class="{ 'is-on': cue.id === state.selectedCue, 'is-stale': cue.state === 'stale' }"
        role="row"
        tabindex="0"
        :aria-selected="cue.id === state.selectedCue"
        @click="select(cue)"
        @keydown.enter.prevent="select(cue)"
        @contextmenu="onRowMenu(cue, $event)"
      >
        <span class="c-i">{{ pad(cue.ordinal) }}</span>
        <span class="c-tc">{{ tc(cue.start_ms) }}<em>→</em>{{ tc(cue.end_ms) }}</span>
        <span class="c-d">{{ ((cue.end_ms - cue.start_ms) / 1000).toFixed(1) }}s</span>
        <span class="c-role">
          <span class="who" :style="{ '--c': roleColor(cue) }">
            <i></i>
            <select
              :value="cue.role_id || roles[0]?.id || ''"
              :aria-label="`第 ${cue.ordinal} 段的角色`"
              @click.stop
              @change="onRole(cue, $event.target.value)"
            >
              <option v-for="role in roles" :key="role.id" :value="role.id">{{ role.name }}</option>
            </select>
          </span>
        </span>
        <span class="c-x">
          <textarea
            v-if="cue.id === state.selectedCue"
            :value="cue.text"
            rows="1"
            :aria-label="`第 ${cue.ordinal} 段的文本`"
            @focus="state.editing += 1"
            @blur="onText(cue, $event)"
            @keydown.enter.prevent="$event.target.blur()"
            @keydown.esc.prevent="cancelEdit(cue, $event)"
          ></textarea>
          <span v-else class="text">{{ cue.text || '（空）' }}</span>
        </span>
        <span class="c-st">
          <span class="st" :class="`st-${cue.state}`">{{ STATE_LABEL[cue.state] || cue.state }}</span>
        </span>
        <span class="c-act">
          <button
            type="button"
            class="seg-more"
            title="更多操作"
            aria-label="更多操作"
            aria-haspopup="menu"
            @click.stop="openMore(cue, $event)"
          >
            <MoreHorizontal :size="14" :stroke-width="1.8" />
          </button>
        </span>
      </div>
      <div :style="{ height: `${padBottom}px` }"></div>
      <p v-if="!cues.length" class="seg-empty">
        还没有段落。先做一次转录，或导入现成的字幕。
      </p>
    </div>
  </div>
</template>

<script setup>
// 段落表：一行一段，编辑即提交。行高固定 + 只渲染可视窗口——
// 两千段的项目常驻两千个输入框会直接卡住输入。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MoreHorizontal } from 'lucide-vue-next'
import {
  cues,
  mergeCue,
  patchCue,
  removeCue,
  roleColor,
  roles,
  seekTo,
  splitCue,
  state,
} from '../store'
import * as api from '../api'
import {
  canTakeOver,
  closeContextMenu,
  menuState,
  openObjectMenu,
} from '../../../composables/contextMenu'

const STATE_LABEL = { empty: '未生成', ready: '已生成', stale: '已过期', failed: '失败' }
const ROW_H = 46
const OVERSCAN = 6

const scrollEl = ref(null)
const scrollTop = ref(0)
const viewHeight = ref(420)

const start = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW_H) - OVERSCAN))
const count = computed(() => Math.ceil(viewHeight.value / ROW_H) + OVERSCAN * 2)
const end = computed(() => Math.min(cues.value.length, start.value + count.value))
const visible = computed(() => cues.value.slice(start.value, end.value))
const padTop = computed(() => start.value * ROW_H)
const padBottom = computed(() => Math.max(0, (cues.value.length - end.value) * ROW_H))

let observer = null

function onScroll(event) {
  scrollTop.value = event.target.scrollTop
}

function measure() {
  if (!scrollEl.value) return
  viewHeight.value = scrollEl.value.clientHeight || 420
}

onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  if (scrollEl.value) observer.observe(scrollEl.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
})

watch(
  () => state.selectedCue,
  (id) => {
    if (!id) return
    const index = cues.value.findIndex((cue) => cue.id === id)
    if (index < 0 || !scrollEl.value) return
    // 从时间轴点过来时把这一行滚进视野；已经可见就不动，避免表格自己跳。
    const top = index * ROW_H
    const view = scrollEl.value
    if (top < view.scrollTop || top + ROW_H > view.scrollTop + view.clientHeight) {
      view.scrollTop = Math.max(0, top - view.clientHeight / 3)
    }
  }
)

function select(cue) {
  state.selectedCue = cue.id
  seekTo(cue.start_ms / 1000)
}

/** Esc 放弃这一行的改动：先把值换回原文本，再失焦（失焦会提交）。 */
function cancelEdit(cue, event) {
  event.target.value = cue.text
  event.target.blur()
}

function pad(value) {
  return String(value).padStart(3, '0')
}

function tc(ms) {
  const total = Math.max(0, Math.round(ms / 10) * 10)
  const minutes = Math.floor(total / 60000)
  const rest = total % 60000
  const seconds = Math.floor(rest / 1000)
  const millis = rest % 1000
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')},${String(millis).padStart(3, '0')}`
}

function onText(cue, event) {
  state.editing = Math.max(0, state.editing - 1)
  const value = event.target.value.trim()
  event.target.value = value
  if (value && value !== cue.text) void patchCue(cue, { text: value })
}

function onRole(cue, roleId) {
  if (roleId && roleId !== cue.role_id) void patchCue(cue, { role_id: roleId })
}

/** @param {any} cue */
function cueContext(cue) {
  return {
    hasAudio: Boolean(cue.audio),
    play: () => playCue(cue),
    split: () => splitHere(cue),
    merge: () => mergeNext(cue),
    remove: () => removeThis(cue),
  }
}

/** 行尾「⋯」：贴按钮展开；再点同一颗键收起。 */
/** @param {any} cue @param {MouseEvent} event */
function openMore(cue, event) {
  const trigger = event?.currentTarget
  if (menuState.open && menuState.trigger === trigger) {
    closeContextMenu({ restoreFocus: true })
    return
  }
  openObjectMenu('dubbing.cue', cueContext(cue), {
    anchor: trigger instanceof HTMLElement ? trigger : null,
  })
}

/** 右键落在哪一句就选中哪一句：菜单里的播放头拆分要的是这一句。 */
/** @param {any} cue @param {MouseEvent} event */
function onRowMenu(cue, event) {
  if (!canTakeOver(event)) return
  select(cue)
  openObjectMenu('dubbing.cue', cueContext(cue), { event })
}

/** @param {any} cue */
function playCue(cue) {
  if (!cue.audio) return
  const audio = new Audio(api.mediaUrl(state.currentId, 'cue', cue.id))
  void audio.play()
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
</script>

<style scoped>
.seg-wrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.seg-head,
.seg-row {
  display: grid;
  grid-template-columns: 40px 130px 46px 90px minmax(0, 1fr) 64px 30px;
  align-items: center;
  gap: 10px;
  padding: 0 10px 0 12px;
}

.seg-head {
  flex: none;
  height: 26px;
  border-bottom: 1px solid var(--sh-line);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
}

.seg-head .c-d,
.seg-head .c-st {
  text-align: right;
}

.seg-head .c-role { padding-left: 2px; }

.seg-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.seg-row {
  height: 46px;
  border-bottom: 1px solid var(--sh-line-soft);
  font-size: 12px;
  color: var(--sh-text-2);
  cursor: default;
}

.seg-row:hover { background: var(--sh-hover); }

.seg-row:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: -2px;
}
.seg-row.is-on { background: var(--sh-accent-soft); }
.seg-row.is-stale .c-x .text { color: var(--sh-text-4); }

.c-i {
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 11px;
  color: var(--sh-text-4);
}

.c-tc {
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 10.5px;
  color: var(--sh-text-3);
  white-space: nowrap;
}

.c-tc em {
  font-style: normal;
  margin: 0 3px;
  color: var(--sh-text-4);
}

.c-d {
  font-size: 10.5px;
  color: var(--sh-text-3);
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.who {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
}

.who i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c);
  flex: none;
}

.who select {
  max-width: 74px;
  height: 22px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11.5px;
  padding: 0 2px;
}

.who select:hover,
.who select:focus {
  border-color: var(--sh-line);
  background: var(--sh-field);
  outline: none;
}

.c-x {
  display: block;
  min-width: 0;
  overflow: hidden;
}

.c-x .text {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sh-text);
}

.c-x textarea {
  display: block;
  box-sizing: border-box;
  width: 100%;
  min-height: 26px;
  max-height: 34px;
  resize: none;
  /* 单行输入：滚动条会把 26px 高的框吃掉一半，光标靠浏览器自动滚。 */
  overflow: hidden;
  padding: 3px 6px;
  border: 1px solid var(--sh-accent-ring);
  border-radius: 6px;
  background: var(--sh-field-focus);
  color: var(--sh-text);
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
  outline: none;
}

.c-st {
  text-align: right;
}

.st {
  font-size: 10.5px;
  color: var(--sh-text-3);
  white-space: nowrap;
}

.st-ready { color: var(--sh-ok); }
.st-stale { color: var(--sh-warn); }
.st-failed { color: var(--sh-danger); }

.c-act {
  text-align: right;
}

.seg-more {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  color: var(--sh-text-4);
  opacity: 0;
}

.seg-row:hover .seg-more,
.seg-row.is-on .seg-more,
.seg-more:focus-visible {
  opacity: 1;
}

.seg-more:hover { background: var(--sh-chip); color: var(--sh-accent); }

.seg-empty {
  padding: 18px 14px;
  font-size: 12px;
  color: var(--sh-text-3);
}
</style>
