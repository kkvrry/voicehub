// @ts-check
// 有声书插件的前端状态与动作。
//
// 放进这里的理由：`overview/projects/chapters/rows/speaker/emotion` 是这本书的数据形状，
// 不属于宿主。宿主只保留「当前打开哪一家生产方」这一个键，其余全在本模块内。
import { reactive, watch } from 'vue'
import { actions, say, store } from '../../store'
import * as api from './api'

export const state = reactive({
  loaded: false,
  overview: {
    storage: '',
    projects: /** @type {any[]} */ ([]),
    emotions: /** @type {any[]} */ ([]),
    emotion_groups: /** @type {any[]} */ ([]),
  },
  detail: /** @type {any} */ (null),
  chapterId: '',
  //: 行表当前的状态筛选；章节栏的灯跟着它亮（`all` 时汇总四类）。
  rowFilter: 'all',
  //: 角色筛选与 rowFilter 同属跨章条件，章节栏据此标出不含该角色的章。
  speakerFilter: '',
  rows: /** @type {any[]} */ ([]),
  marks: {
    common: /** @type {any[]} */ ([]),
    engines: /** @type {any[]} */ ([]),
    participants: /** @type {any[]} */ ([]),
    engine_marks: /** @type {Record<string, any>} */ ({}),
    speaker_engine: /** @type {Record<string, any>} */ ({}),
  },
  busy: '', // 'sentences' | 'merge' | 'merge-all' | 'export' | 'create' | 'resplit' | 'split' | ''
  mergeBatch: /** @type {any} */ (null), // 逐章合并进度：{ done, total, title }
  mergeProgress: /** @type {any} */ (null), // 当前章节处理阶段：{ stage, message, done, total }
  mergeStopping: false, // 已请求停止合并，等待当前进程退出
  job: /** @type {any} */ (null), // 后台任务：analysis / audio / loudness
  projectOpen: false, // 项目管理卡片：导入栏 + 已有项目的改名 / 作者 / 删除
  importing: false,
  // 分章与文本处理：原文只在打开面板时取一次，章节是它的派生物。
  source: /** @type {any} */ (null), // { text, chars, paragraphs, pattern_key }
  patterns: /** @type {any} */ (null), // { chapter_patterns, default_pattern, dialog_presets, bracket_presets }
  splitMeta: /** @type {any} */ (null), // 拆句选项的可选值（模式名）
  splitOpen: false,
  splitPattern: '',
  splitPreview: /** @type {any} */ (null),
  textPreview: /** @type {any} */ (null),
  chapterText: '', // 当前章的正文（没拆句时用它看原文）
  assistStatus: /** @type {any} */ (null), // AI 按钮状态：{ ready, provider, provider_label, model, note }
})

//: 轮询句柄放在模块里，不进响应式状态。
let pollTimer = 0
/** @type {any} */
let activeMerge = null

// 换项目或生产方时立即停止进度请求；旧合并的响应不再写回当前项目。
watch(
  [() => store.producer.key, () => state.detail?.id],
  () => {
    if (activeMerge && !activeMerge.current()) activeMerge.stop()
  },
  { flush: 'sync' }
)

const ACTIVE = new Set(['running', 'cancelling'])

function key() {
  return store.producer.key
}

/** 上次打开的项目：按生产方分开记，换一家插件互不影响。
 *  刷新、关掉再开都站在同一本书上——主页播放条「指定音色」读的也是它。 */
const PROJECT_KEY = 'vh-producer-project'

function projectStoreKey() {
  return `${PROJECT_KEY}:${key()}`
}

function rememberedProject() {
  try {
    return localStorage.getItem(projectStoreKey()) || ''
  } catch {
    return ''
  }
}

/**
 * @param {any} projectId
 */
function rememberProject(projectId) {
  try {
    if (projectId) localStorage.setItem(projectStoreKey(), projectId)
    else localStorage.removeItem(projectStoreKey())
  } catch {
    /* 隐私模式下写不进去，本次会话仍然照常 */
  }
}

/**
 * @param {any} error
 */
function fail(error) {
  const message = error instanceof Error ? error.message : String(error)
  say(message, { kind: 'error' })
}

/** 打开插件：拉项目列表并落到上次用的那一本；没有记录才落到列表第一本。
 *  有未完成的任务就接着显示。 */
export async function open() {
  if (!store.engines?.length) await actions.loadEngines()
  await refresh()
  state.loaded = true
  await loadMarks()
  resumeJob()
}

/** 任何页面上的「指定音色」都从这里取上下文：保证有一本当前项目。
 *  没打开过创作页时 `store.producer.key` 还是空的，用清单里的第一家
 *  （装了一家时就是它）；角色与项目 id 都在项目详情里，没它指定不了。 */
export async function ensureDetail() {
  if (!store.producer.key) store.producer.key = store.producers[0]?.key || ''
  if (state.detail) return state.detail
  if (!key()) return null
  if (!state.loaded) await open()
  else await refresh()
  return state.detail
}

/** 语气词分组：加载失败不影响主流程，标记下拉空着即可。 */
export async function loadMarks() {
  const detail = state.detail
  if (!detail) return
  try {
    state.marks = await api.marks(key(), detail.id)
  } catch {
    state.marks = { common: [], engines: [], participants: [], engine_marks: {}, speaker_engine: {} }
  }
}

function resumeJob() {
  const job = state.detail?.job
  if (job && ACTIVE.has(job.state)) {
    state.job = job
    startPoll()
  }
}

export function runningJob() {
  return state.job && ACTIVE.has(state.job.state) ? state.job : null
}

/** 重取项目列表与当前项目。列表里的项目被删掉时自动落到第一个。 */
export async function refresh() {
  if (!key()) return
  try {
    const overview = await api.overview(key())
    state.overview = overview
    const known = overview.projects.some(/** @param {any} item */ (item) => item.id === state.detail?.id)
    const remembered = rememberedProject()
    // 上次用的是哪本就用哪本（只有它被删掉时才落到列表第一本）：
    // 创作页打开时保持原书，主页「指定音色」问到的角色也属于这一本。
    const target = known
      ? state.detail.id
      : overview.projects.some(/** @param {any} item */ (item) => item.id === remembered)
        ? remembered
        : overview.projects[0]?.id || ''
    if (!target) {
      state.detail = null
      state.chapterId = ''
      state.rows = []
      return
    }
    await openProject(target)
  } catch (error) {
    fail(error)
  }
}

/**
 * @param {any} projectId
 */
export async function openProject(projectId) {
  if (!key()) return
  stopPoll()
  state.projectOpen = false
  state.job = null
  // 原文与规则属于「上一本书」：真的换了项目才清。新建项目后 refresh() 会再进
  // 这里一次，若不分青红皂白地清空，分章面板刚取回的原文会被这一下抹掉。
  if (state.detail?.id !== projectId) {
    activeMerge?.stop()
    state.source = null
    state.splitPreview = null
    state.textPreview = null
    state.splitOpen = false
    state.splitPattern = ''
    // 换了书角色名单就换了，旧角色留着会让整列章都标成「没有该角色」。
    state.speakerFilter = ''
  }
  try {
    const detail = await api.project(key(), projectId)
    state.detail = detail
    rememberProject(projectId)
    void loadMarks()
    if (detail.job && ACTIVE.has(detail.job.state)) {
      state.job = detail.job
      startPoll()
    }
    const chapterId =
      detail.chapters.find(/** @param {any} item */ (item) => item.id === state.chapterId)?.id ||
      detail.chapters[0]?.id ||
      ''
    await selectChapter(chapterId)
  } catch (error) {
    fail(error)
  }
}

/**
 * @param {any} chapterId
 */
export async function selectChapter(chapterId) {
  state.chapterId = chapterId
  const detail = state.detail
  if (!chapterId || !detail) {
    state.rows = []
    state.chapterText = ''
    return
  }
  try {
    const producerKey = key()
    const payload = await api.chapter(producerKey, detail.id, chapterId)
    if (key() !== producerKey || state.detail?.id !== detail.id || state.chapterId !== chapterId) return
    state.rows = payload.rows
    state.chapterText = payload.chapter?.text || ''
  } catch (error) {
    fail(error)
  }
}

/**
 * @param {any} name
 * @param {any} text
 */
export async function createProject(name, text) {
  state.busy = 'create'
  try {
    const detail = await api.createProject(key(), { name, text })
    state.detail = detail
    state.chapterId = detail.chapters[0]?.id || ''
    // 导入栏就在项目管理卡片里：建完关掉它，新书已经在背后打开了。
    state.projectOpen = false
    await refresh()
    return detail
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.busy = ''
  }
}

/**
 * 导入书稿：txt / md / epub / mobi 都交给后端解析，前端只传文件。
 *
 * @param {File} file
 * @param {any} name
 */
export async function importFile(file, name) {
  state.importing = true
  try {
    const detail = await api.importProject(key(), file, name)
    state.detail = detail
    state.chapterId = detail.chapters[0]?.id || ''
    // 导入栏就在项目管理卡片里：导完关掉它，新书已经在背后打开了。
    state.projectOpen = false
    await refresh()
    say(`已导入「${detail.name}」：${detail.stats.chapters} 章`, { kind: 'success' })
    return detail
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.importing = false
  }
}

export function openProjectManager() {
  state.projectOpen = true
}

export function closeProjectManager() {
  if (state.importing) return
  state.projectOpen = false
}

// ---- 分章与文本处理 -----------------------------------------------------

export async function loadPatterns() {
  if (state.patterns) return state.patterns
  try {
    state.patterns = await api.patterns(key())
  } catch (error) {
    fail(error)
  }
  return state.patterns
}

/** 拆句选项的可选值：模式名由后端给。 */
export async function loadSplitMeta() {
  if (state.splitMeta) return state.splitMeta
  try {
    state.splitMeta = await api.splitOptionsMeta(key())
  } catch (error) {
    fail(error)
  }
  return state.splitMeta
}

/**
 * @param {any} options
 */
export async function saveSplitOptions(options) {
  const detail = state.detail
  if (!detail) return null
  try {
    state.detail = await api.saveSplitOptions(key(), detail.id, options)
    say('拆句选项已保存')
    return state.detail
  } catch (error) {
    fail(error)
    return null
  }
}

/** 打开分章面板：取原文与规则表，模式默认沿用这本书上次选的。 */
export async function openSplit() {
  const detail = state.detail
  if (!detail) return
  state.splitOpen = true
  if (state.source) return // 已取过原文：再点只是把面板打开
  state.splitPreview = null
  state.textPreview = null
  await loadPatterns()
  try {
    const payload = await api.source(key(), detail.id)
    // 取的这一会儿里可能已经换了项目：过期的原文不许覆盖当前这本。
    if (state.detail?.id !== detail.id) return
    state.source = payload
    if (!state.splitPattern) {
      state.splitPattern =
        state.source.pattern_key || state.patterns?.default_pattern || 'general'
    }
  } catch (error) {
    fail(error)
  }
}

//: 模式切换会连续触发预览；只让最后一次请求写回结果。
let splitPreviewSeq = 0

export function closeSplit() {
  // 关面板时还在路上的预览结果不再回填；它回来时 seq 已经落后。
  splitPreviewSeq += 1
  if (state.busy === 'split-preview') state.busy = ''
  state.splitOpen = false
  state.splitPreview = null
  state.textPreview = null
}

/**
 * @param {any} patternKey
 */
export async function previewSplit(patternKey) {
  const detail = state.detail
  if (!detail) return
  const seq = ++splitPreviewSeq
  state.splitPattern = patternKey
  state.busy = 'split-preview'
  try {
    const payload = await api.previewSplit(key(), detail.id, patternKey)
    if (seq !== splitPreviewSeq) return
    state.splitPreview = payload
  } catch (error) {
    if (seq !== splitPreviewSeq) return
    state.splitPreview = null
    fail(error)
  } finally {
    if (seq === splitPreviewSeq) state.busy = ''
  }
}

/**
 * 真分章：旧章节、旧行、旧音频都会被换掉，调用前必须确认。
 *
 * @param {any} patternKey
 */
export async function splitProject(patternKey) {
  const detail = state.detail
  if (!detail) return null
  splitPreviewSeq += 1
  state.splitPattern = patternKey
  state.busy = 'split'
  try {
    const saved = await api.splitProject(key(), detail.id, patternKey)
    state.detail = saved
    state.splitOpen = false
    state.splitPreview = null
    await selectChapter(saved.chapters[0]?.id || '')
    say(`已分章：${saved.chapters.length} 章`, { kind: 'success' })
    return saved
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.busy = ''
  }
}

/**
 * @param {any} rules
 */
export async function previewText(rules) {
  const detail = state.detail
  if (!detail) return
  state.busy = 'text-preview'
  try {
    state.textPreview = await api.previewText(key(), detail.id, rules)
  } catch (error) {
    state.textPreview = null
    fail(error)
  } finally {
    state.busy = ''
  }
}

/**
 * 应用文本处理：作用于当前显示的对象；已拆句的章保留行，不强制重新拆句。
 *
 * @param {any} rules
 */
export async function applyText(rules) {
  const detail = state.detail
  if (!detail) return null
  const currentId = state.chapterId
  state.busy = 'text-apply'
  try {
    const saved = await api.applyText(key(), detail.id, rules)
    state.detail = saved
    state.splitPreview = null
    state.textPreview = null
    if (saved.scope === 'chapters' && saved.chapters.length) {
      // 回到刚才那一章：行还在，只把改动后的文本刷回来。
      state.splitOpen = false
      const keep = saved.chapters.some(/** @param {any} item */ (item) => item.id === currentId)
      await selectChapter(keep ? currentId : saved.chapters[0].id)
      if (saved.updated_rows) {
        say(`文本处理已应用：更新 ${saved.updated_rows} 行，无需重新拆句`, { kind: 'success' })
      } else {
        say(`文本处理已应用：更新 ${saved.updated_chapters} 章正文`, { kind: 'success' })
      }
    } else {
      state.source = await api.source(key(), saved.id)
      state.rows = []
      state.chapterId = ''
      say('文本处理已应用，请重新分章')
    }
    return saved
  } catch (error) {
    fail(error)
    return null
  } finally {
    state.busy = ''
  }
}

/**
 * AI 按钮状态：没配置好时按钮置灰并显示后端给的原因。
 *
 * @param {any} force
 */
export async function loadAssistStatus(force = false) {
  if (state.assistStatus && !force) return state.assistStatus
  try {
    state.assistStatus = await api.assistStatus(key())
  } catch (error) {
    state.assistStatus = {
      ready: false,
      note: error instanceof Error ? error.message : String(error),
    }
  }
  return state.assistStatus
}

/**
 * 一次 AI 提案。失败直接抛给调用方：不同入口要各自决定是内联说明还是弹窗。
 *
 * @param {any} task
 * @param {any} body
 */
export async function runAssist(task, body = {}) {
  const detail = state.detail
  if (!detail) throw new Error('还没有打开项目。')
  return api.assist(key(), task, { project_id: detail.id, ...body })
}

/**
 * 应用分配（AI 建议或手动改的）；``overwrite`` 为真时允许改写已有音色的角色。
 *
 * @param {any} assignments
 * @param {any} overwrite
 */
export async function assignVoices(assignments, overwrite = false) {
  const detail = state.detail
  if (!detail) return null
  try {
    const payload = await api.setVoices(key(), detail.id, { assignments, overwrite })
    state.detail = payload.detail
    void loadMarks()
    const skipped = payload.skipped?.length || 0
    say(
      skipped
        ? `已更新 ${payload.applied} 个角色，跳过 ${skipped} 个`
        : `已更新 ${payload.applied} 个角色`,
      { kind: skipped ? 'warn' : 'success' }
    )
    return payload
  } catch (error) {
    fail(error)
    return null
  }
}

/**
 * @param {any} chapterId
 */
export async function deleteChapter(chapterId) {
  const detail = state.detail
  if (!detail || !chapterId) return
  state.busy = 'delete-chapter'
  try {
    const index = detail.chapters.findIndex(/** @param {any} item */ (item) => item.id === chapterId)
    const saved = await api.deleteChapter(key(), detail.id, chapterId)
    state.detail = saved
    const next =
      saved.chapters[Math.min(index, saved.chapters.length - 1)]?.id || ''
    await selectChapter(next)
    say('章节已删除')
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/**
 * @param {any} projectId
 */
export async function deleteProject(projectId) {
  try {
    await api.deleteProject(key(), projectId)
    // 删的正是记住的那一本：把记录一并清掉，refresh 才会落到剩下的第一本。
    if (rememberedProject() === projectId) rememberProject('')
    // 删的是别的项目时，当前这本书不该被顺手切走。
    if (state.detail?.id === projectId) state.detail = null
    await refresh()
  } catch (error) {
    fail(error)
  }
}

/**
 * 改书名与作者：只传改过的那一项，后端按缺省保留原值。
 * ``projectId`` 缺省是当前这本书；项目管理里改别的项目时显式传 id，
 * 此时只更新清单条目，不动屏幕上正在编的那本。
 *
 * @param {any} profile
 * @param {any} projectId
 */
export async function updateProject(profile, projectId = state.detail?.id) {
  if (!projectId) return null
  try {
    const saved = await api.updateProject(key(), projectId, profile)
    if (state.detail?.id === saved.id) state.detail = saved
    // 目录刊头的书籍切换读清单里的名字，同步改掉，省一次全量刷新。
    const item = state.overview.projects.find((project) => project.id === saved.id)
    if (item) {
      item.name = saved.name
      item.author = saved.author
    }
    return saved
  } catch (error) {
    fail(error)
    return null
  }
}

/**
 * @param {any} title
 * @param {any} chapterId
 */
export async function renameChapter(title, chapterId = state.chapterId) {
  const detail = state.detail
  if (!detail || !chapterId) return
  try {
    state.detail = await api.renameChapter(key(), detail.id, chapterId, title)
  } catch (error) {
    fail(error)
  }
}

/** 重新拆一章：文本没变的行连音频一起保留，改过的才作废。 */
export async function resplitChapter() {
  const detail = state.detail
  const chapterId = state.chapterId
  if (!detail || !chapterId) return
  state.busy = 'resplit'
  try {
    const payload = await api.resplitChapter(key(), detail.id, chapterId)
    state.detail = payload
    await selectChapter(chapterId)
    announceSplit(payload.split_report)
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/**
 * 拆句质量只说一句：范围、行数与第一条警告，其余进日志。
 *
 * @param {any} report
 */
function announceSplit(report) {
  if (!report) return
  const warnings = report.warnings || []
  const tail = warnings.length
    ? `；⚠ ${warnings[0]}${warnings.length > 1 ? `（另 ${warnings.length - 1} 条）` : ''}`
    : ''
  const scope = report.chapters ? `${report.chapters} 章 · ` : ''
  const kept = report.kept ? `，沿用 ${report.kept} 行音频` : ''
  say(`拆句完成：${scope}${report.rows} 行，对话 ${report.dialogue} 行${kept}${tail}`)
}

/**
 * 跑一步：分章/拆句是同步的；分析/生成/响度处理返回任务，进度由轮询推进。
 * 拆句默认重切全部章节（文本未变的行保留角色、情绪与音频）；`scope: 'pending'`
 * 只处理还没有行的章。两个入口同一套规则，差别只在范围。
 *
 * @param {any} step
 * @param {any} options
 */
export async function runStep(step, options = {}) {
  const detail = state.detail
  if (!detail) return
  state.busy = step
  try {
    const payload = await api.runStep(
      key(),
      detail.id,
      step,
      step === 'sentences' ? { scope: options.scope || 'all' } : undefined
    )
    if (payload?.kind && payload?.state) {
      state.job = payload
      // 起点就有话要说：跳过了未分配音色的行，或全部已是最新。报一次，别让人干等。
      if (payload.message) say(payload.message)
      if (ACTIVE.has(payload.state)) startPoll()
      else await refresh()
      return
    }
    state.detail = payload
    await selectChapter(state.chapterId)
    if (step === 'sentences') announceSplit(payload.split_report)
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

function startPoll() {
  stopPoll()
  const startedAt = Date.now()
  const tick = async () => {
    const current = state.job
    if (!current?.id) return
    try {
      const job = await api.job(key(), current.id)
      state.job = job
      if (ACTIVE.has(job.state)) {
        // 前 30 秒密一点（行级进度好看），之后放慢：整本生成要按小时计。
        pollTimer = window.setTimeout(tick, Date.now() - startedAt > 30_000 ? 3000 : 800)
        return
      }
      await refresh()
      if (job.state === 'failed') {
        say(job.message || '任务失败', { kind: 'error' })
      } else if (job.state === 'done' && (job.total || job.kind === 'loudness')) {
        const label = job.kind === 'loudness' ? '响度处理' : job.kind === 'audio' ? '生成' : '分析'
        const summary = job.failed
          ? `${label}结束：成功 ${job.done} 行，失败 ${job.failed} 行`
          : `${label}完成：${job.done} 行`
        say(job.kind === 'loudness' ? job.message || summary : summary, {
          kind: job.failed ? 'warn' : 'success',
        })
      }
    } catch (error) {
      fail(error)
    }
  }
  pollTimer = window.setTimeout(tick, 500)
}

function stopPoll() {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = 0
}

/** 离开创作页时收掉任务轮询；回来时 openProject 会按运行中的任务重新起。 */
export function stopJobPolling() {
  stopPoll()
}

/** 取消当前任务：后端在批次/行之间检查，已生成的部分保留。 */
export async function cancelJob() {
  if (!state.job?.id) return
  try {
    state.job = await api.cancelJob(key(), state.job.id)
    // 取消是请求标志，真正停下要等当前批次；继续轮询到终态。
    startPoll()
  } catch (error) {
    fail(error)
  }
}

/**
 * 行级参数：只给对话框用，不进全局状态——换一行就重取。
 *
 * @param {any} rowId
 */
export async function loadRowParams(rowId) {
  const detail = state.detail
  if (!detail) return null
  return api.rowParams(key(), detail.id, rowId)
}

/**
 * 保存行级参数：后端会作废这一行的音频（指纹里含参数），随后按需重新生成。
 *
 * @param {any} rowId
 * @param {any} params
 */
export async function saveRowParams(rowId, params) {
  const detail = state.detail
  if (!detail) return null
  const next = await api.saveRowParams(key(), detail.id, rowId, params)
  const index = state.rows.findIndex(/** @param {any} item */ (item) => item.id === rowId)
  if (index >= 0) state.rows[index] = next
  state.detail = await api.project(key(), detail.id)
  return next
}

/**
 * 单行生成 / 重试：失败原因由后端写在那一行上，前端重取一次把红标显示出来。
 * 成功返回那一行（调用方据此报「已生成」），失败返回 null。
 *
 * @param {any} rowId
 */
export async function retryRow(rowId) {
  const detail = state.detail
  if (!detail) return null
  try {
    const next = await api.retryRow(key(), detail.id, rowId)
    const index = state.rows.findIndex(/** @param {any} item */ (item) => item.id === rowId)
    if (index >= 0) state.rows[index] = next
    state.detail = await api.project(key(), detail.id)
    return next
  } catch (error) {
    fail(error)
    // 别让行停在「待生成」：失败也是一条状态，刷新才能看见重试按钮。
    try {
      state.detail = await api.project(key(), detail.id)
      if (state.chapterId) await selectChapter(state.chapterId)
    } catch {
      // 刷新失败就保持原样：这条错已经报过一次，不再叠第二条。
    }
    return null
  }
}

/**
 * 合并会话固定生产方与项目；切换后旧请求只结束自身，不覆盖新项目。
 *
 * @param {any} kind
 * @param {any} projectId
 */
function beginMerge(kind, projectId) {
  const producerKey = key()
  /** @type {any} */
  const session = {
    producerKey,
    projectId,
    stopping: false,
    stopPolling: null,
    current: () =>
      activeMerge === session && key() === producerKey && state.detail?.id === projectId,
    stop: () => {
      session.stopPolling?.()
      if (activeMerge !== session) return
      activeMerge = null
      state.mergeProgress = null
      state.mergeBatch = null
      state.mergeStopping = false
      if (state.busy === kind) state.busy = ''
    },
  }
  activeMerge = session
  state.mergeStopping = false
  state.busy = kind
  return session
}

/**
 * 仅在单章 POST 等待期间轮询，避免慢请求重叠；结束或切项目时中止在途 GET。
 *
 * @param {any} session
 * @param {any} chapterId
 */
async function mergeWithProgress(session, chapterId) {
  if (!session.current()) return null
  state.mergeProgress = null
  const controller = new AbortController()
  let polling = true
  let inFlight = false
  const timer = window.setInterval(async () => {
    if (!session.current()) {
      session.stop()
      return
    }
    if (inFlight) return
    inFlight = true
    try {
      const payload = await api.mergeProgress(session.producerKey, session.projectId, controller.signal)
      if (!polling || !session.current()) return
      const progress = payload?.merge_progress ?? payload
      state.mergeProgress = progress?.message ? progress : null
    } catch {
      // 进度读取失败不改变合并结果，保留现有逐章进度与最后一条阶段说明。
    } finally {
      inFlight = false
    }
  }, 800)
  const stop = () => {
    polling = false
    window.clearInterval(timer)
    controller.abort()
    if (session.current()) state.mergeProgress = null
  }
  session.stopPolling = stop
  try {
    return await api.mergeChapter(session.producerKey, session.projectId, chapterId)
  } finally {
    stop()
    if (session.stopPolling === stop) session.stopPolling = null
  }
}

/** 停止正在进行的合并：后端终止当前 FFmpeg；已完成章节保留，当前章不落成品。 */
export async function stopMerge() {
  const session = activeMerge
  if (!session || !['merge', 'merge-all'].includes(state.busy) || state.mergeStopping) return
  session.stopping = true
  state.mergeStopping = true
  try {
    await api.cancelMerge(session.producerKey, session.projectId)
  } catch (error) {
    if (session.current()) fail(error)
  }
}

/**
 * @param {any} result
 * @param {any} title
 */
function announceLoudness(result, title = '') {
  const warnings = result?.loudness?.warnings
  if (warnings?.length) {
    say(`${title ? `「${title}」：` : ''}${warnings.join('；')}`, { kind: 'warn' })
  }
}

export async function mergeChapter() {
  const detail = state.detail
  if (!detail || !state.chapterId || state.busy || runningJob()) return
  const session = beginMerge('merge', detail.id)
  try {
    const result = await mergeWithProgress(session, state.chapterId)
    if (!session.current()) return
    announceLoudness(result)
    const fresh = await api.project(session.producerKey, detail.id)
    if (!session.current()) return
    state.detail = fresh
    await selectChapter(state.chapterId)
    if (!session.current()) return
    // 贴齐与顺延是同一件事的两面：贴齐的行已经回到原位置，顺延的行没能贴回去。
    if (result?.fitted?.length) {
      say(`合并完成：${result.fitted.length} 行变速贴齐原字幕时隙`, { kind: 'success' })
    }
    // 顺延：前文音频长过原字幕时隙，后面的只能往后挪——字幕按实际时间轴导出，
    // 但要让用户知道是哪几行（书稿项目没有这一项，`delayed` 为空）。
    if (result?.delayed?.length) {
      const head = result.delayed.slice(0, 5).join('、')
      say(`合并完成：${result.delayed.length} 行顺延（第 ${head} 行${result.delayed.length > 5 ? ' 起' : ''}）`, {
        kind: 'warn',
      })
    }
    // 自检不过才说话：时长与时间轴差太多，听众会发现，先说一声。
    if (result?.check?.checked && !result.check.ok) {
      say(`成品自检：${result.check.note}`, { kind: 'error' })
    }
  } catch (error) {
    const stopped =
      session.stopping ||
      (error instanceof Error ? error.message : String(error)).includes('合并已停止')
    if (stopped) {
      say('合并已停止', { kind: 'warn' })
      try {
        state.detail = await api.project(session.producerKey, detail.id)
        await selectChapter(state.chapterId)
      } catch {
        // 停止后的刷新失败不覆盖已有的停止提示。
      }
    } else if (session.current()) {
      fail(error)
    }
  } finally {
    session.stop()
  }
}

/**
 * 逐章合并全部待合并的章节：串行调用单章接口，不拼成一个全书文件。
 * 已合并的章节跳过——刷新页面、中途失败后再点一次，都只接着做剩下的，不从头重来。
 * ``force`` 为真时才连同已合并的章节一起重做（改了合并设置后要整本重出）。
 *
 * @param {any} options
 */
export async function mergeAllChapters(options = {}) {
  const force = Boolean(options.force)
  const detail = state.detail
  if (!detail || state.busy || runningJob()) return
  const withAudio = detail.chapters.filter(/** @param {any} item */ (item) => Number(item.done) > 0)
  const merged = withAudio.filter(/** @param {any} item */ (item) => item.stage === 'merged' && item.audio)
  const skippedMerged = new Set(merged.map(/** @param {any} item */ (item) => item.id))
  const targets = force
    ? withAudio
    : withAudio.filter(/** @param {any} item */ (item) => !(item.stage === 'merged' && item.audio))
  const skipped = detail.chapters.length - withAudio.length
  if (!targets.length) {
    const tail = merged.length ? `全部 ${merged.length} 章已合并` : ''
    say(tail || '没有可合并的章节：先生成音频。', { kind: tail ? 'success' : 'warn' })
    return
  }
  const session = beginMerge('merge-all', detail.id)
  state.mergeBatch = { done: 0, total: targets.length, title: '' }
  let current = ''
  let failure = null
  let stopped = false
  let done = 0
  try {
    try {
      for (const chapter of targets) {
        if (!session.current()) return
        if (session.stopping) {
          stopped = true
          break
        }
        // 每章之前重取一次：另一窗口可能已经合完，保留既有的跳过与续做规则。
        if (!force) {
          const fresh = await api.project(session.producerKey, detail.id)
          if (!session.current()) return
          state.detail = fresh
          const live = fresh.chapters.find(/** @param {any} item */ (item) => item.id === chapter.id)
          if (!live || !(Number(live.done) > 0)) continue
          if (live.stage === 'merged' && live.audio) {
            done += 1
            state.mergeBatch.done = done
            continue
          }
        }
        current = chapter.title
        state.mergeBatch.title = current
        const result = await mergeWithRetry(session, chapter.id)
        if (!session.current()) return
        if (session.stopping) {
          if (result) {
            announceLoudness(result, current)
            skippedMerged.delete(chapter.id)
            done += 1
            state.mergeBatch.done = done
          }
          stopped = true
          break
        }
        announceLoudness(result, current)
        skippedMerged.delete(chapter.id)
        if (!force && result?.loudness?.rows?.enabled) {
          const fresh = await api.project(session.producerKey, detail.id)
          if (!session.current()) return
          state.detail = fresh
          const queued = new Set(targets.map(/** @param {any} item */ (item) => item.id))
          for (const item of fresh.chapters) {
            if (Number(item.done) > 0 && !(item.stage === 'merged' && item.audio) && !queued.has(item.id)) {
              targets.push(item)
            }
          }
          state.mergeBatch.total = targets.length
        }
        done += 1
        state.mergeBatch.done = done
      }
    } catch (error) {
      if (session.stopping) stopped = true
      else failure = error
    }
    if (!session.current()) return
    state.mergeBatch.done = done
    // 无论中途是否失败，都刷新一次：目录显示已经合完的章节，失败章保持原样。
    try {
      const fresh = await api.project(session.producerKey, detail.id)
      if (session.current()) {
        state.detail = fresh
        await selectChapter(state.chapterId)
      }
    } catch {
      // 刷新失败就保留当前画面；合并结果仍由 POST 的响应决定。
    }
    if (!session.current()) return
    if (stopped) {
      say(`已停止合并：完成 ${done} 章，后续章节未合并。`, { kind: 'warn' })
    } else if (failure) {
      const reason = failure instanceof Error ? failure.message : String(failure)
      fail(new Error(`合并「${current}」失败（已完成 ${done} 章）：${reason}`))
    } else if (force) {
      say(`重新合并完成：${done} 章。`, { kind: 'success' })
    } else {
      const tail = [
        skippedMerged.size ? `跳过 ${skippedMerged.size} 章已合并` : '',
        skipped ? `跳过 ${skipped} 章（没有音频）` : '',
      ]
        .filter(Boolean)
        .join('，')
      say(`合并完成：${done} 章${tail ? `，${tail}` : ''}。`, { kind: 'success' })
    }
  } finally {
    session.stop()
  }
}

/** 同一项目同时只允许一路合并：撞上时等一会儿再试，而不是把整批判死。 */
const MERGE_BUSY = '正在合并'

/**
 * @param {any} session
 * @param {any} chapterId
 * @param {any} attempts
 */
async function mergeWithRetry(session, chapterId, attempts = 3) {
  for (let round = 1; ; round += 1) {
    if (!session.current()) return null
    if (session.stopping) throw new Error('合并已停止')
    try {
      return await mergeWithProgress(session, chapterId)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (round >= attempts || !message.includes(MERGE_BUSY)) throw error
      await new Promise((resolve) => setTimeout(resolve, 3000))
    }
  }
}

/** 导出：拉成 blob 再交给浏览器，失败时留在这个页面而不是跳去错误页。 */
export async function downloadChapters() {
  const detail = state.detail
  if (!detail) return
  state.busy = 'export'
  try {
    const resp = await fetch(api.exportUrl(key(), detail.id))
    if (!resp.ok) throw new Error(`导出失败（${resp.status}）`)
    const url = URL.createObjectURL(await resp.blob())
    const link = document.createElement('a')
    link.href = url
    // 包名就是书名：不带「分章」这类后缀，播放器与资源管理器里一眼认得出是哪本。
    link.download = `${detail.name}.zip`
    link.click()
    URL.revokeObjectURL(url)
    say('ZIP 已开始下载', { kind: 'success' })
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

export async function downloadM4b() {
  const detail = state.detail
  if (!detail) return
  state.busy = 'export-m4b'
  try {
    const resp = await fetch(api.exportM4bUrl(key(), detail.id))
    if (!resp.ok) throw new Error(`导出失败（${resp.status}）`)
    const url = URL.createObjectURL(await resp.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `${detail.name}.m4b`
    link.click()
    URL.revokeObjectURL(url)
    say('M4B 已开始下载', { kind: 'success' })
  } catch (error) {
    fail(error)
  } finally {
    state.busy = ''
  }
}

/**
 * @param {any} speaker
 * @param {any} voice
 * @param {any} options
 */
export async function assignVoice(speaker, voice, options = {}) {
  const detail = state.detail
  if (!detail) return
  try {
    state.detail = await api.setVoice(key(), detail.id, {
      speaker,
      engine: voice?.source || '',
      voice_id: voice?.id || '',
      voice_name: voice?.shown_name || '',
      // 样例策略：按行情绪匹配，或钉住一条；平台音色用不上，后端会忽略。
      sample_mode: options.sample_mode || 'emotion',
      sample_file: options.sample_file || '',
    })
    void loadMarks()
  } catch (error) {
    fail(error)
  }
}

/**
 * 只换引擎、不动音色：本地音色在几台库通道引擎之间切换时用。
 *
 * @param {any} speaker
 * @param {any} engine
 */
export async function setVoiceEngine(speaker, engine) {
  const mapping = state.detail?.voices?.[speaker]
  if (!mapping?.voice_id || !engine) return
  await assignVoice(
    speaker,
    { id: mapping.voice_id, shown_name: mapping.voice_name, source: engine },
    { sample_mode: mapping.sample_mode, sample_file: mapping.sample_file }
  )
}

/**
 * 人工修正一行：说话人 / 情绪 / 文本。角色清单会随之变化，所以顺带重取项目。
 *
 * @param {any} rowId
 * @param {any} patch
 */
export async function editRow(rowId, patch) {
  const detail = state.detail
  if (!detail) return
  try {
    const next = await api.updateRow(key(), detail.id, rowId, patch)
    const index = state.rows.findIndex(/** @param {any} item */ (item) => item.id === rowId)
    if (index >= 0) state.rows[index] = next
    state.detail = await api.project(key(), detail.id)
  } catch (error) {
    fail(error)
  }
}

/**
 * 结构编辑后统一回填：项目详情 + 当前章行表。
 *
 * @param {any} payload
 */
async function applyDetail(payload) {
  state.detail = payload
  await selectChapter(state.chapterId)
}

/**
 * @param {any} rowId
 */
export async function deleteRow(rowId) {
  const detail = state.detail
  if (!detail) return
  try {
    await applyDetail(await api.deleteRow(key(), detail.id, rowId))
  } catch (error) {
    fail(error)
  }
}

/**
 * @param {any} rowId
 * @param {number} at
 */
export async function splitRow(rowId, at) {
  const detail = state.detail
  if (!detail) return
  try {
    await applyDetail(await api.splitRow(key(), detail.id, rowId, at))
  } catch (error) {
    fail(error)
  }
}

/**
 * 批量删除：后端一次删一行，这里串起来，最后一次详情回填。
 *
 * @param {any[]} ids
 */
export async function deleteRows(ids) {
  const detail = state.detail
  if (!detail) return
  try {
    let payload = null
    for (const rowId of ids) {
      payload = await api.deleteRow(key(), detail.id, rowId)
    }
    if (payload) await applyDetail(payload)
  } catch (error) {
    fail(error)
  }
}

/**
 * @param {any[]} ids
 */
export async function mergeRows(ids) {
  const detail = state.detail
  if (!detail || ids.length < 2) return
  try {
    await applyDetail(await api.mergeRows(key(), detail.id, ids))
  } catch (error) {
    fail(error)
  }
}

/**
 * 自动合并：短旁白并进后面的旁白。以行数差回报结果，界面不必自己算。
 *
 * @param {any} chapterId
 * @param {number} maxChars
 */
export async function autoMergeRows(chapterId, maxChars) {
  const detail = state.detail
  if (!detail || !chapterId) return
  const before = state.rows.length
  try {
    await applyDetail(await api.autoMergeRows(key(), detail.id, chapterId, maxChars))
    say(`自动合并：并掉 ${before - state.rows.length} 行`, { kind: 'success' })
  } catch (error) {
    fail(error)
  }
}

/**
 * 全书自动合并：范围是整本，行数按各章行数合计（当前章只是其中一节）。
 *
 * @param {number} maxChars
 */
export async function autoMergeAllRows(maxChars) {
  const detail = state.detail
  if (!detail || !detail.chapters?.length) return
  const total = /** @param {any} data */ (data) =>
    (data?.chapters || []).reduce(/** @param {number} sum @param {any} item */ (sum, item) => sum + Number(item.rows || 0), 0)
  const before = total(detail)
  try {
    const next = await api.autoMergeAllRows(key(), detail.id, maxChars)
    await applyDetail(next)
    say(`全书自动合并：并掉 ${Math.max(0, before - total(next))} 行`, { kind: 'success' })
  } catch (error) {
    fail(error)
  }
}

/**
 * 合并角色：把表里这些说话人统一成一个名字（旧音频作废，重新生成时按新角色配音）。
 *
 * @param {any} names
 * @param {any} target
 */
export async function mergeSpeakers(names, target) {
  const detail = state.detail
  if (!detail || names.length < 2) return
  try {
    await applyDetail(await api.mergeSpeakers(key(), detail.id, { names, target }))
    say(`已把 ${names.length} 个角色合并为「${target}」`, { kind: 'success' })
  } catch (error) {
    fail(error)
  }
}

/**
 * 批量改角色 / 情绪；只传要改的那几项。
 *
 * @param {any[]} ids
 * @param {any} patch
 */
export async function batchUpdateRows(ids, patch) {
  const detail = state.detail
  if (!detail || !ids.length) return
  try {
    await applyDetail(await api.batchUpdateRows(key(), detail.id, { ids, ...patch }))
  } catch (error) {
    fail(error)
  }
}
