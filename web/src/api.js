// @ts-check
// HTTP 层唯一出口——组件不直接 fetch。
// 错误统一取后端 error.message（过渡期回落到 detail），原样展示。
//
// 为什么先给这里开 checkJs：字段名、参数、返回值都在这个边界进出，
// 拼错一个 id 或漏传 timeout 是最容易被运行时才发现的那类错。
/**
 * @typedef {object} RequestOptions
 * @property {string} [method]
 * @property {any} [body] JSON 请求体；由 request 自己序列化
 * @property {number} [timeout] 毫秒；0 = 不设上限
 * @property {AbortSignal} [signal]
 */

/** 普通请求的超时：后端假死时不能让界面无限转圈。长任务逐条覆盖。 */
const DEFAULT_TIMEOUT = 30_000

/** 401：令牌过期或被换掉。带着当前地址回登录页（没设令牌时不会发生）。 */
export function toLogin() {
  const next = `${window.location.pathname}${window.location.search}`
  window.location.assign(`/login?next=${encodeURIComponent(next)}`)
  return new Error('登录已失效，正在返回登录页。')
}

/**
 * 是不是超时取消。`AbortSignal.timeout` 抛的是 DOMException，不是 Error 子类，
 * 只能按 `name` 判。
 * @param {unknown} error
 * @returns {boolean}
 */
export function isTimeout(error) {
  return (
    typeof error === 'object' &&
    error !== null &&
    /** @type {{ name?: string }} */ (error).name === 'TimeoutError'
  )
}

/**
 * @param {string} path
 * @param {RequestOptions} [options]
 * @returns {Promise<any>}
 */
export async function request(path, options = {}) {
  // `timeout: 0` = 不设上限（合并、导出这类按分钟计的请求）；
  // 调用方自己带的 signal 优先，超时不再叠加。
  const { timeout = DEFAULT_TIMEOUT, ...rest } = options
  const signal = rest.signal || (timeout > 0 ? AbortSignal.timeout(timeout) : undefined)
  let resp
  try {
    resp = await fetch(path, {
      headers: rest.body ? { 'Content-Type': 'application/json' } : undefined,
      ...rest,
      signal,
      body: rest.body ? JSON.stringify(rest.body) : undefined,
    })
  } catch (error) {
    // 调用方主动 abort（合并进度轮询）是正常收尾，原样往上抛。
    if (rest.signal) throw error
    if (isTimeout(error)) {
      throw new Error(`请求超时：本地服务没有响应（${Math.round(timeout / 1000)} 秒）。`)
    }
    throw error
  }
  if (resp.status === 401) throw toLogin()
  if (!resp.ok) {
    const error = /** @type {Error & { status?: number }} */ (
      new Error(await errorMessage(resp))
    )
    // 带上状态码：调用方要分得清"这次请求不被接受"（4xx，重试没意义）
    // 与"服务端一时写不进去"（5xx，值得退避重试）。
    error.status = resp.status
    throw error
  }
  return resp.json()
}

/**
 * 集合端点统一信封：`{object: "list", data: [...]}` → 数组。
 * @param {string} path
 * @param {RequestOptions} [options]
 * @returns {Promise<any[]>}
 */
async function listRequest(path, options = {}) {
  const payload = await request(path, options)
  return Array.isArray(payload) ? payload : payload.data || []
}

/** 文案端点统一信封：`{object: "texts", groups: [...]}` → 分组数组。
 *  读、写、恢复默认三个动作都走这里，store 里只会有数组。 */
/**
 * @param {string} path
 * @param {RequestOptions} [options]
 * @returns {Promise<any[]>}
 */
async function textsRequest(path, options = {}) {
  const payload = await request(path, options)
  return Array.isArray(payload.groups) ? payload.groups : []
}

/** 后端错误体 → 一句中文说明；取不到时用状态码。插件自己的上传口也用这一份。 */
/**
 * @param {Response} resp
 * @returns {Promise<string>}
 */
export async function errorMessage(resp) {
  let detail = `${resp.status}`
  try {
    const data = await resp.json()
    detail = data?.error?.message || data?.detail || detail
  } catch {
    /* 非 JSON 错误页，用状态码 */
  }
  return typeof detail === 'string' ? detail : '请求失败'
}

/** 上传一段音频（multipart）。录音是浏览器录的，不是库里的文件，只能这么送上去。 */
/**
 * @param {string} path
 * @param {Blob & { name?: string }} audio
 * @param {Record<string, string>} [fields]
 * @returns {Promise<any>}
 */
async function uploadAudio(path, audio, fields = {}) {
  const form = new FormData()
  form.append('file', audio, audio.name || '录音.wav')
  for (const [name, value] of Object.entries(fields)) form.append(name, value)
  let resp
  try {
    resp = await fetch(path, {
      method: 'POST',
      body: form,
      // 转写可能要等服务把整段听完；上传本身不该无限挂着。
      signal: AbortSignal.timeout(180_000),
    })
  } catch (error) {
    if (isTimeout(error)) throw new Error('上传超时：本地服务没有响应。')
    throw error
  }
  if (resp.status === 401) throw toLogin()
  if (!resp.ok) {
    throw new Error(await errorMessage(resp))
  }
  return resp.json()
}

/**
 * 通用 multipart 上传。音视频导入按 GB 计，所以默认**不设超时**——
 * 界面靠后端进度与取消，而不是把一个传了半天的请求掐掉。
 * @param {string} path
 * @param {File | Blob} file
 * @param {Record<string, string>} [fields]
 * @param {number} [timeout] 毫秒；0 = 不设上限
 * @returns {Promise<any>}
 */
export async function uploadFile(path, file, fields = {}, timeout = 0) {
  const form = new FormData()
  const name = /** @type {{ name?: string }} */ (file).name || 'file'
  form.append('file', file, name)
  for (const [key, value] of Object.entries(fields)) form.append(key, String(value))
  let resp
  try {
    resp = await fetch(path, {
      method: 'POST',
      body: form,
      signal: timeout > 0 ? AbortSignal.timeout(timeout) : undefined,
    })
  } catch (error) {
    if (isTimeout(error)) throw new Error('上传超时：本地服务没有响应。')
    throw error
  }
  if (resp.status === 401) throw toLogin()
  if (!resp.ok) {
    throw new Error(await errorMessage(resp))
  }
  return resp.json()
}

/**
 * @param {Record<string, any>} params
 * @returns {string}
 */
function query(params) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value)
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  health: () => request('/api/v1/health'),
  // 只查 GitHub 的最新正式发布：不下载、不替换文件。`refresh` 跳过一分钟缓存。
  release: (refresh = false) =>
    request(`/api/v1/release${refresh ? '?refresh=1' : ''}`, { timeout: 20_000 }),
  // 监听范围、https 与令牌：只回答"现在生效的是什么"，改完要重启进程。
  service: () => request('/api/v1/service'),
  /**
   * 只提交改过的字段：`lan` / `https` / `access_token` 落盘待重启，`autostart` 立刻生效。
   * @param {{ lan?: boolean, https?: 'auto'|'on'|'off', access_token?: string, autostart?: boolean }} body
   */
  saveService: (body) => request('/api/v1/service', { method: 'PUT', body }),
  // Windows SAPI 5 系统朗读桥接：读状态、改音色/默认、安装与卸载。
  sapi: () => request('/api/v1/sapi'),
  /**
   * 更新 SAPI 桥接音色与是否设为系统默认音色（无需提权，立刻生效）。
   * @param {{ voice_id?: string, is_default?: boolean }} body
   */
  saveSapi: (body) => request('/api/v1/sapi', { method: 'PUT', body }),
  /**
   * 安装或重装 SAPI 桥接（编译 + 注册 HKLM，会触发 UAC 提权）。
   * @param {{ voice_id?: string, set_default?: boolean }} [body]
   */
  installSapi: (body = {}) =>
    request('/api/v1/sapi/install', { method: 'POST', body, timeout: 180_000 }),
  /** 卸载 SAPI 桥接并恢复系统原默认音色。 */
  uninstallSapi: () => request('/api/v1/sapi/uninstall', { method: 'POST', timeout: 120_000 }),
  tree: () => request('/api/v1/tree'),
  folders: () => request('/api/v1/folders'),
  // 保存扫描根会同步扫一遍（大库几十秒），不能按普通请求的 30 秒掐断。
  /** @param {string[]} folders */
  saveFolders: (folders) =>
    request('/api/v1/folders', { method: 'PUT', body: { folders }, timeout: 600_000 }),
  /**
   * 生成结果目录（全局那一层）。每台引擎可在自己的设置里另指一处（`saveEngineOutput`）；
   * 全局目录下面生成过的东西是普通本地音色，会被扫进来。
   */
  output: () => request('/api/v1/output'),
  // 目录迁移会搬文件，按文件量计；只改路径时也很快。
  /** @param {string} dir */
  saveOutput: (dir) =>
    request('/api/v1/output', { method: 'PUT', body: { dir }, timeout: 600_000 }),
  /**
   * 改全局生成目录前问一句：旧目录里有没有东西要一起搬（不碰文件）。
   * @param {string} dir
   */
  previewOutput: (dir) => request('/api/v1/output/preview', { method: 'POST', body: { dir } }),
  /** 存储位置（数据 / 生成结果 / 缓存 / 日志 / 识别权重）与各自占用。 */
  storage: () => request('/api/v1/storage'),
  /**
   * 只发改过的项；空串 = 回默认。
   * @param {Record<string, any>} patch
   */
  saveStorage: (patch) => request('/api/v1/storage', { method: 'PUT', body: patch }),
  /**
   * 让服务在资源管理器里打开这个目录。
   * @param {string} key
   */
  openStorage: (key) => request('/api/v1/storage/open', { method: 'POST', body: { key } }),
  /** 向量模型：状态、按需下载、删除。模型文件不随安装包分发。 */
  embeddingProviders: () => listRequest('/api/v1/embeddings/providers'),
  /** @param {string} key */
  embeddingProvider: (key) =>
    request(`/api/v1/embeddings/providers/${encodeURIComponent(key)}`),
  /** @param {string} key */
  downloadEmbedding: (key) =>
    request(`/api/v1/embeddings/providers/${encodeURIComponent(key)}/download`, {
      method: 'POST',
    }),
  /** @param {string} key */
  removeEmbedding: (key) =>
    request(`/api/v1/embeddings/providers/${encodeURIComponent(key)}/remove`, {
      method: 'POST',
    }),
  /** 起一次后台扫描，立刻回任务快照（`state: running`）；配 `scanStatus` 轮询。 */
  scan: () => request('/api/v1/scan', { method: 'POST' }),
  /** 最近一次扫描的进度：`state` running / done / failed / idle。 */
  scanStatus: () => request('/api/v1/scan/status'),
  /** @param {Record<string, any>} filters */
  voices: (filters) => listRequest(`/api/v1/voices${query(filters)}`),
  /**
   * 历史：录音、内录、生成物的同一张表。筛选与计数都由后端算。
   * @param {Record<string, any>} filters source / engine / q / status / span / limit
   */
  audio: (filters) => request(`/api/v1/audio${query(filters)}`),
  /**
   * 入库：把这几条音频复制进目标音色并登记为样例。
   * @param {{ ids: string[], target: Record<string, unknown> }} body
   */
  absorbAudio: (body) => request('/api/v1/audio/absorb', { method: 'POST', body }),
  /**
   * 移入回收站：生成物连 sidecar 记录一起摘。
   * @param {{ ids: string[] }} body
   */
  deleteAudio: (body) => request('/api/v1/audio', { method: 'DELETE', body }),
  /**
   * 各标签剩几条（``tag`` 之外的筛选都算），给界面那排可点的标签用。
   * @param {Record<string, any>} filters
   */
  tags: (filters) => listRequest(`/api/v1/tags${query(filters)}`),
  /** 合成请求的规范——界面那张表由它渲染，加一项参数不用改前端。 */
  spec: () => request('/api/v1/spec'),
  /** 语气词标签的分组（通用 / 各台引擎），由后端跨引擎算，与勾选无关。 */
  marks: () => request('/api/v1/marks'),
  // 一次合成长则几分钟：超时给足，但仍有上限——服务假死时界面要能报错而不是永远转圈。
  /** @param {any} payload */
  synthesize: (payload) =>
    request('/api/v1/synthesize', { method: 'POST', body: payload, timeout: 600_000 }),
  /** @param {string} id */
  voice: (id) => request(`/api/v1/voices/${encodeURIComponent(id)}`),
  /**
   * @param {string} id
   * @param {any} patch
   */
  updateVoice: (id, patch) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}`, { method: 'PUT', body: patch }),
  /**
   * @param {string} id
   * @param {string} file
   * @param {any} patch
   */
  updateSample: (id, file, patch) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}/samples/${encodeURIComponent(file)}`, {
      method: 'PUT',
      body: patch,
    }),
  /**
   * @param {string} id
   * @param {string} file
   */
  setMainSample: (id, file) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}/main-sample`, {
      method: 'POST',
      body: { file },
    }),
  /**
   * 移除一条样例。文件进回收站（可还原）——只摘记录的话，下次扫描它又回来了。
   * @param {string} id
   * @param {string} file
   */
  deleteSample: (id, file) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}/samples/${encodeURIComponent(file)}`, {
      method: 'DELETE',
    }),
  /**
   * @param {string} id
   * @param {any} prefs
   */
  setPrefs: (id, prefs) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}/prefs`, { method: 'POST', body: prefs }),
  /**
   * 在资源管理器里打开这条音色的文件夹（服务得在本机跑，且这条音色有本地目录）。
   * @param {string} id
   */
  revealVoice: (id) =>
    request(`/api/v1/voices/${encodeURIComponent(id)}/reveal`, { method: 'POST' }),
  /**
   * 移除一条音色。本地音色的文件会进回收站（可还原），不是只删库里那一行。
   * @param {string} id
   */
  deleteVoice: (id) => request(`/api/v1/voices/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  /** @param {string} id */
  deletePlatformVoice: (id) =>
    request(`/api/v1/platform-voices/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ---- 引擎（插件）----
  engines: () => listRequest('/api/v1/engines'),
  /** 适配清单：发现到的全部引擎（含未启用），「引擎清单」照它画。 */
  engineCatalog: () => listRequest('/api/v1/engines/catalog'),
  /**
   * 启用 / 停用。停用不动配置与库内音色，只是不再出现在各处。
   * @param {string} key
   * @param {boolean} enabled
   */
  setEngineEnabled: (key, enabled) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/enabled`, {
      method: 'PUT',
      body: { enabled },
    }),
  /** @param {string} key */
  engineSettings: (key) => request(`/api/v1/engines/${encodeURIComponent(key)}/settings`),
  /**
   * 两个框架认识的口子走同一具请求体：`_clear: [...]` 清空某个机密项，
   * `_reset: true` 恢复插件默认（非机密回落，机密保留）。
   * @param {string} key
   * @param {any} values
   */
  saveEngineSettings: (key, values) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/settings`, { method: 'PUT', body: values }),
  /**
   * 这台引擎的音色通道（音色从哪来、怎么供）。空 `mode` = 回到插件能力推导的默认。
   * @param {string} key
   * @param {any} patch
   */
  saveEngineChannel: (key, patch) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/channel`, { method: 'PUT', body: patch }),
  // 这台引擎的音色保存目录。与设置分开：它是框架给的一格，不属于 settings_schema。
  // 保存前先问 preview：旧目录里有没有东西要一起搬（不搬的话库里的旧路径就失效了）。
  /**
   * @param {string} key
   * @param {string} dir
   */
  previewEngineOutput: (key, dir) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/output/preview`, {
      method: 'POST',
      body: { dir },
    }),
  /**
   * @param {string} key
   * @param {string} dir
   * @param {boolean} [migrate]
   */
  saveEngineOutput: (key, dir, migrate = false) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/output`, {
      method: 'PUT',
      body: { dir, migrate },
      timeout: 600_000,
    }),
  // 拉平台音色清单是一次外部网络请求，慢的时候十几秒。
  /** @param {string} key */
  refreshEngine: (key) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/refresh`, {
      method: 'POST',
      timeout: 180_000,
    }),
  /** 清除这台引擎在库里的音色条目（只删库，磁盘上的文件不动）。 @param {string} key */
  clearEngineVoices: (key) =>
    request(`/api/v1/engines/${encodeURIComponent(key)}/voices`, { method: 'DELETE' }),
  /**
   * 音色克隆走 multipart；音源可以是上传文件，也可以是库里已有样例的 id + 文件名。
   * @param {string} key
   * @param {{ file?: Blob, [name: string]: any }} options
   */
  cloneVoice: async (key, { file, ...fields }) => {
    const form = new FormData()
    if (file) form.append('file', file)
    for (const [name, value] of Object.entries(fields)) form.append(name, value || '')
    let resp
    try {
      resp = await fetch(`/api/v1/engines/${encodeURIComponent(key)}/clone`, {
        method: 'POST',
        body: form,
        // 克隆要训练平台侧的音色，按分钟计。
        signal: AbortSignal.timeout(600_000),
      })
    } catch (error) {
      if (isTimeout(error)) throw new Error('克隆超时：本地服务没有响应。')
      throw error
    }
    if (resp.status === 401) throw toLogin()
    if (!resp.ok) {
      throw new Error(await errorMessage(resp))
    }
    return resp.json()
  },
  /**
   * 让对话模型写一句设计描述：只回一句话，不建音色。
   * @param {any} payload
   */
  designPrompt: (payload) =>
    request('/api/v1/voices/design-prompt', {
      method: 'POST',
      body: payload,
      timeout: 180_000,
    }),

  // ---- 对话模型（LLM 插件）----
  // 与引擎同构、不同契约：那边"给文本出声"，这边"给消息回消息"。
  // 密钥读回来恒为空串；`_clear` 清空、`_reset` 恢复默认走同一具请求体。
  llmProviders: () => listRequest('/api/v1/llm/providers'),
  /** @param {string} key */
  llmProvider: (key) => request(`/api/v1/llm/providers/${encodeURIComponent(key)}`),
  /**
   * @param {string} key
   * @param {any} values
   */
  saveLlmSettings: (key, values) =>
    request(`/api/v1/llm/providers/${encodeURIComponent(key)}/settings`, {
      method: 'PUT',
      body: values,
    }),
  /**
   * 默认模型（不带 provider 的调用落到它）。
   * @param {string} key
   */
  setDefaultLlm: (key) => request('/api/v1/llm/default', { method: 'PUT', body: { key } }),
  /**
   * 平台模型清单：拉不到是 `ok:false` + 理由，不是异常。
   * @param {string} key
   */
  llmModels: (key) =>
    request(`/api/v1/llm/providers/${encodeURIComponent(key)}/models`, { timeout: 120_000 }),
  /**
   * 连通自检：一句极短的话，失败同样返回 200 + `ok:false`。
   * @param {string} key
   */
  testLlm: (key) =>
    request(`/api/v1/llm/providers/${encodeURIComponent(key)}/test`, {
      method: 'POST',
      timeout: 180_000,
    }),
  /** @param {any} payload */
  llmChat: (payload) =>
    request('/api/v1/llm/chat', { method: 'POST', body: payload, timeout: 180_000 }),

  // ---- 日志 ----
  // `logs(since)` 是快照（首屏与重连补帧）；`logStreamUrl(since)` 给的是一个地址
  // 而非 promise——EventSource 只认 URL，装配那条流的事在 store/log.js 里。

  /**
   * `since` 之后的那一段（正序）+ 当前游标 + "中间缺了一段"的标记。
   * @param {number} [since]
   * @param {number} [limit]
   */
  logs: (since = 0, limit = 1000) => request(`/api/v1/logs?since=${since}&limit=${limit}`),
  /** @param {number} [since] */
  logStreamUrl: (since = 0) => `/api/v1/logs/stream?since=${since}`,
  /**
   * 界面上的提示也写进日志链路（失败不打扰用户：toast 已经说过了）。
   * @param {string} message
   * @param {string} [level]
   */
  clientLog: (message, level = 'info') =>
    request('/api/v1/logs', {
      method: 'POST',
      body: { message, level, source: 'ui' },
    }).catch(() => {}),

  // ---- 头像匹配 ----
  // 来源只给候选图片；服务端负责下载、裁成方形、命名与落盘。一次只查一个来源，
  // `source` 取 `avatarStatus()` 下发的 key。
  avatarStatus: () => request('/api/v1/avatar/status'),
  /**
   * @param {string} voiceId
   * @param {string} source 来源 key
   */
  generateAvatar: (voiceId, source) =>
    request(`/api/v1/avatar/voices/${encodeURIComponent(voiceId)}`, {
      method: 'POST',
      body: { source },
      timeout: 180_000,
    }),
  /**
   * 裁好的候选图（缩略图 + 出处），**不落盘**；挑好后再 apply。
   * @param {string} voiceId
   * @param {string} source 来源 key
   */
  avatarCandidates: (voiceId, source) =>
    request(`/api/v1/avatar/voices/${encodeURIComponent(voiceId)}/candidates`, {
      method: 'POST',
      body: { source },
      timeout: 180_000,
    }),
  /**
   * @param {string} voiceId
   * @param {string} token
   */
  applyAvatar: (voiceId, token) =>
    request(`/api/v1/avatar/voices/${encodeURIComponent(voiceId)}/apply`, {
      method: 'POST',
      body: { token },
    }),

  // ---- 语音识别 ----
  // 识别是另一类插件（"音频 → 文字"），与合成引擎共用发现与配置，接口自成一组：
  // 没有"安装/下载"——模型从哪来是插件自己的事。

  /** 每台识别引擎的说明、当前值与就绪情况（不探网络，很便宜）。 */
  asr: () => request('/api/v1/asr'),
  /** 「连接时自动识别」那一轮的进度（toast 轮询它）。 */
  asrFill: () => request('/api/v1/asr/fill'),
  /** 停止补逐字稿：当前这条收尾后不再开始下一条。 */
  cancelAsrFill: () => request('/api/v1/asr/fill/cancel', { method: 'POST' }),
  /**
   * 当前用哪台。空串 = 自动，按引擎顺序用当前开着的服务。
   * @param {string} key
   */
  setAsrCurrent: (key) => request('/api/v1/asr/current', { method: 'PUT', body: { key } }),
  /**
   * 应用层识别选项：自动识别 / 写回样例 / 录音转写。
   * @param {any} patch
   */
  saveAsrOptions: (patch) => request('/api/v1/asr/options', { method: 'PUT', body: patch }),
  /**
   * 保存一台识别引擎的配置；`_clear` / `_reset` 的语义与合成引擎一致。
   * @param {string} key
   * @param {any} patch
   */
  saveAsrSettings: (key, patch) =>
    request(`/api/v1/asr/engines/${encodeURIComponent(key)}/settings`, {
      method: 'PUT',
      body: patch,
    }),
  /**
   * 连一下这台引擎：能不能用、它有哪些模型。
   * @param {string} key
   */
  probeAsr: (key) =>
    request(`/api/v1/asr/engines/${encodeURIComponent(key)}/probe`, { method: 'POST' }),
  /**
   * 听一条样例。给的是"哪条音色的哪个文件"，不是一个路径。
   * @param {string} voiceId
   * @param {string} file
   */
  transcribe: (voiceId, file) =>
    request('/api/v1/asr/transcribe', {
      method: 'POST',
      body: { voice_id: voiceId, file },
      timeout: 180_000,
    }),
  /**
   * 麦克风语音输入：一段录音 → 一句话。这段录音不属于任何音色，听完即删。
   * @param {Blob} audio
   */
  dictate: (audio) => uploadAudio('/api/v1/asr/dictation', audio),
  /**
   * 麦克风录一段当参考音频：落盘并回路径（+ 顺手听出来的逐字稿）。
   * 不传 `transcribe` 时由服务端按「录音后自动转写」决定。
   * @param {Blob} audio
   * @param {boolean} [transcribe]
   */
  saveRecording: (audio, transcribe) =>
    uploadAudio(
      '/api/v1/recordings',
      audio,
      transcribe === undefined ? {} : { transcribe: transcribe ? '1' : '0' },
    ),
  /**
   * 补听一段已落盘的临时录音（自动转写关闭时的手动入口）。
   * @param {string} name
   */
  transcribeRecording: (name) =>
    request(`/api/v1/recordings/${encodeURIComponent(name)}/transcribe`, { method: 'POST' }),
  /**
   * 内录状态：可用性、热键与注册结果。起停由托盘热键完成，页面只读状态。
   */
  capture: () => request('/api/v1/capture/status'),

  // ---- 文案库 ----
  // 生成样例时"挑一句现成的"那几组句子。它写在服务的数据目录里、设置页可改，
  // 不写死在前端：念什么口味是每个人自己的事，为加一句去改代码重构建太重了。

  texts: () => textsRequest('/api/v1/texts'),
  /**
   * 整份替换；返回值仍是分组数组，保存后界面接着读它。
   * @param {any[]} groups
   */
  saveTexts: (groups) => textsRequest('/api/v1/texts', { method: 'PUT', body: { groups } }),
  /** 恢复默认那一份（并且从此算你改过的）。 */
  resetTexts: () => textsRequest('/api/v1/texts/reset', { method: 'POST' }),

  // ---- 用户词表 ----
  // 情绪候选与引导指令预设：默认在仓库（情绪词表 + 各引擎 instructions.json），
  // 用户差别在数据目录。整包给设置页；保存按节局部传（只传改过的那一节）。

  vocab: () => request('/api/v1/vocab'),
  /** @param {any} patch */
  saveVocab: (patch) => request('/api/v1/vocab', { method: 'PUT', body: patch }),
  /** 读音替换：词 → 同音词，合成前由网关作用到正文。 */
  pronunciations: () => request('/api/v1/pronunciations'),
  /** @param {any[]} entries */
  savePronunciations: (entries) =>
    request('/api/v1/pronunciations', { method: 'PUT', body: { entries } }),

  // ---- 生产方插件 ----
  // 宿主只提供通用三件：清单、自描述、配置。书籍相关端点在插件自己的 api 里。
  producers: () => listRequest('/api/v1/producers'),
  /** @param {string} key */
  producer: (key) => request(`/api/v1/producers/${key}`),
  /**
   * @param {string} key
   * @param {boolean} enabled
   */
  setProducerEnabled: (key, enabled) =>
    request(`/api/v1/producers/${key}/enabled`, { method: 'PUT', body: { enabled } }),
  /**
   * @param {string} key
   * @param {any} body
   */
  saveProducer: (key, body) =>
    request(`/api/v1/producers/${key}/settings`, { method: 'PUT', body }),
  /**
   * 打开某个配置字段指向的目录（如创作项目的项目目录）。
   * @param {string} key
   * @param {string} field
   */
  openProducerField: (key, field) =>
    request(`/api/v1/producers/${key}/settings/open`, { method: 'POST', body: { field } }),
  /**
   * 生产方声明的 AI 提示词（设置页「词表」里编辑的那几组）。
   * @param {string} key
   */
  producerPrompts: (key) => request(`/api/v1/producers/${key}/prompts`),
  /**
   * @param {string} key
   * @param {any} patch
   */
  saveProducerPrompts: (key, patch) =>
    request(`/api/v1/producers/${key}/prompts`, { method: 'PUT', body: patch }),
}
