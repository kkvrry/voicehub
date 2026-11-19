// @ts-check
// 音色库这一域：左栏那棵树、卡片列表、详情抽屉、样例、扫描根与生成结果目录，
// 以及"移除音色"这条会真的动磁盘的路。域间允许成环，见 ./index.js 顶部说明。
import { api } from '../api'
import { store } from './state'
import { persistFilters, say } from './shell'
import { loadEngines } from './engine'
import { loadAsr } from './asr'
import { loadSpec } from './synth'
import { ensureTexts } from './texts'
import {
  extendQueue,
  manualizeCurrent,
  playAll,
  playSolo,
  syncVoice,
  togglePlay,
} from './playback'

function currentQuery() {
  const q = { ...store.filters }
  if (store.selection.kind === 'local') {
    q.source = 'local'
    // 按根目录筛：引擎的保存目录也是根，不带上它会把用户自己的音色一起列出来。
    if (store.selection.root) q.root = store.selection.root
    if (store.selection.category) q.category = store.selection.category
  } else if (store.selection.kind === 'platform') {
    q.source = store.selection.name
  } else if (store.selection.kind === 'design') {
    // 设计音色是跨引擎的一组：不带 source，否则只能看到某一台建的那些。
    q.design = 1
  }
  return q
}

/**
 * 树里还找得到这个分类吗（分类可以嵌套，逐层找）。
 * @param {any} node
 * @param {string} name
 * @returns {boolean}
 */
function hasCategory(node, name) {
  return (node.children || []).some(
    /** @param {any} child */
    (child) => child.name === name || hasCategory(child, name)
  )
}

export async function loadTree() {
  store.tree = await api.tree()
  // 扫描/去重之后，正在看的分类可能已经不在树里了——停在空列表上会让人以为
  // "音色全没了"。回到「全部音色」，列表自然重来。
  const selection = store.selection
  if (selection.kind !== 'local') return
  const root = store.tree.roots.find((item) => item.path === selection.root)
  const gone = !root || (selection.category && !hasCategory(root, selection.category))
  if (gone) store.selection = { kind: 'all' }
}

/** 当前列表的顺序：先按「显示已隐藏」筛掉，再把有样例的排前面。
 *
 *  卡片列表与「全部播放」共用这一份——顺序若各算一份，播出来的就不是屏幕上
 *  看到的那个顺序了。排序稳定，同档内维持原顺序。
 */
export function visibleVoices() {
  const list = store.showHidden ? store.voices : store.voices.filter((voice) => !voice.hidden)
  return [...list].sort((a, b) => Number(b.sample_count > 0) - Number(a.sample_count > 0))
}

/** 设计音色：本体只是一段描述，**不属于任何一家引擎**——凡声明了音色设计能力的
 *  引擎都能拿它合成，具体用谁由用户在右边的引擎里选。所以卡片上不写来源引擎，
 *  只写一枚「设计」。
 *
 *  判据以后端给的 `is_design` 为准（它看的是 payload 里那句描述，与左栏分组、
 *  列表筛选同一口径）。标签与分类只是老后端的兜底：那两格用户能改，改完就误判。 */
/**
 * @param {any} voice
 * @returns {boolean}
 */
export function isDesignVoice(voice) {
  if (!voice) return false
  if (typeof voice.is_design === 'boolean') return voice.is_design
  return (voice.tags || []).includes('设计') || voice.category === '设计音色'
}

/**
 * 列表与标签云（标签云刻意不带 tag 条件，见后端 `/tags` 的说明）。
 * @param {any} query
 * @returns {Promise<{ voices: any[], cloud: any[] }>}
 */
async function fetchPair(query) {
  const [voices, cloud] = await Promise.all([api.voices(query), api.tags(query)])
  return { voices, cloud }
}

// 列表请求的序号：**只认最新那一次**。
// 点一下标签会连发两个请求（筛上 → 取消），网络慢时先发的可能后回；
// 没有这条守卫，旧结果会盖掉新结果——界面停在"已取消筛选、却只剩一条音色"，
// 而标签栏明明显示「全部」。这不是显示延迟，是先后顺序丢了。
let loadSeq = 0

export async function loadVoices() {
  const seq = ++loadSeq
  store.loading = true
  try {
    let query = currentQuery()
    let { voices, cloud } = await fetchPair(query)
    // 标签是跟着范围走的：在 MiMo 下选了「活泼」，换到某个本地分类后这枚标签
    // 在那里根本不存在——列表空着、标签栏里也没有它，看上去像"这里没有音色"。
    // 那不是没有音色，是筛选条件失效了；当场退回「全部」，再按这个范围重查一次。
    if (query.tag && !cloud.some((entry) => entry.tag === query.tag)) {
      store.filters.tag = ''
      query = currentQuery()
      ;({ voices, cloud } = await fetchPair(query))
    }
    // 这一轮已经被后来的请求取代：把结果丢掉，让它去写界面。
    if (seq !== loadSeq) return
    // 全量名单若正复用当前列表，换数组时一起换：否则扫描/筛选后，
    // 生成页与创作页拿着的还是旧对象，详情里的改动看不到。
    const allWasShared = store.allVoices === store.voices
    store.voices = voices
    if (allWasShared) store.allVoices = voices
    store.tagCloud = cloud
    store.loadError = ''
    // 筛选的每一次改动都从这里过，"记住筛选"也只在这里做。
    persistFilters()
  } catch (error) {
    if (seq !== loadSeq) return
    // 拉失败要说话：静默的话界面上只是"什么都没有"，看不出是请求挂了。
    store.voices = []
    store.tagCloud = []
    store.loadError = error instanceof Error ? error.message : String(error)
    say(`列表加载失败：${store.loadError}`)
  } finally {
    // 只有最新那一轮能结束 loading：旧请求回来时把转圈关掉，新请求还在路上。
    if (seq === loadSeq) store.loading = false
  }
}

/** 把服务端回来的那条音色按同一套规则写进某个对象：`null` 字段与数组/对象跳过，
 *  免得把详情里的 `samples`、`folder` 这类本地整理的字段抹掉。 */
/**
 * @param {any} target
 * @param {any} view
 */
function applyVoice(target, view) {
  for (const [key, value] of Object.entries(view || {})) {
    if (value == null || key === 'object' || key === 'samples' || key === 'folder') continue
    target[key] = value
  }
}

/** 服务端返回的一条音色 → **就地**补进列表与详情。
 *
 *  刻意不重取整份列表：卡片按 id 复用，只有被改的那一张需要重画。整表重取会让几百张
 *  卡的 props 全部换新（Vue 得逐张 patch），在「点一下隐藏」这种改一个字段的操作上
 *  完全不成比例。``null`` 的字段跳过——更新接口不带引擎归属，别把详情里那份抹掉。
 *
 * @param {any} view
 */
function mergeVoice(view) {
  const row = store.voices.find((item) => item.id === view.id)
  if (row) applyVoice(row, view)
  if (store.detail && store.detail.id === view.id) {
    applyVoice(store.detail, view)
    mergeSamples(store.detail, view.samples)
  }
}

/** 样例按 ``file`` 就地合并：不换数组、不换元素对象。
 *
 *  整段替换（``detail.samples = view.samples``）也能让界面变对，但 `SampleRow` 监听
 *  的是 `props.sample` 这个**引用**：一换，每行的 text / emotion / tags 本地 ref 都被
 *  服务端值重写一遍——正在别的行打字、还没失焦的那份草稿就没了。改完一个字段顺手
 *  抹掉别处的输入，比"没刷新"更糟。就地改属性只触发重画，不触发那个 watch。
 */
/**
 * @param {any} detail
 * @param {any} incoming
 */
function mergeSamples(detail, incoming) {
  if (!Array.isArray(incoming)) return
  const samples = (detail.samples ||= [])
  const keep = new Set(incoming.map((sample) => sample.file))
  for (let index = samples.length - 1; index >= 0; index -= 1) {
    if (!keep.has(samples[index].file)) samples.splice(index, 1)
  }
  for (const sample of incoming) {
    const current = samples.find(
      /** @param {any} item */
      (item) => item.file === sample.file
    )
    if (current) Object.assign(current, sample)
    else samples.push(sample)
  }
}

/** 只刷新标签云：改标签之后各标签的计数变了，列表与左栏树都不受影响。 */
async function loadTags() {
  try {
    store.tagCloud = await api.tags(currentQuery())
  } catch {
    /* 标签云是辅助信息，拉失败不该打断正在做的编辑 */
  }
}

/** 全量刷新：树 / 列表 / 引擎自描述 / 识别档位一起来，另加重取当前详情。
 *  它是各域共用的收尾动作（引擎、生成、目录、扫描都用它）。
 *  先树后列表：树会顺手把"已经不在树里的选中节点"退回全部，列表得按修好的选择查。 */
export async function refresh() {
  await loadTree()
  await Promise.all([loadVoices(), loadEngines(), loadAsr()])
  if (store.detail) {
    store.detail = await api.voice(store.detail.id)
  }
}

/** @param {any} node */
export async function select(node) {
  // 在「生成」页或「创作」页点树 = 用它筛那一列音色卡片（不离开这一页）；
  // 别的页照旧回音色库——那两页才有"树右侧那一列卡片"。
  if (!['library', 'synth', 'producer'].includes(store.view)) store.view = 'library'
  store.selection = node
  await loadVoices()
}

/** 回到音色库那一页（不带筛选）。 */
export async function openLibrary() {
  store.view = 'library'
  store.engineKey = ''
  await loadVoices()
}

/** @param {any} voice */
export async function openDetail(voice) {
  store.detail = await api.voice(voice.id)
  // 换音色 = 换一份试听，上一份的文本与结果属于上一个音色。
  store.audition = { text: '', values: { seed: -1 }, job: null, running: false }
  // 生成页才取规范；抽屉里这份用到时才取，列表页不多拉一次。
  // 文案库同理：抽屉里的「生成样例」要挑一句，第一次打开时才问后端要。
  if (!store.spec.fields.length) await loadSpec()
  await ensureTexts()
}

export function closeDetail() {
  store.detail = null
}

/** 全部试听：这个音色的每一段排成一条队列，从第一段开始。
 *  入口在详情抽屉的样例区标题行（卡片上不再摆这颗按钮）；样例清单在详情接口里，
 *  所以现取一次 `api.voice`——不打开抽屉。 */
/** @param {any} voice */
export async function playAllSamples(voice) {
  const full = await api.voice(voice.id)
  const name = full.shown_name || ''
  playAll(
    (full.samples || []).map(
      /** @param {any} sample */
      (sample) => ({
        url: sample.audio_url,
        // 播放条写音频名称，不写逐字稿：逐字稿属于这一段的编辑内容，
        // 底栏要回答的是"现在放的是哪一个文件"。
        label: sample.file,
        sub: `${name}${sample.is_main ? ' · 主样例' : ''}`,
        voice: full,
      })
    )
  )
}

/** 头像播放键：先出声，再把这条音色自己的样例补成队列。
 *  队列从主样例开始，只含这一条音色的样例；手动队列，放完当前这条不自动续播。 */
/** @param {any} voice */
export function playVoiceCard(voice) {
  const url = voice?.main_audio_url
  if (!url) return
  if (store.player.url === url) {
    // 已经在放这一条：手动队列就暂停/继续；自动队列则收成手动队列，不打断声音。
    if (store.player.manual) {
      togglePlay()
      return
    }
    manualizeCurrent()
    if (!store.player.playing) togglePlay()
  } else {
    playSolo({ url, label: voice.shown_name, sub: '主样例', voice })
  }
  if ((voice.sample_count || 0) > 1) void extendWithSamples(voice, url)
}

/**
 * @param {any} voice
 * @param {string} url
 */
async function extendWithSamples(voice, url) {
  let full = null
  try {
    full = await api.voice(voice.id)
  } catch {
    // 取不到就停在这条：声音已经成立，不为补齐队列把它掐掉。
    return
  }
  const samples = (full.samples || []).filter(
    /** @param {any} sample */
    (sample) => sample.audio_url
  )
  // 主样例排第一：点头像从 1 / N 开始，下一段总是有的可切。
  const ordered = [...samples].sort((a, b) => {
    const mainA = a.is_main || a.audio_url === url ? 1 : 0
    const mainB = b.is_main || b.audio_url === url ? 1 : 0
    return mainB - mainA
  })
  extendQueue(
    url,
    ordered.map(
      /** @param {any} sample */
      (sample) => ({
        url: sample.audio_url,
        // 身份始终是音色；样例文件放在 tooltip，位置由数量行回答。
        label: full.shown_name,
        sub: `${sample.file}${sample.is_main ? ' · 主样例' : ''}`,
        voice: full,
      })
    )
  )
}

/** 改的这一格，正好是当前筛选用的那一格吗？
 *
 *  是的话这条音色可能已经不该出现在列表里了（比如正按「中文」筛着，把它改成英文），
 *  那就得重查一次；不是的话只回填这一条就够——**这才是绝大多数情况**。
 *
 * @param {any} patch
 */
function breaksFilters(patch) {
  const filters = store.filters
  if (filters.language && 'language' in patch) return true
  if (filters.gender && 'gender' in patch) return true
  if (filters.age && 'age' in patch) return true
  if (filters.tag && 'tags' in patch) return true
  if (filters.q && ('display' in patch || 'description' in patch || 'tags' in patch)) return true
  return false
}

/**
 * 只重取并合并一条音色：识别写回语种后让详情与列表跟上，不刷新整棵树。
 * @param {string} voiceId
 */
export async function reloadVoice(voiceId) {
  if (!voiceId) return
  mergeVoice(await api.voice(voiceId))
}

/** 改元数据（一格一个请求）。**只回填这一条**：列表、左栏树都没变——
 *  分类/计数与语言、性别、描述无关，重取整棵树加整份列表只是白白让界面闪一次。 */
/** @param {any} patch */
export async function saveVoice(patch) {
  if (!store.detail) return
  await patchVoice(store.detail, patch)
}

/** 改**任意一条**（卡片上直接点标签走这条）。
 *
 *  与 `saveVoice` 只差"改的是谁"：那个改当前详情，这个改传进来的那条。
 *  两条路必须共用同一段逻辑，否则"卡片上改"和"详情里改"迟早各长一套行为。
 */
/**
 * @param {any} voice
 * @param {any} patch
 */
export async function patchVoice(voice, patch) {
  mergeVoice(await api.updateVoice(voice.id, patch))
  // 改的正好是正在筛的那一格：这条可能已经不符合条件，列表得重新算一次。
  // 重查列表会顺带把标签云一起取回来，所以按标签筛着的时候不必再单独刷一次云。
  if (breaksFilters(patch)) await loadVoices()
  else if ('tags' in patch) await loadTags()
  say('已保存')
}

/**
 * @param {string} file
 * @param {any} patch
 */
export async function saveSample(file, patch) {
  if (!store.detail) return
  // 同样只回填、不换对象：换对象会让详情里的表单 watch 重跑一遍（正在编辑的
  // 描述/显示名被服务端旧值盖回去，焦点还会被 dialog 抢走）。
  mergeVoice(await api.updateSample(store.detail.id, file, patch))
  say('已保存')
}

/** @param {string} file */
export async function setMain(file) {
  if (!store.detail) return
  mergeVoice(await api.setMainSample(store.detail.id, file))
  say(file ? `主样例已设为「${file}」` : '已取消指定，恢复自动选择')
}

/** 移除一条样例：文件进回收站（可还原）。
 *
 *  与「移除音色」同一条纪律——只摘记录不行，文件还在的话下次扫描又把它认回来。
 *  后端把改完的详情整条给回来，`mergeVoice` 顺手把卡片上那个「N 条样例」改掉，
 *  不必重取列表（几百张卡为删一段音频全换一遍 props，不成比例）。
 *
 * @param {string} file
 */
export async function removeSample(file) {
  if (!store.detail) return
  mergeVoice(await api.deleteSample(store.detail.id, file))
  say(`已移除「${file}」`)
}

/** 收藏 / 隐藏：一个字段的偏好，后端已经把改完的那条给回来了，直接补上。
 *  以前这里重取整份列表——点一下 🙈 等于让几百张卡换一遍 props。 */
/**
 * @param {any} voice
 * @param {any} prefs
 */
export async function setPrefs(voice, prefs) {
  const view = await api.setPrefs(voice.id, prefs)
  mergeVoice(view)
  // 播放条上的收藏可能来自一条已经不在当前列表里的音色（切页后列表换了对象）：
  // 那条只活在播放队列里，也要跟上，否则星标按了不亮。
  applyVoice(voice, view)
  syncVoice(voice.id, view)
  // 收藏/隐藏是一个动作，做完说一声（简短、可预期）。
  if ('favorite' in prefs) say(prefs.favorite ? '已收藏' : '已取消收藏')
  else if ('hidden' in prefs) say(prefs.hidden ? '已隐藏' : '已取消隐藏')
}

/** @param {string[]} folders */
export async function saveFolders(folders) {
  const summary = await api.saveFolders(folders)
  await refresh()
  say(`扫描完成：${summary.voices} 个音色、${summary.samples} 条样例`)
}

/**
 * 生成结果目录。不 say：输入框回显的就是生效值，弹出的"扫描完成"更有信息量。
 * @param {string} dir
 */
export async function saveOutputDir(dir) {
  const saved = await api.saveOutput(dir)
  return saved.dir
}

/**
 * 扫描收尾：重取数据，再按结果说一句。失败就把原因原样说出来（别装作扫完了）。
 * @param {any} job
 */
async function finishScan(job) {
  await refresh()
  if (job.state === 'failed') {
    say(job.error || '重扫失败', { kind: 'error' })
    return
  }
  const summary = job.result || {}
  // 说清这次真的做了什么：没动过的音色是整条复用的，不重读不重写。
  const reused = summary.unchanged || 0
  say(
    reused
      ? `重扫完成：${summary.voices} 个音色（${reused} 个未变，直接复用）`
      : `重扫完成：${summary.voices} 个音色`
  )
}

/** 一次重扫最多跟这么久：后端卡在 running 时不能让这个循环永远转下去。 */
const RESCAN_LIMIT_MS = 15 * 60 * 1000

let rescanning = false

/** 重扫。服务端在后台线程里扫，这里只负责"起一次 + 等它说完"：
 *  每 0.8 s 问一次进度，阶段变了就说一声——大库扫几十秒时，界面不是干等。 */
export async function rescan() {
  if (rescanning) return
  rescanning = true
  try {
    let started
    try {
      started = await api.scan()
    } catch (error) {
      say(error instanceof Error ? error.message : String(error), { kind: 'error' })
      return
    }
    if (started.state !== 'running') {
      await finishScan(started)
      return
    }
    const deadline = Date.now() + RESCAN_LIMIT_MS
    let lastPhase = ''
    for (;;) {
      if (Date.now() > deadline) {
        say('扫描还在后台进行，稍后刷新查看结果。', { kind: 'warn' })
        return
      }
      let job
      try {
        job = await api.scanStatus()
      } catch (error) {
        say(error instanceof Error ? error.message : String(error), { kind: 'error' })
        return
      }
      if (job.state !== 'running') {
        await finishScan(job)
        return
      }
      if (job.phase && job.phase !== lastPhase) {
        lastPhase = job.phase
        say(job.phase)
      }
      await new Promise((resolve) => setTimeout(resolve, 800))
    }
  } finally {
    rescanning = false
  }
}

/** 移除一条音色。本地音色会把磁盘上的文件移进回收站（可还原）——只删库里那行的话，
 *  下一次重扫它又回来了。二次确认在组件里做（那里才拿得到文件夹路径）。 */
/** @param {any} voice */
export async function removeVoice(voice) {
  const result = await api.deleteVoice(voice.id)
  if (store.detail && store.detail.id === voice.id) store.detail = null
  await refresh()
  const moved = (result.trashed || []).length
  say(moved ? `已移除「${voice.shown_name}」，${moved} 项已移入回收站` : `已移除「${voice.shown_name}」`)
}
