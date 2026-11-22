// @ts-check
// 音色卡右键菜单的上下文：大卡与小卡共用同一份动作，免得同一条音色
// 在两个地方能做的事不一样。
import { api } from '../api'
import { actions, say, store } from '../store'

/**
 * @param {any} voice
 * @param {{ open?: () => void | Promise<void> }} [extra]
 */
export function voiceMenuContext(voice, extra = {}) {
  return {
    name: voice.shown_name || '',
    folder: voice.folder || '',
    isLocal: Boolean(voice.is_local),
    avatarGenerated: Boolean(voice.avatar_generated),
    hidden: Boolean(voice.hidden),
    sampleCount: Number(voice.sample_count) || 0,
    open: extra.open,
    playAll: () => actions.playAllSamples(voice),
    openFolder: async () => {
      try {
        await api.revealVoice(voice.id)
      } catch (error) {
        say(error instanceof Error ? error.message : String(error))
      }
    },
    pickAvatar: () => {
      store.avatarPick = voice
    },
    toggleHidden: async () => {
      await actions.setPrefs(voice, { hidden: !voice.hidden })
    },
    remove: async () => {
      const where = voice.is_local ? `\n\n${voice.folder}` : ''
      if (!window.confirm(`移除「${voice.shown_name}」？${where}`)) return
      await actions.removeVoice(voice)
    },
  }
}
