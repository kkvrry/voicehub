// 配音工作区的状态与动作。宿主只保留「当前打开哪一家生产方」。
import { computed, reactive } from 'vue'
import { api as hostApi } from '../../api'
import { store as hostStore } from '../../store'
import { say } from '../../store/shell'
import * as api from './api'

const LAST_KEY = 'vh.dubbing.project'
const PANEL_KEY = 'vh.dubbing.panel'
const PANEL_MAX_KEY = 'vh.dubbing.panel-max'

function readNumber(key) {
  const value = Number(window.localStorage.getItem(key) || 0)
  return Number.isFinite(value) && value > 0 ? value : 0
}

export const state = reactive(/** @type {any} */ ({
  loaded: false,
  projects: [],
  storage: '',
  asr: { ready: false, note: '' },
  currentId: '',
  detail: null,
  /** 下区视图：时间轴 / 字幕，同一块区域二选一。默认字幕——配音的日常动作是改字与听音。 */
  view: 'subtitle',
  /** 预览上的字幕叠加：off | text | role。 */
  overlay: 'role',
  /** 下区高度（px）。0 = 按窗口比例算一个默认值，拖动分隔条后是用户定的像素。 */
  panelHeight: readNumber(PANEL_KEY),
  /** 下区占满整列：改字幕时把预览整块收起来。 */
  panelMax: window.localStorage.getItem(PANEL_MAX_KEY) === '1',
  selectedCue: '',
  /** 多选：字幕表里勾中的段；批量改角色 / 配音 / 删除都读这一份。 */
  selectedIds: [],
  /** 上一次勾选的位置：Shift 连选取它到当前行。 */
  anchorId: '',
  playTime: 0,
  playing: false,
  /** 让预览媒体跳到某个时间点：`n` 递增，组件只在变化时执行一次。 */
  seek: { at: 0, n: 0 },
  /** 试听某一段原声：从 `start` 播到 `end` 就停（`n` 递增触发一次）。 */
  audition: { start: 0, end: 0, n: 0 },
  /** 让预览停下来（逐段音频要独占播放，不能和原声一起响）。 */
  pause: { n: 0 },
  uploading: false,
  /** 正在等本机文件选择框 / 引用导入：与上传分开，按钮上说清在做哪一件事。 */
  linking: false,
  /** 分轨步骤里选中的模型 id：换项目后由界面按可用清单重置。 */
  separationModel: '',
  busy: '',
  error: '',
  message: '',
  /** 正在编辑的段 id（空 = 没人在打字）：轮询回来时只放行其他段。 */
  editing: '',
  /** 没写进库的段 id：行上标「未保存」，后台还在重试。 */
  pending: {},
}))

let pollTimer = 0
let pollToken = 0
/** 本地写库的次数与在飞数量：轮询快照比它旧就不能落地，否则会擦掉刚改的字。 */
let writeRev = 0
let writesInFlight = 0

/** 没写进库的改动：`cueId -> { projectId, patch }`。留着退避重试，界面照旧显示它。 */
const pendingWrites = new Map()
let retryTimer = 0
let retryDelay = 1500

/** 包一次写库：进场和落地都算一次版本变化。 */
async function mutate(action) {
  writesInFlight += 1
  writeRev += 1
  try {
    return await action()
  } finally {
    writesInFlight -= 1
    writeRev += 1
  }
}

/** 把没写进去的改动盖在服务端数据上（只盖当前项目的那几条）。 */
function withPending(list) {
  if (!pendingWrites.size) return list
  return list.map((cue) => {
    const item = pendingWrites.get(cue.id)
    return item && item.projectId === state.currentId ? { ...cue, ...item.patch } : cue
  })
}

function forgetWrite(cueId) {
  pendingWrites.delete(cueId)
  delete state.pending[cueId]
}

function armRetry() {
  if (retryTimer) return
  retryTimer = window.setTimeout(() => {
    retryTimer = 0
    void flushPending()
  }, retryDelay)
}

/** 重试所有没写进去的改动；还失败就翻倍延时再来，成功后回到 1.5 秒。 */
async function flushPending() {
  if (!pendingWrites.size) {
    retryDelay = 1500
    return
  }
  for (const [cueId, item] of [...pendingWrites]) {
    if (item.projectId !== state.currentId) continue
    try {
      const updated = await mutate(() => api.updateCue(item.projectId, cueId, item.patch))
      forgetWrite(cueId)
      const list = state.detail?.cues || []
      const at = list.findIndex((cue) => cue.id === cueId)
      if (at >= 0) list[at] = updated
    } catch (error) {
      // 请求本身不被接受（段落已删）：留着也没用，别再重试。
      if (error?.status && error.status < 500) forgetWrite(cueId)
    }
  }
  if (pendingWrites.size) {
    retryDelay = Math.min(15000, Math.round(retryDelay * 2))
    armRetry()
  } else {
    retryDelay = 1500
  }
}

export const project = computed(() => state.detail?.project || null)
export const cues = computed(() => state.detail?.cues || [])
export const roles = computed(() => state.detail?.roles || [])
export const steps = computed(() => state.detail?.steps || [])
export const job = computed(() => state.detail?.job || null)
export const mixReady = computed(() => Boolean(state.detail?.paths?.mix))

export function roleOf(cue) {
  const list = roles.value
  if (!list.length) return null
  return list.find((role) => role.id === cue.role_id) || list[0]
}

export function roleName(cue) {
  return roleOf(cue)?.name || '旁白'
}

export function selectedCue() {
  return cues.value.find((cue) => cue.id === state.selectedCue) || null
}

const pickedCues = computed(() =>
  cues.value.filter((cue) => state.selectedIds.includes(cue.id))
)

export function clearPicks() {
  state.selectedIds = []
  state.anchorId = ''
}

export function togglePick(id) {
  state.selectedIds = state.selectedIds.includes(id)
    ? state.selectedIds.filter((item) => item !== id)
    : [...state.selectedIds, id]
  state.anchorId = id
}

/** Shift 连选：范围由表格按当前筛选结果给出，这里只负责替换这段区间。 */
export function pickRange(ids) {
  const keep = state.selectedIds.filter((id) => !ids.includes(id))
  state.selectedIds = [...new Set([...keep, ...ids])]
  state.anchorId = ids[ids.length - 1] || state.anchorId
}

export function setPicks(ids) {
  state.selectedIds = [...new Set(ids)]
  state.anchorId = ids[ids.length - 1] || ''
}

/** 选中段对应的角色：左栏「指定」先落到这个角色。 */
export const activeRoleId = computed(() => {
  const cue = selectedCue()
  const list = roles.value
  if (!list.length) return ''
  if (cue) return cue.role_id || list[0].id
  const picked = pickedCues.value[0]
  return picked?.role_id || list[0].id
})

/** 有旧音频但文本/音色已改、或干脆没配音的段：导出的成片会和字幕对不上。 */
export const exportBlockers = computed(() => {
  const stats = state.detail?.stats || {}
  return {
    stale: Number(stats.stale || 0),
    failed: Number(stats.failed || 0),
    pending: Number(stats.pending || 0),
    unvoiced: Number(stats.unvoiced || 0),
  }
})

let errorTimer = 0
/** 这一次由界面发起的任务：跑完要把结果说一声（尤其是"有 N 段失败"）。 */
let watchingJob = ''

/** 任务落定：失败或部分失败时给出原因。以前只在行上写"失败"，用户不知道是引擎没起。 */
function reportJob(job) {
  if (!job || !job.id || job.id !== watchingJob || job.state === 'running') return
  watchingJob = ''
  void loadOverview()
  if (job.state === 'failed') {
    fail(new Error(job.message || '任务没有完成。'))
    return
  }
  if (job.state === 'cancelled') {
    say('已取消。', { kind: 'warn' })
    return
  }
  if (job.state === 'interrupted') {
    say(job.message || '任务已中断。', { kind: 'warn' })
    return
  }
  if (job.kind === 'assemble' || job.kind === 'export' || job.kind === 'separate') {
    if (job.message) {
      say(job.message, {
        kind: job.kind === 'assemble' && job.message.includes('截断') ? 'warn' : 'success',
      })
    }
    return
  }
  if (job.failed) {
    say(job.message || `有 ${job.failed} 段没有成功。`, { kind: 'warn' })
  }
}

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

/** 试听一段原声：预览从这一段开头放到结尾。 */
export function auditionCue(cue) {
  if (!cue) return
  const start = cue.start_ms / 1000
  const end = cue.end_ms / 1000
  state.playTime = start
  state.seek = { at: start, n: state.seek.n + 1 }
  state.audition = { start, end, n: state.audition.n + 1 }
}

/** 让预览停下：逐段配音要独占声音。 */
export function pausePreview() {
  state.pause = { n: state.pause.n + 1 }
}

/** 下区高度：抽屉式地用一次记一次，下次打开还是这个高度。 */
export function setPanelHeight(pixels) {
  const value = Math.max(0, Math.round(Number(pixels) || 0))
  state.panelHeight = value
  if (value > 0) window.localStorage.setItem(PANEL_KEY, String(value))
  else window.localStorage.removeItem(PANEL_KEY)
}

export function togglePanelMax() {
  state.panelMax = !state.panelMax
  window.localStorage.setItem(PANEL_MAX_KEY, state.panelMax ? '1' : '0')
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

/** 打开插件时调用：恢复上次的项目，没有就保持空态。

    每次都重拉一遍项目列表：别的标签页删了项目、或上次退在导入中途，
    列表与磁盘可能已经对不上。
 */
export async function open() {
  hostStore.assignTarget = '角色'
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
  clearPicks()
  state.playTime = 0
  hostStore.assignTarget = '角色'
  if (!id) {
    window.localStorage.removeItem(LAST_KEY)
    return
  }
  window.localStorage.setItem(LAST_KEY, id)
  await refresh()
}

export async function refresh({ silent = false } = {}) {
  const id = state.currentId
  if (!id) return null
  const rev = writeRev
  try {
    const payload = await api.detail(id)
    // 快照比本地旧：请求发出后这边改过库（或在飞），落地就会把用户刚敲的字擦掉。
    // 丢掉这份，等下一轮——写库落地后 writeRev 又变了，下一轮必然拿到新数据。
    if (quietStale(rev)) return null
    // 没写进去的那几段盖在服务端数据上：重试成功前，界面不能先"变回原样"。
    const fresh = withPending(payload.cues)
    if (state.editing) {
      // 只保住正在改的那一段，其余照常更新：以前整表冻结，合成进度在编辑时全停。
      const local = new Map((state.detail?.cues || []).map((cue) => [cue.id, cue]))
      state.detail = {
        ...payload,
        cues: fresh.map((cue) =>
          cue.id === state.editing ? local.get(cue.id) || cue : cue
        ),
      }
    } else {
      state.detail = { ...payload, cues: fresh }
    }
    state.error = ''
    reportJob(payload.job)
    if (state.selectedCue && !payload.cues.some((cue) => cue.id === state.selectedCue)) {
      state.selectedCue = ''
    }
    // 段落被删掉后多选里会留下不存在的 id：留给批量操作只会报"找不到这一段"。
    const ids = new Set(payload.cues.map((cue) => cue.id))
    state.selectedIds = state.selectedIds.filter((id) => ids.has(id))
    if (state.anchorId && !ids.has(state.anchorId)) state.anchorId = ''
    if (payload.job && payload.job.state === 'running') schedulePoll()
    return payload
  } catch (error) {
    if (!silent) fail(error)
    return null
  }
}

/** 这份快照能不能落地：在飞时不能，版本变了也不能（变了就再拉一轮）。 */
function quietStale(rev) {
  if (!writesInFlight && rev === writeRev) return false
  schedulePoll()
  return true
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
    clearPicks()
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

/** 引用本机文件建项目：先由宿主问路径，再让后端登记；源文件不复制进项目。 */
export async function linkLocalProject() {
  if (state.linking) return null
  state.linking = true
  try {
    const picked = await api.pickSource()
    const path = String(picked?.path || '')
    if (!path) return null
    const payload = await api.link(path, '')
    await loadOverview()
    state.detail = payload
    state.currentId = payload.project.id
    clearPicks()
    window.localStorage.setItem(LAST_KEY, state.currentId)
    state.selectedCue = payload.cues?.[0]?.id || ''
    say(`已引用「${payload.project.source_name}」，原片留在原处。`, { kind: 'success' })
    return payload
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.linking = false
  }
}

/** 引用的源文件被移动、改名后重新指定；后端校验时长与音轨。 */
export async function relinkSource() {
  if (!state.currentId || state.linking) return null
  state.linking = true
  try {
    const picked = await api.pickSource()
    const path = String(picked?.path || '')
    if (!path) return null
    state.detail = await api.relink(state.currentId, path)
    await loadOverview()
    say('已重新指定源文件；混音需要重新贴齐。', { kind: 'success' })
    return state.detail
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.linking = false
  }
}

export async function importSubtitles(file) {
  if (!state.currentId || !file) return null
  state.uploading = true
  try {
    const payload = await api.importSubtitles(state.currentId, file)
    state.detail = payload
    clearPicks()
    state.selectedCue = payload.cues?.[0]?.id || ''
    await loadOverview()
    say(`已导入字幕：${payload.stats?.total || payload.cues?.length || 0} 段。`, {
      kind: 'success',
    })
    return payload
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.uploading = false
  }
}

export async function renameProject(name) {
  if (!state.currentId) return null
  try {
    state.detail = await mutate(() => api.rename(state.currentId, name))
    await loadOverview()
    return state.detail
  } catch (error) {
    fail(error)
    return null
  }
}

export async function removeProject() {
  const id = state.currentId
  if (!id) return
  try {
    await api.remove(id)
    for (const [cueId, item] of [...pendingWrites]) {
      if (item.projectId === id) forgetWrite(cueId)
    }
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
    watchingJob = payload.id
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

/** 分轨：用户点一次就重跑一次，换模型后旧产物被覆盖。 */
export function separate(model = '') {
  return runJob(() => api.separate(state.currentId, model), 'separate')
}

export async function removeSeparation() {
  if (!state.currentId) return null
  try {
    state.detail = await mutate(() => api.removeSeparation(state.currentId))
    say('已移除分轨；混音回到整条原声。', { kind: 'success' })
    return state.detail
  } catch (error) {
    fail(error)
    return null
  }
}

export function synthAll() {
  return runJob(() => api.synth(state.currentId), 'synth')
}

export function synthOne(cueId) {
  // 用户点名「重配这一段」：同一个键也重新请求引擎，不拿旧产物糊弄。
  return runJob(() => api.synth(state.currentId, [cueId], true), 'synth')
}

export function assemble() {
  return runJob(() => api.assemble(state.currentId), 'assemble')
}

export function exportFilm(allowIncomplete = false) {
  return runJob(() => api.exportFilm(state.currentId, allowIncomplete), 'export')
}

/** 批量改角色：角色的「配音 / 原声」处理方式决定这些段要不要重配。 */
export async function applyRoleToSelection(roleId) {
  const ids = [...state.selectedIds]
  if (!ids.length) return null
  try {
    const payload = await mutate(() => api.batchCues(state.currentId, {
      action: 'role',
      cue_ids: ids,
      role_id: roleId || '',
    }))
    if (state.detail) {
      state.detail.cues = payload.data
      state.detail.stats = payload.stats
    }
    const role = roles.value.find((item) => item.id === roleId) || roles.value[0]
    say(`已把 ${ids.length} 段改为「${role?.name || '旁白'}」。`, { kind: 'success' })
    void refresh({ silent: true })
    return payload
  } catch (error) {
    fail(error)
    return null
  }
}

/** 只重配选中的段：保留原声的段与缺音色的段由后端跳过。 */
export async function synthSelection() {
  const ids = [...state.selectedIds]
  if (!ids.length) return null
  return runJob(() => api.synth(state.currentId, ids, true), 'synth')
}

export async function deleteSelection() {
  const ids = [...state.selectedIds]
  if (!ids.length) return null
  try {
    const payload = await mutate(() => api.batchCues(state.currentId, {
      action: 'delete',
      cue_ids: ids,
    }))
    if (state.detail) {
      state.detail.cues = payload.data
      state.detail.stats = payload.stats
    }
    const first = state.selectedCue
    clearPicks()
    if (first && !payload.data.some((cue) => cue.id === first)) state.selectedCue = ''
    say(`已删除 ${ids.length} 段。`, { kind: 'success' })
    return payload
  } catch (error) {
    fail(error)
    return null
  }
}

export async function patchCue(cue, patch) {
  const list = state.detail?.cues || []
  const index = list.findIndex((item) => item.id === cue.id)
  const before = index >= 0 ? { ...list[index] } : null
  // 先上屏：写库这几十毫秒里，用户看到的也该是自己刚敲的那一句。
  if (index >= 0) list[index] = { ...list[index], ...patch }
  try {
    const updated = await mutate(() => api.updateCue(state.currentId, cue.id, patch))
    forgetWrite(cue.id)
    const at = (state.detail?.cues || []).findIndex((item) => item.id === cue.id)
    if (at >= 0) state.detail.cues[at] = updated
    return updated
  } catch (error) {
    // 请求本身不被接受（段落已删、参数不合法）：退回原样，别把界面留在假状态。
    if (error?.status && error.status < 500) {
      if (before) {
        const at = (state.detail?.cues || []).findIndex((item) => item.id === cue.id)
        if (at >= 0) state.detail.cues[at] = before
      }
      fail(error)
      return null
    }
    // 5xx / 服务重启 / 断网：这次改动不能丢。留在界面上，后台退避重试，
    // 行上显示"未保存"——以前它的表现就是"改完自己变回去了"。
    // 同一段先后改了两格（先改字、再换角色）时按补丁合并，后一次不能把前一次盖掉。
    const queued = pendingWrites.get(cue.id)
    pendingWrites.set(cue.id, {
      projectId: state.currentId,
      patch: { ...(queued?.patch || {}), ...patch },
    })
    state.pending[cue.id] = true
    fail(error)
    armRetry()
    return null
  }
}

export async function patchRole(role, patch) {
  try {
    const updated = await mutate(() => api.updateRole(state.currentId, role.id, patch))
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
    const role = await mutate(() => api.addRole(state.currentId, { name }))
    state.detail?.roles.push(role)
    return role
  } catch (error) {
    fail(error)
    return null
  }
}

export async function removeRole(role) {
  try {
    await mutate(() => api.deleteRole(state.currentId, role.id))
    await refresh()
  } catch (error) {
    fail(error)
  }
}

export async function splitCue(cue, atMs) {
  try {
    const payload = await mutate(() => api.splitCue(state.currentId, cue.id, atMs))
    if (state.detail) state.detail.cues = payload.data
    await refresh({ silent: true })
    return payload.data
  } catch (error) {
    fail(error)
    return null
  }
}

export async function mergeCue(cue) {
  try {
    const payload = await mutate(() => api.mergeCue(state.currentId, cue.id))
    if (state.detail) state.detail.cues = payload.data
    await refresh({ silent: true })
    return payload.data
  } catch (error) {
    fail(error)
    return null
  }
}

export async function removeCue(cue) {
  try {
    const payload = await mutate(() => api.deleteCue(state.currentId, cue.id))
    if (state.detail) state.detail.cues = payload.data
    if (state.selectedCue === cue.id) state.selectedCue = ''
    state.selectedIds = state.selectedIds.filter((id) => id !== cue.id)
  } catch (error) {
    fail(error)
  }
}

/** 轨道音量：`dub_gain` / `original_gain` / `accompaniment_gain`，−60 dB 为静音。 */
export async function saveTrackGain(key, value) {
  try {
    state.detail = await mutate(() =>
      api.saveOptions(state.currentId, { [key]: Number(value) })
    )
  } catch (error) {
    fail(error)
  }
}
