// @ts-check
// 音色处理工作区的状态与动作。
//
// 处理序列就是行表：每条样例按「音色 + 文件名」记一次，往序列里加别的音色不会顶掉
// 已有的行。音色目标仍来自左栏的选中（`store.pick`）：点卡替换序列，「添加」追加整张卡。
// 文件目标存在 `state.fileTarget`，只产出可下载的结果，不写回音色库。
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
  /** 正在下载/加载哪个候选模型（`step:model`）。 */
  modelBusy: '',
  /** 已取回的音色详情：voiceId → detail（samples 带时长与试听地址） */
  /** @type {Record<string, any>} */
  cards: {},
  /** 正在取哪条音色的详情 */
  loadingVoice: '',
  /** 电脑里选中的音频：`{ token, name, duration }`；有它时优先于音色序列。 */
  /** @type {any} */ fileTarget: null,
  /** @type {Record<string, { enabled: boolean, model: string }>} */
  picks: {},
  /** 收尾步骤（统一声道与峰值）可跳过；默认取后端声明。 */
  normalize: true,
  /** 处理序列：每张音色卡一个窗口，窗口里是这张卡要处理的样例（按加入顺序）。 */
  /** @type {{ voiceId: string, files: string[] }[]} */
  windows: [],
  /** 当前打开的窗口（音色 id）。 */
  activeVoiceId: '', 
  /** 每条样例要保留的区间，按行键：key → { segments: [{start, end}, ...] } */
  /** @type {Record<string, { segments: { start: number, end: number }[] }>} */
  trims: {},
  /** 当前选区：key → {start, end}；没有选区时不存在这一项 */
  /** @type {Record<string, { start: number, end: number }>} */
  selections: {},
  /** 裁剪撤销 / 前进：key → { past, future } */
  /** @type {Record<string, { past: { start: number, end: number }[][], future: { start: number, end: number }[][] }>} */
  history: {},
  /** @type {any} */ job: null,
  /** 正在试听 A/B 的 ``index:side`` 不另存：播放条认 URL，见 `playingSide`。 */
  /** @type {any} */ applied: null,
  /** 最近一次入库的回执：`{ saved, files, failures }` */
  /** @type {any} */ saved: null,
  /** 正在试听哪一行 */
  preview: '',
  /** 试听的是整段结果还是选区 */
  previewMode: '',
  previewing: false,
  /** 替换后加一：波形 URL 不变，用它通知已挂着的 Waveform 重新解码。 */
  waveRevision: 0,
})

let timer = 0
let modelTimer = 0
let loading = false
let coreInited = false
/** 刚点过下载的模型：下载完成后优先选中它。 */
let pendingModel = /** @type {{ step: string, model: string } | null} */ (null)
/** @type {HTMLAudioElement|null} */
let previewAudio = null
let previewTimer = 0

/** 一行在序列里按「音色 + 文件名」认：不同音色同名样例不会互相顶掉。 */
/** @param {string} voiceId @param {string} file */
export function rowKey(voiceId, file) {
  return `${voiceId}\u0000${file}`
}

/** @param {string} voiceId @param {string} file */
function keyOf(voiceId, file) {
  return rowKey(voiceId, file)
}

/** @param {any} error */
function fail(error) {
  const text = error instanceof Error ? error.message : String(error)
  state.error = text
  say(text, { kind: 'error' })
}

/** 勾选状态：第一次见到某一步时用后端给的默认值，之后记住用户的选择。
 *  只有下载过的模型才能选；未下载的模型仍列在卡片里，等用户点下载。
 *  只有一个可用模型时自动选中；有多个时留空，由用户在卡片里选。 */
/** @param {any[]} steps */
function mergePicks(steps) {
  for (const step of steps || []) {
    const models = step.models || []
    const ready = models.filter((/** @type {any} */ row) => row.downloaded)
    const known = state.picks[step.key]
    if (!known) {
      state.picks[step.key] = {
        enabled: ready.length === 1 && Boolean(step.default),
        model: ready.length === 1 ? ready[0].id : '',
      }
    } else if (!ready.length) {
      known.enabled = false
      known.model = ''
    } else if (!ready.some((/** @type {any} */ row) => row.id === known.model)) {
      // 选过的那一个不在了：只剩一个可以自动补上，多个则交回用户选。
      known.model = ready.length === 1 ? ready[0].id : ''
      if (!known.model) known.enabled = false
    }
    const pick = state.picks[step.key]
    if (
      pendingModel &&
      pendingModel.step === step.key &&
      ready.some((/** @type {any} */ row) => row.id === pendingModel?.model)
    ) {
      pick.model = pendingModel.model
      pick.enabled = true
      pendingModel = null
    }
  }
}

/** 宿主打开这个工作区时调用：取一次状态，再恢复序列或按左栏的选中铺一次。 */
export async function open() {
  await refresh()
  await restore()
}

/** 离开工作区：停轮询、停试听。序列留给下次进来。 */
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
    if (hasActiveModelJob()) startModelPolling()
    else stopModelPolling()
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

/** 电脑文件目标在界面上的那一条样例（音色序列没有这一份）。 */
export function fileSample() {
  const target = state.fileTarget
  if (!target) return null
  const name = String(target.name || '音频')
  const voiceId = `file:${target.token}`
  return {
    key: keyOf(voiceId, name),
    voice_id: voiceId,
    voice_name: '电脑文件',
    voice_local: true,
    file: name,
    text: '',
    emotion: '',
    language: '',
    duration: Number(target.duration) || 0,
    audio_url: api.sourceUrl(target.token),
    is_main: true,
  }
}

/** 左栏与音色列表里能查到的音色摘要；详情还没到也够画名字。 */
/** @param {string} id */
function listVoice(id) {
  return (
    store.allVoices?.find((/** @type {any} */ row) => row.id === id) ||
    store.voices?.find((/** @type {any} */ row) => row.id === id) ||
    null
  )
}

/** 这张卡的详情：取过就用缓存，`force` 时重新取。 */
/** @param {string} id @param {boolean} [force] */
export async function ensureVoice(id, force = false) {
  if (!id) return null
  if (!force && state.cards[id]) return state.cards[id]
  state.loadingVoice = id
  try {
    const detail = await api.voiceDetail(id)
    state.cards = { ...state.cards, [id]: detail }
    return detail
  } catch (error) {
    fail(error)
    return null
  } finally {
    if (state.loadingVoice === id) state.loadingVoice = ''
  }
}

/** 序列里的音色，按窗口顺序。 */
export function queueVoiceIds() {
  if (state.fileTarget) {
    return [`file:${state.fileTarget.token}`]
  }
  return state.windows.map((win) => win.voiceId)
}

/** @param {string} voiceId */
function windowOf(voiceId) {
  return state.windows.find((win) => win.voiceId === voiceId) || null
}

/** 当前窗口；没有时退回第一个窗口。 */
export function activeWindow() {
  return windowOf(state.activeVoiceId) || state.windows[0] || null
}

/** 替换一个窗口的样例列表；窗口不存在时追加到末尾。 */
/** @param {string} voiceId @param {string[]} files */
function setWindowFiles(voiceId, files) {
  const next = state.windows.map((win) =>
    win.voiceId === voiceId ? { voiceId, files: [...files] } : win
  )
  if (!next.some((win) => win.voiceId === voiceId)) next.push({ voiceId, files: [...files] })
  state.windows = next
}

/** 丢掉一张音色在序列里的裁剪、选区与撤销记录。 */
/** @param {string} voiceId */
function clearVoiceState(voiceId) {
  const prefix = `${voiceId}\u0000`
  /** @param {Record<string, any>} box */
  const keep = (box) =>
    Object.fromEntries(Object.entries(box).filter(([key]) => !key.startsWith(prefix)))
  state.trims = keep(state.trims)
  state.selections = keep(state.selections)
  state.history = keep(state.history)
}

/** 页签数据：窗口里的音色名与条数。 */
export function windowViews() {
  return state.windows.map((win) => {
    const voice = state.cards[win.voiceId] || listVoice(win.voiceId)
    return {
      voiceId: win.voiceId,
      name: voice?.shown_name || '音色',
      count: win.files.length,
      active: win.voiceId === state.activeVoiceId,
    }
  })
}

/** 切换到某个窗口；窗口不存在时不动。 */
/** @param {string} voiceId */
export function activateWindow(voiceId) {
  if (!windowOf(voiceId)) return
  state.activeVoiceId = voiceId
  // 左栏高亮跟着页签走；直接改值，不触发重新加载。
  store.pick.voiceId = voiceId
  store.pick.file = ''
}

/** 把一张卡从序列里移除：连同它的裁剪与选区一起清掉。 */
/** @param {string} voiceId */
export function closeWindow(voiceId) {
  if (isRunning()) return
  if (!windowOf(voiceId)) return
  clearVoiceState(voiceId)
  const index = state.windows.findIndex((win) => win.voiceId === voiceId)
  state.windows = state.windows.filter((win) => win.voiceId !== voiceId)
  if (state.activeVoiceId === voiceId) {
    const next = state.windows[Math.min(index, state.windows.length - 1)]
    state.activeVoiceId = next?.voiceId || ''
  }
  if (store.pick.voiceId === voiceId) {
    store.pick.voiceId = state.activeVoiceId
    store.pick.file = ''
  }
  dropJobIfOrphaned()
}

/** 目标那条音色：序列的第一行属于谁，卡片就显示谁。 */
export function targetVoice() {
  if (state.fileTarget) {
    return {
      id: `file:${state.fileTarget.token}`,
      shown_name: state.fileTarget.name,
      is_local: true,
      is_design: false,
      sample_count: 1,
      main_audio_url: api.sourceUrl(state.fileTarget.token),
    }
  }
  const win = activeWindow()
  if (!win) return null
  return (
    state.cards[win.voiceId] ||
    listVoice(win.voiceId) ||
    { id: win.voiceId, shown_name: '音色', is_local: true, sample_count: 0 }
  )
}

/** 目标是否全部本地：序列里混进平台音色就禁止开始。 */
export function isLocalVoice() {
  if (state.fileTarget) return true
  if (!state.windows.length) return true
  return queueVoiceIds().every((id) => {
    const voice = state.cards[id] || listVoice(id)
    return voice?.is_local !== false
  })
}

/** 一个窗口里的样例行。详情还没到的行先不画。 */
/** @param {string} voiceId */
function windowRows(voiceId) {
  const win = windowOf(voiceId)
  const card = state.cards[voiceId]
  if (!win || !card) return []
  const rows = []
  for (const file of win.files) {
    const sample = (card.samples || []).find((/** @type {any} */ row) => row.file === file)
    if (!sample) continue
    rows.push({
      ...sample,
      key: keyOf(voiceId, file),
      voice_id: voiceId,
      voice_name: card.shown_name || '',
      voice_local: card.is_local !== false,
    })
  }
  return rows
}

/** 当前窗口的表。 */
export function queueRows() {
  if (state.fileTarget) {
    const sample = fileSample()
    return sample ? [sample] : []
  }
  const win = activeWindow()
  return win ? windowRows(win.voiceId) : []
}

/** 整个序列（所有窗口）的行，按页签顺序。 */
export function allRows() {
  if (state.fileTarget) return queueRows()
  return state.windows.flatMap((win) => windowRows(win.voiceId))
}

/** 已结束的任务在序列被清空后没有意义了：清掉它，别让底栏继续报上一轮的结果。 */
function dropJobIfOrphaned() {
  const job = state.job
  if (!job || job.state === 'running') return
  if (job.scope?.kind === 'file') return
  const ids = jobVoiceIds(job)
  const queued = new Set(queueVoiceIds())
  if (!ids.length || ids.some((id) => queued.has(id))) return
  state.job = null
  state.applied = null
  api.clearJob().catch(() => {})
}

/**
 * 点卡片 / 卡右「添加」带来的变化。
 *
 * - 点卡片（`select`）：这条音色的全部样例就是这一轮；
 * - 卡右「添加」（`add`）：把这张卡的样例追加进序列，已有的行不动；
 * - 详情里的「使用」带 `file`：只追加那一条。
 */
export async function loadTarget() {
  const voiceId = store.pick.voiceId
  const mode = store.pick.mode === 'add' ? 'add' : 'select'
  const file = store.pick.file
  if (state.fileTarget) {
    if (!voiceId) {
      prepareFile()
      return
    }
    // 左栏点了音色：电脑文件让位。
    state.fileTarget = null
  }
  if (!voiceId) return
  const detail = await ensureVoice(voiceId)
  if (!detail) return
  if (store.pick.voiceId !== voiceId) return
  const known = windowOf(voiceId)
  if (file) {
    // 详情里的「使用」：只把这一条加进这张卡的窗口。
    const files = known ? [...known.files] : []
    if (!files.includes(file)) files.push(file)
    setWindowFiles(voiceId, files)
    state.activeVoiceId = voiceId
    return
  }
  if (mode === 'add' || !known) {
    // 第一次加进来与再点「添加」是同一种结果：这张卡的全部样例（已移出的补回来）。
    const files = known ? [...known.files] : []
    const seen = new Set(files)
    for (const sample of detail.samples || []) {
      if (seen.has(sample.file)) continue
      seen.add(sample.file)
      files.push(sample.file)
    }
    setWindowFiles(voiceId, files)
    state.activeVoiceId = voiceId
    return
  }
  // 点卡片：打开这张卡自己的窗口，移出过的行保持移出。
  state.activeVoiceId = voiceId
}

/** 进工作区时恢复序列：刷新已在序列里的音色；序列为空才按左栏选中铺一次。 */
export async function restore() {
  if (state.fileTarget) return
  const ids = queueVoiceIds()
  if (ids.length) {
    await Promise.all(ids.map((id) => ensureVoice(id, true)))
    state.windows = state.windows.map((win) => ({
      voiceId: win.voiceId,
      files: win.files.filter((file) =>
        (state.cards[win.voiceId]?.samples || []).some(
          (/** @type {any} */ sample) => sample.file === file
        )
      ),
    }))
    if (!windowOf(state.activeVoiceId)) {
      state.activeVoiceId = state.windows[0]?.voiceId || ''
    }
    return
  }
  if (store.pick.voiceId) await loadTarget()
}

/** 电脑文件：只有一条，整段保留。 */
function prepareFile() {
  stopPreview()
  state.applied = null
  state.preview = ''
  state.trims = {}
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
    clearFinishedJob()
    state.windows = []
    state.activeVoiceId = ''
    state.trims = {}
    state.selections = {}
    state.history = {}
    state.fileTarget = payload
    prepareFile()
    actions.clearPick()
    say(`已选择文件「${payload.name}」。`, { kind: 'ok' })
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** 音频条副行：条数与总时长；跨音色时另说一句。 */
export function targetSub() {
  if (state.fileTarget) {
    const duration = Number(state.fileTarget.duration) || 0
    return duration ? `${duration.toFixed(1)} s · 电脑文件` : '电脑文件'
  }
  const rows = queueRows()
  if (!rows.length) return state.loadingVoice ? '读取样例…' : '没有样例'
  const total = rows.reduce(
    (/** @type {number} */ sum, /** @type {any} */ row) => sum + (row.duration || 0),
    0
  )
  const head = `${rows.length} 条样例`
  return total ? `${head} · 共 ${Math.round(total)} s` : head
}

/** 从处理序列移除。移掉的正是详情里「使用」记下的那一条时，把那个记号一起清掉。 */
/** @param {string} key */
export function removeFromQueue(key) {
  if (!key || isRunning()) return
  for (const win of state.windows) {
    const file = win.files.find((row) => keyOf(win.voiceId, row) === key)
    if (!file) continue
    setWindowFiles(win.voiceId, win.files.filter((row) => row !== file))
    if (store.pick.voiceId === win.voiceId && store.pick.file === file) {
      store.pick.file = ''
    }
    return
  }
}

/** 只保留这张卡：处理序列收成这一行所在的音色卡，卡内样例不动。 */
/** @param {string} key */
export function onlyThis(key) {
  if (!key || isRunning()) return
  for (const win of state.windows) {
    if (!win.files.some((row) => keyOf(win.voiceId, row) === key)) continue
    for (const other of state.windows) {
      if (other.voiceId === win.voiceId) continue
      clearVoiceState(other.voiceId)
    }
    state.windows = [{ voiceId: win.voiceId, files: [...win.files] }]
    state.activeVoiceId = win.voiceId
    store.pick.voiceId = win.voiceId
    return
  }
}

/** 按文件名找序列里的样例。 */
/** @param {string} key */
function sampleFor(key) {
  return queueRows().find((/** @type {any} */ row) => row.key === key) || null
}

/** 整段保留的区间。时长读不出来时为空：拖动手柄仍可用解码得到的时长兜底。 */
/** @param {number} duration */
function fullSegments(duration) {
  const value = round(duration || 0)
  return value > 0 ? [{ start: 0, end: value }] : []
}

/**
 * 一条样例要保留的区间。没有记录或记录为空时按整段算。
 * @param {any} sample
 */
export function segmentsOf(sample) {
  if (!sample) return []
  const rows = state.trims[sample.key]?.segments
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

/** @param {string} key */
function historyOf(key) {
  if (!state.history[key]) state.history[key] = { past: [], future: [] }
  return state.history[key]
}

/** 记一步改动：撤销栈上限只防长时间来回拖，不影响结果。 */
/** @param {string} key @param {{start: number, end: number}[]} segments */
function pushHistory(key, segments) {
  const box = historyOf(key)
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
 * @param {string} key
 * @param {{start: number, end: number}[]} rows
 */
function editSegments(key, rows) {
  const sample = sampleFor(key)
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
  pushHistory(key, current)
  state.trims = { ...state.trims, [key]: { segments: next } }
  return true
}

/** @param {string} key */
export function resetTrim(key) {
  const sample = sampleFor(key)
  if (!sample) return
  const duration = Number(sample.duration) || 0
  if (duration <= 0) return
  editSegments(key, fullSegments(duration))
  clearSelection(key)
  stopPreview()
}

/** 当前选区；空区间视为没有选区。 */
/** @param {string} key */
export function selectionOf(key) {
  const range = state.selections[key]
  if (!range || range.end - range.start < 0.005) return null
  return range
}

/** @param {string} key @param {{start: number, end: number}} range */
export function setSelection(key, range) {
  const sample = sampleFor(key)
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
    clearSelection(key)
    return
  }
  state.selections = {
    ...state.selections,
    [key]: { start: round(start), end: round(end) },
  }
}

/** @param {string} key */
export function clearSelection(key) {
  if (!state.selections[key]) return
  const next = { ...state.selections }
  delete next[key]
  state.selections = next
}

/** @param {any} sample */
export function hasSelection(sample) {
  return Boolean(sample?.key && selectionOf(sample.key))
}

/** 删除选区：把每一段保留区间按选区切开，只留下选区之外的部分。 */
/** @param {any} sample */
export function deleteSelection(sample) {
  if (!sample) return false
  const range = selectionOf(sample.key)
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
  if (!editSegments(sample.key, next)) {
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
  clearSelection(sample.key)
  stopPreview()
  return true
}

/** 仅保留选区：与现有保留区间取交集。 */
/** @param {any} sample */
export function keepSelection(sample) {
  if (!sample) return false
  const range = selectionOf(sample.key)
  if (!range) return false
  const next = segmentsOf(sample)
    .map((row) => ({
      start: Math.max(row.start, range.start),
      end: Math.min(row.end, range.end),
    }))
    .filter((row) => row.end > row.start)
  if (!editSegments(sample.key, next)) {
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
  clearSelection(sample.key)
  stopPreview()
  return true
}

/** @param {string} key */
export function canUndo(key) {
  return Boolean(state.history[key]?.past.length)
}

/** @param {string} key */
export function canRedo(key) {
  return Boolean(state.history[key]?.future.length)
}

/** @param {string} key */
export function undoTrim(key) {
  const box = state.history[key]
  const sample = sampleFor(key)
  if (!box?.past.length || !sample) return false
  box.future.push(segmentsOf(sample).map((row) => ({ ...row })))
  const previous = box.past.pop()
  if (!previous) return false
  state.trims = { ...state.trims, [key]: { segments: previous.map((row) => ({ ...row })) } }
  clearSelection(key)
  stopPreview()
  return true
}

/** @param {string} key */
export function redoTrim(key) {
  const box = state.history[key]
  const sample = sampleFor(key)
  if (!box?.future.length || !sample) return false
  box.past.push(segmentsOf(sample).map((row) => ({ ...row })))
  const next = box.future.pop()
  if (!next) return false
  state.trims = { ...state.trims, [key]: { segments: next.map((row) => ({ ...row })) } }
  clearSelection(key)
  stopPreview()
  return true
}

/** 这一轮要提交的范围：按音色分组，组内按行序；trims 只带真正裁过的条。 */
export function currentScope() {
  const rows = allRows()
  if (state.fileTarget) {
    const sample = rows[0]
    return {
      kind: 'file',
      token: state.fileTarget.token,
      voice_id: '',
      file: '',
      files: rows.map((/** @type {any} */ row) => row.file),
      trims: sample && isTrimmed(sample)
        ? { [sample.file]: { segments: segmentsOf(sample).map((row) => ({ start: row.start, end: row.end })) } }
        : {},
    }
  }
  /** @type {{ voice_id: string, files: string[], trims: Record<string, any> }[]} */
  const groups = []
  for (const sample of rows) {
    let group = groups.find((row) => row.voice_id === sample.voice_id)
    if (!group) {
      group = { voice_id: sample.voice_id, files: [], trims: {} }
      groups.push(group)
    }
    group.files.push(sample.file)
    if (isTrimmed(sample)) {
      group.trims[sample.file] = {
        segments: segmentsOf(sample).map((row) => ({ start: row.start, end: row.end })),
      }
    }
  }
  return { kind: 'voices', voices: groups }
}

/** @param {any} step */
export function pickOf(step) {
  if (!state.picks[step.key]) state.picks[step.key] = { enabled: false, model: '' }
  return state.picks[step.key]
}

/** 这一步已经下载、可以选中的模型。 */
/** @param {any} step */
export function readyModels(step) {
  return (step.models || []).filter((/** @type {any} */ row) => row.downloaded)
}

/** 当前选中的候选模型；没有时返回空。 */
/** @param {any} step */
export function currentModel(step) {
  const id = pickOf(step).model
  return (step.models || []).find((/** @type {any} */ row) => row.id === id) || null
}

/** @param {any} step @param {any} model */
export function selectModel(step, model) {
  if (isRunning() || !model?.downloaded) return
  const pick = pickOf(step)
  pick.model = model.id
  pick.enabled = true
}

/**
 * 下载一个候选模型。服务端下载完成后会自动登记/加载，这里只轮询状态。
 * @param {any} step
 * @param {any} model
 */
export async function downloadModel(step, model) {
  if (isRunning() || state.modelBusy) return
  if (model?.downloaded) {
    selectModel(step, model)
    return
  }
  pendingModel = { step: step.key, model: model.id }
  state.modelBusy = `${step.key}:${model.id}`
  try {
    const payload = await api.downloadModel(step.key, model.id)
    state.data = payload
    mergePicks(payload.steps)
    startModelPolling()
  } catch (error) {
    pendingModel = null
    fail(error)
  } finally {
    state.modelBusy = ''
  }
}

/** @param {any} step */
export function toggleStep(step) {
  const ready = readyModels(step)
  if (isRunning() || !ready.length) return
  const pick = pickOf(step)
  if (!pick.model) {
    if (ready.length !== 1) return
    pick.model = ready[0].id
  }
  pick.enabled = !pick.enabled
}

/** 选了模型就把这一步打开：换模型是个明确的"用它"动作。 */
/** @param {any} step */
export function enableStep(step) {
  const ready = readyModels(step)
  if (isRunning() || !ready.length) return
  const pick = pickOf(step)
  if (!pick.model) {
    if (ready.length !== 1) return
    pick.model = ready[0].id
  }
  pick.enabled = true
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
    if (
      pick?.enabled &&
      pick.model &&
      readyModels(step).some((/** @type {any} */ row) => row.id === pick.model)
    ) {
      rows.push(step.label)
    }
  }
  if (state.normalize) rows.push(state.data?.core?.label || '统一声道与峰值')
  return rows
}

export function stepsPayload() {
  const rows = (state.data?.steps || []).map((/** @type {any} */ step) => {
    const pick = state.picks[step.key] || { enabled: false, model: '' }
    return {
      key: step.key,
      enabled: Boolean(
        pick.enabled &&
          pick.model &&
          readyModels(step).some((/** @type {any} */ row) => row.id === pick.model)
      ),
      model: pick.model,
      options: {},
    }
  })
  rows.push({ key: 'normalize', enabled: state.normalize, model: '', options: {} })
  return rows
}

/** 任务涉及哪些音色（电脑文件不算）。 */
/** @param {any} job @returns {string[]} */
function jobVoiceIds(job) {
  const scope = job?.scope
  if (!scope || scope.kind === 'file') return []
  if (Array.isArray(scope.voices) && scope.voices.length) {
    return scope.voices
      .map((/** @type {any} */ row) => String(row.voice_id || ''))
      .filter(Boolean)
  }
  return scope.voice_id ? [String(scope.voice_id)] : []
}

/** 当前任务只在与序列里的音色有交集时显示：换了一组音色，旧结果不该盖在新序列上。 */
export function jobForTarget() {
  const job = state.job
  if (!job) return null
  if (job.scope?.kind === 'file') {
    return state.fileTarget && job.scope.token === state.fileTarget.token ? job : null
  }
  if (state.fileTarget) return null
  const queued = new Set(queueVoiceIds())
  if (!queued.size) return null
  return jobVoiceIds(job).some((id) => queued.has(id)) ? job : null
}

export function isRunning() {
  return jobForTarget()?.state === 'running'
}

/** 一行对应的任务条目：任务按「音色 + 文件名」记条。 */
/** @param {string} key */
export function itemFor(key) {
  const job = jobForTarget()
  return (
    job?.items?.find(
      (/** @type {any} */ row) => keyOf(row.voice_id, row.file) === key
    ) || null
  )
}

/** 行上的状态：只有任务给得出状态时才返回标签；没有任务时这一格不显示。 */
/** @param {any} sample */
export function statusOf(sample) {
  const item = sample?.key ? itemFor(sample.key) : null
  if (item) {
    if (item.state === 'running') return { key: 'running', label: '处理中', hint: '' }
    if (item.state === 'pending') return { key: 'queued', label: '排队', hint: '' }
    if (item.state === 'failed') return { key: 'failed', label: '失败', hint: item.error || '' }
    if (item.state === 'applied') return { key: 'applied', label: '已替换', hint: '' }
    if (item.state === 'saved') return { key: 'saved', label: '已入库', hint: '' }
    if (item.state === 'done') {
      return item.replaceable
        ? { key: 'done', label: '已完成', hint: '' }
        : { key: 'stale', label: '时长偏差', hint: item.error || '' }
    }
    return { key: 'skipped', label: '跳过', hint: '' }
  }
  return { key: 'idle', label: '', hint: '' }
}

/** @param {any} sample */
export function playSample(sample) {
  stopPreview()
  if (!sample?.audio_url) return
  const voice = sample.voice_id?.startsWith('file:')
    ? null
    : state.cards[sample.voice_id] || listVoice(sample.voice_id)
  if (store.player.url === sample.audio_url) {
    actions.togglePlay()
    return
  }
  actions.play({
    url: sample.audio_url,
    label: sample.file,
    sub: voice?.shown_name || '',
    voice,
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
  if (!sample) return
  const range = selectionOf(sample.key)
  if (!range) return
  previewRanges(sample, 'selection', [range])
}

/** @param {any} sample @param {string} mode @param {{start: number, end: number}[]} ranges */
function previewRanges(sample, mode, ranges) {
  if (!sample?.audio_url || !ranges.length) return
  if (
    state.preview === sample.key &&
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
  state.preview = sample.key
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
  if (!state.fileTarget && !allRows().length) {
    say('处理序列里还没有音频。', { kind: 'warn' })
    return
  }
  if (!isLocalVoice()) {
    say('平台音色的样例不在本机，换一个本地音色再处理。', { kind: 'warn' })
    return
  }
  if (!enabledSteps().length && !allRows().some((/** @type {any} */ row) => isTrimmed(row))) {
    say('没有要执行的步骤：选择处理步骤，或先在波形上裁剪。', { kind: 'warn' })
    return
  }
  stopPreview()
  state.busy = 'run'
  state.applied = null
  state.saved = null
  try {
    const scope = currentScope()
    // 只裁剪时不碰未改动的行：它们既没有模型步骤也没有收尾，处理一遍只会得到同一段音频。
    if (!enabledSteps().length && scope.kind === 'voices' && Array.isArray(scope.voices)) {
      scope.voices = scope.voices
        .map((group) => ({
          ...group,
          files: group.files.filter((file) => Boolean(group.trims[file])),
        }))
        .filter((group) => group.files.length)
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

function hasActiveModelJob() {
  return (state.data?.steps || []).some((/** @type {any} */ step) =>
    (step.models || []).some((/** @type {any} */ model) => {
      const jobState = String(model?.job?.state || '')
      return ['queued', 'running', 'cancelling', 'downloading', 'loading'].includes(jobState)
    })
  )
}

function startModelPolling() {
  if (modelTimer) return
  modelTimer = window.setInterval(pollModels, 1200)
}

function stopModelPolling() {
  if (!modelTimer) return
  window.clearInterval(modelTimer)
  modelTimer = 0
}

async function pollModels() {
  await refresh()
  if (!hasActiveModelJob()) stopModelPolling()
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

/** 停掉播放条上属于这一轮任务的那条音频：任务清掉后它的地址就不再有效。 */
/** @param {any} job */
function stopJobAudio(job) {
  const url = String(store.player.url || '')
  if (!job?.id || !url.includes(`job_id=${job.id}`)) return
  actions.stop()
}

/** 当前这一轮里落在处理序列上的条目（替换与入库都只认这些）。 */
/** @param {any} job */
function queuedItems(job) {
  const keys = new Set(allRows().map((row) => row.key))
  return (job?.items || []).filter((/** @type {any} */ row) =>
    keys.has(keyOf(row.voice_id, row.file))
  )
}

/** 替换序列里可替换的完成条目。 */
export async function applySelected() {
  const job = jobForTarget()
  if (!job || state.busy || job.scope?.kind === 'file') return
  const picked = queuedItems(job).filter(
    (/** @type {any} */ row) => row.state === 'done' && row.replaceable
  )
  if (!picked.length) {
    say('没有可替换的条目。', { kind: 'warn' })
    return
  }
  stopJobAudio(job)
  state.busy = 'apply'
  try {
    const payload = await api.apply(
      job.id,
      picked.map((/** @type {any} */ row) => row.index)
    )
    state.applied = payload
    state.saved = null
    state.job = payload.job
    const voiceIds = [...new Set(picked.map((/** @type {any} */ row) => row.voice_id))]
    const appliedFiles = new Set(`${picked.map((/** @type {any} */ row) => row.file).join('\n')}`.split('\n'))
    for (const voiceId of voiceIds) {
      for (const sample of state.cards[voiceId]?.samples || []) {
        if (appliedFiles.has(sample.file)) invalidateWaveform(sample.audio_url)
      }
    }
    state.waveRevision += 1
    state.trims = {}
    state.history = {}
    state.selections = {}
    await Promise.all([
      refresh(),
      ...voiceIds.map((voiceId) => ensureVoice(voiceId, true)),
      ...voiceIds.map((voiceId) => actions.reloadVoice(voiceId)),
    ])
    state.applied = payload
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** 把完成条目的处理结果入库为新样例；原样例与它的裁剪记录都不动。 */
export async function saveSelected() {
  const job = jobForTarget()
  if (!job || state.busy || job.scope?.kind === 'file') return
  // 时长偏差的条目不能替换，但入库是新样例，不受这条限制。
  const picked = queuedItems(job).filter((/** @type {any} */ row) => row.state === 'done')
  if (!picked.length) {
    say('没有可入库的条目。', { kind: 'warn' })
    return
  }
  stopJobAudio(job)
  state.busy = 'save'
  try {
    const payload = await api.save(
      job.id,
      picked.map((/** @type {any} */ row) => row.index)
    )
    state.saved = payload
    state.applied = null
    state.job = payload.job
    const voiceIds = [...new Set(picked.map((/** @type {any} */ row) => row.voice_id))]
    state.selections = {}
    await Promise.all([
      refresh(),
      ...voiceIds.map((voiceId) => ensureVoice(voiceId, true)),
      ...voiceIds.map((voiceId) => actions.reloadVoice(voiceId)),
    ])
    state.saved = payload
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** 不替换，回到可编辑状态：清掉后端当前任务，保留刚才那组裁剪。 */
export async function resetRun() {
  if (state.busy) return
  const job = state.job
  const applied = job?.state === 'applied'
  stopJobAudio(job)
  try {
    await api.clearJob()
  } catch (error) {
    fail(error)
    return
  }
  state.job = null
  state.applied = null
  state.saved = null
  stopPreview()
  state.selections = {}
  if (applied) {
    // 替换后的文件已经是新内容，旧区间不再对应当前时长：回到整段。
    state.trims = {}
    state.history = {}
  }
}

/** 清掉已结束的任务；运行中的任务不碰。 */
function clearFinishedJob() {
  if (!state.job || state.job.state === 'running') return
  state.job = null
  state.applied = null
  state.saved = null
  api.clearJob().catch(() => {})
}

/** 清掉当前目标：电脑文件连任务一起清；音色清空整个序列。 */
export async function clearTarget() {
  if (isRunning() || state.busy) return
  const hadJob = Boolean(state.job)
  stopJobAudio(state.job)
  state.fileTarget = null
  state.windows = []
  state.activeVoiceId = ''
  state.trims = {}
  state.selections = {}
  state.history = {}
  state.job = null
  state.applied = null
  state.saved = null
  state.preview = ''
  stopPreview()
  actions.clearPick()
  if (hadJob) {
    try {
      await api.clearJob()
    } catch {
      // 清理失败不影响已经回到空态。
    }
  }
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

/** 这一行的原声 / 处理结果是不是播放条上正在放的那一条。 */
/**
 * @param {number} index
 * @param {string} side
 */
export function playingSide(index, side) {
  const job = jobForTarget()
  if (!job?.id || !store.player.playing) return false
  return store.player.url === api.audioUrl(job.id, index, side)
}

/** 一行里 A/B 试听：借用任务目录的两份产物，走同一条播放条（不另起音轨）。 */
/**
 * @param {number} index
 * @param {string} side
 */
export function play(index, side) {
  const job = jobForTarget()
  if (!job?.id) return
  stopPreview()
  const item = (job.items || []).find((/** @type {any} */ row) => row.index === index) || null
  const voice =
    item && !String(item.voice_id || '').startsWith('file:')
      ? state.cards[item.voice_id] || listVoice(item.voice_id)
      : null
  actions.play({
    url: api.audioUrl(job.id, index, side),
    label: item?.file || '试听',
    sub: `${side === 'origin' ? '原声' : '处理结果'}${item?.voice ? ` · ${item.voice}` : ''}`,
    voice,
  })
}

export function reset() {
  stopPolling()
  stopModelPolling()
  pendingModel = null
  state.modelBusy = ''
  stopPreview()
}

/** @param {number} value */
function round(value) {
  return Math.round((Number(value) || 0) * 1000) / 1000
}