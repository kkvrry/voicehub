// @ts-check
// audio.cpp 工作区的状态与动作。宿主只保留「当前打开哪一家生产方」。
import { reactive } from 'vue'
import { say } from '../../store/shell'
import * as api from './api'

export const state = reactive({
  /** @type {any} */ data: null,
  loaded: false,
  busy: '',
  error: '',
  endpointDraft: '',
  endpointDirty: false,
  exeDraft: '',
  exeDirty: false,
  rootDraft: '',
  rootDirty: false,
  keyword: '',
  category: 'all',
  showLog: false,
  logPort: 0,
  confirmUninstall: false,
  confirmDelete: '',
})

/** 轮询与动作会同时写 data：只让最新一次请求落地，避免保存结果被在途轮询覆盖。 */
let seq = 0
let applied = 0
let misses = 0

/** @param {any} error */
function fail(error) {
  const text = error instanceof Error ? error.message : String(error)
  return text === 'Not Found' ? '插件接口暂不可用，正在重试。' : text
}

export async function refresh() {
  const mine = ++seq
  try {
    const payload = await api.state()
    if (mine < applied || state.busy) return
    applied = mine
    misses = 0
    state.data = payload
    state.loaded = true
    state.error = ''
    if (!state.endpointDirty) state.endpointDraft = payload.endpoint || ''
    if (!state.exeDirty) state.exeDraft = payload.runtime?.custom_exe || ''
    if (!state.rootDirty) {
      state.rootDraft = payload.runtime?.models_root_custom ? payload.runtime.models_root : ''
    }
    const rows = payload.instances || []
    if (
      rows.length &&
      !rows.some(
        /** @param {any} row */
        (row) => row.port === state.logPort
      )
    ) {
      state.logPort = rows[0].port
    }
  } catch (error) {
    if (mine < applied || state.busy) return
    // 首次打开偶发 404 / 断连：下一轮轮询会自愈，连错三次才报。
    misses += 1
    if (misses >= 3) state.error = fail(error)
  }
}

/**
 * @param {string} key
 * @param {() => Promise<any>} call
 */
async function act(key, call) {
  state.busy = key
  try {
    const payload = await call()
    applied = ++seq
    state.data = payload
    state.error = ''
    return true
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error)
    state.error = text
    say(text, { kind: 'error' })
    return false
  } finally {
    state.busy = ''
  }
}

export const open = () => refresh()

/** @param {string} url */
export async function setEndpoint(url) {
  const ok = await act('endpoint', () => api.setEndpoint(String(url || '').trim()))
  if (ok) {
    state.endpointDirty = false
    state.endpointDraft = state.data?.endpoint || ''
  }
  return ok
}

/**
 * 可执行文件：空串 = 回到内置运行包。路径不存在由后端拒绝，界面回滚由调用方处理。
 *
 * @param {string} path
 */
export async function saveExe(path) {
  const ok = await act('exe', () => api.settings({ exe: String(path || '').trim() }))
  if (ok) {
    state.exeDirty = false
    state.exeDraft = state.data?.runtime?.custom_exe || ''
  }
  return ok
}

/**
 * 模型目录：空串 = 插件自己的 models/。
 *
 * @param {string} path
 */
export async function saveModelsRoot(path) {
  const ok = await act('root', () => api.settings({ models_root: String(path || '').trim() }))
  if (ok) {
    state.rootDirty = false
    state.rootDraft = state.data?.runtime?.models_root_custom ? state.data.runtime.models_root : ''
  }
  return ok
}

export const start = (port = null) => act(`start:${port}`, () => api.start(port))
export const stop = (port = null) => act(`stop:${port}`, () => api.stop(port))

/**
 * 端口即时保存：引擎带来的行会写回引擎地址，界面显示的就是已落盘的值。
 * @param {any} row
 * @param {number} port
 */
export async function savePort(row, port) {
  if (row.port === port) return true
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    say('端口须为 1–65535 的整数。', { kind: 'error' })
    return false
  }
  const rows = state.data?.instances || []
  if (
    rows.some(
      /** @param {any} item */
      (item) => item.port !== row.port && item.port === port
    )
  ) {
    say(`端口 ${port} 已被其他实例占用。`, { kind: 'error' })
    return false
  }
  const ok = await act('port', () => api.setPort(row.port, port))
  if (ok) {
    say(
      row.source === 'engine'
        ? `已把引擎「${row.label}」的服务地址改为 :${port}，重新启动后生效。`
        : `:${port} 已保存，重新启动后生效。`,
      { kind: 'success' }
    )
  }
  return ok
}

/**
 * 自动拉起是多选：先落界面再提交整张清单（保存接口会顺带扫模型，等它回来才勾会像卡住）。
 * @param {string[]} models
 */
export async function saveAutoStart(models) {
  const previous = [...(state.data?.auto_start || [])]
  if (state.data) state.data.auto_start = [...models]
  const ok = await act('auto', () => api.settings({ auto_start: models }))
  if (!ok && state.data) state.data.auto_start = previous
  return ok
}

/** @param {string} id */
export const setMirror = (id) => act('mirror', () => api.settings({ mirror: id }))

/** @param {string} id */
export const setRuntime = (id) => act('runtime', () => api.settings({ runtime_id: id }))

export const downloadRuntime = () =>
  act('download', () => api.download(state.data?.runtime?.runtime_id || ''))

/** @param {string} id */
export const install = (id) => act(`install:${id}`, () => api.installModel(id))
/** @param {string} id */
export const cancelInstall = (id) => act(`cancel:${id}`, () => api.stopInstall(id))
/** @param {string} id */
export const loadModel = (id) => act(`load:${id}`, () => api.loadModel(id))
/** @param {string} id */
export const unloadModel = (id) => act(`unload:${id}`, () => api.unloadModel(id))

/** @param {string} id */
export async function removePackage(id) {
  const ok = await act(`delete:${id}`, () => api.deleteModel(id))
  if (ok) state.confirmDelete = ''
  return ok
}

/** @param {string} id */
export const cleanPackage = (id) => act(`clean:${id}`, () => api.cleanModel(id))

export const refreshCatalog = () => act('catalog', () => api.refreshCatalog())

export function uninstall() {
  return act('uninstall', () => api.uninstall()).then((ok) => {
    state.confirmUninstall = false
    return ok
  })
}
