// @ts-check
// 语音识别这一域：用哪台识别引擎、它配什么、以及把一段录音听成文字。
// 识别是一类插件（方向与合成相反），发现与配置跟合成引擎共用一套；
// 这里只管"选哪台 / 存它的设置 / 听一段"。没有安装与下载——模型从哪来是插件的事。
// 听是一次请求，只落在 `store.listen`；写回样例是人的动作（结果常要改两个字再存）。
import { api } from '../api'
import { labelOf, store, vocab } from './state'
import { say } from './shell'
import { reloadVoice } from './library'
import { loadSynthSamples } from './synth'

/** 识别引擎表。**不探网络**（就绪与否只看配置），所以跟着每次全量刷新一起取。 */
export async function loadAsr() {
  store.asr = await api.asr()
  // 顺手看一眼补逐字稿那一轮（应用启动 / 刷新列表时都走这里）。
  void syncAsrFill()
}

/** 打开一台识别引擎的面板（「引擎」页右栏的「识别」那一组，与 TTS 并列）。
 *  自描述现取一次——掩码后的当前值、就绪说明都在那一包里。 */
/** @param {string} key */
export async function openAsrEngine(key) {
  store.view = 'engine'
  store.pluginKind = 'asr'
  store.engineKey = key
  await loadAsr()
}

/** 右栏那台：按 `engineKey` 从识别清单里找（与 engine.js 的 `engineOf` 对称）。 */
/** @param {string} key */
export function asrEngineOf(key) {
  return store.asr.engines.find((item) => item.key === key) || null
}

/** 选一台。空串 = 自动，按引擎顺序用当前开着的服务。 */
/** @param {string} key */
export async function setAsrCurrent(key) {
  store.asr = await api.setAsrCurrent(key)
  void syncAsrFill()
}

/** 保存一台的设置，返回它保存后的那一行（掩码 / 恢复默认由后端处理）。 */
/**
 * @param {string} key
 * @param {any} patch
 */
export async function saveAsrSettings(key, patch) {
  store.asr = await api.saveAsrSettings(key, patch)
  // 保存配置 = 连上了：后端可能刚起一轮补逐字稿，toast 得跟上来。
  void syncAsrFill()
  return store.asr.engines.find((item) => item.key === key) || null
}

/** 测一下这台：能不能连上、它有哪些模型。**结果不写进 store**（它是一次性探测）。 */
/** @param {string} key */
export async function probeAsr(key) {
  return api.probeAsr(key)
}

/** 把库里某条样例听成文字。只填结果，不写回任何地方；
 *  识别不出来时不抛（多半是"这段里没有人声"，是正常结果不是故障）。 */
/**
 * @param {string} voiceId
 * @param {string} file
 */
export async function listen(voiceId, file) {
  const job = store.listen
  job.running = true
  job.error = ''
  job.language = ''
  job.languageCode = ''
  job.languageWritten = false
  // 识别是一次后台推理（首次还要加载模型），开始时说一声，结束时再说一声。
  say('识别中…')
  try {
    const result = await api.transcribe(voiceId, file)
    job.text = result.text || ''
    job.model = result.model || ''
    job.file = file
    job.language = result.language || ''
    job.languageCode = result.language_code || ''
    job.languageWritten = Boolean(result.language_written)
    if (!job.text) {
      job.error = '未识别出文字——这段音频里可能没有人声。'
      say('未识别出文字', { level: 'warning' })
    } else if (job.languageWritten) {
      // 语种按主样例写进了音色：让详情/列表里的那一格跟上，再用一句提示确认。
      await reloadVoice(voiceId).catch(() => {})
      const label = labelOf(vocab.languages, job.languageCode) || job.language
      say(`识别完成 · 语种已写入音色：${label || job.languageCode}`, { kind: 'success' })
    } else {
      say('识别完成')
    }
    return job.text
  } catch (error) {
    job.text = ''
    job.error = error instanceof Error ? error.message : String(error)
    say('识别失败', { level: 'error' })
    return ''
  } finally {
    job.running = false
  }
}

/** 补听一段已落盘的临时录音（自动转写关闭时的手动入口）。
 *  与 `listen` 共用结果格；`file` 记录音名，参考音频那一块据此回填。 */
/** @param {string} name */
export async function listenRecording(name) {
  const job = store.listen
  job.running = true
  job.error = ''
  job.language = ''
  job.languageCode = ''
  job.languageWritten = false
  say('识别中…')
  try {
    const result = await api.transcribeRecording(name)
    job.text = result.text || ''
    job.model = result.model || ''
    job.file = name
    job.language = result.language || ''
    job.languageCode = result.language_code || ''
    if (!job.text) {
      job.error = '未识别出文字——这段录音里可能没有人声。'
      say('未识别出文字', { level: 'warning' })
    } else {
      say('识别完成')
    }
    return job.text
  } catch (error) {
    job.text = ''
    job.error = error instanceof Error ? error.message : String(error)
    say('识别失败', { level: 'error' })
    return ''
  } finally {
    job.running = false
  }
}

/** 把识别结果写进那条样例的「说话内容」。
 *  顺手做的一半：如果这一段正是这次要发出去的参考音频，把那格文本也一起填上。 */
/**
 * @param {string} voiceId
 * @param {string} file
 * @param {string} text
 */
export async function saveHeard(voiceId, file, text) {
  const heard = String(text || '').trim()
  if (!heard) return
  await api.updateSample(voiceId, file, { text: heard })
  await loadSynthSamples()
  const sample = store.synth.samples.find((item) => item.file === file)
  if (sample?.path && store.synth.values.reference_audio === sample.path) {
    store.synth.values.reference_text = heard
  }
  say(`已写入「${file}」的说话内容`)
}

// ---- 连接时补逐字稿：进度与停止 ----
//
// 这一轮由后端起线程（启动 / 保存引擎设置 / 测试连接成功时），前端没有"发起来"的
// 动作，只能轮询：还在这一轮里就每 2 秒拉一次，结束即停表。toast 在 App.vue 里；
// 停止的语义与「一键配头像」一致——当前这条收尾，不再开始下一条。
//
// `running` 是"真的在听"，`starting` 是"起了线程、还没确定有没有活"（启动后要等
// 扫描写完库、还要等服务连得上、库里也真的缺文本）。只按 `running` 显示 toast：
// 两者都不成立时才是这一轮结束。

let fillTimer = 0

function scheduleFillPoll() {
  if (fillTimer) return
  fillTimer = window.setTimeout(() => {
    fillTimer = 0
    void syncAsrFill()
  }, 2000)
}

/** 拉一次进度；还在跑就继续排下一次。查询失败不打扰——下次刷新会再拉。 */
export async function syncAsrFill() {
  let fill
  try {
    fill = await api.asrFill()
  } catch {
    return
  }
  const job = store.asrJob
  const wasRunning = job.running
  const wasStopping = job.stopping
  job.running = Boolean(fill.running)
  job.done = Number(fill.done || 0)
  job.total = Number(fill.total || 0)
  job.failed = Number(fill.failed || 0)
  if (job.running || fill.starting) {
    scheduleFillPoll()
    return
  }
  job.stopping = false
  if (!wasRunning) return
  if (wasStopping) {
    say(`已停止补逐字稿：补上 ${job.done} 条。`, { kind: 'warn' })
  } else {
    say(`逐字稿补完了：${job.done} 条${job.failed ? `，失败 ${job.failed} 条` : ''}。`, {
      kind: job.failed ? 'warn' : 'success',
    })
  }
}

/** 停止：发一次信号，toast 留在原地等当前这条收尾（下一次轮询会收起它）。 */
export async function stopAsrFill() {
  if (!store.asrJob.running) return
  store.asrJob.stopping = true
  try {
    await api.cancelAsrFill()
  } catch (error) {
    store.asrJob.stopping = false
    say(error instanceof Error ? error.message : String(error), { kind: 'error' })
    return
  }
  void syncAsrFill()
}
