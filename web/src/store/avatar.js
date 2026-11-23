// @ts-check
// 一键配头像：串行跑当前列表里缺少头像的本地音色。
// 进度放在 store.avatarJob 里，由全局 toast 显示。停止只决定"不再开始下一张"：
// 当前这张会正常收尾，避免服务端已经写到一半、界面却不知道。
// 判据是 `has_cover`（目录里有没有图），不是"这张图谁写的"：手工 cover.* 也算已配。
import { api } from '../api'
import { store } from './state'
import { say } from './shell'
import { reloadVoice } from './library'

let stopped = false

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

/** 顶栏命令入口：先确认规模，再按当前列表补头像。 */
export async function matchAvatars() {
  if (store.avatarJob.running) return
  const targets = avatarTargets()
  if (!targets.length) {
    say('当前列表里的音色都已有头像。')
    return
  }
  if (targets.length > 1) {
    const minutes = Math.max(1, Math.round((targets.length * 3) / 60))
    const hint = `为当前列表缺少头像的 ${targets.length} 个音色查找头像？\n预计约 ${minutes} 分钟，可在提示里停止。`
    if (!window.confirm(hint)) return
  }
  await generateAvatars(targets)
}

/** @param {any[]} voices */
export async function generateAvatars(voices) {
  if (store.avatarJob.running) return
  const targets = (voices || []).filter(
    /** @param {any} voice */
    (voice) => voice.is_local && !voice.has_cover
  )
  if (!targets.length) {
    say('当前列表里的音色都已有头像。')
    return
  }

  try {
    const status = await api.avatarStatus()
    if (!status.ready) {
      say(status.note || '头像来源还没就绪。', { kind: 'error' })
      return
    }
  } catch (error) {
    say(error instanceof Error ? error.message : String(error), { kind: 'error' })
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
      const result = await api.generateAvatar(voice.id)
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
  const already = skipped ? `，已有图跳过 ${skipped} 张` : ''
  if (wasStopped) {
    say(`已停止：完成 ${done} 张${failed ? `，失败 ${failed} 张` : ''}${already}。`, { kind: 'warn' })
  } else if (failed) {
    say(`头像匹配结束：成功 ${done} 张，失败 ${failed} 张${already}。`, { kind: 'warn' })
  } else {
    say(`头像匹配完成：${done} 张${already}。`, { kind: 'success' })
  }
}
