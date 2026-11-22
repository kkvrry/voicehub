<template>
  <VoiceAssignDialog
    :open="open"
    :voice="voice"
    :roles="targets"
    scope="role"
    unit="段"
    :engine="engine"
    :engine-label="engineLabel"
    :assignments="assignments"
    :engines="engines"
    :selection-count="state.selectedIds.length"
    :busy="busy"
    @close="close"
    @assign="submit"
  />
</template>

<script setup>
// @ts-check
// 左栏音色卡的「指定」：复用创作页的目标台账、引擎候选与样例策略，
// 配音这边只补两件工作区自己的事——目标按段计数，选中的段可顺手改角色。
import { computed, ref, watch } from 'vue'
import { say, store as hostStore } from '../../store'
import VoiceAssignDialog from '../../components/VoiceAssignDialog.vue'
import {
  applyRoleToSelection,
  cues,
  patchRole,
  roles,
  state,
} from './store'

const open = ref(false)
const voice = ref(/** @type {any} */ (null))
const busy = ref(false)

const voiceName = computed(() => voice.value?.shown_name || voice.value?.name || '')

/** 可选引擎：由这条音色的 `engines` 声明，名称取宿主下发的引擎表。 */
const engines = computed(() => {
  const list = Array.isArray(voice.value?.engines) ? voice.value.engines : []
  return list.map((/** @type {string} */ key) => {
    const known = (hostStore.engines || []).find((/** @type {any} */ item) => item.key === key)
    return { key, label: known?.label || key }
  })
})

const engine = computed(() => engines.value[0]?.key || '')
const engineLabel = computed(() => engines.value[0]?.label || '')

/** 目标台账：角色名、段数，以及它现在配了哪条音色、用什么样例策略。 */
const targets = computed(() => {
  const fallback = roles.value[0]?.id || ''
  return roles.value.map((/** @type {any} */ role) => ({
    key: role.id,
    name: role.name,
    mode: role.mode,
    rows: cues.value.filter(
      (/** @type {any} */ cue) => (cue.role_id || fallback) === role.id
    ).length,
  }))
})

const assignments = computed(() => {
  const out = /** @type {Record<string, any>} */ ({})
  for (const role of /** @type {any[]} */ (roles.value)) {
    if (!role.voice_id) continue
    out[role.id] = {
      voice_id: role.voice_id || '',
      voice_name: role.voice_name || '',
      engine: role.engine || '',
      sample_mode: role.sample_mode || 'emotion',
      sample_file: role.sample_file || '',
    }
  }
  return out
})

watch(
  () => hostStore.assignVoice,
  (picked) => {
    if (!picked) return
    hostStore.assignVoice = null
    if (!state.currentId || !state.detail) {
      say('还没有打开配音项目：先导入音视频，再给它指定音色。', { kind: 'warn' })
      return
    }
    voice.value = picked
    busy.value = false
    open.value = true
  },
  { immediate: true }
)

function close() {
  if (busy.value) return
  open.value = false
  voice.value = null
}

/**
 * @param {{ key: string, engine: string, sample_mode?: string, sample_file?: string, selection?: boolean }} payload
 */
async function submit(payload) {
  const role = roles.value.find((/** @type {any} */ item) => item.id === payload.key)
  const picked = voice.value
  if (!role || !picked || !payload.engine) return
  busy.value = true
  try {
    // 同一个声音重新指定、而且这个角色原本没选过样例策略：别顺手把"主样例"
    // 改成"按情绪匹配"——那会让所有旧音频白标成待重配。
    const sameVoice =
      role.engine === payload.engine && role.voice_id === picked.id
    const keepLegacy =
      sameVoice &&
      !role.sample_mode &&
      (payload.sample_mode || 'emotion') === 'emotion'
    const sampleMode = keepLegacy ? '' : payload.sample_mode || 'emotion'
    const updated = await patchRole(role, {
      engine: payload.engine,
      voice_id: picked.id,
      voice_name: picked.shown_name || picked.name || '',
      sample_mode: sampleMode,
      sample_file: sampleMode === 'fixed' ? payload.sample_file || '' : '',
    })
    if (!updated) return
    if (payload.selection && state.selectedIds.length) {
      await applyRoleToSelection(role.id)
    }
    say(`已把「${voiceName.value}」指定给「${role.name}」。`, { kind: 'success' })
    open.value = false
    voice.value = null
  } finally {
    busy.value = false
  }
}
</script>
