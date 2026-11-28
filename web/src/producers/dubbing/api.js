// @ts-check
// 配音插件的接口。宿主只给生产方通用三件，其余端点都在这里。
import { request, uploadFile } from '../../api'

const base = '/api/v1/producers/dubbing'
/** @param {string} id */
const one = (id) => `${base}/projects/${id}`

export const overview = () => request(`${base}/overview`)

/** @param {string} id */
export const detail = (id) => request(one(id))

/**
 * @param {File} file
 * @param {string} [name]
 * @param {number} [stream]
 */
export const upload = (file, name = '', stream = 0) =>
  uploadFile(`${base}/projects`, file, { name, stream: String(stream) })

/** 让宿主机弹文件选择框：浏览器拿不到真实路径，引用导入必须先问宿主。 */
export const pickSource = () => request(`${base}/pick-source`, { method: 'POST' })

/**
 * 引用本机文件建项目：源文件留在原处，只把产物写进项目目录。
 * @param {string} path
 * @param {string} [name]
 */
export const link = (path, name = '') =>
  request(`${base}/projects/link`, { method: 'POST', body: { path, name } })

/**
 * 源文件被移动或改名后重新指定；时长必须与项目时间轴一致。
 * @param {string} id
 * @param {string} path
 */
export const relink = (id, path) =>
  request(`${one(id)}/source`, { method: 'POST', body: { path } })

/** @param {string} id */
export const remove = (id) => request(one(id), { method: 'DELETE' })

/**
 * @param {string} id
 * @param {string} name
 */
export const rename = (id, name) =>
  request(one(id), { method: 'PATCH', body: { name } })

/** @param {string} id */
export const extract = (id) => request(`${one(id)}/extract`, { method: 'POST' })

/**
 * @param {string} id
 * @param {Record<string, any>} patch
 */
export const saveOptions = (id, patch) =>
  request(`${one(id)}/options`, { method: 'POST', body: patch })

/** @param {string} id */
export const transcribe = (id) => request(`${one(id)}/transcribe`, { method: 'POST' })

/**
 * 分轨：从原音轨分出人声与伴奏；模型必须是运行中的那个 id。
 * @param {string} id
 * @param {string} [model]
 */
export const separate = (id, model = '') =>
  request(`${one(id)}/separate`, { method: 'POST', body: { model } })

/**
 * 移除分轨：回到整条原声的混音方式，释放两份 WAV。
 * @param {string} id
 */
export const removeSeparation = (id) =>
  request(`${one(id)}/separation/remove`, { method: 'POST' })

/**
 * @param {string} id
 * @param {File} file
 */
export const importSubtitles = (id, file) =>
  uploadFile(`${one(id)}/subtitles`, file)

/** @param {string} id */
export const cancelTranscribe = (id) =>
  request(`${one(id)}/transcribe/cancel`, { method: 'POST' })

/**
 * @param {string} id
 * @param {string[]} [cueIds]
 * @param {boolean} [force]
 */
export const synth = (id, cueIds = [], force = false) =>
  request(`${one(id)}/synth`, { method: 'POST', body: { cue_ids: cueIds, force } })

/** @param {string} id */
export const cancelSynth = (id) => request(`${one(id)}/synth/cancel`, { method: 'POST' })

/** @param {string} id */
export const cancelJob = (id) => request(`${one(id)}/jobs/cancel`, { method: 'POST' })

/** @param {string} id */
export const assemble = (id) => request(`${one(id)}/assemble`, { method: 'POST' })

/**
 * @param {string} id
 * @param {boolean} [allowIncomplete]
 */
export const exportFilm = (id, allowIncomplete = false) =>
  request(`${one(id)}/export`, {
    method: 'POST',
    body: { allow_incomplete: allowIncomplete },
  })

/**
 * 打开成品所在文件夹；`name` 让后端先确认这个产物还在。
 * @param {string} id
 * @param {string} [name]
 */
export const reveal = (id, name = '') =>
  request(`${one(id)}/reveal`, { method: 'POST', body: { name } })

/**
 * @param {string} id
 * @param {string} cueId
 * @param {Record<string, any>} patch
 */
export const updateCue = (id, cueId, patch) =>
  request(`${one(id)}/cues/${cueId}`, { method: 'PUT', body: patch })

/**
 * @param {string} id
 * @param {Record<string, any>} payload
 */
export const batchCues = (id, payload) =>
  request(`${one(id)}/cues/batch`, { method: 'POST', body: payload })

/**
 * @param {string} id
 * @param {string} cueId
 * @param {number} atMs
 */
export const splitCue = (id, cueId, atMs) =>
  request(`${one(id)}/cues/${cueId}/split`, { method: 'POST', body: { at_ms: atMs } })

/**
 * @param {string} id
 * @param {string} cueId
 */
export const mergeCue = (id, cueId) =>
  request(`${one(id)}/cues/${cueId}/merge`, { method: 'POST' })

/**
 * @param {string} id
 * @param {string} cueId
 */
export const deleteCue = (id, cueId) =>
  request(`${one(id)}/cues/${cueId}`, { method: 'DELETE' })

/**
 * @param {string} id
 * @param {Record<string, any>} payload
 */
export const addRole = (id, payload) =>
  request(`${one(id)}/roles`, { method: 'POST', body: payload })

/**
 * @param {string} id
 * @param {string} roleId
 * @param {Record<string, any>} payload
 */
export const updateRole = (id, roleId, payload) =>
  request(`${one(id)}/roles/${roleId}`, { method: 'PUT', body: payload })

/**
 * @param {string} id
 * @param {string} roleId
 */
export const deleteRole = (id, roleId) =>
  request(`${one(id)}/roles/${roleId}`, { method: 'DELETE' })

/**
 * @param {string} id
 * @param {string} jobId
 */
export const job = (id, jobId) => request(`${one(id)}/jobs/${jobId}`)

/**
 * 产物地址。`version` 是后端给的 `audio_version`：重配会覆盖同名文件，
 * 不带版本时浏览器会拿缓存里的旧音（改完字重配，听着还是上一版）。
 * @param {string} id
 * @param {string} kind
 * @param {string} [name]
 * @param {string | number} [version]
 */
export const mediaUrl = (id, kind, name = '', version = '') =>
  `${one(id)}/media/${kind}${name ? `?name=${encodeURIComponent(name)}` : ''}${
    version ? `${name ? '&' : '?'}v=${encodeURIComponent(version)}` : ''
  }`
