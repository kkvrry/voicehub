// @ts-check
// 插件自己的接口。宿主只提供生产方通用三件，其余端点都在这里。
import { request } from '../../api'

const base = '/api/v1/producers/voiceclean'

export const state = () => request(`${base}/state`)

/**
 * @param {any} scope
 * @param {any[]} steps
 */
export const run = (scope, steps) =>
  request(`${base}/run`, { method: 'POST', body: { scope, steps }, timeout: 60_000 })

export const job = () => request(`${base}/job`)

export const cancel = () => request(`${base}/job/cancel`, { method: 'POST' })

/**
 * @param {string} job_id
 * @param {number[]} indexes
 */
export const apply = (job_id, indexes) =>
  request(`${base}/job/apply`, { method: 'POST', body: { job_id, indexes }, timeout: 120_000 })

/** @param {string} id */
export const install = (id) =>
  request(`${base}/models/install`, { method: 'POST', body: { id }, timeout: 60_000 })

/** @param {string} id */
export const voiceDetail = (id) => request(`/api/v1/voices/${encodeURIComponent(id)}`)

/**
 * @param {string} job_id
 * @param {number} index
 * @param {string} side
 */
export function audioUrl(job_id, index, side) {
  const query = new URLSearchParams({ job_id, index: String(index), side })
  return `${base}/job/audio?${query}`
}
