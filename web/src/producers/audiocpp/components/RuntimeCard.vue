<!-- 运行包：只有「由 VoiceHub 启动」这一路才需要；接入已有服务时整块不出现。 -->
<template>
  <PanelCard title="运行包" :desc="cardDesc">
    <template #head>
      <span class="sh-hint">清单 v{{ data.runtime.catalog_version }}{{ data.runtime.catalog_synced ? ' · 已同步' : '' }}</span>
      <ActionButton
        size="sm"
        variant="ghost"
        :loading="state.busy === 'runtime-refresh'"
        title="从上游同步运行包清单"
        aria-label="从上游同步运行包清单"
        @click="refreshRuntime"
      >同步</ActionButton>
      <span v-if="data.runtime.version" class="sh-chip" :class="data.runtime.outdated ? 'is-warn' : ''">
        {{ data.runtime.version }}
      </span>
      <span v-else-if="data.runtime.source === 'custom'" class="sh-chip">你的安装</span>
    </template>

    <template v-if="data.runtime.source !== 'custom'">
      <div class="acp-row">
        <label class="acp-label" for="acp-runtime">平台包</label>
        <select
          id="acp-runtime"
          class="sh-field acp-grow"
          :value="data.runtime.runtime_id"
          :disabled="state.busy === 'runtime' || !data.runtime.packages.length"
          aria-label="运行包"
          @change="onRuntimeChange"
        >
          <option v-for="row in runtimeOptions" :key="row.id" :value="row.id">
            {{ row.label }}{{ row.size ? ` · ${formatSize(row.size + (row.extras_size || 0))}` : '' }}{{ row.extras_size ? ' · 含 CUDA 运行时' : '' }}{{ row.experimental ? ' · 实验' : '' }}
          </option>
        </select>
        <ActionButton
          :variant="data.runtime.installed ? undefined : 'primary'"
          size="sm"
          :loading="state.busy === 'download'"
          :disabled="data.runtime.download.state === 'downloading'"
          @click="downloadRuntime"
        >{{ data.runtime.installed ? '重新下载' : '下载' }}</ActionButton>
        <template v-if="state.confirmUninstall">
          <ActionButton variant="danger" size="sm" :loading="state.busy === 'uninstall'" @click="uninstall">
            确认卸载
          </ActionButton>
          <ActionButton size="sm" variant="ghost" @click="state.confirmUninstall = false">取消</ActionButton>
        </template>
        <ActionButton
          v-else
          size="sm"
          variant="ghost"
          :disabled="!data.runtime.builtin_exe"
          @click="state.confirmUninstall = true"
        >卸载</ActionButton>
      </div>

      <div v-if="data.runtime.download.state === 'downloading'" class="acp-progress">
        <span :style="progressStyle(data.runtime.download.progress)" />
      </div>
      <p v-if="downloadNote" class="acp-note" :class="{ 'is-warn': data.runtime.download.state === 'error' }">
        {{ downloadNote }}
      </p>
    </template>

    <div class="acp-row">
      <label class="acp-label" for="acp-exe">可执行文件</label>
      <input
        id="acp-exe"
        class="sh-field acp-grow"
        :value="state.exeDraft"
        :placeholder="data.runtime.builtin_exe || '未安装内置运行包，可填自己的路径'"
        spellcheck="false"
        @input="onExeInput"
        @keydown.enter="applyExe"
      />
      <ActionButton size="sm" :loading="state.busy === 'exe'" @click="applyExe">应用</ActionButton>
      <ActionButton
        v-if="data.runtime.custom_exe"
        size="sm"
        variant="ghost"
        :loading="state.busy === 'exe'"
        @click="useBuiltin"
      >清除</ActionButton>
    </div>

    <div class="acp-row">
      <label class="acp-label" for="acp-root">模型目录</label>
      <input
        id="acp-root"
        class="sh-field acp-grow"
        :value="state.rootDraft"
        :placeholder="data.runtime.models_root"
        spellcheck="false"
        @input="onRootInput"
        @keydown.enter="applyRoot"
      />
      <ActionButton size="sm" :loading="state.busy === 'root'" @click="applyRoot">应用</ActionButton>
      <ActionButton
        v-if="data.runtime.models_root_custom"
        size="sm"
        variant="ghost"
        :loading="state.busy === 'root'"
        @click="resetRoot"
      >默认</ActionButton>
    </div>

    <p v-if="rootNote" class="acp-note is-warn">{{ rootNote }}</p>
    <p v-if="data.runtime.note" class="acp-note is-warn">{{ data.runtime.note }}</p>
    <p v-if="data.runtime.catalog_note" class="acp-note is-warn">{{ data.runtime.catalog_note }}</p>
    <p v-if="data.runtime.outdated" class="acp-note is-warn">
      当前 {{ data.runtime.version }} 低于最低要求 v{{ data.runtime.minimum_version }}，请重新下载运行包。
    </p>
  </PanelCard>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import ActionButton from '../../../components/ui/ActionButton.vue'
import PanelCard from '../../../components/ui/PanelCard.vue'
import { downloadRuntime, refreshRuntime, saveExe, saveModelsRoot, setRuntime, state, uninstall } from '../store'

const data = computed(() => state.data)

/** @param {Event} event */
function onRuntimeChange(event) {
  void setRuntime(/** @type {HTMLSelectElement} */ (event.target).value)
}
const cardDesc = computed(() => {
  const runtime = data.value?.runtime
  if (runtime?.source === 'custom') return ''
  return runtime?.installed
    ? '卸载只清运行包，已下载的模型保留。'
    : '未安装运行包。也可以直接填你已有的 audio.cpp 程序路径。'
})
const runtimeOptions = computed(() => {
  const rows = data.value?.runtime.packages || []
  return rows.length ? rows : [{ id: '', label: '当前平台无可用运行包', size: 0, extras_size: 0, experimental: false }]
})
const downloadNote = computed(() => {
  const job = data.value?.runtime.download
  if (!job || job.state === 'idle' || job.state === 'done') return ''
  return job.note || (job.state === 'error' ? '下载失败' : '')
})
// 自定义目录填错是最容易踩的一格：audio.cpp 的模型库在安装目录的 models\，不是 audiocpp\models\。
// 那个目录空的也能「应用成功」，只是扫不到已装模型，界面上必须说出来。
const rootNote = computed(() => {
  if (!data.value?.runtime?.models_root_custom) return ''
  const any = (data.value?.models || []).some(/** @param {any} row */ (row) => row.installed)
  if (any) return ''
  return '这个目录里没扫到已装模型。audio.cpp 的模型库通常在上层 models\\（与 audiocpp\\ 同级），不是 audiocpp\\models\\。'
})

/**
 * @param {number} bytes
 */
function formatSize(bytes) {
  const value = Number(bytes) || 0
  if (!value) return ''
  return value >= 1073741824 ? `${(value / 1073741824).toFixed(1)} GB` : `${(value / 1048576).toFixed(0)} MB`
}

/**
 * @param {any} value
 */
function progressStyle(value) {
  return { width: `${Math.max(2, Math.min(100, Number(value) || 0))}%` }
}

/**
 * @param {any} event
 */
function onExeInput(event) {
  state.exeDirty = true
  state.exeDraft = event.target.value
}

async function applyExe() {
  const ok = await saveExe(state.exeDraft)
  if (!ok) state.exeDraft = data.value?.runtime?.custom_exe || ''
}

/**
 * @param {any} event
 */
function onRootInput(event) {
  state.rootDirty = true
  state.rootDraft = event.target.value
}

async function applyRoot() {
  const ok = await saveModelsRoot(state.rootDraft)
  if (!ok) state.rootDraft = data.value?.runtime?.models_root_custom ? data.value.runtime.models_root : ''
}

async function resetRoot() {
  await saveModelsRoot('')
}

async function useBuiltin() {
  state.exeDraft = ''
  state.exeDirty = false
  await saveExe('')
}
</script>

<style scoped>
.acp-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sh-gap-2, 8px);
  min-height: 40px;
  padding: 4px 0;
  border-bottom: 1px solid var(--sh-border, rgba(128, 140, 160, 0.16));
}
.acp-label {
  flex: none;
  width: 72px;
  font-size: 12px;
  color: var(--sh-text-3);
}
.acp-grow {
  flex: 1 1 260px;
  min-width: 0;
}
.acp-progress {
  height: 4px;
  margin: 4px 0;
  border-radius: 2px;
  background: var(--sh-hover);
  overflow: hidden;
}
.acp-progress span {
  display: block;
  height: 100%;
  background: var(--sh-accent);
}
.acp-note {
  margin: 6px 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sh-text-3);
}
.acp-note.is-warn {
  color: var(--sh-warn);
}
</style>
