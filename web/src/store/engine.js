// @ts-check
// 引擎这一域：已启用引擎的自描述、适配清单与启停、面板路由、配置的存/恢复默认、
// 音色通道、音色保存目录、拉平台音色清单、音色克隆。
// 前端不出现任何平台名：`key` 是插件给的，中文说法只有后端那一份。
import { api } from '../api'
import { store } from './state'
import { say } from './shell'
import { loadTree, refresh, rescan } from './library'

export async function loadEngines() {
  store.engines = await api.engines()
}

/**
 * 启用集合变了之后要一起刷新的那几份：语气词「通用」组是已启用引擎之间的交集，
 * 换一批引擎就换一份。「引导指令」预设与此无关——它按全部插件合并，启停不改候选。
 */
function reloadAfterToggle() {
  return Promise.allSettled([
    loadEngines(),
    loadEngineCatalog(),
    loadMarkGroups(),
    loadTree(),
  ])
}

/** 语气词标签的分组（通用 / 各台引擎自己那一组）——后端跨引擎算好的。
 *  与勾了哪台引擎无关：那一排一直摆着，所以是独立的一份状态。 */
export async function loadMarkGroups() {
  store.markGroups = await api.marks()
}

/** 适配清单：发现到的全部引擎（含未启用）。「引擎清单」那一层用它。 */
export async function loadEngineCatalog() {
  store.engineCatalog = await api.engineCatalog()
}

/** 打开「引擎清单」。清单现取一次：新装的插件、别处改过的启用状态都在里面。 */
export function openEngineCatalog() {
  store.catalogOpen = true
  void loadEngineCatalog().catch((error) => say(String(error?.message || error)))
}

export function closeEngineCatalog() {
  store.catalogOpen = false
}

/**
 * 启用一台：它就出现在左栏、生成页与生产方的候选里。
 *
 * @param {string} key
 */
export async function enableEngine(key) {
  const row = await api.setEngineEnabled(key, true)
  await reloadAfterToggle()
  say(`已启用「${row.label}」`)
  return row
}

/**
 * 停用一台：配置与库内音色都留着，只是不再出现。
 *
 * @param {string} key
 */
export async function disableEngine(key) {
  const row = await api.setEngineEnabled(key, false)
  // 勾选里也不该留着它：那一列数字是"这次要发给几台"。
  store.synth.engines = store.synth.engines.filter((item) => item !== key)
  await reloadAfterToggle()
  // 正在看的就是它：换到下一台已启用的；一台不剩就清掉，面板画空态。
  // 落点只写在这一处——面板里的开关与清单里的开关都走这条。
  if (store.pluginKind === 'tts' && store.engineKey === key) {
    const next = store.engines[0]?.key || ''
    store.engineKey = next
    if (next) await openEngine(next)
  }
  say(`已停用「${row.label}」，它的音色仍在库里`)
  return row
}

/**
 * 打开一台 TTS 引擎的面板（「引擎」页的右栏，不是弹窗）。
 * 自描述现取一次——页签、通道、保存目录都在那一包里，可能已被别处改过。
 * 取不到不清空页面（allSettled），面板照旧显示上一份。
 *
 * @param {string} key
 */
export async function openEngine(key) {
  store.view = 'engine'
  store.pluginKind = 'tts'
  store.engineKey = key
  store.engineSection = 'settings'
  await Promise.allSettled([loadEngines(), loadTree()])
}

/** @param {string} key */
export function engineOf(key) {
  return store.engines.find((e) => e.key === key) || null
}

/**
 * 图标：已启用与未启用都要画得出来（左栏「已停用」段、保存目录那一行都要用）。
 *
 * @param {string} key
 */
export function engineIconOf(key) {
  const row =
    store.engines.find((e) => e.key === key) || store.engineCatalog.find((e) => e.key === key)
  return row?.icon_url || ''
}

/**
 * 各引擎声明的取值候选（``value_choices``）→ 按参数键分的表。
 * 生成页是一个音色扇出给好几台，合并 = "表里出现过的东西至少有一台认"，
 * 不会出现"选了也发不出去"的候选。去重与顺序（先出现的在前）在这一层定。
 *
 * @param {any[]} engines
 * @returns {Record<string, any[]>}
 */
export function choicesOf(engines) {
  /** @type {Record<string, any[]>} */
  const merged = {}
  for (const engine of engines || []) {
    for (const [key, values] of Object.entries(engine?.value_choices || {})) {
      const list = (merged[key] ||= [])
      for (const value of values) if (!list.includes(value)) list.push(value)
    }
  }
  return merged
}

/**
 * 勾选里完全不认语气词的引擎：标签发给它们时会被删，面板上只提一句，
 * 不逐颗变灰——否则整排标签一起变淡，反而说不清是谁不认。
 *
 * @param {any[]} engines
 */
export function unsupportedMarkEngines(engines) {
  return (engines || []).filter(
    /** @param {any} engine */
    (engine) => !engine.marks_passthrough && !(engine.marks || []).length
  )
}

/**
 * 勾选里没有声明某一项能力的引擎（如 ``instruction_control``）。
 * 判据只认插件下发的 ``capabilities``，前端不认识引擎名。
 *
 * @param {any[]} engines
 * @param {string} key
 */
export function unsupportedCapabilityEngines(engines, key) {
  return (engines || []).filter(
    /** @param {any} engine */
    (engine) => !engine?.capabilities?.[key]
  )
}

/**
 * 情绪不支持的引擎：**既没有情绪参数，也没有引导指令**才算。
 * 情绪是引导指令的下位表达——支持引导指令的引擎可以用一句话说出情绪，
 * 不该按「没有 emotion 枚举」判成不支持。
 *
 * @param {any[]} engines
 */
export function unsupportedEmotionEngines(engines) {
  return (engines || []).filter(
    /** @param {any} engine */
    (engine) =>
      !engine?.capabilities?.emotion_control && !engine?.capabilities?.instruction_control
  )
}

/**
 * 勾选里不支持所选语言的引擎。判据只认插件下发的
 * ``languages.supported_codes``（方言说法已在后端归一成代码）。
 * 后端没给这一份时不画：宁可不提示，也不替引擎猜它认不认。
 * 不支持就是「不支持」；``languages.hint`` 只解释已支持方言的实际口音来源，
 * 不拿来给不支持的语言开脱。
 *
 * @param {any[]} engines
 * @param {string} code
 */
export function unsupportedLanguageEngines(engines, code) {
  const wanted = String(code || '').trim()
  if (!wanted) return []
  return (engines || []).filter((engine) => {
    const supported = engine?.languages?.supported_codes
    return Array.isArray(supported) && !supported.includes(wanted)
  })
}

/**
 * 所选语言**这台引擎支持**、但它是这台引擎登记过的方言，且引擎给了说明：
 * 返回 ``{label, hint}``。典型是 OmniVoice 的粤语——语言那一格认 ``yue``，
 * 但口音仍由参考音频决定。普通话与非方言不提示。
 *
 * @param {any[]} engines
 * @param {string} code
 */
export function languageHintEngines(engines, code) {
  const wanted = String(code || '').trim()
  if (!wanted) return []
  return (engines || [])
    .filter((engine) => {
      const supported = engine?.languages?.supported_codes
      const dialects = engine?.languages?.dialect_codes
      return (
        Array.isArray(supported) &&
        supported.includes(wanted) &&
        Array.isArray(dialects) &&
        dialects.includes(wanted) &&
        Boolean(String(engine?.languages?.hint || '').trim())
      )
    })
    .map((engine) => ({
      label: engine.label || engine.key || '',
      hint: String(engine.languages.hint).trim(),
    }))
}

/**
 * 正文里那排语气词标签要画的几段——后端（``GET /api/v1/marks``）已算好跨引擎结果：
 * 「通用」= 所有支持语气词的引擎都认的交集（一台都不认的不计入，否则交集恒为空）；
 * 各引擎组 = 它认但不在交集里的那几条。
 * 画哪几段跟着本次勾选走：勾了才算这次要发，没勾的组画出来只会让人点了才发现
 * 标签会被删。「通用」也按勾选重算——只勾一台时，它的专属标签本来就该算"都认"。
 * 这里再补「这次谁会收不到它」：``missing`` 只数同样支持语气词的勾选引擎
 * （完全不支持的那几台由面板上一句话交代），组名与提示照后端给的画。
 * 声明了 ``marks_passthrough`` 的引擎（认任意标签）一律算认，且末位挂一格自定义输入。
 * 前端不认识任何标签名，一律照后端给的画；加一台新引擎不用改这里。
 *
 * @param {any[]} engines
 */
export function markGroupsOf(engines) {
  const picked = engines || []
  const data = store.markGroups || {}
  const silent = new Set(unsupportedMarkEngines(picked).map((engine) => engine.key))
  const supporting = picked.filter((engine) => !silent.has(engine.key))
  /**
   * @param {any} engine
   * @param {string} name
   */
  const supports = (engine, name) =>
    Boolean(engine?.marks_passthrough) ||
    (engine?.marks || []).some(
      /** @param {any} mark */
      (mark) => mark.name === name
    )

  // 通用按本次勾选重算；勾选里全是"认任意标签"的引擎时不作约束，沿用后端给的全局交集。
  const constrained = supporting.filter((engine) => !engine.marks_passthrough)
  let common = (data.common || []).map((mark) => ({ ...mark, missing: [] }))
  let note = commonNote(data.participants)
  if (constrained.length) {
    const labels = new Map()
    for (const mark of data.common || []) labels.set(mark.name, mark)
    for (const engine of constrained) {
      for (const mark of engine.marks || []) if (!labels.has(mark.name)) labels.set(mark.name, mark)
    }
    const names = (constrained[0].marks || [])
      .map(
        /** @param {any} mark */
        (mark) => mark.name
      )
      .filter(
        /** @param {string} name */
        (name) =>
          constrained.every(
            /** @param {any} engine */
            (engine) => supports(engine, name)
          )
      )
    common = names.map(
      /** @param {string} name */
      (name) => ({ ...labels.get(name), name, missing: [] })
    )
    note = `本轮勾选的引擎都认（${supporting.map((engine) => engine.label).join('、')}）`
  }

  /** @type {any[]} */
  const groups = [{ key: '', label: '通用', note, marks: common }]

  for (const group of data.engines || []) {
    if (!picked.some((engine) => engine.key === group.key)) continue
    const marks = (group.marks || [])
      .filter(
        /** @param {any} mark */
        (mark) =>
          !common.some(
            /** @param {any} row */
            (row) => row.name === mark.name
          )
      )
      .map(
        /** @param {any} mark */
        (mark) => ({
          ...mark,
          missing: supporting
            .filter(
              /** @param {any} engine */
              (engine) => engine.key !== group.key && !supports(engine, mark.name)
            )
            .map(
              /** @param {any} engine */
              (engine) => engine.label
            ),
        })
      )
    if (!marks.length && !group.any) continue
    groups.push({
      key: group.key,
      label: group.label,
      any: Boolean(group.any),
      note: group.any
        ? `${group.label} 认任意标签，名字自定`
        : `${group.label} 自己的标签——别的引擎不一定认`,
      marks,
    })
  }

  // 勾了引擎但一台都不认语气词：不画标签，只留面板上那句"会被删"的提示。
  if (picked.length && !supporting.length) return []
  return groups.filter((group) => group.marks.length || group.any)
}

/**
 * 「通用」那一段的说明——**要说出是对谁求的交集**，否则那两个字没法核对。
 *
 * @param {any[]} participants
 */
function commonNote(participants) {
  const names = (participants || []).map((p) => p.label).filter(Boolean)
  if (!names.length) return ''
  return `凡是支持语气词的引擎都认（现在按 ${names.join('、')} 求的交集）`
}

/**
 * @param {string} key
 * @param {any} values
 */
export async function saveEngineSettings(key, values) {
  const saved = await api.saveEngineSettings(key, values)
  // 左栏那条要跟着变（填上密钥后「!」该消失），不能等下一次全量刷新。
  // 保存会顺手把这台加进已启用（后端定的），语气词交集要跟着重算。
  // allSettled：保存已成功，顺带刷新抽风不该把"保存成功"一起吞掉。
  await Promise.allSettled([loadEngines(), loadMarkGroups(), loadTree()])
  say(saved.ready ? `「${saved.label}」已保存` : `已保存——${saved.note}`)
  return saved
}

/**
 * 恢复默认：非机密字段回到插件默认值，机密保留（要抹掉另有「清除」）。
 *
 * @param {string} key
 */
export async function resetEngineSettings(key) {
  const saved = await api.saveEngineSettings(key, { _reset: true })
  await Promise.allSettled([loadEngines(), loadMarkGroups(), loadTree()])
  say(`「${saved.label}」已恢复默认（密钥保留）`)
  return saved
}

/**
 * 改这台引擎的音色通道。
 *
 * @param {string} key
 * @param {any} patch
 */
export async function saveEngineChannel(key, patch) {
  const saved = await api.saveEngineChannel(key, patch)
  await Promise.allSettled([loadEngines(), loadTree()])
  say('通道已保存')
  return saved
}

/**
 * 一台引擎的音色保存目录。不 say：输入框回显的就是生效值。
 * 保存前先问一次 preview：旧目录里有东西的话返回 `needed: true`，调用方据此问
 * 用户"要不要一起搬"，`migrate` 就是用户的回答。
 *
 * @param {string} key
 * @param {string} dir
 * @param {boolean} [migrate]
 */
export async function saveEngineOutput(key, dir, migrate = false) {
  const saved = await api.saveEngineOutput(key, dir, migrate)
  await loadEngines()
  // 搬过东西或换了目录都重扫：库里存的是扫描那一刻的绝对路径，不重扫旧路径就成了
  // 指向不存在地点的记录。allSettled：保存已成功，刷新抽风不该把它一起吞掉。
  if (migrate || saved?.moved) {
    await rescan().catch(() => {})
  }
  return saved
}

/**
 * 保存前照一眼：这次改目录要不要搬东西（不碰任何文件）。
 *
 * @param {string} key
 * @param {string} dir
 */
export function previewEngineOutput(key, dir) {
  return api.previewEngineOutput(key, dir)
}

/**
 * 拉平台音色清单。**"没拉到"不是异常**：后端返回 ok:false + 理由，照原话说。
 *
 * @param {string} key
 */
export async function refreshEngine(key) {
  const result = await api.refreshEngine(key)
  await refresh()
  if (!result.ok) {
    say(result.reason)
    return result
  }
  const extra = result.cost_note ? `（${result.cost_note}）` : ''
  say(`已同步 ${result.total} 个音色：新增 ${result.added}、更新 ${result.updated}${extra}`)
  return result
}

/**
 * @param {string} key
 * @param {any} payload
 */
export async function cloneVoice(key, payload) {
  const meta = await api.cloneVoice(key, payload)
  await refresh()
  say(
    payload.design_prompt
      ? `已创建「${meta.name}」`
      : `已克隆「${meta.name}」，已加入音色库`
  )
  return meta
}

/**
 * 让对话模型写一句设计描述：不建音色，只回那句话填进输入框。
 *
 * @param {string} name
 * @param {string} [brief]
 */
export async function suggestDesignPrompt(name, brief = '') {
  const result = await api.designPrompt({ name, brief })
  return result.text || ''
}
