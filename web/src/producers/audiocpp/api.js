// @ts-check
// 插件自己的接口。宿主只提供生产方通用三件，其余端点都在这里。
import { request } from '../../api'

const base = '/api/v1/producers/audiocpp'

export const state = () => request(`${base}/state`)

// 地址由用户填：填了就用该服务（只读接入），清空则回到自管实例。
/** @param {string} url */
export const setEndpoint = (url) =>
  request(`${base}/endpoint`, { method: 'POST', body: { url } })

export const download = (runtime_id = '') =>
  request(`${base}/download`, { method: 'POST', body: { runtime_id } })

/** @param {string | number | null} port */
export const start = (port) =>
  request(`${base}/start`, { method: 'POST', body: { port } })

// 端口只有一份产地：引擎带来的行改引擎的服务地址，手加的行改插件清单。
/**
 * @param {string | number} port
 * @param {string | number} value
 */
export const setPort = (port, value) =>
  request(`${base}/port`, { method: 'POST', body: { port, value } })

/** @param {string | number | null} port */
export const stop = (port) =>
  request(`${base}/stop`, { method: 'POST', body: { port } })

/** @param {any} patch */
export const settings = (patch) =>
  request(`${base}/settings`, { method: 'POST', body: patch })

/** @param {string} id */
export const installModel = (id) =>
  request(`${base}/models/install`, { method: 'POST', body: { id } })

/** @param {string} id */
export const stopInstall = (id) =>
  request(`${base}/models/stop`, { method: 'POST', body: { id } })

/** @param {string} id */
export const deleteModel = (id) =>
  request(`${base}/models/delete`, { method: 'POST', body: { id } })

/** @param {string} id */
export const cleanModel = (id) =>
  request(`${base}/models/clean`, { method: 'POST', body: { id } })

/** @param {string} id */
export const loadModel = (id) =>
  request(`${base}/models/load`, { method: 'POST', body: { id } })

/** @param {string} id */
export const unloadModel = (id) =>
  request(`${base}/models/unload`, { method: 'POST', body: { id } })

export const refreshCatalog = () =>
  request(`${base}/catalog/refresh`, { method: 'POST' })

export const uninstall = () =>
  request(`${base}/uninstall`, { method: 'POST', body: { confirm: true } })
