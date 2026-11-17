// @ts-check
// 壳层自己那点事：主题、筛选的留存、一闪而过的提示。
//
// 它们不属于任何一域——「说一句话」（`say`）左栏 / 引擎 / 生成都要用，主题更是全页面
// 的事——所以单独摆在这儿，各域从这里引。
import { api } from '../api'
import { store } from './state'

const THEME_KEY = 'vh-theme'
const THEME_MODE_KEY = 'vh-theme-mode'
const FILTERS_KEY = 'vh-filters'

const THEME_MODES = ['light', 'dark', 'system']

function prefersDark() {
  return Boolean(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
}

/** 把主题写到 <html data-theme>——CSS 里只有一堆变量覆盖，组件不用知道主题。
 *  `themeMode` 是用户的选择（可能为 system），`theme` 是解析后的实际值。 */
export function applyTheme() {
  const mode = THEME_MODES.includes(store.themeMode) ? store.themeMode : 'light'
  const theme = mode === 'system' ? (prefersDark() ? 'dark' : 'light') : mode
  const root = document.documentElement
  const changed = root.dataset.theme !== theme
  // 换肤先摘过渡：主题一翻，几十条规则（卡片 / 导航 / 左列 / 顶栏 / 按钮……）的
  // background、border-color、box-shadow 会**同时**开始跑 190ms——弱机上就是一整页
  // 持续重绘的卡顿。摘掉之后这一下变成一次性换肤；hover 那些交互过渡在下一帧照旧。
  if (changed) root.dataset.themeSwitching = ''
  root.dataset.theme = theme
  store.theme = theme
  if (changed) {
    // 双 rAF：等新主题算过样式、画过一帧，再恢复过渡。
    // （同一帧里摘掉标记会和换主题被合并成一次重算，过渡照样跑起来。）
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        delete root.dataset.themeSwitching
      })
    })
  }
}

/** 设定主题偏好（'light' | 'dark' | 'system'）。设置页与左栏按钮都从这里走。 */
/** @param {string} mode */
export function setThemeMode(mode) {
  store.themeMode = THEME_MODES.includes(mode) ? mode : 'light'
  try {
    localStorage.setItem(THEME_MODE_KEY, store.themeMode)
    // 兼容旧键：首屏内联脚本与老版本仍会读 vh-theme。
    localStorage.setItem(THEME_KEY, store.theme)
  } catch {
    /* 隐私模式下写不进去，本次会话仍然生效 */
  }
  applyTheme()
}

/** 启动时恢复上次选的主题；没存过就跟系统的深色偏好走。 */
export function initTheme() {
  let mode = ''
  try {
    mode = localStorage.getItem(THEME_MODE_KEY) || ''
    if (!mode) {
      const legacy = localStorage.getItem(THEME_KEY)
      mode = legacy === 'dark' || legacy === 'light' ? legacy : 'system'
    }
  } catch {
    mode = 'system'
  }
  store.themeMode = THEME_MODES.includes(mode) ? mode : 'system'
  applyTheme()
  // 跟随系统时，系统切主题要跟着变（只监听一次）。
  if (window.matchMedia) {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    if (media.addEventListener) {
      media.addEventListener('change', () => {
        if (store.themeMode === 'system') applyTheme()
      })
    }
  }
}

/** 启动时恢复上次的筛选（必须在第一次拉列表之前，否则会闪一下完整列表）。
 *  只认形状对得上的值：认不出来的整份丢掉、回到不筛选——半份筛选条件会让列表
 *  看起来是空的，而用户并不知道自己带着筛选。
 */
export function initFilters() {
  // 设置页「记住筛选条件」关掉时，直接回到不筛选（偏好由设置域写在这条键里，
  // 避免 shell 反向依赖 Pinia）。
  try {
    if (localStorage.getItem('vh-pref-remember-filters') === '0') {
      localStorage.removeItem(FILTERS_KEY)
      return
    }
  } catch {
    /* 忽略 */
  }
  let saved = null
  try {
    saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || 'null')
  } catch {
    saved = null
  }
  if (!saved || typeof saved !== 'object') return
  const keys = Object.keys(store.filters)
  if (!keys.every((key) => typeof saved[key] === 'string')) return
  for (const key of keys) store.filters[key] = saved[key]
  if (typeof saved.showHidden === 'boolean') store.showHidden = saved.showHidden
}

/** 写回筛选。出口只有 `loadVoices` 一个——筛选改动终归都要重新拉列表。
 *  全空就把键删掉，"从没筛过"与"筛过又清掉"才分得清。
 */
export function persistFilters() {
  try {
    if (localStorage.getItem('vh-pref-remember-filters') === '0') return
  } catch {
    /* 忽略 */
  }
  const empty = Object.keys(store.filters).every((key) => !store.filters[key])
  if (empty && store.showHidden) {
    localStorage.removeItem(FILTERS_KEY)
    return
  }
  localStorage.setItem(FILTERS_KEY, JSON.stringify({ ...store.filters, showHidden: store.showHidden }))
}

let noticeTimer = 0
let noticeDeadline = 0
let noticeRemaining = 0
let noticePaused = false

function clearNoticeTimer() {
  window.clearTimeout(noticeTimer)
  noticeTimer = 0
}

/** 按剩余时间重新排表。鼠标停在 toast 上时只记截止时间，不启动定时器。 */
/** @param {number} delay */
function scheduleNotice(delay) {
  clearNoticeTimer()
  noticeRemaining = Math.max(0, delay)
  noticeDeadline = Date.now() + noticeRemaining
  if (noticePaused) return
  noticeTimer = window.setTimeout(() => {
    store.notice = ''
    noticeTimer = 0
    noticeRemaining = 0
  }, noticeRemaining)
}

/** 鼠标移入 / 移出 toast：悬停时停表，移开按剩余时间续计。**只由那一颗气泡调用。** */
/** @param {boolean} paused */
export function setNoticePaused(paused) {
  if (paused === noticePaused) return
  noticePaused = paused
  if (!store.notice) return
  if (paused) {
    clearNoticeTimer()
    noticeRemaining = Math.max(0, noticeDeadline - Date.now())
  } else {
    scheduleNotice(noticeRemaining)
  }
}

/**
 * 一闪而过的那句话。**唯一的出处**——谁要报一句什么，都从这儿走。
 * `kind` 决定醒目程度（info / success / warn / error），默认 info；
 * 同时写进日志链路：界面看得见一次，日志里留得住一次。
 * @param {string} text
 * @param {{ kind?: string, level?: string }} [options]
 */
export function say(text, options = {}) {
  const message = String(text || '').trim()
  if (!message) return
  const requested = options.kind || 'info'
  const kind = ['info', 'success', 'warn', 'error'].includes(requested) ? requested : 'info'
  store.notice = message
  store.noticeKind = kind
  store.noticeAt = Date.now()
  scheduleNotice(kind === 'error' ? 5200 : 3200)
  const level =
    options.level || (kind === 'error' ? 'error' : kind === 'warn' ? 'warning' : 'info')
  void api.clientLog(message, level)
}

/** 打开「设置」页（右侧主区那一页，不是弹窗：`view` 就是这个状态，
 *  没有第二个同义布尔）。识别引擎不在设置页里——它是「引擎」页的一类
 *  插件，去那儿用 `openAsrEngine`。 */
export function openSettings() {
  store.view = 'settings'
}
