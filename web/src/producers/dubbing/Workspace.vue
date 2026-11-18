<template>
  <section
    class="app-panel dub"
    :class="{ 'is-drop': dragging }"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="engine-title">
          配音
          <span v-if="project" class="badge gray">{{ kindLabel }}</span>
          <span v-if="project" class="badge gray">{{ durationLabel }}</span>
        </div>
      </div>

      <select
        v-if="state.projects.length"
        class="sh-field dub-pick"
        :value="state.currentId"
        aria-label="选择项目"
        @change="onPick($event.target.value)"
      >
        <option value="">选择项目…</option>
        <option v-for="item in state.projects" :key="item.id" :value="item.id">
          {{ item.name }}（{{ item.cue_count }} 段）
        </option>
      </select>

      <button class="sh-btn is-soft" type="button" :disabled="state.uploading" @click="pick">
        <Upload :size="14" :stroke-width="1.8" />
        {{ state.uploading ? '导入中…' : '导入音视频' }}
      </button>
      <button
        v-if="project"
        class="sh-btn is-ghost is-icon"
        type="button"
        title="删除项目"
        aria-label="删除项目"
        @click="onRemove"
      >
        <Trash2 :size="14" :stroke-width="1.8" />
      </button>
      <button
        class="sh-btn is-primary"
        type="button"
        :disabled="!project || state.exporting"
        @click="onExport"
      >
        <Download :size="14" :stroke-width="1.8" />
        {{ state.exporting ? '导出中…' : '导出成片' }}
      </button>
      <input ref="fileEl" type="file" accept="audio/*,video/*" class="dub-file" @change="onFile" />
    </header>

    <div v-if="!state.loaded" class="dub-splash">载入中</div>

    <div v-else-if="!project" class="dub-empty-body">
      <button class="dropzone" type="button" @click="pick">
        <Upload :size="26" :stroke-width="1.6" />
        <b>把音频或视频拖到这里</b>
        <span>导入后按停顿切段转写；字幕即时间轴，段落可逐句修改</span>
        <span class="dropzone-note">支持 mp4 / mov / mkv / wav / mp3 / m4a / flac</span>
      </button>
      <p v-if="state.projects.length" class="dub-empty-hint">
        已有 {{ state.projects.length }} 个项目，从上方下拉里选择。
      </p>
      <p v-if="state.asr.note" class="dub-empty-hint warn">{{ state.asr.note }}</p>
    </div>

    <div v-else class="dub-body">
      <SourcePanel />
      <StagePanel />
      <WorkflowPanel />
    </div>

    <p v-if="state.error" class="dub-error" role="alert">{{ state.error }}</p>
    <div v-if="dragging" class="dub-drop-veil" aria-hidden="true">松手导入</div>
  </section>
</template>

<script setup>
// 工作区：三列（源与角色 / 预览与下区 / 工作流）。空态整块是拖放区。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, Trash2, Upload } from 'lucide-vue-next'
import './workspace.css'
import SourcePanel from './components/SourcePanel.vue'
import StagePanel from './components/StagePanel.vue'
import WorkflowPanel from './components/WorkflowPanel.vue'
import {
  exportFilm,
  mergeCue,
  open,
  project,
  refresh,
  removeProject,
  select,
  selectedCue,
  splitCue,
  state,
  uploadFile,
} from './store'

const fileEl = ref(null)
const dragging = ref(false)

const kindLabel = computed(() => (project.value?.kind === 'video' ? '视频' : '音频'))
const durationLabel = computed(() => {
  const total = Math.round((project.value?.duration_ms || 0) / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
})

function pick() {
  fileEl.value?.click()
}

async function onFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (file) await uploadFile(file)
}

async function onDrop(event) {
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  if (file) await uploadFile(file)
}

function onPick(id) {
  void select(id)
}

async function onRemove() {
  if (!project.value) return
  if (!window.confirm(`删除项目「${project.value.name}」？源文件与产物一并删除。`)) return
  await removeProject()
}

async function onExport() {
  await exportFilm()
}

function isTyping(target) {
  const tag = (target?.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select'
}

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
}

.dub-pick {
  max-width: 220px;
  height: 30px;
}

.dub-file { display: none; }

.dub-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 286px;
  gap: 12px;
  padding: 6px 18px 14px;
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
}

.dub-empty-body {
  grid-auto-rows: min-content;
}

.dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  width: min(560px, 80%);
  padding: 44px 28px;
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

.dub-error {
  flex: none;
  margin: 0 18px 12px;
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
