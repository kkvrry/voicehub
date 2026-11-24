// @ts-check
// 历史：麦克风录音、系统内录、引擎生成三类音频的同一张表。
//
// 列表、计数与引擎分组都由后端算（`GET /api/v1/audio`）：生成物在盘上按引擎分目录，
// 录音与内录按原有规则写在录音目录（重启即清），前端不另建一份索引，也不缓存结果。
// 这里只做三件事：拉列表、把筛选改动变成一次请求、把入库与删除交给后端。
import { api } from '../api'
import { store } from './state'
import { say } from './shell'
import { play, playAll } from './playback'
import { loadVoices } from './library'
import { loadSpec } from './synth'

/**
 * 来源。只有三类，值即落盘位置：录音是麦克风的临时录音、内录是系统输出、
 * 生成是引擎产物（再按引擎分一层）。文案暂放前端——接后端后由列表接口下发。
 */
export const SOURCES = [
  { key: 'record', label: '录音', hint: '麦克风录入，重启服务后清空' },
  { key: 'loopback', label: '内录', hint: '系统正在播放的声音，重启服务后清空' },
  { key: 'engine', label: '生成', hint: '引擎合成的音频' },
]

/** @type {Record<string, string>} */
const RANGE_KEYS = { all: '', today: 'today', '7d': '7d', '30d': '30d' }

let timer = 0

/** @param {unknown} error */
function errorText(error) {
  return error instanceof Error ? error.message : String(error)
}

/** 打开历史页。音色名单与情绪候选与生成页同一份，只为入库弹层备着。 */
export function openAudio() {
  store.view = 'audio'
  store.engineKey = ''
  store.detail = null
  if (!store.voices.length) void loadVoices()
  if (!store.spec.fields.length) void loadSpec()
  void loadAudios()
}

/** 拉当前筛选下的列表。 */
export async function loadAudios() {
  store.audio.loading = true
  try {
    const data = await api.audio({
      source: store.audio.source === 'all' ? '' : store.audio.source,
      engine: store.audio.engine,
      q: store.audio.q.trim(),
      status: store.audio.absorbed,
      span: RANGE_KEYS[store.audio.range] || '',
      limit: 200,
    })
    store.audio.items = data.data || []
    store.audio.counts = data.counts || { all: 0, record: 0, loopback: 0, engine: 0 }
    store.audio.engines = data.engines || []
    store.audio.total = data.total || 0
    store.audio.unabsorbed = data.unabsorbed || 0
    store.audio.hasMore = Boolean(data.has_more)
    store.audio.error = ''
  } catch (error) {
    store.audio.error = errorText(error)
  } finally {
    store.audio.loading = false
  }
}

/** 搜索框：按键即筛，但不必每敲一个字都发一次请求。 */
/** @param {string} value */
export function setAudioQuery(value) {
  store.audio.q = value
  window.clearTimeout(timer)
  timer = window.setTimeout(() => void loadAudios(), 220)
}

/**
 * 来源筛选。换来源时清掉选择：选中的行可能已经不在眼前，批量动作不该带上它。
 * @param {string} key
 */
export function pickSource(key) {
  store.audio.source = key || 'all'
  if (store.audio.source !== 'engine') store.audio.engine = ''
  store.audio.selected = []
  void loadAudios()
}

/**
 * 生成物按引擎再分一层。选引擎即把来源收在「生成」上——两者不是并列的两个条件。
 * @param {string} label
 */
export function pickEngine(label) {
  store.audio.engine = store.audio.engine === label ? '' : label
  store.audio.source = 'engine'
  store.audio.selected = []
  void loadAudios()
}

/** @param {string} key */
export function pickRange(key) {
  store.audio.range = key
  store.audio.selected = []
  void loadAudios()
}

/** @param {string} key */
export function pickAbsorbed(key) {
  store.audio.absorbed = store.audio.absorbed === key ? '' : key
  store.audio.selected = []
  void loadAudios()
}

export function clearAudioFilters() {
  store.audio.q = ''
  store.audio.source = 'all'
  store.audio.engine = ''
  store.audio.range = 'all'
  store.audio.absorbed = ''
  store.audio.selected = []
  void loadAudios()
}

/** @param {any} row */
export function isAbsorbed(row) {
  return Boolean(row?.absorbed?.length)
}

/** @param {string} id */
export function toggleRow(id) {
  const index = store.audio.selected.indexOf(id)
  if (index >= 0) store.audio.selected.splice(index, 1)
  else store.audio.selected.push(id)
}

/** 全选当前列表；已经全选就清空。 */
export function toggleAllVisible() {
  const rows = store.audio.items
  const all = rows.length > 0 && rows.every((row) => store.audio.selected.includes(row.id))
  store.audio.selected = all ? [] : rows.map((row) => row.id)
}

export function clearSelected() {
  store.audio.selected = []
}

/** 试听：交给全局播放条，列表不自己持有 `<audio>`。 */
/** @param {any} row */
export function playRow(row) {
  if (!row.url) return
  play({ url: row.url, label: row.text || row.file, sub: rowLabel(row) })
}

/** 顺序播放当前列表；没有文件的跳过。 */
export function playVisible() {
  const rows = store.audio.items.filter((row) => row.url)
  if (!rows.length) return
  playAll(rows.map((row) => ({ url: row.url, label: row.text || row.file, sub: rowLabel(row) })))
}

/** @param {any} row */
export function rowLabel(row) {
  if (row.source === 'engine') return [row.engine, row.voice_name].filter(Boolean).join(' · ')
  return SOURCES.find((item) => item.key === row.source)?.label || ''
}

/**
 * 入库：把这几条音频登记为某条音色的参考音频。
 *
 * 情绪随音频一起写进样例（`samples[file].emotion`）——它是这条音频自己的属性，
 * 不是音色的；以弹层里那一格为准，默认带入原值。
 *
 * @param {string[]} ids
 * @param {{ kind: string, voiceId?: string, voiceName?: string, folder?: string, gender?: string, language?: string, mainSample?: boolean, emotion?: string }} target
 */
export async function absorb(ids, target) {
  try {
    const result = await api.absorbAudio({
      ids,
      target: {
        kind: target.kind,
        voice_id: target.voiceId || '',
        name: target.voiceName || '',
        gender: target.gender || '',
        language: target.language || '',
        folder: target.folder || '',
        main_sample: Boolean(target.mainSample),
        emotion: target.emotion || '',
      },
    })
    const rows = result.data || []
    store.audio.selected = []
    const name = rows[0]?.name || target.voiceName || ''
    say(`已入库 ${rows.length} 条到「${name}」`, { kind: 'success' })
    await loadAudios()
  } catch (error) {
    say(errorText(error), { kind: 'error' })
  }
}

/**
 * 移入回收站。
 * @param {string[]} ids
 */
export async function discard(ids) {
  if (!ids.length) return
  try {
    const result = await api.deleteAudio({ ids })
    store.audio.selected = []
    say(`已移入回收站 ${result.removed} 条`, { kind: 'success' })
    await loadAudios()
  } catch (error) {
    say(errorText(error), { kind: 'error' })
  }
}

/**
 * 时长：`m:ss`。超过一小时给 `h:mm:ss`；没量出来给 `--:--`。
 * @param {number} seconds
 */
export function clock(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (!total) return '--:--'
  const hour = Math.floor(total / 3600)
  const minute = Math.floor((total % 3600) / 60)
  const second = total % 60
  /** @param {number} value */
  const pad = (value) => String(value).padStart(2, '0')
  return hour ? `${hour}:${pad(minute)}:${pad(second)}` : `${minute}:${pad(second)}`
}

/**
 * 时间列：`MM-DD HH:MM`。
 * @param {number} seconds
 */
export function stamp(seconds) {
  if (!seconds) return '—'
  const date = new Date(seconds * 1000)
  /** @param {number} value */
  const pad = (value) => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
