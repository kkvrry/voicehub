// @ts-check
// 「拾取」：`rail_mode = pick` 的生产方工作区用它记"处理谁"。
//
// 只有一份状态（`store.pick`）：左栏卡片点亮、工作区顶上的音频条、详情里的「使用」
// 三处读的都是它，谁也不留副本。宿主不认识是哪个插件在用——工作区自己决定拿它做什么。
import { store } from './state'

/**
 * 点卡片 = 选中这条音色的全部样例。
 *
 * 已经停在整条时不动；停在它的一条样例上时，再点卡片回到整条——卡片表达的是
 * "整条音色"，只处理一条要走「选样例」里的「使用」。再点一下不会取消选择。
 *
 * @param {any} voice
 */
export function pickVoice(voice) {
  const id = String(voice?.id || '')
  if (!id) return
  if (store.pick.voiceId === id && !store.pick.file) return
  store.pick = { voiceId: id, file: '' }
}

/**
 * 详情里的「使用」：这一条样例就是处理目标（连音色一起落到它那条上）。
 *
 * @param {any} voice
 * @param {any} sample
 */
export function pickSample(voice, sample) {
  const id = String(voice?.id || store.pick.voiceId || '')
  if (!id) return
  store.pick = { voiceId: id, file: String(sample?.file || '') }
}

/** 清掉目标：工作区回到空态。 */
export function clearPick() {
  store.pick = { voiceId: '', file: '' }
}
