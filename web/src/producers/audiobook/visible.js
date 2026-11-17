// @ts-check
// 行表的一串筛选（状态档 → 角色）只在这里落成一条链路：
// 表格显示、逐行试听队列、章节栏的排除标记、滚轮换章都读同一份结果，
// 别在各自的地方把判据重写一遍——同一条规则错在两个地方，界面就会自相矛盾。
import { matchesSpeaker } from './assign'

/** 分析没能定人的行，后端把说话人写成「未知」。界面读作「未分配」。 */
export const UNASSIGNED_SPEAKER = '未知'

/**
 * 缺字判据由后端随行给出（`broken`）：章节栏的缺字灯用的是同一句。
 *
 * @param {any} row
 */
export function isBroken(row) {
  return Boolean(row.broken)
}

/**
 * @param {any} row
 * @param {string} filter
 */
export function matchesFilter(row, filter) {
  if (filter === 'failed') return row.status === 'failed'
  if (filter === 'broken') return isBroken(row)
  return true
}

/**
 * 行是否通过整条筛选链（状态档 + 角色）。
 *
 * @param {any} row
 * @param {{ filter?: string, speaker?: string }} [options]
 */
export function rowVisible(row, { filter = 'all', speaker = '' } = {}) {
  if (!matchesFilter(row, filter)) return false
  return speaker ? matchesSpeaker(row, speaker) : true
}

/**
 * @param {any[]} rows
 * @param {{ filter?: string, speaker?: string }} [options]
 */
export function filterRows(rows, options) {
  return rows.filter(
    /** @param {any} row */
    (row) => rowVisible(row, options)
  )
}

/**
 * 章节里还有没有能通过筛选的行。当前章用已加载的行实时判（改角色立刻跟着变），
 * 其余章用项目详情里按章聚合的状态计数与角色名单。
 *
 * @param {any} chapter
 * @param {{ filter?: string, speaker?: string, rows?: any[] | null, currentId?: string }} [options]
 */
export function chapterVisible(
  chapter,
  { filter = 'all', speaker = '', rows = null, currentId = '' } = {}
) {
  if (!Number(chapter.rows)) return false
  if (chapter.id === currentId && Array.isArray(rows) && rows.length) {
    return rows.some(
      /** @param {any} row */
      (row) => rowVisible(row, { filter, speaker })
    )
  }
  if (filter !== 'all' && !(Number(chapter[filter]) > 0)) return false
  if (!speaker || chapter.id === currentId) return true
  const names = chapter.speakers
  // 旧后端没有这份名单：不误标，等重启后再按名单判定。
  if (!names) return true
  return speaker === '__narrator__' ? names.includes('旁白') : names.includes(speaker)
}
