// @ts-check
// 音色处理工作区的状态与动作。
//
// 处理目标不在这里存副本：音色目标来自左栏的选中（`store.pick`，宿主那份），
// 这里只负责按它取回样例、记每条样例的勾选与裁剪、按勾选的步骤跑任务、留下 A/B 产物。
// 左栏点哪条、这里就处理哪条；点卡片列全部，指定一条只列那一条。
// 另一条目标是电脑文件：上传后存在 `state.fileTarget`，项目只产出可下载的结果。
import { reactive } from 'vue'
import { actions, say, store } from '../../store'
import { invalidateWaveform } from '../../composables/waveform'
import * as api from './api'

/** 裁剪后至少保留的时长（秒），与服务端的 MIN_TRIM_SECONDS 一致。 */
const MIN_TRIM = 0.1

export const state = reactive({
  /** @type {any} */ data: null,
  loaded: false,
  error: '',
  busy: '',
  /** 选中音色的详情（带样例与时长）：跟着 `store.pick.voiceId` 取 */
  /** @type {any} */ target: null,
  targetLoading: false,
  /** 电脑里选中的音频：`{ token, name, duration }`；有它时优先于左栏音色。 */
  /** @type {any} */ fileTarget: null,
  /** 左栏这一栏现在是"整条音色"还是"单条样例" */
  pickMode: '',
  /** @type {Record<string, { enabled: boolean, model: string }>} */
  picks: {},
  /** 收尾步骤（统一声道与峰值）可跳过；默认取后端声明。 */
  normalize: true,
  /** 每条样例是否参与这一轮：file → boolean */
  /** @type {Record<string, boolean>} */
  selected: {},
  /** 每条样例要保留的区间：file → { segments: [{start, end}, ...] } */
  /** @type {Record<string, { segments: { start: number, end: number }[] }>} */
  trims: {},
  /** 当前选区：file → {start, end}；没有选区时不存在这一项 */
  /** @type {Record<string, { start: number, end: number }>} */
  selections: {},
  /** 裁剪撤销 / 前进：file → { past, future } */
  /** @type {Record<string, { past: { start: number, end: number }[][], future: { start: number, end: number }[][] }>} */
  history: {},
  /** @type {any} */ job: null,
  /** 正在试听 A/B 的 ``index:side`` */
  playing: '',
  /** @type {any} */ applied: null,
  /** 正在试听哪条样例 */
  preview: '',
  /** 试听的是整段结果还是选区 */
  previewMode: '',
  previewing: false,
  /** 替换后加一：波形 URL 不变，用它通知已挂着的 Waveform 重新解码。 */
  waveRevision: 0,
})

let timer = 0
let loading = false
let coreInited = false
/** @type {HTMLAudioElement|null} */
let audio = null
/** @type {HTMLAudioElement|null} */
let previewAudio = null
let previewTimer = 0

/** @param {any} error */
function fail(error) {
  const text = error instanceof Error ? error.message : String(error)
  state.error = text
  say(text, { kind: 'error' })
}

/** 勾选状态：第一次见到某一步时用后端给的默认值，之后记住用户的选择。
 *  没有可选模型时一律不勾：模型来自运行中的实例，没启动就选不了。 */
/** @param {any[]} steps */
function mergePicks(steps) {
  for (const step of steps || []) {
    const models = step.models || []
    const known = state.picks[step.key]
    if (!known) {
      state.picks[step.key] = {
        enabled: models.length > 0 && Boolean(step.default),
        model: models[0]?.id || '',
      }
      continue
    }
    if (!models.length) {
      known.enabled = false
      known.model = ''
      continue
    }
    const wasEmpty = !known.model
    if (!models.some((/** @type {any} */ row) => row.id === known.model)) {
      known.model = models[0].id
    }
    // 第一次出现可选项、且这一步默认勾选：顺手勾上，不必让用户回去补一下。
    if (wasEmpty && step.default) known.enabled = true
  }
}

/** 宿主打开这个工作区时调用：取一次状态，再按左栏的选中取目标。 */
export async function open() {
  await refresh()
  await loadTarget()
}

/** 离开工作区：停轮询、停试听。选中留给左栏，下次进来还是这条。 */
export function close() {
  reset()
}

export async function refresh() {
  if (loading) return
  loading = true
  try {
    const payload = await api.state()
    state.data = payload
    state.job = payload.job || null
    state.loaded = true
    state.error = ''
    if (!coreInited) {
      coreInited = true
      state.normalize = payload.core?.default !== false
    }
    mergePicks(payload.steps)
    if (state.job?.state === 'running') startPolling()
  } catch (error) {
    fail(error)
  } finally {
    loading = false
  }
}

/** 当前是不是在处理一份电脑文件。 */
export function isFileTarget() {
  return Boolean(state.fileTarget)
}

/** 电脑文件目标在界面上的那一条样例（音色目标没有这一份）。 */
export function fileSample() {
  const target = state.fileTarget
  if (!target) return null
  return {
    file: String(target.name || '音频'),
    text: '',
    emotion: '',
    language: '',
    duration: Number(target.duration) || 0,
    audio_url: api.sourceUrl(target.token),
    is_main: true,
  }
}

/** 目标音色在列表里的那一份（详情还没取回时用列表项，够画名字）。 */
export function targetVoice() {
  if (state.fileTarget) {
    return {
      id: `file:${state.fileTarget.token}`,
      shown_name: state.fileTarget.name,
      is_local: true,
      is_design: false,
      sample_count: 1,
    }
  }
  const id = store.pick.voiceId
  if (!id) return null
  if (state.target?.id === id) return state.target
  return store.voices.find((/** @type {any} */ row) => row.id === id) || null
}

/** 目标是否本地音色：电脑文件当然在本机；详情没到也先看列表项，避免按钮先亮后灭。 */
export function isLocalVoice() {
  if (state.fileTarget) return true
  return targetVoice()?.is_local !== false
}

/** 目标那一条样例（整条音色时取主样例）：试听与时长都看它。 */
export function targetSample() {
  if (state.fileTarget) return fileSample()
  const detail = state.target
  if (!detail || detail.id !== store.pick.voiceId) return null
  const samples = detail.samples || []
  const file = store.pick.file
  if (file) return samples.find((/** @type {any} */ row) => row.file === file) || null
  return samples.find((/** @type {any} */ row) => row.is_main) || samples[0] || null
}

/** 当前这一屏的样例行：电脑文件只有一条；整条音色列全部，指定了一条只列那一条。 */
export function visibleSamples() {
  if (state.fileTarget) {
    const sample = fileSample()
    return sample ? [sample] : []
  }
  const detail = state.target
  if (!detail || detail.id !== store.pick.voiceId) return []
  const samples = detail.samples || []
  return store.pick.file
    ? samples.filter((/** @type {any} */ row) => row.file === store.pick.file)
    : samples
}

/** 取回目标音色的详情。样例被替换 / 移除后文件已经不在时，落回整条音色。 */
export async function loadTarget() {
  // 左栏点了新音色：电脑文件让位。没有新音色时，文件目标自己留着。
  if (store.pick.voiceId && state.fileTarget) state.fileTarget = null
  if (state.fileTarget) return
  const id = store.pick.voiceId
  if (!id) {
    state.target = null
    state.selected = {}
    state.trims = {}
    state.selections = {}
    state.history = {}
    state.pickMode = ''
    stopPreview()
    return
  }
  if (state.target?.id === id && !state.targetLoading) {
    // 已经取过：只补一次"选中的样例还在不在"；模式（整条 / 单条）换了就重摆勾选。
    if (
      store.pick.file &&
      !(state.target.samples || []).some(
        (/** @type {any} */ row) => row.file === store.pick.file
      )
    ) {
      store.pick.file = ''
    }
    const mode = store.pick.file ? 'single' : 'all'
    if (state.pickMode !== mode) {
      state.pickMode = mode
      resetSelection(state.target)
    }
    return
  }
  state.targetLoading = true
  try {
    const detail = await api.voiceDetail(id)
    if (store.pick.voiceId !== id) return
    if (
      store.pick.file &&
      !(detail.samples || []).some(
        (/** @type {any} */ row) => row.file === store.pick.file
      )
    ) {
      store.pick.file = ''
    }
    state.target = detail
    state.pickMode = store.pick.file ? 'single' : 'all'
    prepareTarget(detail)
  } catch (error) {
    // 换了一条又被清掉的音色：目标没了就回空态，不要把上一份详情留在界面上。
    if (store.pick.voiceId === id) state.target = null
    fail(error)
  } finally {
    state.targetLoading = false
  }
}

/** 选中一份电脑文件：只有一条样例，默认全选、整段保留。 */
/** @param {any} target */
function prepareFile(target) {
  stopPreview()
  state.target = null
  state.applied = null
  state.preview = ''
  state.pickMode = 'single'
  const name = String(target?.name || '音频')
  const duration = round(target?.duration || 0)
  state.selected = { [name]: true }
  state.trims = { [name]: { segments: fullSegments(duration) } }
  state.selections = {}
  state.history = {}
}

/**
 * 上传一份电脑里的音频并设为处理目标；上传成功后清掉左栏的选中，
 * 让"现在处理的是文件"这件事只有一处说法。
 * @param {File} file
 */
export async function uploadSource(file) {
  if (state.busy || isRunning()) return
  stopPreview()
  state.busy = 'upload'
  try {
    const payload = await api.uploadSource(file)
    state.fileTarget = payload
    prepareFile(payload)
    actions.clearPick()
    say(`已选择文件「${payload.name}」。`, { kind: 'ok' })
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** 新取回一份详情：从全选、整段保留开始，停掉上一份的试听。 */
/** @param {any} detail */
function prepareTarget(detail) {
  stopPreview()
  state.applied = null
  state.preview = ''
  state.trims = Object.fromEntries(
    (detail.samples || []).map((/** @type {any} */ row) => [
      row.file,
      { segments: fullSegments(round(row.duration || 0)) },
    ])
  )
  state.selections = {}
  state.history = {}
  resetSelection(detail)
}

/** @param {any} detail */
function resetSelection(detail) {
  const only = store.pick.file
  state.selected = Object.fromEntries(
    (detail.samples || []).map((/** @type {any} */ row) => [
      row.file,
      only ? row.file === only : true,
    ])
  )
}

/** 标题下那一行：说到"哪一条音色、哪一条音频"就够，不写范围。 */
export function scopeLabel() {
  if (state.fileTarget) return `文件「${state.fileTarget.name}」`
  const voice = targetVoice()
  if (!voice) return '从左侧选一个音色，或放入一个文件'
  return store.pick.file
    ? `${voice.shown_name} · ${store.pick.file}`
    : `${voice.shown_name} · 整条音色`
}

/** 音频条副行：整条音色写条数与总时长，单条 / 文件写时长。 */
export function targetSub() {
  if (state.fileTarget) {
    const duration = Number(state.fileTarget.duration) || 0
    return duration ? `${duration.toFixed(1)} s · 电脑文件` : '电脑文件'
  }
  const detail = state.target
  if (!detail || detail.id !== store.pick.voiceId) return state.targetLoading ? '读取样例…' : ''
  const samples = detail.samples || []
  if (!samples.length) return '没有样例'
  if (store.pick.file) {
    const sample = targetSample()
    return sample?.duration ? `${sample.duration.toFixed(1)} s` : '时长未知'
  }
  const total = samples.reduce(
    (/** @type {number} */ sum, /** @type {any} */ row) => sum + (row.duration || 0),
    0
  )
  const head = `${samples.length} 条样例`
  return total ? `${head} · 共 ${Math.round(total)} s` : head
}

/** 当前这一屏已选的条数。 */
export function selectedSamples() {
  return visibleSamples().filter((/** @type {any} */ row) => state.selected[row.file])
}

/** @param {string} file */
export function toggleSelected(file) {
  if (isRunning()) return
  state.selected = { ...state.selected, [file]: !state.selected[file] }
}

/** @param {boolean} value */
export function selectAll(value) {
  if (isRunning()) return
  const next = { ...state.selected }
  for (const row of visibleSamples()) next[row.file] = value
  state.selected = next
}

/** 整段保留的区间。时长读不出来时为空：拖动手柄仍可用解码得到的时长兜底。 */
/** @param {number} duration */
function fullSegments(duration) {
  const value = round(duration || 0)
  return value > 0 ? [{ start: 0, end: value }] : []
}

/** 按文件名找这一屏里的样例：音色详情与电脑文件都认。 */
/** @param {string} file */
function sampleFor(file) {
  return visibleSamples().find((/** @type {any} */ row) => row.file === file) || null
}

/**
 * 一条样例要保留的区间。没有记录或记录为空时按整段算。
 * @param {any} sample
 */
export function segmentsOf(sample) {
  if (!sample) return []
  const rows = state.trims[sample.file]?.segments
  if (Array.isArray(rows) && rows.length) return rows
  return fullSegments(Number(sample.duration) || 0)
}

/** 清洗与合并：夹到时长内、排好序，重叠或贴着的段并成一段。 */
/** @param {{start: number, end: number}[]} rows @param {number} duration */
function cleanSegments(rows, duration) {
  /** @type {{ start: number, end: number }[]} */
  const parts = []
  for (const row of rows || []) {
    let start = Math.max(0, Number(row?.start) || 0)
    let end = Math.max(0, Number(row?.end) || 0)
    if (duration > 0) {
      start = Math.min(start, duration)
      end = Math.min(end, duration)
    }
    if (end - start > 0.001) parts.push({ start, end })
  }
  parts.sort((a, b) => a.start - b.start)
  /** @type {{ start: number, end: number }[]} */
  const merged = []
  for (const part of parts) {
    const last = merged[merged.length - 1]
    if (last && part.start <= last.end + 0.002) {
      last.end = Math.max(last.end, part.end)
      continue
    }
    merged.push({ ...part })
  }
  return merged.map((row) => ({ start: round(row.start), end: round(row.end) }))
}

/** @param {{start: number, end: number}[]} a @param {{start: number, end: number}[]} b */
function sameSegments(a, b) {
  if (a.length !== b.length) return false
  return a.every(
    (row, index) =>
      Math.abs(row.start - b[index].start) < 0.0005 &&
      Math.abs(row.end - b[index].end) < 0.0005
  )
}

/** @param {string} file */
function historyOf(file) {
  if (!state.history[file]) state.history[file] = { past: [], future: [] }
  return state.history[file]
}

/** 记一步改动：撤销栈上限只防长时间来回拖，不影响结果。 */
/** @param {string} file @param {{start: number, end: number}[]} segments */
function pushHistory(file, segments) {
  const box = historyOf(file)
  box.past.push(segments.map((row) => ({ ...row })))
  if (box.past.length > 80) box.past.shift()
  box.future = []
}

/** @param {any} sample */
export function isTrimmed(sample) {
  const rows = segmentsOf(sample)
  const duration = Number(sample?.duration) || 0
  if (!rows.length || duration <= 0) return false
  if (rows.length !== 1) return true
  return rows[0].start > 0.005 || rows[0].end < duration - 0.005
}

/** 保留的总秒数。 */
/** @param {any} sample */
export function keptSeconds(sample) {
  const rows = segmentsOf(sample)
  if (!rows.length) return Number(sample?.duration) || 0
  return rows.reduce((sum, row) => sum + Math.max(0, row.end - row.start), 0)
}

/**
 * 提交一次裁剪改动：清洗、查至少保留时长、记撤销。
 * @param {string} file
 * @param {{start: number, end: number}[]} rows
 */
function editSegments(file, rows) {
  const sample = sampleFor(file)
  if (!sample) return false
  const duration = Number(sample.duration) || 0
  const next = cleanSegments(rows, duration)
  const kept = next.reduce((sum, row) => sum + (row.end - row.start), 0)
  if (!next.length || kept < MIN_TRIM) {
    say(`至少保留 ${MIN_TRIM} 秒音频。`, { kind: 'warn' })
    return false
  }
  const current = segmentsOf(sample)
  if (sameSegments(current, next)) return false
  pushHistory(file, current)
  state.trims = { ...state.trims, [file]: { segments: next } }
  return true
}

/** @param {string} file */
export function resetTrim(file) {
  const sample = sampleFor(file)
  if (!sample) return
  const duration = Number(sample.duration) || 0
  if (duration <= 0) return
  editSegments(file, fullSegments(duration))
  clearSelection(file)
  stopPreview()
}

/** 当前选区；空区间视为没有选区。 */
/** @param {string} file */
export function selectionOf(file) {
  const range = state.selections[file]
  if (!range || range.end - range.start < 0.005) return null
  return range
}

/** @param {string} file @param {{start: number, end: number}} range */
export function setSelection(file, range) {
  const sample = sampleFor(file)
  if (!sample) return
  const duration = Number(sample.duration) || 0
  let start = Math.max(0, Number(range?.start) || 0)
  let end = Math.max(0, Number(range?.end) || 0)
  if (duration > 0) {
    start = Math.min(start, duration)
    end = Math.min(end, duration)
  }
  if (end < start) [start, end] = [end, start]
  if (end - start < 0.005) {
    clearSelection(file)
    return
  }
  state.selections = {
    ...state.selections,
    [file]: { start: round(start), end: round(end) },
  }
}

/** @param {string} file */
export function clearSelection(file) {
  if (!state.selections[file]) return
  const next = { ...state.selections }
  delete next[file]
  state.selections = next
}

/** @param {any} sample */
export function hasSelection(sample) {
  return Boolean(selectionOf(sample.file))
}

/** 删除选区：把每一段保留区间按选区切开，只留下选区之外的部分。 */
/** @param {any} sample */
export function deleteSelection(sample) {
  const range = selectionOf(sample.file)
  if (!range) return false
  /** @type {{ start: number, end: number }[]} */
  const next = []
  for (const row of segmentsOf(sample)) {
    if (range.end <= row.start + 0.0005 || range.start >= row.end - 0.0005) {
      next.push(row)
      continue
    }
    if (range.start > row.start + 0.0005) {
      next.push({ start: row.start, end: Math.min(range.start, row.end) })
    }
    if (range.end < row.end - 0.0005) {
      next.push({ start: Math.max(range.end, row.start), end: row.end })
    }
  }
  if (!editSegments(sample.file, next)) {
    if (
      sameSegments(
        segmentsOf(sample),
        cleanSegments(next, Number(sample.duration) || 0)
      )
    ) {
      say('选区里没有可删除的音频。', { kind: 'warn' })
    }
    return false
  }
  clearSelection(sample.file)
  stopPreview()
  return true
}

/** 仅保留选区：与现有保留区间取交集。 */
/** @param {any} sample */
export function keepSelection(sample) {
  const range = selectionOf(sample.file)
  if (!range) return false
  const next = segmentsOf(sample)
    .map((row) => ({
      start: Math.max(row.start, range.start),
      end: Math.min(row.end, range.end),
    }))
    .filter((row) => row.end > row.start)
  if (!editSegments(sample.file, next)) {
    if (
      sameSegments(
        segmentsOf(sample),
        cleanSegments(next, Number(sample.duration) || 0)
      )
    ) {
      say('选区里没有可保留的音频。', { kind: 'warn' })
    }
    return false
  }
  clearSelection(sample.file)
  stopPreview()
  return true
}

/** @param {string} file */
export function canUndo(file) {
  return Boolean(state.history[file]?.past.length)
}

/** @param {string} file */
export function canRedo(file) {
  return Boolean(state.history[file]?.future.length)
}

/** @param {string} file */
export function undoTrim(file) {
  const box = state.history[file]
  const sample = sampleFor(file)
  if (!box?.past.length || !sample) return false
  box.future.push(segmentsOf(sample).map((row) => ({ ...row })))
  const previous = box.past.pop()
  if (!previous) return false
  state.trims = { ...state.trims, [file]: { segments: previous.map((row) => ({ ...row })) } }
  clearSelection(file)
  stopPreview()
  return true
}

/** @param {string} file */
export function redoTrim(file) {
  const box = state.history[file]
  const sample = sampleFor(file)
  if (!box?.future.length || !sample) return false
  box.past.push(segmentsOf(sample).map((row) => ({ ...row })))
  const next = box.future.pop()
  if (!next) return false
  state.trims = { ...state.trims, [file]: { segments: next.map((row) => ({ ...row })) } }
  clearSelection(file)
  stopPreview()
  return true
}

/** 这一轮要提交的范围：files 是勾选结果，trims 只带真正裁过的条。 */
export function currentScope() {
  const rows = selectedSamples()
  /** @type {Record<string, { segments: { start: number, end: number }[] }>} */
  const trims = {}
  for (const sample of rows) {
    if (!isTrimmed(sample)) continue
    trims[sample.file] = {
      segments: segmentsOf(sample).map((row) => ({ start: row.start, end: row.end })),
    }
  }
  if (state.fileTarget) {
    return {
      kind: 'file',
      token: state.fileTarget.token,
      voice_id: '',
      file: '',
      files: rows.map((/** @type {any} */ row) => row.file),
      trims,
    }
  }
  return {
    kind: 'voice',
    voice_id: store.pick.voiceId,
    file: '',
    files: rows.map((/** @type {any} */ row) => row.file),
    trims,
  }
}

/** @param {any} step */
export function pickOf(step) {
  if (!state.picks[step.key]) state.picks[step.key] = { enabled: false, model: '' }
  return state.picks[step.key]
}

/** @param {any} step */
export function toggleStep(step) {
  if (isRunning() || !step.models?.length) return
  const pick = pickOf(step)
  pick.enabled = !pick.enabled
}

/** 选了模型就把这一步打开：换模型是个明确的"用它"动作。 */
/** @param {any} step */
export function enableStep(step) {
  if (isRunning() || !step.models?.length) return
  pickOf(step).enabled = true
}

/** 收尾可以跳过：不勾就不做高通与限峰，服务端仍统一单声道。 */
export function toggleNormalize() {
  if (isRunning() || state.busy) return
  state.normalize = !state.normalize
}

/** 已启用的步骤名（模型步骤 + 收尾），按执行顺序。 */
export function enabledSteps() {
  const rows = []
  for (const step of state.data?.steps || []) {
    const pick = state.picks[step.key]
    if (pick?.enabled && pick.model && step.models?.length) rows.push(step.label)
  }
  if (state.normalize) rows.push(state.data?.core?.label || '统一声道与峰值')
  return rows
}

export function stepsPayload() {
  const rows = (state.data?.steps || []).map((/** @type {any} */ step) => {
    const pick = state.picks[step.key] || { enabled: false, model: '' }
    return {
      key: step.key,
      enabled: Boolean(pick.enabled && pick.model && step.models?.length),
      model: pick.model,
      options: {},
    }
  })
  rows.push({ key: 'normalize', enabled: state.normalize, model: '', options: {} })
  return rows
}

/** 当前任务只在对上左栏这条音色时才显示：换一条音色，旧结果不该盖在新目标上。 */
export function jobForTarget() {
  const job = state.job
  if (!job || !job.scope) return null
  if (job.scope.kind === 'file') {
    return state.fileTarget && job.scope.token === state.fileTarget.token ? job : null
  }
  if (state.fileTarget) return null
  return job.scope.voice_id === store.pick.voiceId ? job : null
}

export function isRunning() {
  return jobForTarget()?.state === 'running'
}

/** @param {string} file */
export function itemFor(file) {
  const job = jobForTarget()
  return job?.items?.find((/** @type {any} */ row) => row.file === file) || null
}

/** 行上的状态：把任务状态与"有没有被选中"合成一句话。 */
/** @param {any} sample */
export function statusOf(sample) {
  const item = itemFor(sample.file)
  if (item) {
    if (item.state === 'running') return { key: 'running', label: '处理中', hint: '' }
    if (item.state === 'pending') return { key: 'queued', label: '排队', hint: '' }
    if (item.state === 'failed') return { key: 'failed', label: '失败', hint: item.error || '' }
    if (item.state === 'applied') return { key: 'applied', label: '已替换', hint: '' }
    if (item.state === 'done') {
      return item.replaceable
        ? { key: 'done', label: '已完成', hint: '' }
        : { key: 'stale', label: '时长偏差', hint: item.error || '' }
    }
    return { key: 'skipped', label: '跳过', hint: '' }
  }
  if (jobForTarget()) return { key: 'idle', label: '未选择', hint: '' }
  return state.selected[sample.file]
    ? { key: 'ready', label: '待处理', hint: '' }
    : { key: 'idle', label: '不处理', hint: '' }
}

/** @param {any} sample */
export function playSample(sample) {
  stopPreview()
  const voice = targetVoice()
  if (!sample?.audio_url || !voice) return
  if (store.player.url === sample.audio_url) {
    actions.togglePlay()
    return
  }
  actions.play({
    url: sample.audio_url,
    label: sample.file,
    sub: voice.shown_name,
    // 电脑文件不属于任何音色：播放条上的收藏 / 指定对它没有意义。
    voice: state.fileTarget ? null : voice,
  })
}

/** 试听裁剪后的结果：保留的每一段依次播放，段与段之间直接跳。 */
/** @param {any} sample */
export function previewTrim(sample) {
  const ranges = segmentsOf(sample)
  if (!ranges.length) return
  previewRanges(sample, 'result', ranges)
}

/** 试听当前选区。 */
/** @param {any} sample */
export function previewSelection(sample) {
  const range = selectionOf(sample.file)
  if (!range) return
  previewRanges(sample, 'selection', [range])
}

/** @param {any} sample @param {string} mode @param {{start: number, end: number}[]} ranges */
function previewRanges(sample, mode, ranges) {
  if (!sample?.audio_url || !ranges.length) return
  if (
    state.preview === sample.file &&
    state.previewMode === mode &&
    previewAudio &&
    !previewAudio.paused
  ) {
    stopPreview()
    return
  }
  stopPreview()
  // 两路声音同时出来听不出差别：壳上的播放条正在放整段时，先暂停它。
  if (store.player.playing) actions.togglePlay()
  const element = new Audio(sample.audio_url)
  let index = 0
  const playNext = () => {
    const range = ranges[index]
    if (!range) {
      stopPreview()
      return
    }
    try {
      element.currentTime = Math.max(0, range.start)
    } catch {
      // 元数据还没到时个别浏览器会拒绝设 currentTime；直接从头放也比不放强。
    }
    element.play().catch(() => stopPreview())
    previewTimer = window.setTimeout(
      () => {
        index += 1
        playNext()
      },
      Math.max(0, range.end - range.start) * 1000 + 40
    )
  }
  if (element.readyState >= 1) playNext()
  else element.addEventListener('loadedmetadata', playNext, { once: true })
  element.onended = () => stopPreview()
  previewAudio = element
  state.preview = sample.file
  state.previewMode = mode
  state.previewing = true
}

export function stopPreview() {
  if (previewTimer) {
    window.clearTimeout(previewTimer)
    previewTimer = 0
  }
  if (previewAudio) {
    previewAudio.pause()
    previewAudio = null
  }
  state.preview = ''
  state.previewMode = ''
  state.previewing = false
}

export async function start() {
  if (state.busy) return
  if (!state.fileTarget && !store.pick.voiceId) {
    say('先从左侧选一个音色，或放入一个文件。', { kind: 'warn' })
    return
  }
  if (!isLocalVoice()) {
    say('平台音色的样例不在本机，换一个本地音色再处理。', { kind: 'warn' })
    return
  }
  if (!selectedSamples().length) {
    say('先选中至少一条音频。', { kind: 'warn' })
    return
  }
  if (!enabledSteps().length && !selectedSamples().some((/** @type {any} */ row) => isTrimmed(row))) {
    say('没有要执行的步骤：选择处理步骤，或先在波形上裁剪。', { kind: 'warn' })
    return
  }
  stopPreview()
  state.busy = 'run'
  state.applied = null
  try {
    const scope = currentScope()
    // 只裁剪时不碰未改动的行：它们既没有模型步骤也没有收尾，处理一遍只会得到同一段音频。
    if (!enabledSteps().length) {
      scope.files = scope.files.filter((/** @type {string} */ file) => Boolean(scope.trims[file]))
    }
    const payload = await api.run(scope, stepsPayload())
    state.job = payload
    startPolling()
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

export async function cancel() {
  try {
    state.job = await api.cancel()
  } catch (error) {
    fail(error)
  }
}

function startPolling() {
  if (timer) return
  timer = window.setInterval(poll, 1200)
}

function stopPolling() {
  if (!timer) return
  window.clearInterval(timer)
  timer = 0
}

export async function poll() {
  try {
    const payload = await api.job()
    if (!payload || !payload.id) return
    state.job = payload
    if (payload.state !== 'running') stopPolling()
  } catch {
    // 轮询失败不弹提示：下一次会重试，服务确实停了时开始处理那一步会报。
  }
}

/** 替换勾选且可替换的完成条目。勾选就是行本身，不再另开一轮选择。 */
export async function applySelected() {
  const job = jobForTarget()
  if (!job || state.busy || job.scope?.kind === 'file') return
  const indexes = (job.items || [])
    .filter(
      (/** @type {any} */ row) =>
        state.selected[row.file] && row.state === 'done' && row.replaceable
    )
    .map((/** @type {any} */ row) => row.index)
  if (!indexes.length) {
    say('没有可替换的条目。', { kind: 'warn' })
    return
  }
  state.busy = 'apply'
  try {
    const payload = await api.apply(job.id, indexes)
    state.applied = payload
    state.job = payload.job
    const voiceId = job.scope.voice_id
    // 换过的文件时长 / 处理链 / 波形都变了：目标详情、音色列表与波形缓存一起刷新。
    const appliedFiles = new Set(
      (job.items || [])
        .filter((/** @type {any} */ row) => indexes.includes(row.index))
        .map((/** @type {any} */ row) => row.file)
    )
    for (const sample of state.target?.samples || []) {
      if (appliedFiles.has(sample.file)) invalidateWaveform(sample.audio_url)
    }
    state.waveRevision += 1
    state.target = null
    await Promise.all([refresh(), loadTarget(), actions.reloadVoice(voiceId)])
    state.applied = payload
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** 不替换，回到可编辑状态：清掉后端当前任务，保留刚才那组裁剪。 */
export async function resetRun() {
  if (state.busy) return
  const applied = state.job?.state === 'applied'
  try {
    await api.clearJob()
  } catch (error) {
    fail(error)
    return
  }
  state.job = null
  state.applied = null
  state.playing = ''
  if (audio) {
    audio.pause()
    audio = null
  }
  stopPreview()
  state.selections = {}
  if (applied) {
    // 替换后的文件已经是新内容，旧区间不再对应当前时长：回到整段。
    if (state.fileTarget) {
      prepareFile(state.fileTarget)
      return
    }
    const detail = state.target
    if (detail) {
      state.trims = Object.fromEntries(
        (detail.samples || []).map((/** @type {any} */ row) => [
          row.file,
          { segments: fullSegments(round(row.duration || 0)) },
        ])
      )
      state.history = {}
      resetSelection(detail)
    }
  }
}

/** 换样例：复用宿主那张详情（与生成页「选样例」同一条路），在里面点「使用」。 */
export function changeSample() {
  const voice = targetVoice()
  if (voice) void actions.openDetail(voice)
}

/** 清掉当前目标：电脑文件连任务一起清；音色交给左栏（宿主那份）。 */
export async function clearTarget() {
  if (isRunning() || state.busy) return
  if (state.fileTarget) {
    // 只清这份文件自己的任务；换成文件后，上一条音色的旧任务不在这里替它清。
    const wasFileJob = Boolean(jobForTarget())
    state.fileTarget = null
    state.target = null
    state.pickMode = ''
    state.selected = {}
    state.trims = {}
    state.selections = {}
    state.history = {}
    state.job = null
    state.applied = null
    state.preview = ''
    stopPreview()
    if (wasFileJob) {
      try {
        await api.clearJob()
      } catch {
        // 清理失败不影响已经回到空态。
      }
    }
    return
  }
  actions.clearPick()
}

/** 电脑文件的结果下载名：与原文件同名，加「（处理）」。 */
export function downloadName() {
  const target = state.fileTarget
  if (!target) return '处理结果.wav'
  const stem = String(target.name || '').replace(/\.[^.]+$/, '').trim() || '音频'
  return `${stem}（处理）.wav`
}

/** 触发浏览器下载电脑文件的处理结果。 */
export function downloadResult() {
  const job = jobForTarget()
  const item = job?.items?.[0]
  if (!job || job.scope?.kind !== 'file' || item?.state !== 'done') return
  const link = document.createElement('a')
  link.href = api.downloadUrl(job.id, item.index)
  link.download = downloadName()
  document.body.appendChild(link)
  link.click()
  link.remove()
}

/** 一行里 A/B 试听：借用任务目录的两份产物。 */
/**
 * @param {number} index
 * @param {string} side
 */
export function play(index, side) {
  const job = jobForTarget()
  if (!job?.id) return
  stopPreview()
  const key = `${index}:${side}`
  if (audio) {
    audio.pause()
    audio = null
  }
  if (state.playing === key) {
    state.playing = ''
    return
  }
  const element = new Audio(api.audioUrl(job.id, index, side))
  element.onended = () => {
    state.playing = ''
  }
  element.onerror = () => {
    state.playing = ''
    say('这一段试听不了。', { kind: 'error' })
  }
  element.play().catch(() => {
    state.playing = ''
  })
  audio = element
  state.playing = key
}

export function reset() {
  stopPolling()
  stopPreview()
  if (audio) audio.pause()
  audio = null
  state.playing = ''
}

/** @param {number} value */
function round(value) {
  return Math.round((Number(value) || 0) * 1000) / 1000
}
