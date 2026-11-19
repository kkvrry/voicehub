// @ts-check
// 音频库：麦克风录音、系统内录、引擎生成三类音频的同一张表。
//
// 生成物在数据目录里本来就按引擎分目录（`generated/<引擎>/<音色>/`，缓存同理），
// 所以「生成」下面还能再按引擎列一层——这一层不另建索引，直接读条目上的 `engine`。
//
// 后端未接之前，列表由 demoItems() 提供：条目指向本机上真实存在的音频，能直接试听；
// 其余字段是示例。接后端时只换两处——`items` 换成 `GET /api/v1/audio` 的结果，
// 入库换成 `POST /api/v1/audio/{id}/absorb`；筛选、选择与排序不属于后端。
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
  { key: 'record', label: '录音', hint: '麦克风录入' },
  { key: 'loopback', label: '内录', hint: '系统正在播放的声音' },
  { key: 'engine', label: '生成', hint: '引擎合成的音频' },
]

/**
 * 相对现在往前推一个时刻，单位秒。示例数据用，接后端后删除。
 * @param {number} daysAgo
 * @param {number} hour
 * @param {number} minute
 */
function at(daysAgo, hour, minute) {
  const date = new Date()
  date.setDate(date.getDate() - daysAgo)
  date.setHours(hour, minute, 0, 0)
  return Math.floor(date.getTime() / 1000)
}

/**
 * 生成物在盘上的相对路径：`<引擎 label>/<音色>/<文件>`，`/api/v1/generated` 按它取回。
 * @param {string} key 引擎 key（取回时先查这台引擎自己的目录）
 * @param {string} label 盘上的目录名
 * @param {string} voice
 * @param {string} file
 */
function generatedUrl(key, label, voice, file) {
  const path = [label, voice, file].map((part) => encodeURIComponent(part)).join('/')
  return `/api/v1/generated/${path}?engine=${encodeURIComponent(key)}`
}

function demoItems() {
  return [
    {
      id: 'au-1',
      source: 'loopback',
      file: '内录-20261119-142703.wav',
      text: '',
      duration: 5.3,
      created: at(0, 14, 27),
      detail: 'WAV · 44.1 kHz · 单声道',
      url: `/api/v1/recordings/${encodeURIComponent('内录-20261119-142703.wav')}`,
      engine: '',
      voiceId: '',
      voiceName: '',
      emotion: '',
      params: [],
      absorbed: [],
    },
    {
      id: 'au-2',
      source: 'record',
      file: '录音-20261119-140512.wav',
      text: '你说的对，但是《原神》是由米哈游自主研发的一款全新开放世界冒险游戏。',
      duration: 8.5,
      created: at(0, 14, 5),
      detail: '已识别',
      url: '',
      engine: '',
      voiceId: '',
      voiceName: '',
      emotion: '',
      params: [],
      absorbed: [],
    },
    {
      id: 'au-3',
      source: 'engine',
      file: '20261111-144814-01.wav',
      text: '因为你的素养很差，我现在每天玩原神都能赚 150 原石，每个月差不多 5000 原石。',
      duration: 24.5,
      created: at(0, 13, 58),
      detail: '',
      url: generatedUrl('mimo', 'MiMo', '冰糖', '20261111-144814-01.wav'),
      engine: 'MiMo',
      voiceId: 'mimo:冰糖',
      voiceName: '冰糖',
      emotion: '得意',
      params: ['seed -1'],
      absorbed: [],
    },
    {
      id: 'au-4',
      source: 'engine',
      file: '20261111-145046-01.wav',
      text: '因为你的素养很差，我现在每天玩原神都能赚 150 原石，每个月差不多 5000 原石。',
      duration: 21,
      created: at(0, 13, 51),
      detail: '',
      url: generatedUrl('mimo', 'MiMo', '冰糖', '20261111-145046-01.wav'),
      engine: 'MiMo',
      voiceId: 'mimo:冰糖',
      voiceName: '冰糖',
      emotion: '开心',
      params: ['seed -1'],
      absorbed: [{ voiceId: 'mimo:冰糖', name: '冰糖', emotion: '开心' }],
    },
    {
      id: 'au-5',
      source: 'engine',
      file: '20261106-090257-01.wav',
      text: '你说的对，但是《原神》是由米哈游自主研发的一款全新开放世界冒险游戏。',
      duration: 8.5,
      created: at(1, 20, 12),
      detail: '',
      url: generatedUrl('mimo', 'MiMo', 'Chloe', '20261106-090257-01.wav'),
      engine: 'MiMo',
      voiceId: 'mimo:Chloe',
      voiceName: 'Chloe',
      emotion: '无奈',
      params: ['seed -1'],
      absorbed: [],
    },
    {
      id: 'au-6',
      source: 'engine',
      file: '20261019-093404-01.wav',
      text: '后来人们常常提起那个夏天，和那场没有下完的雨。',
      duration: 9.5,
      created: at(2, 9, 34),
      detail: '',
      url: generatedUrl('mimo', 'MiMo', '白桦', '20261019-093404-01.wav'),
      engine: 'MiMo',
      voiceId: 'mimo:白桦',
      voiceName: '白桦',
      emotion: '平静',
      params: ['seed -1'],
      absorbed: [],
    },
    {
      id: 'au-7',
      source: 'engine',
      file: '20261110-111331-01.mp3',
      text: '沙场百战疾，争衡天下间。',
      duration: 5.2,
      created: at(1, 11, 31),
      detail: '',
      url: generatedUrl(
        'minimax',
        'MiniMax',
        'Chinese (Mandarin)_Crisp_Girl',
        '20261110-111331-01.mp3'
      ),
      engine: 'MiniMax',
      voiceId: 'minimax:Chinese (Mandarin)_Crisp_Girl',
      voiceName: 'Crisp_Girl',
      emotion: '兴奋',
      params: ['语速 1.0'],
      absorbed: [],
    },
    {
      id: 'au-8',
      source: 'loopback',
      file: '内录-20261118-210402.wav',
      text: '',
      duration: 198,
      created: at(1, 21, 4),
      detail: 'WAV · 48 kHz',
      url: '',
      engine: '',
      voiceId: '',
      voiceName: '',
      emotion: '',
      params: [],
      absorbed: [],
    },
  ]
}

/** 打开音频库。示例数据只在第一次进入时铺一次，之后的改动留在内存里。 */
export function openAudio() {
  store.view = 'audio'
  store.engineKey = ''
  store.detail = null
  if (!store.audio.seeded) {
    store.audio.items = demoItems()
    store.audio.seeded = true
  }
  // 入库弹层要挑音色与情绪：音色名单与音色库同一份，情绪候选与生成页同一份。
  if (!store.voices.length) void loadVoices()
  if (!store.spec.fields.length) void loadSpec()
}

/**
 * 来源筛选。换来源时清掉选择：选中的行可能已经不在眼前，批量动作不该带上它。
 * @param {string} key
 */
export function pickSource(key) {
  store.audio.source = key || 'all'
  if (store.audio.source !== 'engine') store.audio.engine = ''
  store.audio.selected = []
}

/**
 * 生成物按引擎再分一层。选引擎即把来源收在「生成」上——两者不是并列的条件。
 * @param {string} key
 */
export function pickEngine(key) {
  store.audio.engine = store.audio.engine === key ? '' : key
  store.audio.source = 'engine'
  store.audio.selected = []
}

/** @param {string} key */
export function pickRange(key) {
  store.audio.range = key
  store.audio.selected = []
}

/** @param {string} key */
export function pickAbsorbed(key) {
  store.audio.absorbed = store.audio.absorbed === key ? '' : key
  store.audio.selected = []
}

export function clearAudioFilters() {
  store.audio.q = ''
  store.audio.source = 'all'
  store.audio.engine = ''
  store.audio.range = 'all'
  store.audio.absorbed = ''
}

/** @param {number} [offset] */
function dayStart(offset = 0) {
  const date = new Date()
  date.setHours(0, 0, 0, 0)
  date.setDate(date.getDate() - offset)
  return date.getTime()
}

/** @param {any} item */
function inRange(item) {
  const range = store.audio.range
  if (range === 'today') return item.created * 1000 >= dayStart(0)
  if (range === '7d') return item.created * 1000 >= dayStart(6)
  if (range === '30d') return item.created * 1000 >= dayStart(29)
  return true
}

/** 当前筛选下的行。顺序固定为最新在前：时间是这张表的主轴。 */
export function visibleAudios() {
  const query = store.audio.q.trim().toLowerCase()
  return store.audio.items
    .filter((item) => store.audio.source === 'all' || item.source === store.audio.source)
    .filter((item) => !store.audio.engine || item.engine === store.audio.engine)
    .filter(inRange)
    .filter((item) => {
      const absorbed = item.absorbed.length > 0
      if (store.audio.absorbed === 'yes') return absorbed
      if (store.audio.absorbed === 'no') return !absorbed
      return true
    })
    .filter((item) => {
      if (!query) return true
      const hay = `${item.text} ${item.file} ${item.voiceName} ${item.engine}`.toLowerCase()
      return hay.includes(query)
    })
    .sort((a, b) => b.created - a.created)
}

/** 各来源的条数。左栏计数与筛选条用的是这一份，不另算一遍。 */
export function sourceCounts() {
  /** @type {Record<string, number>} */
  const counts = { all: 0, record: 0, loopback: 0, engine: 0 }
  for (const item of store.audio.items) {
    counts.all += 1
    counts[item.source] = (counts[item.source] || 0) + 1
  }
  return counts
}

/**
 * 有生成物的引擎。顺序按最后一条出现的时间：最近在用的排在上面；
 * 没有产物的引擎不列——空目录不是一个可点的地方。
 */
export function engineCounts() {
  /** @type {Map<string, number>} */
  const counts = new Map()
  for (const item of [...store.audio.items].sort((a, b) => b.created - a.created)) {
    if (item.source !== 'engine' || !item.engine) continue
    counts.set(item.engine, (counts.get(item.engine) || 0) + 1)
  }
  return [...counts].map(([label, count]) => ({ label, count }))
}

/** @param {string} id */
export function toggleRow(id) {
  const at_ = store.audio.selected.indexOf(id)
  if (at_ >= 0) store.audio.selected.splice(at_, 1)
  else store.audio.selected.push(id)
}

/** 全选当前筛选下的行；已经全选就清空。 */
export function toggleAllVisible() {
  const rows = visibleAudios()
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

/** 顺序播放当前筛选下的行；没有文件的跳过。 */
export function playVisible() {
  const rows = visibleAudios().filter((row) => row.url)
  if (!rows.length) return
  playAll(rows.map((row) => ({ url: row.url, label: row.text || row.file, sub: rowLabel(row) })))
}

/** @param {any} row */
export function rowLabel(row) {
  if (row.source === 'engine') return [row.engine, row.voiceName].filter(Boolean).join(' · ')
  return SOURCES.find((item) => item.key === row.source)?.label || ''
}

/**
 * 入库：把这几条音频登记为某条音色的参考音频。
 *
 * 情绪随音频一起写进样例（`samples[file].emotion`）——它是这条音频自己的属性，
 * 不是音色的；换音色入库时以弹层里那一格为准，默认带入原值。
 *
 * `target.kind`：`origin` 回每一条自己的音色（生成物才有）、`voice` 是选定的音色、
 * `new` 是新建音色。示例实现只在本机记下归属，接后端后换成一次 POST。
 *
 * @param {string[]} ids
 * @param {{ kind: string, voiceId?: string, voiceName?: string, folder?: string, emotion?: string }} target
 */
export function absorb(ids, target) {
  const rows = store.audio.items.filter((item) => ids.includes(item.id))
  if (!rows.length) return
  let name = target.voiceName || ''
  for (const row of rows) {
    const voiceId = target.kind === 'origin' ? row.voiceId : target.voiceId || ''
    const voiceName = target.kind === 'origin' ? row.voiceName : name
    if (!voiceId) continue
    row.absorbed = [
      ...row.absorbed,
      { voiceId, name: voiceName, emotion: target.emotion ?? row.emotion },
    ]
  }
  if (target.kind === 'origin') name = rows[0].voiceName
  store.audio.selected = []
  say(`已入库 ${rows.length} 条到「${name}」`, { kind: 'success' })
}

/**
 * 移入回收站。示例实现只从列表里摘掉；接后端后走 DELETE。
 * @param {string[]} ids
 */
export function discard(ids) {
  if (!ids.length) return
  store.audio.items = store.audio.items.filter((item) => !ids.includes(item.id))
  store.audio.selected = []
  say(`已移入回收站 ${ids.length} 条`, { kind: 'success' })
}

/**
 * 时长：`m:ss`。超过一小时给 `h:mm:ss`。
 * @param {number} seconds
 */
export function clock(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
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
  const date = new Date(seconds * 1000)
  /** @param {number} value */
  const pad = (value) => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
