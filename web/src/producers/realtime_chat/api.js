// @ts-check
// 本插件自己的接口：角色、背景与聊天记录。
// 对话 / 合成 / 识别不在这里——它们走宿主的通用接口，换模型不用改本插件。
import { errorMessage, isTimeout, request, uploadFile } from '../../api'

const base = '/api/v1/producers/realtime_chat'

export const getState = () => request(`${base}/state`)

/** @param {any} state */
export const saveState = (state) =>
  request(`${base}/state`, { method: 'PUT', body: state, timeout: 60_000 })

/** @param {File} file */
export const uploadBackground = (file) => uploadFile(`${base}/backgrounds`, file, {}, 60_000)

/** @param {string} name */
export function backgroundUrl(name) {
  return `${base}/backgrounds/${encodeURIComponent(name)}`
}

/** 音频容器 → 落盘后缀。合成结果的实际格式由引擎决定，取不到就按 WAV 存。 */
/** @param {string} type */
function audioSuffix(type) {
  const mime = String(type || '').toLowerCase()
  if (mime.includes('mpeg') || mime.includes('mp3')) return 'mp3'
  if (mime.includes('mp4') || mime.includes('m4a')) return 'm4a'
  if (mime.includes('aac')) return 'aac'
  if (mime.includes('ogg') || mime.includes('opus')) return 'ogg'
  if (mime.includes('flac')) return 'flac'
  if (mime.includes('webm')) return 'webm'
  return 'wav'
}

/**
 * 合成一条回复，拿到的就是音频本身。
 *
 * 走宿主的 ``persist="none"``：不进全局缓存目录。调用方随后把字节存进插件
 * 自己的数据目录（``uploadAudio``），消息里只记那份地址。
 *
 * @param {any} payload
 * @returns {Promise<Blob>}
 */
export async function synthesizeAudio(payload) {
  let resp
  try {
    resp = await fetch('/api/v1/synthesize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, persist: 'none' }),
      signal: AbortSignal.timeout(600_000),
    })
  } catch (error) {
    if (isTimeout(error)) {
      throw new Error('语音合成超时：本地服务没有响应。')
    }
    throw error
  }
  if (resp.status === 401) throw new Error('登录已失效，正在返回登录页。')
  if (!resp.ok) throw new Error(await errorMessage(resp))
  return resp.blob()
}

/** @param {Blob} blob */
export async function uploadAudio(blob) {
  const type = blob.type || 'audio/wav'
  const file = new File([blob], `reply.${audioSuffix(type)}`, { type })
  return uploadFile(`${base}/audio`, file, {}, 120_000)
}

/** 删掉一条已经没人引用的回复语音；文件本来就不在也算成功。 */
/** @param {string} url */
export function deleteAudio(url) {
  const name = String(url || '').split('/').pop() || ''
  if (!name) return Promise.resolve(null)
  return request(`${base}/audio/${encodeURIComponent(name)}`, { method: 'DELETE' })
}
