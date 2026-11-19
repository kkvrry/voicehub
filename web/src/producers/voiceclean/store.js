// @ts-check
// 音色处理工作区的状态与动作。
//
// 处理目标不在这里存副本：它来自左栏的选中（`store.pick`，宿主那份），这里只负责
// 按它取回样例、按勾选的步骤跑任务、留下 A/B 产物。左栏点哪条、这里就处理哪条。
import { reactive } from 'vue'
import { actions, say, store } from '../../store'
import * as api from './api'

export const state = reactive({
  /** @type {any} */ data: null,
  loaded: false,
  error: '',
  busy: '',
  /** 选中音色的详情（带样例与时长）：跟着 `store.pick.voiceId` 取 */
  /** @type {any} */ target: null,
  targetLoading: false,
  /** @type {Record<string, { enabled: boolean, model: string }>} */ picks: {},
  /** @type {any} */ job: null,
  /** 正在试听的 ``index:side`` */
  playing: '',
  /** 正在安装的包 id */
  installing: '',
  /** @type {string[]} */ picked: [],
  /** @type {any} */ applied: null,
})

let timer = 0
let loading = false
let audio = null

/** @param {any} error */
function fail(error) {
  const text = error instanceof Error ? error.message : String(error)
  state.error = text
  say(text, { kind: 'error' })
}

/** 勾选状态：第一次见到某一步时用后端给的默认值，之后记住用户的选择。 */
function mergePicks(steps) {
  for (const step of steps || []) {
    if (state.picks[step.key]) {
      const known = state.picks[step.key].model
      if (!step.models.some((/** @type {any} */ row) => row.id === known)) {
        state.picks[step.key].model = step.models[0]?.id || ''
      }
      continue
    }
    state.picks[step.key] = {
      enabled: Boolean(step.default),
      model: step.models[0]?.id || '',
    }
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
    mergePicks(payload.steps)
    if (state.job?.state === 'running') startPolling()
  } catch (error) {
    fail(error)
  } finally {
    loading = false
  }
}

/** 目标音色在列表里的那一份（详情还没取回时用列表项，够画名字）。 */
export function targetVoice() {
  const id = store.pick.voiceId
  if (!id) return null
  if (state.target?.id === id) return state.target
  return store.voices.find((/** @type {any} */ row) => row.id === id) || null
}

/** 目标那一条样例（整条音色时取主样例）：试听与时长都看它。 */
export function targetSample() {
  const detail = state.target
  if (!detail || detail.id !== store.pick.voiceId) return null
  const samples = detail.samples || []
  const file = store.pick.file
  if (file) return samples.find((/** @type {any} */ row) => row.file === file) || null
  return samples.find((/** @type {any} */ row) => row.is_main) || samples[0] || null
}

/** 取回目标音色的详情。样例被替换 / 移除后文件已经不在时，落回整条音色。 */
export async function loadTarget() {
  const id = store.pick.voiceId
  if (!id) {
    state.target = null
    return
  }
  if (state.target?.id === id && !state.targetLoading) {
    // 已经取过：只补一次"选中的样例还在不在"，不重复拉整包。
    if (store.pick.file && !(state.target.samples || []).some((/** @type {any} */ row) => row.file === store.pick.file)) {
      store.pick.file = ''
    }
    return
  }
  state.targetLoading = true
  try {
    const detail = await api.voiceDetail(id)
    if (store.pick.voiceId !== id) return
    state.target = detail
    if (store.pick.file && !(detail.samples || []).some((/** @type {any} */ row) => row.file === store.pick.file)) {
      store.pick.file = ''
    }
  } catch (error) {
    // 换了一条又被清掉的音色：目标没了就回空态，不要把上一份详情留在界面上。
    if (store.pick.voiceId === id) state.target = null
    fail(error)
  } finally {
    state.targetLoading = false
  }
}

/** 标题下那一行：说到"哪一条音色、哪一条音频"就够，不写范围。 */
export function scopeLabel() {
  const voice = targetVoice()
  if (!voice) return '从左侧选一个音色'
  return store.pick.file ? `${voice.shown_name} · ${store.pick.file}` : `${voice.shown_name} · 整条音色`
}

/** 音频条副行：整条音色写条数与总时长，单条写时长。 */
export function targetSub() {
  const detail = state.target
  if (!detail || detail.id !== store.pick.voiceId) return state.targetLoading ? '读取样例…' : ''
  const samples = detail.samples || []
  if (!samples.length) return '没有样例'
  if (store.pick.file) {
    const sample = targetSample()
    return sample?.duration ? `${sample.duration.toFixed(1)} s` : '时长未知'
  }
  const total = samples.reduce((sum, /** @type {any} */ row) => sum + (row.duration || 0), 0)
  const head = `${samples.length} 条样例`
  return total ? `${head} · 共 ${Math.round(total)} s` : head
}

export function currentScope() {
  return { voice_id: store.pick.voiceId, file: store.pick.file || '' }
}

export function stepsPayload() {
  return (state.data?.steps || []).map((/** @type {any} */ step) => {
    const pick = state.picks[step.key] || { enabled: false, model: '' }
    return { key: step.key, enabled: pick.enabled, model: pick.model, options: {} }
  })
}

export async function start() {
  if (state.busy) return
  if (!store.pick.voiceId) {
    say('先从左侧选一个音色。', { kind: 'warn' })
    return
  }
  state.busy = 'run'
  state.applied = null
  state.picked = []
  try {
    const payload = await api.run(currentScope(), stepsPayload())
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
  timer = window.setInterval(poll, 1500)
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

/** 默认勾选所有可替换的完成条目。 */
export function selectAll() {
  const items = state.job?.items || []
  state.picked = items
    .filter((/** @type {any} */ row) => row.replaceable && row.state === 'done')
    .map((/** @type {any} */ row) => String(row.index))
}

/** @param {number} index */
export function togglePick(index) {
  const key = String(index)
  state.picked = state.picked.includes(key)
    ? state.picked.filter((/** @type {string} */ row) => row !== key)
    : [...state.picked, key]
}

export async function applyPicked() {
  if (!state.job?.id || !state.picked.length) return
  state.busy = 'apply'
  try {
    const payload = await api.apply(
      state.job.id,
      state.picked.map((/** @type {string} */ row) => Number(row)),
    )
    state.applied = payload
    state.job = payload.job
    state.picked = []
    // 换过的文件时长 / 处理链都变了：目标详情与音色列表一起刷新。
    state.target = null
    await Promise.all([refresh(), loadTarget(), actions.reloadVoice(store.pick.voiceId)])
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/** @param {string} id */
export async function installModel(id) {
  if (state.installing) return
  state.installing = id
  try {
    state.data = await api.install(id)
    mergePicks(state.data.steps)
    say('已开始下载模型。', { kind: 'info' })
  } catch (error) {
    fail(error)
  } finally {
    state.installing = ''
  }
}

/** 音频条上的试听：借壳上的播放器，与卡片列那颗 ▶ 是同一条路。 */
export function playTarget() {
  const sample = targetSample()
  const voice = targetVoice()
  if (!sample?.audio_url || !voice) return
  if (store.player.url === sample.audio_url) {
    actions.togglePlay()
    return
  }
  actions.play({
    url: sample.audio_url,
    label: sample.text || sample.file,
    sub: `${voice.shown_name}${sample.is_main ? ' · 主样例' : ''}`,
    voice,
  })
}

/** 换样例：复用宿主那张详情（与生成页「选样例」同一条路），在里面点「使用」。 */
export function changeSample() {
  const voice = targetVoice()
  if (voice) void actions.openDetail(voice)
}

export function clearTarget() {
  actions.clearPick()
}

/**
 * @param {number} index
 * @param {string} side
 */
export function play(index, side) {
  if (!state.job?.id) return
  const key = `${index}:${side}`
  if (audio) {
    audio.pause()
    audio = null
  }
  if (state.playing === key) {
    state.playing = ''
    return
  }
  const element = new Audio(api.audioUrl(state.job.id, index, side))
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
  if (audio) audio.pause()
  audio = null
  state.playing = ''
}
