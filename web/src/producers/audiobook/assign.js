// @ts-check
// 「指定音色」这一域的判据：谁可被指定、这条音色能落到哪台引擎、没选时落到哪台。
//
// 两处界面（创作页的角色栏 / 弹窗，以及任何页面都能打开的全局浮层）都从这里取，
// 各自只画自己的样子。判据分成两份，迟早一边允许的引擎另一边不认。
import { store } from '../../store'

/** 拆句与分析留下的占位说话人：它们不是书里的角色。 */
export const PLACEHOLDER_SPEAKERS = new Set(['对话', '未知'])

/**
 * 这本书识别出的具名角色：只剩占位说话人时，分配表按章列行。
 *
 * @param {any} detail
 */
export function namedSpeakersOf(detail) {
  return (detail?.speakers || []).filter(
    /** @param {any} item */ (item) => !PLACEHOLDER_SPEAKERS.has(item.speaker)
  )
}

/**
 * 角色表：旁白固定第一行，其余按出现次数。
 * **只列正文里的说话人**——`voices` 里的孤儿映射（行早没了 / 重拆后换了名字）
 * 不进来：它属于"这个项目配过音色"，不属于"这本书里有谁"。
 *
 * @param {any} detail
 */
export function rolesOf(detail) {
  const speakers = detail?.speakers || []
  const narrator = {
    name: '旁白',
    rows: Math.max(
      0,
      (detail?.stats?.rows || 0) -
        speakers.reduce(
          /** @param {number} sum @param {any} item */
          (sum, item) => sum + item.rows,
          0
        )
    ),
  }
  return [
    narrator,
    ...speakers.map(
      /** @param {any} item */
      (item) => ({ name: item.speaker, rows: item.rows })
    ),
  ]
}

/**
 * 这条音色能被哪些引擎用——**后端算好的那份**（`voice.engines`）。
 * 凭据规则（预置音色发 id、样例可跨平台复刻、设计音色只在自己的平台）只在
 * `app/domain/credential.py` 一处判定；界面再推一遍的话，选项会与合成分叉。
 * `（克隆）` 只是标签：不是它自己的平台，就意味着拿样例复刻。
 *
 * @param {any} voice
 */
export function enginesForVoice(voice) {
  if (!voice) return []
  const keys = Array.isArray(voice.engines) ? voice.engines : []
  return keys.map(
    /** @param {string} key */
    (key) => {
    const engine = (store.engines || []).find((item) => item.key === key) || {}
    const clone = !voice.is_local && voice.source !== key
      return { key, label: `${engine.label || key}${clone ? '（克隆）' : ''}` }
    }
  )
}

/**
 * 这条音色落到哪台引擎：用户选的优先，否则平台音色回自己的引擎、本地音色回原引擎。
 *
 * @param {string} speaker
 * @param {any} voice
 * @param {Record<string, any>} [assignments]
 */
export function engineForAssign(speaker, voice, assignments = {}) {
  if (!voice) return ''
  const allowed = enginesForVoice(voice)
  if (!allowed.length) return voice.source || ''
  const current = assignments[speaker]?.engine
  if (
    allowed.some(
      /** @param {{ key: string }} item */
      (item) => item.key === current
    )
  ) {
    return current
  }
  return allowed[0].key
}

/**
 * 行属不属于这个角色：旁白在下拉里是 `__narrator__`，正文里的空值也算旁白。
 * 行表筛选与章节栏标记共用这一句，避免两处对「旁白」的判据分叉。
 *
 * @param {any} row
 * @param {string} role
 */
export function matchesSpeaker(row, role) {
  if (!role) return true
  if (role === '__narrator__') return !row.speaker || row.speaker === '旁白'
  return row.speaker === role
}

/**
 * 不能选引擎时那一格写什么：平台音色回它的平台名，本地音色留空由调用方说明。
 * 「本地音色」是来源不是引擎，不能拿它填引擎栏。
 *
 * @param {any} voice
 */
export function engineLabelForVoice(voice) {
  if (!voice) return ''
  if (!voice.is_local) {
    return (
      voice.engine?.label ||
      (store.engines || []).find((engine) => engine.key === voice.source)?.label ||
      voice.source
    )
  }
  return voice.engine?.label || ''
}
