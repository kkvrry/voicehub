// @ts-check
// 顶栏斜杠命令：在搜索框里输入 / 后按关键字匹配，回车执行。命令集中登记，
// 顶栏只负责输入、候选与执行，不认识具体业务。新增命令只在这里加一条。
import { actions } from '../store'

/** @typedef {{ id: string, label: string, hint: string, keywords: string[], run: () => void }} Command */

/** @type {Command[]} */
const COMMANDS = [
  {
    id: 'avatar',
    label: '匹配头像',
    hint: '为当前列表缺头像的音色补图',
    keywords: ['avatar', '头像', '配图', '匹配'],
    run: () => actions.matchAvatars(),
  },
]

/**
 * @param {string} text / 后面的输入；空串返回全部。
 * @returns {Command[]}
 */
export function searchCommands(text) {
  const q = text.trim().toLowerCase()
  if (!q) return COMMANDS
  return COMMANDS.filter((cmd) =>
    [cmd.id, cmd.label, cmd.hint, ...cmd.keywords].some((word) =>
      word.toLowerCase().includes(q)
    )
  )
}
