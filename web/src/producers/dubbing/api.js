// 配音插件的接口。宿主只给生产方通用三件，其余端点都在这里。
import { request, uploadFile } from '../../api'

const base = '/api/v1/producers/dubbing'
const one = (id) => `${base}/projects/${id}`

export const overview = () => request(`${base}/overview`)

export const detail = (id) => request(one(id))

export const upload = (file, name = '', stream = 0) =>
  uploadFile(`${base}/projects`, file, { name, stream })

export const remove = (id) => request(one(id), { method: 'DELETE' })

export const extract = (id) => request(`${one(id)}/extract`, { method: 'POST' })

export const saveOptions = (id, patch) =>
  request(`${one(id)}/options`, { method: 'POST', body: patch })

export const transcribe = (id) => request(`${one(id)}/transcribe`, { method: 'POST' })

export const cancelTranscribe = (id) =>
  request(`${one(id)}/transcribe/cancel`, { method: 'POST' })

export const synth = (id, cueIds = []) =>
  request(`${one(id)}/synth`, { method: 'POST', body: { cue_ids: cueIds } })

export const cancelSynth = (id) => request(`${one(id)}/synth/cancel`, { method: 'POST' })

export const assemble = (id) => request(`${one(id)}/assemble`, { method: 'POST', timeout: 0 })

export const exportFilm = (id) => request(`${one(id)}/export`, { method: 'POST', timeout: 0 })

export const updateCue = (id, cueId, patch) =>
  request(`${one(id)}/cues/${cueId}`, { method: 'PUT', body: patch })

export const splitCue = (id, cueId, atMs) =>
  request(`${one(id)}/cues/${cueId}/split`, { method: 'POST', body: { at_ms: atMs } })

export const mergeCue = (id, cueId) =>
  request(`${one(id)}/cues/${cueId}/merge`, { method: 'POST' })

export const deleteCue = (id, cueId) =>
  request(`${one(id)}/cues/${cueId}`, { method: 'DELETE' })

export const addRole = (id, payload) =>
  request(`${one(id)}/roles`, { method: 'POST', body: payload })

export const updateRole = (id, roleId, payload) =>
  request(`${one(id)}/roles/${roleId}`, { method: 'PUT', body: payload })

export const deleteRole = (id, roleId) =>
  request(`${one(id)}/roles/${roleId}`, { method: 'DELETE' })

export const job = (id, jobId) => request(`${one(id)}/jobs/${jobId}`)

/** 音色清单：角色选音色用宿主的库接口，不在这里另建一份。 */
export const voices = () => request('/api/v1/voices?limit=0')

export const mediaUrl = (id, kind, name = '') =>
  `${one(id)}/media/${kind}${name ? `?name=${encodeURIComponent(name)}` : ''}`
