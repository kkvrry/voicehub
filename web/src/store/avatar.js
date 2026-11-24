// @ts-check
// 匹配头像：用户先选来源，再串行跑当前列表里缺少头像的本地音色。
// 进度放在 store.avatarJob 里，由全局 toast 显示。停止只决定"不再开始下一张"：
// 当前这张会正常收尾，避免服务端已经写到一半、界面却不知道。
// 判据是 `has_cover`（目录里有没有图），不是"这张图谁写的"：手工 cover.* 也算已配。
import { api } from '../api'
import { store } from './state'
import { say } from './shell'
import { reloadVoice } from './library'

let stopped = false

/** @param {unknown} error */
function errorText(error) {
  return error instanceof Error ? error.message : String(error)
}

export function stopAvatarJob() {
  if (!store.avatarJob.running) return
  store.avatarJob.stopping = true
  stopped = true
}

/** 当前列表里缺头像的本地音色：隐藏按「显示已隐藏」口径，平台音色没有目录。 */
export function avatarTargets() {
  const list = store.showHidden ? store.voices : store.voices.filter(
    /** @param {any} voice */ (voice) => !voice.hidden
  )
  return list.filter(
    /** @param {any} voice */ (voice) => voice.is_local && !voice.has_cover
  )
}

/** 来源清单由后端给（具体站点），前端不写死；取到一次就留着，选择器与弹层共用。 */
export async function ensureAvatarSources() {
  if (store.avatarSources.length) return
  const status = await api.avatarStatus()
  if (status.ready === false) throw new Error(status.note || '头像来源还没就绪。')
  store.avatarSources = status.sources || []
}

/** @param {string} source */
function sourceLabel(source) {
  return store.avatarSources.find((item) => item.key === source)?.label || ''
}

/** 顶栏命令入口：先让用户选来源，再按当前列表补头像。 */
export async function matchAvatars() {
  if (store.avatarJob.running) return
  const targets = avatarTargets()
  if (!targets.length) {
    say('当前列表里的音色都已有头像。')
    return
  }
  try {
    await ensureAvatarSources()
  } catch (error) {
    say(errorText(error), { kind: 'error' })
    return
  }
  store.avatarMatch.total = targets.length
  store.avatarMatch.source = ''
  store.avatarMatch.open = true
}

export function closeAvatarMatch() {
  store.avatarMatch.open = false
}

/** 弹层里的「开始匹配」：来源必选，不替用户挑。 */
export async function startAvatarMatch() {
  const source = store.avatarMatch.source
  if (!source) {
    say('先选择一个头像来源。', { kind: 'warn' })
    return
  }
  const targets = avatarTargets()
  store.avatarMatch.open = false
  await generateAvatars(targets, source)
}

/** @param {any[]} voices @param {string} source */
export async function generateAvatars(voices, source) {
  if (store.avatarJob.running) return
  if (!source) {
    say('先选择一个头像来源。', { kind: 'warn' })
    return
  }
  const targets = (voices || []).filter(
    /** @param {any} voice */
    (voice) => voice.is_local && !voice.has_cover
  )
  if (!targets.length) {
    say('当前列表里的音色都已有头像。')
    return
  }

  try {
    await ensureAvatarSources()
  } catch (error) {
    say(errorText(error), { kind: 'error' })
    return
  }

  Object.assign(store.avatarJob, {
    running: true,
    done: 0,
    total: targets.length,
    failed: 0,
    name: '',
    stopping: false,
  })
  stopped = false
  let done = 0
  let failed = 0
  let skipped = 0

  for (const voice of targets) {
    if (stopped) break
    store.avatarJob.name = voice.shown_name || voice.name || ''
    try {
      const result = await api.generateAvatar(voice.id, source)
      // 服务端发现目录里已经有图就不落盘：这是跳过，不是失败，也不算这一张做完了。
      if (result.skipped) {
        skipped += 1
      } else {
        done += 1
        await reloadVoice(voice.id)
      }
    } catch {
      failed += 1
    }
    store.avatarJob.done = done
    store.avatarJob.failed = failed
  }

  const wasStopped = stopped
  stopped = false
  store.avatarJob.running = false
  store.avatarJob.stopping = false
  store.avatarJob.name = ''
  const label = sourceLabel(source)
  const where = label ? `（${label}）` : ''
  const already = skipped ? `，已有图跳过 ${skipped} 张` : ''
  if (wasStopped) {
    say(`已停止：完成 ${done} 张${failed ? `，失败 ${failed} 张` : ''}${already}。`, { kind: 'warn' })
  } else if (failed) {
    say(`匹配结束：成功 ${done} 张，失败 ${failed} 张${where}${already}。`, { kind: 'warn' })
  } else {
    say(`匹配完成：${done} 张${where}${already}。`, { kind: 'success' })
  }
}
