// 配音工作区的状态与动作。宿主只保留「当前打开哪一家生产方」。
import { computed, reactive } from 'vue'
import { api as hostApi } from '../../api'
import { say } from '../../store/shell'
import * as api from './api'

const LAST_KEY = 'vh.dubbing.project'

export const state = reactive({
  loaded: false,
  projects: [],
  storage: '',
  asr: { ready: false, note: '' },
  currentId: '',
  detail: null,
  voices: [],
  /** 下区视图：时间轴 / 字幕，同一块区域二选一。 */
  view: 'timeline',
  /** 预览上的字幕叠加：off | text | role。 */
  overlay: 'role',
  selectedCue: '',
  playTime: 0,
  playing: false,
  /** 让预览媒体跳到某个时间点：`n` 递增，组件只在变化时执行一次。 */
  seek: { at: 0, n: 0 },
  uploading: false,
  exporting: false,
  busy: '',
  error: '',
  message: '',
  /** 正在输入的段落数：轮询回来时不覆盖用户正在改的那一行。 */
  editing: 0,
})

let pollTimer = 0
let pollToken = 0

export const project = computed(() => state.detail?.project || null)
export const cues = computed(() => state.detail?.cues || [])
export const roles = computed(() => state.detail?.roles || [])
export const steps = computed(() => state.detail?.steps || [])
export const job = computed(() => state.detail?.job || null)
export const mixReady = computed(() => Boolean(state.detail?.paths?.mix))
export const exportReady = computed(() => (state.detail?.paths?.exports || []).length > 0)

export function roleOf(cue) {
  const list = roles.value
  if (!list.length) return null
  return list.find((role) => role.id === cue.role_id) || list[0]
}

export function roleName(cue) {
  return roleOf(cue)?.name || '旁白'
}

export function roleColor(cue) {
  return roleOf(cue)?.color || 'var(--sh-accent)'
}

export function selectedCue() {
  return cues.value.find((cue) => cue.id === state.selectedCue) || null
}

let errorTimer = 0

function fail(error) {
  const text = error instanceof Error ? error.message : String(error)
  state.error = text
  // 页内底栏是唯一提示：同一句话再弹一次 toast 只是把屏幕占满两次。
  // 日志仍要留一条，事后排查看得到。
  void hostApi.clientLog(text, 'error')
  // 底栏那句是"这一次没成功"的说明，不是常驻状态：留八秒够读完。
  if (errorTimer) window.clearTimeout(errorTimer)
  errorTimer = window.setTimeout(() => {
    if (state.error === text) state.error = ''
  }, 8000)
  return text
}

/** 把播放头移到某处：预览媒体与段落表共用同一个入口。 */
export function seekTo(seconds) {
  const at = Math.max(0, Number(seconds) || 0)
  state.playTime = at
  state.seek = { at, n: state.seek.n + 1 }
}

export async function loadOverview() {
  try {
    const payload = await api.overview()
    state.projects = payload.projects || []
    state.storage = payload.storage || ''
    state.asr = payload.asr || { ready: false, note: '' }
    state.loaded = true
    state.error = ''
    return payload
  } catch (error) {
    fail(error)
    return null
  }
}

export async function loadVoices() {
  if (state.voices.length) return state.voices
  try {
    const payload = await api.voices()
    state.voices = (payload.data || []).filter((voice) => (voice.engines || []).length)
  } catch {
    // 音色库读不到不该让整个工作区停住：角色仍可保留原声。
    state.voices = []
  }
  return state.voices
}

/** 打开插件时调用：恢复上次的项目，没有就保持空态。

    每次都重拉一遍项目列表：别的标签页删了项目、或上次退在导入中途，
    列表与磁盘可能已经对不上。
 */
export async function open() {
  await loadOverview()
  if (state.currentId) {
    await refresh({ silent: true })
    return
  }
  const remembered = window.localStorage.getItem(LAST_KEY) || ''
  const exists = state.projects.some((item) => item.id === remembered)
  if (exists) await select(remembered)
}

export async function select(id) {
  stopPolling()
  state.currentId = id || ''
  state.detail = null
  state.selectedCue = ''
  state.playTime = 0
  if (!id) {
    window.localStorage.removeItem(LAST_KEY)
    return
  }
  window.localStorage.setItem(LAST_KEY, id)
  await refresh()
  void loadVoices()
}

export async function refresh({ silent = false } = {}) {
  const id = state.currentId
  if (!id) return null
  try {
    const payload = await api.detail(id)
    // 用户正在改的那一行不能被轮询覆盖：整表替换会吞掉输入。
    if (!silent || !state.editing) state.detail = payload
    else state.detail = { ...payload, cues: state.detail?.cues || payload.cues }
    state.error = ''
    if (state.selectedCue && !payload.cues.some((cue) => cue.id === state.selectedCue)) {
      state.selectedCue = ''
    }
    if (payload.job && payload.job.state === 'running') schedulePoll()
    return payload
  } catch (error) {
    if (!silent) fail(error)
    return null
  }
}

function stopPolling() {
  pollToken += 1
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = 0
}

function schedulePoll() {
  const token = ++pollToken
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(async () => {
    if (token !== pollToken) return
    await refresh({ silent: true })
  }, 1000)
}

export async function uploadFile(file) {
  state.uploading = true
  try {
    const payload = await api.upload(file, file.name.replace(/\.[^.]+$/, ''))
    await loadOverview()
    state.detail = payload
    state.currentId = payload.project.id
    window.localStorage.setItem(LAST_KEY, state.currentId)
    state.selectedCue = payload.cues?.[0]?.id || ''
    say(`已导入「${payload.project.name}」。`, { kind: 'success' })
    return payload
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.uploading = false
  }
}

export async function removeProject() {
  const id = state.currentId
  if (!id) return
  try {
    await api.remove(id)
    stopPolling()
    state.detail = null
    state.currentId = ''
    window.localStorage.removeItem(LAST_KEY)
    await loadOverview()
    say('项目已删除。', { kind: 'success' })
  } catch (error) {
    fail(error)
  }
}

async function runJob(action, label) {
  state.busy = label
  try {
    const payload = await action()
    state.detail = { ...(state.detail || {}), job: payload }
    schedulePoll()
    return payload
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.busy = ''
  }
}

export function transcribe() {
  return runJob(() => api.transcribe(state.currentId), 'transcribe')
}

export function synthAll() {
  return runJob(() => api.synth(state.currentId), 'synth')
}

export function synthOne(cueId) {
  return runJob(() => api.synth(state.currentId, [cueId]), 'synth')
}

export async function assemble() {
  state.busy = 'assemble'
  try {
    const result = await api.assemble(state.currentId)
    await refresh()
    await loadOverview()
    const parts = [`贴齐 ${result.cues} 段`]
    if (result.delayed?.length) parts.push(`顺延 ${result.delayed.length}`)
    if (result.fitted?.length) parts.push(`变速 ${result.fitted.length}`)
    if (result.skipped) parts.push(`跳过 ${result.skipped} 段未合成`)
    say(parts.join(' · '), { kind: 'success' })
    return result
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.busy = ''
  }
}

export async function exportFilm() {
  state.exporting = true
  try {
    const result = await api.exportFilm(state.currentId)
    await refresh()
    say(`已导出 ${result.file}（${(result.size / 1024 / 1024).toFixed(1)} MB）。`, {
      kind: 'success',
    })
    return result
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.exporting = false
  }
}

export async function patchCue(cue, patch) {
  try {
    const updated = await api.updateCue(state.currentId, cue.id, patch)
    const list = state.detail?.cues || []
    const index = list.findIndex((item) => item.id === cue.id)
    if (index >= 0) list[index] = updated
    return updated
  } catch (error) {
    fail(error)
    return null
  }
}

export async function patchRole(role, patch) {
  try {
    const updated = await api.updateRole(state.currentId, role.id, patch)
    const list = state.detail?.roles || []
    const index = list.findIndex((item) => item.id === role.id)
    if (index >= 0) list[index] = updated
    // 换音色会把该角色名下的段标成过期：重拉一次状态，行上的标记才会变。
    await refresh({ silent: true })
    return updated
  } catch (error) {
    fail(error)
    return null
  }
}

export async function createRole(name) {
  try {
    const role = await api.addRole(state.currentId, { name })
    state.detail?.roles.push(role)
    return role
  } catch (error) {
    fail(error)
    return null
  }
}

export async function removeRole(role) {
  try {
    await api.deleteRole(state.currentId, role.id)
    await refresh()
  } catch (error) {
    fail(error)
  }
}

export async function splitCue(cue, atMs) {
  try {
    const payload = await api.splitCue(state.currentId, cue.id, atMs)
    if (state.detail) state.detail.cues = payload.data
    return payload.data
  } catch (error) {
    fail(error)
    return null
  }
}

export async function mergeCue(cue) {
  try {
    const payload = await api.mergeCue(state.currentId, cue.id)
    if (state.detail) state.detail.cues = payload.data
    return payload.data
  } catch (error) {
    fail(error)
    return null
  }
}

export async function removeCue(cue) {
  try {
    const payload = await api.deleteCue(state.currentId, cue.id)
    if (state.detail) state.detail.cues = payload.data
    if (state.selectedCue === cue.id) state.selectedCue = ''
  } catch (error) {
    fail(error)
  }
}

export async function saveOriginalGain(value) {
  try {
    state.detail = await api.saveOptions(state.currentId, { original_gain: value })
  } catch (error) {
    fail(error)
  }
}
