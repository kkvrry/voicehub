// @ts-check
// 「拾取」：`rail_mode = pick` 的生产方工作区用它记"处理谁"。
//
// 只有一份状态（`store.pick`）：左栏卡片点亮、工作区顶上的音频条、详情里的「使用」
// 三处读的都是它，谁也不留副本。宿主不认识是哪个插件在用——工作区自己决定拿它做什么。
import { store } from './state'

/**
 * 点卡片 = 把这条音色的样例摆成这一轮的处理序列（替换已有序列）。
 * 工作区比对序列内容，同一张卡再点一次不会洗掉裁剪。
 *
 * @param {any} voice
 */
export function pickVoice(voice) {
  const id = String(voice?.id || '')
  if (!id) return
  store.pick = {
    voiceId: id,
    file: '',
    mode: 'select',
    rev: (store.pick.rev || 0) + 1,
  }
}

/**
 * 卡右「添加」：把这张卡（整条音色的全部样例）加入处理序列。
 * 同一条音色再添加一次也要能把移出的样例补回来，所以每次递增 `rev`。
 *
 * @param {any} voice
 */
export function addPickVoice(voice) {
  const id = String(voice?.id || '')
  if (!id) return
  store.pick = {
    voiceId: id,
    file: '',
    mode: 'add',
    rev: (store.pick.rev || 0) + 1,
  }
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
  store.pick = {
    ...store.pick,
    voiceId: id,
    file: String(sample?.file || ''),
    mode: 'add',
    rev: (store.pick.rev || 0) + 1,
  }
}

/** 清掉目标：工作区回到空态。 */
export function clearPick() {
  store.pick = { voiceId: '', file: '', mode: '', rev: (store.pick.rev || 0) + 1 }
}
