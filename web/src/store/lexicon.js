// @ts-check
// 用户词表：设置页「词表」那一栏的状态（情绪候选 + 引导指令预设）。
//
// 生效值在后端算（仓库默认 + 数据目录里的用户差别）：这里只存与转发。引导指令
// 的预设**不在前端按引擎合并**——曾有三处各合各的（生成页、试听、行参数弹框），
// 现在后端一次算清，各处读同一份 `instructions.effective`。
import { api } from '../api'
import { store } from './state'

/** 取过没有：空的一份也是有效配置（用户可以把两节都删空），不能用"空不空"判断。 */
let loaded = false

/** 用到时才取（不是每个页面都要词表）。 */
export async function ensureLexicon() {
  if (loaded) return
  await loadLexicon()
}

export async function loadLexicon() {
  store.lexicon = await api.vocab()
  loaded = true
}

/**
 * 整节保存后后端回整份新状态；下游各页进页面时重取，这里只换这一份。
 * @param {any} patch
 */
export async function saveLexicon(patch) {
  store.lexicon = await api.saveVocab(patch)
  loaded = true
}

/** 引导指令预设的生效值：生成页、试听、行参数弹框共用。 */
export function instructionPresets() {
  return store.lexicon.instructions?.effective || []
}
