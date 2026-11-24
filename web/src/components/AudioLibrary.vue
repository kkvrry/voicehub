<template>
  <!-- 历史：录音 / 内录 / 生成三类音频放在同一张表里，按时间倒序。
       数据表页不叠卡——标题、筛选、表格依次铺在平面上，用分隔线分层（与创作页同一政策）。
       选中与创作页、配音段落表同一套：点行选中，Ctrl 逐条、Shift 连选。
       行内主操作只有「入库」，低频动作在行尾「⋯」与右键菜单，两处共用一份定义。 -->
  <div class="audio-page">
    <header class="audio-head">
      <h1>
        历史
        <!-- 顺序播放紧贴标题：它管的是眼前这一列，不是页面的一个工具。 -->
        <button
          type="button"
          class="audio-run"
          :disabled="!playableCount"
          title="按列表顺序播放当前筛选下的音频"
          aria-label="顺序播放当前列表"
          @click="playVisible"
        >
          <Play :size="12" :stroke-width="1.8" fill="currentColor" aria-hidden="true" />
        </button>
        <span class="audio-sum">
          共 {{ store.audio.total }} 条 · 未入库 {{ visibleUnabsorbed }}
        </span>
      </h1>
    </header>

    <div class="audio-bar">
      <button
        v-for="item in RANGES"
        :key="item.key"
        type="button"
        class="audio-chip"
        :class="{ 'is-on': store.audio.range === item.key }"
        :aria-pressed="store.audio.range === item.key"
        @click="pickRange(item.key)"
      >
        {{ item.label }}
      </button>
      <span class="audio-divider" aria-hidden="true"></span>
      <button
        v-for="item in ABSORBED"
        :key="item.key"
        type="button"
        class="audio-chip"
        :class="{ 'is-on': store.audio.absorbed === item.key }"
        :aria-pressed="store.audio.absorbed === item.key"
        @click="pickAbsorbed(item.key)"
      >
        {{ item.label }}
      </button>
      <span class="spacer"></span>
      <button v-if="filtered" type="button" class="audio-clear" @click="clearFilters">
        清除筛选
      </button>
    </div>

    <!-- 批量动作贴着选择列放：全选在最左，动作就紧跟其后，不必横跨整行去找。 -->
    <div v-if="selected.length" class="audio-bulk">
      <span class="audio-bulk-count">已选 {{ selected.length }} 条</span>
      <ActionButton size="sm" variant="soft" @click="openAbsorb(selected)">入库到…</ActionButton>
      <ActionButton size="sm" variant="danger" @click="discard(selected)">移入回收站</ActionButton>
      <button type="button" class="audio-clear" @click="clearSelected">取消选择</button>
    </div>

    <div class="audio-table">
      <div class="audio-tr is-head">
        <span class="audio-pick">
          <button
            type="button"
            class="audio-ord"
            role="checkbox"
            :class="{ 'is-on': allPicked, 'is-mixed': somePicked }"
            :aria-checked="allPicked ? 'true' : somePicked ? 'mixed' : 'false'"
            :title="allPicked ? '取消全选' : '全选当前列表'"
            :aria-label="allPicked ? '取消全选' : '全选当前列表'"
            @click="toggleAllVisible"
          >#</button>
        </span>
        <span>试听</span>
        <span>内容</span>
        <span>来源</span>
        <span>音色</span>
        <span>时间</span>
        <span class="audio-ops-head">操作</span>
      </div>

      <div
        v-for="(row, index) in rows"
        :key="row.id"
        class="audio-tr"
        :class="{ 'is-picked': picked(row), 'is-playing': playing(row) }"
        @click="pickRow(row, $event)"
        @contextmenu="openRowMenu(row, $event)"
      >
        <span class="audio-pick">
          <button
            type="button"
            class="audio-ord"
            :class="{ 'is-on': picked(row) }"
            :aria-pressed="picked(row) ? 'true' : 'false'"
            :title="picked(row) ? '取消选择' : '选择；Shift 点另一行连选'"
            :aria-label="picked(row) ? `取消选择第 ${index + 1} 条` : `选择第 ${index + 1} 条`"
            @click.stop="toggleRow(row.id, $event)"
          >{{ String(index + 1).padStart(2, '0') }}</button>
        </span>

        <span class="audio-listen">
          <button
            type="button"
            class="audio-play"
            :class="{ 'is-on': playing(row) }"
            :disabled="!row.url"
            :title="row.url ? (playing(row) ? '暂停' : '试听') : '这条没有对应的音频文件'"
            :aria-label="playing(row) ? '暂停' : '试听'"
            @click.stop="playRow(row)"
          >
            <Pause v-if="playing(row) && store.player.playing" :size="11" :stroke-width="1.8" fill="currentColor" />
            <Play v-else :size="11" :stroke-width="1.8" fill="currentColor" />
          </button>
          <span class="audio-clock">{{ clock(row.duration) }}</span>
        </span>

        <span class="audio-body">
          <span class="audio-text" :class="{ 'is-file': !row.text }">{{ row.text || row.file }}</span>
          <span class="audio-meta">
            <span
              v-if="row.emotion"
              class="audio-emo vchip"
              :class="`tone-${emotionTone(row.emotion)}`"
            >{{ row.emotion }}</span>
            <span class="audio-meta-text">{{ meta(row) }}</span>
          </span>
        </span>

        <span class="audio-src" :class="`is-${row.source}`">{{ sourceText(row) }}</span>

        <span class="audio-voice" :title="row.voice_name || undefined">
          {{ row.voice_name || '—' }}
        </span>

        <span class="audio-stamp">{{ stamp(row.created) }}</span>

        <span class="audio-ops">
          <template v-if="isAbsorbed(row)">
            <span class="audio-done" :title="`已入库到 ${absorbedNames(row)}`">已入库</span>
          </template>
          <ActionButton v-else size="sm" variant="soft" @click.stop="openAbsorb([row.id])">入库</ActionButton>
          <button
            type="button"
            class="audio-more"
            title="更多操作"
            aria-label="更多操作"
            aria-haspopup="menu"
            @click.stop="openRowMenu(row, $event)"
          >
            <MoreHorizontal :size="14" :stroke-width="1.8" />
          </button>
        </span>
      </div>

      <div v-if="store.audio.hasMore" class="audio-more-note">
        只显示前 200 条，收窄筛选查看更多。
      </div>

      <div v-if="!rows.length" class="audio-empty">
        <p>{{ emptyText }}</p>
        <ActionButton v-if="filtered" size="sm" @click="clearFilters">清除筛选</ActionButton>
      </div>
    </div>

    <AbsorbDialog :open="absorbOpen" :ids="absorbIds" @close="absorbOpen = false" @confirm="onAbsorb" />
  </div>
</template>

<script setup>
// @ts-check
import { computed, ref } from 'vue'
import { MoreHorizontal, Pause, Play } from 'lucide-vue-next'
import { actions, store } from '../store'
import { SOURCES, clock, isAbsorbed, stamp, absorb as absorbRows } from '../store/audio'
import ActionButton from './ui/ActionButton.vue'
import { emotionTone } from './ui/tones'
import { closeContextMenu, menuState, openObjectMenu } from '../composables/contextMenu'
import AbsorbDialog from './AbsorbDialog.vue'

const SOURCE_LABELS = Object.fromEntries(SOURCES.map((item) => [item.key, item.label]))
const RANGES = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今天' },
  { key: '7d', label: '7 天' },
  { key: '30d', label: '30 天' },
]
const ABSORBED = [
  { key: 'no', label: '未入库' },
  { key: 'yes', label: '已入库' },
]

const rows = computed(() => store.audio.items)
const visibleUnabsorbed = computed(() => store.audio.unabsorbed)
const selected = computed(() => store.audio.selected)

const playableCount = computed(() => rows.value.filter((row) => row.url).length)
const allPicked = computed(
  () => rows.value.length > 0 && rows.value.every((row) => selected.value.includes(row.id))
)
const somePicked = computed(
  () => !allPicked.value && rows.value.some((row) => selected.value.includes(row.id))
)
const filtered = computed(
  () =>
    Boolean(store.audio.q.trim()) ||
    store.audio.source !== 'all' ||
    store.audio.range !== 'all' ||
    Boolean(store.audio.absorbed)
)

const emptyText = computed(() => {
  if (store.audio.error) return store.audio.error
  if (store.audio.loading) return '正在读取…'
  if (filtered.value) return '没有符合当前筛选的音频。'
  return '还没有音频。录音、内录与生成的音频都会出现在这里。'
})

/** @param {any} row */
function picked(row) {
  return selected.value.includes(row.id)
}

/** @param {any} row */
function playing(row) {
  return Boolean(row.url) && store.player.url === row.url
}

/** 来源列：生成物直接写引擎名（产物在盘上就按引擎分目录），其余写来源。 */
/** @param {any} row */
function sourceText(row) {
  return row.source === 'engine' ? row.engine || SOURCE_LABELS.engine : SOURCE_LABELS[row.source]
}

/** @param {any} row */
function meta(row) {
  const parts = [row.detail]
  if (row.source === 'engine' && row.params?.length) parts.push(row.params.join(' · '))
  return parts.filter(Boolean).join(' · ')
}

const absorbOpen = ref(false)
const absorbIds = ref(/** @type {string[]} */ ([]))
/** Shift 连选的起点：最近一次点选过的那一行。 */
const anchorId = ref('')

/**
 * 行菜单：行尾「⋯」贴按钮，右键贴指针；两处共用 menus/catalog.js 里的一份定义。
 * @param {any} row
 * @param {MouseEvent} event
 */
function openRowMenu(row, event) {
  const trigger = event.currentTarget
  if (trigger instanceof HTMLElement && trigger.classList.contains('audio-more')) {
    if (menuState.open && menuState.trigger === trigger) {
      closeContextMenu({ restoreFocus: true })
      return
    }
    openObjectMenu('audio.row', rowMenu(row), { anchor: trigger })
    return
  }
  openObjectMenu('audio.row', rowMenu(row), { event })
}

/** @param {any} row */
function rowMenu(row) {
  return {
    text: row.text,
    file: row.file,
    url: row.url,
    playing: playing(row) && store.player.playing,
    play: () => playRow(row),
    discard: () => discard([row.id]),
  }
}

/** @param {any} row */
function absorbedNames(row) {
  /** @type {{ name: string }[]} */
  const list = row.absorbed || []
  return list.map((item) => item.name).join('、')
}

/** @param {string[]} ids */
function openAbsorb(ids) {
  absorbIds.value = [...ids]
  absorbOpen.value = true
}

/** @param {any} target */
function onAbsorb(target) {
  absorbRows(absorbIds.value, {
    kind: target.mode,
    voiceId: target.voiceId,
    voiceName: target.voiceName,
    folder: target.folder,
    gender: target.gender,
    language: target.language,
    mainSample: target.mainSample,
    emotion: target.emotion,
  })
  absorbOpen.value = false
}

/** @param {string} key */
function pickRange(key) {
  actions.pickRange(key)
}

/** @param {string} key */
function pickAbsorbed(key) {
  actions.pickAbsorbed(key)
}

function clearFilters() {
  actions.clearAudioFilters()
}

/**
 * 点行：普通点选 = 只选这一行；Ctrl/⌘ 逐条切换；Shift 从锚点连选到这一行。
 * @param {any} row
 * @param {MouseEvent} event
 */
function pickRow(row, event) {
  if (event.shiftKey) {
    selectRange(row.id)
    return
  }
  if (event.ctrlKey || event.metaKey) {
    actions.toggleRow(row.id)
    anchorId.value = row.id
    return
  }
  actions.setSelected([row.id])
  anchorId.value = row.id
}

/** @param {string} id @param {MouseEvent} event */
function toggleRow(id, event) {
  if (event?.shiftKey) {
    selectRange(id)
    return
  }
  actions.toggleRow(id)
  anchorId.value = id
}

/** 从这里到锚点整段选上；锚点不在当前列表（换过筛选）就只选这一行。 */
/** @param {string} id */
function selectRange(id) {
  const ids = rows.value.map((row) => row.id)
  const from = ids.indexOf(anchorId.value)
  const to = ids.indexOf(id)
  if (from < 0 || to < 0) {
    actions.setSelected([id])
    anchorId.value = id
    return
  }
  actions.setSelected(ids.slice(Math.min(from, to), Math.max(from, to) + 1))
}

function toggleAllVisible() {
  actions.toggleAllVisible()
  anchorId.value = ''
}

function clearSelected() {
  actions.clearSelected()
  anchorId.value = ''
}

/** @param {any} row */
function playRow(row) {
  actions.playRow(row)
}

function playVisible() {
  actions.playVisible()
}

/** @param {string[]} ids */
function discard(ids) {
  actions.discard([...ids])
}
</script>

<style scoped>
.audio-page {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.audio-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px var(--sh-gutter) 10px;
}
.audio-run {
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: transparent;
  color: var(--sh-text-3);
  cursor: pointer;
}
.audio-run:hover:not(:disabled) {
  background: var(--sh-hover);
  color: var(--sh-accent);
}
.audio-run:disabled {
  opacity: 0.4;
  cursor: default;
}
.audio-head h1 {
  margin: 0;
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 20px;
  letter-spacing: -0.012em;
}
.audio-sum {
  font-size: 12px;
  font-weight: 400;
  color: var(--sh-text-3);
}
.audio-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 var(--sh-gutter) 10px;
}
.audio-chip {
  height: 24px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--sh-line);
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.audio-chip:hover {
  color: var(--sh-text);
  border-color: var(--sh-line);
  background: var(--sh-hover);
}
.audio-chip.is-on {
  color: var(--sh-accent);
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.audio-divider {
  width: 1px;
  height: 16px;
  background: var(--sh-line);
  margin: 0 4px;
}
.audio-clear {
  border: 0;
  background: transparent;
  color: var(--sh-accent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
}
.audio-bulk {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px var(--sh-gutter);
  background: var(--sh-accent-soft);
  border-top: 1px solid var(--sh-line-soft);
  border-bottom: 1px solid var(--sh-line-soft);
}
.audio-bulk-count {
  font-size: 12px;
  color: var(--sh-text-2);
}
.audio-table {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.audio-tr {
  display: grid;
  grid-template-columns: 32px 104px minmax(0, 1fr) 84px 118px 76px 100px;
  gap: 10px;
  align-items: center;
  padding: 9px var(--sh-gutter);
  border-bottom: 1px solid var(--sh-line-soft);
}
.audio-tr.is-head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--sh-canvas);
  font-size: 12px;
  color: var(--sh-text-3);
  border-bottom-color: var(--sh-line);
}
.audio-tr.is-picked {
  background: var(--sh-accent-soft);
}
.audio-tr.is-playing {
  box-shadow: inset 3px 0 0 var(--sh-accent);
}
.audio-pick {
  display: flex;
  align-items: center;
}
/* 行首序号与表头的 # 是同一种按钮：点一下选中这一行，选中只换底色。
   与创作行表、配音段落表一致，不另用原生复选框。 */
.audio-ord {
  min-width: 28px;
  height: 20px;
  padding: 0 5px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-2xs);
  background: transparent;
  color: var(--sh-text-4);
  font: inherit;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}
.audio-ord:hover {
  border-color: var(--sh-line-soft);
  background: var(--sh-panel);
  color: var(--sh-text-2);
}
.audio-ord.is-on {
  border-color: transparent;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
}
.audio-ord.is-mixed {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.audio-listen {
  display: flex;
  align-items: center;
  gap: 9px;
}
.audio-play {
  width: 24px;
  height: 24px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  border: 1px solid var(--sh-line);
  background: transparent;
  color: var(--sh-text-2);
  cursor: pointer;
}
.audio-play:hover:not(:disabled) {
  border-color: var(--sh-accent);
  color: var(--sh-accent);
}
.audio-play:disabled {
  opacity: 0.4;
  cursor: default;
}
.audio-play.is-on {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.audio-clock {
  font-size: 12px;
  color: var(--sh-text-3);
  font-variant-numeric: tabular-nums;
}
.audio-body {
  min-width: 0;
}
.audio-text {
  display: block;
  font-size: 13px;
  color: var(--sh-text);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.audio-text.is-file {
  color: var(--sh-text-2);
}
.audio-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  min-width: 0;
}
.audio-meta-text {
  font-size: 11px;
  color: var(--sh-text-4);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
/* 情绪是这条音频自己的属性（会随入库写进样例），给一枚彩色小牌，与来源、参数分开。 */
.audio-emo {
  flex: none;
  padding: 0 7px;
  font-size: 11px;
  line-height: 16px;
}
.audio-src {
  justify-self: start;
  padding: 2px 6px;
  border-radius: var(--sh-r-2xs);
  font-size: 11px;
}
.audio-src.is-record {
  color: var(--sh-ok);
  background: var(--sh-ok-soft);
}
.audio-src.is-loopback {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}
.audio-src.is-engine {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.audio-voice,
.audio-stamp {
  font-size: 12px;
  color: var(--sh-text-2);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.audio-stamp {
  font-size: 11px;
  color: var(--sh-text-3);
  font-variant-numeric: tabular-nums;
}
.audio-ops {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}
.audio-ops-head {
  text-align: right;
}
.audio-done {
  font-size: 11px;
  color: var(--sh-ok);
}
.audio-more {
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: transparent;
  color: var(--sh-text-3);
  cursor: pointer;
}
.audio-more:hover {
  background: var(--sh-hover);
  color: var(--sh-accent);
}
.audio-more-note {
  padding: 10px var(--sh-gutter);
  font-size: 11px;
  color: var(--sh-text-4);
  border-bottom: 1px solid var(--sh-line-soft);
}
.audio-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 60px 20px;
  color: var(--sh-text-3);
  font-size: 13px;
}
.audio-empty p {
  margin: 0;
}
.spacer {
  flex: 1;
}
</style>
