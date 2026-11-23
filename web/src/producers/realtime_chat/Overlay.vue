<template>
  <span hidden aria-hidden="true"></span>
</template>

<script setup>
// @ts-check
// 左栏音色卡的动作：`create`（本页的默认）用这条音色新建一个角色；
// `assign`（播放条那条路）把音色交给当前角色。指定的是声音，不是某一条样例；
// 合成时网关会按这条音色的主样例 / 情绪样例自己挑。
import { watch } from 'vue'
import { say, store as hostStore } from '../../store'
import { active, assignVoiceToActive, createFromVoice, open, state } from './store'

watch(
  () => hostStore.assignVoice,
  async (picked) => {
    if (!picked) return
    const mode = hostStore.assignMode
    hostStore.assignVoice = null
    hostStore.assignMode = 'assign'
    try {
      if (!state.loaded) await open()
      if (mode === 'create') {
        const char = createFromVoice(picked)
        if (char) say(`已用「${char.name}」新建角色，补上人设就能开聊。`, { kind: 'success' })
        return
      }
      const char = active.value
      if (!char) {
        say('还没有角色，先新建一个再指定音色。', { kind: 'warn' })
        return
      }
      const engines = Array.isArray(picked.engines) ? picked.engines.filter(Boolean) : []
      if (!engines.length) {
        say('这条音色没有可用的合成引擎。', { kind: 'warn' })
        return
      }
      await assignVoiceToActive(picked)
      const name = picked.shown_name || picked.name || '这条音色'
      say(`已把「${name}」指定给「${char.name}」。`, { kind: 'success' })
    } catch (error) {
      say(
        `${mode === 'create' ? '新建角色' : '指定音色'}失败：${
          error instanceof Error ? error.message : String(error)
        }`,
        { kind: 'error' }
      )
    }
  },
  { immediate: true }
)
</script>