// 设置的单一数据源（Pinia）。
//
// 四类东西在一起，但保存路径不同：
// 1. 本机偏好（外观 / 音频默认值 / 快捷键 / 行为）：改了就生效，并即时写 localStorage
//    ——刷新回来还是这份草稿，不会因为没点「保存」就丢。
// 2. 扫描根目录：后端说了算（`PUT /api/v1/folders` 会顺手重扫），只有点「保存设置」才提交。
// 3. 存储位置：后端说了算（`PUT /api/v1/storage`），同样等「保存设置」。
// 4. 应用层识别选项：后端说了算（`PUT /api/v1/asr/options`），同样等「保存设置」；
//    能力开关按当前引擎的声明出现（见 AsrSection）。
//
// 「未保存」= 当前快照 ≠ 上次保存快照。两半都要算，否则改完目录不点保存也显示"已保存"。
import { computed, reactive, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { api } from '../api'
import { actions, say, setThemeMode, store as shellStore } from './index'

export type ThemeMode = 'light' | 'dark' | 'system'
export type AccentKey = 'azure' | 'indigo' | 'violet' | 'teal'
export type Density = 'compact' | 'cozy' | 'roomy'
/** 界面风格：classic 是最早的平面界面，glass 是玻璃拟态，paper 是纸墨编辑台，celadon 是素瓷青釉。 */
export type Look = 'classic' | 'glass' | 'paper' | 'celadon' | 'stage'
/** 左栏可见档：`both` = 目录树 + 音色栏；`cards` = 只有音色栏；`none` = 都收起。 */
export type RailVisibility = 'both' | 'cards' | 'none'
export type SectionKey =
  | 'general'
  | 'appearance'
  | 'shortcuts'
  | 'asr'
  | 'texts'
  | 'storage'
/** https 启动方式：`auto` 跟随监听地址（非回环即开），另两个是用户的明确选择。 */
export type HttpsMode = 'auto' | 'on' | 'off'
export type ShortcutAction =
  | 'openSettings'
  | 'toggleTheme'
  | 'focusSearch'
  | 'openFilters'
  | 'backToLibrary'
  | 'saveSettings'

export interface GeneralPrefs {
  autoScan: boolean
  rememberFilters: boolean
  showDescriptions: boolean
}

/** 应用层识别选项（「设置 → 语音识别」）。存在后端 ``config.asr_options``，
 *  与扫描根、存储位置一样走统一保存；`language_writeback` 只在当前引擎声明
 *  语种检测时出现。 */
export interface AsrPrefs {
  auto_transcript: boolean
  /** 识别服务连上后，自动把库里还没有逐字稿的样例补一批。 */
  auto_on_connect: boolean
  writeback: boolean
  transcribe_recordings: boolean
  language_writeback: boolean
}

export type AvatarSize = 'auto' | 'sm' | 'lg'
export type AvatarShape = 'auto' | 'round' | 'soft' | 'square'

/** 头像显示偏好。`show` 管图片/字母二选一，三个范围分开控制；
 *  大小与形状的 `auto` = 跟随各自原有样式。 */
export interface AvatarPrefs {
  show: boolean
  rail: boolean
  cards: boolean
  detail: boolean
  size: AvatarSize
  shape: AvatarShape
}

export interface AppearancePrefs {
  theme: ThemeMode
  look: Look
  accent: AccentKey
  glass: number
  density: Density
  radius: number
  ambient: boolean
  reduceMotion: boolean
  /** 主导航自动隐藏：不占宽度，鼠标移到左边缘滑出。 */
  navAutoHide: boolean
  /** 主导航只留图标，文字不占宽度。 */
  navHideLabels: boolean
  /** 左栏可见档：页内导航图标的重复点击在 both / cards / none 之间循环。 */
  railVisibility: RailVisibility
  /** 音色库标签条收起：标签太多时省出平板高度。 */
  tagBarCollapsed: boolean
  avatar: AvatarPrefs
}

export interface StorageDirs {
  output: string
  cache: string
  log: string
  asr: string
}

/** 转换产物的回收线；``0`` = 那条规则不启用（后端为准，这里只是离线时的回显值）。 */
export interface CachePolicy {
  limit_mb: number
  keep_days: number
}

export interface StorageEntry {
  key: string
  label: string
  path: string
  value: string
  size: number
  files?: number
  note: string
  editable: boolean
  locked_by?: string
}

export const SHORTCUT_LABELS: Record<ShortcutAction, string> = {
  openSettings: '打开设置',
  toggleTheme: '切换主题',
  focusSearch: '聚焦搜索',
  openFilters: '筛选音色',
  backToLibrary: '返回音色库',
  saveSettings: '保存设置',
}

/** 「筛选音色」发布前的历史默认键位：见到就换新默认，见 load() 里的说明。 */
const DEV_FILTER_KEYS = ['Ctrl+Shift+F', 'Alt+F']

const DEFAULTS = {
  general: {
    autoScan: true,
    rememberFilters: true,
    showDescriptions: true,
  } as GeneralPrefs,
  asr: {
    auto_transcript: true,
    auto_on_connect: false,
    writeback: true,
    transcribe_recordings: true,
    language_writeback: true,
  } as AsrPrefs,
  appearance: {
    theme: 'light',
    look: 'classic',
    accent: 'azure',
    glass: 72,
    density: 'cozy',
    radius: 18,
    ambient: true,
    reduceMotion: false,
    navAutoHide: false,
    navHideLabels: false,
    railVisibility: 'both',
    tagBarCollapsed: false,
    avatar: {
      show: true,
      rail: true,
      cards: true,
      detail: true,
      size: 'auto',
      shape: 'auto',
    },
  } as AppearancePrefs,
  shortcuts: {
    openSettings: 'Ctrl+,',
    toggleTheme: 'Ctrl+Shift+L',
    focusSearch: '/',
    openFilters: 'Alt+S',
    backToLibrary: 'Escape',
    saveSettings: 'Ctrl+S',
  } as Record<ShortcutAction, string>,
  storage: { scanRoots: [] as string[], dirs: { output: '', cache: '', log: '', asr: '' } as StorageDirs },
}

const AVATAR_SIZES: AvatarSize[] = ['auto', 'sm', 'lg']
const AVATAR_SHAPES: AvatarShape[] = ['auto', 'round', 'soft', 'square']
const RAIL_VISIBILITIES: RailVisibility[] = ['both', 'cards', 'none']

/** 存档里的头像段：逐项取用，旧版本带来的多余字段不进入新快照。 */
function normalizeAvatar(raw?: Partial<AvatarPrefs>): AvatarPrefs {
  return {
    show: raw?.show ?? true,
    rail: raw?.rail ?? true,
    cards: raw?.cards ?? true,
    detail: raw?.detail ?? true,
    size: raw?.size && AVATAR_SIZES.includes(raw.size) ? raw.size : 'auto',
    shape: raw?.shape && AVATAR_SHAPES.includes(raw.shape) ? raw.shape : 'auto',
  }
}

const STORE_KEY = 'vh-settings-v1'
const REMEMBER_KEY = 'vh-pref-remember-filters'

/** 存储条目 key → 表单字段。数据目录没有字段：它由环境变量决定，只读。 */
const STORAGE_FIELDS: Record<string, keyof StorageDirs> = {
  output: 'output',
  cache: 'cache',
  log: 'log',
  asr: 'asr',
}

/** 表单字段 → ``PUT /storage`` 认的名字（``output_dir`` / ``cache_dir`` / ``log_dir``）。
 *  发短名（``output``）会被 pydantic 当未知字段丢掉：接口照样回 200，配置一个字没动，
 *  界面还显示"已保存"——症状就是"改了目录保存不过去"。 */
const STORAGE_API_FIELDS: Record<string, string> = {
  output: 'output_dir',
  cache: 'cache_dir',
  log: 'log_dir',
}

/** 两个目录是不是同一个（Windows 上 ``F:/a`` 与 ``F:\a`` 是同一个，末尾斜杠不算）。 */
function sameDir(left: string, right: string): boolean {
  const fold = (value: string) => (value || '').trim().replace(/[\\/]+$/, '').replace(/\\/g, '/')
  return fold(left) === fold(right)
}

/** 后端存的是三态字符串；认不出（老进程没这一格）按 `auto` 显示。 */
function normalizeHttps(value: unknown): HttpsMode {
  return value === 'on' || value === 'off' ? value : 'auto'
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

interface PersistedSettings {
  general?: Partial<GeneralPrefs>
  appearance?: Partial<AppearancePrefs>
  shortcuts?: Partial<Record<ShortcutAction, string>>
  asr?: Partial<AsrPrefs>
  scanRoots?: string[]
  dirs?: Partial<StorageDirs>
}

function normalizeSnapshot(raw: unknown): PersistedSettings | null {
  if (!raw || typeof raw !== 'object') return null
  return raw as PersistedSettings
}

export const useSettingsStore = defineStore('voicehub-settings', () => {
  const general = reactive<GeneralPrefs>(clone(DEFAULTS.general))
  const appearance = reactive<AppearancePrefs>(clone(DEFAULTS.appearance))
  const shortcuts = reactive<Record<ShortcutAction, string>>(clone(DEFAULTS.shortcuts))
  const asr = reactive<AsrPrefs>(clone(DEFAULTS.asr))

  const scanRoots = ref<string[]>([])
  const dirs = reactive<StorageDirs>(clone(DEFAULTS.storage.dirs))
  const entries = ref<StorageEntry[]>([])
  const cachePolicy = reactive<CachePolicy>({ limit_mb: 512, keep_days: 30 })
  const asrLoaded = ref(false)

  /** 后端里当前生效的值——只提交真正改过的字段，空串表示回默认。 */
  const remoteSeed = reactive({
    roots: [] as string[],
    dirs: clone(DEFAULTS.storage.dirs),
    asr: clone(DEFAULTS.asr),
    listen: { lan: false, https: 'auto' as HttpsMode, token: '', autostart: false },
    sapi: { voiceId: '', isDefault: false },
  })

  /** 后端里当前生效的回收线：只提交改过的那一格。 */
  const remoteCache = reactive<CachePolicy>({ limit_mb: 0, keep_days: 0 })

  /** `version` 取自 /health：唯一可信的版本来源，前端不另存一份。 */
  const backend = reactive({
    checked: false,
    online: false,
    message: '',
    version: '',
  })

  /** 监听范围、https 与令牌。字段回答"现在生效的是什么"；改动只落盘，重启进程才变。 */
  const service = reactive({
    host: '',
    port: 0,
    lan: false,
    /** 配置里的 https 方式（`auto` / `on` / `off`）。 */
    https: 'auto' as HttpsMode,
    tokenSet: false,
    /** 输入框里的新令牌；提交后清空，从不回填已存的那份。 */
    token: '',
    /** 当前**生效**的暴露面（与上面的编辑值不同：改了要重启才算数）。 */
    liveLan: false,
    liveTokenSet: false,
    liveHttps: false,
    /** 后端报过 scheme 才谈得上生效值；老进程没有这一格，界面不猜。 */
    httpsKnown: false,
    /** 开机自启：只有桌面版（由启动器拉起）可设。 */
    autostart: false,
    autoSupported: false,
  })
  /** Windows SAPI 5 系统朗读桥接状态与配置；引擎固定自动路由。 */
  const sapi = reactive({
    supported: false,
    installed: false,
    isDefault: false,
    canInstall: false,
    voiceId: '',
    engine: 'auto',
    endpoint: '',
    busy: false,
  })
  const savedSnapshot = ref('')
  const saving = ref(false)
  const loaded = ref(false)

  function prefsSnapshot() {
    return JSON.stringify({
      general: clone(general),
      appearance: clone(appearance),
      shortcuts: clone(shortcuts),
      asr: clone(asr),
      listen: {
        lan: service.lan,
        https: service.https,
        token: service.token,
        autostart: service.autostart,
      },
      sapi: {
        voiceId: sapi.voiceId,
        isDefault: sapi.isDefault,
      },
      scanRoots: scanRoots.value,
      dirs: clone(dirs),
      cachePolicy: clone(cachePolicy),
    })
  }

  const snapshot = computed(prefsSnapshot)
  const dirty = computed(() => loaded.value && savedSnapshot.value !== '' && snapshot.value !== savedSnapshot.value)

  /** dirty 的按快照记忆化。`dirtySections` 与 `dirtyCount` 每次求值都要把两份快照
   *  各 parse 一遍再逐项比，而设置页每敲一个字 snapshot 就变、两个都要重算——
   *  同一对快照内无论被求值多少次（多个组件、多次重渲染）都只算一遍。
   *  键就是两份快照本身，值变了键自然变，没有失效问题。 */
  let dirtyMemo: { key: string; sections: Record<SectionKey, boolean>; count: number } | null = null

  function dirtyOf() {
    const key = `${savedSnapshot.value}\u0000${snapshot.value}`
    if (dirtyMemo && dirtyMemo.key === key) return dirtyMemo
    const saved = savedSnapshot.value ? JSON.parse(savedSnapshot.value) : null
    const now = JSON.parse(snapshot.value)
    const diff = (a: unknown, b: unknown) => JSON.stringify(a) !== JSON.stringify(b)
    const sections: Record<SectionKey, boolean> = {
      general:
        Boolean(saved) &&
        (diff(now.general, saved.general) ||
          diff(now.listen, saved.listen) ||
          diff(now.sapi, saved.sapi)),
      appearance: Boolean(saved) && diff(now.appearance, saved.appearance),
      shortcuts: Boolean(saved) && diff(now.shortcuts, saved.shortcuts),
      asr: Boolean(saved) && diff(now.asr, saved.asr),
      texts: false, // 文案自己有「保存文案」那颗按钮，不进这一页的统一保存
      storage:
        Boolean(saved) &&
        (diff(now.scanRoots, saved.scanRoots) ||
          diff(now.dirs, saved.dirs) ||
          diff(now.cachePolicy, saved.cachePolicy)),
    }
    let count = 0
    if (saved) {
      const walk = (a: unknown, b: unknown) => {
        if (JSON.stringify(a) === JSON.stringify(b)) return
        if (a && b && typeof a === 'object' && typeof b === 'object' && !Array.isArray(a) && !Array.isArray(b)) {
          const keys = new Set([...Object.keys(a), ...Object.keys(b)])
          for (const key of keys) walk((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key])
          return
        }
        count += 1
      }
      walk(now, saved)
    }
    dirtyMemo = { key, sections, count }
    return dirtyMemo
  }

  const dirtySections = computed<Record<SectionKey, boolean>>(() => dirtyOf().sections)
  const dirtyCount = computed(() => dirtyOf().count)

  const blurPx = computed(() => Math.round(4 + (appearance.glass / 100) * 22))

  /** 左栏三档的派生：`railTree` = 目录树那一列在不在，`railCards` = 音色卡片列在不在。
   *  壳、生成页、创作页都只读这一份，不在各自模板前再推一遍。 */
  const railTree = computed(() => appearance.railVisibility === 'both')
  const railCards = computed(() => appearance.railVisibility !== 'none')

  /** 再点当前页的导航图标时的档位循环：有音色卡片列的页走三档；
   *  没有这一列的页只在树的有无之间切——不写 `none`，否则在音色库收起树，
   *  回到生成页会把音色栏也一起收掉。 */
  function cycleRails(withCards: boolean) {
    const state = appearance.railVisibility
    if (!withCards) {
      appearance.railVisibility = state === 'both' ? 'cards' : 'both'
      return
    }
    appearance.railVisibility = state === 'both' ? 'cards' : state === 'cards' ? 'none' : 'both'
  }

  /** 原色、纸墨与素瓷都没有玻璃与圆角可调：模糊归零，圆角按各自的材质下发。
   *  必须在这里按风格算——样式表覆盖不了元素上的 style。 */
  const shellStyle = computed(() => {
    if (appearance.look === 'classic') {
      return {
        '--sh-blur': '0px',
        '--sh-blur-strong': '0px',
        '--sh-radius-card': '10px',
        '--sh-radius-inner': '8px',
      }
    }
    if (appearance.look === 'paper') {
      return {
        '--sh-blur': '0px',
        '--sh-blur-strong': '0px',
        '--sh-radius-card': '4px',
        '--sh-radius-inner': '3px',
      }
    }
    if (appearance.look === 'celadon') {
      return {
        '--sh-blur': '0px',
        '--sh-blur-strong': '0px',
        '--sh-radius-card': '15px',
        '--sh-radius-inner': '10px',
      }
    }
    if (appearance.look === 'stage') {
      return {
        '--sh-blur': '20px',
        '--sh-blur-strong': '32px',
        '--sh-radius-card': '14px',
        '--sh-radius-inner': '9px',
      }
    }
    return {
      '--sh-blur': `${blurPx.value}px`,
      '--sh-blur-strong': `${blurPx.value + 10}px`,
      '--sh-radius-card': `${appearance.radius}px`,
      '--sh-radius-inner': `${Math.max(8, appearance.radius - 6)}px`,
    }
  })

  function applyTheme() {
    if (shellStore.themeMode !== appearance.theme) setThemeMode(appearance.theme)
    // 风格落在 <html> 上：toast 在壳外，也要认得出当前风格。
    const root = document.documentElement
    root.dataset.look = appearance.look
    applyAvatar(root)
  }

  /** 头像规则也挂在 <html> 上：卡片、左列、详情三处共用同一组属性。 */
  function applyAvatar(root: HTMLElement) {
    const avatar = appearance.avatar
    root.dataset.avatarShow = avatar.show ? 'on' : 'off'
    root.dataset.avatarRail = avatar.rail ? 'on' : 'off'
    root.dataset.avatarCards = avatar.cards ? 'on' : 'off'
    root.dataset.avatarDetail = avatar.detail ? 'on' : 'off'
    root.dataset.avatarSize = avatar.size
    root.dataset.avatarShape = avatar.shape
  }

  /** 本机偏好落盘。键写在 localStorage，index.html 首屏与 shell.initFilters 都认它。 */
  function persist() {
    try {
      localStorage.setItem(STORE_KEY, snapshot.value)
      localStorage.setItem(REMEMBER_KEY, general.rememberFilters ? '1' : '0')
    } catch {
      /* 隐私模式：本次会话仍生效 */
    }
  }

  function hydrate() {
    let stored: ReturnType<typeof normalizeSnapshot> = null
    try {
      stored = normalizeSnapshot(JSON.parse(localStorage.getItem(STORE_KEY) || 'null'))
    } catch {
      stored = null
    }
    if (stored) {
      Object.assign(general, DEFAULTS.general, stored.general)
      // 先克隆：直接挂 stored 的引用再往上合并默认值，会把同一份已存值改回默认。
      const savedAppearance = stored.appearance ? clone(stored.appearance) : undefined
      Object.assign(appearance, clone(DEFAULTS.appearance), savedAppearance)
      // 这里必须换成新对象——在原对象上合并默认值，会把作为来源的已存值一起覆盖。
      appearance.avatar = normalizeAvatar(savedAppearance?.avatar)
      // 旧存档没有这两项，或者存了不认识的值：回到默认档，不让坏值把左栏锁死。
      appearance.railVisibility = RAIL_VISIBILITIES.includes(appearance.railVisibility)
        ? appearance.railVisibility
        : 'both'
      appearance.tagBarCollapsed = appearance.tagBarCollapsed === true
      Object.assign(shortcuts, DEFAULTS.shortcuts, stored.shortcuts)
      // 「筛选音色」发布前换过默认键位（Ctrl+Shift+F → Alt+F → Alt+S）。
      // 旧值只可能来自未发布的开发版存档，留着会把新默认永远盖住，当作没存过。
      if (DEV_FILTER_KEYS.includes(shortcuts.openFilters)) {
        shortcuts.openFilters = DEFAULTS.shortcuts.openFilters
      }
      Object.assign(asr, DEFAULTS.asr, stored.asr)
      scanRoots.value = Array.isArray(stored.scanRoots) ? stored.scanRoots : []
      Object.assign(dirs, DEFAULTS.storage.dirs, stored.dirs)
    }
    applyTheme()
    loaded.value = true
    // 第一次来：当前值就是"已保存"的基线，不该一进页面就显示"未保存"。
    savedSnapshot.value = snapshot.value
    persist()
  }

  function applySapiState(data: Record<string, unknown> | null | undefined) {
    if (!data) return
    sapi.supported = Boolean(data.supported)
    sapi.installed = Boolean(data.installed)
    sapi.isDefault = Boolean(data.is_default)
    sapi.canInstall = Boolean(data.can_install)
    sapi.voiceId = String(data.voice_id || '')
    sapi.engine = String(data.engine || 'auto')
    sapi.endpoint = String(data.endpoint || '')
    remoteSeed.sapi = {
      voiceId: sapi.voiceId,
      isDefault: sapi.isDefault,
    }
  }

  function syncSavedSapiBaseline() {
    if (!savedSnapshot.value) {
      savedSnapshot.value = snapshot.value
      return
    }
    try {
      const saved = JSON.parse(savedSnapshot.value)
      saved.sapi = { voiceId: sapi.voiceId, isDefault: sapi.isDefault }
      savedSnapshot.value = JSON.stringify(saved)
    } catch {
      savedSnapshot.value = snapshot.value
    }
  }

  /** 进设置页时问一遍后端：扫描根、存储位置与各自占用。后端没起就退回本机草稿。 */
  async function loadRemote() {
    try {
      const [folders, storage, health, listen, sapiState] = await Promise.all([
        api.folders(),
        api.storage(),
        // 版本只用于「关于」：取不到不算离线，其余两项仍照常。
        api.health().catch(() => null),
        api.service().catch(() => null),
        api.sapi().catch(() => null),
      ])
      const roots: string[] = Array.isArray(folders?.folders) ? folders.folders : []
      entries.value = Array.isArray(storage?.entries) ? (storage.entries as StorageEntry[]) : []
      remoteSeed.roots = [...roots]
      scanRoots.value = [...roots]
      for (const entry of entries.value) {
        const field = STORAGE_FIELDS[entry.key]
        if (field) dirs[field] = entry.value || ''
      }
      remoteSeed.dirs = clone(dirs)
      const policy = storage?.cache as Partial<CachePolicy> | undefined
      if (policy) {
        cachePolicy.limit_mb = Number(policy.limit_mb ?? 0)
        cachePolicy.keep_days = Number(policy.keep_days ?? 0)
      }
      Object.assign(remoteCache, clone(cachePolicy))
      backend.online = true
      backend.message = ''
      backend.version = String(health?.version || '')
      if (listen) {
        service.host = String(listen.host || '')
        service.port = Number(listen.port || 0)
        // 开关显示配置意图，状态行显示生效事实：两者不同时才有"待重启"这句话。
        service.lan = Boolean(listen.config_lan)
        service.https = normalizeHttps(listen.https)
        service.tokenSet = Boolean(listen.token_set)
        service.liveLan = Boolean(listen.lan)
        service.liveTokenSet = service.tokenSet
        // 老进程不报 scheme：生效值无从谈起，状态行也就不提 https 的待重启。
        service.httpsKnown = typeof listen.scheme === 'string'
        service.liveHttps = listen.scheme === 'https'
        service.autoSupported = Boolean(listen.autostart_supported)
        service.autostart = Boolean(listen.autostart_enabled)
        remoteSeed.listen = {
          lan: service.lan,
          https: service.https,
          token: '',
          autostart: service.autostart,
        }
      }
      applySapiState(sapiState as Record<string, unknown> | null)
      // 后端值就是这一层的基线；本机偏好仍按原快照算未保存。
      savedSnapshot.value = snapshot.value
      persist()
    } catch (error) {
      backend.online = false
      backend.version = ''
      const raw = error instanceof Error ? error.message : String(error)
      // dev server 的代理在后端没起时只给一个裸状态码，读的人看不出发生了什么。
      backend.message = /^\d{3}$/.test(raw.trim()) ? '本地服务未启动或端口不可达' : raw
    } finally {
      backend.checked = true
    }
  }

  let remoteLoading = false

  /** 多个入口（壳层、设置页）都会问后端：只问一次，后到的等前一次的结果。 */
  async function ensureRemote() {
    if (backend.checked || remoteLoading) return
    remoteLoading = true
    try {
      await loadRemote()
      // 识别选项读不到不影响其余设置；引擎栏自己还有一条加载路径。
      await ensureAsrOptions().catch(() => {})
    } finally {
      remoteLoading = false
    }
  }

  let asrLoading = false

  /** 读应用层识别选项；与引擎清单是同一次响应，顺手写进全局状态。
   *  只在第一次读：别的分组保存存储位置时会再调 loadRemote，不能冲掉这一页
   *  尚未保存的勾选。 */
  async function loadAsrOptions() {
    if (asrLoaded.value) return
    const payload = await api.asr()
    shellStore.asr = payload
    Object.assign(asr, DEFAULTS.asr, payload?.options || {})
    remoteSeed.asr = clone(asr)
    asrLoaded.value = true
    // 只替换基线里的 ASR 段：本机偏好可能已有未保存草稿，整份覆盖会把它们洗掉。
    if (savedSnapshot.value) {
      try {
        const saved = JSON.parse(savedSnapshot.value)
        saved.asr = clone(asr)
        savedSnapshot.value = JSON.stringify(saved)
      } catch {
        savedSnapshot.value = snapshot.value
      }
    } else {
      savedSnapshot.value = snapshot.value
    }
    persist()
  }

  async function ensureAsrOptions() {
    if (asrLoaded.value || asrLoading) return
    asrLoading = true
    try {
      await loadAsrOptions()
    } finally {
      asrLoading = false
    }
  }

  function addRoot() {
    scanRoots.value = [...scanRoots.value, '']
  }

  function removeRoot(index: number) {
    scanRoots.value = scanRoots.value.filter((_, i) => i !== index)
  }

  async function openEntry(key: string) {
    try {
      await api.openStorage(key)
    } catch (error) {
      say(error instanceof Error ? error.message : String(error))
    }
  }

  async function save() {
    if (saving.value) return
    saving.value = true
    try {
      const roots = scanRoots.value.map((line) => line.trim()).filter(Boolean)
      const rootsChanged = JSON.stringify(roots) !== JSON.stringify(remoteSeed.roots)
      let storageChanged = false
      let asrChanged = false
      let listenChanged = false

      if (backend.online) {
        if (rootsChanged) {
          // saveFolders 内部会重扫并 toast「扫描完成」，这里不再重复说一句。
          await actions.saveFolders(roots)
          remoteSeed.roots = [...roots]
        }
        const patch: Record<string, string | number> = {}
        if (cachePolicy.limit_mb !== remoteCache.limit_mb) patch.cache_limit_mb = cachePolicy.limit_mb
        if (cachePolicy.keep_days !== remoteCache.keep_days) patch.cache_keep_days = cachePolicy.keep_days
        for (const entry of entries.value) {
          const field = STORAGE_FIELDS[entry.key]
          const apiField = field ? STORAGE_API_FIELDS[field] : ''
          if (!field || !apiField || !entry.editable || entry.locked_by) continue
          const wanted = dirs[field].trim()
          if (wanted !== (entry.value || '')) patch[apiField] = wanted
        }
        if (Object.keys(patch).length) {
          const saved = await api.saveStorage(patch)
          storageChanged = true
          if (Array.isArray(saved?.entries)) {
            entries.value = saved.entries as StorageEntry[]
            // 回读核对：字段名对不上时后端只回 200、什么都没存（旧进程也一样）。
            for (const entry of entries.value) {
              const field = STORAGE_FIELDS[entry.key]
              const wanted = field ? patch[STORAGE_API_FIELDS[field]] : undefined
              // 目录那一格才是字符串；回收线那两格是数字，不走这条核对。
              const wantedDir = typeof wanted === 'string' ? wanted : ''
              if (field && wantedDir && !wantedDir.startsWith('~') && !sameDir(entry.path, wantedDir)) {
                throw new Error(
                  `「${entry.label}」没有保存成功，可能仍是旧进程；请重启服务后重试。`
                )
              }
              if (field) dirs[field] = entry.value || ''
            }
          }
          remoteSeed.dirs = clone(dirs)
          const sentPolicy = 'cache_limit_mb' in patch || 'cache_keep_days' in patch
          if (sentPolicy) {
            // 旧进程只回 200、不认这两格：核对一次，免得"提示已保存、实际没变"。
            const savedPolicy = saved?.cache as Partial<CachePolicy> | undefined
            if (!savedPolicy) {
              throw new Error('本地服务没有接受缓存回收设置，可能仍是旧进程；请重启服务后重试。')
            }
            Object.assign(cachePolicy, clone(savedPolicy) as CachePolicy)
            Object.assign(remoteCache, clone(savedPolicy) as CachePolicy)
          }
        }

        // 识别选项与扫描根 / 存储位置一样，等这一颗「保存设置」。
        const asrPatch: Partial<AsrPrefs> = {}
        for (const key of Object.keys(DEFAULTS.asr) as (keyof AsrPrefs)[]) {
          if (asr[key] !== remoteSeed.asr[key]) asrPatch[key] = asr[key]
        }
        // 监听范围、https、令牌、自启：只提交改过的字段。前三项落盘待重启，自启立刻生效。
        const servicePatch: {
          lan?: boolean
          https?: HttpsMode
          access_token?: string
          autostart?: boolean
        } = {}
        if (service.lan !== remoteSeed.listen.lan) servicePatch.lan = service.lan
        if (service.https !== remoteSeed.listen.https) servicePatch.https = service.https
        if (service.token) servicePatch.access_token = service.token
        if (service.autoSupported && service.autostart !== remoteSeed.listen.autostart) {
          servicePatch.autostart = service.autostart
        }
        if (Object.keys(servicePatch).length) {
          const saved = await api.saveService(servicePatch)
          if ('access_token' in servicePatch) {
            service.token = ''
            service.tokenSet = Boolean(saved?.token_set)
            service.liveTokenSet = service.tokenSet
            remoteSeed.listen.token = ''
          }
          if ('lan' in servicePatch) {
            // 生效值要等重启，这里不假装它变了；状态行据此显示"重启后…"。
            remoteSeed.listen.lan = service.lan
            listenChanged = true
          }
          if ('https' in servicePatch) {
            // 老进程不认这一格：不核对就会"提示已保存、重启后还是 http"。
            if (saved?.https !== service.https) {
              throw new Error('本地服务没有接受 https 设置，可能仍是旧进程；请重启服务后重试。')
            }
            remoteSeed.listen.https = service.https
            listenChanged = true
          }
          if ('autostart' in servicePatch) {
            service.autostart = Boolean(saved?.autostart_enabled)
            remoteSeed.listen.autostart = service.autostart
          }
        }

        if (sapi.supported) {
          const sapiPatch: { voice_id?: string; is_default?: boolean } = {}
          if (sapi.voiceId !== remoteSeed.sapi.voiceId) sapiPatch.voice_id = sapi.voiceId
          if (sapi.installed && sapi.isDefault !== remoteSeed.sapi.isDefault) {
            sapiPatch.is_default = sapi.isDefault
          }
          if (Object.keys(sapiPatch).length) {
            const saved = await api.saveSapi(sapiPatch)
            applySapiState(saved as Record<string, unknown>)
          }
        }

        if (Object.keys(asrPatch).length) {
          const saved = await api.saveAsrOptions(asrPatch)
          const savedOptions = (saved?.options || {}) as Partial<AsrPrefs>
          // 旧进程会静默丢掉不认识的字段。不核对就会"提示已保存、实际没变"。
          for (const key of Object.keys(asrPatch) as (keyof AsrPrefs)[]) {
            if (savedOptions[key] !== asrPatch[key]) {
              throw new Error('本地服务没有接受识别选项，可能仍是旧进程；请重启服务后重试。')
            }
          }
          shellStore.asr = saved
          Object.assign(asr, DEFAULTS.asr, savedOptions)
          remoteSeed.asr = clone(asr)
          asrChanged = true
          // 打开「连接时自动识别」后，后端会立刻起一轮补逐字稿；等它起来再拉进度。
          if (asr.auto_on_connect) {
            window.setTimeout(() => void actions.syncAsrFill(), 1200)
          }
        }
      }

      savedSnapshot.value = snapshot.value
      persist()

      if (!rootsChanged) {
        if (!backend.online) say('已保存到本机（后端未连接）')
        else if (asrChanged && !storageChanged) say('设置已保存，识别选项已更新')
        else if (listenChanged) say('已保存，重启服务后生效。')
        else say(storageChanged ? '设置已保存，存储位置已更新' : '设置已保存')
      }
    } catch (error) {
      say(error instanceof Error ? error.message : String(error))
    } finally {
      saving.value = false
    }
  }

  function resetSection(section: SectionKey) {
    if (section === 'general') Object.assign(general, clone(DEFAULTS.general))
    if (section === 'appearance') {
      Object.assign(appearance, clone(DEFAULTS.appearance))
      applyTheme()
    }
    if (section === 'shortcuts') Object.assign(shortcuts, clone(DEFAULTS.shortcuts))
    if (section === 'asr') Object.assign(asr, clone(DEFAULTS.asr))
    if (section === 'storage') {
      scanRoots.value = [...remoteSeed.roots]
      Object.assign(dirs, clone(remoteSeed.dirs))
      Object.assign(cachePolicy, clone(remoteCache))
    }
    persist()
  }

  function resetAll() {
    Object.assign(general, clone(DEFAULTS.general))
    Object.assign(appearance, clone(DEFAULTS.appearance))
    Object.assign(shortcuts, clone(DEFAULTS.shortcuts))
    Object.assign(asr, clone(DEFAULTS.asr))
    scanRoots.value = [...remoteSeed.roots]
    Object.assign(dirs, clone(remoteSeed.dirs))
    Object.assign(cachePolicy, clone(remoteCache))
    applyTheme()
    persist()
    say('已恢复默认设置')
  }

  async function installSapi() {
    if (sapi.busy) return
    sapi.busy = true
    try {
      const state = await api.installSapi({
        voice_id: sapi.voiceId,
        set_default: true,
      })
      applySapiState(state as Record<string, unknown>)
      syncSavedSapiBaseline()
      persist()
      say('系统朗读桥接已安装')
    } catch (error) {
      say(error instanceof Error ? error.message : String(error))
    } finally {
      sapi.busy = false
    }
  }

  async function uninstallSapi() {
    if (sapi.busy) return
    sapi.busy = true
    try {
      const state = await api.uninstallSapi()
      applySapiState(state as Record<string, unknown>)
      syncSavedSapiBaseline()
      persist()
      say('系统朗读桥接已卸载')
    } catch (error) {
      say(error instanceof Error ? error.message : String(error))
    } finally {
      sapi.busy = false
    }
  }

  // 任何改动：立刻生效、立刻落盘（草稿），「保存」只负责提交后端与清掉未保存标记。
  watch(snapshot, () => {
    applyTheme()
    persist()
  })

  return {
    general,
    appearance,
    shortcuts,
    asr,
    asrLoaded,
    scanRoots,
    dirs,
    entries,
    cachePolicy,
    backend,
    service,
    sapi,
    saving,
    loaded,
    dirty,
    dirtyCount,
    dirtySections,
    blurPx,
    railTree,
    railCards,
    cycleRails,
    shellStyle,
    hydrate,
    loadRemote,
    ensureRemote,
    loadAsrOptions,
    ensureAsrOptions,
    addRoot,
    removeRoot,
    openEntry,
    installSapi,
    uninstallSapi,
    save,
    resetAll,
    resetSection,
    persist,
  }
})
