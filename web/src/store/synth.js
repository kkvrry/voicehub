// @ts-check
// 生成这一域（docs/request-spec.md 那一套）：规范、一个音色扇出给多台引擎、
// 以及平台音色详情里那条"一台引擎一个音色一句话"的试听。两条路是两件事：
// 扇出勾几台，试听只有一条结果。
import { api } from '../api'
import { store } from './state'
import { engineOf, loadEngines, loadMarkGroups, orderedEngines } from './engine'
import { say } from './shell'
import { ensureLexicon } from './lexicon'
import { loadTree, loadVoices } from './library'
import { dropTrack } from './playback'

/**
 * 请求体怎么组装——`emotion` 与 `format` 是顶层格，其余进 `extra`。
 * - 空串一律不发：规范里"不写 = 没要求"，发空串等于替引擎要求一个空值。
 * - 填了 0 要发：`lead_silence: 0` 是明确要求，当"没要求"丢掉会吃掉那次静默。
 * 值的类型由规范的 `type` 决定，前端不按字段名猜。
 *
 * @param {string} text
 * @param {string} voiceId
 * @param {Record<string, any>} values
 * @param {string} engine
 */
function requestBody(text, voiceId, values, engine) {
  const types = Object.fromEntries(
    store.spec.fields.map(
      /** @param {any} f */
      (f) => [f.key, f.type]
    )
  )
  /** @type {Record<string, any>} */
  const extra = {}
  for (const [key, raw] of Object.entries(values)) {
    if (key === 'emotion' || key === 'format') continue
    if (raw === '' || raw === null || raw === undefined) continue
    extra[key] = types[key] === 'number' ? Number(raw) : raw
  }
  return {
    engine,
    voice_id: voiceId,
    text,
    emotion: values.emotion || '',
    format: values.format || '',
    extra,
  }
}

export async function loadSpec() {
  store.spec = await api.spec()
}

/** 一台引擎"认得"的那几格：映射表里这一格有落点，才发得出去。
 *  `voice` / `reference` 由各自的入口摆（音色在库里挑、参考音频在生成页），
 *  `text` 是文案本身，不在这份参数列里。
 *
 *  「生成样例」的窗口与标题行那颗按钮上的小点共用这一条判据——两处各写一遍的话，
 *  按钮会替窗口说错话。 */
const NOT_PARAMS = ['voice', 'reference']

/** @param {any} engine */
export function auditionFields(engine) {
  if (!engine) return []
  const accepted = new Set(
    (engine.mapping || []).map(
      /** @param {any} p */
      (p) => p.source
    )
  )
  return store.spec.fields.filter(
    /** @param {any} f */
    (f) => accepted.has(f.key) && f.key !== 'text' && !NOT_PARAMS.includes(f.widget)
  )
}

/**
 * 这一份值里有几格是"要求过的"（空串 / 没这个键 = 没要求）。
 * @param {any[]} fields
 * @param {Record<string, any>} values
 * @returns {number}
 */
export function countSet(fields, values) {
  return fields.filter(
    /** @param {any} field */
    (field) => {
      const value = values[field.key]
      return value !== '' && value !== undefined && value !== null
    }
  ).length
}

/** 当前列表是否已经是不带筛选的全量（左栏「全部音色」且五个筛选格都为空）。 */
function showingAll() {
  const filters = store.filters
  return (
    store.selection.kind === 'all' &&
    !filters.q &&
    !filters.gender &&
    !filters.age &&
    !filters.language &&
    !filters.tag
  )
}

/** 生成页 / 创作页共用的全量名单。
 *
 *  当前列表本来就是全量时直接复用 `store.voices`，不再发第二份几千条的请求；
 *  否则拉一份，并把 id 相同的换成 `store.voices` 里的对象——两页拿到的是同一批
 *  音色对象，详情里改一格，列表与全量名单一起变，也少一份深响应式代理。
 *
 *  :param force: 刚建完音色等"列表未必包含新条目"的场合传 true，跳过复用直接重取。
 */
export async function loadAllVoices(force = false) {
  if (!force && showingAll() && store.voices.length) {
    store.allVoices = store.voices
    return
  }
  const all = await api.voices({})
  const known = new Map(store.voices.map((voice) => [voice.id, voice]))
  store.allVoices = all.map((voice) => known.get(voice.id) || voice)
}

/** 打开「生成」：规范、语气词、音色名单都只有这一页要用，所以到这一刻才取。
 *  列表（`store.voices`）也取一份：最左那列音色卡片就是它，跟着左栏树走。 */
export async function openSynth() {
  store.view = 'synth'
  // 种子的显示默认就是 -1（随机）。不写进 values 的话，界面写着 -1、实际发的是
  // 引擎配置里的 0——"我填了 -1 怎么发的是 0"就是这么来的。
  if (store.synth.values.seed === undefined) store.synth.values.seed = -1
  await Promise.all([
    loadSpec(),
    loadMarkGroups(),
    loadEngines(),
    loadVoices(),
    // 引导指令预设（用户词表的生效值）：后端一次算清，含各引擎默认 + 用户自定义。
    ensureLexicon(),
  ])
  // 列表先到位，全量名单才能复用它（见 loadAllVoices）：并行会让复用落空，
  // 两页各拿一份几千条的对象图。
  await loadAllVoices()
}

/** 样例名单的取数序号：慢请求回来时若已经换过音色，它手上那份结果就不再作数。 */
let samplesTicket = 0

/** 「参考音频」那一格的名单 = 当前音色的样例（本地音色才有）。
 *  换音色时旧样例立刻作废：不清的话，那个路径会跟着新音色一起发出去。
 *  已经「使用」过的那一条（`pinned`）跟着同一套判定走：同一条音色才留，
 *  取回样例后重新落回去——否则换一次音色就得再挑一遍。
 */
export async function loadSynthSamples() {
  const { synth } = store
  const ticket = ++samplesTicket
  if (synth.pinned && synth.pinned.voiceId !== synth.voiceId) synth.pinned = null
  const pinnedFile = synth.pinned?.file || ''
  synth.samples = []
  synth.recording = null
  delete synth.values.reference_audio
  delete synth.values.reference_text
  delete synth.values.emotion
  if (!synth.voiceId) return
  // 这个 id 库里未必有（那一格允许手打 id）：没有可选样例是正常状态，
  // 不接住的话控制台会躺一个没头没尾的 404。
  try {
    const detail = await api.voice(synth.voiceId)
    if (ticket !== samplesTicket) return
    synth.samples = detail.samples || []
    // 这一趟取列表期间用户录了一段：录音优先，别让样例把参考路径换回去
    // （否则卡片上仍是那段录音，实际发出去的却是音色的主样例）。
    if (synth.recording) return
    // 主样例就是"默认参考"：显式写进 values，别指望中转站补。
    // 平台音色（自带本地样例的那几家，如 MiMo）在库里没有 main_sample 可查，
    // 中转站补不出参考音频——扇出给需要它的引擎（BreezeTTS / GPT-SoVITS）时会
    // 报"参考音频与引导指令均为空"。这里带上，路径与逐字稿成对。
    const main = synth.samples.find((s) => s.is_main) || synth.samples[0]
    if (main?.path) {
      synth.values.reference_audio = main.path
      synth.values.reference_text = main.text || ''
    }
    // 钉住的那一条：写了文件名就是详情里挑的，空着就是"点卡片 = 用它的主样例"。
    // 文件已经不在了也落回主样例——钉一个看不见的空位，用户只会以为参考丢了。
    if (synth.pinned) {
      const target = (pinnedFile && synth.samples.find((s) => s.file === pinnedFile)) || main
      if (target?.path) {
        synth.pinned = { voiceId: synth.voiceId, file: target.file }
        synth.values.reference_audio = target.path
        synth.values.reference_text = target.text || ''
      } else if (!synth.samples.length) {
        // 设计音色（以及"一条样例都没有的音色"）：没有文件可钉，钉住的是这条音色本身。
        // 参考音频留空——描述才是它的凭据，这里不该编一段文件出来。
        synth.pinned = { voiceId: synth.voiceId, file: '' }
        delete synth.values.reference_audio
        delete synth.values.reference_text
      } else {
        synth.pinned = null
      }
    }
  } catch {
    synth.samples = []
  }
}

/**
 * 挑一条参考音频 = 用哪段声音（连带定下它念的那句话）。
 * 参考文本一起发（它们本来就是一对）；**情绪不动**——克隆靠参考音频本身带出
 * 语气，样例上的情绪标签不由这里代填，要加由用户自己填那一格。
 * 传空只撤音频与文本，让中转站照主样例去补；情绪仍不动。
 *
 * @param {string} file
 */
export function setReferenceAudio(file) {
  const { synth } = store
  // 挑了样例就等于不用那段录音了：录音不是"选中的一项"，它只是上一次录的东西。
  synth.recording = null
  const sample = synth.samples.find((s) => s.file === file)
  if (!sample || !sample.path) {
    delete synth.values.reference_audio
    delete synth.values.reference_text
    // 撤掉参考 = 顶上那张卡也没有意义了：它说的是"这次用这一段"。
    synth.pinned = null
    return
  }
  synth.values.reference_audio = sample.path
  synth.values.reference_text = sample.text || ''
  synth.pinned = { voiceId: synth.voiceId, file: sample.file }
}

/**
 * 详情卡里的「使用」：这一条样例就是这次的参考音频，卡片列顶上立刻钉住它。
 * 与 `setReferenceAudio` 的分别：这条路的样例来自详情（可能是刚点开、`samples`
 * 还没取回来的那条音色），所以路径与逐字稿直接用样例对象给的值，不全靠名单。
 * 换音色时先落 `pinned` 再改 `voiceId`：`loadSynthSamples` 会照 `pinned` 复原，
 * 反过来的话那一次重取会把它抹成主样例。
 *
 * @param {any} voice
 * @param {any} sample
 */
export function pinReference(voice, sample) {
  const { synth } = store
  if (!sample?.path) return
  const voiceId = voice?.id || synth.voiceId
  if (voiceId) synth.pinned = { voiceId, file: sample.file }
  if (voiceId && synth.voiceId !== voiceId) synth.voiceId = voiceId
  synth.recording = null
  synth.values.reference_audio = sample.path
  synth.values.reference_text = sample.text || ''
}

/**
 * 麦克风录的一段当这次的参考音频（落盘后的路径由后端给）。
 * 录音**不在** `samples` 名单里：它不是这条音色的样例，只是"这一次用哪段声音"，
 * 所以另记一份并让「参考音频」那一格认它——混进名单会让它看着像可以当主样例的东西。
 *
 * @param {any} payload
 */
export function setReferenceRecording(payload) {
  const { synth } = store
  const path = String(payload?.path || '')
  if (!path) return
  const name = String(payload?.name || path.split(/[\\/]/).pop() || '录音')
  synth.recording = {
    name,
    path,
    text: String(payload?.text || ''),
    // 临时录音也能试听：地址由后端那个只认临时目录的端点给。
    audio_url: String(payload?.audio_url || `/api/v1/recordings/${encodeURIComponent(name)}`),
  }
  synth.values.reference_audio = path
  synth.values.reference_text = synth.recording.text
  // 录音顶掉样例：顶上那张卡跟着换成这段录音（同一件事只有一个落点）。
  synth.pinned = null
}

/**
 * 详情里那次「生成样例」：一台引擎、一个音色、一句话，只有一条结果。
 * 引擎与音色 id 由调用方显式给（`options`）：落在引擎保存目录里的音色是本地音色，
 * MiniMax 这类只认自己的音色 id，把本地 id 发过去会被拒。失败时照原样摆引擎的话。
 *
 * @param {any} voice
 * @param {{ engineKey?: string, voiceId?: string }} [options]
 */
export async function runAudition(voice, options = {}) {
  const { text, values } = store.audition
  if (!text.trim()) return
  const engineKey = options.engineKey || voice.source
  const voiceId = options.voiceId || voice.id
  store.audition.running = true
  store.audition.job = { state: 'running', detail: '', saved_rel: '', notes: [] }
  try {
    // `persist: 'sample'` 是这一路与生成页唯一的分野：落进音色保存目录、当场成为它的一条样例
    //（生成页那一路是 'cache'，落进缓存目录；第三方 API 不带，服务端默认不落盘）。
    const result = await api.synthesize({
      ...requestBody(text, voiceId, values, engineKey),
      persist: 'sample',
    })
    const job = store.audition.job
    job.state = 'ok'
    job.detail = `${result.format} · ${(result.bytes / 1024).toFixed(0)} KB`
    job.saved_rel = result.saved_rel || ''
    // "这一次替你做了什么" + 引擎自己报的"哪几件没做到"，都在 notes 里。
    job.notes = result.notes || []
    // 刚生成的东西当场就是它的样例，所以重取一次详情：不重取的话用户会以为白花了钱。
    // 两个 id 可能是同一条，也可能是同一条的两半——两个都刷新。
    const touched = new Set([voice.id, voiceId])
    if (store.detail && touched.has(store.detail.id)) {
      store.detail = await api.voice(store.detail.id)
    }
    await loadVoices()
  } catch (error) {
    const job = store.audition.job
    job.state = 'failed'
    job.detail = error instanceof Error ? error.message : String(error)
    say(job.detail, { kind: 'error' })
  } finally {
    store.audition.running = false
  }
}

/**
 * 一台引擎这一轮的结果格子：先摆成"生成中"，等请求回来再填。
 *
 * @param {string} key
 */
function newJob(key) {
  return {
    key,
    label: engineOf(key)?.label || key,
    state: 'running',
    detail: '',
    audio_url: '',
    cost_note: '',
    saved_path: '', // 音频落在磁盘上的绝对路径（悬停看得到）
    saved_rel: '', // 相对生成结果目录的路径（= 归档结构，直接显示）
    notes: [], // 这一次"替你做了什么"以及"哪几件没做到"，成功也要说
    stale: false, // 上一轮留下的结果（这一轮没勾它）
  }
}

/** 一个音色扇出给勾选的每一台引擎，**各跑各的**（一台报错不该拦住别的）。
 *
 *  只重跑勾上的那几台：没勾的留着上一次的结果。它这一轮没有发请求，音频也没有被
 *  谁取代——收走它等于替用户删掉他自己生成的那一条（取消勾选是"这次不生成"，
 *  不是"把上次的丢掉"）。
 */
export async function runSynthesis() {
  const { text, voiceId, engines, values } = store.synth
  store.synthRunning = true
  /** 这一轮真正要跑的：`{ key, job }`，顺序就是勾选顺序。 */
  const fresh = new Map(engines.map((key) => [key, newJob(key)]))
  // 没参与这一轮的旧结果原样留着，只标上"不是这一轮的"（悬停里说清，免得
  // 用户以为取消勾选之后它又跑了一次）。
  const kept = store.synthJobs
    .filter((job) => !fresh.has(job.key))
    .map((job) => ({ ...job, stale: true }))
  // 摆回引擎行的顺序：产物列表同时是「全部播放」的队列，顺序得跟屏幕一致。
  const rank = new Map(orderedEngines(store.engines).map((engine, at) => [engine.key, at]))
  const order = (/** @type {any} */ job) => rank.get(job.key) ?? Number.MAX_SAFE_INTEGER
  store.synthJobs = [...fresh.values(), ...kept].sort((a, b) => order(a) - order(b))
  // 从 store 上取回来再写：存进去的是响应式代理，改代理才驱动界面（改手边那批
  // 原始对象只是改了数据，波形与"生成中"都不会更新）。
  const running = store.synthJobs.filter((job) => !job.stale)
  try {
    await Promise.all(
      running.map(async (job) => {
        try {
          const result = await api.synthesize({ ...requestBody(text, voiceId, values, job.key), persist: 'cache' })
          job.state = 'ok'
          job.audio_url = result.audio_url
          job.cost_note = result.cost_note
          job.detail = `${result.format} · ${(result.bytes / 1024).toFixed(0)} KB`
          // 存到哪要摆在界面上：按量计费的那几家生成一次就花一次钱。
          job.saved_path = result.saved_path || ''
          job.saved_rel = result.saved_rel || ''
          job.notes = result.notes || []
        } catch (error) {
          // 原样显示那台引擎报的话：它是唯一知道"缺什么"的一方。
          // 扇出给多台时 toast 会连着来，带上引擎名才分得清是谁失败的。
          job.state = 'failed'
          job.detail = error instanceof Error ? error.message : String(error)
          say(`${job.label}：${job.detail}`, { kind: 'error' })
        }
      }),
    )
    // 生成结果进了那台引擎的缓存目录，不是那条音色的样例——所以不刷新音色列表，
    // 刷了也什么都不会变。样例只有一个入口：详情里那颗「生成样例」。
    await loadTree()
  } finally {
    store.synthRunning = false
  }
}

/**
 * 只重跑这一台：失败的行不必等整批重跑（按量计费的引擎重跑整批就是重复花钱）。
 * 用的是当前正文 / 音色 / 参数——与块头「生成」看的是同一份输入。
 *
 * @param {any} job
 */
export async function retrySynthJob(job) {
  if (!job?.key || job.state === 'running') return
  if (store.synthRunning) {
    say('正在生成，等这一轮结束再重跑', { kind: 'warn' })
    return
  }
  const { text, voiceId, values } = store.synth
  if (!text.trim() || (!voiceId && !store.synth.recording)) {
    say('缺正文、音色或录音，先补齐再重跑', { kind: 'error' })
    return
  }
  const at = store.synthJobs.findIndex((row) => row.key === job.key)
  if (at < 0) return
  store.synthJobs.splice(at, 1, newJob(job.key))
  // 从 store 上取回来再写：splice 进去的是原始对象，改代理才驱动界面。
  const target = store.synthJobs[at]
  try {
    const result = await api.synthesize({
      ...requestBody(text, voiceId, values, job.key),
      persist: 'cache',
    })
    target.state = 'ok'
    target.audio_url = result.audio_url
    target.cost_note = result.cost_note
    target.detail = `${result.format} · ${(result.bytes / 1024).toFixed(0)} KB`
    target.saved_path = result.saved_path || ''
    target.saved_rel = result.saved_rel || ''
    target.notes = result.notes || []
  } catch (error) {
    target.state = 'failed'
    target.detail = error instanceof Error ? error.message : String(error)
    say(`${target.label}：${target.detail}`, { kind: 'error' })
  }
}

/**
 * 把这一条从这一页的结果里撤下：文件不删——生成音频归缓存目录管，
 * 这里只是把它移出这一批（「全部播放」与行内波形都不再认它）。
 * 播放条里正放着它时一并摘掉：队列是开播时的快照，不摘就还会放出来。
 *
 * @param {any} job
 */
export function dismissSynthJob(job) {
  if (!job?.audio_url) return
  dropTrack(job.audio_url)
  store.synthJobs = store.synthJobs.filter(
    (row) => !(row.key === job.key && row.audio_url === job.audio_url)
  )
  say(`已撤下「${job.label}」，文件仍在缓存`)
}
