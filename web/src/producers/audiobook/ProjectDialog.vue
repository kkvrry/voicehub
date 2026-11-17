<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div class="vh-modal project-dialog" role="dialog" aria-modal="true" aria-label="项目管理">
          <header class="project-head">
            <div>
              <h2 class="project-title">项目管理</h2>
              <p class="project-note">
                {{ tab === 'import' ? '拖入文件或粘贴正文，直接建项目。' : '整理已有项目的书名、作者，或删除。' }}
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <SegmentedControl v-model="tab" label="项目管理栏目" :options="tabs" block />

          <div class="project-body">
            <!-- 导入就是这一栏本身：选文件 / 粘文本、起个书名、直接导入，不再开二级弹窗。 -->
            <div v-show="tab === 'import'" class="import-pane">
              <div class="import-tabs" role="tablist" aria-label="导入方式">
                <button
                  v-for="item in importModes"
                  :key="item.key"
                  type="button"
                  role="tab"
                  class="import-tab"
                  :class="{ 'is-active': importMode === item.key }"
                  :aria-selected="importMode === item.key"
                  @click="importMode = item.key"
                >
                  {{ item.label }}
                </button>
              </div>

              <template v-if="importMode === 'file'">
                <button
                  type="button"
                  class="drop-zone"
                  :class="{ 'is-over': over, 'is-filled': file }"
                  @click="pickFile"
                  @dragover.prevent="over = true"
                  @dragleave.prevent="over = false"
                  @drop.prevent="drop"
                >
                  <input
                    ref="input"
                    type="file"
                    class="drop-input"
                    accept=".txt,.md,.markdown,.epub,.mobi,.azw3,.azw,.prc,.srt"
                    @change="choose"
                  />
                  <template v-if="file">
                    <component :is="fileIcon" :size="22" :stroke-width="1.8" class="drop-icon" />
                    <span class="drop-name">{{ file.name }}</span>
                    <span class="drop-meta">{{ formatLabel }} · {{ sizeText }} · 点击重新选择</span>
                  </template>
                  <template v-else>
                    <UploadCloud :size="22" :stroke-width="1.8" class="drop-icon" />
                    <span class="drop-name">拖入文件，或点击选择</span>
                    <span class="drop-meta">
                      最大 64 MB；EPUB / MOBI / AZW3 沿用书里的章节，SRT 按时间轴一条一行
                    </span>
                  </template>
                </button>
                <label class="project-field" for="import-name">
                  书名
                  <input
                    id="import-name"
                    v-model="importName"
                    class="sh-field"
                    type="text"
                    placeholder="留空就用文件名"
                    autocomplete="off"
                  />
                </label>
              </template>

              <template v-else>
                <label class="project-field" for="import-name-text">
                  书名
                  <input
                    id="import-name-text"
                    v-model="importName"
                    class="sh-field"
                    type="text"
                    placeholder="未命名项目"
                    autocomplete="off"
                  />
                </label>
                <label class="project-field" for="import-text">
                  正文
                  <textarea
                    id="import-text"
                    v-model="importText"
                    class="sh-field import-text"
                    rows="9"
                    placeholder="粘贴全文；章节按「第X章」或 Markdown 标题切分。"
                  />
                </label>
              </template>

              <p v-if="storage" class="project-hint import-path" :title="storage">
                保存到：{{ storage }}
              </p>

              <div class="project-actions">
                <button class="sh-btn is-ghost" :disabled="importing" @click="$emit('close')">
                  取消
                </button>
                <span class="project-spacer"></span>
                <button class="sh-btn is-primary" :disabled="!canImport" @click="submitImport">
                  <Loader2 v-if="importing" :size="14" class="sh-spin" />
                  {{ importLabel }}
                </button>
              </div>
            </div>

            <template v-if="tab === 'manage'">
              <p v-if="!projects.length" class="project-hint">
                还没有项目。切到「导入」导一本书稿。
              </p>
              <template v-else>
                <ul class="project-list">
                  <li v-for="item in projects" :key="item.id">
                    <button
                      class="project-row"
                      :class="{ 'is-active': item.id === pickedId }"
                      :title="item.name"
                      @click="pick(item.id)"
                    >
                      <span class="project-row-main">
                        <span class="project-row-name">{{ item.name }}</span>
                        <span class="project-row-meta">
                          <template v-if="item.kind === 'subtitle'">字幕项目</template>
                          <template v-else>{{ item.chapters || 0 }} 章 · {{ item.rows || 0 }} 行</template>
                          <template v-if="item.author"> · {{ item.author }}</template>
                          <span v-if="item.id === detail?.id" class="project-row-tag">当前</span>
                        </span>
                      </span>
                      <Check v-if="item.id === pickedId" class="project-row-check" :size="14" />
                    </button>
                  </li>
                </ul>

                <section v-if="picked" class="project-group">
                  <div class="project-group-title">项目信息</div>
                  <label class="project-field" for="project-name">
                    书名
                    <input id="project-name" v-model="form.name" class="sh-field" type="text" />
                  </label>
                  <label class="project-field" for="project-author">
                    作者
                    <input
                      id="project-author"
                      v-model="form.author"
                      class="sh-field"
                      type="text"
                      placeholder="留空则不写成品标签"
                      spellcheck="false"
                    />
                  </label>
                  <p class="project-hint">作者写进成品标签的「艺术家」，合并时生效。</p>
                  <div class="project-actions">
                    <button class="sh-btn is-ghost project-danger" :disabled="busy" @click="remove">
                      <Trash2 :size="14" :stroke-width="1.8" />
                      删除项目
                    </button>
                    <span class="project-spacer"></span>
                    <button
                      class="sh-btn is-primary"
                      :disabled="!form.name.trim() || !dirty || busy"
                      @click="save"
                    >
                      {{ busy ? '保存中……' : '保存' }}
                    </button>
                  </div>
                </section>
              </template>
            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref, watch } from 'vue'
import { BookOpen, Check, FileText, Loader2, Trash2, UploadCloud, X } from 'lucide-vue-next'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import { confirmAction } from './confirm'
import {
  closeProjectManager,
  createProject,
  deleteProject,
  importFile,
  state,
  updateProject,
} from './store'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close'])

const detail = computed(() => state.detail)
const projects = computed(() => state.overview?.projects || [])
const importing = computed(() => state.importing)

const tabs = [
  { label: '导入', value: 'import' },
  { label: '管理', value: 'manage' },
]
const tab = ref('import')
const pickedId = ref('')
const busy = ref(false)
const form = reactive({ name: '', author: '' })

// ---- 导入栏：选文件 / 粘文本都在这里完成，不再另开一个对话框 --------------

const importModes = [
  { key: 'file', label: '上传文件' },
  { key: 'text', label: '粘贴文本' },
]
const importMode = ref('file')
const input = ref(/** @type {any} */ (null))
const file = ref(/** @type {any} */ (null))
const over = ref(false)
const importName = ref('')
const importText = ref('')
const storage = computed(() => state.overview?.storage || '')

const FORMATS = {
  '.txt': 'TXT',
  '.md': 'Markdown',
  '.markdown': 'Markdown',
  '.epub': 'EPUB',
  '.mobi': 'MOBI',
  '.azw3': 'AZW3',
  '.azw': 'AZW',
  '.prc': 'PRC',
  '.srt': 'SRT',
}
const suffix = computed(() => {
  const name = file.value?.name || ''
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
})
const formatLabel = computed(
  () =>
    /** @type {Record<string, string>} */ (FORMATS)[suffix.value] ||
    (suffix.value || '文件').toUpperCase()
)
const fileIcon = computed(() =>
  ['.epub', '.mobi', '.azw3', '.azw', '.prc'].includes(suffix.value) ? BookOpen : FileText,
)
const sizeText = computed(() => {
  const bytes = file.value?.size || 0
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
})
const canImport = computed(() => {
  if (importing.value) return false
  return importMode.value === 'file' ? Boolean(file.value) : Boolean(importText.value.trim())
})
const importLabel = computed(() => {
  if (importing.value) return '解析中……'
  if (importMode.value !== 'file') return '创建并分章'
  // 字幕自带时间码，导入后不再分章——按钮上就不写「分章」了
  return suffix.value === '.srt' ? '导入字幕' : '导入并分章'
})

function pickFile() {
  input.value?.click()
}

/**
 * @param {any} next
 */
function takeFile(next) {
  if (!next) return
  file.value = next
  if (!importName.value.trim()) importName.value = next.name.replace(/\.[^.]+$/, '')
}

/**
 * @param {any} event
 */
function choose(event) {
  takeFile(event.target.files?.[0])
  // 换一个同名文件时也要能再次触发 change。
  event.target.value = ''
}

/**
 * @param {any} event
 */
function drop(event) {
  over.value = false
  takeFile(event.dataTransfer?.files?.[0])
}

function resetImport() {
  importMode.value = 'file'
  file.value = null
  over.value = false
  importName.value = ''
  importText.value = ''
}

function submitImport() {
  if (!canImport.value) return
  // 成功后由 store 关掉这个弹窗并打开新项目；失败则留在这里，文件与书名都还在。
  if (importMode.value === 'file') {
    void importFile(file.value, importName.value.trim())
  } else {
    void createProject(importName.value.trim() || '未命名项目', importText.value)
  }
}

/** 管理栏只认清单里的条目：改完一个再点下一个，不用重新取详情。 */
const picked = computed(() => projects.value.find(/** @param {any} item */ (item) => item.id === pickedId.value) || null)

/**
 * @param {any} project
 */
function syncFrom(project) {
  form.name = project?.name || ''
  form.author = project?.author || ''
}

/**
 * @param {any} projectId
 */
function pick(projectId) {
  if (busy.value) return
  pickedId.value = projectId
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    tab.value = 'import'
    resetImport()
    pickedId.value = detail.value?.id || projects.value[0]?.id || ''
    syncFrom(picked.value)
  }
)
watch(
  () => [picked.value?.id, picked.value?.name, picked.value?.author],
  () => {
    if (!busy.value) syncFrom(picked.value)
  },
  { immediate: true }
)
// 打开管理栏时，清单可能刚被删/导入改过：选中的那条没了就落到当前书。
watch(tab, (value) => {
  if (value !== 'manage') return
  if (projects.value.some(/** @param {any} item */ (item) => item.id === pickedId.value)) return
  pickedId.value = detail.value?.id || projects.value[0]?.id || ''
})

const dirty = computed(
  () =>
    picked.value &&
    (form.name.trim() !== (picked.value.name || '') ||
      form.author.trim() !== (picked.value.author || ''))
)

async function save() {
  const project = picked.value
  if (!project) return
  const profile = {}
  if (form.name.trim() !== (project.name || '')) profile.name = form.name.trim()
  if (form.author.trim() !== (project.author || '')) profile.author = form.author.trim()
  if (!Object.keys(profile).length) return
  busy.value = true
  try {
    const saved = await updateProject(profile, project.id)
    if (saved) syncFrom(saved)
  } finally {
    busy.value = false
  }
}

async function remove() {
  const project = picked.value
  if (!project) return
  const answer = await confirmAction({
    title: '删除项目',
    message: `「${project.name}」及其音频、成品会被删除，无法还原。`,
    confirmText: '删除',
  })
  if (!answer) return
  const wasCurrent = project.id === detail.value?.id
  busy.value = true
  try {
    await deleteProject(project.id)
    pickedId.value = detail.value?.id || projects.value[0]?.id || ''
    // 删掉的就是当前这本时，创作页已经切到别的书了，弹窗没有可停留的上下文。
    if (wasCurrent) {
      closeProjectManager()
      emit('close')
    }
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.project-dialog {
  width: min(520px, 100%);
  padding: 18px 20px 16px;
  gap: 12px;
}
.project-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.project-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.project-note {
  margin: 3px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.project-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.project-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 13px 13px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 12px;
  background: var(--sh-glass-soft);
}
.project-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-4);
}
.project-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.project-field .sh-field {
  width: 100%;
}
.project-hint {
  margin: 0;
  font-size: 11.2px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
.project-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}
.project-spacer {
  flex: 1;
}
.project-danger {
  color: var(--sh-danger);
}
.project-danger:hover {
  background: var(--sh-danger-soft);
}

/* ---- 导入栏：选文件 / 粘文本 ---- */
.import-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.import-tabs {
  display: inline-flex;
  align-self: flex-start;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-glass-soft);
}
.import-tab {
  height: 26px;
  padding: 0 12px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11.8px;
  font-weight: 600;
  cursor: pointer;
}
.import-tab:hover {
  color: var(--sh-text-2);
}
.import-tab.is-active {
  background: var(--sh-glass-strong);
  color: var(--sh-text);
  box-shadow: var(--sh-shadow-1);
}
.drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  min-height: 132px;
  padding: 18px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-glass-soft);
  color: var(--sh-text-3);
  font: inherit;
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease);
}
.drop-zone:hover,
.drop-zone.is-over {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.drop-zone.is-filled {
  border-style: solid;
}
.drop-input {
  display: none;
}
.drop-icon {
  color: var(--sh-accent);
}
.drop-name {
  overflow: hidden;
  max-width: 100%;
  font-size: 12.8px;
  font-weight: 600;
  color: var(--sh-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.drop-meta {
  max-width: 100%;
  font-size: 11px;
  line-height: 1.5;
  color: var(--sh-text-4);
  text-align: center;
}
.import-text {
  height: auto;
  min-height: 168px;
  padding: 10px 11px;
  font-size: 12.6px;
  line-height: 1.65;
  resize: vertical;
}
.import-path {
  overflow: hidden;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 管理栏：项目清单 ---- */
.project-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 196px;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  border: 1px solid var(--sh-line-soft);
  border-radius: 12px;
  background: var(--sh-glass-soft);
  list-style: none;
}
.project-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease);
}
.project-row:hover {
  background: var(--sh-hover);
}
.project-row.is-active {
  background: var(--sh-accent-soft, var(--sh-hover));
  color: var(--sh-text);
}
.project-row-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.project-row-name {
  overflow: hidden;
  font-size: 12.5px;
  font-weight: 550;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.project-row-meta {
  overflow: hidden;
  font-size: 10.8px;
  color: var(--sh-text-4);
  white-space: nowrap;
  text-overflow: ellipsis;
}
.project-row-tag {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--sh-accent-soft, var(--sh-hover));
  color: var(--sh-accent, var(--sh-text-3));
  font-size: 10px;
}
.project-row-check {
  flex: none;
  color: var(--sh-accent, var(--sh-text-2));
}
</style>
