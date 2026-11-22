<template>
  <section
    class="app-panel dub"
    :class="{ 'is-drop': dragging }"
    @dragover.prevent="onDragFile"
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <header class="panel-head dub-head">
      <div class="panel-head-main dub-heading">
        <div class="engine-title">
          配音
          <span v-if="project" class="badge gray">{{ kindLabel }}</span>
          <span v-if="project" class="badge gray">{{ durationLabel }}</span>
          <span
            v-if="project && project.source_linked"
            class="badge gray"
            title="源文件留在原处，没有复制进项目"
          >引用</span>
        </div>
        <div
          v-if="project"
          class="dub-source-line"
          :title="project.source_linked ? project.source_path : project.source_name"
        >
          <template v-if="!sourceOk">
            <span class="dub-source-missing">源文件不在了</span>
            <button
              v-if="project.source_linked"
              type="button"
              class="dub-source-link"
              :disabled="state.linking || working"
              @click="onRelink"
            >{{ state.linking ? '重新指定中…' : '重新指定' }}</button>
          </template>
          <template v-else>
            {{ project.source_name }} · 第 {{ (project.stream_index ?? 0) + 1 }} 条音轨
            <template v-if="project.source_linked"> · 引用，未复制</template>
          </template>
        </div>
      </div>

      <div class="dub-head-tools">
        <select
          v-if="state.projects.length"
          class="sh-field dub-pick"
          :value="state.currentId"
          aria-label="选择项目"
          @change="onPickEvent($event)"
        >
          <option value="">选择项目…</option>
          <option v-for="item in state.projects" :key="item.id" :value="item.id">
            {{ item.name }}（{{ item.cue_count }} 段）
          </option>
        </select>
        <button
          v-if="project"
          class="sh-btn is-sm is-icon"
          type="button"
          title="重命名项目"
          aria-label="重命名项目"
          @click="onRename"
        >
          <Pencil :size="14" :stroke-width="1.8" />
        </button>
        <button
          v-if="project"
          class="sh-btn is-sm is-icon is-danger"
          type="button"
          :disabled="working"
          :title="working ? '先等当前任务结束' : '删除项目'"
          aria-label="删除项目"
          @click="onRemove"
        >
          <Trash2 :size="14" :stroke-width="1.8" />
        </button>
        <button
          class="sh-btn is-sm is-soft"
          type="button"
          :disabled="state.uploading || state.linking || working"
          :title="working ? '先等当前任务结束' : ''"
          @click="pick"
        >
          <Upload :size="14" :stroke-width="1.8" />
          {{ state.uploading ? '导入中…' : '导入音视频' }}
        </button>
        <button
          class="sh-btn is-sm is-soft"
          type="button"
          :disabled="state.linking || state.uploading || working"
          :title="
            working
              ? '先等当前任务结束'
              : '选中电脑上的原片，只登记路径，不复制进项目'
          "
          @click="onLink"
        >
          <FileSymlink :size="14" :stroke-width="1.8" />
          {{ state.linking ? '处理中…' : '引用本机文件' }}
        </button>
        <button
          class="sh-btn is-sm is-primary"
          type="button"
          :disabled="!project || working || !canExport"
          :title="working ? '先等当前任务结束' : exportTitle"
          @click="onExport"
        >
          <Download :size="14" :stroke-width="1.8" />
          {{ exportRunning ? '导出中…' : '导出成片' }}
        </button>
        <input ref="fileEl" type="file" accept="audio/*,video/*" class="dub-file" @change="onFile" />
      </div>
    </header>

    <div v-if="!state.loaded" class="dub-splash">载入中</div>

    <div v-else-if="!project" class="dub-empty-body">
      <button class="dropzone" type="button" @click="pick">
        <Upload :size="26" :stroke-width="1.6" />
        <b>把音频或视频拖到这里</b>
        <span>导入后按停顿切段转写；也可以导入现成的 SRT / VTT 字幕当段落表</span>
        <span class="dropzone-note">
          支持 mp4 / mov / mkv / wav / mp3 / m4a / flac；大文件可点「引用本机文件」，不复制原片
        </span>
      </button>
      <p v-if="state.asr.note" class="dub-empty-hint warn">{{ state.asr.note }}</p>

      <section v-if="state.projects.length" class="recent" aria-label="最近项目">
        <header class="recent-head">
          <span>最近项目</span>
          <span>{{ state.projects.length }} 个</span>
        </header>
        <button
          v-for="item in state.projects.slice(0, 6)"
          :key="item.id"
          type="button"
          class="recent-row"
          @click="onPick(item.id)"
        >
          <span class="recent-name">{{ item.name }}</span>
          <span class="recent-meta">{{ item.cue_count }} 段 · {{ item.ready_count }} 已配音</span>
          <span class="recent-go">打开</span>
        </button>
      </section>
    </div>

    <template v-else>
      <CastBar />
      <div class="dub-body">
        <StagePanel />
        <WorkflowPanel />
      </div>
    </template>

    <p v-if="state.error" class="dub-error" role="alert">{{ state.error }}</p>
    <div v-if="dragging" class="dub-drop-veil" aria-hidden="true">
      {{ isSubtitleDrag ? '松手导入字幕' : '松手导入音视频' }}
    </div>
  </section>
</template>

<script setup>
// @ts-check
// 配音工作区：页头（项目 / 导入 / 导出）+ 横向角色条 + 预览与字幕 + 工作流。
// 音色从主壳左侧的卡片列挑（宿主按插件声明的 `voice_rail` 摆），这里不另存一份。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, FileSymlink, Pencil, Trash2, Upload } from 'lucide-vue-next'
import './workspace.css'
import CastBar from './components/CastBar.vue'
import StagePanel from './components/StagePanel.vue'
import WorkflowPanel from './components/WorkflowPanel.vue'
import {
  exportBlockers,
  exportFilm,
  importSubtitles,
  linkLocalProject,
  mergeCue,
  open,
  project,
  refresh,
  relinkSource,
  removeProject,
  renameProject,
  select,
  selectedCue,
  splitCue,
  state,
  uploadFile,
} from './store'

const fileEl = ref(/** @type {HTMLInputElement | null} */ (null))
const dragging = ref(false)
const dragFile = ref(/** @type {File | null} */ (null))

const kindLabel = computed(() => (project.value?.kind === 'video' ? '视频' : '音频'))
const sourceOk = computed(() => Boolean(state.detail?.source_ok))
const durationLabel = computed(() => {
  const total = Math.round((project.value?.duration_ms || 0) / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
})

/** 贴齐、合成或转录还在跑：导出要先等它们结束，不能同时写同一批产物。 */
const working = computed(
  () => Boolean(state.busy) || state.detail?.job?.state === 'running'
)
const exportRunning = computed(
  () =>
    state.busy === 'export' ||
    (state.detail?.job?.kind === 'export' && state.detail.job.state === 'running')
)
const canExport = computed(() => Number(state.detail?.stats?.voiced || 0) > 0)
const exportTitle = computed(() => {
  if (!project.value) return '先导入音视频'
  if (!canExport.value) return '还没有合成任何配音'
  const blockers = exportBlockers.value
  const parts = []
  if (blockers.stale) parts.push(`${blockers.stale} 段待重配`)
  if (blockers.failed) parts.push(`${blockers.failed} 段失败`)
  if (blockers.pending) parts.push(`${blockers.pending} 段未配音`)
  return parts.length
    ? `导出前会按当前段落重新混音；${parts.join('、')}，会先请你确认`
    : '按当前段落重新贴齐、混音并导出'
})

const isSubtitleDrag = computed(() =>
  /\.(srt|vtt)$/i.test(String(dragFile.value?.name || ''))
)

function pick() {
  fileEl.value?.click()
}

async function onLink() {
  if (working.value || state.uploading || state.linking) return
  await linkLocalProject()
}

async function onRelink() {
  if (working.value || state.linking) return
  await relinkSource()
}

/** @param {Event} event */
async function onFile(event) {
  const input = /** @type {HTMLInputElement} */ (event.target)
  const file = input.files?.[0]
  input.value = ''
  if (file) await uploadFile(file)
}

/** @param {DragEvent} event */
async function onDrop(event) {
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  dragFile.value = null
  if (!file) return
  if (/\.(srt|vtt)$/i.test(file.name)) {
    if (!project.value) {
      state.error = '先导入音视频，再导入字幕。'
      return
    }
    await importSubtitles(file)
    return
  }
  await uploadFile(file)
}

/** @param {DragEvent} event */
function onDragFile(event) {
  dragFile.value = event.dataTransfer?.files?.[0] || null
  dragging.value = true
}

/** @param {DragEvent} event */
function onDragLeave(event) {
  // 掠过子元素也会触发 dragleave：指针还在工作区里时不撤遮罩。
  const box = event.currentTarget
  if (box instanceof HTMLElement) {
    const rect = box.getBoundingClientRect()
    const { clientX, clientY } = event
    if (
      clientX >= rect.left &&
      clientX <= rect.right &&
      clientY >= rect.top &&
      clientY <= rect.bottom
    ) {
      return
    }
  }
  dragging.value = false
  dragFile.value = null
}

/** @param {string} id */
function onPick(id) {
  void select(id)
}

/** @param {Event} event */
function onPickEvent(event) {
  const target = /** @type {HTMLSelectElement | null} */ (event.target)
  onPick(target?.value || '')
}

async function onRename() {
  if (!project.value) return
  const name = window.prompt('项目名', project.value.name)
  if (name === null) return
  const value = name.trim()
  if (!value || value === project.value.name) return
  await renameProject(value)
}

async function onRemove() {
  if (!project.value) return
  // 引用项目的原片在项目外：删项目不能让人以为连原片一起删了。
  const tail = project.value.source_linked
    ? '项目里的音轨与产物会删除，引用的原片留在原处。'
    : '源文件与产物一并删除。'
  if (!window.confirm(`删除项目「${project.value.name}」？${tail}`)) return
  await removeProject()
}

async function onExport() {
  if (!project.value || !canExport.value || working.value) return
  const blockers = exportBlockers.value
  const parts = []
  if (blockers.stale) parts.push(`${blockers.stale} 段待重配`)
  if (blockers.failed) parts.push(`${blockers.failed} 段失败`)
  if (blockers.pending) parts.push(`${blockers.pending} 段未配音`)
  if (parts.length) {
    const ok = window.confirm(
      `${parts.join('、')}。这样导出的成片可能与字幕不一致；要用旧音频继续导出吗？`
    )
    if (!ok) return
  }
  await exportFilm(parts.length > 0)
}

/** @param {any} target */
function isTyping(target) {
  const tag = (target?.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select'
}

/** @param {KeyboardEvent} event */
async function onKey(event) {
  if (!project.value || isTyping(event.target)) return
  const cue = selectedCue()
  if (!cue) return
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    const at = Math.round(state.playTime * 1000)
    if (at > cue.start_ms && at < cue.end_ms) await splitCue(cue, at)
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'j') {
    event.preventDefault()
    await mergeCue(cue)
  }
}

let timer = 0

onMounted(async () => {
  await open()
  window.addEventListener('keydown', onKey)
  // 任务在后台跑时靠这一条把步骤状态刷新出来；空闲时 4 秒一次足够。
  timer = window.setInterval(() => {
    if (!state.currentId) return
    if (state.detail?.job?.state === 'running' || state.busy) return
    void refresh({ silent: true })
  }, 4000)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.dub {
  position: relative;
  min-height: 0;
  overflow: hidden;
}

.dub-head {
  flex-wrap: wrap;
  row-gap: 8px;
  padding-bottom: 9px;
}

.dub-heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 140px;
}

.dub-source-line {
  max-width: min(420px, 40vw);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.dub-source-missing {
  color: var(--sh-warn);
}

/* 行内文字按钮：源文件丢了时，修复动作就在提示旁边，不必去翻别处。 */
.dub-source-link {
  margin-left: 6px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--sh-accent);
  font: inherit;
  cursor: pointer;
}

.dub-source-link:hover {
  text-decoration: underline;
}

.dub-head-tools {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-left: auto;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.dub-pick {
  width: 190px;
  height: 30px;
}

.dub-file { display: none; }

.dub-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 292px;
  gap: 12px;
  padding: 0 22px 14px;
}

@media (max-width: 1280px) {
  .dub-body { grid-template-columns: minmax(0, 1fr) 260px; }
}

@media (max-width: 980px) {
  .dub {
    overflow-y: auto;
  }

  /* 单列时两行都按内容走，页面自己滚；继续用 1fr + auto 会让工作流那一行
     只分到剩余高度，步骤列表被 overflow 裁掉。 */
  .dub-body {
    flex: none;
    min-height: auto;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: 460px max-content;
  }
}

.dub-splash,
.dub-empty-body {
  flex: 1;
  min-height: 0;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 10px;
  padding: 20px;
  overflow-y: auto;
}

.dub-empty-body { grid-auto-rows: min-content; }

.dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  width: min(560px, 82%);
  padding: 40px 28px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-r-lg);
  background: var(--sh-field);
  color: var(--sh-text-3);
  transition: border-color var(--sh-t-fast) var(--sh-ease), background var(--sh-t-fast) var(--sh-ease);
}

.dropzone:hover {
  border-color: var(--sh-accent-ring);
  background: var(--sh-accent-soft);
}

.dropzone b {
  font-size: 14px;
  color: var(--sh-text);
}

.dropzone span {
  font-size: 12px;
  line-height: 1.6;
}

.dropzone .dropzone-note {
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.dub-empty-hint {
  font-size: 11.5px;
  color: var(--sh-text-3);
}

.dub-empty-hint.warn { color: var(--sh-warn); }

.recent {
  width: min(560px, 82%);
  margin-top: 4px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  overflow: hidden;
}

.recent-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border-bottom: 1px solid var(--sh-line-soft);
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.recent-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border-bottom: 1px solid var(--sh-line-soft);
  text-align: left;
}

.recent-row:last-child { border-bottom: 0; }
.recent-row:hover { background: var(--sh-hover); }

.recent-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--sh-text);
}

.recent-meta {
  flex: none;
  font-size: 10.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.recent-go {
  flex: none;
  font-size: 10.5px;
  color: var(--sh-accent);
}

.dub-error {
  flex: none;
  margin: 0 22px 12px;
  padding: 6px 10px;
  border-radius: var(--sh-r-xs);
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
  font-size: 11.5px;
}

.dub-drop-veil {
  position: absolute;
  inset: 6px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--sh-accent-ring);
  border-radius: var(--sh-r-md);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
  font-size: 13px;
  font-weight: 600;
  pointer-events: none;
}
</style>
