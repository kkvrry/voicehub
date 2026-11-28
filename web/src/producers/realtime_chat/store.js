// @ts-check
// 实时对话工作区的状态与动作。
//
// 模型、音色、识别都不在这里存第二份：对话走宿主的 /llm/chat，合成走
// /synthesize，识别走 /asr/stream（服务端 VAD 流式分段，说完一段发一段）；
// 本文件只负责角色、背景、聊天记录，以及把一轮对话串起来。
import { computed, reactive } from 'vue'
import { api as hostApi } from '../../api'
import { AsrStream } from '../../api/asrStream'
import { actions as hostActions, say, store as hostStore } from '../../store'
import bgDawn from './assets/bg-dawn.jpg'
import bgNeon from './assets/bg-neon.jpg'
import bgRain from './assets/bg-rain.jpg'
import bgStudy from './assets/bg-study.jpg'
import * as api from './api'

// 立绘是随包分发的二进制素材。缺文件时（素材还没放进来）只少掉那一张，
// 不让整个构建 / 页面跟着失败；把 `assets/portraits/<key>.webp` 放回去就会自动出现。
const portraitFiles = /** @type {Record<string, string>} */ (
  import.meta.glob('./assets/portraits/*.webp', { eager: true, import: 'default' })
)

/** @param {string} key */
function portraitFile(key) {
  return portraitFiles[`./assets/portraits/${key}.webp`] || ''
}

/** 立绘的默认站位与大小：靠左站，整幅高度铺满房间。 */
export const PORTRAIT_X = 0.02
export const PORTRAIT_Y = 0
export const PORTRAIT_SCALE = 1
/** 拖动 / 滚轮缩放的边界：出了这个范围，人要么跑出画框，要么小得看不清。 */
export const PORTRAIT_X_MIN = -0.05
export const PORTRAIT_X_MAX = 0.72
export const PORTRAIT_Y_MAX = 0.45
export const PORTRAIT_SCALE_MIN = 0.6
export const PORTRAIT_SCALE_MAX = 1.25

/**
 * @typedef {object} ChatMessage
 * @property {string} id
 * @property {'user'|'assistant'} role
 * @property {string} text
 * @property {string} audio_url
 * @property {number} created
 * @property {boolean} [error]
 * @property {string} [audio_error]
 * @property {string[]} [notes]
 *
 * @typedef {object} Session 一份记录：一段连续聊下来、可以整体翻回去的东西。
 * @property {string} id
 * @property {number} created
 * @property {ChatMessage[]} messages
 *
 * @typedef {object} Character
 * @property {string} id
 * @property {string} name
 * @property {string} intro
 * @property {string} persona
 * @property {string} voice_id
 * @property {string} voice_name
 * @property {string} voice_engine ``auto`` = 每次从已开启的本地引擎里挑
 * @property {string[]} voice_engines 这条音色可用的引擎（建角色时的快照）
 * @property {string} voice_cover
 * @property {boolean} voice_local 本地音色才有样例可选
 * @property {'emotion'|'fixed'} voice_sample_mode 样例策略
 * @property {string} voice_sample_file ``fixed`` 时钉住的那一条
 * @property {string} background
 * @property {string} portrait
 * @property {number} portrait_x 立绘离左沿的比例（0–1），拖动即改
 * @property {number} portrait_y 立绘离底边的比例（0–1），拖动即改
 * @property {number} portrait_scale 立绘高度倍数（0.6–1.25）
 * @property {Session[]} sessions
 * @property {string} active_session
 *
 * @typedef {object} ChatState
 * @property {number} version
 * @property {string} active_id
 * @property {string} llm_provider 整份状态共用一家对话服务：换角色不该换模型。
 * @property {string} llm_model
 * @property {Character[]} characters
 */

export const state = reactive({
  /** @type {ChatState|null} */
  data: null,
  loaded: false,
  error: '',
  /** ``''`` | ``thinking`` | ``speaking`` */
  busy: '',
  saving: false,
  draft: '',
  /** 宿主已掩码的对话模型清单。 */
  /** @type {any[]} */
  providers: [],
  /** 当前服务能列出的模型；拉不到时为空，改用文本框。 */
  /** @type {any[]} */
  models: [],
  modelsReason: '',
  modelsBusy: false,
  chatError: '',
  backgroundBusy: false,
  /** 角色设置那张纸：浮在房间里，不换页。 */
  sheetOpen: false,
  /** 这张纸是「用音色卡新建角色」打开的：标题与提示跟着换。 */
  sheetNew: false,
  /** 正在让对话模型写人设。 */
  personaBusy: false,
  personaError: '',
  /** 当前角色那条音色可选的样例（本地音色才有）。 */
  /** @type {any[]} */
  voiceSamples: [],
  voiceSamplesBusy: false,
  /** 上面这份样例属于哪条音色：换人时据此决定要不要重取。 */
  voiceSamplesFor: '',
  /** 正在合成的消息 id。 */
  synthesizingId: '',
  /** 正在放出来的那一条（宿主播放条放的就是它）：台本上只有这一条亮着。 */
  speakingId: '',
  /** 本轮刚收到的回复 id：气泡立即显示它，即使未配音色或播完后也保持可读，直到关闭或下一轮。 */
  lastReplyId: '',
  /** 语音输入完成后让输入框拿焦点。 */
  focusTick: 0,
  /** 连续听写：``''`` 没开 | ``connecting`` | ``listening``。 */
  listening: '',
  /** 服务端 VAD 判定"正在说话"（点亮麦克风指示）。 */
  micSpeaking: false,
  /** 说话中的部分识别文字（实时上屏，随说话推进变化；段落定稿即清）。 */
  liveText: '',
})

export const active = computed(() => {
  const data = state.data
  if (!data || !data.characters.length) return null
  return data.characters.find((item) => item.id === data.active_id) || data.characters[0]
})

/** 当前角色下的全部记录，从旧到新。 */
export const sessions = computed(() => active.value?.sessions || [])

/** 正在看的那一份记录。 */
export const session = computed(() => {
  const char = active.value
  if (!char) return null
  return char.sessions.find((row) => row.id === char.active_session) || char.sessions[0] || null
})

/** 台本上的消息：只来自当前这一份记录。 */
export const messages = computed(() => session.value?.messages || [])

let saveTimer = 0
let saveInFlight = false
let saveAgain = false

/** @param {unknown} value */
function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

/** @param {unknown} error */
function errorText(error) {
  return error instanceof Error ? error.message : String(error)
}

/** @param {string} prefix */
function uid(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`
}

/** 一份记录最多留这么多条；再久的内容模型也用不上。 */
const MAX_MESSAGES = 60
/** 一个角色最多留这么多份记录；服务端同样裁到 30，这里先在本地裁，回包不会整份反过来。 */
const MAX_SESSIONS = 30

/** @param {string} id */
function findCharacter(id) {
  return state.data?.characters.find((item) => item.id === id) || null
}

/** @param {Character} char @param {string} id */
function findSession(char, id) {
  return char.sessions.find((row) => row.id === id) || null
}

/** 按消息 id 在当前状态里重新找一遍：await 期间记录可能已经被删。 */
/** @param {string} id */
function findMessage(id) {
  for (const char of state.data?.characters || []) {
    for (const row of char.sessions) {
      const found = row.messages.find((message) => message.id === id)
      if (found) return found
    }
  }
  return null
}

/** @param {unknown} a @param {unknown} b */
function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** 丢掉已经没有任何消息引用的语音文件；失败只说明它先被别人收走了。 */
/** @param {string[]} urls */
function forgetAudio(urls) {
  const unique = [...new Set(urls.filter(Boolean))]
  for (const url of unique) {
    void api.deleteAudio(url).catch(() => {})
  }
}

/** @param {Session} session */
function trimMessages(session) {
  if (session.messages.length <= MAX_MESSAGES) return
  const dropped = session.messages.splice(0, session.messages.length - MAX_MESSAGES)
  forgetAudio(dropped.map((message) => message.audio_url))
}

/** 记录数在本地先裁到上限；被挤掉的是最旧的那几份。 */
/** @param {Character} char */
function trimSessions(char) {
  if (char.sessions.length <= MAX_SESSIONS) return
  const dropped = char.sessions.splice(0, char.sessions.length - MAX_SESSIONS)
  for (const row of dropped) forgetAudio(row.messages.map((message) => message.audio_url))
  if (!char.sessions.some((row) => row.id === char.active_session)) {
    char.active_session = char.sessions[char.sessions.length - 1].id
  }
}

/** 空记录：新的一份从这里开始。 */
/** @returns {Session} */
function blankSession() {
  return { id: uid('rec'), created: Date.now(), messages: [] }
}

/** 服务端是唯一写方；这里只把缺字段补齐，免得旧状态让整页打不开。 */
/** @param {any} item */
function normalizeSession(item) {
  const messages = Array.isArray(item?.messages) ? item.messages : []
  return {
    id: String(item?.id || uid('rec')),
    created: Number(item?.created) || Date.now(),
    messages,
  }
}

/** @param {any} payload */
function normalizeState(payload) {
  if (!payload || typeof payload !== 'object') throw new Error('服务没有返回对话状态。')
  const rows = Array.isArray(payload.characters) ? payload.characters : []
  if (!rows.length) throw new Error('对话状态里没有角色。')
  /** @type {Character[]} */
  const characters = rows.map(/** @param {any} item */ (item) => {
    /** @type {Session[]} */
    let list = Array.isArray(item.sessions) ? item.sessions.map(normalizeSession) : []
    if (!list.length) list.push(blankSession())
    if (list.length > MAX_SESSIONS) list = list.slice(-MAX_SESSIONS)
    const activeSession = list.some((row) => row.id === item.active_session)
      ? item.active_session
      : list[list.length - 1].id
    return {
      ...item,
      background: String(item.background || ''),
      portrait: String(item.portrait || ''),
      sessions: list,
      active_session: activeSession,
    }
  })
  const activeId = characters.some((item) => item.id === payload.active_id)
    ? payload.active_id
    : characters[0].id
  return {
    version: Number(payload.version) || 2,
    active_id: activeId,
    llm_provider: String(payload.llm_provider || ''),
    llm_model: String(payload.llm_model || ''),
    characters,
  }
}

export async function refresh() {
  try {
    state.data = normalizeState(await api.getState())
    state.loaded = true
    state.error = ''
  } catch (error) {
    state.error = errorText(error)
    say(`对话状态读取失败：${state.error}`, { kind: 'error' })
  }
}

/** 宿主打开这个工作区时调用：状态、对话模型清单、识别状态各取一次。
 *  引擎清单也现取：这里要按"此刻有绿点的那几台"挑声音，不能拿启动时的旧状态。 */
export async function open() {
  if (!state.loaded) await refresh()
  await Promise.allSettled([
    hostActions.loadAsr(),
    hostActions.loadLlm(),
    hostActions.loadEngines(),
  ])
  state.providers = Array.isArray(hostStore.llm?.providers) ? hostStore.llm.providers : []
  await syncModels()
}

/** 离开工作区：把还在防抖窗口里的改动落盘；不打断宿主的播放条。 */
export function close() {
  clearTimeout(saveTimer)
  saveTimer = 0
  void saveNow()
}

/** @param {string} id */
export function setActive(id) {
  if (!state.data) return
  if (!state.data.characters.some((item) => item.id === id)) return
  state.data.active_id = id
  state.chatError = ''
  state.speakingId = ''
  state.lastReplyId = ''
  scheduleSave()
}

/** 换一份记录看：台本整块换成那一份，正在放的回复先停下。 */
/** @param {string} id */
export function setSession(id) {
  const char = active.value
  if (!char) return
  if (!char.sessions.some((row) => row.id === id)) return
  char.active_session = id
  state.chatError = ''
  state.speakingId = ''
  state.lastReplyId = ''
  if (hostStore.player?.playing && hostStore.player.sub === '回复') hostActions.stop()
  scheduleSave()
}

/** 新的一份记录：不打断角色与音色，只是另起一页。 */
export function newSession() {
  const char = active.value
  if (!char) return null
  const session = blankSession()
  char.sessions.push(session)
  trimSessions(char)
  char.active_session = session.id
  state.chatError = ''
  state.speakingId = ''
  state.lastReplyId = ''
  scheduleSave()
  void saveNow()
  return session
}

/** 删一份记录。只剩一份时不删角色，换成一张白纸——列表永远有东西可点。 */
/** @param {string} id */
export function removeSession(id) {
  if (state.busy) return
  const char = active.value
  if (!char) return
  const at = char.sessions.findIndex((row) => row.id === id)
  if (at < 0) return
  const [dropped] = char.sessions.splice(at, 1)
  if (dropped) forgetAudio(dropped.messages.map((message) => message.audio_url))
  if (!char.sessions.length) {
    const fresh = blankSession()
    char.sessions.push(fresh)
    char.active_session = fresh.id
  } else if (char.active_session === id) {
    const fallback = char.sessions[Math.max(0, at - 1)] || char.sessions[0]
    char.active_session = fallback.id
  }
  state.chatError = ''
  state.speakingId = ''
  state.lastReplyId = ''
  scheduleSave()
  void saveNow()
}

/** 新建一个角色，并切到它。 */
export function addCharacter() {
  if (!state.data) return null
  if (state.data.characters.length >= 12) {
    say('最多 12 个角色。', { kind: 'warn' })
    return null
  }
  const index = state.data.characters.length + 1
  const session = blankSession()
  session.messages.push({
    id: uid('welcome'),
    role: 'assistant',
    text: '你好，我是新角色。',
    audio_url: '',
    created: Date.now(),
  })
  /** @type {Character} */
  const char = {
    id: uid('char'),
    name: `角色 ${index}`,
    intro: '',
    persona: '',
    voice_id: '',
    voice_name: '',
    voice_engine: '',
    voice_engines: [],
    voice_cover: '',
    voice_local: false,
    voice_sample_mode: 'emotion',
    voice_sample_file: '',
    background: '',
    portrait: '',
    portrait_x: PORTRAIT_X,
    portrait_y: PORTRAIT_Y,
    portrait_scale: PORTRAIT_SCALE,
    sessions: [session],
    active_session: session.id,
  }
  state.data.characters.push(char)
  state.data.active_id = char.id
  scheduleSave()
  return char
}

/**
 * 用左栏那张音色卡新建一个角色：名字与介绍取自音色卡，声音就是它。
 *
 * - 介绍 = 音色卡的描述（用户要"音色卡的描述自动填入介绍"）；
 * - 引擎默认「自动」：每次从此刻有绿点的本地引擎里挑，云端不参与。
 * - 人设留空，纸上那颗「AI 生成」按名字 + 介绍现写（不在创建时偷偷花一次模型调用）。
 *
 * @param {any} voice 左栏音色卡对象
 * @returns {Character|null}
 */
export function createFromVoice(voice) {
  if (!state.data) return null
  if (state.data.characters.length >= 12) {
    say('最多 12 个角色。', { kind: 'warn' })
    return null
  }
  const engines = /** @type {string[]} */ (
    Array.isArray(voice?.engines) ? voice.engines.map(String).filter(Boolean) : []
  )
  const session = blankSession()
  /** @type {Character} */
  const char = {
    id: uid('char'),
    name: String(voice?.shown_name || voice?.name || '新角色').trim().slice(0, 24) || '新角色',
    intro: String(voice?.description || '').replace(/\s+/g, ' ').trim().slice(0, 160),
    persona: '',
    voice_id: String(voice?.id || ''),
    voice_name: String(voice?.shown_name || voice?.name || ''),
    voice_engine: engines.length ? 'auto' : '',
    voice_engines: engines,
    voice_cover: String(voice?.cover_url || ''),
    voice_local: Boolean(voice?.is_local),
    voice_sample_mode: 'emotion',
    voice_sample_file: '',
    background: '',
    portrait: '',
    portrait_x: PORTRAIT_X,
    portrait_y: PORTRAIT_Y,
    portrait_scale: PORTRAIT_SCALE,
    sessions: [session],
    active_session: session.id,
  }
  state.data.characters.push(char)
  state.data.active_id = char.id
  state.sheetNew = true
  state.sheetOpen = true
  state.personaError = ''
  scheduleSave()
  void saveNow()
  return char
}

/**
 * 让对话模型按姓名 + 介绍写一段人设，直接落到角色上。
 * 返回写好的文本；失败返回空串（不挡别的事，界面上说原因）。
 *
 * @param {Character|null} [char]
 */
export async function generatePersona(char = active.value) {
  const target = char
  if (!target) return ''
  const name = String(target.name || '').trim()
  const intro = String(target.intro || '').trim()
  if (!name) {
    say('先给角色起个名字，再让人设引擎写。', { kind: 'warn' })
    return ''
  }
  if (!effectiveProviderKey()) {
    say('还没有可用的对话模型：先到「引擎 → 对话模型」配一家。', { kind: 'warn' })
    return ''
  }
  state.personaBusy = true
  state.personaError = ''
  try {
    const result = await hostApi.llmChat({
      provider: effectiveProviderKey(),
      model: state.data?.llm_model || '',
      messages: [
        {
          role: 'system',
          content:
            '你在给一段角色对话写人设。用第二人称「你」，写清她是谁、怎么说话、' +
            '和对方是什么关系、有什么习惯与边界。只输出人设正文，不要标题、列表或引号，' +
            '控制在 160 字以内。',
        },
        { role: 'user', content: `名字：${name}\n介绍：${intro || '（还没有介绍）'}` },
      ],
      temperature: 0.9,
      max_tokens: 360,
    })
    const text = String(result?.text || '').trim()
    if (!text) throw new Error('模型没有返回人设。')
    // await 期间角色可能已被删：只改还活着的那一个。
    const found = state.data?.characters.find((item) => item.id === target.id)
    if (!found) return ''
    found.persona = text.slice(0, 1200)
    scheduleSave()
    return found.persona
  } catch (error) {
    state.personaError = errorText(error)
    say(`人设生成失败：${state.personaError}`, { kind: 'error' })
    return ''
  } finally {
    state.personaBusy = false
  }
}

/** @param {string} id */
export function removeCharacter(id) {
  if (state.busy) return
  if (!state.data || state.data.characters.length <= 1) return
  const at = state.data.characters.findIndex((item) => item.id === id)
  if (at < 0) return
  const [dropped] = state.data.characters.splice(at, 1)
  if (dropped) {
    forgetAudio(
      dropped.sessions.flatMap((row) => row.messages.map((message) => message.audio_url))
    )
  }
  if (state.data.active_id === id) {
    const fallback = state.data.characters[Math.max(0, at - 1)] || state.data.characters[0]
    state.data.active_id = fallback.id
    state.speakingId = ''
    state.lastReplyId = ''
  }
  scheduleSave()
}

/** 关掉当前留在画面上的回复气泡（不打断正在播放的语音）。 */
export function dismissBubble() {
  state.lastReplyId = ''
}

/** 改当前角色的字段；界面上的编辑都走这里。 */
/** @param {Partial<Character>} patch */
export function patchActive(patch) {
  const char = active.value
  if (!char) return
  Object.assign(char, patch)
  scheduleSave()
}

/** 左栏「指定」交给当前角色。 */
/**
 * @param {any} voice
 */
export async function assignVoiceToActive(voice) {
  const char = active.value
  if (!char) return false
  const engines = /** @type {string[]} */ (
    Array.isArray(voice?.engines) ? voice.engines.map(String).filter(Boolean) : []
  )
  // 一律从「自动」起步：按此刻有绿点的本地引擎现算，云端不参与。
  char.voice_id = String(voice?.id || '')
  char.voice_name = String(voice?.shown_name || voice?.name || '')
  char.voice_engine = engines.length ? 'auto' : ''
  char.voice_engines = engines
  char.voice_cover = String(voice?.cover_url || '')
  char.voice_local = Boolean(voice?.is_local)
  char.voice_sample_mode = 'emotion'
  char.voice_sample_file = ''
  scheduleSave()
  await saveNow()
  return true
}

// ---- 保存 -------------------------------------------------------------- //

export function scheduleSave() {
  if (!state.data) return
  clearTimeout(saveTimer)
  saveTimer = window.setTimeout(() => {
    saveTimer = 0
    void saveNow()
  }, 350)
}

export async function saveNow() {
  if (!state.data) return
  if (saveInFlight) {
    saveAgain = true
    return
  }
  saveInFlight = true
  state.saving = true
  const sent = clone(state.data)
  try {
    const saved = await api.saveState(sent)
    // 只把服务端规范化过的字段合回来：保存期间新打的字、新翻的记录不能被旧快照擦掉。
    applySaved(saved, sent)
    state.error = ''
  } catch (error) {
    state.error = errorText(error)
    say(`对话保存失败：${state.error}`, { kind: 'error' })
  } finally {
    saveInFlight = false
    state.saving = false
    if (saveAgain) {
      saveAgain = false
      void saveNow()
    }
  }
}

/** 服务端回包 → 本地状态，逐字段合并。
 *
 *  - 角色 / 记录在我们提交之后又改过的，保留本地值；
 *  - 标量字段（名字、背景、音色、模型……）本地没动过才接受服务端规范化；
 *  - 记录列表本地没动过才整块接受服务端的裁剪与 id 去重；
 *  - 提交之后新加的角色 / 记录不会被回包删掉。
 *
 * @param {any} saved
 * @param {any} sent
 */
function applySaved(saved, sent) {
  if (!state.data || !saved || !Array.isArray(saved.characters)) return
  const local = state.data
  const sentChars = /** @type {Character[]} */ (
    Array.isArray(sent.characters) ? sent.characters : []
  )
  const savedChars = /** @type {any[]} */ (saved.characters)
  /** @type {Map<string, Character>} */
  const sentById = new Map(sentChars.map((item) => [item.id, item]))
  /** @type {Map<string, any>} */
  const savedById = new Map(savedChars.map((item) => [item.id, item]))
  if (typeof saved.active_id === 'string' && saved.active_id) {
    const localActive = local.active_id
    const sentActive = sent.active_id
    if (localActive === sentActive && savedById.has(saved.active_id)) {
      local.active_id = saved.active_id
    }
  }
  // 服务端只会在形状非法时丢角色；提交后本地又加的角色不跟着丢。
  local.characters = local.characters.filter(
    (item) => savedById.has(item.id) || !sentById.has(item.id)
  )
  // 对话模型挂在整份状态上：本地没动过就接受服务端那份。
  if (local.llm_provider === sent.llm_provider) local.llm_provider = saved.llm_provider || ''
  if (local.llm_model === sent.llm_model) local.llm_model = saved.llm_model || ''
  /** @type {(keyof Character)[]} */
  const scalar = [
    'name',
    'intro',
    'persona',
    'voice_id',
    'voice_name',
    'voice_engine',
    'voice_engines',
    'voice_cover',
    'voice_local',
    'voice_sample_mode',
    'voice_sample_file',
    'background',
    'portrait',
    'portrait_x',
    'portrait_y',
    'portrait_scale',
  ]
  for (const item of local.characters) {
    const server = savedById.get(item.id)
    const before = sentById.get(item.id)
    if (!server || !before) continue
    for (const key of scalar) {
      if (same(item[key], before[key])) /** @type {any} */ (item)[key] = server[key]
    }
    if (same(item.sessions, before.sessions)) {
      item.sessions = server.sessions
      if (item.active_session === before.active_session) {
        item.active_session = server.active_session
      }
    }
  }
}

// ---- 模型清单 ---------------------------------------------------------- //

/** @param {string} key */
export function providerOf(key) {
  return state.providers.find((item) => item.key === key) || null
}

/** 当前实际会用哪家：状态里选的那家，空 = 宿主的默认。 */
export function effectiveProviderKey() {
  return state.data?.llm_provider || hostStore.llm?.key || ''
}

export function effectiveProvider() {
  return providerOf(effectiveProviderKey()) || providerOf(hostStore.llm?.key) || null
}

export function providerLabel() {
  return effectiveProvider()?.label || '宿主默认'
}

export function providerNote() {
  const provider = effectiveProvider()
  if (!provider) return '没有已启用的对话模型。先到「引擎 → 对话模型」里配一家。'
  if (provider.ready) return ''
  return provider.note || `${provider.label} 还没配好，先去「引擎 → 对话模型」检查。`
}

export function modelHint() {
  const provider = effectiveProvider()
  if (!provider) return '未配置对话模型'
  return String(state.data?.llm_model || '').trim() || '服务默认模型'
}

/** 按钮宽度只够放模型名，服务来源放在悬停提示里。 */
export function modelFullHint() {
  const provider = effectiveProvider()
  if (!provider) return '未配置对话模型'
  const model = String(state.data?.llm_model || '').trim()
  return model ? `${provider.label} · ${model}` : `${provider.label} · 服务默认模型`
}

/** 切服务：清掉旧模型，按新服务重拉清单。整份状态共用，换角色不跟着变。 */
/** @param {string} key */
export function setProvider(key) {
  if (!state.data) return
  state.data.llm_provider = key || ''
  state.data.llm_model = ''
  scheduleSave()
  void syncModels()
}

/** @param {string} model */
export function setModel(model) {
  if (!state.data) return
  state.data.llm_model = model || ''
  scheduleSave()
}

/** 拉当前服务的模型清单；服务不提供清单时留空，界面改用文本框。 */
export async function syncModels() {
  const key = effectiveProviderKey()
  if (!key) {
    state.models = []
    state.modelsReason = ''
    return
  }
  state.modelsBusy = true
  try {
    const result = await hostApi.llmModels(key)
    // 用户可能在请求期间换了服务：只接受仍然对得上的结果。
    if (key !== effectiveProviderKey()) return
    state.models = Array.isArray(result?.items) ? result.items : []
    state.modelsReason = result?.ok ? '' : String(result?.reason || '')
  } catch (error) {
    state.models = []
    state.modelsReason = errorText(error)
  } finally {
    state.modelsBusy = false
  }
}

// ---- 一轮对话 ---------------------------------------------------------- //

/** 模型上下文：角色人设 + **这一份记录里**最近的对话。换一份记录就是另起一段话头。 */
/** @param {Character} char */
function contextMessages(char) {
  const persona =
    char.persona.trim() || '你是一个简洁、自然、口语化的聊天伙伴。'
  const system =
    `${persona}\n你是${char.name}。回答保持一两句话，语言跟用户一致，` +
    '适合直接念出来；不要写动作、旁白、Markdown 或括号说明。'
  const history = messages.value
    .filter((message) => !message.error && message.text.trim())
    .slice(-18)
    .map((message) => ({ role: message.role, content: message.text }))
  return [{ role: 'system', content: system }, ...history]
}

/** 送进 TTS 前去掉 Markdown 记号：念出来不该带上星号与反引号。 */
/** @param {string} text */
function speechText(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[*_#>~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** @param {string} url @param {string} label */
function playUrl(url, label) {
  if (!url) return
  hostActions.playAll([{ url, label, sub: '回复' }], 0)
}

/** 合成一条回复并用宿主播放条放出来；失败只记在这一条上，不把整轮对话判死。 */
/**
 * @param {Character} char
 * @param {ChatMessage} message
 */
export async function speak(char, message) {
  const charId = String(char?.id || '')
  const messageId = String(message?.id || '')
  const text = speechText(message?.text)
  if (!charId || !messageId || !char.voice_id || !text) return false
  if (!findCharacter(charId) || !findMessage(messageId)) return false
  // 自动档每次现算："哪个开着用哪个"，换引擎不用改角色。
  const engine = resolveEngine(char)
  if (!engine) {
    // 没有可发的引擎是这一句的事，不是整页的事：写在行上，不弹一个每句都来的错。
    message.audio_url = ''
    message.audio_error = '没有可用的合成引擎：在角色设置里选一台可用的引擎（自动档只挑本地且实例在跑的）。'
    scheduleSave()
    return false
  }
  state.synthesizingId = messageId
  let uploaded = ''
  try {
    /** @type {Record<string, any>} */
    const payload = {
      engine,
      voice_id: char.voice_id,
      text,
    }
    // 本地音色才谈得上"用哪一段参考音频"：固定样例钉一条，情绪档让网关自己匹配。
    if (isLocalVoice(char)) {
      if (char.voice_sample_mode === 'fixed' && char.voice_sample_file) {
        payload.sample_file = char.voice_sample_file
      } else {
        payload.match_emotion = true
      }
    }
    // persist="none"：宿主不写全局缓存，字节直接回到这里。
    const blob = await api.synthesizeAudio(payload)
    // 合成期间记录 / 角色可能已被删：不再落盘，也不再上屏。
    if (!findCharacter(charId) || !findMessage(messageId)) return false
    const saved = await api.uploadAudio(blob)
    uploaded = String(saved?.url || '')
    if (!uploaded) throw new Error('合成没有返回音频。')
    const latest = findMessage(messageId)
    if (!latest || !findCharacter(charId)) {
      void api.deleteAudio(uploaded).catch(() => {})
      return false
    }
    const previous = latest.audio_url
    latest.audio_url = uploaded
    delete latest.audio_error
    if (previous && previous !== uploaded) forgetAudio([previous])
    scheduleSave()
    state.speakingId = messageId
    playUrl(latest.audio_url, char.name)
    return true
  } catch (error) {
    // 已经删掉的记录没有地方写错误，也不再打扰当前页。
    if (!findCharacter(charId) || !findMessage(messageId)) return false
    message.audio_url = ''
    message.audio_error = errorText(error)
    scheduleSave()
    say(`语音合成失败：${message.audio_error}`, { kind: 'error' })
    return false
  } finally {
    // 只清自己那一条：期间可能已经换了一条在合成。
    if (state.synthesizingId === messageId) state.synthesizingId = ''
  }
}

/** 发一条文字：先显示用户消息，再等模型，最后合成。 */
/** @param {string} text */
export async function send(text) {
  const content = String(text ?? '').trim()
  if (!content || state.busy) return
  const char = active.value
  const target = session.value
  if (!char || !target) return
  const charId = char.id
  const sessionId = target.id
  state.chatError = ''
  state.draft = ''
  state.busy = 'thinking'
  state.speakingId = ''
  state.lastReplyId = ''
  /** @type {ChatMessage} */
  const userMessage = {
    id: uid('msg'),
    role: 'user',
    text: content,
    audio_url: '',
    created: Date.now(),
  }
  target.messages.push(userMessage)
  trimMessages(target)
  scheduleSave()
  try {
    const result = await hostApi.llmChat({
      provider: effectiveProviderKey(),
      model: state.data?.llm_model || '',
      messages: contextMessages(char),
      temperature: 0.8,
      max_tokens: 320,
    })
    // 模型在想的这段时间里，角色或这份记录可能已经被删：回包直接丢弃。
    const latestChar = findCharacter(charId)
    const latestSession = latestChar ? findSession(latestChar, sessionId) : null
    if (!latestChar || !latestSession) return
    const reply = String(result?.text || '').trim()
    if (!reply) throw new Error('模型没有返回文字。')
    /** @type {ChatMessage} */
    const assistant = {
      id: uid('msg'),
      role: 'assistant',
      text: reply,
      audio_url: '',
      created: Date.now(),
    }
    latestSession.messages.push(assistant)
    trimMessages(latestSession)
    if (active.value?.id === charId && session.value?.id === sessionId) {
      state.lastReplyId = assistant.id
    }
    scheduleSave()
    if (latestChar.voice_id) {
      state.busy = 'speaking'
      await speak(latestChar, assistant)
    }
  } catch (error) {
    const detail = errorText(error)
    // 记录已经没了就不再往任何地方写失败行；当前页也不用弹一条与它无关的错。
    const latestChar = findCharacter(charId)
    const latestSession = latestChar ? findSession(latestChar, sessionId) : null
    if (!latestChar || !latestSession) return
    state.chatError = detail
    /** @type {ChatMessage} */
    const failed = {
      id: uid('msg'),
      role: 'assistant',
      // 只说原因；"出错了"这层说法由台本上的标签给，别在正文里说两遍。
      text: detail,
      error: true,
      audio_url: '',
      created: Date.now(),
    }
    latestSession.messages.push(failed)
    trimMessages(latestSession)
    scheduleSave()
    say(detail, { kind: 'error' })
  } finally {
    state.busy = ''
  }
}

export function sendDraft() {
  void send(state.draft)
}

/** 重新合成某条回复（例如换了音色或上次 TTS 失败）。 */
/** @param {ChatMessage} message */
export async function retryAudio(message) {
  if (!message || state.synthesizingId || state.busy) return
  const char = active.value
  if (!char) return
  const previous = state.busy
  state.busy = 'speaking'
  try {
    await speak(char, message)
  } finally {
    // 只恢复自己占用的那种忙碌；期间状态被别人改写就不动它。
    if (state.busy === 'speaking') state.busy = previous
  }
}

/** @param {ChatMessage} message */
export function playMessage(message) {
  const char = active.value
  if (!message?.audio_url) return
  state.speakingId = message.id
  playUrl(message.audio_url, char?.name || '回复')
}

/** 播放条停了 / 换了别的曲目：台本上的高亮跟着熄。
 *  只看地址，不看 ``playing``：暂停时播放条仍指着这一条，高亮不该闪掉。 */
export function syncSpeaking() {
  if (!state.speakingId) return
  const player = hostStore.player
  const current = messages.value.find((row) => row.id === state.speakingId)
  if (!current || !player?.url || current.audio_url !== player.url) {
    state.speakingId = ''
  }
}

// ---- 对话的标题与日期 -------------------------------------------------- //

/** 一段对话叫什么：第一句**你说的**话。没有就按日期说，不再造一个假标题。 */
/** @param {Session|null} target */
export function sessionTitle(target = session.value) {
  if (!target) return '新对话'
  const first = target.messages.find((row) => row.role === 'user' && row.text.trim())
  if (!first) return '新对话'
  const line = first.text.replace(/\s+/g, ' ').trim()
  return line.length > 24 ? `${line.slice(0, 24)}…` : line
}

/** 记录的时间：今天 / 昨天 / 几月几日。列表与页头共用这一份说法。 */
/** @param {number|null|undefined} stamp */
export function sessionDate(stamp) {
  const date = new Date(Number(stamp) || Date.now())
  const now = new Date()
  const clock = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const days = Math.round((today - day) / 86_400_000)
  if (days <= 0) return `今天 ${clock}`
  if (days === 1) return `昨天 ${clock}`
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日`
  }
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`
}

/** 一份记录里"聊了几句"：只数说出口的话，不看失败提示。 */
/** @param {Session|null} target */
export function sessionCount(target = session.value) {
  if (!target) return 0
  return target.messages.filter((row) => !row.error).length
}

// ---- 语音输入与背景 ---------------------------------------------------- //

/** @param {string} text */
export function onDictation(text) {
  const clean = String(text || '').trim()
  if (!clean) return
  state.draft = state.draft.trim() ? `${state.draft.trim()} ${clean}` : clean
  state.focusTick += 1
}

/** 语音说完直接发送，不再落到草稿等手动确认。
 *  识别回来时若这一轮还在忙，先把文字留在草稿里并提示，避免丢话。 */
/** @param {string} text */
export function voiceSend(text) {
  const clean = String(text || '').trim()
  if (!clean) return
  if (state.busy) {
    state.draft = state.draft.trim() ? `${state.draft.trim()} ${clean}` : clean
    state.focusTick += 1
    say('上一条还在处理，这段文字已放进输入框。', { kind: 'info' })
    return
  }
  void send(clean)
}

// ---- 连续听写（服务端 VAD 流式识别） ------------------------------------ //

/** 麦克风常开的听写会话；空 = 没开。说话时段由服务端切，切一段发一段。 */
/** @type {import('../../api/asrStream').AsrStream | null} */
let streamSession = null
/** 正在 stop、还没等到收尾段的会话：它的 segment 仍要发出去。 */
/** @type {import('../../api/asrStream').AsrStream | null} */
let closingSession = null
/** start 还在连麦克风 / 连服务；listenStop 要等它落定，别让后台继续建链。 */
/** @type {Promise<void> | null} */
let startPromise = null

/**
 * 切换连续听写：开着就关，关着就开。
 * 打开失败（麦克风被拒、服务连不上）原话报错，状态回到"没开"。
 */
export async function listenToggle() {
  if (state.listening) {
    await listenStop()
    return
  }
  state.listening = 'connecting'
  // 先挂上会话再 start：start 期间服务端就可能推 error / onclose，
  // 不先挂上这些事件会被当成"没有会话"直接丢掉，界面永远停在连接中。
  const session = new AsrStream({
    onEvent: (event) => onStreamEvent(session, event),
    onClosed: () => {
      if (streamSession !== session) return
      streamSession = null
      state.listening = ''
      state.micSpeaking = false
      state.liveText = ''
    },
  })
  streamSession = session
  const pending = session.start()
  startPromise = pending
  try {
    await pending
    if (streamSession === session) state.listening = 'listening'
  } catch (error) {
    // 已经在连接途中被 listenStop 收掉：它负责收尾，不再报一次错。
    if (streamSession === session) {
      streamSession = null
      state.listening = ''
      say(error instanceof Error ? error.message : String(error), { kind: 'error' })
    }
  } finally {
    if (startPromise === pending) startPromise = null
  }
}

/** 关闭听写：把没说完的话冲出来（可能还发最后一段），再断开。
 *  连接还在进行时先立刻标记关闭，再等 start 落定——它每个 await 后都会自检。 */
export async function listenStop() {
  const session = streamSession
  if (session) closingSession = session
  streamSession = null
  const pending = startPromise
  startPromise = null
  state.micSpeaking = false
  state.liveText = ''
  state.listening = ''
  if (!session) {
    if (pending) await pending.catch(() => {})
    return
  }
  try {
    await session.stop()
    if (pending) await pending.catch(() => {})
  } finally {
    if (closingSession === session) closingSession = null
  }
}

/** @param {import('../../api/asrStream').AsrStream} session @param {any} event */
function onStreamEvent(session, event) {
  const live = session === streamSession
  const closing = session === closingSession
  // stop 之后仍可能收到收尾段：那正是用户刚说完、还没定稿的那一句，不能丢。
  if (closing) {
    if (event.type === 'error') {
      say(event.message || '流式识别出错。', { kind: 'error' })
      return
    }
    if (event.type !== 'segment') return
    state.micSpeaking = false
    state.liveText = ''
    if (event.error) {
      say(`识别失败：${event.error}`, { kind: 'error' })
      return
    }
    voiceSend(String(event.text || ''))
    return
  }
  // 既不是当前会话、也不是正在收尾的会话：迟到的旧事件，不当真。
  if (!live) return
  if (event.type === 'speech') {
    state.micSpeaking = true
    state.liveText = ''
    // 打断（barge-in）：AI 的回复正在放、用户开口了 → 立刻停播。
    // 只认本插件灌进播放条的曲目（sub 固定 '回复'），别的页面在放的书、
    // 试听不受影响；这段话随后识别完，照常走 voiceSend 开新一轮。
    // 回声抑制（getUserMedia echoCancellation）管着 AI 自己的声音，
    // 正常不会把自己误判成"有人说话"。
    if (hostStore.player?.playing && hostStore.player.sub === '回复') {
      hostActions.stop()
    }
    return
  }
  if (event.type === 'partial') {
    // 边说边出字：部分结果只管展示，定稿以 segment 为准。
    const text = String(event.text || '').trim()
    if (text) state.liveText = text
    return
  }
  if (event.type === 'segment') {
    state.micSpeaking = false
    state.liveText = ''
    if (event.error) {
      say(`识别失败：${event.error}`, { kind: 'error' })
      return
    }
    voiceSend(String(event.text || ''))
    return
  }
  if (event.type === 'error') {
    say(event.message || '流式识别出错。', { kind: 'error' })
    void listenStop()
  }
  // ready 在 start 里同步等过；done 由 stop 自己消费。
}

/** @param {string} key ``custom:<name>`` 或空（回到默认底色） */
export function setBackground(key) {
  const char = active.value
  if (!char) return
  char.background = key || ''
  scheduleSave()
}

/** 上传一张图并立即换到当前角色；图存在插件数据目录，不进浏览器存储。 */
/** @param {File} file */
export async function uploadBackground(file) {
  if (!file) return
  if (!String(file.type || '').startsWith('image/')) {
    say('只接受图片文件。', { kind: 'warn' })
    return
  }
  state.backgroundBusy = true
  try {
    const payload = await api.uploadBackground(file)
    setBackground(String(payload?.key || ''))
    await saveNow()
    say('背景已换上。', { kind: 'success' })
  } catch (error) {
    say(`上传背景失败：${errorText(error)}`, { kind: 'error' })
  } finally {
    state.backgroundBusy = false
  }
}

/**
 * 取当前角色那条音色的样例，给角色设置里的「样例」用。
 *
 * 顺带把服务端此刻算出的 `engines` 写回角色：自动档按"现在开着哪台"走，
 * 新启用的引擎下次就能被挑到，不必重建角色。
 *
 * ``force``：打开设置、切到固定样例时强制重取一次。缓存过一次失败（空数组）
 * 不能一直沿用，否则样例区永远空着，看着就像"固定样例不能选"。
 *
 * @param {boolean} [force]
 */
export async function loadVoiceSamples(force = false) {
  const char = active.value
  const id = String(char?.voice_id || '')
  if (!char || !id) {
    state.voiceSamples = []
    state.voiceSamplesFor = ''
    return []
  }
  if (!force && state.voiceSamplesFor === id) return state.voiceSamples
  state.voiceSamplesBusy = true
  try {
    const detail = await hostApi.voice(id)
    state.voiceSamples = Array.isArray(detail?.samples) ? detail.samples : []
    state.voiceSamplesFor = id
    const engines = Array.isArray(detail?.engines)
      ? detail.engines.map(String).filter(Boolean)
      : []
    const now = state.data?.characters.find((item) => item.id === char.id)
    if (now) {
      let changed = false
      // 旧状态没有 voice_local：详情里这一位就是权威值，顺手补上。
      const local = Boolean(detail?.is_local)
      if (now.voice_local !== local) {
        now.voice_local = local
        changed = true
      }
      if (!same(now.voice_engines, engines)) {
        now.voice_engines = engines
        changed = true
      }
      // 用户明确选的那台：只要这条音色还落得到它、它自身也可用（云端照样算），就留着。
      // 落不到或真不能用了才回到自动，别让下一句必然失败。
      if (
        now.voice_engine &&
        now.voice_engine !== 'auto' &&
        !(engines.includes(now.voice_engine) && engineSendable(engineOf(now.voice_engine)))
      ) {
        now.voice_engine = now.voice_id ? 'auto' : ''
        changed = true
      }
      if (changed) scheduleSave()
    }
    return state.voiceSamples
  } catch {
    // 扫不到样例不影响聊天：合成时网关会退回主样例。不缓存失败，下次再取。
    state.voiceSamples = []
    state.voiceSamplesFor = ''
    return []
  } finally {
    state.voiceSamplesBusy = false
  }
}

/** 四张随包分发的房间：书房、雨夜、晨光、霓虹。第一张是没选过时的默认。 */
export const BACKGROUNDS = [
  { key: 'study', label: '书房', image: bgStudy, position: '28% 34%' },
  { key: 'rain', label: '雨夜', image: bgRain, position: '30% 34%' },
  { key: 'dawn', label: '晨光', image: bgDawn, position: '26% 34%' },
  { key: 'neon', label: '霓虹', image: bgNeon, position: '32% 34%' },
]

/**
 * 十一张随包立绘：各家模型的拟人形象，透明底，站在房间左下。
 * 后两张是同一位画手的备选稿（Claude 的 Q 版、GLM 的轮椅版），和正片一样可选。
 * `background` 是这张立绘配好的默认房间——选人时一并换上，省得再搭一次。
 */
export const PORTRAITS = [
  { key: 'chatgpt', label: 'ChatGPT', image: portraitFile('chatgpt'), background: 'builtin:dawn' },
  { key: 'claude', label: 'Claude', image: portraitFile('claude'), background: 'builtin:study' },
  {
    key: 'claude-chibi',
    label: 'Claude·Q版',
    image: portraitFile('claude-chibi'),
    background: 'builtin:study',
  },
  { key: 'copilot', label: 'Copilot', image: portraitFile('copilot'), background: 'builtin:neon' },
  { key: 'deepseek', label: 'DeepSeek', image: portraitFile('deepseek'), background: 'builtin:rain' },
  { key: 'gemini', label: 'Gemini', image: portraitFile('gemini'), background: 'builtin:neon' },
  { key: 'glm', label: 'GLM', image: portraitFile('glm'), background: 'builtin:study' },
  {
    key: 'glm-wheelchair',
    label: 'GLM·轮椅',
    image: portraitFile('glm-wheelchair'),
    background: 'builtin:study',
  },
  { key: 'kimi', label: 'Kimi', image: portraitFile('kimi'), background: 'builtin:dawn' },
  { key: 'minimax', label: 'MiniMax', image: portraitFile('minimax'), background: 'builtin:study' },
  { key: 'qwen', label: 'Qwen', image: portraitFile('qwen'), background: 'builtin:rain' },
].filter((item) => item.image)

/** 这个角色现在用的是哪张内置背景：``builtin:<key>``，没选过就是第一张。 */
/** @param {Character|null} char */
export function backgroundOf(char) {
  const raw = String(char?.background || '')
  const key = raw.startsWith('builtin:') ? raw.slice('builtin:'.length) : ''
  return BACKGROUNDS.find((row) => row.key === key) || BACKGROUNDS[0]
}

/** 这个角色站在谁面前：没选立绘就是空（只有房间，没有人）。 */
/** @param {Character|null} char */
export function portraitOf(char) {
  const raw = String(char?.portrait || '')
  const key = raw.startsWith('builtin:') ? raw.slice('builtin:'.length) : ''
  return PORTRAITS.find((row) => row.key === key) || null
}

/** 立绘的图片地址；没选就是空串（模板据此决定要不要那张 img）。 */
/** @param {Character|null} char */
export function portraitSrc(char) {
  return portraitOf(char)?.image || ''
}

/** 换立绘：顺手换上它配好的房间，再让人自己改背景。 */
/** @param {string} key 立绘 key；空串 = 不站人 */
export function setPortrait(key) {
  const char = active.value
  if (!char) return
  const row = PORTRAITS.find((item) => item.key === key)
  char.portrait = row ? `builtin:${row.key}` : ''
  if (row) char.background = row.background
  scheduleSave()
}

/** 立绘回到默认站位与大小（拖动 / 滚轮调乱之后的复位）。 */
export function resetPortraitTransform() {
  patchActive({
    portrait_x: PORTRAIT_X,
    portrait_y: PORTRAIT_Y,
    portrait_scale: PORTRAIT_SCALE,
  })
}

/** 背景：用户上传的图优先，否则四张内置图之一（不再有纯黑底那一档）。 */
/** @param {Character|null} char */
export function backgroundStyle(char) {
  const raw = String(char?.background || '')
  if (raw.startsWith('custom:')) {
    return {
      backgroundImage: `url("${api.backgroundUrl(raw.slice('custom:'.length))}")`,
      backgroundPosition: 'center',
    }
  }
  const found = backgroundOf(char)
  return {
    backgroundImage: `url("${found.image}")`,
    backgroundPosition: found.position,
  }
}

/** @param {string} key */
export function engineOf(key) {
  return (hostStore.engines || []).find((item) => item.key === key) || null
}

/** @param {string} key */
export function engineLabel(key) {
  if (!key) return ''
  const engine = engineOf(key)
  return engine?.label || key
}

/**
 * 这台引擎此刻能不能合成：后端算好的 ``available`` 是唯一判据（云端看密钥，本地看实例）。
 * 用户显式钉住某台时照这个发——云端按量计费由用户自己点头，不在这里替他改。
 * @param {any} engine
 */
function engineSendable(engine) {
  return Boolean(engine?.available)
}

/**
 * 自动档的候选判据：本地 + 此刻可用。云端按量计费，不能悄悄被自动选中。
 * @param {any} engine
 */
function engineUsable(engine) {
  return engineSendable(engine) && engine.kind !== 'cloud'
}

/** 这条音色可用的引擎里，此刻真有绿点的那几台。 */
/** @param {Character|null} char */
export function usableEngineKeys(char) {
  const keys = Array.isArray(char?.voice_engines) ? char.voice_engines : []
  return keys.filter((key) => engineUsable(engineOf(key)))
}

/**
 * 「自动」此刻落在哪台：这条音色可用的引擎里，挑一台**此刻可用（绿点）的本地引擎**。
 *
 * - 云端引擎按量计费，不参与自动；要花钱由用户在下拉里明确选那台。
 * - 本地引擎光填了地址不算数：实例没起就是不能用，不能拿它顶上去。
 * - 一台都没有就返回空串，由界面说清"没有可用的本地引擎"。
 *
 * @param {Character|null} char
 */
export function resolveAutoEngine(char) {
  return usableEngineKeys(char)[0] || ''
}

/** 这次真正发给哪台引擎：`auto` 现算；用户钉住的那台照发，不可用时才回落。 */
/** @param {Character|null} char */
export function resolveEngine(char) {
  if (!char) return ''
  if (char.voice_engine === 'auto') return resolveAutoEngine(char)
  const pinned = String(char.voice_engine || '')
  if (!pinned) return ''
  // 云端也按用户的选择发：按量计费是他自己点的头，不在这里替他省钱。
  // 只有真不可用（停用 / 没配好 / 实例没跑）才回落到自动档。
  return engineSendable(engineOf(pinned)) ? pinned : resolveAutoEngine(char)
}

/**
 * 这条音色是不是本地音色（只有本地音色才有"用哪段样例"这一层）。
 *
 * 新角色会把 `voice_local` 存下来；旧状态没这个字段时按引擎形态推断：
 * 一条音色能落到本地引擎，它就来自音色库的本地目录。
 *
 * @param {Character|null} char
 */
export function isLocalVoice(char) {
  if (!char?.voice_id) return false
  if (char.voice_local) return true
  return (Array.isArray(char.voice_engines) ? char.voice_engines : []).some((key) => {
    const engine = engineOf(key)
    return Boolean(engine) && engine.kind !== 'cloud'
  })
}

/**
 * 角色设置里引擎候选：这条音色能落到的引擎都列，云端也列——按量计费由用户自己选。
 * ``available`` 仍是后端算好的绿点；自动档另有判据，不会挑云端。
 */
/** @param {Character|null} char */
export function engineChoices(char) {
  const keys = Array.isArray(char?.voice_engines) ? char.voice_engines : []
  /** @type {any[]} */
  const out = []
  for (const key of keys) {
    const engine = engineOf(key)
    // 已停用 / 已找不到的不列——列出来只会让人选了才发现用不了。
    if (!engine) continue
    out.push({
      key,
      label: engine.label || key,
      kind: engine.kind,
      available: engineSendable(engine),
      note: engine.available ? '' : engine.note || '没有实例在跑',
    })
  }
  return out
}

/** @param {Character|null} char */
export function voiceLabel(char) {
  if (!char?.voice_id) return '未指定音色'
  if (char.voice_engine === 'auto' || !engineUsable(engineOf(char.voice_engine))) {
    const key = resolveAutoEngine(char)
    const which = key ? `（${engineLabel(key)}）` : '（没有可用引擎）'
    return `${char.voice_name || '已指定音色'} · 自动${which}`
  }
  return `${char.voice_name || '已指定音色'} · ${engineLabel(char.voice_engine)}`
}

/** 名牌上那一句：这台词的介绍，不是给模型的指令。没写就明说没写，不编一句。 */
/** @param {Character|null} char */
export function introLine(char) {
  const text = String(char?.intro || '').replace(/\s+/g, ' ').trim()
  return text || '还没有写下她是谁。'
}
