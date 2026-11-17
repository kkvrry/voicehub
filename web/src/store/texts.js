// @ts-check
// 文案库：生成样例时"挑一句现成的"那几组句子。
//
// 它**不在前端写死**（`presets.js` 就是这么来的，加一句要改代码重构建）；
// 在服务的数据目录里、设置页可改，所以这里只有一个缓存 + 三个动作。
import { api } from '../api'
import { store } from './state'

/** 取过没有：**空的一份也是有效配置**（用户可以删空），所以不能用"空不空"判断。 */
let loaded = false

/** 用到时才取（列表页不多拉一次）。 */
export async function ensureTexts() {
  if (loaded) return
  await loadTexts()
}

export async function loadTexts() {
  store.texts = await api.texts()
  loaded = true
}

/**
 * 整份替换（设置页那一张表改完一次发一次）。
 * @param {any[]} groups
 */
export async function saveTexts(groups) {
  store.texts = await api.saveTexts(groups)
  loaded = true
}

/** 恢复默认那一份（从此算你改过的）。 */
export async function resetTexts() {
  store.texts = await api.resetTexts()
  loaded = true
}

/** 第一组的第一句：给"需要有句话但用户没挑"的地方当缺省（克隆试听那种）。
 *  一句都没有时给空串——那里宁可空着，也不要凭空编一句话。 */
export function firstText() {
  const group = store.texts.find((item) => (item.lines || []).length)
  return group?.lines?.[0] || ''
}
