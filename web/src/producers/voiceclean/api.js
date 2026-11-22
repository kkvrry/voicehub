// @ts-check
// 插件自己的接口。宿主只提供生产方通用三件，其余端点都在这里。
import { request, uploadFile } from '../../api'

const base = '/api/v1/producers/voiceclean'

export const state = () => request(`${base}/state`)

/**
 * 上传一份电脑里的音频：只处理与下载，不写回音色库。
 * @param {File} file
 */
export const uploadSource = (file) => uploadFile(`${base}/source`, file, {}, 120_000)

/** @param {string} token */
export function sourceUrl(token) {
  const query = new URLSearchParams({ token })
  return `${base}/source/audio?${query}`
}

/**
 * @param {any} scope
 * @param {any[]} steps
 */
export const run = (scope, steps) =>
  request(`${base}/run`, { method: 'POST', body: { scope, steps }, timeout: 60_000 })

export const job = () => request(`${base}/job`)

/**
 * 下载一个候选模型；下载完成后服务端会自动登记/加载。
 * @param {string} step
 * @param {string} model
 */
export const downloadModel = (step, model) =>
  request(`${base}/models/download`, {
    method: 'POST',
    body: { step, model },
    timeout: 600_000,
  })

export const cancel = () => request(`${base}/job/cancel`, { method: 'POST' })

/** 清掉已完成 / 已替换的任务，回到可编辑状态。 */
export const clearJob = () => request(`${base}/job/clear`, { method: 'POST' })

/**
 * @param {string} job_id
 * @param {number[]} indexes
 */
export const apply = (job_id, indexes) =>
  request(`${base}/job/apply`, { method: 'POST', body: { job_id, indexes }, timeout: 120_000 })

/**
 * 把处理结果作为新样例入库（不替换原声）。
 * @param {string} job_id
 * @param {number[]} indexes
 */
export const save = (job_id, indexes) =>
  request(`${base}/job/save`, { method: 'POST', body: { job_id, indexes }, timeout: 120_000 })

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

/**
 * 电脑文件处理结果的下载地址；文件名由后端按原文件生成。
 * @param {string} job_id
 * @param {number} index
 */
export function downloadUrl(job_id, index) {
  const query = new URLSearchParams({ job_id, index: String(index) })
  return `${base}/job/download?${query}`
}
