// @ts-check
// 播放这一域：一个 <audio> 实例 + 顶栏上的一条播放条（`PlayerBar.vue`）。
// 播放不跟着列表被重画掉（从前生成页是行内 `<audio controls>`，重画即断声）；
// 「全部试听」需要队列；底栏要的标题/进度/时长是同一份状态的几面。
// 谁想放点什么就递一条曲目进来（`play` / `playAll`），界面上只读 `store.player`。
import { store } from './state'

/** 唯一的 <audio>，挂在 body 上：自检要读它、要确认"它没被重画掉"。
 *  它没有 controls，页面上看不见。 */
export const el = document.createElement('audio')
el.id = 'vh-player'
el.preload = 'metadata'
// 挂进 body 之前关掉 controls，免得某些浏览器自己冒出一个迷你播放器。
el.controls = false
document.body.appendChild(el)

/** 曲目形状：`{ url, label, sub, voice }`。都不给时退回 URL 最后一段。
 *  `voice` 是这条音频属于哪个音色（可空）：带上它，播放条上的「收藏」「指定」
 *  才知道说的是谁；行音频、章节成品、合成结果不属于任何音色，不带。 */
/**
 * @param {{ url?: string, label?: string, sub?: string, voice?: any, start?: number, end?: number }} [item]
 * @returns {{ url: string, label: string, sub: string, voice: any, start: number, end: number }}
 */
function track(item = {}) {
  const url = String(item.url || '')
  const start = Number(item.start)
  const end = Number(item.end)
  const startAt = Number.isFinite(start) && start > 0 ? start : 0
  return {
    url,
    label: item.label || decodeURIComponent(url.split('/').pop() || ''),
    sub: item.sub || '',
    voice: item.voice || null,
    // 一段曲目可以只放整条音频里的 `[start, end)`：成片试听里的原声段
    // 就是源音轨的一段，没必要为每一段切一个临时文件。
    start: startAt,
    end: Number.isFinite(end) && end > startAt ? end : 0,
  }
}

/** 收藏 / 隐藏改完之后，把队列里那条音色的快照跟着改。
 *  播放条读的是曲目上这份引用：切页后列表可能换了对象，不跟着改就会停在旧星标。 */
/**
 * @param {string} voiceId
 * @param {any} view
 */
export function syncVoice(voiceId, view) {
  if (!voiceId || !view) return
  for (const item of store.player.queue) {
    if (item.voice?.id !== voiceId) continue
    if ('favorite' in view) item.voice.favorite = view.favorite
    if ('hidden' in view) item.voice.hidden = view.hidden
  }
}

/** @param {any} patch */
function set(patch) {
  Object.assign(store.player, patch)
}

/** 当前曲目截取的起止（秒）。`end > start` 时只放这一段。 */
let rangeStart = 0
let rangeEnd = 0
/** 段尾已经处理过：高频检查与 `timeupdate` 都会到这里，避免连续跳下一条。 */
let rangeGuard = false
/** 截取段的段尾检查。`timeupdate` 约 4Hz，按它停会多放一二百毫秒；
 *  只在对白段试听时开一个 25ms 的定时器，停得干净。 */
let rangeTimer = 0

/** 当前曲目。取的是开播那一刻的快照，队列不会被列表重画改掉。 */
function currentItem() {
  return store.player.queue[store.player.index]
}

function unwatchRange() {
  if (rangeTimer) {
    window.clearInterval(rangeTimer)
    rangeTimer = 0
  }
}

/** 截取段播放时开一个短周期检查：段尾精确到 25ms 左右，不靠 4Hz 的 timeupdate。 */
function watchRange() {
  if (rangeTimer || !(rangeEnd > rangeStart)) return
  rangeTimer = window.setInterval(() => {
    if (!store.player.playing) return
    if (el.currentTime < rangeEnd - 0.01) return
    if (rangeGuard) return
    rangeGuard = true
    el.pause()
    advance()
  }, 25)
}

/** 播放条逐帧绘制用的实时进度（0–1）。
 *  `timeupdate` 只有约 4Hz，直接拿它画整块底色会一段一段往前跳；
 *  这里直接读隐藏 `<audio>` 的当前时间，由组件在每帧取一次。 */
export function liveFraction() {
  const start = rangeStart
  const duration = rangeEnd > start ? rangeEnd - start : Number(el.duration)
  if (!Number.isFinite(duration) || duration <= 0) return 0
  return Math.min(1, Math.max(0, (el.currentTime - start) / duration))
}

/** 起播点（0–1 的比例）：换源那一瞬 `duration` 还是 NaN，换算不成秒，只能挂着
 *  等元数据到了再落。同一时刻最多挂一个。 */
let pendingFrac = 0

/** 把第 `index` 条装进 `<audio>` 并开始放。`frac` 是起播点（点波形哪一格就从哪起）。 */
/**
 * @param {number} index
 * @param {number} [frac]
 */
function load(index, frac = 0) {
  const item = store.player.queue[index]
  if (!item) {
    stop()
    return
  }
  unwatchRange()
  rangeStart = Number(item.start) || 0
  rangeEnd = Number(item.end) > rangeStart ? Number(item.end) : 0
  rangeGuard = false
  set({
    index,
    url: item.url,
    label: item.label,
    sub: item.sub,
    position: 0,
    duration: 0,
    error: '',
  })
  pendingFrac = frac > 0 && frac < 1 ? frac : 0
  el.src = item.url
  // 被自动播放策略挡下时会 reject，不接住就是一条未捕获异常。
  el.play().catch((error) => {
    set({ playing: false, error: playError(error) })
  })
}

/** 自动播放被挡时说人话（其余照原样——那些是解码/网络问题）。 */
/** @param {any} error */
function playError(error) {
  const name = String(error?.name || '')
  if (name === 'NotAllowedError') return '浏览器拦下了自动播放，点一下 ▶ 继续'
  return String(error?.message || error)
}

/** 放一条（替换队列，除非它本来就是当前队列里的那一条）。 */
/** @param {any} item */
export function play(item) {
  const one = track(item)
  if (!one.url) return
  // 点的是正在放的那一条 = 暂停/继续。
  if (one.url === store.player.url) {
    togglePlay()
    return
  }
  const queue = store.player.queue
  const here = queue.findIndex((row) => row.url === one.url)
  // 已在队列里：就地跳过去，别把队列缩成一条。
  if (here >= 0) {
    load(here)
    return
  }
  set({ queue: [one], manual: false })
  load(0)
}

/** 单条试听：只放这一条，替换掉当前队列；放完停住，不自动接下一段。
 *  头像播放键先走这里出声，随后由 `library.playVoiceCard` 补成该音色的样例队列。 */
/** @param {any} item */
export function playSolo(item) {
  const one = track(item)
  if (!one.url) return
  const here = store.player.url === one.url
  set({ queue: [one], index: 0, manual: true })
  if (here) togglePlay()
  else load(0)
}

/** 起播后把单条试听补成完整队列，停在正在放的那一条；换过曲子就作废。
 *  头像播放键的队列是手动队列：上/下一段可用，但播完当前这条不自动续播。 */
/**
 * @param {string} url
 * @param {any[]} items
 */
export function extendQueue(url, items) {
  if (!url || store.player.url !== url) return
  const queue = (items || []).map(track).filter(
    /** @param {{ url: string }} row */
    (row) => row.url
  )
  const index = queue.findIndex(
    /** @param {{ url: string }} row */
    (row) => row.url === url
  )
  if (queue.length < 2 || index < 0) return
  set({ queue, index, manual: true })
}

/** 把当前这条收成手动单选队列，不动声音。
 *  从自动队列切到卡片队列时用：先别暂停，等样例清单回来再补齐。 */
export function manualizeCurrent() {
  const item = store.player.queue[store.player.index]
  if (!item) return
  set({ queue: [item], index: 0, manual: true })
}

/** 全部试听：把它们排成一条队列，从 `start` 那条开始；`frac` 是这条的起播点。 */
/**
 * @param {any[]} items
 * @param {number} [start]
 * @param {number} [frac]
 */
export function playAll(items, start = 0, frac = 0) {
  const queue = (items || []).map(track).filter(
    /** @param {{ url: string }} row */
    (row) => row.url
  )
  if (!queue.length) return
  const index = Math.min(Math.max(start, 0), queue.length - 1)
  set({ queue, manual: false })
  load(index, frac)
}

/** 从队列里摘掉一条（生成页的「撤下」）：队列是开播那一刻的快照，
 *  不摘的话「全部播放」还会把它放出来。
 *  摘的正是当前这条时：播着就接着放下一条（已是最后一条就收条），停着就收条。 */
/** @param {string} url */
export function dropTrack(url) {
  const here = url ? store.player.queue.findIndex((row) => row.url === url) : -1
  if (here < 0) {
    if (url && store.player.url === url) stop()
    return
  }
  const wasPlaying = store.player.playing
  store.player.queue.splice(here, 1)
  if (here < store.player.index) {
    set({ index: store.player.index - 1 })
    return
  }
  if (here > store.player.index) return
  if (wasPlaying && here < store.player.queue.length) load(here)
  else stop()
}

/** 把一批音色排成队列（只取有主样例的），从第 `start` 条开始。
 *  只给「全部播放 / 全部试听」用；头像单条试听走 `playSolo`，不经过这里。 */
/**
 * @param {any[]} voices
 * @param {number} [start]
 */
export function playVoices(voices, start = 0) {
  const items = (voices || [])
    .filter(
      /** @param {any} voice */
      (voice) => voice.main_audio_url
    )
    .map(
      /** @param {any} voice */
      (voice) => ({
      url: voice.main_audio_url,
      label: voice.shown_name,
      sub: '主样例',
      voice,
    }))
  if (items.length) playAll(items, Math.min(Math.max(start, 0), items.length - 1))
  return items.length
}

export function togglePlay() {
  if (!store.player.url) return
  if (el.paused) {
    set({ error: '' })
    el.play().catch((error) => set({ playing: false, error: playError(error) }))
  } else {
    el.pause()
  }
}

/** 拖进度条，只在拖动结束/输入时调——每次都 seek 会把音频拽得一顿一顿的。 */
/** @param {number} seconds */
export function seek(seconds) {
  const target = Number(seconds)
  if (!Number.isFinite(target)) return
  if (rangeEnd > rangeStart) {
    const inside = Math.min(Math.max(target, 0), rangeEnd - rangeStart)
    el.currentTime = rangeStart + inside
    set({ position: inside })
    return
  }
  el.currentTime = Math.min(Math.max(target, 0), el.duration || target)
  set({ position: el.currentTime })
}

/** 按比例定位（0–1）：行内波形点了哪一格就是哪一格。
 *  装好的就地 seek；还没装（或还在取元数据）就挂成起播点，等元数据到了再落。 */
/** @param {number} frac */
export function seekFraction(frac) {
  if (!store.player.url) return
  const ratio = Math.min(Math.max(Number(frac) || 0, 0), 1)
  const duration = rangeEnd > rangeStart ? rangeEnd - rangeStart : Number(el.duration)
  if (Number.isFinite(duration) && duration > 0) {
    seek(ratio * duration)
    return
  }
  pendingFrac = ratio
}

/** 下一段 / 上一段。队尾再点"下一段"停在最后一条（"又从头开始了"比"没了"更让人困惑）。 */
export function nextTrack() {
  if (store.player.index + 1 < store.player.queue.length) load(store.player.index + 1)
}

export function prevTrack() {
  if (store.player.index > 0) load(store.player.index - 1)
}

/** 一条放完（自然结束或到了截取段尾）接到下一条；手动队列停在当前这条。 */
function advance() {
  if (store.player.manual) {
    set({ playing: false, position: store.player.duration || store.player.position })
    return
  }
  if (store.player.index + 1 < store.player.queue.length) {
    load(store.player.index + 1)
    return
  }
  stop()
}

/** 关掉播放条：停声、清空队列与标题。不重置 `<audio>.src`——清 src 会触发一次
 *  error 事件，那句"加载失败"会挂在已被关掉的播放条上。 */
export function stop() {
  el.pause()
  unwatchRange()
  rangeStart = 0
  rangeEnd = 0
  rangeGuard = false
  set({
    url: '', label: '', sub: '', queue: [], index: 0, manual: false,
    playing: false, position: 0, duration: 0, error: '',
  })
}

el.addEventListener('play', () => {
  set({ playing: true, error: '' })
  watchRange()
})
el.addEventListener('pause', () => {
  set({ playing: false })
  unwatchRange()
})
el.addEventListener('timeupdate', () => {
  const item = currentItem()
  const start = Number(item?.start) || 0
  const end = Number(item?.end) || 0
  if (end > start) {
    if (el.currentTime >= end - 0.03) {
      if (!rangeGuard) {
        rangeGuard = true
        el.pause()
        advance()
      }
      return
    }
    set({ position: Math.max(0, el.currentTime - start) })
    return
  }
  set({ position: el.currentTime })
})
el.addEventListener('durationchange', () => {
  const item = currentItem()
  const start = Number(item?.start) || 0
  const end = Number(item?.end) || 0
  set({
    duration:
      end > start ? end - start : Number.isFinite(el.duration) ? el.duration : 0,
  })
})
// 换源时挂着的起播点：元数据到了才知道总长，比例这时才换得成秒。
el.addEventListener('loadedmetadata', () => {
  const item = currentItem()
  const start = Number(item?.start) || 0
  const end = Number(item?.end) || 0
  if (end > start) {
    rangeStart = start
    rangeEnd = end
    if (Number.isFinite(el.duration) && el.duration > start) el.currentTime = start
    set({ duration: end - start, position: 0 })
    return
  }
  if (!pendingFrac) return
  const ratio = pendingFrac
  pendingFrac = 0
  if (Number.isFinite(el.duration) && el.duration > 0) seek(ratio * el.duration)
})
// 放完接下一段：这就是「全部试听」的全部实现。
// 手动队列（头像试听该音色的样例）不续播：停在当前这条，让用户自己点上/下一段。
el.addEventListener('ended', () => advance())
el.addEventListener('error', () => {
  // 没有 src 时（stop() 之后）也会走到这里，那不是错误。
  if (!store.player.url) return
  unwatchRange()
  set({ playing: false, error: '这段音频加载失败（文件被移走或格式不支持）' })
})
