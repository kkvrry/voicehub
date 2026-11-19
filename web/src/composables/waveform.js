// @ts-check
/**
 * 音频波形：解码一次、按 RMS 降采样，结果按 URL 缓存。
 *
 * 为什么是 RMS：峰值会被一小段里的毛刺拉满，安静处也像一直在说话；RMS 跟着能量走。
 * 解码用 `OfflineAudioContext`：它不出声，不要求用户手势。
 *
 * 行内小波形与裁剪面板共用这一份缓存：同一个 URL 只取一次、只解一次，两边只是
 * 按各自需要重采样点数。
 */

/** 解码后保留的点数。够画一条 500px 的波形，也够再降采样到行内的小图。 */
export const POINTS = 180

/** @typedef {{ peaks: number[], duration: number }} Waveform */

/** @type {Map<string, Promise<Waveform>>} */
const cache = new Map()
/** @type {OfflineAudioContext | null} */
let decoder = null

/**
 * @param {ArrayBuffer} raw
 * @returns {Promise<AudioBuffer>}
 */
async function decodeOnce(raw) {
  if (!decoder) {
    // Safari 老版本只有带前缀的构造器；Window 类型里没有它，按 any 取。
    const win = /** @type {any} */ (window)
    const Offline = win.OfflineAudioContext || win.webkitOfflineAudioContext
    if (!Offline) throw new Error('没有可用的音频解码器')
    decoder = new Offline(1, 1, 44100)
  }
  return /** @type {OfflineAudioContext} */ (decoder).decodeAudioData(raw)
}

/**
 * 解码失败的兜底：换一个全新的 OfflineAudioContext 再试，再不行用实时
 * AudioContext。部分浏览器与容器（FLAC 尤其明显）在复用旧的离线上下文时会拒解，
 * 换一个上下文就能过；这里只影响画波形，不阻塞拖动手柄。
 *
 * @param {ArrayBuffer} raw
 * @returns {Promise<AudioBuffer|null>}
 */
async function decodeAudio(raw) {
  try {
    return await decodeOnce(raw.slice(0))
  } catch {
    // 换一个全新的离线上下文再试一次。
  }
  try {
    const win = /** @type {any} */ (window)
    const Offline = win.OfflineAudioContext || win.webkitOfflineAudioContext
    if (Offline) return await new Offline(1, 1, 44100).decodeAudioData(raw.slice(0))
  } catch {
    // 继续尝试实时上下文。
  }
  try {
    const win = /** @type {any} */ (window)
    const Ctx = win.AudioContext || win.webkitAudioContext
    if (!Ctx) return null
    const context = new Ctx()
    try {
      return await context.decodeAudioData(raw.slice(0))
    } finally {
      void context.close()
    }
  } catch {
    return null
  }
}

/**
 * @param {string} url
 * @returns {Promise<Waveform>}
 */
export function loadWaveform(url) {
  if (cache.has(url)) return /** @type {Promise<Waveform>} */ (cache.get(url))
  const job = (async () => {
    const nothing = { peaks: [], duration: 0 }
    const response = await fetch(url, { cache: 'no-store' })
    if (!response.ok) return nothing
    const raw = await response.arrayBuffer()
    const audio = await decodeAudio(raw)
    if (!audio) return nothing
    const duration = Number(audio.duration) || 0
    const data = audio.getChannelData(0)
    if (!data.length) return { peaks: [], duration }
    const step = Math.max(1, Math.floor(data.length / POINTS))
    const values = []
    for (let point = 0; point < POINTS; point += 1) {
      const from = point * step
      const to = Math.min(from + step, data.length)
      let energy = 0
      for (let i = from; i < to; i += 1) energy += data[i] * data[i]
      values.push(Math.sqrt(energy / Math.max(1, to - from)))
    }
    const top = Math.max(...values)
    if (!top) return { peaks: [], duration }
    // 归一化后开一点方（0.85）：轻声不至于缩成一条线，气口也不会被抬成"还在说话"。
    return {
      duration,
      peaks: values.map((value) => 0.05 + Math.pow(value / top, 0.85) * 0.95),
    }
  })().catch(() => ({ peaks: [], duration: 0 }))
  cache.set(url, job)
  return job
}

/**
 * 丢掉一条音频的缓存：替换样例后文件名不变、URL 也不变，
 * 不丢就会一直画替换前那一段的波形。
 *
 * @param {string} url
 */
export function invalidateWaveform(url) {
  if (url) cache.delete(url)
}

/**
 * 把缓存的波形重采样到指定点数（取每段最大值，保留短促的起落）。
 *
 * @param {number[]} peaks
 * @param {number} count
 * @returns {number[]}
 */
export function resample(peaks, count) {
  if (!peaks.length || count <= 0) return []
  if (peaks.length === count) return peaks
  const out = []
  for (let point = 0; point < count; point += 1) {
    const from = Math.floor((point * peaks.length) / count)
    const to = Math.max(from + 1, Math.floor(((point + 1) * peaks.length) / count))
    let top = 0
    for (let i = from; i < to && i < peaks.length; i += 1) {
      if (peaks[i] > top) top = peaks[i]
    }
    out.push(top)
  }
  return out
}
