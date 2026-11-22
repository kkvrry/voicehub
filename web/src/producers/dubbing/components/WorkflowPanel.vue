<template>
  <aside class="dub-col dub-flow" aria-label="工作流">
    <div class="dub-col-head">
      <span class="t">工作流</span>
      <span class="n">{{ steps.length }} 步</span>
      <button
        ref="settingsEl"
        type="button"
        class="dub-mini"
        title="配音参数：切分 / 贴齐 / 导出"
        aria-label="配音参数"
        @click="settingsOpen = !settingsOpen"
      >
        <Settings2 :size="14" :stroke-width="1.8" />
      </button>
    </div>

    <div ref="scrollEl" class="flow-scroll" :class="{ 'is-more': more }" @scroll="checkMore">
      <div v-if="running" class="flow-job" role="status" aria-live="polite">
        <div class="flow-job-head">
          <span class="flow-job-kind">{{ jobLabel }}</span>
          <span class="flow-job-count">{{ state.detail?.job?.done }}/{{ state.detail?.job?.total }}</span>
          <button type="button" class="sh-btn is-sm is-ghost" @click="cancel">
            取消
          </button>
        </div>
        <div class="flow-bar">
          <i :style="{ width: progressWidth }"></i>
        </div>
        <p class="flow-job-msg">{{ state.detail.job.message || '进行中…' }}</p>
      </div>

      <ol class="flow">
        <li v-for="(step, index) in steps" :key="step.key" class="flow-node" :class="`is-${step.state}`">
          <div class="flow-top">
            <span class="flow-index">{{ pad(Number(index) + 1) }}</span>
            <span class="flow-name">{{ step.label }}</span>
            <span class="flow-state">{{ STATE_LABEL[step.state] || step.state }}</span>
          </div>
          <div class="flow-sum" :title="step.summary">{{ step.summary }}</div>
          <div v-if="actionOf(step.key)" class="flow-actions">
            <button
              class="sh-btn is-sm is-soft"
              type="button"
              :disabled="Boolean(state.busy) || actionOf(step.key)?.disabled"
              :title="actionOf(step.key)?.title"
              @click="actionOf(step.key)?.run()"
            >{{ actionOf(step.key)?.label }}</button>
            <button
              v-if="step.key === 'transcribe'"
              type="button"
              class="sh-btn is-sm is-ghost"
              :disabled="Boolean(state.busy) || running"
              title="导入现成的 SRT / VTT 当段落表"
              @click="subtitleEl?.click()"
            >
              导入字幕
            </button>
          </div>
          <div v-if="step.key === 'separate'" class="flow-actions flow-sep">
            <template v-if="separation.ready">
              <button
                class="sh-btn is-sm is-ghost is-danger"
                type="button"
                :disabled="busy"
                title="删除分轨文件，混音回到整条原声"
                @click="onRemoveSeparation"
              >移除分轨</button>
            </template>
            <template v-else>
              <select
                v-if="stemModels.length"
                v-model="state.separationModel"
                class="sh-field flow-sep-pick"
                :disabled="busy"
                aria-label="分轨模型"
              >
                <option v-for="model in stemModels" :key="model.id" :value="model.id">
                  {{ model.label }}{{ model.note ? ` · ${model.note}` : '' }}
                </option>
              </select>
              <button
                class="sh-btn is-sm is-soft"
                type="button"
                :disabled="busy || !stemModels.length"
                :title="
                  stemModels.length
                    ? '按原音轨分出人声与伴奏，后台任务'
                    : separation.note
                "
                @click="onSeparate"
              >{{ separation.stale ? '重新分轨' : '开始分轨' }}</button>
            </template>
          </div>
        </li>
      </ol>
      <input
        ref="subtitleEl"
        type="file"
        accept=".srt,.vtt,text/vtt,application/x-subrip"
        class="dub-file"
        @change="onSubtitleFile"
      />

      <!-- 成品：右列下半截以前是空的，成品只能靠记忆去项目目录里翻。 -->
      <div class="flow-out">
        <div class="flow-out-head">
          <span class="t">成品</span>
          <span class="n">{{ products.length ? `${products.length} 个文件` : '未生成' }}</span>
        </div>
        <ul v-if="products.length" class="flow-files">
          <li v-for="item in products" :key="item.kind + item.name">
            <a class="flow-file" :href="item.url" :download="item.name" :title="`下载 ${item.name}`">
              <component :is="item.icon" :size="14" :stroke-width="1.8" class="ficon" />
              <span class="fmain">
                <span class="fname">{{ item.name }}</span>
                <span class="ftime">
                  {{ item.time || '—' }}
                  <em v-if="item.latest" class="fnew">最新</em>
                </span>
              </span>
              <span class="fsize">{{ sizeLabel(item.size) }}</span>
            </a>
            <button
              type="button"
              class="sh-btn is-icon flow-folder"
              title="在资源管理器里打开所在文件夹"
              aria-label="打开成品文件夹"
              @click.stop="revealProduct(item)"
            >
              <FolderOpen :size="14" :stroke-width="1.8" />
            </button>
          </li>
        </ul>
        <p v-else class="flow-none">转录或导入字幕后，字幕会先列在这里；导出成片后视频也在这。</p>
      </div>
    </div>

    <div class="dub-col-foot">
      <div class="dub-mixer" aria-label="轨道音量">
        <label class="dub-field">
          <span>配音</span>
          <input
            type="range"
            min="-60"
            max="12"
            step="1"
            :value="dubGain"
            :disabled="!project"
            @change="onTrackGain('dub_gain', $event)"
          />
          <b>{{ trackGainLabel(dubGain) }}</b>
        </label>
        <label class="dub-field">
          <span>{{ separationReady ? '原声人声' : '原声' }}</span>
          <input
            type="range"
            min="-60"
            max="12"
            step="1"
            :value="originalGain"
            :disabled="!project"
            @change="onTrackGain('original_gain', $event)"
          />
          <b>{{ trackGainLabel(originalGain) }}</b>
        </label>
        <label v-if="separationReady" class="dub-field">
          <span>伴奏</span>
          <input
            type="range"
            min="-60"
            max="12"
            step="1"
            :value="accompanimentGain"
            :disabled="!project"
            @change="onTrackGain('accompaniment_gain', $event)"
          />
          <b>{{ trackGainLabel(accompanimentGain) }}</b>
        </label>
      </div>
      <p class="dub-hint">{{ mixerHint }}</p>
    </div>

    <SettingsPopover :open="settingsOpen" :anchor="settingsEl" @close="settingsOpen = false" />
  </aside>
</template>

<script setup>
// @ts-check
// 右列：步骤状态、唯一主动作与成品。状态由后端算（detail.steps），界面不自己推。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FileAudio, FileText, FileVideo, FolderOpen, Settings2 } from 'lucide-vue-next'
import SettingsPopover from './SettingsPopover.vue'
import {
  assemble,
  importSubtitles,
  project,
  refresh,
  removeSeparation,
  saveTrackGain,
  separate,
  state,
  steps,
  synthAll,
  transcribe,
} from '../store'
import * as api from '../api'

const STATE_LABEL = /** @type {Record<string, string>} */ ({ done: '完成', run: '运行中', wait: '等待', off: '未启用' })

const VIDEO_SUFFIXES = new Set(['mp4', 'mov', 'mkv', 'webm', 'avi'])

/** 产物列表：成片在前、按导出时间新的在上；字幕在后。体积与时间都来自后端扫描。 */
const products = computed(() => {
  const id = state.currentId
  const paths = state.detail?.paths
  if (!id || !paths) return []
  const size = (/** @type {string} */ name) => Number(paths.sizes?.[name] || 0)
  const meta = new Map(
    /** @type {any[]} */ (paths.export_meta || []).map(
      (/** @type {any} */ item) => [item.name, item]
    )
  )
  const exports = /** @type {string[]} */ (paths.exports || []).map((name, index) => {
    const info = meta.get(name)
    return {
      kind: 'export',
      name,
      size: info?.size ?? size(name),
      time: shortTime(info?.at || ''),
      // 同名或长名字被截断时，靠这枚标记认当前这份成片。
      latest: index === 0,
      icon: VIDEO_SUFFIXES.has(name.split('.').pop()?.toLowerCase() || '') ? FileVideo : FileAudio,
      url: api.mediaUrl(id, 'export', name),
    }
  })
  const subs = /** @type {string[]} */ (paths.subtitles || []).map((name) => ({
    kind: 'subtitle',
    name,
    size: size(name),
    time: '',
    latest: false,
    icon: FileText,
    url: api.mediaUrl(id, 'subtitle', name),
  }))
  return [...exports, ...subs]
})

/** @param {string} value */
function shortTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (/** @type {number} */ number) => String(number).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** @param {any} item */
async function revealProduct(item) {
  if (!state.currentId) return
  try {
    await api.reveal(state.currentId, item.name)
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error)
  }
}

/** @param {number} bytes */
function sizeLabel(bytes) {
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

// 矮窗口下步骤列表放不下：底边渐隐表示"下面还有"，别让人以为导出那一步不见了。
const scrollEl = ref(/** @type {HTMLElement | null} */ (null))
const more = ref(false)
const settingsEl = ref(/** @type {any} */ (null))
const settingsOpen = ref(false)
const subtitleEl = ref(/** @type {HTMLInputElement | null} */ (null))
let observer = /** @type {ResizeObserver | null} */ (null)

function checkMore() {
  const el = scrollEl.value
  if (!el) return
  more.value = el.scrollHeight - el.scrollTop - el.clientHeight > 2
}

onMounted(() => {
  checkMore()
  observer = new ResizeObserver(checkMore)
  if (scrollEl.value) observer.observe(scrollEl.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch(
  () => [steps.value.length, products.value.length, state.detail?.job?.state],
  () => nextTick(checkMore)
)

const running = computed(() => state.detail?.job?.state === 'running')
const busy = computed(() => Boolean(state.busy) || running.value)
const jobLabel = computed(() => {
  const kind = state.detail?.job?.kind
  if (kind === 'transcribe') return '转录中'
  if (kind === 'separate') return '分轨中'
  if (kind === 'synth') return '配音中'
  if (kind === 'assemble') return '贴齐中'
  if (kind === 'export') return '导出中'
  return '处理中'
})

/** 分轨状态由后端下发：界面不按引擎名或文件名判断。 */
const separation = computed(
  () =>
    state.detail?.separation || {
      ready: false,
      stale: false,
      models: [],
      note: '',
      label: '',
    }
)
const separationReady = computed(() => Boolean(separation.value.ready))
/** @type {import('vue').ComputedRef<{ id: string, label: string, note?: string }[]>} */
const stemModels = computed(() => separation.value.models || [])

// 换项目或换可用清单时把选择落到现有模型上；已分轨的模型优先。
watch(
  () => stemModels.value.map((model) => model.id).join('|'),
  () => {
    const list = stemModels.value
    if (list.some((model) => model.id === state.separationModel)) return
    const current = separation.value.model
    state.separationModel = list.some((model) => model.id === current)
      ? current
      : list[0]?.id || ''
  },
  { immediate: true }
)

const progressWidth = computed(() => {
  const current = state.detail?.job
  if (!current?.total) return '8%'
  return `${Math.min(100, Math.round((current.done / current.total) * 100))}%`
})

const dubGain = computed(() => Number(project.value?.options?.dub_gain ?? 0))
const originalGain = computed(() => Number(project.value?.options?.original_gain ?? 0))
const accompanimentGain = computed(() =>
  Number(project.value?.options?.accompaniment_gain ?? 0)
)

/** @param {number} value */
function trackGainLabel(value) {
  return value <= -60 ? '静音' : `${value > 0 ? '+' : ''}${value} dB`
}

/** @param {string} key @param {Event} event */
function onTrackGain(key, event) {
  const target = /** @type {HTMLInputElement | null} */ (event.target)
  void saveTrackGain(key, Number(target?.value || 0))
}

const mixerHint = computed(() =>
  separationReady.value
    ? '配音段只压原声人声，伴奏继续；−60 dB 为静音。'
    : '只压保留原声的段落；贴齐与导出会按当前音量重算混音。'
)

async function onSeparate() {
  if (busy.value || !state.separationModel) return
  await separate(state.separationModel)
}

async function onRemoveSeparation() {
  if (busy.value) return
  const ok = window.confirm(
    '移除分轨？混音会回到整条原声，配音段的音乐也会一起静音；两份 WAV 会被删除。'
  )
  if (!ok) return
  await removeSeparation()
}

async function cancel() {
  try {
    await api.cancelJob(state.currentId)
    await refresh({ silent: true })
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error)
  }
}

/** @param {Event} event */
async function onSubtitleFile(event) {
  const input = /** @type {HTMLInputElement} */ (event.target)
  const file = input?.files?.[0]
  input.value = ''
  if (file) await importSubtitles(file)
}

/** 每一步能不能点、点了做什么；没有动作的步骤返回 null。 */
/** @param {string} key */
function actionOf(key) {
  const busy = Boolean(state.busy) || running.value
  if (key === 'extract') {
    const hasAudio = Boolean(state.detail?.paths?.original)
    const hasSource = state.detail?.source_ok !== false
    return {
      label: hasAudio ? '重新抽取' : '抽音轨',
      disabled: busy || !hasSource,
      title: hasSource
        ? hasAudio
          ? '按当前选择的音轨重新解复用'
          : '从源文件抽出音轨'
        : '源文件不在了：先在项目头重新指定路径',
      run: () => void extractAudio(),
    }
  }
  if (key === 'transcribe') {
    // 取消只留在任务条上：同一步里再放一枚「取消」，两处按钮做同一件事。
    const ready = state.detail?.asr?.ready
    return {
      // 已经有段落时这一下会整批替换：说清是"重新"，不让人以为是补几段。
      label: (state.detail?.stats?.total || 0) > 0 ? '重新转录' : '转录',
      disabled: busy || !project.value || !ready,
      title: ready ? '按静音切段逐段识别' : state.detail?.asr?.note || '识别服务不可用',
      run: () => transcribe(),
    }
  }
  if (key === 'synth') {
    const stats = state.detail?.stats || {
      dubbed: 0,
      ready: 0,
      stale: 0,
      failed: 0,
      pending: 0,
      unvoiced: 0,
    }
    const todo = Number(stats.stale) + Number(stats.failed) + Number(stats.pending)
    const allReady = stats.dubbed > 0 && stats.ready >= stats.dubbed
    return {
      label: allReady ? '全部已合成' : stats.ready > 0 ? '合成剩余' : '开始配音',
      disabled: busy || stats.dubbed - stats.unvoiced <= 0 || (allReady && !todo),
      title:
        stats.unvoiced
          ? `${stats.unvoiced} 段缺音色：先在角色条里选音色`
          : allReady
            ? '所有配音段都是最新的；改文本、角色或音色后只需重配改动的那几段'
            : '按角色逐段合成，未改动的段直接复用',
      run: () => synthAll(),
    }
  }
  if (key === 'align') {
    const ready = state.detail?.stats?.ready || 0
    return {
      label: '贴齐',
      disabled: Boolean(busy) || ready <= 0,
      title: ready ? '按时间码补静音、加速贴齐并混音' : '先合成至少一段配音',
      run: () => assemble(),
    }
  }
  return null
}

/** 重新抽音轨：入口只在这里，成功后刷新详情。 */
async function extractAudio() {
  if (!state.currentId) return
  state.busy = 'extract'
  try {
    await api.extract(state.currentId)
    await refresh()
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error)
  } finally {
    state.busy = ''
  }
}

/** @param {number} value */
function pad(value) {
  return String(value).padStart(2, '0')
}
</script>

<style scoped>
.dub-flow {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* 步骤与成品共用一条滚动：分成两个滚动区时，矮窗口下最后一步会被关在
   自己那块 400px 的窗口里，用户看不到"后面还有导出"。 */
.flow-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--sh-line) transparent;
}

/* 还有内容在下面时底边渐隐一截：不然半截按钮卡在边上，像画坏了。 */
.flow-scroll.is-more {
  mask-image: linear-gradient(to bottom, var(--sh-text) calc(100% - 22px), transparent 100%);
}

/* 单列布局里工作流在表格下面，没有固定高度。沿用 flex:1 + overflow 会让
   步骤列表收缩成 0，只剩页头和音量滑块——看起来像工作流没加载出来。 */
@media (max-width: 980px) {
  .dub-flow { height: auto; }

  .flow-scroll {
    flex: none;
    overflow-y: visible;
  }

  .flow-scroll.is-more { mask-image: none; }
}


.flow-job {
  margin: 2px 10px 8px;
  padding: 8px 10px;
  border: 1px solid var(--sh-accent-ring);
  border-radius: var(--sh-r-xs);
  background: var(--sh-accent-soft);
}

.flow-job-head {
  display: flex;
  align-items: center;
  gap: 7px;
}

.flow-job-kind {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--sh-text);
}

.flow-job-count {
  font-size: 10.5px;
  color: var(--sh-accent);
  font-variant-numeric: tabular-nums;
}

.flow-job-head .sh-btn { margin-left: auto; }

.flow-job-msg {
  margin: 6px 0 0;
  font-size: 10.5px;
  line-height: 1.5;
  color: var(--sh-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.flow {
  margin: 0;
  padding: 0 10px 6px;
  list-style: none;
}

.flow-node {
  position: relative;
  padding: 6px 10px 7px 12px;
  border-left: 1px solid var(--sh-line);
  margin-left: 6px;
}

.flow-node::before {
  content: '';
  position: absolute;
  left: -4px;
  top: 13px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sh-line);
  box-shadow: 0 0 0 3px var(--sh-panel);
}

.flow-node.is-done::before { background: var(--sh-ok); }
.flow-node.is-run::before { background: var(--sh-accent); }
.flow-node.is-off { opacity: 0.55; }

.flow-top {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.flow-index {
  font-family: var(--sh-mono);
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.flow-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}

.flow-state {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.is-run .flow-state { color: var(--sh-accent); }
.is-done .flow-state { color: var(--sh-ok); }

.flow-sum {
  margin-top: 1px;
  font-size: 11px;
  color: var(--sh-text-3);
  line-height: 1.45;
  word-break: break-word;
}

.flow-bar {
  margin-top: 6px;
  height: 3px;
  border-radius: 2px;
  background: var(--sh-track);
  overflow: hidden;
}

.flow-bar i {
  display: block;
  height: 100%;
  background: var(--sh-accent);
  transition: width 240ms var(--sh-ease);
}

.flow-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 5px;
}

/* 分轨：模型下拉与开始键同一行；没有可用模型时只留原因。 */
.flow-sep-pick {
  max-width: 190px;
}

/* 成品：撑住右列下半截，也给出"导出去哪了"的答案（点名字即下载）。 */
.flow-out {
  flex: none;
  border-top: 1px solid var(--sh-line-soft);
}

.flow-out-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px 4px;
}

.flow-out-head .t {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
}

.flow-out-head .n {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.flow-files {
  margin: 0;
  padding: 0 8px 8px;
  list-style: none;
}

.flow-files li {
  display: flex;
  align-items: center;
  gap: 2px;
}

.flow-file {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 6px;
  border-radius: var(--sh-r-xs);
  color: var(--sh-text-2);
  text-decoration: none;
}

.flow-file:hover { background: var(--sh-hover); color: var(--sh-text); }

.flow-folder {
  flex: none;
  width: 24px;
  height: 24px;
  color: var(--sh-text-4);
}

.flow-folder:hover {
  color: var(--sh-accent);
  background: var(--sh-hover);
}

.flow-file .ficon { flex: none; color: var(--sh-text-4); }
.flow-file:hover .ficon { color: var(--sh-accent); }

.fmain {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.fname {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
}

.ftime {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.fnew {
  padding: 0 4px;
  border-radius: 4px;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
  font-size: 9.5px;
  font-style: normal;
  line-height: 14px;
}

.fsize {
  flex: none;
  font-size: 10px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.flow-none {
  margin: 0;
  padding: 2px 14px 10px;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.dub-col-foot {
  flex: none;
  padding: 10px 14px 12px;
  border-top: 1px solid var(--sh-line-soft);
}

.dub-mixer {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* 一条轨一行：名字、滑块、电平。三条轨的混音器不能把右列撑到只剩滑轨。 */
.dub-field {
  display: grid;
  grid-template-columns: 66px minmax(0, 1fr) 46px;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--sh-text-3);
}

.dub-field b {
  color: var(--sh-text);
  font-weight: 600;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.dub-field input {
  width: 100%;
  margin: 0;
  accent-color: var(--sh-accent);
}

.dub-hint {
  margin: 8px 0 0;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}

.dub-file { display: none; }
</style>
