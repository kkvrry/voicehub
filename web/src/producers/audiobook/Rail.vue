<template>
  <section class="producer-rail">
    <div class="rail-book">
      <span class="rail-book-mark" aria-hidden="true">
        <BookOpen :size="14" :stroke-width="1.8" />
      </span>
      <div class="rail-book-main">
        <button
          v-if="projects.length"
          ref="pickerEl"
          type="button"
          class="rail-book-picker"
          :class="{ 'is-open': pickerOpen }"
          aria-haspopup="listbox"
          :aria-expanded="pickerOpen"
          title="切换书籍"
          @click="pickerOpen = !pickerOpen"
        >
          <span class="rail-book-name">{{ detail?.name || projects[0]?.name }}</span>
          <ChevronDown class="rail-book-caret" :size="14" :stroke-width="1.8" />
        </button>
        <div v-else class="rail-book-empty">未导入书稿</div>
        <div v-if="detail" class="rail-book-meta">
          {{ chapters.length }} 章 · {{ totalRows }} 行
          <template v-if="detail.author"> · {{ detail.author }}</template>
        </div>
      </div>

      <div class="rail-book-actions">
        <button
          class="rail-book-icon"
          title="项目管理"
          aria-label="项目管理"
          @click="openProjectManager"
        >
          <FolderCog :size="14" :stroke-width="1.8" />
        </button>
      </div>
    </div>

    <Popover
      :open="pickerOpen"
      :anchor="pickerEl"
      :width="260"
      label="切换书籍"
      @close="pickerOpen = false"
    >
      <ul class="book-menu" role="listbox" aria-label="切换书籍">
        <li v-for="item in projects" :key="item.id">
          <button
            type="button"
            role="option"
            class="book-option"
            :class="{ on: item.id === detail?.id }"
            :aria-selected="item.id === detail?.id"
            @click="pickProject(item.id)"
          >
            <span class="book-option-name">{{ item.name }}</span>
            <Check v-if="item.id === detail?.id" :size="14" :stroke-width="1.9" />
          </button>
        </li>
      </ul>
    </Popover>

    <div class="rail-chapters">
      <div
        v-for="chapter in chapters"
        :key="chapter.id"
        class="chapter-row"
        :class="{ 'is-active': chapter.id === chapterId, 'is-absent': !available(chapter) }"
        @contextmenu="onChapterMenu(chapter, $event)"
      >
        <button
          type="button"
          class="chapter-item"
          :title="chapterTitle(chapter)"
          @click="pickChapter(chapter.id)"
        >
          <span class="chapter-ord">{{ chapter.ordinal + 1 }}</span>
          <span class="chapter-title" @dblclick.stop="renamePrompt(chapter)">{{ chapter.title }}</span>
          <span v-if="chapter.rows" class="chapter-rows" :title="`${chapter.rows} 行`">
            {{ chapter.rows }} 行
          </span>
          <!-- 一章一盏灯：跟着行表的筛选走——筛「失败」就只有失败的行让灯亮，
               筛「全部」时四类合成一盏（未分配 / 待生成 / 失败 / 需校对）。
               具体是哪一类、还剩几行，点进来在行表上方的筛选档里看——不在目录栏分第二遍。 -->
          <span
            class="chapter-lamp"
            :class="{ 'is-alert': issueCount(chapter) > 0 }"
            :title="issueTitle(chapter)"
          />
        </button>
        <button
          type="button"
          class="chapter-del"
          title="删除本章"
          @click.stop="removeChapter(chapter)"
        >
          <Trash2 :size="12" :stroke-width="1.8" />
        </button>
      </div>
      <div v-if="!chapters.length" class="rail-empty">尚未分章</div>
    </div>
  </section>
</template>

<script setup>
// @ts-check
import { computed, ref } from 'vue'
import { BookOpen, Check, ChevronDown, FolderCog, Trash2 } from 'lucide-vue-next'
import Popover from '../../components/ui/Popover.vue'
import { actions, store as appStore } from '../../store'
import { canTakeOver, openObjectMenu } from '../../composables/contextMenu'
import { chapterVisible } from './visible'
import { confirmAction } from './confirm'
import {
  deleteChapter,
  openProject,
  openProjectManager,
  renameChapter,
  selectChapter,
  state,
} from './store'

const detail = computed(() => state.detail)
const chapters = computed(() => detail.value?.chapters || [])
const chapterId = computed(() => state.chapterId)
const projects = computed(() => state.overview.projects || [])
const speakerFilter = computed(() => state.speakerFilter || '')

//: 书籍切换用主题内的弹层，不用原生 <select>：后者的下拉列表由系统画，
//: 夜间模式下会跳出一张白底列表，与页面配色对不上。
const pickerEl = ref(/** @type {any} */ (null))
const pickerOpen = ref(false)

/**
 * 这一章还有没有通过筛选链的行：与行表、试听队列、滚轮换章同一份判据。
 * 当前章用已加载的行实时判，改角色后立刻跟着变；其余章看详情里的聚合。
 * 状态档也参与——筛「失败」时，没有失败行的章一样被标成排除。
 *
 * @param {any} chapter
 */
function available(chapter) {
  return chapterVisible(chapter, {
    filter: rowFilter.value,
    speaker: speakerFilter.value,
    rows: state.rows,
    currentId: chapterId.value,
  })
}

/**
 * @param {any} chapter
 */
function chapterTitle(chapter) {
  const base = `${chapter.title}（双击重命名）`
  return available(chapter) ? base : `${base} · 当前筛选下没有可看的行`
}
const totalRows = computed(() =>
  chapters.value.reduce(
    /** @param {number} sum @param {any} chapter */
    (sum, chapter) => sum + (Number(chapter.rows) || 0),
    0
  )
)

/**
 * @param {any} id
 */
function pickProject(id) {
  pickerOpen.value = false
  if (id !== detail.value?.id) void openProject(id)
}

/** 行表的两种"要人动手"筛选：灯跟着当前选中的那一档，而不是永远报告全部问题。
 *  「需校对」的章节数由后端随详情给出（行文本扫描，与行表同一句判据）。
 *  未定角色与待生成不进灯：行上自带标记，也不再有对应的筛选档。 */
const FILTER_LABELS = {
  failed: '失败',
  broken: '需校对',
}
const rowFilter = computed(() => state.rowFilter || 'all')

/**
 * @param {any} chapter
 */
function issueCount(chapter) {
  const selected = /** @type {Record<string, string>} */ (FILTER_LABELS)[rowFilter.value]
  if (selected) return Number(chapter[rowFilter.value]) || 0
  // 全部：两类任一有行都该让灯亮。「需校对」与「失败」可能重叠，
  // 这里只判有没有，不做"去重后总数"。
  return Object.keys(FILTER_LABELS).reduce(
    (count, key) => count + (Number(chapter[key]) || 0),
    0
  )
}

/**
 * @param {any} chapter
 */
function issueTitle(chapter) {
  const selected = /** @type {Record<string, string>} */ (FILTER_LABELS)[rowFilter.value]
  if (selected) {
    const count = Number(chapter[rowFilter.value]) || 0
    return count ? `本章有 ${count} 行「${selected}」` : `本章没有「${selected}」的行`
  }
  const parts = Object.entries(FILTER_LABELS)
    .map(([key, label]) => ({ count: Number(chapter[key]) || 0, label }))
    .filter((item) => item.count > 0)
    .map((item) => `${item.label} ${item.count}`)
  return parts.length ? `本章待处理：${parts.join(' · ')}` : '本章没有待处理的行'
}

/**
 * @param {any} id
 */
function pickChapter(id) {
  if (id === chapterId.value) return
  void selectChapter(id)
}

/**
 * @param {any} chapter
 */
async function renamePrompt(chapter) {
  const title = await confirmAction({
    title: '重命名章节',
    confirmText: '保存',
    danger: false,
    input: { type: 'text', placeholder: '章节标题' },
    value: chapter.title,
  })
  if (!title || title === chapter.title) return
  void renameChapter(title, chapter.id)
}

/**
 * @param {any} chapter
 */
async function removeChapter(chapter) {
  const answer = await confirmAction({
    title: '删除本章',
    message: `「${chapter.title}」连同 ${chapter.rows} 行、逐行音频一起删除，后面的章序号会补齐。`,
    confirmText: '删除本章',
  })
  if (!answer) return
  void deleteChapter(chapter.id)
}

/** @param {any} chapter */
function chapterAudioUrlOf(chapter) {
  if (!detail.value || !chapter?.audio) return ''
  return `/api/v1/producers/${appStore.producer.key}/projects/${detail.value.id}/chapters/${chapter.id}/audio`
}

/** 菜单里的「播放本章」不必先把这一章切到中间：直接放它的成品音频。 */
/** @param {any} chapter */
function playChapterAudio(chapter) {
  const url = chapterAudioUrlOf(chapter)
  if (!url) return
  actions.play({ url, label: chapter.title, sub: detail.value?.name || '' })
}

/** @param {any} chapter @param {MouseEvent} event */
function onChapterMenu(chapter, event) {
  if (!canTakeOver(event)) return
  openObjectMenu(
    'audiobook.chapter',
    {
      title: chapter.title,
      hasAudio: Boolean(chapter.audio),
      play: () => playChapterAudio(chapter),
      rename: () => renamePrompt(chapter),
      remove: () => removeChapter(chapter),
    },
    { event }
  )
}
</script>

<style scoped>
/* 工作区右列的一张卡：刊头固定，章节列表自己滚。 */
.producer-rail {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 12px 10px 0;
  overflow: hidden;
}
/* 刊头：一块书脊 + 书名 + 体量，下面是章节账目。
   有它以后，这一列不再是"一串没有出处的章节"。 */
.rail-book {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 0 2px 8px;
  border-bottom: 1px solid var(--sh-line-soft);
}
.rail-book-mark {
  flex: none;
  width: 26px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 1px;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
  box-shadow: inset 2px 0 0 var(--sh-accent);
}
.rail-book-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rail-book-meta {
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sh-text-4);
  letter-spacing: 0.03em;
  font-variant-numeric: tabular-nums;
}
.rail-book-actions {
  flex: none;
  display: flex;
  gap: 1px;
}
.rail-book-picker {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  min-width: 0;
  width: fit-content;
  max-width: 100%;
  padding: 1px 3px;
  border: 0;
  border-radius: var(--sh-r-sm);
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.rail-book-picker:hover,
.rail-book-picker.is-open,
.rail-book-picker:focus-visible {
  background: var(--sh-hover);
}
.rail-book-picker:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--sh-accent-ring);
}
.rail-book-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--sh-text);
}
.rail-book-caret {
  flex: none;
  color: var(--sh-text-4);
}
/* 书籍清单：与情绪菜单同一套配色（令牌全走主题），夜间模式不会跳出白底列表。 */
.book-menu {
  margin: 0;
  padding: 4px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 320px;
  overflow-y: auto;
}
.book-option {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.book-option:hover,
.book-option:focus-visible {
  outline: none;
  background: var(--sh-hover);
}
.book-option.on {
  color: var(--sh-accent);
  font-weight: 600;
}
.book-option-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-book-empty {
  font-size: 12px;
  color: var(--sh-text-4);
}
.rail-book-icon {
  flex: none;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--sh-r-sm);
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
}
.rail-book-icon:hover {
  background: var(--sh-hover);
  color: var(--sh-text-2);
}
.rail-chapters {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.chapter-row {
  position: relative;
}
.chapter-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 26px 6px 8px;
  border: 0;
  border-radius: var(--sh-r-sm);
  background: transparent;
  color: var(--sh-text-2);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.chapter-item:hover {
  background: var(--sh-hover);
}
.chapter-row.is-active .chapter-item {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
  font-weight: 600;
}
.chapter-del {
  position: absolute;
  right: 3px;
  top: 50%;
  transform: translateY(-50%);
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: transparent;
  color: var(--sh-text-4);
  opacity: 0;
  cursor: pointer;
  transition: opacity var(--sh-t-fast) var(--sh-ease), color var(--sh-t) var(--sh-ease);
}
.chapter-row:hover .chapter-del,
.chapter-row.is-active .chapter-del,
.chapter-del:focus-visible {
  opacity: 1;
}
.chapter-del:hover {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
}
.chapter-ord {
  flex: none;
  width: 18px;
  font-size: 10.5px;
  color: var(--sh-text-4);
  text-align: right;
}
.chapter-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chapter-rows {
  flex: none;
  font-size: 10.5px;
  color: var(--sh-text-4);
}
/* 灯固定在最右：不管章名多长，一排看下来是齐的。
   只有两种状态——有事红、没事灰。阶段色不在这里表达（主区标题旁边写着
   「拆句 / 分析 / 已合并」，那才是它该出现的地方），否则"灭了"也像个红点。 */
.chapter-lamp {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sh-line);
}
/* 有事必须处理：实心红 + 一圈淡红光晕，扫一眼整列就知道哪几章还欠着。 */
.chapter-lamp.is-alert {
  width: 10px;
  height: 10px;
  background: var(--sh-danger);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--sh-danger) 20%, transparent);
}
.rail-empty {
  padding: 8px 6px;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
/* 角色筛选下不含该角色的章：降色 + 删除线，仍可点击进入。 */
/* 被筛选链排除的章：整行压暗、章名划掉，和还能看的章一眼分得开。
   灯与行数跟着整行一起淡下去，不再各留一档透明度（叠起来会淡到看不见）。 */
.chapter-row.is-absent .chapter-item {
  color: var(--sh-text-4);
  opacity: 0.45;
}
.chapter-row.is-absent .chapter-title {
  text-decoration: line-through;
  text-decoration-color: var(--sh-text-4);
  text-decoration-thickness: 1px;
}
/* 正看着的章即使被筛掉也要认得出来：保留强调色，但比正常章暗一档。 */
.chapter-row.is-active.is-absent .chapter-item {
  color: var(--sh-accent);
  opacity: 0.6;
}
</style>
