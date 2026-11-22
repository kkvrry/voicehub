<template>
  <aside class="dub-col dub-flow" aria-label="工作流">
    <div class="dub-col-head">
      <span class="t">工作流</span>
      <span class="n">{{ trackedSteps.length ? `${doneCount} / ${trackedSteps.length} 完成` : '' }}</span>
    </div>
    <div class="flow-meter" aria-hidden="true"><i :style="{ width: meterWidth }"></i></div>

    <div ref="scrollEl" class="flow-scroll" :class="{ 'is-more': more }" @scroll="checkMore">
      <ol class="flow">
        <li
          v-for="(step, index) in steps"
          :key="step.key"
          class="flow-node"
          :class="[`is-${step.state}`, { 'is-active': step.key === currentKey }]"
        >
          <span class="flow-rail" aria-hidden="true">
            <span class="flow-dot">
              <Check v-if="step.state === 'done'" :size="12" :stroke-width="2.2" />
              <Loader2
                v-else-if="step.state === 'run'"
                :size="12"
                :stroke-width="1.8"
                class="sh-spin"
              />
              <template v-else>{{ pad(Number(index) + 1) }}</template>
            </span>
          </span>

          <div class="flow-main">
            <div class="flow-top">
              <span class="flow-name" :title="step.note || step.label">{{ step.label }}</span>
              <span class="flow-state">{{ STATE_LABEL[step.state] || step.state }}</span>
            </div>
            <p class="flow-sum" :title="step.summary">{{ step.summary }}</p>
            <p
              v-if="stepConfig(step.key)"
              class="flow-config"
              :title="stepConfig(step.key)"
            >{{ stepConfig(step.key) }}</p>

            <!-- 模型就在步骤里选：分轨模型来自运行中的实例，识别引擎是全局那台。 -->
            <div v-if="step.key === 'separate'" class="flow-controls">
              <label v-if="stemModels.length" class="wf-field">
                <span class="wf-field-label">分轨模型</span>
                <select
                  v-model="state.separationModel"
                  class="sh-field"
                  :disabled="busy"
                >
                  <option v-for="model in stemModels" :key="model.id" :value="model.id">
                    {{ model.label }}{{ model.note ? ` · ${model.note}` : '' }}
                  </option>
                </select>
              </label>
            </div>

            <div v-if="step.key === 'transcribe'" class="flow-controls">
              <label
                class="wf-field"
                title="全局设置：换台后所有项目都使用这个识别引擎"
              >
                <span class="wf-field-label">识别引擎（全局）</span>
                <select
                  class="sh-field"
                  :value="asrValue"
                  :disabled="busy || !asrEngines.length"
                  @change="onAsrEngine"
                >
                  <option v-if="asrEngines.length" value="">
                    自动（{{ asrCurrentLabel }}）
                  </option>
                  <option v-for="row in asrEngines" :key="row.key" :value="row.key">
                    {{ row.label }}{{ row.ready ? '' : ' · 未就绪' }}
                  </option>
                  <option v-if="!asrEngines.length" value="">没有识别引擎</option>
                </select>
              </label>
            </div>

            <!-- 进度挂在正在跑的那一步上：不再另起一张任务卡盖在步骤列表上方。 -->
            <div v-if="step.key === jobKey && running" class="flow-job" role="status" aria-live="polite">
              <div class="flow-job-head">
                <span class="flow-job-count">{{ state.detail?.job?.done }}/{{ state.detail?.job?.total }}</span>
                <span class="flow-job-msg" :title="state.detail?.job?.message || '进行中…'">
                  {{ state.detail?.job?.message || '进行中…' }}
                </span>
                <button type="button" class="sh-btn is-sm is-ghost" @click="cancel">取消</button>
              </div>
              <div class="flow-bar"><i :style="{ width: progressWidth }"></i></div>
            </div>

            <div v-if="actionOf(step.key) || step.key === 'transcribe' || hasParams(step.key)" class="flow-actions">
              <button
                v-if="actionOf(step.key)"
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
              >导入字幕</button>
              <button
                v-if="step.key === 'separate' && separation.ready"
                type="button"
                class="sh-btn is-sm is-ghost is-danger"
                :disabled="busy"
                title="删除分轨文件，混音回到整条原声"
                @click="onRemoveSeparation"
              >移除分轨</button>
              <button
                v-if="hasParams(step.key)"
                type="button"
                class="flow-params-toggle"
                :class="{ 'is-open': openParams === step.key }"
                :aria-expanded="openParams === step.key"
                :title="`${step.label}参数`"
                @click="toggleParams(step.key)"
              >
                <SlidersHorizontal :size="12" :stroke-width="1.8" />
                参数
                <ChevronDown :size="12" :stroke-width="1.8" />
              </button>
            </div>

            <StepParams
              v-if="hasParams(step.key)"
              v-show="openParams === step.key"
              :step-key="step.key"
              :step-label="step.label"
              @saved="onParamsSaved"
            />
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
      <div class="dub-mixer-head">
        <span class="t">混音</span>
        <span class="n">−60 dB 为静音</span>
      </div>
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
  </aside>
</template>

<script setup>
// @ts-check
// 右列：步骤状态、每一步的模型与参数、唯一主动作与成品。
// 状态由后端算（detail.steps），参数归属由 settings schema 的 `step` 声明。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Check,
  ChevronDown,
  FileAudio,
  FileText,
  FileVideo,
  FolderOpen,
  Loader2,
  SlidersHorizontal,
} from 'lucide-vue-next'
import StepParams from './StepParams.vue'
import {
  assemble,
  importSubtitles,
  project,
  refresh,
  removeSeparation,
  roles,
  saveTrackGain,
  separate,
  state,
  steps,
  synthAll,
  transcribe,
} from '../store'
import * as api from '../api'
import { actions as hostActions, store as hostStore } from '../../../store'

const STATE_LABEL = /** @type {Record<string, string>} */ ({
  done: '完成',
  run: '运行中',
  wait: '等待',
  off: '可选',
})
const JOB_STEPS = /** @type {Record<string, string>} */ ({
  transcribe: 'transcribe',
  separate: 'separate',
  synth: 'synth',
  assemble: 'align',
  export: 'export',
})

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
const subtitleEl = ref(/** @type {HTMLInputElement | null} */ (null))
const openParams = ref('')
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
  void ensureHostLists()
})

onBeforeUnmount(() => observer?.disconnect())

watch(
  () => [
    steps.value.length,
    products.value.length,
    state.detail?.job?.state,
    openParams.value,
  ],
  () => nextTick(checkMore)
)

watch(() => state.currentId, () => {
  openParams.value = ''
})

async function ensureHostLists() {
  if (!(hostStore.producers || []).some((item) => item.key === 'dubbing')) {
    await hostActions.loadProducers().catch(() => {})
  }
  if (!hostStore.asr?.engines?.length) {
    await hostActions.loadAsr().catch(() => {})
  }
}

const running = computed(() => state.detail?.job?.state === 'running')
const busy = computed(() => Boolean(state.busy) || running.value)
const jobKey = computed(() => JOB_STEPS[state.detail?.job?.kind] || '')

/** 分轨是可选项，不占必做进度：否则全部必做步骤做完了，进度仍永远差一截。 */
const trackedSteps = computed(() =>
  steps.value.filter((/** @type {any} */ step) => step.key !== 'separate')
)
const doneCount = computed(
  () => trackedSteps.value.filter((/** @type {any} */ step) => step.state === 'done').length
)
const meterWidth = computed(() => {
  const total = trackedSteps.value.length
  if (!total) return '0%'
  return `${Math.round((doneCount.value / total) * 100)}%`
})
/** 正在跑的那步优先；否则指向第一个未完成、且不是「可选」的步骤。 */
const currentKey = computed(() => {
  if (jobKey.value) return jobKey.value
  const next = steps.value.find(
    (/** @type {any} */ step) => step.state !== 'done' && step.state !== 'off'
  )
  return next?.key || ''
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

/* ---- 识别引擎：全局那一台，换完重新取项目详情（可用性与提示都变了） ---- */
const asrEngines = computed(() => hostStore.asr?.engines || [])
const asrValue = computed(() => (hostStore.asr?.auto ? '' : hostStore.asr?.current || ''))
const asrCurrentLabel = computed(() => {
  const key = hostStore.asr?.current
  return asrEngines.value.find((row) => row.key === key)?.label || '按顺序选择'
})

/** @param {Event} event */
async function onAsrEngine(event) {
  const target = /** @type {HTMLSelectElement | null} */ (event.target)
  const key = String(target?.value || '')
  try {
    await hostActions.setAsrCurrent(key)
    await refresh({ silent: true })
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error)
  }
}

/* ---- 参数：按 schema 的 `step` 归到对应步骤，只展开一步 ---- */
const plugin = computed(
  () => (hostStore.producers || []).find((item) => item.key === 'dubbing') || null
)
const schemaProps = computed(
  () => /** @type {Record<string, any>} */ (plugin.value?.settings_schema?.properties || {})
)
const stepParamKeys = computed(() => {
  /** @type {Record<string, string[]>} */
  const out = {}
  for (const [key, spec] of Object.entries(schemaProps.value)) {
    const step = String(/** @type {any} */ (spec)?.step || '')
    if (!step) continue
    if (!out[step]) out[step] = []
    out[step].push(key)
  }
  return out
})

/** @param {string} key */
function hasParams(key) {
  return Boolean(stepParamKeys.value[key]?.length)
}

/** @param {string} key */
function toggleParams(key) {
  openParams.value = openParams.value === key ? '' : key
  void nextTick(checkMore)
}

async function onParamsSaved() {
  await refresh({ silent: true })
}

/** @param {string} key @param {any} value */
function choiceLabel(key, value) {
  const choices = /** @type {any[]} */ (schemaProps.value[key]?.choices || [])
  const hit = choices.find((pair) => String(pair[0]) === String(value ?? ''))
  return hit?.[1] || String(value ?? '')
}

/** 一步当前生效的参数摘要：标签与候选说法都从后端 schema 取，不在这里另写一份。 */
/** @param {string} key */
function configLine(key) {
  const saved = state.detail?.settings
  if (!saved) return ''
  const parts = []
  for (const field of stepParamKeys.value[key] || []) {
    const spec = schemaProps.value[field] || {}
    const value = saved[field]
    if (value === undefined || value === null) continue
    let text = String(value)
    if (spec.widget === 'select') text = choiceLabel(field, value)
    else if (spec.widget === 'switch') text = value ? '开' : '关'
    parts.push(`${spec.label || field} ${text}`)
  }
  return parts.join(' · ')
}

const engineLabels = computed(() => {
  const map = new Map()
  for (const item of [...(hostStore.engines || []), ...(hostStore.engineCatalog || [])]) {
    if (item?.key) map.set(item.key, item.label || item.key)
  }
  return map
})

/** 配音这一步用哪些引擎：让"模型是什么"在不展开角色条时也看得见。 */
const castLine = computed(() => {
  const tts = roles.value.filter((/** @type {any} */ role) => role.mode === 'tts')
  if (!tts.length) return '全部角色保留原声'
  const used = [
    ...new Set(
      tts
        .filter((/** @type {any} */ role) => role.voice_id && role.engine)
        .map((/** @type {any} */ role) => engineLabels.value.get(role.engine) || role.engine)
    ),
  ]
  return used.length ? `引擎 ${used.join('、')}` : ''
})

/** @param {string} key */
function stepConfig(key) {
  if (key === 'synth') {
    return [castLine.value, configLine(key)].filter(Boolean).join(' · ')
  }
  return configLine(key)
}

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
    ? '配音段只压原声人声，伴奏继续。'
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
  const busyNow = Boolean(state.busy) || running.value
  if (key === 'extract') {
    const hasAudio = Boolean(state.detail?.paths?.original)
    const hasSource = state.detail?.source_ok !== false
    return {
      label:
        state.busy === 'extract' ? '抽取中…' : hasAudio ? '重新抽取' : '抽音轨',
      disabled: busyNow || !hasSource,
      title: hasSource
        ? hasAudio
          ? '按当前选择的音轨重新解复用'
          : '从源文件抽出音轨'
        : '源文件不在了：先在项目头重新指定路径',
      run: () => void extractAudio(),
    }
  }
  if (key === 'separate') {
    const hasModels = stemModels.value.length > 0
    // 没有模型时按钮点了也只报同一句话：把位置留给摘要里的原因。
    if (!hasModels && !separation.value.ready) return null
    return {
      label: separation.value.ready ? '重新分轨' : '开始分轨',
      disabled: busyNow || !hasModels,
      title: hasModels
        ? separation.value.ready
          ? '按当前选择的模型重新分出人声与伴奏'
          : '按原音轨分出人声与伴奏，后台任务'
        : separation.value.note || '没有正在运行的分离模型',
      run: () => void onSeparate(),
    }
  }
  if (key === 'transcribe') {
    // 取消只留在任务条上：同一步里再放一枚「取消」，两处按钮做同一件事。
    const ready = state.detail?.asr?.ready
    return {
      // 已经有段落时这一下会整批替换：说清是"重新"，不让人以为是补几段。
      label: (state.detail?.stats?.total || 0) > 0 ? '重新转录' : '转录',
      disabled: busyNow || !project.value || !ready,
      title: ready ? '按静音切段逐段识别' : state.detail?.asr?.note || '识别服务不可用',
      run: () => transcribe(),
    }
  }
  if (key === 'synth') {
    const stats = state.detail?.stats || {
      total: 0,
      dubbed: 0,
      ready: 0,
      stale: 0,
      failed: 0,
      pending: 0,
      unvoiced: 0,
      keep: 0,
    }
    const todo = Number(stats.stale) + Number(stats.failed) + Number(stats.pending)
    const allReady = stats.dubbed > 0 && stats.ready >= stats.dubbed
    return {
      label: !stats.dubbed
        ? '无需配音'
        : allReady
          ? '全部已合成'
          : stats.ready > 0
            ? '合成剩余'
            : '开始配音',
      disabled: busyNow || stats.dubbed <= 0 || (allReady && !todo),
      title: !stats.dubbed
        ? '所有段落都保留原声；到上方角色条把要配音的角色改为「配音」'
        : stats.unvoiced
          ? `${stats.unvoiced} 段缺音色：先在角色条里给角色指定音色`
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
      disabled: busyNow || ready <= 0,
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

@media (max-width: 980px) {
  .dub-flow { height: auto; }

  .flow-scroll {
    flex: none;
    overflow-y: visible;
  }

  .flow-scroll.is-more { mask-image: none; }
}

.flow-meter {
  flex: none;
  height: 2px;
  margin: 0 12px 4px;
  border-radius: 2px;
  background: var(--sh-track);
  overflow: hidden;
}

.flow-meter i {
  display: block;
  height: 100%;
  background: var(--sh-accent);
  transition: width 240ms var(--sh-ease);
}

.flow {
  margin: 0;
  padding: 2px 12px 8px;
  list-style: none;
}

.flow-node {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  gap: 9px;
  padding: 8px 4px;
  border-top: 1px solid var(--sh-line-soft);
}

.flow-node:first-child { border-top: 0; }

/* 序号列同时是时间线：细线只画在圆点下面，最后一步自然收尾。 */
.flow-rail {
  position: relative;
  display: flex;
  justify-content: center;
}

.flow-rail::before {
  content: '';
  position: absolute;
  top: 24px;
  bottom: -12px;
  width: 1px;
  background: var(--sh-line-soft);
}

.flow-node:last-child .flow-rail::before { display: none; }

.flow-dot {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border: 1px solid var(--sh-line);
  border-radius: 50%;
  background: var(--sh-panel);
  color: var(--sh-text-4);
  font-size: 9.5px;
  font-variant-numeric: tabular-nums;
}

.flow-node.is-done .flow-dot {
  border-color: transparent;
  background: var(--sh-ok-soft);
  color: var(--sh-ok);
}

.flow-node.is-active .flow-dot {
  border-color: var(--sh-accent-ring);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.flow-node.is-run .flow-dot {
  border-color: var(--sh-accent-ring);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.flow-node.is-off .flow-dot {
  border-style: dashed;
  color: var(--sh-text-3);
}

.flow-main { min-width: 0; }

.flow-top {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.flow-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}

.flow-node.is-done .flow-name { color: var(--sh-text-2); }
.flow-node.is-off .flow-name { color: var(--sh-text-2); }

.flow-state {
  flex: none;
  margin-left: auto;
  font-size: 10px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.flow-node.is-done .flow-state { color: var(--sh-ok); }
.flow-node.is-active .flow-state,
.flow-node.is-run .flow-state {
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.flow-sum {
  margin: 2px 0 0;
  font-size: 10.8px;
  line-height: 1.45;
  color: var(--sh-text-3);
  word-break: break-word;
}

.flow-config {
  margin: 3px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.2px;
  line-height: 1.4;
  color: var(--sh-text-4);
}

.flow-controls {
  display: grid;
  gap: 6px;
  margin-top: 6px;
}

.wf-field {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.wf-field-label {
  font-size: 10px;
  color: var(--sh-text-4);
}

.wf-field .sh-field {
  height: 30px;
  font-size: 12px;
}

.flow-job {
  margin-top: 8px;
  padding: 7px 9px 8px;
  border: 1px solid var(--sh-accent-ring);
  border-radius: var(--sh-r-sm);
  background: var(--sh-accent-soft);
}

.flow-job-head {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.flow-job-count {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--sh-accent);
  font-variant-numeric: tabular-nums;
}

.flow-job-msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.flow-job .sh-btn {
  flex: none;
  height: 24px;
  padding: 0 8px;
  font-size: 10.5px;
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
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.flow-actions .sh-btn {
  height: 27px;
  padding: 0 10px;
  font-size: 11.5px;
}

/* 参数入口收在动作行右端：一眼能看出"这一步还能调什么"。 */
.flow-params-toggle {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 24px;
  margin-left: auto;
  padding: 0 3px;
  border: 0;
  background: none;
  color: var(--sh-text-4);
  font-size: 10.5px;
}

.flow-params-toggle:hover {
  color: var(--sh-accent);
}

.flow-params-toggle svg:last-child {
  transition: transform var(--sh-t-fast) var(--sh-ease);
}

.flow-params-toggle.is-open svg:last-child {
  transform: rotate(180deg);
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
  padding: 0 12px 12px;
  border-top: 1px solid var(--sh-line-soft);
}

.dub-mixer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 2px 6px;
}

.dub-mixer-head .t {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
}

.dub-mixer-head .n {
  margin-left: auto;
  font-size: 10px;
  color: var(--sh-text-4);
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
  margin: 7px 0 0;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}

.dub-file { display: none; }
</style>
